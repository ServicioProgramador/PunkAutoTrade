"""F43: bounded full-history reconciliation by position; GET-only, fail-closed.

Historical position-scoped fills do NOT prove entry/TP causality, and this
module deliberately never grants trading or retries. Only aggregate counters
leave the report. The caller supplies a GET-only client.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from punk.f40_history import fetch_history_trades, HistoryIncomplete
from punk.f41_linkage import fetch_tpsl_history, LinkageIncomplete, _ID, _SYMBOL
from punk.f42_position_audit import _read_scoped, PositionAuditError

MAX_AUDIT_POSITIONS = 64  # hard upper bound on independently filtered GET scopes


def _number(value):
    if value is None or isinstance(value, (bool, float)):
        raise ValueError('INVALID_SCOPED_NUMERIC_VALUE')
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('INVALID_SCOPED_NUMERIC_VALUE') from exc
    if not n.is_finite() or n < 0:
        raise ValueError('INVALID_SCOPED_NUMERIC_VALUE')
    return n


def audit_all(client, symbol, *, max_positions=MAX_AUDIT_POSITIONS):
    """Audit complete or explicitly partial snapshot. No persisted state."""
    base = {'build': 'F43', 'symbol': symbol, 'mode': 'GET_ONLY_ALL_POSITION_AUDIT',
            'write_enabled': False, 'orders_sent': 0, 'allow_retry': False,
            'protected': False, 'ready_for_live': False,
            'entry_to_position_verified': False, 'f39_execution_ledger_verified': False,
            'tp_fill_verified': False, 'live_execution_verified': False}
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        return {**base, 'complete': False, 'state': 'INVALID_SYMBOL'}
    if type(max_positions) is not int or not 1 <= max_positions <= MAX_AUDIT_POSITIONS:
        return {**base, 'complete': False, 'state': 'INVALID_PROBE_LIMIT'}
    try:
        trades = fetch_history_trades(client, symbol)
        tpsls = fetch_tpsl_history(client, symbol)
    except (HistoryIncomplete, LinkageIncomplete) as exc:
        return {**base, 'complete': False, 'source_complete': False, 'state': str(exc)}
    baseline = {t['tradeId']: t for t in trades}
    positions = sorted({p['positionId'] for p in tpsls})
    for pid in positions:
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            return {**base, 'complete': False, 'source_complete': True, 'state': 'INVALID_POSITION_ID'}
    sample = positions[:max_positions]
    result = {**base, 'source_complete': True, 'trade_count': len(trades),
              'historical_tpsl_count': len(tpsls), 'distinct_tpsl_positions': len(positions),
              'positions_probed': 0, 'remaining_positions_unprobed': len(positions) - len(sample),
              'scoped_fills': 0, 'candidate_fills': 0, 'explicit_position_ids': 0,
              'explicit_reduce_only_fills': 0, 'unknown_reduce_only_fills': 0,
              'scoped_fills_outside_baseline': 0, 'scope_conflicts': 0,
              'identity_conflicts': 0, 'unassigned_baseline_fills': len(trades),
              'position_coverage_complete': False}
    assignments = {}
    try:
        for pid in sample:
            fills = _read_scoped(client, symbol, pid)
            result['positions_probed'] += 1
            result['scoped_fills'] += len(fills)
            for fill in fills:
                tid = fill['tradeId']
                original = baseline.get(tid)
                if original is None:
                    result['scoped_fills_outside_baseline'] += 1
                    continue
                if original['orderId'] != fill['orderId']:
                    result['identity_conflicts'] += 1
                    continue
                for key in ('qty', 'price', 'fee'):
                    if key in fill and fill[key] is not None:
                        if _number(fill[key]) != _number(original[key]):
                            result['identity_conflicts'] += 1
                            break
                else:
                    prior = assignments.get(tid)
                    if prior is not None and prior != pid:
                        result['scope_conflicts'] += 1
                        continue
                    # Skip duplicates within the same position even if returned
                    # inconsistently by API across independent queries.
                    if prior == pid:
                        continue
                    assignments[tid] = pid
                    result['candidate_fills'] += 1
                    if fill.get('positionId') == pid:
                        result['explicit_position_ids'] += 1
                    if fill.get('reduceOnly') is True:
                        result['explicit_reduce_only_fills'] += 1
                    elif fill.get('reduceOnly') is not False:
                        result['unknown_reduce_only_fills'] += 1
    except (PositionAuditError, ValueError) as exc:
        return {**result, 'state': str(exc), 'complete': False}
    result['unassigned_baseline_fills'] = len(baseline) - len(assignments)
    if result['scope_conflicts']:
        return {**result, 'complete': False, 'state': 'POSITION_FILTER_NOT_RELIABLE'}
    if result['scoped_fills_outside_baseline']:
        return {**result, 'complete': False, 'state': 'POSITION_HISTORY_NOT_IN_BASELINE'}
    if result['identity_conflicts']:
        return {**result, 'complete': False, 'state': 'POSITION_TRADE_FIELDS_CONFLICT'}
    # Complete means every historical TP/SL position in this snapshot was
    # queried successfully, NOT that all trades are mapped, or TP is proven.
    result['position_coverage_complete'] = result['remaining_positions_unprobed'] == 0
    result['complete'] = result['position_coverage_complete']
    result['state'] = ('POSITION_HISTORY_SCOPE_AUDITED_UNLINKED' if result['complete']
                       else 'POSITION_HISTORY_PARTIAL_SCOPE_AUDIT')
    result['reason'] = 'POSITION_FILTER_DOES_NOT_PROVE_ENTRY_OR_TP_OR_LIVE_PROTECTION'
    return result
