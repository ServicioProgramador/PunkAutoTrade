"""Continuous PAPER position settlement (public-price simulation only).

Conservative sequencing: STOP first, otherwise ordered targets, then ROI BE.
No Bitunix authenticated writes, no LIVE code.
"""
from punk.core import D
from punk.paper_be import observe as observe_be


def favorable(side, price, trigger):
    return price >= trigger if side == 'LONG' else price <= trigger


def observe(engine, symbol, observed_price):
    price=D(observed_price)
    if not price.is_finite() or price<=0:
        raise ValueError('INVALID_OBSERVED_PRICE')
    results=[]
    with engine.lock:
        for row in engine.repo.trades(limit=None):
            if row['symbol']!=symbol or row['state']!='PROTECTED' or D(row['remaining'])<=0:
                continue
            sid=row['signal_id']
            sl=next((p for p in engine.repo.protections(sid)
                     if p['kind']=='SL' and p['confirmed']),None)
            if sl is None:
                results.append({'signal_id':sid,'status':'UNCONFIRMED_SL_REVIEW_REQUIRED'})
                continue
            # Stop always wins over take profits and BE at a given observation.
            if not favorable(row['side'],price,D(sl['price'])):
                try:
                    receipt=engine.paper_exit(sid,'SL',observed_price=price)
                    results.append({'signal_id':sid,'status':'SL_SIMULATED_'+receipt['status']})
                except (ValueError,TimeoutError):
                    results.append({'signal_id':sid,'status':'STOP_UNCERTAIN_REVIEW_REQUIRED'})
                continue
            protections=engine.repo.protections(sid)
            tp_list=sorted((p for p in protections if p['kind'].startswith('TP')),
                           key=lambda p:int(p['kind'][2:]))
            blocked=False
            for p in tp_list:
                kind=p['kind']
                if engine.repo.fill_exists(sid,kind): continue
                if not p['confirmed']:
                    results.append({'signal_id':sid,'status':'TP_UNCONFIRMED_REVIEW_REQUIRED','kind':kind})
                    blocked=True
                    break
                if not favorable(row['side'],price,D(p['price'])): break
                try:
                    if kind=='TP1' and len(tp_list)>1:
                        result=engine.partial_tp(sid,1)
                        status=result['status']
                    else:
                        result=engine.paper_exit(sid,kind)
                        status=result['status']
                    results.append({'signal_id':sid,'status':kind+'_SIMULATED_'+status})
                except (ValueError,TimeoutError):
                    results.append({'signal_id':sid,'status':'TP_UNCERTAIN_REVIEW_REQUIRED','kind':kind})
                    blocked=True
                    break
                if engine.repo.trade(sid)['state']=='CLOSED': break
            if blocked or engine.repo.trade(sid)['state']=='CLOSED': continue
        # Apply ROI BE once after handling all stop/TP transitions.
        results.extend(observe_be(engine,symbol,price))
    return results
