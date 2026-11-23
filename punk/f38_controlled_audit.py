"""F38: connect F37 local-only ACK to F33/F35 GET-only audits.

No LIVE write authority, no implicit retries, and no claim that a pending
stop will execute. Only an exact, previously journalled intention is audited.
"""
from __future__ import annotations
from punk.f37_live_executor import F37OneShot, LocalQAOnlyWire
from punk.f35_partial_recovery import PartialFillRecovery
from punk.bitunix_ack_reconcile import AckGetReconciler
from punk.bitunix_reconciliation import ReadOnlyBoundary
from punk.live_write_lab import Intent, LabBlocked


def _result(state, *, ack_match=False, fill=None):
    return {'build':'F38', 'mode':'LOCAL_QA_GET_AUDIT', 'state':state,
            'ack_snapshot_match':ack_match, 'observed_fill':fill,
            'protected':False, 'allow_send':False, 'allow_retry':False,
            'live_enabled':False, 'write_transport_enabled':False,
            'ready_for_live':False, 'orders_sent':0}


class ControlledAudit:
    def __init__(self, path):
        self.entry = F37OneShot(path)
        self.partial = PartialFillRecovery(path)
        self.path = path

    def local_entry(self, intent, preview, wire):
        """Only exact F37 synthetic wire may be used for laboratory entry."""
        if type(wire) is not LocalQAOnlyWire:
            raise LabBlocked('F38_LOCAL_WIRE_ONLY')
        return self.entry.execute_local_qa(intent, preview, wire)

    def audit(self, intent, *, tp_quantities, boundary):
        """Read-only audit; never automatically resolves an uncertain order."""
        if type(intent) is not Intent or type(boundary) is not ReadOnlyBoundary:
            return _result('INVALID_AUDIT_INPUT')
        try:
            intent.validate()
            ack = AckGetReconciler(self.path, boundary=boundary).inspect(
                intent, tp_quantities=tp_quantities)
        except Exception:
            return _result('ACK_GET_UNKNOWN')
        if not ack.get('snapshot_match'):
            return _result('ACK_OR_TPSL_NOT_VERIFIED')
        try:
            partial = self.partial.inspect(intent, boundary=boundary)
        except Exception:
            return _result('FILL_GET_UNKNOWN', ack_match=True)
        state = partial.get('state')
        if state not in ('PARTIAL_FILL_STOP_SNAPSHOT_MATCH', 'FULL_FILL_STOP_SNAPSHOT_MATCH'):
            return _result('PARTIAL_FILL_OR_SL_UNCERTAIN', ack_match=True)
        return _result('CONSISTENT_GET_SNAPSHOT_AUDIT_ONLY',
                       ack_match=True, fill=partial.get('observed_fill'))

    def submit_live(self, *_args, **_kwargs):
        raise LabBlocked('F38_LIVE_TRANSMISSION_DISABLED')
