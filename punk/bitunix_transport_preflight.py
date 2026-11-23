"""F21: fail-closed REST boundary verification. GET-only on real exchange.

No function in this module sends POST, PATCH, DELETE or a trade command.
It compiles synthetic write contracts offline, never with account credentials.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from punk.bitunix_readonly import ReadOnlyBitunix
from punk.bitunix_write_contract import (
    PATH_PLACE, PATH_TPSL, entry_contract, protection_plan, compile_offline,
)
from punk.live_write_lab import Intent, LabBlocked


class TransportBoundary:
    """Hard allowlist that delegates ONLY to the F2-F20 GET-only client."""
    _allowed = frozenset(('account', 'positions', 'orders', 'tpsl',
                          'order_detail', 'contracts', 'position_tiers', 'tickers'))

    def __init__(self, reader=None):
        self._reader = reader if reader is not None else ReadOnlyBitunix()
        if not callable(getattr(self._reader, 'get', None)):
            raise LabBlocked('INVALID_READONLY_CLIENT')

    def request(self, method, resource, params=None):
        if method != 'GET' or resource not in self._allowed:
            raise LabBlocked('LIVE_WRITE_TRANSPORT_DISABLED')
        if params is not None and not isinstance(params, dict):
            raise LabBlocked('INVALID_READONLY_PARAMS')
        # Forbid caller-controlled non-string keys/values and malformed resources.
        if params and any(type(k) is not str or not k or type(v) not in (str, int)
                          for k, v in params.items()):
            raise LabBlocked('INVALID_READONLY_PARAMS')
        return self._reader.get(resource, params)

    def post(self, *_args, **_kwargs):
        raise LabBlocked('LIVE_WRITE_TRANSPORT_DISABLED')

    def put(self, *_args, **_kwargs):
        raise LabBlocked('LIVE_WRITE_TRANSPORT_DISABLED')

    def delete(self, *_args, **_kwargs):
        raise LabBlocked('LIVE_WRITE_TRANSPORT_DISABLED')


def _positive_number(value):
    try:
        v = Decimal(str(value))
        return v if v.is_finite() and v > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def offline_write_preview(intent: Intent, *, position_id='SYNTHETIC_POSITION',
                          quantity_step='0.01', weights=None):
    """Validate payloads with F16, sign with SYNTHETIC keys, do not send.

    Returns only counts and booleans, never signed headers, ids or amounts.
    This is a rehearsal, NOT evidence that exchange supports these parameters.
    """
    if weights is None:
        weights = tuple('0.2' for _ in intent.take_profits) if len(intent.take_profits) == 5 else None
    entry = entry_contract(intent)
    tps = protection_plan(intent, position_id=position_id,
                           tp_weights=weights, quantity_step=quantity_step)
    sample = compile_offline('POST', PATH_PLACE, entry,
                             api_key='SYNTHETIC_API_KEY', secret='SYNTHETIC_SECRET',
                             nonce='1234567890abcdef', timestamp='1763683200000')
    for tp in tps:
        compile_offline('POST', PATH_TPSL, tp,
                        api_key='SYNTHETIC_API_KEY', secret='SYNTHETIC_SECRET',
                        nonce='1234567890abcdef', timestamp='1763683200000')
    return {'entry_contract_valid': True, 'sl_in_entry': 'slPrice' in entry,
            'tp_count': len(tps), 'synthetic_signature_prepared': bool(sample.signed_digest),
            'request_sent': False, 'real_protection_verified': False,
            'live_enabled': False, 'ready_for_live': False}


def real_get_boundary_probe(reader=None):
    """GET-only integrity probe using F14 snapshot, no printed account amounts.

    An unexpected response/error fails CLOSED without exposing raw HTTP payloads.
    A successful snapshot still is not an authorization for sending orders.
    """
    client = reader if reader is not None else ReadOnlyBitunix()
    result = {'build': 'F21', 'mode': 'GET_ONLY_TRANSPORT_PREFLIGHT',
              'get_only_verified': False, 'existing_exposure': 'UNKNOWN',
              'pending_orders': 'UNKNOWN', 'pending_protections': 'UNKNOWN',
              'write_transport_enabled': False, 'orders_sent': 0,
              'live_enabled': False, 'ready_for_live': False, 'allow_retry': False}
    try:
        snapshot = client.snapshot()
        if not isinstance(snapshot, dict) or snapshot.get('status') != 'GET_ONLY_VERIFIED':
            result['reason'] = 'SNAPSHOT_UNVERIFIED'
            return result
        positions = snapshot.get('positions')
        orders = snapshot.get('pending_orders')
        protections = snapshot.get('pending_tpsl')
        account = snapshot.get('account')
        if not (isinstance(positions, list) and isinstance(orders, list) and
                isinstance(protections, list) and isinstance(account, dict) and
                account.get('marginCoin') == 'USDT' and
                _positive_number(account.get('available')) is not None):
            result['reason'] = 'SNAPSHOT_INCOMPLETE'
            return result
        result.update({'get_only_verified': True,
                       'existing_exposure': 'PRESENT' if positions else 'NONE',
                       'pending_orders': 'PRESENT' if orders else 'NONE',
                       'pending_protections': 'PRESENT' if protections else 'NONE',
                       'reason': 'WRITE_TRANSPORT_NOT_IMPLEMENTED'})
    except Exception:
        result['reason'] = 'GET_ONLY_UNAVAILABLE_OR_INCONSISTENT'
    return result


def offline_report():
    intent = Intent('f21:rune', 'RUNEUSDT', 'SHORT', '10', '0.4785', '0.487456',
                    ('0.470246', '0.469008', '0.467584', '0.465947', '0.464063'))
    return {'build': 'F21', 'mode': 'OFFLINE_WRITE_BOUNDARY',
            **offline_write_preview(intent), 'network_calls': 0,
            'write_transport_enabled': False}
