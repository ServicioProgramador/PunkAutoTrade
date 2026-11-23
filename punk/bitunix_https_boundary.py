"""F28: production-shaped Bitunix HTTPS request preparation, NO LIVE send authority.

Signed request is built only in memory. Any attempt to submit to Bitunix through
this component fails closed. Mocked transport is available for QA but cannot use
credentials from the environment and is restricted to the explicit QA harness.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit

from punk.bitunix_write_contract import PATH_PLACE, PATH_TPSL, _canonical, signature
from punk.live_write_lab import LabBlocked, TransportUnknown

BASE = 'https://fapi.bitunix.com'
_ALLOWED = frozenset((PATH_PLACE, PATH_TPSL))
_MAX_BODY = 16_384


@dataclass(frozen=True)
class PreparedHttpsPost:
    """Ephemeral wire representation; never print/repr credentials or bodies."""
    path: str
    body: bytes
    headers: dict
    url: str

    def __repr__(self):
        return '<PreparedHttpsPost REDACTED>'


def prepare_https_post(path, payload, *, api_key, secret, nonce, timestamp):
    """Prepare exact bytes and signing headers; does NOT connect or send."""
    if path not in _ALLOWED or type(path) is not str:
        raise LabBlocked('WRITE_PATH_NOT_ALLOWED')
    if type(api_key) is not str or not api_key or type(secret) is not str or not secret:
        raise LabBlocked('EXPLICIT_TEST_CREDENTIALS_REQUIRED')
    if type(nonce) is not str or not re.fullmatch(r'[a-zA-Z0-9_-]{1,63}', nonce):
        raise LabBlocked('INVALID_NONCE')
    if type(timestamp) is not str or not re.fullmatch(r'\d{13}', timestamp):
        raise LabBlocked('INVALID_TIMESTAMP')
    if type(payload) is not dict:
        raise LabBlocked('INVALID_BODY')
    canonical = _canonical(payload)
    body = canonical.encode('utf-8')
    if not 0 < len(body) <= _MAX_BODY:
        raise LabBlocked('BODY_SIZE_INVALID')
    # The official signature must cover the *identical* bytes being sent.
    digest = signature(api_key, secret, nonce, timestamp, canonical)
    return PreparedHttpsPost(path, body, {
        'api-key': api_key, 'nonce': nonce, 'timestamp': timestamp,
        'sign': digest, 'Content-Type': 'application/json',
    }, BASE + path)


class BitunixHttpsBoundary:
    """Production-facing boundary: there is NO public write capability in F28."""
    def submit(self, request):
        raise LabBlocked('LIVE_TRANSMISSION_NOT_AUTHORIZED_F28')

    def place_order(self, request):
        raise LabBlocked('LIVE_TRANSMISSION_NOT_AUTHORIZED_F28')

    def place_tpsl(self, request):
        raise LabBlocked('LIVE_TRANSMISSION_NOT_AUTHORIZED_F28')


def validate_test_response(raw, *, status):
    """Parse an injected synthetic wire reply; never infer exchange protection."""
    if type(status) is not int or status != 200:
        raise TransportUnknown('HTTP_OUTCOME_UNCERTAIN')
    if type(raw) is not bytes or not 0 < len(raw) <= 32768:
        raise TransportUnknown('RESPONSE_OUTCOME_UNCERTAIN')
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise TransportUnknown('RESPONSE_OUTCOME_UNCERTAIN') from None
    if type(data) is not dict or type(data.get('code')) is not int:
        raise TransportUnknown('RESPONSE_OUTCOME_UNCERTAIN')
    if data['code'] != 0:
        # Even an API business rejection doesn't prove there was no previous fill.
        raise TransportUnknown('EXCHANGE_REJECTION_REQUIRES_RECONCILIATION')
    return {'state': 'ACK_ONLY', 'protected': False, 'allow_retry': False,
            'real_order_verified': False, 'ready_for_live': False}


def boundary_report():
    return {'build':'F28', 'mode':'HTTPS_WRITE_BOUNDARY_DISABLED',
            'target':BASE, 'allowed_paths': sorted(_ALLOWED),
            'signed_request_compiler':True, 'orders_sent':0,
            'write_transport_enabled':False, 'live_enabled':False,
            'ready_for_live':False, 'allow_retry':False,
            'notice':'NO_PRODUCTION_TRANSMISSION_OR_AUTHORIZATION'}
