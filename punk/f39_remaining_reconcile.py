"""F39: conservative GET-only reconciliation of residual exposure/SL/TP.

The adapter must provide a normalized execution_ledger on account_snapshot;
Bitunix GET-only adapter currently does NOT populate this field. Missing ledger =>
UNVERIFIED, never infer close fills from differences between snapshots.
No LIVE authority is exposed, and only an exact journalled ACK is accepted.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

from punk.live_write_lab import Intent, LabBlocked
from punk.bitunix_ack_journal import AckJournal
from punk.bitunix_reconciliation import ReadOnlyBoundary


def _d(v, zero=False):
    if v is None or isinstance(v, (bool, float)):
        raise ValueError('QUANTITY_INVALID')
    try:
        n = Decimal(str(v))
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise ValueError('QUANTITY_INVALID') from exc
    if not n.is_finite() or (n < 0 if zero else n <= 0):
        raise ValueError('QUANTITY_INVALID')
    return n


def _report(state, *, filled=None, open_qty=None, closed_qty=None):
    return dict(build='F39', mode='GET_ONLY_REMAINING_AUDIT', state=state,
                observed_fill=filled, observed_open=open_qty, observed_closed=closed_qty,
                snapshot_match=state in ('RESIDUAL_COVERAGE_SNAPSHOT_MATCH', 'CLOSED_LEDGER_SNAPSHOT_MATCH'),
                protected=False, allow_send=False, allow_retry=False, live_enabled=False,
                write_transport_enabled=False, ready_for_live=False, orders_sent=0)


class RemainingReconciler:
    def __init__(self, path):
        self.path = Path(path)
        self.ack = AckJournal(path)
        con = self._connect()
        try:
            con.execute('''CREATE TABLE IF NOT EXISTS f39_remaining_audit (
                client_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                order_id TEXT NOT NULL, filled TEXT NOT NULL, closed TEXT NOT NULL,
                ledger_json TEXT NOT NULL, observation_count INTEGER NOT NULL)''')
            con.commit()
        finally:
            con.close()

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=8)
        con.execute('PRAGMA busy_timeout=8000')
        return con

    def inspect(self, intent, *, tp_quantities, boundary):
        if type(intent) is not Intent or type(boundary) is not ReadOnlyBoundary:
            return _report('INVALID_AUDIT_INPUT')
        try:
            intent.validate()
            receipt = self.ack.read(intent)
            if receipt is None or receipt['state'] != 'ACK_OBSERVED':
                return _report('ACK_NOT_TRUSTED')
            con = self._connect()
            try:
                row = con.execute('''SELECT order_id,fingerprint FROM entry_receipts
                                     WHERE client_id=?''', (intent.client_id(),)).fetchone()
            finally:
                con.close()
            if not row or row[1] != intent.fingerprint() or not row[0]:
                return _report('ACK_NOT_TRUSTED')
            order_id = row[0]
            expected_total = _d(intent.quantity)
            if not isinstance(tp_quantities, (list, tuple)) or len(tp_quantities) != len(intent.take_profits):
                return _report('INVALID_TP_PLAN')
            intended = {_d(p): _d(q) for p, q in zip(intent.take_profits, tp_quantities)}
            if len(intended) != len(intent.take_profits) or sum(intended.values()) != expected_total:
                return _report('INVALID_TP_PLAN')
        except (LabBlocked, ValueError, TypeError):
            return _report('INTENT_OR_PLAN_CONFLICT')

        try:
            order = boundary.request('GET', 'order_detail', {'clientId': intent.client_id()})
            if (not isinstance(order, dict) or order.get('orderId') != order_id or
                order.get('clientId') != intent.client_id() or order.get('symbol') != intent.symbol or
                order.get('side') != ('BUY' if intent.side == 'LONG' else 'SELL') or
                order.get('reduceOnly') is not False or
                order.get('status') not in ('PART_FILLED', 'FILLED')):
                return _report('ORDER_IDENTITY_OR_STATE_UNKNOWN')
            fill = _d(order.get('tradeQty'), zero=True)
            if fill > expected_total:
                return _report('FILL_EXCEEDS_INTENT')
            snap = boundary.request('GET', 'account_snapshot')
        except Exception:
            return _report('GET_UNKNOWN')
        if not isinstance(snap, dict) or snap.get('status') != 'GET_ONLY_VERIFIED' or not all(
                isinstance(snap.get(k), list) for k in ('positions', 'pending_tpsl', 'execution_ledger')):
            return _report('CLOSE_EXECUTION_LEDGER_UNAVAILABLE')

        try:
            # A trade execution ledger (not price ticks or guessed position deltas)
            # is necessary to attribute closures to this exact entry/position.
            ledger = snap['execution_ledger']
            if len(ledger) > 5000 or len(snap['pending_tpsl']) > 5000:
                return _report('SNAPSHOT_TOO_LARGE')
            matches = [p for p in snap['positions'] if isinstance(p, dict) and
                       p.get('symbol') == intent.symbol and p.get('side') == intent.side]
            if len(matches) > 1:
                return _report('POSITION_AMBIGUOUS')
            if matches:
                pos = matches[0]
                pos_id = str(pos.get('positionId') or '')
                open_qty = _d(pos.get('qty'))
                if not pos_id:
                    return _report('POSITION_IDENTITY_UNKNOWN')
            else:
                # Cannot attribute fills to a closed position unless the ledger
                # explicitly contains its position id in every execution.
                pos_id = ''
                open_qty = Decimal(0)
            executions = {}
            closed_per_price = {p: Decimal(0) for p in intended}
            seen_pos = set()
            for item in ledger:
                if not isinstance(item, dict) or set(item) != {'executionId', 'entryClientId', 'positionId', 'symbol', 'side', 'kind', 'price', 'qty'}:
                    return _report('LEDGER_RECORD_INVALID')
                if item['entryClientId'] != intent.client_id():
                    continue
                eid = item['executionId']
                pid = str(item['positionId'] or '')
                if (not isinstance(eid, str) or not 1 <= len(eid) <= 128 or not pid or
                    item['symbol'] != intent.symbol or item['side'] != intent.side or item['kind'] != 'TP'):
                    return _report('LEDGER_IDENTITY_CONFLICT')
                price, qty = _d(item['price']), _d(item['qty'])
                if price not in intended or eid in executions:
                    return _report('LEDGER_DUPLICATE_OR_UNKNOWN_TP')
                executions[eid] = {'positionId':pid,'price':str(price),'qty':str(qty)}
                seen_pos.add(pid)
                closed_per_price[price] += qty
            if len(seen_pos) != 1:
                return _report('LEDGER_POSITION_LINK_UNVERIFIED')
            ledger_pos_id = next(iter(seen_pos))
            if pos_id and pos_id != ledger_pos_id:
                return _report('LEDGER_POSITION_LINK_UNVERIFIED')
            if not pos_id:
                pos_id = ledger_pos_id
            closed_qty = sum(closed_per_price.values(), Decimal(0))
            if fill == 0 or fill != closed_qty + open_qty:
                return _report('FILL_EXPOSURE_LEDGER_MISMATCH')
            if any(closed_per_price[p] > intended[p] for p in intended):
                return _report('TP_OVERFILLED')
            relevant = [r for r in snap['pending_tpsl'] if isinstance(r, dict) and
                        str(r.get('positionId') or '') == pos_id]
            if any(r.get('symbol') != intent.symbol for r in relevant):
                return _report('PROTECTION_LINK_MISMATCH')
            ids = [r.get('id') for r in relevant]
            if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
                return _report('PROTECTION_IDENTITIES_INVALID')
            if open_qty == 0:
                if relevant:
                    return _report('CLOSED_POSITION_HAS_PENDING_PROTECTIONS')
                state = 'CLOSED_LEDGER_SNAPSHOT_MATCH'
            else:
                stops = [r for r in relevant if r.get('slQty') is not None]
                targets = [r for r in relevant if r.get('tpQty') is not None]
                if len(stops) != 1 or len(targets) > len(intended):
                    return _report('STOP_OR_TP_COUNT_UNVERIFIED')
                sl = stops[0]
                stop_price = _d(sl.get('slPrice'))
                initial_stop = _d(intent.stop)
                if (_d(sl.get('slQty')) != open_qty or sl.get('slOrderType') != 'MARKET' or
                    sl.get('slStopType') not in ('MARK_PRICE','LAST_PRICE') or
                    (stop_price < initial_stop if intent.side == 'LONG' else stop_price > initial_stop)):
                    return _report('STOP_REMAINING_COVERAGE_UNKNOWN')
                if any('tpQty' in r for r in stops) or any('slQty' in r for r in targets):
                    return _report('MIXED_PROTECTION_RECORD')
                remaining = {p:Decimal(0) for p in intended}
                for r in targets:
                    p, q = _d(r.get('tpPrice')), _d(r.get('tpQty'))
                    if p not in remaining or remaining[p] != 0 or r.get('tpOrderType') != 'MARKET' or r.get('tpStopType') not in ('MARK_PRICE','LAST_PRICE'):
                        return _report('TP_REMAINING_COVERAGE_UNKNOWN')
                    remaining[p] = q
                if sum(remaining.values(), Decimal(0)) != open_qty or any(
                    remaining[p] + closed_per_price[p] > intended[p] for p in intended):
                    return _report('TP_REMAINING_COVERAGE_UNKNOWN')
                state = 'RESIDUAL_COVERAGE_SNAPSHOT_MATCH'
        except (ValueError, TypeError):
            return _report('SNAPSHOT_QUANTITY_INVALID')

        # Atomic monotone journal. Every observed execution ID and its details
        # are immutable; disappearances or changes cause a permanent fail-closed
        # observation (never grant any send or retry authority).
        con = self._connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            prev = con.execute('''SELECT fingerprint,order_id,filled,closed,ledger_json,observation_count
                FROM f39_remaining_audit WHERE client_id=?''',(intent.client_id(),)).fetchone()
            if prev:
                if prev[0] != intent.fingerprint() or prev[1] != order_id:
                    con.rollback();return _report('JOURNAL_IDENTITY_CONFLICT')
                old = json.loads(prev[4])
                if (_d(prev[2],zero=True) > fill or _d(prev[3],zero=True) > closed_qty or
                    any(eid not in executions or executions[eid] != record for eid,record in old.items())):
                    con.rollback();return _report('STALE_OR_MUTATED_EXECUTION_LEDGER')
            con.execute('''INSERT INTO f39_remaining_audit
                (client_id,fingerprint,order_id,filled,closed,ledger_json,observation_count)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET
                filled=excluded.filled,closed=excluded.closed,ledger_json=excluded.ledger_json,
                observation_count=f39_remaining_audit.observation_count+1''',
                (intent.client_id(),intent.fingerprint(),order_id,str(fill),str(closed_qty),
                 json.dumps(executions,sort_keys=True),1))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
        return _report(state,filled=str(fill),open_qty=str(open_qty),closed_qty=str(closed_qty))

    def read(self, intent):
        if type(intent) is not Intent:
            return _report('INVALID_AUDIT_INPUT')
        try:
            intent.validate()
            con=self._connect()
            try:
                row=con.execute('SELECT fingerprint,filled,closed FROM f39_remaining_audit WHERE client_id=?',
                                (intent.client_id(),)).fetchone()
            finally:con.close()
        except (LabBlocked,ValueError):
            return _report('INTENT_OR_PLAN_CONFLICT')
        if not row:
            return _report('NO_OBSERVATION')
        if row[0]!=intent.fingerprint():
            return _report('JOURNAL_IDENTITY_CONFLICT')
        return _report('PERSISTED_AUDIT_ONLY',filled=row[1],closed_qty=row[2])

    def submit_live(self,*_args,**_kwargs):
        raise LabBlocked('F39_LIVE_DISABLED')
