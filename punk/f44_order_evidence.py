"""F44: GET-only audit of historical fills against historical orders.

A matching historical order proves only order metadata for the same orderId;
it does NOT prove a specific TP, entry-to-position linkage or current SL.
No writes, API keys, individual IDs, quantities or balances in reports.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from punk.f40_history import fetch_history_trades, HistoryIncomplete
from punk.f41_linkage import _ID, _SYMBOL

PAGE_LIMIT = 100
MAX_PAGES = 100

class OrderEvidenceError(ValueError):
    pass


def _decimal(value, *, positive=False):
    if value is None or type(value) in (bool, float):
        raise OrderEvidenceError('ORDER_NUMERIC_INVALID')
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OrderEvidenceError('ORDER_NUMERIC_INVALID') from exc
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        raise OrderEvidenceError('ORDER_NUMERIC_INVALID')
    return value


def _total(value):
    if type(value) is int:
        result = value
    elif type(value) is str and value and value.isascii() and value.isdecimal():
        result = int(value)
    else:
        raise OrderEvidenceError('ORDER_TOTAL_INVALID')
    if not 0 <= result <= PAGE_LIMIT * MAX_PAGES:
        raise OrderEvidenceError('ORDER_TOTAL_INVALID')
    return result


def fetch_history_orders(client, symbol):
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise OrderEvidenceError('INVALID_SYMBOL')
    seen = {}
    total = None
    skip = 0
    for _ in range(MAX_PAGES):
        try:
            page = client.get('history_orders', {'symbol':symbol,'skip':skip,'limit':PAGE_LIMIT})
        except Exception as exc:
            raise OrderEvidenceError('ORDER_HISTORY_GET_FAILED') from exc
        if type(page) is not dict or type(page.get('orderList')) is not list:
            raise OrderEvidenceError('ORDER_HISTORY_SHAPE_INVALID')
        count = _total(page.get('total'))
        if total is None:
            total = count
        elif count != total:
            raise OrderEvidenceError('ORDER_TOTAL_CHANGED')
        batch = page['orderList']
        if len(batch) > PAGE_LIMIT or skip + len(batch) > count or (skip < count and not batch):
            raise OrderEvidenceError('ORDER_HISTORY_PAGE_INCOMPLETE')
        for row in batch:
            if type(row) is not dict or row.get('symbol') != symbol:
                raise OrderEvidenceError('ORDER_HISTORY_IDENTITY_INVALID')
            oid = row.get('orderId')
            if type(oid) is not str or not _ID.fullmatch(oid) or oid in seen:
                raise OrderEvidenceError('ORDER_HISTORY_DUPLICATE_OR_INVALID_ID')
            ro = row.get('reduceOnly')
            if ro is not None and type(ro) is not bool:
                raise OrderEvidenceError('ORDER_REDUCE_ONLY_INVALID')
            qty = _decimal(row.get('qty'), positive=True)
            filled = _decimal(row.get('tradeQty', '0'))
            if filled > qty:
                raise OrderEvidenceError('ORDER_FILLED_EXCEEDS_QUANTITY')
            seen[oid] = {'orderId':oid, 'symbol':symbol,'reduceOnly':ro,
                         'qty':qty,'tradeQty':filled,
                         'clientId':row.get('clientId') if type(row.get('clientId')) is str else None,
                         'status':row.get('status') if type(row.get('status')) is str else None}
        skip += len(batch)
        if skip == count:
            return seen
    raise OrderEvidenceError('ORDER_HISTORY_PAGINATION_LIMIT')


def audit_orders(client, symbol):
    base = {'build':'F44','symbol':symbol,'mode':'GET_ONLY_ORDER_EVIDENCE_AUDIT',
            'write_enabled':False,'orders_sent':0,'allow_retry':False,
            'ready_for_live':False,'protected':False,'tp_fill_verified':False,
            'entry_to_position_verified':False,'f39_execution_ledger_verified':False}
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        return {**base,'complete':False,'state':'INVALID_SYMBOL'}
    try:
        fills = fetch_history_trades(client, symbol)
        orders = fetch_history_orders(client, symbol)
    except (HistoryIncomplete, OrderEvidenceError) as exc:
        return {**base,'complete':False,'state':str(exc)}
    grouped = defaultdict(list)
    for fill in fills:
        grouped[fill['orderId']].append(fill)
    matched=0; explicit_closes=0; entries=0; unknown=0; conflicts=0; qty_conflicts=0
    for oid, group in grouped.items():
        order = orders.get(oid)
        if order is None:
            continue
        matched += len(group)
        filled_sum = sum((_decimal(x['qty'], positive=True) for x in group), Decimal('0'))
        if filled_sum > order['qty'] or filled_sum > order['tradeQty']:
            qty_conflicts += 1
        for fill in group:
            ro_fill=fill['reduceOnly'];ro_order=order['reduceOnly']
            if ro_fill is not None and ro_order is not None and ro_fill != ro_order:
                conflicts += 1
            elif ro_fill is True and ro_order is True:
                explicit_closes += 1
            elif ro_fill is False and ro_order is False:
                entries += 1
            else:
                unknown += 1
            if fill.get('clientId') and order['clientId'] and fill['clientId'] != order['clientId']:
                conflicts += 1
    complete = not (conflicts or qty_conflicts)
    return {**base,'complete':complete,
            'state':('ORDER_EVIDENCE_CONFLICT' if not complete else 'ORDER_EVIDENCE_AUDITED_UNLINKED'),
            'reason':('ORDER_METADATA_CONFLICT' if not complete else 'HISTORICAL_ORDERS_NOT_PROOF_OF_TP_OR_CURRENT_PROTECTION'),
            'trade_count':len(fills),'history_order_count':len(orders),
            'matched_fills':matched,'unmatched_fills':len(fills)-matched,
            'explicit_reduce_only_matches':explicit_closes,
            'explicit_entry_matches':entries,'unknown_direction_matches':unknown,
            'identity_conflicts':conflicts,'quantity_conflicts':qty_conflicts}
