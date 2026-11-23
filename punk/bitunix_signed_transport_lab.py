"""F26: signed HTTP-wire rehearsal, OFFLINE ONLY.

No HTTP opener, network socket, live credentials, or LIVE switch exists here.
The wire verifies exact body bytes and Bitunix signatures before responding with
synthetic acknowledgments. No acceptance is a real fill or protection guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json

from punk.bitunix_pretrade_gate import compile_pretrade
from punk.bitunix_write_contract import PATH_PLACE, PATH_TPSL, _canonical, signature, validate_entry_ack
from punk.bitunix_write_journal import WriteJournal
from punk.live_write_lab import Intent, LabBlocked, TransportUnknown


@dataclass(frozen=True)
class SignedOfflineRequest:
    method: str
    path: str
    body: bytes
    signature: str
    nonce: str
    timestamp: str
    # Synthetic signatures only; never keeps real API secrets or credentials.


def compile_signed_request(path, payload, *, nonce='offline-nonce', timestamp='1800000001000'):
    if path not in (PATH_PLACE, PATH_TPSL):
        raise LabBlocked('WRITE_PATH_NOT_ALLOWLISTED')
    if type(payload) is not dict:
        raise LabBlocked('INVALID_WIRE_PAYLOAD')
    text = _canonical(payload)
    sig = signature('F26_SYNTHETIC_KEY', 'F26_SYNTHETIC_SECRET', nonce, timestamp, text)
    return SignedOfflineRequest('POST', path, text.encode('utf-8'), sig, nonce, timestamp)


class OfflineSignedWire:
    """Exact class required. No opener/URL/requests/socket methods."""
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.tps = {}
        self.timeout_entry_before = False
        self.timeout_entry_after = False
        self.timeout_tp_after = False
        self.reject_tp = False
        self.tamper_response_client_id = False

    def exchange(self, request):
        if type(request) is not SignedOfflineRequest or request.method != 'POST' or request.path not in (PATH_PLACE, PATH_TPSL):
            raise LabBlocked('INVALID_OFFLINE_WIRE_REQUEST')
        try:
            decoded = request.body.decode('utf-8')
            payload = json.loads(decoded)
        except (ValueError, UnicodeError):
            raise LabBlocked('INVALID_WIRE_BODY') from None
        if type(payload) is not dict or _canonical(payload).encode('utf-8') != request.body:
            raise LabBlocked('NONCANONICAL_WIRE_BODY')
        expected = signature('F26_SYNTHETIC_KEY','F26_SYNTHETIC_SECRET', request.nonce, request.timestamp, decoded)
        if expected != request.signature:
            raise LabBlocked('WIRE_SIGNATURE_MISMATCH')
        if request.path == PATH_PLACE:
            if 'slPrice' not in payload or payload.get('slOrderType') != 'MARKET':
                raise LabBlocked('STOP_REQUIRED')
            self.calls.append(('ENTRY', payload['clientId']))
            if self.timeout_entry_before:
                raise TransportUnknown('SYNTHETIC_TIMEOUT')
            cid = payload['clientId']
            self.orders.setdefault(cid, {'orderId': f'OFFLINE-{len(self.orders)+1}', 'clientId': cid,
                                         'symbol': payload['symbol']})
            if self.timeout_entry_after:
                raise TransportUnknown('SYNTHETIC_TIMEOUT')
            result = dict(self.orders[cid])
            if self.tamper_response_client_id:
                result['clientId'] = 'INCONSISTENT'
            return {'code': 0, 'data': result}
        if not payload.get('positionId') or not payload.get('tpPrice') or not payload.get('tpQty'):
            raise LabBlocked('INVALID_PROTECTION_REQUEST')
        self.calls.append(('TP', payload['tpPrice']))
        if self.reject_tp:
            return {'code': 41001, 'data': None}
        self.tps.setdefault(payload['positionId'], []).append(dict(payload))
        if self.timeout_tp_after:
            raise TransportUnknown('SYNTHETIC_TIMEOUT')
        return {'code': 0, 'data': {'orderId': 'OFFLINE-TP'}}


class SignedWriteRehearsal:
    """F24 preflight + F22 durable arm + signed, entirely local HTTP wire."""
    def __init__(self, db_path):
        self.journal = WriteJournal(db_path)

    @staticmethod
    def report(state, stage='OFFLINE'):
        return {'build': 'F26', 'state': state, 'stage': stage,
                'mode': 'SIGNED_HTTP_WIRE_OFFLINE', 'real_orders_sent': 0,
                'live_enabled': False, 'write_transport_enabled': False,
                'ready_for_live': False, 'allow_retry': False,
                'real_protection_confirmed': False}

    def run(self, intent, reader, wire, *, margin_usdt='5', leverage=20, now=None):
        if type(intent) is not Intent or type(wire) is not OfflineSignedWire:
            raise LabBlocked('OFFLINE_WIRE_REQUIRED')
        preview = compile_pretrade(intent, reader, margin_usdt=margin_usdt,
                                   requested_leverage=leverage, now=now)
        state = self.journal.prepare(intent)
        if state['state'] != 'PREPARED':
            return self.report('NO_RETRY', state['state'])
        if self.journal.arm_uncertain_offline(intent)['state'] != 'ARMED_UNKNOWN':
            return self.report('NO_RETRY')
        try:
            ack = wire.exchange(compile_signed_request(PATH_PLACE, preview['entry_payload']))
            validate_entry_ack(ack, intent.client_id())
        except Exception:
            return self.report('ENTRY_UNKNOWN', 'SEND_OR_ACK_INCONCLUSIVE')
        try:
            pid = 'OFFLINE_POSITION'
            expected = [{**p, 'positionId': pid} for p in preview['tp_payloads']]
            for p in expected:
                response = wire.exchange(compile_signed_request(PATH_TPSL, p))
                if type(response) is not dict or type(response.get('code')) is not int or response['code'] != 0:
                    raise LabBlocked('PROTECTION_ACK_UNCONFIRMED')
            if wire.tps.get(pid) != expected:
                raise LabBlocked('SYNTHETIC_PROTECTION_MISMATCH')
        except Exception:
            self.journal.observe(intent, order_observed=True, snapshot_verified=True)
            return self.report('PROTECTION_UNKNOWN', 'GET_RECONCILIATION_REQUIRED')
        self.journal.observe(intent, order_observed=True, snapshot_verified=True,
                             protection_coverage=True)
        return self.report('AUDIT_ONLY', 'SYNTHETIC_FULL_COVERAGE')

    def audit_after_uncertainty(self, intent, wire):
        if type(wire) is not OfflineSignedWire:
            raise LabBlocked('OFFLINE_WIRE_REQUIRED')
        record = self.journal.read(intent)
        if not record or record['state'] == 'PREPARED':
            raise LabBlocked('NOT_ARMED')
        order = wire.orders.get(intent.client_id())
        if type(order) is not dict or order.get('clientId') != intent.client_id() or order.get('symbol') != intent.symbol:
            return self.report('ORDER_UNKNOWN', 'NO_CONCLUSIVE_EVIDENCE')
        self.journal.observe(intent, order_observed=True, snapshot_verified=True)
        return self.report('PROTECTION_UNKNOWN', 'GET_AUDIT_REQUIRED')
