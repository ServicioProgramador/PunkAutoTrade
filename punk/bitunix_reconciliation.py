"""F17: authenticated GET-only order reconciliation; all write attempts fail closed.

No POST implementation, no configuration switch can enable one. A successful
GET can establish an order's observed state but never authorize re-submission.
"""
from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation
from punk.bitunix_readonly import ReadOnlyBitunix
from punk.live_write_lab import LabBlocked

_CLIENT = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_SYMBOL = re.compile(r'[A-Z0-9]{2,30}USDT\Z')


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


class ReadOnlyBoundary:
    """Allowlist, never delegates arbitrary paths/methods to an HTTP client."""
    def __init__(self, client=None):
        self._client = client if client is not None else ReadOnlyBitunix()

    def request(self, method, resource, params=None):
        if method != 'GET' or resource not in ('order_detail', 'account_snapshot'):
            raise LabBlocked('LIVE_WRITE_TRANSPORT_DISABLED')
        if resource == 'account_snapshot':
            if params not in (None, {}):
                raise LabBlocked('INVALID_RECONCILIATION_QUERY')
            return self._client.snapshot()
        if not isinstance(params, dict) or set(params) != {'clientId'}:
            raise LabBlocked('INVALID_RECONCILIATION_QUERY')
        cid = params['clientId']
        if not isinstance(cid, str) or not _CLIENT.fullmatch(cid):
            raise LabBlocked('INVALID_RECONCILIATION_QUERY')
        return self._client.get('order_detail', {'clientId': cid})

    def post(self, *_args, **_kwargs):
        raise LabBlocked('LIVE_WRITE_TRANSPORT_DISABLED')


def _blocked(reason):
    return {'state': reason, 'protected': False, 'allow_retry': False,
            'live_enabled': False, 'ready_for_live': False}


def reconcile(boundary, client_id, symbol):
    """Proof is deliberately conservative: no negative result permits a retry.

    A GET detail response is already unwrapped by ReadOnlyBitunix.get; compare
    identities before considering its state. Never print IDs, balances or bodies.
    """
    if not isinstance(client_id, str) or not _CLIENT.fullmatch(client_id):
        return _blocked('INVALID_CLIENT_ID')
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        return _blocked('INVALID_SYMBOL')
    try:
        detail = boundary.request('GET', 'order_detail', {'clientId': client_id})
    except Exception:
        return _blocked('ORDER_LOOKUP_UNKNOWN')
    if not isinstance(detail, dict) or detail.get('clientId') != client_id or detail.get('symbol') != symbol:
        return _blocked('ORDER_IDENTITY_UNCONFIRMED')
    if not str(detail.get('orderId') or '').strip():
        return _blocked('ORDER_IDENTITY_UNCONFIRMED')
    status = detail.get('status')
    if status not in ('INIT', 'NEW', 'PART_FILLED', 'FILLED', 'CANCELED'):
        return _blocked('ORDER_STATE_UNKNOWN')
    if status in ('INIT', 'NEW', 'CANCELED'):
        return _blocked('ORDER_' + status)
    filled = _number(detail.get('tradeQty'))
    if filled is None or filled == 0:
        return _blocked('FILL_UNCONFIRMED')
    try:
        snap = boundary.request('GET', 'account_snapshot')
    except Exception:
        return _blocked('EXPOSURE_SNAPSHOT_UNKNOWN')
    if not isinstance(snap, dict) or snap.get('status') != 'GET_ONLY_VERIFIED':
        return _blocked('EXPOSURE_SNAPSHOT_UNKNOWN')
    positions = snap.get('positions')
    tpsl = snap.get('pending_tpsl')
    if not isinstance(positions, list) or not isinstance(tpsl, list):
        return _blocked('EXPOSURE_SNAPSHOT_UNKNOWN')
    # Only claim that exposure is seen; do not claim that a protective order is
    # executable or covers the fill without exchange-grade reconciliation.
    matches = [p for p in positions if isinstance(p, dict) and p.get('symbol') == symbol
               and _number(p.get('qty')) is not None and _number(p.get('qty')) > 0
               and str(p.get('positionId') or '')]
    if len(matches) != 1:
        return _blocked('POSITION_UNCONFIRMED')
    pos = matches[0]
    linked = [t for t in tpsl if isinstance(t, dict) and t.get('symbol') == symbol
              and str(t.get('positionId') or '') == str(pos['positionId'])]
    if not linked:
        return _blocked('PROTECTION_UNCONFIRMED')
    # Never report protected=True until TP/SL quantities, direction, trigger,
    # order status, and all intended TP levels are reconciled independently.
    return _blocked('PROTECTION_AUDIT_REQUIRED')


def report(boundary=None, client_id='', symbol=''):
    if not client_id:
        return {'build': 'F17', 'mode': 'GET_ONLY_RECONCILIATION',
                'write_transport_enabled': False, 'ready_for_live': False,
                'live_enabled': False, 'allow_retry': False,
                'notice': 'SUPPLY_CLIENT_ID_AND_SYMBOL_FOR_READ_ONLY_RECONCILIATION'}
    result = reconcile(boundary or ReadOnlyBoundary(), client_id, symbol)
    return {'build': 'F17', 'mode': 'GET_ONLY_RECONCILIATION',
            'write_transport_enabled': False, **result}
