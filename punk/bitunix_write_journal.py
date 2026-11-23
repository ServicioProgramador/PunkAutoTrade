"""F22: durable write-intent gate; no HTTP, credentials, or executable trade transport.

PREPARED is an offline plan. ARMED_UNKNOWN is persisted *before* any future send,
and cannot be converted back to PREPARED. F22 never performs the send itself.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from punk.live_write_lab import Intent, LabBlocked

STATES = frozenset(('PREPARED', 'ARMED_UNKNOWN', 'ENTRY_OBSERVED', 'PROTECTION_UNKNOWN', 'AUDIT_ONLY'))


class WriteJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connection()
        try:
            con.execute('''CREATE TABLE IF NOT EXISTS write_intents (
                client_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                observations INTEGER NOT NULL DEFAULT 0)''')
            con.commit()
        finally:
            con.close()

    def _connection(self):
        con = sqlite3.connect(str(self.path), timeout=8)
        con.execute('PRAGMA busy_timeout=8000')
        return con

    def _transaction(self, fn):
        con = self._connection()
        try:
            con.execute('BEGIN IMMEDIATE')
            result = fn(con)
            con.commit()
            return result
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _identity(intent):
        if type(intent) is not Intent:
            raise LabBlocked('INVALID_INTENT_TYPE')
        intent.validate()
        return intent.client_id(), intent.fingerprint()

    @staticmethod
    def _public(row):
        return {'state': row[0], 'observation_count': row[1],
                'allow_send': False, 'allow_retry': False, 'ready_for_live': False}

    def prepare(self, intent):
        cid, digest = self._identity(intent)
        def fn(con):
            now = int(time.time())
            con.execute('''INSERT OR IGNORE INTO write_intents (
                client_id,signal_id,fingerprint,state,created_at,updated_at)
                VALUES (?,?,?,'PREPARED',?,?)''', (cid, intent.signal_id, digest, now, now))
            row = con.execute('SELECT signal_id,fingerprint,state,observations FROM write_intents WHERE client_id=?', (cid,)).fetchone()
            if row[:2] != (intent.signal_id, digest):
                raise LabBlocked('CLIENT_ID_INTENT_CONFLICT')
            return self._public((row[2], row[3]))
        return self._transaction(fn)

    def arm_uncertain_offline(self, intent):
        """Simulates the durable pre-send boundary. NEVER grants actual send authority."""
        cid, digest = self._identity(intent)
        def fn(con):
            row = con.execute('SELECT signal_id,fingerprint,state,observations FROM write_intents WHERE client_id=?', (cid,)).fetchone()
            if row is None:
                raise LabBlocked('INTENT_NOT_PREPARED')
            if row[:2] != (intent.signal_id,digest):
                raise LabBlocked('CLIENT_ID_INTENT_CONFLICT')
            if row[2] == 'PREPARED':
                con.execute('UPDATE write_intents SET state=?,updated_at=? WHERE client_id=?',
                            ('ARMED_UNKNOWN', int(time.time()),cid))
                return self._public(('ARMED_UNKNOWN',row[3]))
            return self._public((row[2],row[3]))
        return self._transaction(fn)

    def observe(self, intent, *, order_observed=False, protection_coverage=False, snapshot_verified=False):
        """Record sanitized GET-only evidence. Never grants sending or protection authority."""
        cid, digest = self._identity(intent)
        if any(type(v) is not bool for v in (order_observed,protection_coverage,snapshot_verified)):
            raise LabBlocked('INVALID_OBSERVATION')
        def fn(con):
            row = con.execute('SELECT signal_id,fingerprint,state,observations FROM write_intents WHERE client_id=?', (cid,)).fetchone()
            if row is None:
                raise LabBlocked('INTENT_NOT_PREPARED')
            if row[:2] != (intent.signal_id,digest):
                raise LabBlocked('CLIENT_ID_INTENT_CONFLICT')
            old = row[2]
            if old == 'PREPARED':
                raise LabBlocked('NOT_ARMED')
            if snapshot_verified and order_observed:
                state = 'AUDIT_ONLY' if protection_coverage else 'PROTECTION_UNKNOWN'
            elif snapshot_verified and not order_observed:
                state = 'ARMED_UNKNOWN'  # no proof of non-execution
            else:
                state = 'ARMED_UNKNOWN'
            # State can regress to unknown on inconsistent or stale evidence.
            con.execute('UPDATE write_intents SET state=?,updated_at=?,observations=observations+1 WHERE client_id=?',
                        (state,int(time.time()),cid))
            return self._public((state,row[3]+1))
        return self._transaction(fn)

    def read(self, intent):
        cid, digest = self._identity(intent)
        def fn(con):
            row = con.execute('SELECT signal_id,fingerprint,state,observations FROM write_intents WHERE client_id=?', (cid,)).fetchone()
            if row is None: return None
            if row[:2] != (intent.signal_id,digest): raise LabBlocked('CLIENT_ID_INTENT_CONFLICT')
            return self._public((row[2],row[3]))
        return self._transaction(fn)

    def summary(self):
        def fn(con):
            count = con.execute('SELECT COUNT(*) FROM write_intents').fetchone()[0]
            return {'build': 'F22','mode':'OFFLINE_DURABLE_WRITE_GATE',
                    'intents': count,'live_enabled': False,'write_transport_enabled': False,
                    'ready_for_live':False,'allow_retry':False,'orders_sent':0}
        return self._transaction(fn)


def offline_report():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='punk_f22_') as tmp:
        journal=WriteJournal(Path(tmp)/'write_gate.db')
        intent=Intent('f22:rune', 'RUNEUSDT','SHORT','10','0.4785','0.487456',
                      ('0.470246','0.469008','0.467584','0.465947','0.464063'))
        journal.prepare(intent)
        journal.arm_uncertain_offline(intent)
        journal.observe(intent, order_observed=True,protection_coverage=True,snapshot_verified=True)
        return {**journal.summary(), 'intent_state':journal.read(intent)['state'],
                'notice':'SYNTHETIC_EVIDENCE_ONLY_NO_LIVE_AUTHORIZATION'}
