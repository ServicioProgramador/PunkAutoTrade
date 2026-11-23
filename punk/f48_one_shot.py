"""F48: production HTTPS transport and durable one-shot commissioning boundary.

No Telegram route, CLI option or environment variable grants production writes.
The production HTTPS wire is implemented and isolated; commissioning exercises
exactly the same request format against a typed QA wire. A subsequent reviewed
release must provide independently verified real account approval/protection
handling before making an operational send path available.
"""
from __future__ import annotations

import http.client
import json
import ssl
from dataclasses import dataclass
from pathlib import Path

from punk.bitunix_https_boundary import PreparedHttpsPost, prepare_https_post, BASE
from punk.bitunix_write_contract import PATH_PLACE, entry_contract
from punk.bitunix_write_journal import WriteJournal
from punk.bitunix_ack_journal import AckJournal
from punk.live_write_lab import Intent, LabBlocked


@dataclass(frozen=True)
class WireReply:
    status: int
    body: bytes


class BitunixProductionWire:
    """Real destination implementation. Not issued send authority by F48."""

    def send_production(self, request, *, authorization=None):
        # Even a string/env/flag indicating LIVE is insufficient in this build.
        raise LabBlocked('F48_LIVE_CAPABILITY_NOT_ISSUED')

    def _transport_impl_for_review(self, request):
        """Network code for a future explicitly reviewed, one-shot invocation.

        No retries, no redirects, bounded response, fail-closed on network errors.
        Not reachable via run.py, Telegram or the commissioning method below.
        """
        if type(request) is not PreparedHttpsPost or request.path != PATH_PLACE:
            raise LabBlocked('F48_NONCANONICAL_REQUEST')
        if request.url != BASE + PATH_PLACE or not 0 < len(request.body) <= 16384:
            raise LabBlocked('F48_NONCANONICAL_DESTINATION')
        if not isinstance(request.headers, dict) or not all(request.headers.get(k) for k in
                                                           ('api-key', 'nonce', 'timestamp', 'sign')):
            raise LabBlocked('F48_UNSIGNED_REQUEST')
        conn = http.client.HTTPSConnection('fapi.bitunix.com', timeout=5,
                                            context=ssl.create_default_context())
        try:
            conn.request('POST', PATH_PLACE, body=request.body, headers=request.headers)
            response = conn.getresponse()
            raw = response.read(32769)
            if len(raw) > 32768 or response.status != 200:
                raise RuntimeError('F48_RESPONSE_UNKNOWN')
            return WireReply(response.status, raw)
        except Exception:
            raise RuntimeError('F48_TRANSPORT_OUTCOME_UNKNOWN') from None
        finally:
            conn.close()


class F48LocalWire:
    """Exact-type test boundary, no sockets. Deliberately not subclassable for send."""
    def __init__(self, *, mode='ack'):
        self.mode = mode
        self.calls = 0

    def post(self, request):
        if type(request) is not PreparedHttpsPost or request.url != BASE + PATH_PLACE:
            raise LabBlocked('F48_LOCAL_ENTRY_ONLY')
        payload = json.loads(request.body.decode('utf8'))
        if payload.get('slPrice') is None or payload.get('reduceOnly') is not False:
            raise LabBlocked('F48_UNPROTECTED_ENTRY')
        self.calls += 1
        if self.mode == 'timeout':
            raise TimeoutError('synthetic lost response')
        if self.mode == 'http503':
            return WireReply(503, b'')
        if self.mode == 'bad_ack':
            return WireReply(200, b'{"code":0,"data":{"orderId":"LAB","clientId":"WRONG"}}')
        return WireReply(200, json.dumps({'code': 0, 'data': {
            'orderId': 'LAB-F48-ONLY', 'clientId': payload['clientId']}}).encode())


