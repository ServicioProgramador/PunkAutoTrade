"""F23: integrated offline execution rehearsal; NO exchange write transport.

The only accepted writer is F15's exact SyntheticExchange class. An intent is
persistently armed as uncertain BEFORE simulated submission. Once armed, retries
are forbidden, including across process restarts or network ambiguity.
"""
from __future__ import annotations

from punk.bitunix_write_journal import WriteJournal
from punk.bitunix_write_contract import entry_contract, protection_plan
from punk.live_write_lab import Intent, LabBlocked, SyntheticExchange, TransportUnknown, positive


def _report(state, *, stage='OFFLINE', observed=False, coverage=False):
    return {'build': 'F23', 'mode': 'SYNTHETIC_WRITE_BRIDGE', 'state': state,
            'stage': stage, 'order_observed_synthetic': observed,
            'coverage_observed_synthetic': coverage,
            'exchange_write_calls': 0, 'live_enabled': False,
            'write_transport_enabled': False, 'allow_retry': False,
            'ready_for_live': False}


class OfflineBridge:
    def __init__(self, db_path):
        self.journal = WriteJournal(db_path)

    def preview(self, intent, *, qty_step='0.01', weights=None):
        if type(intent) is not Intent:
            raise LabBlocked('INVALID_INTENT_TYPE')
        entry = entry_contract(intent)
        if weights is None:
            # Avoid binary floats: exact distribution for 1..5 TPs.
            from decimal import Decimal
            count = len(intent.take_profits)
            base = Decimal('1') / Decimal(count)
            weights = tuple(str(base) for _ in range(count - 1)) + (str(Decimal('1') - base * (count-1)),)
        protections = protection_plan(intent, position_id='SYNTHETIC_POSITION',
                                      tp_weights=weights, quantity_step=qty_step)
        return {'entry': entry, 'protection_count': len(protections),
                'sl_required': 'slPrice' in entry, 'live_enabled': False}

    def run_synthetic(self, intent, exchange, *, qty_step='0.01', weights=None):
        """Exactly one synthetic entry attempt after durable arming.

        Unknown outcomes never trigger a resend. Protection installation is
        allowed only after an observed synthetic order identity; its own
        exceptions leave the durable state uncertain.
        """
        if type(exchange) is not SyntheticExchange:
            raise LabBlocked('SYNTHETIC_EXCHANGE_REQUIRED')
        self.preview(intent, qty_step=qty_step, weights=weights)
        current = self.journal.prepare(intent)
        if current['state'] != 'PREPARED':
            return _report('NO_RETRY', stage=current['state'])
        armed = self.journal.arm_uncertain_offline(intent)
        if armed['state'] != 'ARMED_UNKNOWN':
            return _report('NO_RETRY', stage=armed['state'])
        try:
            # This type has no network capability, credentials, or HTTP.
            exchange.submit(intent)
        except TransportUnknown:
            return _report('ENTRY_UNKNOWN', stage='SIMULATED_TIMEOUT')
        except Exception:
            return _report('ENTRY_UNKNOWN', stage='SIMULATED_ERROR')
        return self.reconcile_synthetic(intent, exchange)

    def reconcile_synthetic(self, intent, exchange):
        if type(exchange) is not SyntheticExchange:
            raise LabBlocked('SYNTHETIC_EXCHANGE_REQUIRED')
        current = self.journal.read(intent)
        if current is None or current['state'] == 'PREPARED':
            raise LabBlocked('NOT_ARMED')
        try:
            order = exchange.lookup(intent.client_id())
        except Exception:
            self.journal.observe(intent)
            return _report('ORDER_UNKNOWN', stage='LOOKUP_UNAVAILABLE')
        if not order:
            self.journal.observe(intent, snapshot_verified=True)
            return _report('ORDER_UNKNOWN', stage='NO_CONCLUSIVE_EVIDENCE')
        if (order.get('symbol') != intent.symbol or order.get('side') != intent.side or
                order.get('qty') != str(positive(intent.quantity)) or
                not order.get('order_id')):
            self.journal.observe(intent)
            return _report('IDENTITY_UNCONFIRMED', stage='CONFLICT')
        try:
            exchange.install_protections(intent, order['order_id'])
            verified = exchange.verify(intent, order['order_id'])
        except Exception:
            self.journal.observe(intent, order_observed=True, snapshot_verified=True)
            return _report('PROTECTION_UNKNOWN', observed=True)
        observed = self.journal.observe(intent, order_observed=True,
                      protection_coverage=bool(verified), snapshot_verified=True)
        return _report('AUDIT_ONLY' if verified else 'PROTECTION_UNKNOWN',
                       stage=observed['state'], observed=True, coverage=bool(verified))


def offline_report():
    from tempfile import TemporaryDirectory
    from pathlib import Path
    with TemporaryDirectory(prefix='punk_f23_') as tmp:
        intent = Intent('f23:rune', 'RUNEUSDT', 'SHORT', '10', '0.4785', '0.487456',
                        ('0.470246', '0.469008', '0.467584', '0.465947', '0.464063'))
        bridge = OfflineBridge(Path(tmp) / 'write_gate.db')
        exchange = SyntheticExchange()
        result = bridge.run_synthetic(intent, exchange)
        again = OfflineBridge(Path(tmp) / 'write_gate.db').run_synthetic(intent, exchange)
        return {**result, 'second_attempt': again['state'],
                'synthetic_submit_count': exchange.calls.count('submit'),
                'notice': 'LAB_ONLY_NOT_BITUNIX_LIVE_EVIDENCE'}
