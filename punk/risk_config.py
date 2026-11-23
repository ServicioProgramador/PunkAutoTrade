"""Safe settings and bounded exchange metadata for PAPER; no trade endpoints."""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import time
from threading import RLock

class ConfigurationError(ValueError): pass

SETTINGS = {'margin':('decimal','0.01','100000'), 'leverage':('int',3,200),
            'max_positions':('int',1,100),
            'max_exposure_usdt':('decimal','1','10000000'),
            'max_loss_usdt':('decimal','0.01','100000'), 'balance_basis':('enum',('PAPER','BITUNIX_GET_ONLY'),None)}

def validated_settings(values):
    if not isinstance(values,dict) or set(values)!=set(SETTINGS):
        raise ConfigurationError('INVALID_SETTINGS_FIELDS')
    result={}
    for key,(kind,low,high) in SETTINGS.items():
        v=values[key]
        if kind=='enum':
            if type(v) is not str or v not in low: raise ConfigurationError('INVALID_'+key.upper())
        elif kind=='int':
            if type(v) not in (str,int) or not str(v).isascii() or not str(v).isdecimal():
                raise ConfigurationError('INVALID_'+key.upper())
            v=int(v)
            if not low<=v<=high: raise ConfigurationError('INVALID_'+key.upper())
        else:
            try: v=Decimal(str(v))
            except (ValueError,InvalidOperation): raise ConfigurationError('INVALID_'+key.upper()) from None
            if not v.is_finite() or not Decimal(low)<=v<=Decimal(high) or v.as_tuple().exponent < -2:
                raise ConfigurationError('INVALID_'+key.upper())
            v=str(v)
        result[key]=str(v)
    return result

def current(repo):
    return {'margin':repo.setting('margin','5'), 'leverage':repo.setting('leverage','20'),
            'max_positions':repo.setting('max_positions','3'),
            'max_exposure_usdt':repo.setting('max_exposure_usdt','300'),
            'max_loss_usdt':repo.setting('max_loss_usdt','5'),
            'balance_basis':repo.setting('balance_basis','PAPER')}

