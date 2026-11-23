"""F42: bounded, GET-only position scoped history cross-check.

This is *not* a proof of live protection, and scoped API filtering alone is not
proof of the entry-to-position relationship. No identifiers leave the report.
"""
from __future__ import annotations
from collections import defaultdict
from punk.f40_history import fetch_history_trades, HistoryIncomplete
from punk.f41_linkage import fetch_tpsl_history, LinkageIncomplete, _SYMBOL, _ID

MAX_POSITION_PROBES = 3
MAX_POSITION_PAGES = 8
PAGE_SIZE = 100

class PositionAuditError(ValueError):
    pass


def _read_scoped(client, symbol, position_id):
    """Read bounded paginated GET history with positionId filter; fail closed."""
    seen = {}
    total = None
    skip = 0
    for _ in range(MAX_POSITION_PAGES):
        try:
            response = client.get('history_trades', {'symbol': symbol, 'positionId': position_id,
                                                     'skip': skip, 'limit': PAGE_SIZE})
        except Exception as exc:
            raise PositionAuditError('POSITION_HISTORY_GET_FAILED') from exc
        if not isinstance(response, dict) or type(response.get('tradeList')) is not list:
            raise PositionAuditError('POSITION_HISTORY_SHAPE_INVALID')
        raw_total = response.get('total')
        if type(raw_total) is int:
            n = raw_total
        elif type(raw_total) is str and raw_total.isascii() and raw_total.isdecimal():
            n = int(raw_total)
        else:
            raise PositionAuditError('POSITION_HISTORY_TOTAL_INVALID')
        if not 0 <= n <= MAX_POSITION_PAGES * PAGE_SIZE:
            raise PositionAuditError('POSITION_HISTORY_TOTAL_INVALID')
        if total is None:
            total = n
        elif n != total:
            raise PositionAuditError('POSITION_HISTORY_TOTAL_CHANGED')
        batch = response['tradeList']
        if len(batch) > PAGE_SIZE or skip + len(batch) > total:
            raise PositionAuditError('POSITION_HISTORY_PAGE_INCONSISTENT')
        if skip < total and not batch:
            raise PositionAuditError('POSITION_HISTORY_PAGE_MISSING')
        for row in batch:
            if not isinstance(row, dict) or row.get('symbol') != symbol:
                raise PositionAuditError('POSITION_HISTORY_IDENTITY_INVALID')
            tid, oid = row.get('tradeId'), row.get('orderId')
            if not isinstance(tid, str) or not _ID.fullmatch(tid) or not isinstance(oid, str) or not _ID.fullmatch(oid):
                raise PositionAuditError('POSITION_HISTORY_IDENTITY_INVALID')
            if tid in seen:
                raise PositionAuditError('POSITION_HISTORY_DUPLICATE_TRADE')
            if row.get('positionId') is not None and row['positionId'] != position_id:
                raise PositionAuditError('POSITION_HISTORY_POSITION_CONFLICT')
            seen[tid] = row
        skip += len(batch)
        if skip == total:
            return tuple(seen.values())
    raise PositionAuditError('POSITION_HISTORY_PAGINATION_LIMIT')


def audit(client, symbol, *, max_positions=MAX_POSITION_PROBES):
    """Report only aggregate counters. No orders, ids, or balances included."""
    base = {'build': 'F42', 'symbol': symbol, 'mode': 'GET_ONLY_POSITION_SCOPED_AUDIT',
            'write_enabled': False, 'orders_sent': 0, 'allow_retry': False,
            'protected': False, 'ready_for_live': False,
            'entry_to_position_verified': False, 'f39_execution_ledger_verified': False}
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        return {**base, 'complete': False, 'state': 'INVALID_SYMBOL'}
    if type(max_positions) is not int or not 1 <= max_positions <= MAX_POSITION_PROBES:
        return {**base, 'complete': False, 'state': 'INVALID_PROBE_LIMIT'}
    try:
        trades = fetch_history_trades(client, symbol)
        protections = fetch_tpsl_history(client, symbol)
    except (HistoryIncomplete, LinkageIncomplete) as exc:
        return {**base, 'complete': False, 'state': str(exc), 'source_complete': False}
    baseline = {x['tradeId']: x for x in trades}
    positions = sorted({x['positionId'] for x in protections})
    sample = positions[:max_positions]
    report = {**base, 'source_complete': True, 'trade_count': len(trades),
              'historical_tpsl_count': len(protections), 'distinct_tpsl_positions': len(positions),
              'positions_probed': 0, 'scoped_fills': 0, 'candidate_fills': 0,
              'scoped_fills_outside_baseline': 0, 'explicit_position_ids': 0,
              'scope_conflicts': 0, 'remaining_positions_unprobed': len(positions) - len(sample)}
    assignment = {}
    try:
        for pid in sample:
            if not isinstance(pid, str) or not _ID.fullmatch(pid):
                raise PositionAuditError('INVALID_POSITION_ID')
            scoped = _read_scoped(client, symbol, pid)
            report['positions_probed'] += 1
            report['scoped_fills'] += len(scoped)
            for fill in scoped:
                tid = fill['tradeId']
                if tid not in baseline or baseline[tid]['orderId'] != fill['orderId']:
                    report['scoped_fills_outside_baseline'] += 1
                    continue
                prior = assignment.get(tid)
                if prior is not None and prior != pid:
                    report['scope_conflicts'] += 1
                    continue
                assignment[tid] = pid
                if fill.get('positionId') == pid:
                    report['explicit_position_ids'] += 1
                report['candidate_fills'] += 1
    except PositionAuditError as exc:
        return {**report, 'complete': False, 'state': str(exc)}
    if report['scope_conflicts']:
        return {**report, 'complete': False, 'state': 'POSITION_FILTER_NOT_RELIABLE'}
    if report['scoped_fills_outside_baseline']:
        return {**report, 'complete': False, 'state': 'POSITION_HISTORY_NOT_IN_BASELINE'}
    # Position filter alone is server-side metadata, not proof of a closing TP
    # or a protected open position. No speculative order/position assignments.
    return {**report, 'complete': True,
            'state': 'POSITION_SCOPED_CANDIDATES_AUDIT_ONLY' if assignment else 'NO_POSITION_SCOPED_FILLS',
            'reason': 'FILTERED_TRADE_LIST_IS_NOT_PROOF_OF_TP_FILL_OR_LIVE_PROTECTION'}
