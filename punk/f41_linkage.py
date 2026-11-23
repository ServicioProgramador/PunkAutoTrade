"""F41: strict, read-only matching of Futures trade history to TP/SL order history.

The public Bitunix schema does not expose positionId in trade-list records.
We only link when the *exchange-provided* trade.orderId equals historical
TP/SL id, that record carries a non-empty positionId and is an unambiguous TP.
A match is audit evidence, NEVER proof of protection or LIVE readiness.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import re

from punk.f40_history import fetch_history_trades, HistoryIncomplete

_SYMBOL = re.compile(r'[A-Z0-9]{2,30}USDT\Z')
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_LIMIT = 100
_MAX_PAGES = 100

class LinkageIncomplete(ValueError):
    pass


def _integer(value):
    if type(value) is int:
        n = value
    elif type(value) is str and value and value.isascii() and value.isdecimal():
        n = int(value)
    else:
        raise LinkageIncomplete('INVALID_TPSL_TOTAL')
    if not 0 <= n <= _LIMIT * _MAX_PAGES:
        raise LinkageIncomplete('TPSL_TOTAL_OUT_OF_RANGE')
    return n


def _qty(value):
    if value is None or isinstance(value, (bool, float)):
        raise LinkageIncomplete('INVALID_TPSL_QUANTITY')
    try:
        val = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise LinkageIncomplete('INVALID_TPSL_QUANTITY') from exc
    if not val.is_finite() or val <= 0:
        raise LinkageIncomplete('INVALID_TPSL_QUANTITY')
    return val


def _ctime(row):
    """An integer millisecond timestamp is required for temporal recovery."""
    value = row.get('ctime')
    if type(value) is int:
        n = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        n = int(value)
    else:
        raise LinkageIncomplete('TPSL_CURSOR_TIMESTAMP_UNAVAILABLE')
    if not 1_000_000_000_000 <= n <= 9_999_999_999_999:
        raise LinkageIncomplete('TPSL_CURSOR_TIMESTAMP_UNAVAILABLE')
    return n


def fetch_tpsl_history(client, symbol, *, audit=None):
    """Read TP/SL history without trusting an ignored skip parameter.

    When Bitunix repeats pages despite increasing skip, switch to bounded
    endTime windows anchored to the oldest observed ctime. A filtered page
    may include the boundary timestamp, but can never be newer. If this cannot be demonstrated,
    fail closed. No approximate order/position linking is introduced.
    """
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise LinkageIncomplete('INVALID_SYMBOL')
    expected = None
    offset = 0
    ids = {}
    result = []
    exact_duplicates = 0
    variant_ids = set()
    pages = 0
    mode = 'SKIP'
    end_time = None
    raw_records = 0
    temporal_windows = 0
    boundary_times = set()
    recovery_probes = 0
    recovery_added = 0
    recovery_supported = None

    def snapshot(**more):
        if audit is not None:
            audit.update(raw_records=raw_records, distinct_ids=len(ids),
                         exact_duplicates=exact_duplicates, variant_ids=len(variant_ids),
                         pages_read=pages, expected_total=expected,
                         pagination_mode=mode, temporal_windows=temporal_windows,
                         recovery_probes=recovery_probes, recovery_added=recovery_added,
                         recovery_supported=recovery_supported, **more)

    def recover_boundary_ties():
        """Bounded GET-only check of rows hidden on time-cursor boundaries.

        A saturated history endpoint may truncate a group of TP/SL orders with
        the same millisecond ctime. Probe narrow [t-2000,t+2000] windows with both
        supported filters. Do not trust a server ignoring either filter.
        This can recover exact IDs but cannot prove completeness unless the
        original total is reached; no trading decisions depend on it.
        """
        nonlocal recovery_probes, recovery_added, recovery_supported
        if expected is None or not boundary_times:
            return
        recovery_supported = True
        # Hard cap to avoid unbounded calls, excessive API usage or rate limit.
        for stamp in sorted(boundary_times, reverse=True)[:20]:
            if len(ids) >= expected:
                break
            recovery_probes += 1
            params = {'symbol': symbol, 'skip': 0, 'limit': _LIMIT,
                      'startTime': stamp - 2000, 'endTime': stamp + 2000}
            try:
                response = client.get('tpsl_history', params)
            except Exception:
                recovery_supported = False
                break
            if not isinstance(response, dict) or type(response.get('orderList')) is not list:
                recovery_supported = False
                break
            batch = response['orderList']
            if len(batch) > _LIMIT:
                recovery_supported = False
                break
            if any(not isinstance(row, dict) or
                   not (stamp - 2000 <= _ctime(row) <= stamp + 2000) for row in batch):
                # The endpoint ignored the bounded interval; never count rows.
                recovery_supported = False
                break
            for row in batch:
                oid, pid = row.get('id'), row.get('positionId')
                if (not isinstance(oid, str) or not _ID.fullmatch(oid) or
                    not isinstance(pid, str) or not _ID.fullmatch(pid) or
                    row.get('symbol') != symbol):
                    raise LinkageIncomplete('TPSL_IDENTITY_UNVERIFIED')
                if row.get('tpQty') is not None:
                    _qty(row['tpQty'])
                if row.get('slQty') is not None:
                    _qty(row['slQty'])
                if oid in ids:
                    if ids[oid] != row:
                        variant_ids.add(oid)
                else:
                    ids[oid] = dict(row)
                    result.append(dict(row))
                    recovery_added += 1
            if variant_ids:
                raise LinkageIncomplete('TPSL_ID_VARIANTS_UNRESOLVED')
            if len(ids) > expected:
                raise LinkageIncomplete('TPSL_DISTINCT_EXCEEDS_TOTAL')

    for _ in range(_MAX_PAGES):
        params = {'symbol': symbol, 'skip': offset if mode == 'SKIP' else 0,
                  'limit': _LIMIT}
        if mode == 'TIME':
            params['endTime'] = end_time
        try:
            data = client.get('tpsl_history', params)
        except Exception as exc:
            snapshot(failure='GET_FAILED')
            raise LinkageIncomplete('TPSL_HISTORY_GET_FAILED') from exc
        if not isinstance(data, dict) or type(data.get('orderList')) is not list:
            snapshot(failure='UNEXPECTED_PAGE_SHAPE')
            raise LinkageIncomplete('TPSL_HISTORY_SHAPE_UNVERIFIED')
        total = _integer(data.get('total'))
        if expected is None:
            expected = total
        elif mode == 'SKIP' and expected != total:
            snapshot(failure='TOTAL_CHANGED', observed_total=total)
            raise LinkageIncomplete('TPSL_TOTAL_CHANGED')
        batch = data['orderList']
        pages += 1
        if len(batch) > _LIMIT:
            snapshot(failure='PAGE_EXCEEDS_LIMIT', page_rows=len(batch), page_skip=offset)
            raise LinkageIncomplete('TPSL_PAGE_EXCEEDS_LIMIT')
        if mode == 'SKIP' and offset < expected and not batch:
            snapshot(failure='EMPTY_BEFORE_TOTAL', page_rows=0, page_skip=offset)
            raise LinkageIncomplete('TPSL_PAGE_EMPTY_BEFORE_TOTAL')
        if mode == 'SKIP' and offset + len(batch) > expected:
            snapshot(failure='ROWS_EXCEED_TOTAL', page_rows=len(batch), page_skip=offset,
                     rows_if_accepted=offset + len(batch))
            raise LinkageIncomplete('TPSL_ROWS_EXCEED_TOTAL')
        if mode == 'TIME' and not batch and len(ids) < expected:
            recover_boundary_ties()
            if len(ids) == expected:
                snapshot(recovery='BOUNDARY_TIE_VERIFIED')
                return tuple(result)
            snapshot(failure='CURSOR_EXHAUSTED', page_rows=0, remaining=expected-len(ids))
            raise LinkageIncomplete('TPSL_CURSOR_EXHAUSTED_BEFORE_TOTAL')

        # Validate every row *before* considering it a duplicate or candidate.
        # Some servers treat endTime as inclusive, others exclusive.
        # Allow boundary ties but never newer rows; require new IDs to advance.
        batch_times = []
        unseen = 0
        for row in batch:
            if not isinstance(row, dict):
                raise LinkageIncomplete('INVALID_TPSL_RECORD')
            oid, pid = row.get('id'), row.get('positionId')
            if (not isinstance(oid, str) or not _ID.fullmatch(oid) or
                not isinstance(pid, str) or not _ID.fullmatch(pid) or row.get('symbol') != symbol):
                raise LinkageIncomplete('TPSL_IDENTITY_UNVERIFIED')
            if row.get('tpQty') is not None:
                _qty(row['tpQty'])
            if row.get('slQty') is not None:
                _qty(row['slQty'])
            if mode == 'TIME':
                ts = _ctime(row)
                if ts > end_time:
                    snapshot(failure='CURSOR_IGNORED', cutoff=end_time)
                    raise LinkageIncomplete('TPSL_TIME_FILTER_NOT_HONORED')
                batch_times.append(ts)
            if oid in ids:
                if ids[oid] == row:
                    exact_duplicates += 1
                else:
                    variant_ids.add(oid)
            else:
                unseen += 1
                ids[oid] = dict(row)
                result.append(dict(row))
        raw_records += len(batch)

        if variant_ids:
            snapshot(failure='ID_VARIANTS')
            raise LinkageIncomplete('TPSL_ID_VARIANTS_UNRESOLVED')
        if len(ids) > expected:
            snapshot(failure='DISTINCT_EXCEEDS_TOTAL')
            raise LinkageIncomplete('TPSL_DISTINCT_EXCEEDS_TOTAL')
        if mode == 'TIME' and len(ids) == expected and unseen == 0:
            # Final overlap produced only already-verified rows: no new IDs
            # beyond total. This also terminates inclusive endTime servers.
            snapshot()
            return tuple(result)
        if mode == 'SKIP' and offset + len(batch) == expected:
            snapshot()
            return tuple(result)
        if len(ids) == expected:
            # In temporal mode the oldest boundary must also be exhausted to
            # avoid silently omitting rows with an identical timestamp.
            if mode == 'TIME' and not batch:
                snapshot()
                return tuple(result)
            if mode == 'SKIP':
                # Existing standard skip/limit behavior: completeness by total
                # and the number of raw rows, including exact duplicates.
                if offset + len(batch) == expected:
                    snapshot()
                    return tuple(result)
            else:
                # Perform one extra, strictly older request to detect further
                # results outside the total rather than declare success early.
                if not batch_times:
                    snapshot()
                    return tuple(result)
                end_time = min(batch_times)
                temporal_windows += 1
                continue

        if batch:
            # Inspect both edges because a full page may cut through a ctime tie.
            # These are the only timestamps probed by bounded recovery.
            try:
                boundary_times.add(_ctime(batch[0]))
                boundary_times.add(_ctime(batch[-1]))
            except LinkageIncomplete:
                pass
        if mode == 'SKIP':
            # Some Bitunix accounts return the same first page for each skip.
            # If a whole nonempty page contains no fresh order IDs, stop
            # depending on skip and try a timestamp cursor; never count it as
            # a new page toward completion.
            if batch and unseen == 0 and offset > 0:
                try:
                    end_time = min(_ctime(row) for row in result)
                except LinkageIncomplete:
                    snapshot(failure='SKIP_STALLED_NO_TIMESTAMP', page_skip=offset)
                    raise LinkageIncomplete('TPSL_SKIP_STALLED_NO_CURSOR')
                mode = 'TIME'
                temporal_windows += 1
                continue
            offset += len(batch)
        else:
            if not batch_times:
                snapshot(failure='CURSOR_EMPTY')
                raise LinkageIncomplete('TPSL_CURSOR_EXHAUSTED_BEFORE_TOTAL')
            if unseen == 0:
                snapshot(failure='CURSOR_NOT_ADVANCING')
                raise LinkageIncomplete('TPSL_CURSOR_NOT_ADVANCING')
            end_time = min(batch_times)
            temporal_windows += 1
    snapshot(failure='PAGE_LIMIT_EXCEEDED')
    raise LinkageIncomplete('TPSL_HISTORY_PAGE_LIMIT')

def link_histories(trades, protections, symbol):
    """Join exchange order IDs only. No approximate price/time matching.

    Returns aggregates, never an execution ledger fit for F39: the mapping from
    original entry to position is not established by these two endpoints.
    """
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise LinkageIncomplete('INVALID_SYMBOL')
    if not isinstance(trades, (tuple, list)) or not isinstance(protections, (tuple, list)):
        raise LinkageIncomplete('INVALID_LINK_INPUT')
    by_id = {}
    for row in protections:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            raise LinkageIncomplete('TPSL_IDENTITY_UNVERIFIED')
        oid, pid = row.get('id'), row.get('positionId')
        if not isinstance(oid, str) or not _ID.fullmatch(oid) or not isinstance(pid, str) or not _ID.fullmatch(pid):
            raise LinkageIncomplete('TPSL_IDENTITY_UNVERIFIED')
        if oid in by_id:
            raise LinkageIncomplete('DUPLICATE_TPSL_ID')
        by_id[oid] = row
    seen_trades = set()
    quantity_by_order = defaultdict(Decimal)
    matched_by_position = defaultdict(lambda: {'fills': 0, 'quantity': Decimal(0), 'orders': set()})
    matched = 0
    unmatched = 0
    ambiguous = 0
    for trade in trades:
        if not isinstance(trade, dict) or trade.get('symbol') != symbol:
            raise LinkageIncomplete('TRADE_IDENTITY_UNVERIFIED')
        tid, oid = trade.get('tradeId'), trade.get('orderId')
        if not isinstance(tid, str) or not _ID.fullmatch(tid) or tid in seen_trades or not isinstance(oid, str) or not _ID.fullmatch(oid):
            raise LinkageIncomplete('TRADE_IDENTITY_UNVERIFIED')
        seen_trades.add(tid)
        q = _qty(trade.get('qty'))
        candidate = by_id.get(oid)
        if candidate is None:
            unmatched += 1
            continue
        # Only unambiguous take-profit, not a combined TP+SL record.
        if candidate.get('tpQty') is None or candidate.get('slQty') is not None or candidate.get('tpPrice') is None:
            ambiguous += 1
            continue
        # Historic trades must explicitly say reduceOnly=true to prove closing
        # rather than accidental same-order-id association with entry activity.
        if trade.get('reduceOnly') is not True:
            ambiguous += 1
            continue
        quantity_by_order[oid] += q
        if quantity_by_order[oid] > _qty(candidate['tpQty']):
            raise LinkageIncomplete('TP_FILLED_QTY_EXCEEDS_ORDER')
        pid = candidate['positionId']
        item = matched_by_position[pid]
        item['fills'] += 1
        item['quantity'] += q
        item['orders'].add(oid)
        matched += 1
    return {'matched_tp_fills': matched, 'unlinked_trades': unmatched,
            'ambiguous_matches': ambiguous, 'linked_positions': len(matched_by_position),
            'distinct_tp_orders_matched': len(quantity_by_order),
            'entry_to_position_verified': False,
            'f39_execution_ledger_verified': False,
            'protected': False, 'ready_for_live': False}


def report_linkage(client, symbol):
    """Only coarse counts/status leave this interface. Never print trade IDs."""
    base = {'build': 'F41 FIX5', 'mode': 'GET_ONLY_TPSL_LINKAGE_AUDIT', 'symbol': symbol,
            'write_enabled': False, 'orders_sent': 0, 'allow_retry': False,
            'ready_for_live': False, 'protected': False, 'f39_execution_ledger_verified': False}
    audit = {}
    try:
        trades = fetch_history_trades(client, symbol)
        protections = fetch_tpsl_history(client, symbol, audit=audit)
        matches = link_histories(trades, protections, symbol)
    except (HistoryIncomplete, LinkageIncomplete) as exc:
        return {**base, 'complete': False, 'state': str(exc), **audit}
    return {**base, 'complete': True, **audit,
            'trade_count': len(trades), 'historical_tpsl_count': len(protections),
            'state': 'EXACT_TP_LINKS_AUDIT_ONLY' if matches['matched_tp_fills'] else 'NO_EXACT_TP_LINKS',
            **matches, 'reason': 'ENTRY_POSITION_AND_CLOSE_PROOF_NOT_VERIFIED'}
