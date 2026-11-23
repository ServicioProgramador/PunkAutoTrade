"""F31: real GET-only evidence wired to durable F29 journal; fail-closed.

No live writer, no API credentials persisted, no resend authority. A pending
SL/TP snapshot match is only observation, not a guarantee of execution.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from punk.bitunix_durable_boundary import DurableHttpsBoundary
from punk.bitunix_protection_audit import audit
from punk.bitunix_reconciliation import ReadOnlyBoundary
from punk.live_write_lab import Intent, LabBlocked


def _fail(code):
    return {'build': 'F31', 'state': code, 'snapshot_match': False,
            'protected': False, 'orders_sent': 0, 'allow_send': False,
            'allow_retry': False, 'live_enabled': False, 'ready_for_live': False}


def _decimal(v):
    try:
        d = Decimal(str(v))
        if d.is_finite() and d > 0:
            return d
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


class DurableGetReconciler:
    def __init__(self, journal_path, *, boundary=None):
        self.durable = DurableHttpsBoundary(journal_path)
        self.boundary = boundary  # allow test doubles; None uses strict real GET-only

    def inspect(self, intent, *, tp_quantities):
        """Read exchange evidence ONLY for a previously armed exact intent.

        tp_quantities must be the actual intended allocation: guessing equal
        allocation would falsely report coverage when partial fills occur.
        """
        if type(intent) is not Intent:
            return _fail('INVALID_INTENT')
        try:
            intent.validate()
            current = self.durable.read(intent)
        except LabBlocked as e:
            # Suppress unexpected details in exception strings.
            return _fail('CLIENT_ID_INTENT_CONFLICT' if str(e) == 'CLIENT_ID_INTENT_CONFLICT' else 'INVALID_INTENT')
        if current is None:
            return _fail('INTENT_NOT_REGISTERED')
        if current['state'] == 'PREPARED':
            return _fail('INTENT_NOT_ARMED')
        if not isinstance(tp_quantities, (list, tuple)) or len(tp_quantities) != len(intent.take_profits):
            return _fail('INVALID_TP_ALLOCATION')
        qtys = [_decimal(x) for x in tp_quantities]
        total = _decimal(intent.quantity)
        if any(x is None for x in qtys) or total is None or sum(qtys, Decimal(0)) != total:
            return _fail('INVALID_TP_ALLOCATION')
        expected = [{'price': str(price), 'qty': str(qty)} for price, qty in zip(intent.take_profits, qtys)]
        try:
            observed = audit(self.boundary if self.boundary is not None else ReadOnlyBoundary(),
                             client_id=intent.client_id(), symbol=intent.symbol, side=intent.side,
                             quantity=str(total), entry_price=intent.entry, sl_price=intent.stop,
                             tp_levels=expected)
            if not isinstance(observed, dict):
                return _fail('GET_AUDIT_UNKNOWN')
            match = (observed.get('state') == 'PENDING_PROTECTION_SNAPSHOT_MATCH' and
                     observed.get('snapshot_coverage_verified') is True)
            # Only a full snapshot match contributes positive evidence. Any
            # other response stays uncertain; never infer non-execution.
            persist = self.durable.reconcile_observation(
                intent, order_observed=bool(match),
                protection_coverage=bool(match), snapshot_verified=bool(match))
        except Exception:
            return _fail('GET_OR_JOURNAL_UNKNOWN')
        return {**_fail('PENDING_SNAPSHOT_MATCH_AUDIT_ONLY' if match else 'EXCHANGE_EVIDENCE_INCOMPLETE'),
                'snapshot_match': bool(match), 'journal_state': persist['state'],
                'observations': persist['observations']}

    def read(self, intent):
        return self.durable.read(intent)

    def submit(self, *_args, **_kwargs):
        raise LabBlocked('LIVE_TRANSMISSION_NOT_AUTHORIZED_F31')
