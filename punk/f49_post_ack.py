"""F49: F48 durable lab ACK -> GET-only reconciliation -> partial-fill audit.

Never sends a production order. GET matches are observations, not guaranteed
executable protections or permission to open/retry a position.
"""
from __future__ import annotations

from punk.f48_one_shot import F48Commissioning, F48LocalWire
from punk.bitunix_ack_reconcile import AckGetReconciler
from punk.f35_partial_recovery import PartialFillRecovery
from punk.bitunix_reconciliation import ReadOnlyBoundary
from punk.live_write_lab import Intent, LabBlocked


def _report(state, *, ack_match=False, observed_fill=None):
    return {'build': 'F49', 'mode': 'LOCAL_ACK_GET_ONLY_RECONCILIATION',
            'state': state, 'ack_snapshot_match': bool(ack_match),
            'observed_fill': observed_fill, 'protected': False,
            'ready_for_live': False, 'live_enabled': False,
            'write_transport_enabled': False, 'allow_retry': False,
            'orders_sent': 0}


class F49PostAck:
    """One lab entry, then read-only audit of the *same* durable identity.

    An inconclusive GET can never create authority to resubmit. The separate
    F35 tracker is only called after F33 establishes an exact ACK / snapshot.
    """
    def __init__(self, journal_path):
        self.entry = F48Commissioning(journal_path)
        self.journal_path = journal_path

    def prepare_local(self, intent, preview, wire):
        # Preserve F48's exact-type local-wire only boundary, including arm-before-I/O.
        return self.entry.commission(intent, preview, wire)

    def audit(self, intent, *, tp_quantities, boundary):
        if type(intent) is not Intent or type(boundary) is not ReadOnlyBoundary:
            return _report('INVALID_AUDIT_INPUT')
        try:
            intent.validate()
            reconciled = AckGetReconciler(self.journal_path, boundary=boundary).inspect(
                intent, tp_quantities=tp_quantities)
        except Exception:
            return _report('ACK_GET_UNKNOWN')
        if reconciled.get('snapshot_match') is not True or reconciled.get('ack_bound') is not True:
            return _report('ACK_OR_TPSL_NOT_VERIFIED')
        try:
            partial = PartialFillRecovery(self.journal_path).inspect(intent, boundary=boundary)
        except Exception:
            return _report('PARTIAL_FILL_GET_UNKNOWN', ack_match=True)
        state = partial.get('state')
        if state not in ('PARTIAL_FILL_STOP_SNAPSHOT_MATCH', 'FULL_FILL_STOP_SNAPSHOT_MATCH'):
            return _report('PARTIAL_FILL_OR_STOP_UNCERTAIN', ack_match=True)
        return _report('ACK_FILL_STOP_GET_AUDIT_ONLY', ack_match=True,
                       observed_fill=partial.get('observed_fill'))

    def submit_live(self, *args, **kwargs):
        raise LabBlocked('F49_LIVE_DISABLED')


def lab_report():
    """Fully synthetic demonstration in a disposable SQLite database."""
    import tempfile
    from pathlib import Path
    from punk.bitunix_write_contract import entry_contract, protection_plan
    from punk.bitunix_reconciliation import ReadOnlyBoundary
    from punk.live_write_lab import Intent

    class SyntheticGet:
        def __init__(self, intent):
            self.order = {'orderId': 'LAB-F48-ONLY', 'clientId': intent.client_id(),
                          'symbol': intent.symbol, 'side': 'SELL', 'status': 'FILLED',
                          'tradeQty': '10', 'reduceOnly': False}
            self.account = {'status': 'GET_ONLY_VERIFIED',
                'positions': [{'symbol': intent.symbol, 'side': 'SHORT',
                               'positionId': 'LAB-POSITION', 'qty': '10'}],
                'pending_tpsl': [
                    {'id': 'SL', 'positionId': 'LAB-POSITION', 'symbol': intent.symbol,
                     'slPrice': intent.stop, 'slQty': '10', 'slStopType': 'MARK_PRICE',
                     'slOrderType': 'MARKET'}] + [
                    {'id': f'TP{n}', 'positionId': 'LAB-POSITION', 'symbol': intent.symbol,
                     'tpPrice': price, 'tpQty': '2', 'tpStopType': 'MARK_PRICE',
                     'tpOrderType': 'MARKET'}
                    for n, price in enumerate(intent.take_profits, 1)]}

        def get(self, name, params):
            return self.order

        def snapshot(self):
            return self.account

    with tempfile.TemporaryDirectory(prefix='punk_f49_') as folder:
        intent = Intent('f49:synthetic:rune', 'RUNEUSDT', 'SHORT', '10', '0.4785',
                        '0.487456', ('0.470246', '0.469008', '0.467584',
                                     '0.465947', '0.464063'))
        preview = {'state': 'PRETRADE_PREVIEW_ONLY', 'entry_payload': entry_contract(intent),
                   'symbol': intent.symbol, 'snapshot_get_only_verified': True,
                   'sl_required': True, 'tp_count': 5,
                   'tp_payloads': protection_plan(intent, position_id='LAB-POSITION',
                                     tp_weights=('0.2',)*5, quantity_step='0.01'),
                   'required_reserve_usdt': '5.1', 'request_sent': False,
                   'write_transport_enabled': False, 'live_enabled': False,
                   'ready_for_live': False, 'allow_retry': False}
        db = Path(folder) / 'f49.db'
        wire = F48LocalWire()
        first = F49PostAck(db).prepare_local(intent, preview, wire)
        audited = F49PostAck(db).audit(intent, tp_quantities=('2',)*5,
                                      boundary=ReadOnlyBoundary(SyntheticGet(intent)))
        replay = F49PostAck(db).prepare_local(intent, preview, wire)
        return {**audited, 'initial_state': first['state'],
                'replay_state': replay['state'], 'qa_wire_calls': wire.calls,
                'real_orders_sent': 0}
