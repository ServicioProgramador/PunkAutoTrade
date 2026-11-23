"""F40: read-only Bitunix Futures trade-history intake, conservative by design.

This validates official GET history-trades pagination and fill identities but does
NOT guess which TP or position a trade belongs to. Mapping F39 remains blocked
without independently verified orderId->TP/position links.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import re

_SYMBOL = re.compile(r'[A-Z0-9]{2,30}USDT\Z')
_ORDER_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_MAX_PAGES = 100
_PAGE_LIMIT = 100

class HistoryIncomplete(ValueError):
    pass

def _integer(value):
    if type(value) is int:
        out = value
    elif type(value) is str and value and value.isascii() and value.isdecimal():
        out = int(value)
    else:
        raise HistoryIncomplete('INVALID_TOTAL')
    if out < 0 or out > _MAX_PAGES * _PAGE_LIMIT:
        raise HistoryIncomplete('TOTAL_OUT_OF_RANGE')
    return out

def _positive_decimal(value, *, allow_zero=False):
    if value is None or type(value) in (bool, float):
        raise HistoryIncomplete('INVALID_TRADE_QUANTITY')
    try:
        out = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise HistoryIncomplete('INVALID_TRADE_QUANTITY') from exc
    if not out.is_finite() or (out < 0 if allow_zero else out <= 0):
        raise HistoryIncomplete('INVALID_TRADE_QUANTITY')
    return out

def fetch_history_trades(client, symbol, *, order_id=None):
    """Return normalized fills only after all pages validate, no partial results.

    `client` is an injected ReadOnlyBitunix-compatible GET-only client.
    Nothing is sent or persisted. Existing trades are never imported as signals.
    """
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise HistoryIncomplete('INVALID_SYMBOL')
    if order_id is not None and (not isinstance(order_id,str) or not _ORDER_ID.fullmatch(order_id)):
        raise HistoryIncomplete('INVALID_ORDER_ID')
    seen = {}
    offset = 0
    expected_total = None
    for _ in range(_MAX_PAGES):
        params = {'symbol':symbol, 'skip':offset, 'limit':_PAGE_LIMIT}
        if order_id is not None:
            params['orderId'] = order_id
        try:
            page = client.get('history_trades', params)
        except Exception as exc:
            raise HistoryIncomplete('HISTORY_GET_FAILED') from exc
        if not isinstance(page, dict) or type(page.get('tradeList')) is not list:
            raise HistoryIncomplete('INVALID_TRADE_LIST')
        count = _integer(page.get('total'))
        if expected_total is None:
            expected_total = count
        elif count != expected_total:
            raise HistoryIncomplete('TOTAL_CHANGED_DURING_PAGINATION')
        batch = page['tradeList']
        if len(batch) > _PAGE_LIMIT or offset + len(batch) > count:
            raise HistoryIncomplete('PAGE_LENGTH_CONFLICT')
        if count and not batch:
            raise HistoryIncomplete('MISSING_PAGE')
        for trade in batch:
            if not isinstance(trade, dict):
                raise HistoryIncomplete('INVALID_TRADE_RECORD')
            tid, oid = trade.get('tradeId'), trade.get('orderId')
            if not isinstance(tid, str) or not _ORDER_ID.fullmatch(tid):
                raise HistoryIncomplete('INVALID_TRADE_ID')
            if not isinstance(oid, str) or not _ORDER_ID.fullmatch(oid):
                raise HistoryIncomplete('INVALID_TRADE_ORDER_ID')
            if trade.get('symbol') != symbol or (order_id is not None and oid != order_id):
                raise HistoryIncomplete('HISTORY_IDENTITY_CONFLICT')
            qty = _positive_decimal(trade.get('qty'))
            price = _positive_decimal(trade.get('price'))
            fee = _positive_decimal(trade.get('fee', '0'),allow_zero=True)
            timestamp_raw = trade.get('ctime')
            if type(timestamp_raw) is int:
                timestamp = timestamp_raw
            elif type(timestamp_raw) is str and timestamp_raw.isascii() and timestamp_raw.isdecimal():
                timestamp = int(timestamp_raw)
            else:
                timestamp = -1
            if not 1 <= timestamp <= 9999999999999999:
                raise HistoryIncomplete('INVALID_TRADE_TIMESTAMP')
            if tid in seen:
                raise HistoryIncomplete('DUPLICATE_TRADE_ID')
            seen[tid] = {'tradeId':tid,'orderId':oid,'symbol':symbol,'qty':str(qty),
                         'price':str(price),'fee':str(fee),'ctime':timestamp,
                         'clientId':trade.get('clientId') if isinstance(trade.get('clientId'),str) else None,
                         'reduceOnly':trade.get('reduceOnly') if type(trade.get('reduceOnly')) is bool else None}
        offset += len(batch)
        if offset == count:
            return tuple(seen.values())
    raise HistoryIncomplete('PAGINATION_LIMIT_EXCEEDED')

def report_history(client, symbol):
    """Safe console report: no IDs, quantities, order contents, fees or balances."""
    result = {'build':'F40', 'mode':'GET_ONLY_HISTORY_AUDIT', 'symbol':symbol,
              'write_enabled':False, 'orders_sent':0, 'allow_retry':False,
              'ready_for_live':False, 'protected':False,
              'f39_execution_ledger_verified':False}
    try:
        trades = fetch_history_trades(client,symbol)
    except HistoryIncomplete as exc:
        return {**result,'state':str(exc),'complete':False}
    return {**result,'state':'HISTORY_READ_COMPLETE_UNLINKED', 'complete':True,
            'trade_count':len(trades), 'distinct_order_count':len({t['orderId'] for t in trades}),
            'reason':'TP_ORDER_POSITION_LINK_NOT_VERIFIED'}
