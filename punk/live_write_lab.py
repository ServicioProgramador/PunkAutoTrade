"""F15: isolated, synthetic write-path rehearsal. NEVER sends network requests.

The rehearsal is intentionally not wired to ReadOnlyBitunix, Telegram, or any
real exchange adapter. All execution reports are SIMULATED and never confer
LIVE authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import sqlite3
import threading


class LabBlocked(RuntimeError):
    """A fail-closed attempt, with a fixed, non-sensitive reason code."""


class TransportUnknown(RuntimeError):
    """Simulates an indeterminate exchange result (timeout / connection loss)."""


def positive(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LabBlocked('INVALID_DECIMAL') from exc
    if not d.is_finite() or d <= 0:
        raise LabBlocked('INVALID_DECIMAL')
    return d


@dataclass(frozen=True)
class Intent:
    signal_id: str
    symbol: str
    side: str
    quantity: str
    entry: str
    stop: str
    take_profits: tuple[str, ...]

    def validate(self):
        if not self.signal_id or not self.symbol.endswith('USDT') or self.side not in ('LONG', 'SHORT'):
            raise LabBlocked('INVALID_INTENT')
        if not 1 <= len(self.take_profits) <= 5:
            raise LabBlocked('PROTECTION_REQUIRED')
        qty, price, sl = positive(self.quantity), positive(self.entry), positive(self.stop)
        tps = [positive(v) for v in self.take_profits]
        if len(set(tps)) != len(tps):
            raise LabBlocked('INVALID_TP_LEVELS')
        if self.side == 'LONG':
            valid = sl < price and all(x > price for x in tps) and tps == sorted(tps)
        else:
            valid = sl > price and all(x < price for x in tps) and tps == sorted(tps, reverse=True)
        if not valid or qty <= 0:
            raise LabBlocked('INVALID_PROTECTION_LEVELS')

    def fingerprint(self):
        content='|'.join((self.signal_id,self.symbol,self.side,str(positive(self.quantity)),
                          str(positive(self.entry)),str(positive(self.stop)),
                          *(str(positive(x)) for x in self.take_profits)))
        return hashlib.sha256(content.encode()).hexdigest()

    def client_id(self):
        # deterministic identifier across restarts; no secrets
        digest = hashlib.sha256((self.signal_id + ':' + self.symbol).encode()).hexdigest()[:24]
        return 'PA-LAB-' + digest


class Ledger:
    """Durable synthetic intent log with one transaction per transition.

    Connections are opened and CLOSED on every operation for Windows safety.
    """
    def __init__(self, path):
        self.path = str(path)
        con=sqlite3.connect(self.path,timeout=5)
        try:
            con.execute('PRAGMA busy_timeout=5000')
            con.execute('''CREATE TABLE IF NOT EXISTS live_lab(
                client_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, order_id TEXT NOT NULL DEFAULT '',
                protection_count INTEGER NOT NULL DEFAULT 0
            )''')
            con.commit()
        finally:
            con.close()

    def _run(self, func):
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            con.execute('PRAGMA busy_timeout=5000')
            con.execute('BEGIN IMMEDIATE')
            output = func(con)
            con.commit()
            return output
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def claim(self, intent):
        def action(con):
            con.execute('INSERT OR IGNORE INTO live_lab(client_id,signal_id,fingerprint,state) VALUES (?,?,?,?)',
                        (intent.client_id(), intent.signal_id, intent.fingerprint(), 'CLAIMED'))
            return con.execute('SELECT state,order_id,protection_count,signal_id,fingerprint FROM live_lab WHERE client_id=?',
                               (intent.client_id(),)).fetchone()
        state, order, n, signal_id, fingerprint = self._run(action)
        if fingerprint != intent.fingerprint():
            raise LabBlocked('SIGNAL_REVISION_CONFLICT')
        if signal_id != intent.signal_id:
            raise LabBlocked('CLIENT_ID_COLLISION')
        return state, order, n

    def transition(self, client_id, permitted, state, *, order_id=None, count=None):
        def action(con):
            current = con.execute('SELECT state FROM live_lab WHERE client_id=?', (client_id,)).fetchone()
            if not current or current[0] not in permitted:
                raise LabBlocked('ILLEGAL_STATE_TRANSITION')
            con.execute('UPDATE live_lab SET state=?, order_id=COALESCE(?,order_id), protection_count=COALESCE(?,protection_count) WHERE client_id=?',
                        (state,order_id,count,client_id))
        self._run(action)

    def read(self, client_id):
        def action(con):
            return con.execute('SELECT state,order_id,protection_count FROM live_lab WHERE client_id=?',
                               (client_id,)).fetchone()
        return self._run(action)


class SyntheticExchange:
    """Controlled test double; no HTTP, no key, no signing, no trading methods."""
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.protections = {}
        self.fail_entry_after_accept = False
        self.fail_entry_before_accept = False
        self.fail_protection = False
        self.report_incomplete = False
        self.unknown_lookup = False

    def submit(self, intent):
        self.calls.append('submit')
        cid = intent.client_id()
        if self.fail_entry_before_accept:
            raise TransportUnknown('SYNTHETIC_TIMEOUT')
        self.orders.setdefault(cid, {'order_id':'SYN-' + cid[-10:], 'symbol':intent.symbol,
                                      'side':intent.side, 'qty':str(positive(intent.quantity))})
        if self.fail_entry_after_accept:
            raise TransportUnknown('SYNTHETIC_TIMEOUT')
        return self.orders[cid]['order_id']

    def lookup(self, client_id):
        self.calls.append('lookup')
        if self.unknown_lookup:
            raise TransportUnknown('SYNTHETIC_LOOKUP_TIMEOUT')
        return self.orders.get(client_id)

    def install_protections(self, intent, order_id):
        self.calls.append('install_protections')
        if self.fail_protection:
            raise TransportUnknown('SYNTHETIC_PROTECTION_TIMEOUT')
        self.protections[order_id] = {'sl':intent.stop, 'tps':tuple(intent.take_profits)}

    def verify(self, intent, order_id):
        self.calls.append('verify')
        order=self.orders.get(intent.client_id())
        protection=self.protections.get(order_id)
        if self.report_incomplete:
            return False
        return bool(order and order['order_id']==order_id and order['symbol']==intent.symbol
                    and order['side']==intent.side and order['qty']==str(positive(intent.quantity))
                    and protection and protection['sl']==intent.stop
                    and protection['tps']==tuple(intent.take_profits))


class WriteRehearsal:
    """Strict workflow: uncertain operations block new requests until reconciled."""
    def __init__(self, ledger, exchange):
        if not isinstance(exchange, SyntheticExchange):
            raise LabBlocked('REAL_EXCHANGE_FORBIDDEN')
        self.ledger, self.exchange = ledger, exchange
        self._lock = threading.RLock()

    def submit(self, intent):
        intent.validate()
        cid = intent.client_id()
        with self._lock:
            state, oid, count = self.ledger.claim(intent)
            if state == 'CLAIMED':
                # Mark uncertain BEFORE calling exchange, as crash may occur inside submit.
                self.ledger.transition(cid, ('CLAIMED',), 'ENTRY_UNKNOWN')
                try:
                    oid = self.exchange.submit(intent)
                except TransportUnknown:
                    return {'state':'ENTRY_UNKNOWN','protected':False}
                self.ledger.transition(cid, ('ENTRY_UNKNOWN',), 'ENTRY_CONFIRMED',order_id=oid)
                state = 'ENTRY_CONFIRMED'
            elif state in ('ENTRY_UNKNOWN','PROTECTION_UNKNOWN','ENTRY_CONFIRMED'):
                return {'state':state, 'protected':False}
            elif state=='PROTECTED':
                return self.reconcile(intent)
            else:
                return {'state':state,'protected':False}
            return self._protect(intent, oid)

    def _protect(self, intent, oid):
        cid = intent.client_id()
        self.ledger.transition(cid, ('ENTRY_CONFIRMED',), 'PROTECTION_UNKNOWN')
        try:
            self.exchange.install_protections(intent, oid)
            confirmed = self.exchange.verify(intent, oid)
        except TransportUnknown:
            confirmed = False
        if not confirmed:
            return {'state':'PROTECTION_UNKNOWN','protected':False}
        self.ledger.transition(cid, ('PROTECTION_UNKNOWN',), 'PROTECTED',count=len(intent.take_profits))
        return {'state':'PROTECTED','protected':True}

    def reconcile(self, intent):
        intent.validate()
        cid=intent.client_id()
        with self._lock:
            row=self.ledger.read(cid)
            if row is None:
                return {'state':'NOT_FOUND','protected':False}
            state,oid,_ = row
            if state=='PROTECTED':
                # Even a previously protected record must be checked against exchange state.
                try:
                    confirmed = self.exchange.verify(intent, oid)
                except TransportUnknown:
                    confirmed = False
                if confirmed:
                    return {'state':'PROTECTED','protected':True}
                self.ledger.transition(cid, ('PROTECTED',), 'PROTECTION_UNKNOWN')
                return {'state':'PROTECTION_UNKNOWN','protected':False}
            if state=='ENTRY_UNKNOWN':
                try:
                    observed=self.exchange.lookup(cid)
                except TransportUnknown:
                    return {'state':'ENTRY_UNKNOWN','protected':False}
                if not observed:
                    # Missing lookup does NOT prove rejection. Never retry automatically.
                    return {'state':'ENTRY_UNKNOWN','protected':False}
                if observed.get('symbol')!=intent.symbol or observed.get('side')!=intent.side or observed.get('qty')!=str(positive(intent.quantity)):
                    return {'state':'ENTRY_UNKNOWN','protected':False}
                oid=observed['order_id']
                self.ledger.transition(cid, ('ENTRY_UNKNOWN',),'ENTRY_CONFIRMED',order_id=oid)
                return self._protect(intent,oid)
            if state in ('PROTECTION_UNKNOWN','ENTRY_CONFIRMED'):
                if not oid:
                    return {'state':state,'protected':False}
                try:
                    confirmed = self.exchange.verify(intent,oid)
                except TransportUnknown:
                    confirmed = False
                if confirmed:
                    self.ledger.transition(cid, (state,),'PROTECTED',count=len(intent.take_profits))
                    return {'state':'PROTECTED','protected':True}
                # Unknown protection status is NEVER retried blindly.
                if state=='ENTRY_CONFIRMED':
                    self.ledger.transition(cid, ('ENTRY_CONFIRMED',),'PROTECTION_UNKNOWN')
                return {'state':'PROTECTION_UNKNOWN','protected':False}
            return {'state':state,'protected':False}


def run_lab():
    from tempfile import TemporaryDirectory
    from pathlib import Path
    with TemporaryDirectory(prefix='pa_f15_lab_') as d:
        sim=SyntheticExchange()
        lab=WriteRehearsal(Ledger(Path(d)/'lab.sqlite'),sim)
        intent=Intent('f15-test:1','RUNEUSDT','SHORT','10','0.4785','0.487456',
                      ('0.470246','0.469008','0.467584','0.465947','0.464063'))
        first=lab.submit(intent)
        second=lab.submit(intent)
        return {'build':'F15','mode':'SYNTHETIC_WRITE_LAB','network_calls':0,
                'order_submissions':sim.calls.count('submit'),'protection_verified':first['protected'],
                'duplicate_blocked':sim.calls.count('submit')==1 and second['state']=='PROTECTED',
                'live_enabled':False,'ready_for_live':False}
