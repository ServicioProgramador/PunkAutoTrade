"""F20 integrated OFFLINE rehearsal of contracts, simulated writes and GET-only audit.

There is no HTTP transport in this module. Only SyntheticExchange is accepted.
An exchange snapshot matching its planned TP/SL levels is evidence in a TEST
DOUBLE, never a guarantee of real protections or permission to enable LIVE.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from punk.live_write_lab import Intent, Ledger, SyntheticExchange, WriteRehearsal, LabBlocked
from punk.bitunix_write_contract import entry_contract, protection_plan
from punk.bitunix_recovery import RecoveryJournal, recover
from punk.bitunix_reconciliation import ReadOnlyBoundary


class SyntheticGetAdapter:
    """Translates synthetic fills into the schema expected by F18 GET audit.

    This is test-only: it never calls the network and cannot accept a real client.
    """
    def __init__(self, exchange: SyntheticExchange, intent: Intent, tp_weights, step):
        if type(exchange) is not SyntheticExchange:
            raise LabBlocked('SYNTHETIC_ONLY')
        self.exchange, self.intent = exchange, intent
        self.weights, self.step = tp_weights, step
        self.read_calls = 0
        self.snapshot_timeout = False
        self.omit_sl = False
        self.drop_tp = False
        self.conflict_qty = False

    @property
    def position_id(self):
        return 'SYN-POS-' + self.intent.client_id()[-12:]

    def get(self, endpoint, params):
        if endpoint != 'order_detail' or params != {'clientId': self.intent.client_id()}:
            raise LabBlocked('GET_ONLY_ALLOWLIST')
        self.read_calls += 1
        order = self.exchange.orders.get(self.intent.client_id())
        if not order:
            return {}
        return {'clientId': self.intent.client_id(), 'symbol': self.intent.symbol,
                'orderId': order['order_id'], 'status': 'FILLED',
                'tradeQty': order['qty'], 'side': 'BUY' if self.intent.side == 'LONG' else 'SELL',
                'reduceOnly': False}

    def snapshot(self):
        self.read_calls += 1
        if self.snapshot_timeout:
            raise TimeoutError('SYNTHETIC_TIMEOUT')
        oid = self.exchange.orders.get(self.intent.client_id(), {}).get('order_id')
        if not oid:
            return {'status': 'GET_ONLY_VERIFIED', 'positions': [], 'pending_tpsl': []}
        qty = str(self.intent.quantity)
        protections = self.exchange.protections.get(oid)
        records = []
        if protections:
            if not self.omit_sl:
                records.append({'id': 'SYN-SL', 'positionId': self.position_id,
                                'symbol': self.intent.symbol, 'slQty': qty,
                                'slPrice': self.intent.stop, 'slStopType': 'MARK_PRICE',
                                'slOrderType': 'MARKET'})
            plan = protection_plan(self.intent, position_id=self.position_id,
                                   tp_weights=self.weights, quantity_step=self.step)
            for i, tp in enumerate(plan):
                records.append({'id': f'SYN-TP-{i}', **tp})
            if self.drop_tp:
                records.pop()
        return {'status': 'GET_ONLY_VERIFIED',
                'positions': [{'positionId': self.position_id, 'symbol': self.intent.symbol,
                               'side': self.intent.side,
                               'qty': str(Decimal(qty) + (Decimal('1') if self.conflict_qty else 0))}],
                'pending_tpsl': records}

    def post(self, *_args, **_kwargs):
        raise LabBlocked('LIVE_WRITE_TRANSPORT_DISABLED')


def _intent_for_audit(intent, plan):
    return {'client_id': intent.client_id(), 'symbol': intent.symbol, 'side': intent.side,
            'quantity': intent.quantity, 'entry_price': intent.entry,
            'sl_price': intent.stop,
            'tp_levels': [{'price': row['tpPrice'], 'qty': row['tpQty']} for row in plan]}


class IntegratedLab:
    def __init__(self, directory, intent=None, exchange=None):
        self.intent = intent or Intent('f20:rune', 'RUNEUSDT', 'SHORT', '10', '0.4785',
                    '0.487456', ('0.470246', '0.469008', '0.467584', '0.465947', '0.464063'))
        self.exchange = exchange if exchange is not None else SyntheticExchange()
        if type(self.exchange) is not SyntheticExchange:
            raise LabBlocked('REAL_EXCHANGE_FORBIDDEN')
        self.weights = ('0.2',) * 5
        self.step = '0.01'
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(directory / 'synthetic_entry.db')
        self.journal = RecoveryJournal(directory / 'synthetic_audit.db')
        self.runner = WriteRehearsal(self.ledger, self.exchange)
        self.source = SyntheticGetAdapter(self.exchange, self.intent, self.weights, self.step)

    def validate_contracts(self):
        self.intent.validate()
        entry = entry_contract(self.intent)
        plan = protection_plan(self.intent, position_id=self.source.position_id,
                               tp_weights=self.weights, quantity_step=self.step)
        if entry.get('slPrice') != self.intent.stop or sum(Decimal(x['tpQty']) for x in plan) != Decimal(self.intent.quantity):
            raise LabBlocked('CONTRACT_COVERAGE_FAILURE')
        return plan

    def execute_synthetic(self):
        self.validate_contracts()
        return self.runner.submit(self.intent)

    def audit_synthetic(self):
        plan = self.validate_contracts()
        boundary = ReadOnlyBoundary(self.source)
        return recover(self.journal, _intent_for_audit(self.intent, plan), boundary)

    def reconcile_synthetic(self):
        return self.runner.reconcile(self.intent)

    def summary(self):
        first = self.execute_synthetic()
        rec = self.audit_synthetic()
        again = self.execute_synthetic()
        return {'build': 'F20', 'mode': 'SYNTHETIC_INTEGRATION_ONLY',
                'entry_state': first['state'], 'synthetic_protection_reported': bool(first['protected']),
                'audit_state': rec['state'], 'snapshot_coverage_verified_in_lab': rec['snapshot_coverage_verified'],
                'repeat_state': again['state'], 'synthetic_submission_count': self.exchange.calls.count('submit'),
                'network_calls': 0, 'live_enabled': False, 'write_transport_enabled': False,
                'ready_for_live': False, 'protected_in_real_exchange': False, 'allow_retry': False}


def run_lab():
    with TemporaryDirectory(prefix='punk_f20_') as path:
        return IntegratedLab(path).summary()
