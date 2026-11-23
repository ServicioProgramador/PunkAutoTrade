"""F25: in-process REST-style write rehearsal, intentionally no HTTP/network.

This module has no Bitunix authenticated writer or live activation path.
All POST-like calls require the exact OfflineBitunixGateway type.
"""
from __future__ import annotations

from punk.bitunix_pretrade_gate import compile_pretrade
from punk.bitunix_write_contract import PATH_PLACE, PATH_TPSL, validate_entry_ack
from punk.bitunix_write_journal import WriteJournal
from punk.live_write_lab import Intent, LabBlocked, TransportUnknown


class OfflineBitunixGateway:
    """Synthetic responses in Bitunix JSON envelope format; no socket access."""
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.protections = {}
        self.timeout_before_entry = False
        self.timeout_after_entry = False
        self.timeout_on_protection = False
        self.reject_protection = False

    def post_simulated(self, path, body):
        if path == PATH_PLACE:
            self.calls.append('ENTRY')
            if self.timeout_before_entry:
                raise TransportUnknown('SYNTHETIC_TIMEOUT')
            cid = body['clientId']
            # Deliberately enforce single order per clientId in the fake exchange.
            if cid not in self.orders:
                self.orders[cid] = {'orderId': 'SYNTHETIC-' + str(len(self.orders)+1),
                                    'clientId': cid, 'symbol': body['symbol']}
            if self.timeout_after_entry:
                raise TransportUnknown('SYNTHETIC_TIMEOUT')
            return {'code': 0, 'data': dict(self.orders[cid])}
        if path == PATH_TPSL:
            self.calls.append('TP')
            if self.timeout_on_protection:
                raise TransportUnknown('SYNTHETIC_TIMEOUT')
            if self.reject_protection:
                return {'code': 41001, 'data': None}
            position = body['positionId']
            self.protections.setdefault(position, []).append(dict(body))
            return {'code': 0, 'data': {'orderId': 'SYNTHETIC-TP'}}
        raise LabBlocked('ENDPOINT_FORBIDDEN')

    def lookup_simulated(self, cid):
        self.calls.append('LOOKUP')
        return self.orders.get(cid)


class WriteTransportRehearsal:
    """F24 verified pretrade -> F22 durable arm -> in-process REST contract.

    A failed or uncertain entry/protection cannot be sent again with this
    journal, even after recreation of this object or the gateway.
    """
    def __init__(self, db_path):
        self.journal = WriteJournal(db_path)

    @staticmethod
    def _result(state, *, stage='OFFLINE', observed=False):
        return {'build': 'F25', 'mode': 'OFFLINE_REST_REHEARSAL', 'state': state,
                'stage': stage, 'synthetic_order_observed': observed,
                'real_orders_sent': 0, 'real_protection_confirmed': False,
                'write_transport_enabled': False, 'live_enabled': False,
                'allow_retry': False, 'ready_for_live': False}

    def run(self, intent, reader, gateway, *, margin_usdt='5', leverage=20, now=None):
        if type(intent) is not Intent or type(gateway) is not OfflineBitunixGateway:
            raise LabBlocked('SYNTHETIC_GATEWAY_REQUIRED')
        # A GET-only snapshot must be complete and fresh before arming.
        preview = compile_pretrade(intent, reader, margin_usdt=margin_usdt,
                                   requested_leverage=leverage, now=now)
        current = self.journal.prepare(intent)
        if current['state'] != 'PREPARED':
            return self._result('NO_RETRY', stage=current['state'])
        armed = self.journal.arm_uncertain_offline(intent)
        if armed['state'] != 'ARMED_UNKNOWN':
            return self._result('NO_RETRY', stage=armed['state'])
        try:
            response = gateway.post_simulated(PATH_PLACE, preview['entry_payload'])
            ack = validate_entry_ack(response, intent.client_id())
        except Exception:
            return self._result('ENTRY_UNKNOWN', stage='SUBMISSION_OR_ACK_UNCERTAIN')
        try:
            # Position identity is not inferred from an entry ack in reality.
            # This is a synthetic position ID used by the fake exchange only.
            position_id = 'SYNTHETIC_POSITION'
            from decimal import Decimal
            count = len(intent.take_profits)
            base = Decimal('1') / Decimal(count)
            weights = tuple(str(base) for _ in range(count-1)) + (str(Decimal('1') - base * (count-1)),)
            # Pretrade validates the actual contract quantity step; synthetic TP
            # payloads are already validated in its preview.
            for tp in preview['tp_payloads']:
                body = dict(tp)
                body['positionId'] = position_id
                reply = gateway.post_simulated(PATH_TPSL, body)
                if type(reply) is not dict or reply.get('code') != 0:
                    raise LabBlocked('SYNTHETIC_PROTECTION_NOT_ACCEPTED')
            installed = gateway.protections.get(position_id, [])
            complete = len(installed) == len(preview['tp_payloads']) and all(
                a == b for a,b in zip(installed, ({**p, 'positionId': position_id} for p in preview['tp_payloads'])))
        except Exception:
            self.journal.observe(intent, order_observed=True, snapshot_verified=True)
            return self._result('PROTECTION_UNKNOWN', observed=True)
        self.journal.observe(intent, order_observed=True,
                             protection_coverage=complete, snapshot_verified=True)
        return self._result('AUDIT_ONLY' if complete else 'PROTECTION_UNKNOWN',
                            stage='SYNTHETIC_FULL_COVERAGE' if complete else 'SYNTHETIC_INCOMPLETE',
                            observed=True)

    def reconcile_simulated(self, intent, gateway):
        if type(gateway) is not OfflineBitunixGateway:
            raise LabBlocked('SYNTHETIC_GATEWAY_REQUIRED')
        state = self.journal.read(intent)
        if not state or state['state'] == 'PREPARED':
            raise LabBlocked('NOT_ARMED')
        try:
            order = gateway.lookup_simulated(intent.client_id())
        except Exception:
            return self._result('ORDER_UNKNOWN')
        if not order or order.get('clientId') != intent.client_id() or order.get('symbol') != intent.symbol:
            return self._result('ORDER_UNKNOWN')
        self.journal.observe(intent, order_observed=True, snapshot_verified=True)
        return self._result('PROTECTION_UNKNOWN', stage='GET_AUDIT_REQUIRED', observed=True)
