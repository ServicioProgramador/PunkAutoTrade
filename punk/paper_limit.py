"""PAPER-only LIMIT intent ledger. No exchange connection, no LIVE methods."""
import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta


def utc_now():
    return datetime.now(timezone.utc)


def install(repo):
    with repo.lock, repo.conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS paper_limit_intents (
            signal_id TEXT PRIMARY KEY, channel TEXT NOT NULL, message_id INTEGER NOT NULL,
            symbol TEXT NOT NULL, side TEXT NOT NULL, entry TEXT NOT NULL,
            sl TEXT NOT NULL, tps TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            triggered_price TEXT, result TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS paper_limit_reservations (signal_id TEXT PRIMARY KEY, margin TEXT NOT NULL)''')


def stage(repo, channel, message_id, signal):
    install(repo)
    key=f'{channel}:{message_id}'
    now=utc_now()
    ttl=int(repo.setting('paper_limit_ttl_seconds','7200'))
    reserved_margin=str(repo.setting('margin','5'))
    if ttl<=0 or ttl>86400:
        return {'status':'RECHAZADA','reason':'TTL LIMIT fuera de rango'}
    with repo.lock, repo.conn() as c:
        existing=c.execute('SELECT status FROM paper_limit_intents WHERE signal_id=?',(key,)).fetchone()
        if existing:
            return {'status':'LIMIT_ALREADY_TRACKED','reason':existing['status'],'signal_id':key}
        c.execute('''INSERT INTO paper_limit_intents
          (signal_id,channel,message_id,symbol,side,entry,sl,tps,status,created_at,expires_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
          (key,str(channel),int(message_id),signal.symbol,signal.side,str(signal.entry_price),
           str(signal.sl),json.dumps([str(x) for x in signal.tps]),'PENDING',
           now.isoformat(),(now+timedelta(seconds=ttl)).isoformat()))
        c.execute('INSERT INTO paper_limit_reservations VALUES(?,?)',(key,reserved_margin))
    return {'status':'LIMIT_PENDING_PAPER','signal_id':key,'reason':'Esperando precio PAPER; sin orden enviada'}


def pending(repo):
    install(repo)
    with repo.conn() as c:
        return [dict(row) for row in c.execute('SELECT * FROM paper_limit_intents ORDER BY created_at DESC LIMIT 100')]


def trigger(engine, symbol, observed_price):
    """Manual PAPER price feed. One-shot atomic claim prevents double execution.

    Existing pending intents are not activated by Telegram history or GET-only Bitunix.
    """
    repo=engine.repo
    install(repo)
    price=Decimal(str(observed_price))
    if not price.is_finite() or price<=0:
        raise ValueError('PAPER_PRICE_INVALID')
    symbol=symbol.upper()
    outcomes=[]
    with engine.lock:
        with repo.lock, repo.conn() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM paper_limit_intents WHERE symbol=? AND status=?',
                                                (symbol,'PENDING'))]
        for intent in rows:
            now=utc_now()
            expiry=datetime.fromisoformat(intent['expires_at'])
            if now>=expiry:
                with repo.lock, repo.conn() as c:
                    c.execute('UPDATE paper_limit_intents SET status=? WHERE signal_id=? AND status=?',
                              ('EXPIRED',intent['signal_id'],'PENDING'))
                outcomes.append({'signal_id':intent['signal_id'],'status':'EXPIRED'})
                continue
            entry=Decimal(intent['entry'])
            # Buy limit crosses downward, sell limit upward; no fill at any other price.
            crossed=(price<=entry if intent['side']=='LONG' else price>=entry)
            if not crossed:
                outcomes.append({'signal_id':intent['signal_id'],'status':'WAITING_PRICE'})
                continue
            # A limit order cannot be simulated as filled beyond its risk stop.
            sl=Decimal(intent['sl'])
            if (intent['side']=='LONG' and price<=sl) or (intent['side']=='SHORT' and price>=sl):
                with repo.lock, repo.conn() as c:
                    c.execute('UPDATE paper_limit_intents SET status=?, result=? WHERE signal_id=? AND status=?',
                              ('REVIEW_REQUIRED','PRICE_BEYOND_SL',intent['signal_id'],'PENDING'))
                outcomes.append({'signal_id':intent['signal_id'],'status':'REVIEW_REQUIRED','reason':'PRICE_BEYOND_SL'})
                continue
            with repo.lock, repo.conn() as c:
                claimed=c.execute('UPDATE paper_limit_intents SET status=?,triggered_price=? WHERE signal_id=? AND status=?',
                                  ('CLAIMED',str(price),intent['signal_id'],'PENDING')).rowcount
            if not claimed:
                outcomes.append({'signal_id':intent['signal_id'],'status':'ALREADY_CLAIMED'})
                continue
            # Process a new, synthetic PAPER-only revision with original signal identity.
            # No direct exchange methods beyond the in-memory PaperExchange.
            tps=json.loads(intent['tps'])
            synthetic=(f"{intent['side']} {intent['symbol']}\nEntrada: MARKET\nSL: {intent['sl']}\n"
                       + ''.join(f'TP{i}: {tp}\n' for i,tp in enumerate(tps,1)))
            old_price=engine.exchange.price
            try:
                engine.exchange.price=price
                result=engine.ingest(intent['channel'],intent['message_id'],synthetic,age_seconds=0)
            except Exception:
                # Crash/unknown: never automatically retry a claimed intent.
                result={'status':'UNKNOWN_BLOCKED','reason':'SIMULATED_EXECUTION_EXCEPTION'}
            finally:
                engine.exchange.price=old_price
            status=result['status']
            with repo.lock, repo.conn() as c:
                c.execute('UPDATE paper_limit_intents SET status=?,result=? WHERE signal_id=? AND status=?',
                          ('EXECUTED_PAPER' if status=='PROTECTED' else 'REVIEW_REQUIRED',status,
                           intent['signal_id'],'CLAIMED'))
            outcomes.append({'signal_id':intent['signal_id'],'status':status})
    return outcomes
