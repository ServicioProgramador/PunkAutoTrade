"""F56: Bitunix Futures signed WRITE transport (real HTTPS POST).

Scope and safety rules:
- Only an explicit allowlist of documented Futures write endpoints.
- Body is the exact compact JSON that was signed (official REST signature:
  sign = SHA256(SHA256(nonce + timestamp + api-key + query + body) + secret)).
- One attempt per call. This module NEVER retries.
- Outcome classification is conservative:
    ACK       HTTP 200 and code == 0 (request accepted; NOT a fill, NOT protection)
    REJECTED  HTTP 200 with code != 0, or HTTP 4xx (business/auth rejection;
              still requires GET reconciliation before any new attempt)
    UNKNOWN   timeout, connection loss, 5xx, redirect, oversized or malformed body
- It does not decide WHEN to send. Authorization lives in punk.live_executor.
- Credentials are never printed or stored; repr is redacted.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from punk.bitunix_readonly import BASE, NoRedirect
from punk.bitunix_write_contract import _canonical, signature
from punk.live_write_lab import LabBlocked

WRITE_PATHS = {
    'place_order': '/api/v1/futures/trade/place_order',
    'cancel_orders': '/api/v1/futures/trade/cancel_orders',
    'tpsl_place': '/api/v1/futures/tpsl/place_order',
    'tpsl_modify': '/api/v1/futures/tpsl/modify_order',
    'tpsl_position_modify': '/api/v1/futures/tpsl/position/modify_order',
    'change_leverage': '/api/v1/futures/account/change_leverage',
    'flash_close': '/api/v1/futures/trade/flash_close_position',
}
_MAX_BODY = 16_384
_MAX_REPLY = 65_536


@dataclass(frozen=True)
class PostOutcome:
    state: str                 # ACK | REJECTED | UNKNOWN
    endpoint: str
    http_status: int = 0
    code: int | None = None
    message: str = ''
    data: object = None
    raw: bytes = field(default=b'', repr=False)

    @property
    def ack(self):
        return self.state == 'ACK'

    def public(self):
        """Bounded, secret-free summary for logs/SQLite."""
        return {'state': self.state, 'endpoint': self.endpoint, 'http_status': self.http_status,
                'code': self.code, 'message': (self.message or '')[:120]}


def _classify(endpoint, status, raw):
    if status >= 500 or status in (408, 429) or 300 <= status < 400:
        return PostOutcome('UNKNOWN', endpoint, status, message='HTTP_OUTCOME_UNCERTAIN', raw=raw)
    try:
        body = json.loads(raw.decode('utf-8')) if raw else None
    except (ValueError, UnicodeError):
        body = None
    if 400 <= status < 500:
        code = body.get('code') if isinstance(body, dict) and type(body.get('code')) is int else None
        msg = str(body.get('msg', '')) if isinstance(body, dict) else ''
        return PostOutcome('REJECTED', endpoint, status, code, msg[:200] or 'HTTP_' + str(status), raw=raw)
    if status != 200 or not isinstance(body, dict) or type(body.get('code')) is not int:
        return PostOutcome('UNKNOWN', endpoint, status, message='MALFORMED_RESPONSE', raw=raw)
    if body['code'] != 0:
        return PostOutcome('REJECTED', endpoint, status, body['code'], str(body.get('msg', ''))[:200], raw=raw)
    return PostOutcome('ACK', endpoint, status, 0, str(body.get('msg', ''))[:200], body.get('data'), raw)


class BitunixTradeClient:
    """Real signed POST client. Construct explicitly; there is no global instance."""

    def __init__(self, api_key, secret_key, *, opener=None, base=BASE, timeout=8,
                 nonce_fn=None, clock_ms=None):
        if not isinstance(api_key, str) or not api_key or not isinstance(secret_key, str) or not secret_key:
            raise LabBlocked('TRADE_CREDENTIALS_REQUIRED')
        if base != BASE and not base.startswith(('http://127.0.0.1:', 'https://127.0.0.1:')):
            # Only the official host, or a loopback QA server.
            raise LabBlocked('NONCANONICAL_DESTINATION')
        self._api_key = api_key
        self._secret = secret_key
        self._base = base
        self._timeout = timeout
        self._opener = opener or build_opener(NoRedirect).open
        self._nonce = nonce_fn or (lambda: secrets.token_hex(16))
        self._clock = clock_ms or (lambda: str(int(time.time() * 1000)))
        self.sent = []  # endpoint names only (audit / QA), never bodies or headers

    def __repr__(self):
        return '<BitunixTradeClient REDACTED>'

    @classmethod
    def from_env(cls, **kw):
        key = os.getenv('PA_BITUNIX_API_KEY', '')
        secret = os.getenv('PA_BITUNIX_API_SECRET') or os.getenv('PA_BITUNIX_SECRET_KEY', '')
        return cls(key, secret, **kw)

    def post(self, endpoint, payload):
        if endpoint not in WRITE_PATHS:
            raise LabBlocked('WRITE_ENDPOINT_NOT_ALLOWED')
        if type(payload) is not dict or not payload:
            raise LabBlocked('INVALID_BODY')
        body = _canonical(payload)
        if ' ' in body:
            # Official rule: signed body has no spaces. Values must not contain them.
            raise LabBlocked('BODY_CONTAINS_SPACES')
        raw_body = body.encode('utf-8')
        if len(raw_body) > _MAX_BODY:
            raise LabBlocked('BODY_SIZE_INVALID')
        nonce, timestamp = self._nonce(), self._clock()
        headers = {'api-key': self._api_key, 'nonce': nonce, 'timestamp': timestamp,
                   'sign': signature(self._api_key, self._secret, nonce, timestamp, body),
                   'Content-Type': 'application/json', 'language': 'en-US'}
        url = self._base + WRITE_PATHS[endpoint]
        request = Request(url, data=raw_body, headers=headers, method='POST')
        self.sent.append(endpoint)
        try:
            response = self._opener(request, timeout=self._timeout)
        except HTTPError as exc:
            try:
                raw = exc.read(_MAX_REPLY + 1)
            except Exception:
                raw = b''
            return _classify(endpoint, int(exc.code), raw[:_MAX_REPLY])
        except (URLError, TimeoutError, OSError, ValueError):
            return PostOutcome('UNKNOWN', endpoint, message='TRANSPORT_OUTCOME_UNKNOWN')
        except Exception:
            return PostOutcome('UNKNOWN', endpoint, message='TRANSPORT_OUTCOME_UNKNOWN')
        try:
            if response.geturl() != url:
                return PostOutcome('UNKNOWN', endpoint, message='REDIRECT_BLOCKED')
            status = int(getattr(response, 'status', None) or response.getcode())
            raw = response.read(_MAX_REPLY + 1)
        except Exception:
            return PostOutcome('UNKNOWN', endpoint, message='RESPONSE_READ_FAILED')
        finally:
            try:
                response.close()
            except Exception:
                pass
        if len(raw) > _MAX_REPLY:
            return PostOutcome('UNKNOWN', endpoint, status, message='RESPONSE_TOO_LARGE')
        return _classify(endpoint, status, raw)
