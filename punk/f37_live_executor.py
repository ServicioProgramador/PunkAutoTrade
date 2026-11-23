"""F37: guarded production-shaped one-shot write executor.

Network code for Bitunix exists but cannot be reached by the public workflow:
no LIVE capability is issued, and ProductionHTTPS is hard-locked. In QA an
injected exact-class local wire exercises the complete durable send boundary.
DO NOT interpret lab ACK as a protected position or enable automatic trading.
"""
from __future__ import annotations

import http.client
import json
import ssl
from dataclasses import dataclass

from punk.bitunix_https_boundary import PreparedHttpsPost, BASE, prepare_https_post
from punk.bitunix_write_contract import PATH_PLACE, PATH_TPSL
from punk.bitunix_write_journal import WriteJournal
from punk.bitunix_ack_journal import AckJournal
from punk.live_write_lab import Intent, LabBlocked


@dataclass(frozen=True)
class WireResult:
    status: int
    body: bytes


class ProductionHTTPS:
    """Actual HTTPS implementation, but purposefully not granted send authority.

    The enabling condition is code-controlled and not configurable by .env,
    CLI flags, a callback, or an external API. A future reviewed release needs
    to replace this boundary *after* real protection/recovery testing.
    """
    def post(self, request):
        raise LabBlocked('F37_LIVE_TRANSMISSION_DISABLED')

    def _https_wire_for_future_review(self, request):
        # Not connected to public F37 workflow. Must not be enabled until
        # exchange-specific safety and authorization reviews are complete.
        if type(request) is not PreparedHttpsPost or request.path not in (PATH_PLACE, PATH_TPSL):
            raise LabBlocked('INVALID_SIGNED_REQUEST')
        if request.url != BASE + request.path or len(request.body) > 16384:
            raise LabBlocked('NONCANONICAL_DESTINATION')
        conn = http.client.HTTPSConnection('fapi.bitunix.com', timeout=5,
                                            context=ssl.create_default_context())
        try:
            conn.request('POST', request.path, body=request.body, headers=request.headers)
            response = conn.getresponse()
            raw = response.read(32769)
            if len(raw) > 32768 or 300 <= response.status < 400:
                raise RuntimeError('BITUNIX_RESPONSE_UNCERTAIN')
            return WireResult(response.status, raw)
        except Exception:
            raise RuntimeError('BITUNIX_TRANSPORT_UNKNOWN') from None
        finally:
            conn.close()


class LocalQAOnlyWire:
    """Typed local test double, never sends network traffic."""
    def __init__(self):
        self.calls = []
        self.timeout_before = False
        self.timeout_after = False
        self.mismatch = False

    def post(self, request):
        if type(request) is not PreparedHttpsPost or request.url != BASE + PATH_PLACE:
            raise LabBlocked('QA_ENTRY_ONLY')
        self.calls.append(request.path)
        if self.timeout_before:
            raise TimeoutError('synthetic timeout')
        payload = json.loads(request.body.decode('utf-8'))
        response = {'code': 0, 'data': {
            'orderId': 'QA-F37-1',
            'clientId': 'WRONG' if self.mismatch else payload['clientId']}}
        if self.timeout_after:
            raise TimeoutError('synthetic lost acknowledgement')
        return WireResult(200, json.dumps(response).encode('utf-8'))


def _report(state):
    return {'build': 'F37', 'mode': 'ONE_SHOT_LOCAL_QA', 'state': state,
            'protected': False, 'real_orders_sent': 0, 'allow_retry': False,
            'live_enabled': False, 'write_transport_enabled': False,
            'ready_for_live': False}


class F37OneShot:
    """The irreversible pre-send transition is COMMITTED before test wire POST.

    Refuses real transport and never accepts user/env credentials, URLs or LIVE
    flags. Durable records can only be reconciled with GET-only evidence later.
    """
    def __init__(self, db_path):
        self.journal = WriteJournal(db_path)
        self.acks = AckJournal(db_path)

    def execute_local_qa(self, intent, pretrade_preview, wire):
        if type(intent) is not Intent or type(wire) is not LocalQAOnlyWire:
            raise LabBlocked('F37_LOCAL_QA_ONLY')
        intent.validate()
        if type(pretrade_preview) is not dict or pretrade_preview.get('state') != 'PRETRADE_PREVIEW_ONLY':
            raise LabBlocked('PRETRADE_REQUIRED')
        from punk.bitunix_write_contract import entry_contract
        if (pretrade_preview.get('entry_payload') != entry_contract(intent)
                or pretrade_preview.get('snapshot_get_only_verified') is not True
                or pretrade_preview.get('sl_required') is not True
                or pretrade_preview.get('tp_count') != len(intent.take_profits)):
            raise LabBlocked('PRETRADE_MISMATCH')
        if any(pretrade_preview.get(flag) is not False for flag in
               ('request_sent','write_transport_enabled','live_enabled','ready_for_live','allow_retry')):
            raise LabBlocked('PRETRADE_UNSAFE')
        record = self.journal.prepare(intent)
        if record['state'] != 'PREPARED':
            return _report('ALREADY_ARMED_NO_RETRY')
        # IMPORTANT: the transition is in a SQLite committed transaction.
        self.journal.arm_uncertain_offline(intent)
        request = prepare_https_post(PATH_PLACE, pretrade_preview['entry_payload'],
                api_key='QA_SYNTHETIC', secret='QA_SYNTHETIC_SECRET',
                nonce='f37synthetic', timestamp='1800000000000')
        try:
            reply = wire.post(request)
        except Exception:
            return _report('UNKNOWN_NO_RETRY')
        try:
            ack = self.acks.observe(intent, raw=reply.body, http_status=reply.status)
        except Exception:
            return _report('ACK_UNKNOWN_NO_RETRY')
        if ack['state'] == 'ACK_OBSERVED':
            return _report('ACK_ONLY_GET_CONFIRMATION_REQUIRED')
        return _report('ACK_UNKNOWN_NO_RETRY')

    def submit_live(self, *_args, **_kwargs):
        raise LabBlocked('F37_LIVE_TRANSMISSION_DISABLED')


def status():
    return {'build':'F37', 'https_transport_compiled':True,
            'production_post_enabled':False, 'live_enabled':False,
            'ready_for_live':False, 'orders_sent':0,
            'remaining':['real_sl_tp_confirmation','partial_fill_recovery',
                         'real_break_even','explicit_one_order_authorization']}
