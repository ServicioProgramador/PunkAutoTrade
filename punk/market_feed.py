"""Public Bitunix Futures ticker polling. Explicit opt-in required for PAPER fills.

GET-only and fail-closed. Ticker snapshots are not execution proofs.
"""
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from punk.bitunix_readonly import ReadOnlyBitunix, BitunixContractError

SYMBOL = re.compile(r'^[A-Z0-9]{3,24}USDT$')


def observed_tickers(client, symbols):
    unique = sorted(set(symbols))
    if not unique:
        return {}
    if len(unique)>30 or any(not SYMBOL.fullmatch(s) for s in unique):
        raise BitunixContractError('INVALID_TICKER_SYMBOLS')
    data = client.get('tickers', {'symbols': ','.join(unique)})
    if not isinstance(data,list):
        raise BitunixContractError('INVALID_TICKERS_SHAPE')
    found={}
    for item in data:
        if not isinstance(item,dict):
            raise BitunixContractError('INVALID_TICKERS_SHAPE')
        symbol=item.get('symbol')
        if symbol not in unique or symbol in found:
            raise BitunixContractError('INVALID_TICKERS_SYMBOL')
        try:
            price=Decimal(str(item['lastPrice']))
        except (ValueError, KeyError, TypeError, InvalidOperation):
            raise BitunixContractError('INVALID_TICKER_PRICE') from None
        if not price.is_finite() or price<=0:
            raise BitunixContractError('INVALID_TICKER_PRICE')
        found[symbol]=str(price)
    if set(found)!=set(unique):
        raise BitunixContractError('MISSING_TICKERS')
    return found


class PaperMarketFeed:
    def __init__(self, engine, enabled=False, client=None, interval=10):
        self.engine=engine
        self.enabled=bool(enabled)
        self.client=client if client is not None else ReadOnlyBitunix()
        self.interval=max(5,int(interval))
        self.lock=threading.Lock()
        self.state={'status':'NOT_CHECKED','mode':'PAPER_OPT_IN' if enabled else 'OBSERVE_ONLY',
                    'source':'BITUNIX_FUTURES_PUBLIC_LAST', 'prices':{},'fills_are_simulated':True}

    def poll(self):
        from punk.paper_limit import pending, trigger
        rows=pending(self.engine.repo)
        symbols=sorted({r['symbol'] for r in rows if r['status']=='PENDING'} |
                       ({t['symbol'] for t in self.engine.repo.trades(limit=None)
                         if t['state']=='PROTECTED' and Decimal(t['remaining'])>0} if self.enabled else set()))
        try:
            prices=observed_tickers(self.client, symbols)
            outcomes=[]
            if self.enabled:
                # tick is a publicly observed LAST price; simulated fill, not exchange execution
                for symbol,price in prices.items():
                    outcomes.extend(trigger(self.engine,symbol,price))
                    from punk.paper_lifecycle import observe
                    outcomes.extend(observe(self.engine,symbol,price))
            result={'status':'OK','mode':'PAPER_OPT_IN' if self.enabled else 'OBSERVE_ONLY',
                    'source':'BITUNIX_FUTURES_PUBLIC_LAST','prices':prices,
                    'fills_are_simulated':True,'results':outcomes,
                    'checked_at':int(time.time())}
        except Exception as exc:
            code=exc.reason if isinstance(exc,BitunixContractError) else 'PUBLIC_FEED_UNAVAILABLE'
            result={'status':'ERROR','error_code':code,'mode':'PAPER_OPT_IN' if self.enabled else 'OBSERVE_ONLY',
                    'source':'BITUNIX_FUTURES_PUBLIC_LAST','prices':{},'fills_are_simulated':True}
        with self.lock:self.state=result
        return result

    def snapshot(self):
        with self.lock:state=dict(self.state)
        if state.get('checked_at') and time.time()-state['checked_at']>self.interval*3:
            state['status']='STALE';state['prices']={}
        return state

    def run(self):
        while True:
            self.poll()
            time.sleep(self.interval)
