"""F18 GET-only protection coverage audit.

An observed pending TP/SL record is *not* proof of exchange trigger execution,
price realization, or that a future LIVE path is safe. Never authorizes retry.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from punk.bitunix_reconciliation import ReadOnlyBoundary, _CLIENT, _SYMBOL


def _d(v, *, strictly_positive=True):
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not n.is_finite() or (n <= 0 if strictly_positive else n < 0):
        return None
    return n


def _deny(code):
    return {'state': code, 'snapshot_coverage_verified': False, 'protected': False,
            'allow_retry': False, 'ready_for_live': False, 'live_enabled': False}


def _verified():
    return {'state': 'PENDING_PROTECTION_SNAPSHOT_MATCH',
            'snapshot_coverage_verified': True, 'protected': False,
            'allow_retry': False, 'ready_for_live': False, 'live_enabled': False,
            'notice': 'GET_ONLY_PENDING_IS_NOT_TRIGGER_OR_EXECUTION_PROOF'}


def audit(boundary, *, client_id, symbol, side, quantity, entry_price, sl_price, tp_levels,
          stop_trigger='MARK_PRICE', tp_trigger='MARK_PRICE'):
    """Compare one fully identified fill/position with exact intended SL/TP coverage.

    tp_levels is [{"price": str, "qty": str}, ...], 1..5. Requires a single
    full-quantity MARKET SL and exactly the intended MARKET TP records. Unknown,
    extra or mismatched records are deliberately considered inconclusive.
    """
    if (not isinstance(client_id, str) or not _CLIENT.fullmatch(client_id)
            or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
            or side not in ('LONG', 'SHORT') or _d(quantity) is None
            or _d(entry_price) is None or _d(sl_price) is None
            or stop_trigger not in ('MARK_PRICE', 'LAST_PRICE')
            or tp_trigger not in ('MARK_PRICE', 'LAST_PRICE')
            or not isinstance(tp_levels, (tuple, list)) or not 1 <= len(tp_levels) <= 5):
        return _deny('INVALID_AUDIT_INTENT')
    q, entry, stop = _d(quantity), _d(entry_price), _d(sl_price)
    targets = []
    for item in tp_levels:
        if not isinstance(item, dict) or set(item) != {'price', 'qty'}:
            return _deny('INVALID_TP_PLAN')
        price, size = _d(item['price']), _d(item['qty'])
        if price is None or size is None or (price, size) in targets:
            return _deny('INVALID_TP_PLAN')
        targets.append((price, size))
    if sum((size for _, size in targets), Decimal('0')) != q:
        return _deny('TP_ALLOCATION_MISMATCH')
    favorable = (lambda price: price > entry) if side == 'LONG' else (lambda price: price < entry)
    if favorable(stop) or stop == entry or not all(favorable(price) for price, _ in targets):
        return _deny('INVALID_PRICE_GEOMETRY')
    try:
        order = boundary.request('GET', 'order_detail', {'clientId': client_id})
    except Exception:
        return _deny('ORDER_LOOKUP_UNKNOWN')
    if not isinstance(order, dict) or order.get('clientId') != client_id or order.get('symbol') != symbol or not str(order.get('orderId') or '').strip():
        return _deny('ORDER_IDENTITY_UNCONFIRMED')
    if order.get('status') != 'FILLED' or _d(order.get('tradeQty')) != q:
        return _deny('FILL_NOT_EXACTLY_CONFIRMED')
    if order.get('side') != ('BUY' if side == 'LONG' else 'SELL') or order.get('reduceOnly') is not False:
        return _deny('ORDER_DIRECTION_UNCONFIRMED')
    try:
        snap = boundary.request('GET', 'account_snapshot')
    except Exception:
        return _deny('SNAPSHOT_UNAVAILABLE')
    if not isinstance(snap, dict) or snap.get('status') != 'GET_ONLY_VERIFIED':
        return _deny('SNAPSHOT_UNAVAILABLE')
    positions, pending = snap.get('positions'), snap.get('pending_tpsl')
    if not isinstance(positions, list) or not isinstance(pending, list):
        return _deny('SNAPSHOT_UNAVAILABLE')
    ps = [p for p in positions if isinstance(p, dict) and p.get('symbol') == symbol
          and p.get('side') == side and _d(p.get('qty')) == q and str(p.get('positionId') or '')]
    if len(ps) != 1:
        return _deny('POSITION_AMBIGUOUS_OR_CHANGED')
    pos_id = str(ps[0]['positionId'])
    relevant = [p for p in pending if isinstance(p, dict) and str(p.get('positionId') or '') == pos_id]
    if not relevant or any(p.get('symbol') != symbol for p in relevant):
        return _deny('PROTECTION_LINKAGE_UNCONFIRMED')
    if len({str(p.get('id') or '') for p in relevant}) != len(relevant) or any(not p.get('id') for p in relevant):
        return _deny('PROTECTION_IDENTITIES_INVALID')
    # Insist upon exact coverage, not just sums of unverified or overlapping orders.
    stops = [p for p in relevant if _d(p.get('slQty')) is not None]
    tps = [p for p in relevant if _d(p.get('tpQty')) is not None]
    if len(stops) != 1 or _d(stops[0].get('slQty')) != q or _d(stops[0].get('slPrice')) != stop:
        return _deny('STOP_COVERAGE_UNCONFIRMED')
    if stops[0].get('slStopType') != stop_trigger or stops[0].get('slOrderType') != 'MARKET':
        return _deny('STOP_EXECUTION_CONTRACT_UNCONFIRMED')
    if len(tps) != len(targets):
        return _deny('TP_COUNT_MISMATCH')
    found = []
    for p in tps:
        price, size = _d(p.get('tpPrice')), _d(p.get('tpQty'))
        if price is None or size is None or p.get('tpStopType') != tp_trigger or p.get('tpOrderType') != 'MARKET':
            return _deny('TP_EXECUTION_CONTRACT_UNCONFIRMED')
        found.append((price, size))
    if sorted(found) != sorted(targets):
        return _deny('TP_COVERAGE_MISMATCH')
    if any(_d(p.get('slQty')) is None and _d(p.get('tpQty')) is None for p in relevant):
        return _deny('UNACCOUNTED_PROTECTION_RECORD')
    return _verified()


def offline_report():
    return {'build': 'F18', 'mode': 'GET_ONLY_PROTECTION_AUDIT',
            'live_enabled': False, 'write_transport_enabled': False,
            'ready_for_live': False, 'snapshot_coverage_verified': False,
            'notice': 'OFFLINE_NO_ACCOUNT_QUERIES'}
