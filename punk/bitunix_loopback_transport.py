"""F27: REAL HTTP protocol rehearsal to localhost, NEVER to Bitunix.

This adapter has a fixed loopback destination and signs with synthetic test-only
credentials. It cannot be configured with production API credentials or hosts.
An HTTP reply/timeout is ALWAYS inconclusive as to LIVE order execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from punk.bitunix_signed_transport_lab import SignedOfflineRequest, OfflineSignedWire
from punk.bitunix_write_contract import PATH_PLACE, PATH_TPSL
from punk.live_write_lab import LabBlocked, TransportUnknown


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class LoopbackReply:
    state: str
    code: int | None = None
    # Never represent a test response as actual protected position.
    real_order_sent: bool = False
    protected: bool = False
    ready_for_live: bool = False
    retry_allowed: bool = False


class LoopbackHttpWire:
    """Network write restricted to explicit 127.0.0.1 test server only.

    Deliberately no parameter accepts a hostname, credentials or arbitrary URL.
    """
    def __init__(self, port, *, timeout=1.5):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise LabBlocked('INVALID_LOOPBACK_PORT')
        if type(timeout) not in (int, float) or not .05 <= timeout <= 5:
            raise LabBlocked('INVALID_LOOPBACK_TIMEOUT')
        self._port = port
        self._timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def exchange(self, request):
        if type(request) is not SignedOfflineRequest or request.method != 'POST' or request.path not in (PATH_PLACE, PATH_TPSL):
            raise LabBlocked('INVALID_OFFLINE_WIRE_REQUEST')
        if type(request.body) is not bytes or len(request.body) > 16_384:
            raise LabBlocked('INVALID_OFFLINE_WIRE_BODY')
        # Test-only header names. Never accept user-supplied API keys.
        url = 'http://127.0.0.1:' + str(self._port) + request.path
        if urlsplit(url).hostname != '127.0.0.1':
            raise LabBlocked('LOOPBACK_REQUIRED')
        req = Request(url, data=request.body, method='POST', headers={
            'Content-Type': 'application/json',
            'x-lab-sign': request.signature,
            'x-lab-nonce': request.nonce,
            'x-lab-timestamp': request.timestamp,
        })
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                if resp.status != 200:
                    raise TransportUnknown('HTTP_UNCERTAIN')
                raw = resp.read(32_769)
                if len(raw) > 32_768:
                    raise TransportUnknown('RESPONSE_TOO_LARGE')
        except (HTTPError, URLError, TimeoutError, OSError):
            raise TransportUnknown('HTTP_UNCERTAIN') from None
        try:
            doc = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise TransportUnknown('INVALID_ACK') from None
        if type(doc) is not dict or type(doc.get('code')) is not int:
            raise TransportUnknown('INVALID_ACK')
        # Even an accepted simulated ack is only a LAB observation.
        return doc


def loopback_report(result=None):
    return {'build': 'F27', 'mode': 'LOOPBACK_HTTP_SYNTHETIC_ONLY',
            'response_state': 'NOT_CHECKED' if result is None else result,
            'network_target': '127.0.0.1', 'real_orders_sent': 0,
            'live_enabled': False, 'ready_for_live': False,
            'write_transport_enabled': False, 'allow_retry': False}
