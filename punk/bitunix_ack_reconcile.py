"""F33: reconcile durable ACK to GET-only evidence; never transmits an order.

An ACK is merely a receipt. An exact pending TP/SL GET snapshot is an audit
observation, NOT exchange execution assurance nor authority to send/retry.
"""
from __future__ import annotations
import sqlite3
from decimal import Decimal, InvalidOperation
from punk.bitunix_ack_journal import AckJournal
from punk.bitunix_durable_reconcile import DurableGetReconciler
from punk.bitunix_reconciliation import ReadOnlyBoundary
from punk.live_write_lab import Intent, LabBlocked


def _result(state, **extra):
    return {'build':'F33','state':state,'ack_bound':False,'snapshot_match':False,
            'protected':False,'allow_send':False,'allow_retry':False,
            'live_enabled':False,'ready_for_live':False,'orders_sent':0, **extra}


class _OrderBinding:
    """Only passes GETs. Fails a mismatched orderId before any snapshot query."""
    def __init__(self, boundary, order_id):
        self.boundary = boundary
        self.order_id = order_id

    def request(self, method, resource, params=None):
        if method != 'GET' or resource not in ('order_detail','account_snapshot'):
            raise LabBlocked('GET_ONLY')
        if resource == 'order_detail':
            order = self.boundary.request(method, resource, params)
            if not isinstance(order, dict) or order.get('orderId') != self.order_id:
                raise LabBlocked('ACK_ORDER_ID_MISMATCH')
            return order
        return self.boundary.request(method, resource, params)


class AckGetReconciler:
    def __init__(self, path, *, boundary=None):
        self.ack = AckJournal(path)
        self.durable = DurableGetReconciler(path)
        self.boundary = boundary if boundary is not None else ReadOnlyBoundary()

    def _receipt(self, intent):
        # AckJournal.read enforces exact fingerprint. Read order ID separately
        # without publishing it to the dashboard or logs.
        existing = self.ack.read(intent)
        if existing is None or existing['state'] != 'ACK_OBSERVED':
            return None
        con = self.ack._connection()
        try:
            row = con.execute('SELECT order_id,fingerprint,receipt_state FROM entry_receipts WHERE client_id=?',
                              (intent.client_id(),)).fetchone()
            if (row is None or row[1] != intent.fingerprint() or
                    row[2] != 'ACK_OBSERVED' or not row[0]):
                return None
            return row[0]
        finally:
            con.close()

    def inspect(self, intent, *, tp_quantities):
        if type(intent) is not Intent:
            return _result('INVALID_INTENT')
        try:
            intent.validate()
            receipt = self.ack.read(intent)
        except LabBlocked:
            return _result('INTENT_CONFLICT_OR_INVALID')
        if receipt is None:
            return _result('ACK_NOT_REGISTERED')
        if receipt['state'] != 'ACK_OBSERVED':
            return _result('ACK_NOT_TRUSTED')
        # Validate allocations BEFORE network access.
        try:
            expected = Decimal(intent.quantity)
            if (not isinstance(tp_quantities, (list,tuple)) or
                len(tp_quantities) != len(intent.take_profits)):
                return _result('INVALID_TP_ALLOCATION')
            values = [Decimal(str(x)) for x in tp_quantities]
            if any(not v.is_finite() or v <= 0 for v in values) or sum(values) != expected:
                return _result('INVALID_TP_ALLOCATION')
        except (InvalidOperation, TypeError, ValueError):
            return _result('INVALID_TP_ALLOCATION')
        try:
            order_id = self._receipt(intent)
            if not order_id:
                return _result('ACK_NOT_TRUSTED')
            wrapped = _OrderBinding(self.boundary, order_id)
            reconciler = DurableGetReconciler(self.ack.path, boundary=wrapped)
            observed = reconciler.inspect(intent, tp_quantities=tp_quantities)
            match = observed.get('snapshot_match') is True
            return _result('ACK_BOUND_SNAPSHOT_AUDIT_ONLY' if match else 'GET_EVIDENCE_INCOMPLETE',
                           ack_bound=match, snapshot_match=match,
                           journal_state=observed.get('journal_state','UNKNOWN'))
        except Exception:
            return _result('GET_OR_JOURNAL_UNKNOWN')

    def submit(self, *args, **kwargs):
        raise LabBlocked('LIVE_TRANSMISSION_NOT_AUTHORIZED_F33')
