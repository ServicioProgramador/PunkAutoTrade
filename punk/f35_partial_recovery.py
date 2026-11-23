"""F35: persistent GET-only partial-fill observation, fail-closed by design.

This is *not* a LIVE executor. A GET observation cannot prove future stop
execution. Every recorded observation has zero send/retry authority.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from punk.live_write_lab import Intent, LabBlocked
from punk.bitunix_ack_journal import AckJournal
from punk.bitunix_write_journal import WriteJournal


def _number(value, allow_zero=False):
    if value is None or isinstance(value, (bool, float)):
        raise LabBlocked('INVALID_EXCHANGE_QUANTITY')
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LabBlocked('INVALID_EXCHANGE_QUANTITY') from exc
    if not n.is_finite() or n < 0 or (n == 0 and not allow_zero):
        raise LabBlocked('INVALID_EXCHANGE_QUANTITY')
    return n


def _view(state, *, observed=None):
    return {'build':'F35', 'state':state, 'observed_fill': observed,
            'mode':'GET_ONLY_PARTIAL_RECOVERY', 'protected':False,
            'allow_send':False, 'allow_retry':False, 'live_enabled':False,
            'write_transport_enabled':False, 'ready_for_live':False, 'orders_sent':0}


class PartialFillRecovery:
    """Records safe *observations* only. Never submits or retries an order."""
    def __init__(self, path):
        self.path = Path(path)
        self.intent_journal = WriteJournal(path)
        self.ack = AckJournal(path)
        con = self._connect()
        try:
            con.execute('''CREATE TABLE IF NOT EXISTS f35_fill_observations (
                client_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                order_id TEXT NOT NULL, cumulative_fill TEXT NOT NULL,
                last_state TEXT NOT NULL, sample_count INTEGER NOT NULL)''')
            con.commit()
        finally:
            con.close()

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=8)
        con.execute('PRAGMA busy_timeout=8000')
        return con

    def inspect(self, intent, *, boundary):
        if type(intent) is not Intent:
            return _view('INVALID_INTENT')
        try:
            intent.validate()
            row = self.intent_journal.read(intent)
            receipt = self.ack.read(intent)
        except LabBlocked:
            return _view('INTENT_CONFLICT')
        if row is None or row['state'] == 'PREPARED':
            return _view('INTENT_NOT_ARMED')
        if receipt is None or receipt['state'] != 'ACK_OBSERVED':
            return _view('ACK_NOT_TRUSTED')
        con = self._connect()
        try:
            receipt_row = con.execute('''SELECT order_id,fingerprint,receipt_state FROM entry_receipts
                WHERE client_id=?''', (intent.client_id(),)).fetchone()
        finally:
            con.close()
        if not receipt_row or receipt_row[1] != intent.fingerprint() or receipt_row[2] != 'ACK_OBSERVED':
            return _view('ACK_NOT_TRUSTED')
        order_id = receipt_row[0]
        try:
            order = boundary.request('GET', 'order_detail', {'clientId':intent.client_id()})
            snapshot = boundary.request('GET','account_snapshot')
        except Exception:
            return _view('GET_UNKNOWN')
        if (not isinstance(order,dict) or order.get('clientId') != intent.client_id()
            or order.get('orderId') != order_id or order.get('symbol') != intent.symbol
            or order.get('side') != ('BUY' if intent.side == 'LONG' else 'SELL')
            or order.get('reduceOnly') is not False):
            return _view('ORDER_IDENTITY_MISMATCH')
        if not isinstance(snapshot,dict) or snapshot.get('status') != 'GET_ONLY_VERIFIED':
            return _view('SNAPSHOT_NOT_VERIFIED')
        positions = snapshot.get('positions')
        protections = snapshot.get('pending_tpsl')
        if not isinstance(positions,list) or not isinstance(protections,list):
            return _view('SNAPSHOT_INCOMPLETE')
        try:
            total = _number(intent.quantity)
            fill = _number(order.get('tradeQty'), allow_zero=True)
            if fill > total:
                return _view('FILL_EXCEEDS_INTENT')
            # Cannot determine a closed/partially closed position without a fill ledger.
            # All open positions for the same symbol/side must be unambiguous.
            matches = [p for p in positions if isinstance(p,dict) and
                       p.get('symbol') == intent.symbol and p.get('side') == intent.side]
            if len(matches) > 1:
                return _view('POSITION_AMBIGUOUS')
            if matches:
                position = matches[0]
                qty = _number(position.get('qty'))
                pos_id = str(position.get('positionId') or '')
                if not pos_id or qty > fill or fill == 0:
                    return _view('POSITION_FILL_INCONSISTENT')
                if not isinstance(position.get('positionId'),(str,int)):
                    return _view('POSITION_ID_INVALID')
                # Conservative coverage test: one full-size market stop, with
                # correct side of entry price, associated with exact position.
                linked = [p for p in protections if isinstance(p,dict) and
                          str(p.get('positionId') or '') == pos_id]
                stops = [p for p in linked if p.get('slQty') is not None]
                coverage = (len(stops) == 1 and
                            _number(stops[0].get('slQty')) == qty and
                            _number(stops[0].get('slPrice')) == _number(intent.stop) and
                            stops[0].get('slOrderType') == 'MARKET' and
                            stops[0].get('slStopType') in ('MARK_PRICE','LAST_PRICE'))
            else:
                qty = Decimal(0)
                coverage = False
        except (LabBlocked, ValueError, TypeError):
            return _view('QUANTITY_OR_STOP_INVALID')
        # Atomic, monotone observations guard against stale or out-of-order GET
        # snapshots. Every SQLite connection is explicitly closed on Windows.
        con = self._connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('''SELECT fingerprint,order_id,cumulative_fill,sample_count
                FROM f35_fill_observations WHERE client_id=?''',(intent.client_id(),)).fetchone()
            if row and (row[0] != intent.fingerprint() or row[1] != order_id):
                con.rollback()
                return _view('JOURNAL_IDENTITY_CONFLICT')
            if row and fill < _number(row[2], allow_zero=True):
                con.rollback()
                return _view('STALE_OR_REGRESSIVE_FILL')
            # Never declare a position protected from a GET snapshot. Even
            # full coverage is only evidence to be rechecked at execution time.
            state = ('NO_FILL_OBSERVED' if fill == 0 else
                     'PARTIAL_FILL_STOP_SNAPSHOT_MATCH' if fill < total and coverage else
                     'PARTIAL_FILL_PROTECTION_UNKNOWN' if fill < total else
                     'FULL_FILL_STOP_SNAPSHOT_MATCH' if coverage else
                     'FULL_FILL_PROTECTION_UNKNOWN')
            con.execute('''INSERT INTO f35_fill_observations
                (client_id,fingerprint,order_id,cumulative_fill,last_state,sample_count)
                VALUES (?,?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET
                cumulative_fill=excluded.cumulative_fill,last_state=excluded.last_state,
                sample_count=f35_fill_observations.sample_count+1''',
                (intent.client_id(),intent.fingerprint(),order_id,str(fill),state,1))
            con.commit()
            return _view(state, observed=str(fill))
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def read(self, intent):
        if type(intent) is not Intent:
            return _view('INVALID_INTENT')
        try:
            intent.validate()
        except LabBlocked:
            return _view('INVALID_INTENT')
        con = self._connect()
        try:
            row = con.execute('''SELECT fingerprint,cumulative_fill,last_state FROM
                f35_fill_observations WHERE client_id=?''',(intent.client_id(),)).fetchone()
        finally:
            con.close()
        if not row:
            return _view('NO_OBSERVATION')
        if row[0] != intent.fingerprint():
            return _view('JOURNAL_IDENTITY_CONFLICT')
        return _view(row[2], observed=row[1])

    def submit(self, *args, **kwargs):
        raise LabBlocked('LIVE_TRANSMISSION_NOT_AUTHORIZED_F35')
