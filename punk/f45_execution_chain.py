"""F45: read-only, cross-source historical order/fill/position evidence.

Corroboration is historical metadata, not proof of a specific take-profit,
current protective orders, or permission to open/retry a LIVE order.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from punk.f40_history import fetch_history_trades, HistoryIncomplete
from punk.f41_linkage import fetch_tpsl_history, LinkageIncomplete, _SYMBOL, _ID
from punk.f42_position_audit import _read_scoped, PositionAuditError
from punk.f44_order_evidence import fetch_history_orders, OrderEvidenceError, _decimal

MAX_POSITIONS = 64


def audit_execution_chain(client, symbol):
    base = {'build':'F45', 'symbol':symbol, 'mode':'GET_ONLY_CROSS_SOURCE_CHAIN',
            'write_enabled':False, 'orders_sent':0, 'allow_retry':False,
            'ready_for_live':False, 'protected':False, 'tp_fill_verified':False,
            'live_execution_verified':False, 'entry_to_position_verified':False}
    if not isinstance(symbol,str) or not _SYMBOL.fullmatch(symbol):
        return {**base, 'complete':False, 'state':'INVALID_SYMBOL'}
    try:
        fills=fetch_history_trades(client, symbol)
        orders=fetch_history_orders(client,symbol)
        tpsl=fetch_tpsl_history(client,symbol)
    except (HistoryIncomplete,OrderEvidenceError,LinkageIncomplete) as exc:
        return {**base,'complete':False,'state':str(exc)}
    positions=sorted({r['positionId'] for r in tpsl})
    if len(positions)>MAX_POSITIONS:
        return {**base,'complete':False,'state':'POSITION_SCOPE_LIMIT_EXCEEDED',
                'position_count':len(positions)}
    baseline={f['tradeId']:f for f in fills}
    assignments={}
    conflicts=0
    scoped_count=0
    try:
        for pid in positions:
            if type(pid) is not str or not _ID.fullmatch(pid):
                raise PositionAuditError('INVALID_POSITION_ID')
            for row in _read_scoped(client,symbol,pid):
                scoped_count+=1
                tid=row['tradeId']
                prior=assignments.get(tid)
                if prior is not None and prior!=pid:
                    conflicts+=1
                    continue
                original=baseline.get(tid)
                if original is None or row['orderId']!=original['orderId']:
                    conflicts+=1
                    continue
                # The scoped endpoint may return explicit inconsistent metadata.
                if row.get('positionId') is not None and row['positionId']!=pid:
                    conflicts+=1
                    continue
                if row.get('reduceOnly') is not None and original['reduceOnly'] is not None and row['reduceOnly']!=original['reduceOnly']:
                    conflicts+=1
                    continue
                for field in ('qty','price','fee'):
                    if field in row and row[field] is not None and _decimal(row[field]) != _decimal(original[field]):
                        conflicts+=1
                        break
                else:
                    assignments[tid]=pid
    except (PositionAuditError,OrderEvidenceError) as exc:
        return {**base,'complete':False,'state':str(exc), 'positions_probed':len(positions)}
    # Independently aggregate all order fills so a partial fill does not
    # accidentally exceed the source order's cumulative executed quantity.
    quantities=defaultdict(lambda:Decimal('0'))
    matching=0; closes=0; entries=0; unknown=0
    for f in fills:
        o=orders.get(f['orderId'])
        if o is None:
            continue
        quantities[f['orderId']]+=_decimal(f['qty'],positive=True)
        if f['clientId'] and o['clientId'] and f['clientId']!=o['clientId']:
            conflicts+=1
        if f['reduceOnly'] is not None and o['reduceOnly'] is not None and f['reduceOnly'] != o['reduceOnly']:
            conflicts+=1
        if f['tradeId'] in assignments:
            matching+=1
            if f['reduceOnly'] is True and o['reduceOnly'] is True: closes+=1
            elif f['reduceOnly'] is False and o['reduceOnly'] is False: entries+=1
            else: unknown+=1
    for oid, total in quantities.items():
        o=orders[oid]
        if total>o['qty'] or total>o['tradeQty']:
            conflicts+=1
    complete=conflicts==0
    return {**base,'complete':complete,
            'state':'CROSS_SOURCE_CONFLICT' if conflicts else 'HISTORICAL_CHAIN_AUDITED_NO_LIVE_PROOF',
            'reason':'HISTORICAL_EVIDENCE_NOT_CURRENT_PROTECTION_OR_TP_CAUSALITY',
            'trade_count':len(fills),'history_order_count':len(orders),
            'historical_tpsl_count':len(tpsl),'positions_probed':len(positions),
            'position_scoped_fills':len(assignments),
            'fills_with_historical_order_and_position':matching,
            'fills_without_three_source_chain':len(fills)-matching,
            'three_source_reduce_only_fills':closes,
            'three_source_entry_fills':entries,
            'three_source_unknown_direction':unknown,
            'identity_or_quantity_conflicts':conflicts}
