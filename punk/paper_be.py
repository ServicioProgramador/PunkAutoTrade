"""PAPER-only price observation for TP1 and break-even.

A public last price is not a trade confirmation. LIVE functions are not imported.
"""
from decimal import Decimal, InvalidOperation
from punk.core import D


def _favorable(side, price, target):
    return price >= target if side == 'LONG' else price <= target


def _fee_inclusive_be(entry, side, fee):
    return entry * (1 + 2 * fee if side == 'LONG' else 1 - 2 * fee)


def observe(engine, symbol, observed_price):
    """Return simulated receipts. Only new TP1 fills/SL modifications mutate PAPER.

    Logic runs under engine lock so it cannot race its own PAPER executor.
    """
    price = D(observed_price)
    if not price.is_finite() or price <= 0:
        raise ValueError('INVALID_OBSERVED_PRICE')
    result=[]
    with engine.lock:
        for t in engine.repo.trades():
            if t['symbol'] != symbol or t['state'] != 'PROTECTED' or D(t['remaining']) <= 0:
                continue
            signal_id=t['signal_id']
            entry=D(t['entry']); stop=D(t['sl'])
            side=t['side']
            protections=engine.repo.protections(signal_id)
            sl=next((p for p in protections if p['kind']=='SL' and p['confirmed']), None)
            if sl is None:
                result.append({'signal_id':signal_id,'status':'BE_BLOCKED_UNCONFIRMED_SL'})
                continue
            if not _favorable(side, price, stop) and t['be_state']!='CONFIRMED':
                result.append({'signal_id':signal_id,'status':'STOP_CROSSED_REVIEW_REQUIRED'})
                continue
            tp1=next((p for p in protections if p['kind']=='TP1' and p['confirmed']),None)
            if not tp1:
                result.append({'signal_id':signal_id,'status':'BE_BLOCKED_UNCONFIRMED_TP1'})
                continue
            if not engine.repo.fill_exists(signal_id,'TP1') and _favorable(side,price,D(tp1['price'])):
                try:
                    filled=engine.partial_tp(signal_id,1)
                    result.append({'signal_id':signal_id,'status':'TP1_SIMULATED_'+filled['status']})
                except (ValueError, TimeoutError) as exc:
                    result.append({'signal_id':signal_id,'status':'TP1_PROTECTION_UNCERTAIN'})
                continue
            if t['be_state']=='CONFIRMED':
                continue
            reservation=engine.repo.reservation(signal_id)
            leverage=int(reservation['leverage']) if reservation else int(engine.repo.setting('leverage','20'))
            if leverage<1 or leverage>125:
                result.append({'signal_id':signal_id,'status':'INVALID_LEVERAGE'})
                continue
            # ROI is unrealized leveraged return on notional/margin, before fees.
            raw=(price-entry)/entry * (1 if side=='LONG' else -1)
            roi=raw*leverage*100
            if roi < D(engine.repo.setting('paper_be_roi_pct','1')):
                continue
            fee=D(engine.repo.setting('fee_rate','0.0006'))
            if fee<0 or fee>=D('0.1'):
                result.append({'signal_id':signal_id,'status':'INVALID_FEE'})
                continue
            target=_fee_inclusive_be(entry,side,fee)
            # A stop must stay on the valid side of the observed price.
            if not _favorable(side,price,target) or price==target:
                result.append({'signal_id':signal_id,'status':'ROI_REACHED_BE_NOT_YET_FEASIBLE','roi_pct':str(roi)})
                continue
            # Never loosen a protective stop.
            if (side=='LONG' and target<=stop) or (side=='SHORT' and target>=stop):
                result.append({'signal_id':signal_id,'status':'BE_BLOCKED_NON_IMPROVING_SL'})
                continue
            try:
                engine.exchange.place_protection(t['client_id'],'SL',target,D(t['remaining']))
                if not engine.exchange.confirm_protection(t['client_id'],'SL',target,D(t['remaining'])):
                    raise ValueError('BE_NOT_CONFIRMED')
                engine.repo.confirm_paper_be(signal_id,target,D(t['remaining']))
                result.append({'signal_id':signal_id,'status':'BE_CONFIRMED_PAPER_ROI','roi_pct':str(roi),'be':str(target)})
            except (ValueError,TimeoutError,InvalidOperation):
                result.append({'signal_id':signal_id,'status':'BE_UNCERTAIN_REVIEW_REQUIRED'})
    return result
