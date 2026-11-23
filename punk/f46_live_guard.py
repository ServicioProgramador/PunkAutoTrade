"""F46: current Futures exposure/protection auditor (GET only, never permission to trade).

A pending TP/SL object is only an observed order, NOT verified executable protection.
No SQL, write transport, client secrets or order placement in this module.
"""
from decimal import Decimal, InvalidOperation
import re
import time

_SYMBOL=re.compile(r'^[A-Z0-9]{2,28}USDT$')

def _positive(value):
    if isinstance(value, bool): return None
    try:
        d=Decimal(str(value))
        return d if d.is_finite() and d>0 else None
    except (ValueError, TypeError, InvalidOperation):return None

def guard_snapshot(snapshot, symbol, *, now=None):
    base=dict(build='F46',mode='GET_ONLY_CURRENT_EXPOSURE_GUARD',symbol=symbol,
              complete=False,ready_for_live=False,live_enabled=False,
              write_enabled=False,orders_sent=0,allow_retry=False,
              real_sl_tp_confirmed=False)
    if not isinstance(symbol,str) or not _SYMBOL.fullmatch(symbol):
        return {**base,'state':'INVALID_SYMBOL'}
    if not isinstance(snapshot,dict) or snapshot.get('status')!='GET_ONLY_VERIFIED':
        return {**base,'state':'GET_ONLY_UNAVAILABLE'}
    ts=snapshot.get('checked_at')
    clock=time.time() if now is None else now
    if type(ts) not in (int,float) or type(clock) not in (int,float) or not 0<=clock-ts<=90:
        return {**base,'state':'STALE_SNAPSHOT'}
    positions=snapshot.get('positions');orders=snapshot.get('pending_orders');tpsl=snapshot.get('pending_tpsl')
    if any(not isinstance(items,list) or any(not isinstance(x,dict) for x in items) for items in (positions,orders,tpsl)):
        return {**base,'state':'INCOMPLETE_INVENTORY'}
    # No instrument can be safely declared inactive if the API returned a non-string symbol.
    if any(type(x.get('symbol')) is not str for x in positions+orders+tpsl):
        return {**base,'state':'UNIDENTIFIED_INSTRUMENT'}
    ps=[x for x in positions if x['symbol']==symbol]
    os=[x for x in orders if x['symbol']==symbol]
    tsls=[x for x in tpsl if x['symbol']==symbol]
    result={**base,'complete':True,'positions_open':len(ps),
            'pending_orders':len(os),'pending_tpsl':len(tsls)}
    if not ps:
        return {**result,'state':'NO_OPEN_POSITION_AUDITED' if not os and not tsls else 'ORPHAN_ORDERS_REVIEW',
                'coverage_observed':False,'reason':'NO_ACTIVE_POSITION_TO_VALIDATE' if not os and not tsls else 'ORDERS_WITHOUT_POSITION_REQUIRE_REVIEW'}
    ids=set()
    for p in ps:
        pid=p.get('positionId')
        if not isinstance(pid,str) or not pid or pid in ids or _positive(p.get('qty')) is None:
            return {**result,'state':'INVALID_POSITION_INVENTORY','complete':False}
        ids.add(pid)
    # Strict: a pending stop cannot be counted unless it is a single unambiguous
    # independently identified object tied to the exact current position.
    failed=0;covered=0;observed_tp=0
    for p in ps:
        pid=p['positionId']; qty=_positive(p['qty'])
        stops=[x for x in tsls if x.get('positionId')==pid and _positive(x.get('slPrice')) is not None]
        if len(stops)!=1 or _positive(stops[0].get('slQty')) is None or _positive(stops[0]['slQty']) < qty:
            failed+=1
        else:covered+=1
        observed_tp+=sum(1 for x in tsls if x.get('positionId')==pid and _positive(x.get('tpPrice')) is not None and _positive(x.get('tpQty')) is not None)
    unknown=sum(1 for x in tsls if x.get('positionId') not in ids)
    if unknown: failed+=unknown
    return {**result,'state':'EXPOSURE_WITH_INCOMPLETE_STOP_EVIDENCE' if failed else 'STOP_COVERAGE_OBSERVED_AUDIT_ONLY',
            'stop_covered_positions':covered,'coverage_gaps':failed,
            'tp_orders_observed':observed_tp,'unlinked_tpsl':unknown,
            'coverage_observed':bool(covered) and failed==0,
            'reason':'PENDING_ORDERS_DO_NOT_PROVE_TRIGGER_OR_EXECUTION'}

def audit_current(client,symbol,*,now=None):
    try:
        return guard_snapshot(client.snapshot(symbol),symbol,now=now)
    except Exception:
        return {'build':'F46','symbol':symbol,'mode':'GET_ONLY_CURRENT_EXPOSURE_GUARD',
                'state':'GET_ONLY_FAILED','complete':False,'ready_for_live':False,
                'live_enabled':False,'write_enabled':False,'orders_sent':0,
                'allow_retry':False,'real_sl_tp_confirmed':False}