class F48Commissioning:
    """One-shot dry-run with durable SQLite and actual canonical signed bytes.

    This never reads .env or sends network traffic; production wire is rejected.
    A lab ACK is only evidence of a synthetic receipt, NEVER protection or fill.
    """
    def __init__(self, journal_path):
        self.path = Path(journal_path)
        self.journal = WriteJournal(self.path)
        self.acks = AckJournal(self.path)

    @staticmethod
    def _validate_preview(intent, preview):
        if type(intent) is not Intent:
            raise LabBlocked('F48_INVALID_INTENT')
        intent.validate()
        if type(preview) is not dict or preview.get('state') != 'PRETRADE_PREVIEW_ONLY':
            raise LabBlocked('F48_PREFLIGHT_REQUIRED')
        expected = entry_contract(intent)
        if preview.get('entry_payload') != expected or preview.get('symbol') != intent.symbol:
            raise LabBlocked('F48_PREVIEW_MISMATCH')
        if preview.get('snapshot_get_only_verified') is not True or preview.get('sl_required') is not True:
            raise LabBlocked('F48_SNAPSHOT_OR_SL_MISSING')
        if type(preview.get('tp_count')) is not int or preview['tp_count'] != len(intent.take_profits):
            raise LabBlocked('F48_TP_PLAN_MISSING')
        if any(preview.get(k) is not False for k in
               ('request_sent','write_transport_enabled','live_enabled','ready_for_live','allow_retry')):
            raise LabBlocked('F48_PREVIEW_MUST_BE_OFFLINE')
        if not isinstance(preview.get('tp_payloads'), tuple) or len(preview['tp_payloads']) != len(intent.take_profits):
            raise LabBlocked('F48_TP_QUANTITY_PLAN_REQUIRED')
        if not preview.get('required_reserve_usdt'):
            raise LabBlocked('F48_RESERVE_UNVERIFIED')
        return expected

    def commission(self, intent, preview, wire, *, nonce='F48QANONCE', timestamp='1800000000000'):
        if type(wire) is not F48LocalWire:
            raise LabBlocked('F48_LOCAL_COMMISSION_ONLY')
        payload = self._validate_preview(intent, preview)
        record = self.journal.prepare(intent)
        if record['state'] != 'PREPARED':
            return self._report('PREVIOUS_INTENT_BLOCKED_NO_RETRY')
        request = prepare_https_post(PATH_PLACE, payload, api_key='F48_SYNTHETIC',
                                     secret='F48_SYNTHETIC_SECRET', nonce=nonce, timestamp=timestamp)
        # Durable transition BEFORE any I/O, including local test wire.
        self.journal.arm_uncertain_offline(intent)
        try:
            response = wire.post(request)
            outcome = self.acks.observe(intent, raw=response.body, http_status=response.status)
            state = 'ACK_ONLY_RECONCILIATION_REQUIRED' if outcome['state'] == 'ACK_OBSERVED' else 'UNKNOWN_NO_RETRY'
        except Exception:
            state = 'UNKNOWN_NO_RETRY'
        return self._report(state)

    @staticmethod
    def _report(state):
        return {'build':'F48','mode':'LOCAL_COMMISSION_NO_LIVE', 'state':state,
                'live_enabled':False,'write_transport_enabled':False,
                'protected':False,'ready_for_live':False,'allow_retry':False,'orders_sent':0}

    def submit_live(self, *args, **kwargs):
        raise LabBlocked('F48_LIVE_CAPABILITY_NOT_ISSUED')


def lab_report():
    """Temporary workspace demo; does not touch operational databases."""
    import tempfile
    from punk.bitunix_write_contract import protection_plan
    with tempfile.TemporaryDirectory(prefix='punk_f48_') as folder:
        intent = Intent('f48:synthetic:rune','RUNEUSDT','SHORT','10','0.4785','0.487456',
                        ('0.470246','0.469008','0.467584','0.465947','0.464063'))
        preview = {'state':'PRETRADE_PREVIEW_ONLY','entry_payload':entry_contract(intent),
                   'symbol':intent.symbol,'snapshot_get_only_verified':True,
                   'sl_required':True,'tp_count':5, 'tp_payloads':protection_plan(intent,
                   position_id='F48LAB',tp_weights=('0.2',)*5, quantity_step='0.01'),
                   'required_reserve_usdt':'5.1','request_sent':False,
                   'write_transport_enabled':False,'live_enabled':False,
                   'ready_for_live':False,'allow_retry':False}
        app=F48Commissioning(Path(folder)/'one_shot.db')
        wire=F48LocalWire()
        first=app.commission(intent,preview,wire)
        replay=F48Commissioning(Path(folder)/'one_shot.db').commission(intent,preview,wire)
        return {'build':'F48','initial_state':first['state'],'replay_state':replay['state'],
                'qa_wire_calls':wire.calls,'real_orders_sent':0,'live_enabled':False,
                'ready_for_live':False, 'protected':False}
