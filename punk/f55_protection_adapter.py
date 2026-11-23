"""F55 production-shaped Bitunix SL/TP contracts and GET readback.

Never sends network writes, consumes credentials or returns a LIVE permit.
This adapter is independent of F54 LocalProtectionPort; it prepares exact
exchange contracts for subsequent one-shot reviewed transmission.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from punk.live_write_lab import Intent, LabBlocked
from punk.bitunix_write_contract import protection_plan

PATH_PROTECTION = '/api/v1/futures/tpsl/place_order'
PATH_MODIFY = '/api/v1/futures/tpsl/modify_order'


def dec(value):
    if isinstance(value,bool): raise LabBlocked('INVALID_DECIMAL')
    try: n=Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation): raise LabBlocked('INVALID_DECIMAL') from None
    if not n.is_finite() or n<=0: raise LabBlocked('INVALID_DECIMAL')
    return n

def fmt(value): return format(dec(value),'f')

def _identity(intent, position_id):
    if type(intent) is not Intent: raise LabBlocked('INVALID_INTENT')
    intent.validate()
    if type(position_id) is not str or not position_id or len(position_id)>80 or not all(c.isalnum() or c in '_-' for c in position_id):
        raise LabBlocked('POSITION_ID_UNVERIFIED')

@dataclass(frozen=True)
class Plan:
    path: str
    payload: dict
    kind: str
    def __repr__(self): return '<F55Plan UNSENT>'

def stop_plan(intent, position_id):
    _identity(intent,position_id)
    return Plan(PATH_PROTECTION,{'symbol':intent.symbol,'positionId':position_id,
        'slPrice':fmt(intent.stop),'slQty':fmt(intent.quantity),
        'slStopType':'MARK_PRICE','slOrderType':'MARKET'},'SL')

def tp_plans(intent, position_id, *, weights, qty_step):
    _identity(intent,position_id)
    return tuple(Plan(PATH_PROTECTION,x,'TP'+str(i)) for i,x in enumerate(
        protection_plan(intent,position_id=position_id,tp_weights=weights,quantity_step=qty_step),1))

def be_plan(intent, position_id, *, stop_order_id, be_price, remaining_qty, trigger_evidence):
    _identity(intent,position_id)
    if type(stop_order_id) is not str or not stop_order_id or len(stop_order_id)>80:
        raise LabBlocked('STOP_ORDER_ID_REQUIRED')
    if trigger_evidence is not True: raise LabBlocked('TRIGGER_NOT_VERIFIED')
    entry=dec(intent.entry); be=dec(be_price); qty=dec(remaining_qty)
    if qty>dec(intent.quantity) or (intent.side=='LONG' and be<entry) or (intent.side=='SHORT' and be>entry):
        raise LabBlocked('BE_OUTSIDE_VALID_RANGE')
    # A modification cannot be considered protected until GET readback.
    return Plan(PATH_MODIFY,{'orderId':stop_order_id,'slPrice':fmt(be),
       'slQty':fmt(qty),'slStopType':'MARK_PRICE','slOrderType':'MARKET'},'BE')


def readback(snapshot, intent, position_id, *, tp_weights, qty_step, expected_sl=None):
    """Validate a single real GET-only inventory snapshot. No causal claims."""
    base={'build':'F55','mode':'GET_ONLY_PROTECTION_READBACK',
          'protected_real':False,'ready_for_live':False,'orders_sent':0,'allow_retry':False}
    try:
        _identity(intent,position_id)
        if not isinstance(snapshot,dict) or snapshot.get('status')!='GET_ONLY_VERIFIED':
            raise LabBlocked('SNAPSHOT_NOT_VERIFIED')
        if not isinstance(snapshot.get('checked_at'),int): raise LabBlocked('SNAPSHOT_TIME_MISSING')
        import time
        age=time.time()-snapshot['checked_at']
        if age<0 or age>30: raise LabBlocked('STALE_SNAPSHOT')
        ps=snapshot.get('positions'); orders=snapshot.get('pending_tpsl')
        if not isinstance(ps,list) or not isinstance(orders,list): raise LabBlocked('INCOMPLETE_SNAPSHOT')
        positions=[p for p in ps if isinstance(p,dict) and str(p.get('positionId'))==position_id]
        if len(positions)!=1 or positions[0].get('symbol')!=intent.symbol or positions[0].get('side')!=intent.side:
            raise LabBlocked('POSITION_IDENTITY_MISMATCH')
        live_qty=dec(positions[0].get('qty'))
        if live_qty>dec(intent.quantity): raise LabBlocked('EXPOSURE_GREATER_THAN_INTENT')
        relevant=[o for o in orders if isinstance(o,dict) and str(o.get('positionId'))==position_id]
        if any(o.get('symbol')!=intent.symbol or not str(o.get('id') or '') for o in relevant):
            raise LabBlocked('INVALID_PROTECTION_IDENTITY')
        if len({str(o['id']) for o in relevant})!=len(relevant): raise LabBlocked('DUPLICATE_PROTECTION_ID')
        stops=[o for o in relevant if o.get('slPrice') not in (None,'')]
        if len(stops)!=1: raise LabBlocked('STOP_MISSING_OR_AMBIGUOUS')
        sl=stops[0]; wanted=dec(expected_sl if expected_sl is not None else intent.stop)
        if dec(sl['slPrice'])!=wanted or dec(sl.get('slQty'))<live_qty:
            raise LabBlocked('STOP_PRICE_OR_COVERAGE_MISMATCH')
        if sl.get('slStopType')!='MARK_PRICE' or sl.get('slOrderType')!='MARKET':
            raise LabBlocked('STOP_TRIGGER_MISMATCH')
        # Full TP equality only before any partial closures. Following fills,
        # require separate verified execution ledger before interpreting remaining targets.
        if live_qty != dec(intent.quantity):
            raise LabBlocked('PARTIAL_FILL_REQUIRES_EXECUTION_LEDGER')
        expected=tp_plans(intent,position_id,weights=tp_weights,qty_step=qty_step)
        tps=[o for o in relevant if o.get('tpPrice') not in (None,'')]
        if len(tps)!=len(expected): raise LabBlocked('TP_COUNT_MISMATCH')
        observed=sorted((fmt(o['tpPrice']),fmt(o['tpQty'])) for o in tps
                        if o.get('tpStopType')=='MARK_PRICE' and o.get('tpOrderType')=='MARKET')
        target=sorted((p.payload['tpPrice'],p.payload['tpQty']) for p in expected)
        if observed!=target: raise LabBlocked('TP_COVERAGE_MISMATCH')
        if any(o.get('slPrice') not in (None,'') and o.get('tpPrice') not in (None,'') for o in relevant):
            raise LabBlocked('COMBINED_PROTECTION_AMBIGUOUS')
        if len(relevant)!=1+len(tps): raise LabBlocked('EXTRA_PROTECTION_RECORD')
        return {**base,'state':'SL_AND_TPS_GET_MATCH_AUDIT_ONLY',
                'stop_order_id':str(sl['id']),'tp_orders_found':len(tps),
                'readback_consistent':True}
    except (LabBlocked,ValueError,TypeError) as err:
        return {**base,'state':str(err),'readback_consistent':False}


def send_live(*args,**kwargs):
    raise LabBlocked('F55_NO_LIVE_SEND_AUTHORITY')


def lab_report():
    import time
    intent=Intent('f55:rune:qa','RUNEUSDT','SHORT','10','0.4785','0.487456',
       ('0.470246','0.469008','0.467584','0.465947','0.464063'))
    stop=stop_plan(intent,'P-55');tps=tp_plans(intent,'P-55',weights=('0.2',)*5,qty_step='0.01')
    records=[dict(p.payload,id='lab-'+str(i)) for i,p in enumerate((stop,)+tps)]
    snap={'status':'GET_ONLY_VERIFIED','checked_at':int(time.time()),
          'positions':[{'positionId':'P-55','symbol':'RUNEUSDT','side':'SHORT','qty':'10'}],
          'pending_tpsl':records}
    r=readback(snap,intent,'P-55',tp_weights=('0.2',)*5,qty_step='0.01')
    return {'build':'F55','state':r['state'],'readback_consistent':r['readback_consistent'],
         'plans_prepared':6,'be_contract_path':be_plan(intent,'P-55',stop_order_id='lab-0',
           be_price='0.477',remaining_qty='8',trigger_evidence=True).path,
         'orders_sent':0,'ready_for_live':False,'live_enabled':False}