def update(repo, settings):
    config=validated_settings(settings)
    # single SQLite transaction; existing entries never rewritten
    with repo.lock,repo.conn() as c:
        for k,v in config.items():
            c.execute('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,v))
    return config

def effective_leverage(configured, contract):
    requested=int(configured)
    if requested<3 or requested>200: raise ConfigurationError('INVALID_LEVERAGE')
    if not isinstance(contract,dict): raise ConfigurationError('CONTRACT_UNVERIFIED')
    try:
        minimum=int(contract['min_leverage']); maximum=int(contract['max_leverage'])
    except (KeyError,TypeError,ValueError,OverflowError):
        raise ConfigurationError('CONTRACT_LEVERAGE_UNVERIFIED') from None
    if minimum<1 or maximum<minimum or maximum>200: raise ConfigurationError('CONTRACT_LEVERAGE_UNVERIFIED')
    actual=min(requested,maximum,int(contract.get('tier_max_leverage',200)))
    if actual<max(minimum,3): raise ConfigurationError('CONTRACT_LEVERAGE_UNAVAILABLE')
    return actual

class PublicContractLimits:
    """Read-only Bitunix trading_pairs; deny PAPER entries if unavailable."""
    def __init__(self,client,ttl=120,margin_source=None,leverage_source=None):
        self.client=client; self.ttl=ttl; self.lock=RLock(); self.cache={}
        self.margin_source=margin_source or (lambda:Decimal('5'))
        self.leverage_source=leverage_source or (lambda:20)
    def __call__(self,symbol):
        budget_key=(symbol,str(self.margin_source()),str(self.leverage_source()))
        with self.lock:
            cached=self.cache.get(budget_key)
            if cached and time.monotonic()-cached[0]<self.ttl: return dict(cached[1])
        data=self.client.public_contract(symbol)
        if data.get('symbol')!=symbol or data.get('symbolStatus')!='OPEN' or data.get('isApiSupported') is not True:
            raise ConfigurationError('CONTRACT_NOT_OPEN')
        try:
            minimum=Decimal(str(data['minTradeVolume']))
            precision=int(data['basePrecision'])
            minlev=int(data['minLeverage']); maxlev=int(data['maxLeverage'])
            if not 0<=precision<=12 or not minimum.is_finite() or minimum<=0: raise ValueError()
        except (ValueError,KeyError,TypeError,InvalidOperation):
            raise ConfigurationError('CONTRACT_METADATA_INVALID') from None
        result={'step':Decimal(1).scaleb(-precision),'min_qty':minimum,
                'min_leverage':minlev,'max_leverage':maxlev}
        try:
            tiers=self.client.get('position_tiers',{'symbol':symbol})
            margin=Decimal(str(self.margin_source()))
            requested=int(self.leverage_source())
            notional=margin*min(requested,maxlev)
            if not isinstance(tiers,list) or not tiers: raise ValueError()
            eligible=[]
            for item in tiers:
                if not isinstance(item,dict) or item.get('symbol')!=symbol: raise ValueError()
                start=Decimal(str(item['startValue']));end=Decimal(str(item['endValue']))
                cap=int(item['leverage'])
                if not (start.is_finite() and end.is_finite() and 0<=start<end and 1<=cap<=200): raise ValueError()
                if start<=notional<end: eligible.append(cap)
            if len(eligible)!=1: raise ValueError()
            result['tier_max_leverage']=eligible[0]
        except (ValueError,KeyError,TypeError,InvalidOperation):
            raise ConfigurationError('POSITION_TIER_UNVERIFIED') from None
        effective_leverage(3,result)
        with self.lock: self.cache[budget_key]=(time.monotonic(),result)
        return dict(result)

def available_budget(engine):
    balance=engine.exchange.available
    mode=engine.repo.setting('balance_basis','PAPER')
    if mode=='PAPER':
        # Active PAPER trades already debit exchange.available. Pending LIMIT
        # intentions need separate, persistent reservations.
        from punk.paper_limit import install
        install(engine.repo)
        with engine.repo.conn() as c:
            rows=c.execute('''SELECT i.signal_id,r.margin FROM paper_limit_intents i
                LEFT JOIN paper_limit_reservations r ON r.signal_id=i.signal_id
                WHERE i.status IN ('PENDING','CLAIMED')''').fetchall()
        reserved=Decimal(0)
        for row in rows:
            if engine.repo.trade(row['signal_id']) is not None:
                continue
            if row['margin'] is None:
                raise ConfigurationError('PENDING_MARGIN_UNVERIFIED')
            reserved+=Decimal(row['margin'])
        return max(Decimal(0),balance-reserved)
    if mode!='BITUNIX_GET_ONLY': raise ConfigurationError('BALANCE_MODE_INVALID')
    monitor=getattr(engine,'account_monitor',None)
    if monitor is None: raise ConfigurationError('BITUNIX_BALANCE_NOT_VERIFIED')
    state=monitor.snapshot()
    if state.get('status')!='GET_ONLY_VERIFIED': raise ConfigurationError('BITUNIX_BALANCE_NOT_VERIFIED')
    try: real=Decimal(str(state['account']['available']))
    except (TypeError,KeyError,ValueError,InvalidOperation): raise ConfigurationError('BITUNIX_BALANCE_NOT_VERIFIED') from None
    if not real.is_finite() or real<0: raise ConfigurationError('BITUNIX_BALANCE_NOT_VERIFIED')
    # Real available is not debited by PAPER. Deduct only local reservations
    # for active simulated positions and pending entry margin commitments.
    reserved=sum((Decimal(r['margin']) for t in engine.repo.trades(limit=None)
             if t['state'] not in ('CLOSED','REJECTED')
             for r in [engine.repo.reservation(t['signal_id'])] if r),Decimal(0))
    from punk.paper_limit import pending
    pend=[r for r in pending(engine.repo) if r['status'] in ('PENDING','CLAIMED')]
    # Per-intent receipts survive config changes and restarts.
    from punk.paper_limit import install
    install(engine.repo)
    with engine.repo.conn() as c:
        receipts={r['signal_id']:r['margin'] for r in c.execute('SELECT signal_id,margin FROM paper_limit_reservations')}
    for intent in pend:
        if engine.repo.trade(intent['signal_id']) is not None: continue
        v=receipts.get(intent['signal_id'])
        if v is None: raise ConfigurationError('PENDING_MARGIN_UNVERIFIED')
        reserved+=Decimal(v)
    return max(Decimal(0), min(balance,real-reserved))

def capacity(engine):
    setting=current(engine.repo)
    margin=Decimal(str(setting['margin']))
    try:
        available=available_budget(engine)
        slots=int(setting['max_positions'])-sum(t['state'] not in ('CLOSED','REJECTED') for t in engine.repo.trades(limit=None))
        from punk.paper_limit import pending
        slots-=sum(r['status'] in ('PENDING','CLAIMED') and engine.repo.trade(r['signal_id']) is None
                   for r in pending(engine.repo))
        return {'status':'OK','mode':setting['balance_basis'], 'available_usdt':str(max(available,Decimal(0))),
                'possible_new_trades':max(0,min(int((available/margin).to_integral_value(rounding=ROUND_FLOOR)),slots))}
    except (ConfigurationError,ValueError,InvalidOperation) as exc:
        return {'status':'UNAVAILABLE','reason':str(exc),'mode':setting['balance_basis'],'possible_new_trades':0}
