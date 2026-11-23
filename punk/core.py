import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path


def D(value):
    return Decimal(str(value))


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    entry_type: str
    entry_price: Decimal | None
    sl: Decimal
    tps: tuple[Decimal, ...]
    leverage: int | None = None   # F58: "Apalancamiento: x20" from the signal, if present


def parse(text):
    """Parse conventional and actual Punkchainer VIP signal formats.

    This function only classifies. Numeric entries are LIMIT intents, never
    silently converted to MARKET orders.
    """
    raw = text.upper().replace(',', '.')
    # Punk VIP: #RUNE/USDT - Short / #RUNEUSDT LONG / BTCUSDT SHORT.
    # Avoid accidentally treating prose as a signal by anchoring the heading.
    vip = re.search(r'(?m)^\s*#?\s*([A-Z0-9]{2,20})\s*(?:/\s*(USDT))?\s*[-–—:]?\s*(LONG|SHORT)\b', raw)
    conventional = re.search(r'(?m)^\s*(LONG|SHORT)\s+\$?([A-Z0-9]{2,20})(?:/\s*(USDT))?\b', raw)
    # F56: "🟢 NUEVA SEÑAL LONG — $AR/USDT" (heading may start with an emoji).
    headline = re.search(r'(?m)^[^\w\n]*NUEVA\s+SE[ÑN]AL\s+(LONG|SHORT)\s*[-–—:]?\s*[$#]?([A-Z0-9]{2,20})\s*/\s*(USDT)\b', raw)
    if vip:
        asset, quote, side = vip.groups()
    elif conventional:
        side, asset, quote = conventional.groups()
    elif headline:
        side, asset, quote = headline.groups()
    else:
        return None, 'INFORMATIVA', 'Sin direccion/simbolo'
    symbol = asset if asset.endswith('USDT') else (asset + 'USDT' if quote else asset)
    # Source always has to identify the USDT contract explicitly. Do not
    # convert a bare ticker into a Bitunix symbol by guessing its quote.
    if not re.fullmatch(r'[A-Z0-9]{2,20}USDT', symbol):
        return None, 'INCOMPLETA', 'Falta par USDT'
    entry = re.search(r'(?m)^\s*(?:ENTRADA|ENTRY|ENTER)\s*[:=\-]?\s*(MARKET|MERCADO|\d+(?:\.\d+)?)\b', raw)
    # F56: emoji-prefixed lines ("💰 Entrada: CMP hasta 2.206", "🛑 SL: 2.119").
    # "CMP hasta X" = market price up to X: a marketable LIMIT at X, never an
    # unbounded MARKET order.
    entry = entry or re.search(r'(?m)^[^\w\n]*(?:ENTRADA|ENTRY)\s*[:=\-]?\s*(?:CMP|MERCADO|MARKET)\s+(?:HASTA|UP\s*TO)\s+(\d+(?:\.\d+)?)\b', raw)
    entry = entry or re.search(r'(?m)^[^\w\n]*(?:ENTRADA|ENTRY)\s*[:=\-]?\s*(\d+(?:\.\d+)?)\b', raw)
    sl = re.search(r'(?m)^\s*(?:SL|STOP\s*LOSS)\s*[:=\-]?\s*(\d+(?:\.\d+)?)\b', raw)
    sl = sl or re.search(r'(?m)^[^\w\n]*(?:SL|STOP\s*LOSS)\s*[:=\-]?\s*(\d+(?:\.\d+)?)\b', raw)
    tps = re.findall(r'(?m)^\s*(?:🎯\s*)?(?:TP|OBJETIVO|TARGET)\s*([1-9]\d*)\s*[:=\-]?\s*(\d+(?:\.\d+)?)\b', raw)
    if not tps:
        single = re.findall(r'(?m)^[^\w\n]*(?:TP|TAKE\s*PROFIT|OBJETIVO)\s*[:=\-]\s*(\d+(?:\.\d+)?)\b', raw)
        if len(single) == 1:
            tps = [('1', single[0])]
    if not entry or not sl or not tps:
        return None, 'INCOMPLETA', 'Falta entrada, SL o TP'
    if len(tps) > 5 or len(set(n for n, _ in tps)) != len(tps):
        return None, 'RECHAZADA', 'TP duplicados o mas de cinco objetivos'
    if [int(n) for n, _ in tps] != list(range(1, len(tps) + 1)):
        return None, 'RECHAZADA', 'TP no correlativos'
    try:
        market = entry.group(1) in ('MARKET', 'MERCADO')
        e = None if market else D(entry.group(1))
        stop = D(sl.group(1))
        targets = tuple(D(price) for _, price in tps)
    except InvalidOperation:
        return None, 'RECHAZADA', 'Precio invalido'
    if stop <= 0 or any(x <= 0 for x in targets) or (e is not None and e <= 0):
        return None, 'RECHAZADA', 'Precios no positivos'
    if len(set(targets)) != len(targets):
        return None, 'RECHAZADA', 'TP repetidos'
    if side == 'LONG' and list(targets) != sorted(targets):
        return None, 'RECHAZADA', 'TP LONG desordenados'
    if side == 'SHORT' and list(targets) != sorted(targets, reverse=True):
        return None, 'RECHAZADA', 'TP SHORT desordenados'
    if e is not None:
        if side == 'LONG' and not (stop < e < min(targets)):
            return None, 'RECHAZADA', 'Niveles incompatibles LONG'
        if side == 'SHORT' and not (stop > e > max(targets)):
            return None, 'RECHAZADA', 'Niveles incompatibles SHORT'
    lev = re.search(r'(?m)^[^\w\n]*(?:APALANCAMIENTO|LEVERAGE|LEV)\s*[:=\-]?\s*X?\s*(\d{1,3})\s*X?\b', raw)
    leverage = int(lev.group(1)) if lev and 1 <= int(lev.group(1)) <= 200 else None
    return Signal(symbol, side, 'MARKET' if market else 'LIMIT', e, stop, targets, leverage), 'VALIDA', ''


class Repository:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.conn() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                  channel TEXT NOT NULL, message_id INTEGER NOT NULL, revision TEXT NOT NULL,
                  created_at TEXT NOT NULL, classification TEXT NOT NULL, reason TEXT NOT NULL,
                  PRIMARY KEY(channel,message_id,revision));
                CREATE TABLE IF NOT EXISTS trades (
                  signal_id TEXT PRIMARY KEY, channel TEXT NOT NULL, message_id INTEGER NOT NULL,
                  symbol TEXT NOT NULL, side TEXT NOT NULL, entry TEXT NOT NULL, sl TEXT NOT NULL,
                  tps TEXT NOT NULL, qty TEXT NOT NULL, remaining TEXT NOT NULL,
                  client_id TEXT NOT NULL UNIQUE, order_id TEXT NOT NULL,
                  state TEXT NOT NULL, be_state TEXT NOT NULL, realized TEXT NOT NULL DEFAULT '0',
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS protections (
                  signal_id TEXT NOT NULL, kind TEXT NOT NULL, price TEXT NOT NULL,
                  qty TEXT NOT NULL, confirmed INTEGER NOT NULL,
                  PRIMARY KEY(signal_id,kind));
                CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_reservations (
                    signal_id TEXT PRIMARY KEY, margin TEXT NOT NULL,
                    leverage INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_fills (
                    signal_id TEXT NOT NULL, tp_kind TEXT NOT NULL,
                    filled_qty TEXT NOT NULL, fill_price TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(signal_id,tp_kind));
            ''')

    def conn(self):
        return Connection(self.path)

    def setting(self, key, default):
        with self.conn() as c:
            row = c.execute('SELECT v FROM settings WHERE k=?', (key,)).fetchone()
        return row['v'] if row else default

    def set_setting(self, key, value):
        with self.lock, self.conn() as c:
            c.execute('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (key, str(value)))

    def register(self, channel, message_id, text, classification, reason):
        revision = hashlib.sha256(text.encode()).hexdigest()
        with self.lock, self.conn() as c:
            cur = c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)',
                            (str(channel), int(message_id), revision, now(), classification, reason))
            return cur.rowcount == 1

    def trade(self, signal_id):
        with self.conn() as c:
            row = c.execute('SELECT * FROM trades WHERE signal_id=?', (signal_id,)).fetchone()
        return dict(row) if row else None

    def insert_trade(self, t, protections):
        with self.lock, self.conn() as c:
            c.execute('''INSERT INTO trades(signal_id,channel,message_id,symbol,side,entry,sl,tps,qty,remaining,client_id,order_id,state,be_state,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (t['signal_id'],t['channel'],t['message_id'],t['symbol'],t['side'],t['entry'],t['sl'],
                       json.dumps(t['tps']),t['qty'],t['qty'],t['client_id'],t['order_id'],t['state'],'PENDING',now()))
            c.executemany('INSERT INTO protections VALUES(?,?,?,?,?)', [(t['signal_id'], *p) for p in protections])
            if t['order_id'] != 'UNKNOWN' and 'margin' in t:
                c.execute('INSERT INTO paper_reservations VALUES(?,?,?)',
                          (t['signal_id'], str(t['margin']), int(t['leverage'])))

    def update_trade(self, signal_id, **fields):
        allowed={'remaining','state','be_state','realized','sl'}
        if not fields or set(fields)-allowed: raise ValueError('Campos invalidos')
        with self.lock, self.conn() as c:
            c.execute('UPDATE trades SET '+','.join(k+'=?' for k in fields)+' WHERE signal_id=?',tuple(map(str,fields.values()))+(signal_id,))

    def update_protection(self, signal_id, kind, price=None, confirmed=None):
        with self.lock, self.conn() as c:
            if price is not None: c.execute('UPDATE protections SET price=? WHERE signal_id=? AND kind=?',(str(price),signal_id,kind))
            if confirmed is not None: c.execute('UPDATE protections SET confirmed=? WHERE signal_id=? AND kind=?',(int(confirmed),signal_id,kind))

    def protections(self, signal_id):
        with self.conn() as c:
            return [dict(x) for x in c.execute('SELECT * FROM protections WHERE signal_id=? ORDER BY kind',(signal_id,))]

    def reservation(self, signal_id):
        with self.conn() as c:
            row=c.execute('SELECT margin,leverage FROM paper_reservations WHERE signal_id=?',
                          (signal_id,)).fetchone()
        return dict(row) if row else None

    def fill_exists(self, signal_id, tp_kind):
        with self.conn() as c:
            return c.execute('SELECT 1 FROM paper_fills WHERE signal_id=? AND tp_kind=?',
                             (signal_id,tp_kind)).fetchone() is not None

    def apply_paper_fill(self, signal_id, tp_kind, qty, price, remaining, realized,
                         be_price, be_state):
        # All PAPER fill/accounting updates commit or roll back together.
        with self.lock, self.conn() as c:
            changed = c.execute('INSERT OR IGNORE INTO paper_fills VALUES(?,?,?,?,?)',
                                (signal_id,tp_kind,str(qty),str(price),now())).rowcount
            if not changed:
                return False
            c.execute('UPDATE trades SET remaining=?, realized=?, sl=?, be_state=? WHERE signal_id=?',
                      (str(remaining),str(realized),str(be_price),be_state,signal_id))
            c.execute('UPDATE protections SET price=?,qty=?,confirmed=1 WHERE signal_id=? AND kind=?',
                      (str(be_price),str(remaining),signal_id,'SL'))
            return True

    def apply_paper_exit(self, signal_id, kind, qty, fill_price, remaining, realized, closed):
        """Durable one-shot PAPER exit. No external order or account write."""
        with self.lock, self.conn() as c:
            t=c.execute('SELECT state,remaining FROM trades WHERE signal_id=?',
                        (signal_id,)).fetchone()
            if not t or t['state']!='PROTECTED':
                return False
            if c.execute('SELECT 1 FROM paper_fills WHERE signal_id=? AND tp_kind=?',
                         (signal_id,kind)).fetchone():
                return False
            if D(t['remaining']) < D(qty) or D(qty)<=0 or D(remaining)<0:
                raise ValueError('INVALID_EXIT_QUANTITY')
            c.execute('INSERT INTO paper_fills VALUES(?,?,?,?,?)',
                      (signal_id,kind,str(qty),str(fill_price),now()))
            c.execute('UPDATE trades SET remaining=?, realized=?, state=? WHERE signal_id=?',
                      (str(remaining),str(realized),'CLOSED' if closed else 'PROTECTED',signal_id))
            if closed:
                c.execute('UPDATE protections SET confirmed=0,qty=? WHERE signal_id=?',
                          ('0',signal_id))
            else:
                c.execute("UPDATE protections SET qty=? WHERE signal_id=? AND kind='SL'",
                          (str(remaining),signal_id))
            return True

    def confirm_paper_be(self, signal_id, price, remaining):
        # Atomic persistence; do not invent a TP fill for the ROI trigger.
        with self.lock, self.conn() as c:
            changed=c.execute("UPDATE trades SET sl=?, be_state='CONFIRMED' WHERE signal_id=? AND state='PROTECTED' AND be_state!='CONFIRMED'", (str(price),signal_id)).rowcount
            if changed != 1: raise ValueError('BE_NOT_ELIGIBLE')
            p=c.execute("UPDATE protections SET price=?,qty=?,confirmed=1 WHERE signal_id=? AND kind='SL' AND confirmed=1", (str(price),str(remaining),signal_id)).rowcount
            if p != 1: raise ValueError('SL_NOT_CONFIRMED')
            return True

    def trades(self, limit=100):
        with self.conn() as c:
            sql='SELECT * FROM trades ORDER BY created_at DESC'
            if limit is None:
                return [dict(x) for x in c.execute(sql)]
            if type(limit) is not int or not 1 <= limit <= 1000: raise ValueError('INVALID_LIMIT')
            return [dict(x) for x in c.execute(sql+' LIMIT ?',(limit,))]

    def events(self):
        with self.conn() as c:
            return [dict(x) for x in c.execute('SELECT * FROM events ORDER BY created_at DESC LIMIT 100')]


class Connection:
    def __init__(self,path): self.path=path
    def __enter__(self):
        self.c=sqlite3.connect(self.path,timeout=15,isolation_level=None)
        self.c.row_factory=sqlite3.Row
        self.c.execute('PRAGMA busy_timeout=15000')
        self.c.execute('BEGIN IMMEDIATE')
        return self.c
    def __exit__(self,exc_type,exc,tb):
        try:
            if exc_type: self.c.rollback()
            else: self.c.commit()
        finally: self.c.close()


class PaperExchange:
    """Deterministic simulated adapter, never connects to Bitunix."""
    def __init__(self, balance='1000', price='66000'):
        self.available=D(balance)
        self.price=D(price)
        self.orders={}
        self.protections={}
        self.fail_at=None

    def market_price(self, symbol): return self.price
    def contract(self, symbol):
        if not re.fullmatch(r'[A-Z0-9]{3,20}USDT',symbol): raise ValueError('Contrato no admitido por simulador')
        return {'step':D('0.00001'),'min_qty':D('0.00001'),'min_leverage':1,'max_leverage':125}
    def open(self, client_id, symbol, side, qty):
        if self.fail_at=='open': raise TimeoutError('Resultado desconocido')
        if client_id in self.orders:return self.orders[client_id]
        order={'order_id':'P-'+client_id[:20], 'price':str(self.price),'qty':str(qty)}
        self.orders[client_id]=order
        return order
    def place_protection(self, client_id, kind, price, qty):
        if self.fail_at==kind: raise TimeoutError('Proteccion incierta')
        key=(client_id,kind)
        self.protections[key]={'price':str(price),'qty':str(qty)}
        return self.protections[key]
    def confirm_protection(self, client_id, kind, price, qty):
        p=self.protections.get((client_id,kind))
        return bool(p and p['price']==str(price) and p['qty']==str(qty))


class Engine:
    def __init__(self, repo, exchange=None):
        self.repo=repo
        self.exchange=exchange or PaperExchange()
        self.lock=threading.RLock()
        self.account_monitor=None
        self.contract_limits=None
        self.recover_paper()

    def recover_paper(self):
        # PAPER ONLY. Rebuild deterministic simulator state from persisted receipts.
        # Do not turn UNCERTAIN or unconfirmed protections into success.
        with self.lock:
            trades = self.repo.trades(limit=None)
            initial = D(self.repo.setting('paper_initial_balance','1000'))
            used = D(0)
            legacy = 0
            settled = sum((D(t['realized']) for t in trades if t['state']=='CLOSED'), D(0))
            for t in trades:
                if t['order_id'] != 'UNKNOWN' and t['state'] not in ('CLOSED','REJECTED'):
                    reservation=self.repo.reservation(t['signal_id'])
                    if reservation:
                        used += D(reservation['margin'])
                    else:
                        # F1/F2 lacked per-trade margin receipts; historical balance
                        # is an estimate and must be labelled as such.
                        lev=int(self.repo.setting('leverage','20'))
                        used += D(t['qty'])*D(t['entry'])/D(lev)
                        legacy += 1
                    self.exchange.orders[t['client_id']] = {
                        'order_id':t['order_id'],'price':t['entry'],'qty':t['qty']}
                    for p in self.repo.protections(t['signal_id']):
                        if p['confirmed']:
                            self.exchange.protections[(t['client_id'],p['kind'])] = {
                                'price':p['price'],'qty':p['qty']}
            self.exchange.available = max(D(0), initial + settled-used)
            self.recovery_status=('PAPER_LEGACY_BALANCE_ESTIMATE' if legacy
                                  else 'PAPER_RESTORED_FROM_SQLITE')


    def ingest(self, channel, message_id, text, age_seconds=0, edited=False):
        with self.lock:
            channel=str(channel)
            allowed={s.strip() for s in self.repo.setting('channels','').split(',') if s.strip()}
            if channel not in allowed: return {'status':'RECHAZADA','reason':'Canal no autorizado'}
            sig,status,reason=parse(text)
            if age_seconds>int(self.repo.setting('max_age_seconds','120')):
                status,reason='EXPIRADA','Mensaje fuera de ventana temporal'
            inserted=self.repo.register(channel,message_id,text,status,reason)
            if not inserted:return {'status':'DUPLICADA','reason':'Revision ya procesada'}
            key=f'{channel}:{message_id}'
            existing=self.repo.trade(key)
            if existing:
                # Edited messages cannot silently modify existing open positions.
                return {'status':'REQUIERE_REVISION','reason':'Mensaje existente editado; protecciones sin cambios'}
            if status!='VALIDA':return {'status':status,'reason':reason}
            if sig.entry_type!='MARKET':
                if self.repo.setting('auto_paper','ON')!='ON':
                    return {'status':'PAUSADA','reason':'AutoTrade PAPER OFF'}
                from punk.paper_limit import stage
                # Admission is checked for both PAPER and BITUNIX_GET_ONLY.
                # The engine lock serializes concurrent Telegram messages.
                from punk.risk_config import capacity
                quota=capacity(self)
                if quota['possible_new_trades']<1:
                    return {'status':'RECHAZADA','reason':'CAPITAL_OR_SLOTS_UNAVAILABLE',
                            'capacity_reason':quota.get('reason','')}
                return stage(self.repo,channel,message_id,sig)
            if self.repo.setting('auto_paper','ON')!='ON':return {'status':'PAUSADA','reason':'AutoTrade PAPER OFF'}
            try:
                price=self.exchange.market_price(sig.symbol)
                spec=(self.contract_limits(sig.symbol) if self.contract_limits else self.exchange.contract(sig.symbol))
                if price<=0 or sig.sl<=0 or any(x<=0 for x in sig.tps):raise ValueError('Precio no positivo')
                if len(sig.tps)>5:raise ValueError('Se admiten hasta 5 TP')
                if sig.side=='LONG' and not (sig.sl<price<min(sig.tps)):
                    raise ValueError('Niveles incompatibles LONG')
                if sig.side=='SHORT' and not (sig.sl>price>max(sig.tps)):
                    raise ValueError('Niveles incompatibles SHORT')
                if sig.side=='LONG' and list(sig.tps)!=sorted(set(sig.tps)):
                    raise ValueError('TP no crecientes o repetidos')
                if sig.side=='SHORT' and list(sig.tps)!=sorted(set(sig.tps),reverse=True):
                    raise ValueError('TP no decrecientes o repetidos')
                margin=D(self.repo.setting('margin','5'))
                from punk.risk_config import effective_leverage,available_budget
                leverage=effective_leverage(self.repo.setting('leverage','20'),spec)
                max_positions=int(self.repo.setting('max_positions','3'))
                if margin<=0:raise ValueError('Margen invalido')
                active=[t for t in self.repo.trades(limit=None) if t['state'] not in ('CLOSED','REJECTED')]
                from punk.paper_limit import pending
                queued=sum(r['status'] in ('PENDING','CLAIMED') and self.repo.trade(r['signal_id']) is None
                           for r in pending(self.repo))
                # Claimed LIMIT transitions into this same trade and must not count twice.
                key=f'{channel}:{message_id}'
                own_queued=any(r['signal_id']==key and r['status']=='CLAIMED' for r in pending(self.repo))
                if len(active)+queued-(1 if own_queued else 0)>=max_positions:
                    raise ValueError('Limite de posiciones')
                if available_budget(self)<margin:raise ValueError('Margen insuficiente')
                qty=(margin*leverage/price/spec['step']).to_integral_value(rounding=ROUND_DOWN)*spec['step']
                if qty<spec['min_qty']:raise ValueError('Cantidad inferior al minimo')
                loss=qty*abs(price-sig.sl)
                max_loss=D(self.repo.setting('max_loss_usdt','5'))
                if loss>max_loss:raise ValueError('Perdida SL supera limite')
                max_exposure=D(self.repo.setting('max_exposure_usdt','300'))
                active_exposure=sum((D(t['remaining'])*D(t['entry']) for t in active),D(0))
                if active_exposure+qty*price>max_exposure:raise ValueError('Exposicion maxima')
                # No order is sent before basic risk checks.
                client_id=hashlib.sha256(('PAPER:'+key).encode()).hexdigest()[:32]
                try: order=self.exchange.open(client_id,sig.symbol,sig.side,qty)
                except TimeoutError:
                    # Unknown outcome is NEVER retried automatically.
                    self.repo.insert_trade({'signal_id':key,'channel':channel,'message_id':message_id,'symbol':sig.symbol,'side':sig.side,'entry':str(price),'sl':str(sig.sl),'tps':[str(x) for x in sig.tps],'qty':str(qty),'client_id':client_id,'order_id':'UNKNOWN','state':'UNKNOWN_BLOCKED'},[])
                    return {'status':'UNKNOWN_BLOCKED','reason':'Entrada incierta: requiere conciliacion'}
                splits=[qty/len(sig.tps)]*len(sig.tps)
                protections=[('SL',str(sig.sl),str(qty),0)]
                protections += [('TP'+str(i+1),str(tp),str(splits[i]),0) for i,tp in enumerate(sig.tps)]
                t={'signal_id':key,'channel':channel,'message_id':message_id,'symbol':sig.symbol,
                   'side':sig.side,'entry':order['price'],'sl':str(sig.sl),'tps':[str(x) for x in sig.tps],
                   'qty':str(qty),'client_id':client_id,'order_id':order['order_id'],'state':'UNPROTECTED',
                   'margin':str(margin),'leverage':leverage}
                self.repo.insert_trade(t,protections)
                for kind,p,q,_ in protections:
                    try: self.exchange.place_protection(client_id,kind,p,q)
                    except TimeoutError:
                        return {'status':'PROTECTION_UNKNOWN','reason':f'{kind} incierto; bloqueado hasta conciliacion'}
                    if not self.exchange.confirm_protection(client_id,kind,p,q):
                        return {'status':'UNPROTECTED','reason':f'{kind} sin confirmacion'}
                    self.repo.update_protection(key,kind,confirmed=True)
                self.repo.update_trade(key,state='PROTECTED')
                self.exchange.available-=margin
                return {'status':'PROTECTED','signal_id':key,'order_id':order['order_id']}
            except (ValueError,InvalidOperation) as exc:
                return {'status':'RECHAZADA','reason':str(exc)}

    def partial_tp(self, signal_id, tp_number):
        """PAPER simulation only: a caller-supplied TP fill, never a real exchange fill."""
        with self.lock:
            t=self.repo.trade(signal_id)
            if not t or t['state']!='PROTECTED':raise ValueError('Posicion no protegida')
            kind=f'TP{tp_number}'
            if self.repo.fill_exists(signal_id,kind):return {'status':'ALREADY_FILLED'}
            p=next((x for x in self.repo.protections(signal_id) if x['kind']==kind),None)
            if not p or not p['confirmed']:raise ValueError('TP no confirmado')
            remaining=D(t['remaining'])-D(p['qty'])
            if remaining<0:raise ValueError('Cantidad imposible')
            fee_rate=D(self.repo.setting('fee_rate','0.0006'))
            entry=D(t['entry']); exit_price=D(p['price'])
            fee=fee_rate*(entry+exit_price)*D(p['qty'])
            gross=(exit_price-entry)*D(p['qty'])*(1 if t['side']=='LONG' else -1)
            realized=D(t['realized'])+gross-fee
            be=(entry*(1+2*fee_rate) if t['side']=='LONG' else entry*(1-2*fee_rate))
            # Subsequent TP fills must not replace an already confirmed BE.
            be_price=D(t['sl']) if t['be_state']=='CONFIRMED' else be
            self.exchange.place_protection(t['client_id'],'SL',be_price,remaining)
            if not self.exchange.confirm_protection(t['client_id'],'SL',be_price,remaining):
                raise ValueError('BE sin confirmacion')
            changed=self.repo.apply_paper_fill(signal_id,kind,p['qty'],p['price'],remaining,
                                               realized,be_price,'CONFIRMED')
            return {'status':'BE_CONFIRMED' if changed else 'ALREADY_FILLED',
                    'remaining':str(remaining),'be':str(be_price)}

    def paper_exit(self, signal_id, kind, observed_price=None):
        """Apply PAPER-only TP2..TP5 or stop settlement with idempotent receipts.

        Stops are filled at the less favorable of stop and observed last price,
        so a gap cannot create an optimistic stop execution.
        """
        with self.lock:
            t=self.repo.trade(signal_id)
            if not t or t['state']!='PROTECTED':
                return {'status':'NOT_OPEN'}
            if kind!='SL' and (not kind.startswith('TP') or not kind[2:].isdigit()):
                raise ValueError('INVALID_EXIT_KIND')
            if self.repo.fill_exists(signal_id,kind):
                return {'status':'ALREADY_FILLED'}
            protections=self.repo.protections(signal_id)
            p=next((x for x in protections if x['kind']==kind and x['confirmed']),None)
            if not p: raise ValueError('PROTECTION_NOT_CONFIRMED')
            if kind.startswith('TP') and int(kind[2:])>1:
                for i in range(1,int(kind[2:])):
                    if not self.repo.fill_exists(signal_id,'TP'+str(i)):
                        raise ValueError('PREVIOUS_TP_NOT_FILLED')
            remaining=D(t['remaining'])
            if remaining<=0: return {'status':'NOT_OPEN'}
            if kind=='SL':
                if observed_price is None: raise ValueError('STOP_OBSERVATION_REQUIRED')
                observed=D(observed_price)
                stop=D(p['price'])
                if not observed.is_finite() or observed<=0: raise ValueError('INVALID_PRICE')
                if (t['side']=='LONG' and observed>stop) or (t['side']=='SHORT' and observed<stop):
                    raise ValueError('STOP_NOT_REACHED')
                fill_price=min(stop,observed) if t['side']=='LONG' else max(stop,observed)
                qty=remaining
            else:
                fill_price=D(p['price'])
                requested=D(p['qty'])
                qty=min(remaining,requested)
                # Final TP consumes the residual including rounding remainder.
                if int(kind[2:])==len(json.loads(t['tps'])):
                    qty=remaining
            new_remaining=remaining-qty
            fee_rate=D(self.repo.setting('fee_rate','0.0006'))
            if not fee_rate.is_finite() or fee_rate<0 or fee_rate>=D('0.1'):
                raise ValueError('INVALID_FEE')
            gross=(fill_price-D(t['entry']))*qty*(1 if t['side']=='LONG' else -1)
            fees=(D(t['entry'])+fill_price)*qty*fee_rate
            realized=D(t['realized'])+gross-fees
            closed=new_remaining==0
            if not closed:
                # Simulated protective quantity must be confirmed before accounting.
                self.exchange.place_protection(t['client_id'],'SL',D(t['sl']),new_remaining)
                if not self.exchange.confirm_protection(t['client_id'],'SL',D(t['sl']),new_remaining):
                    raise ValueError('SL_QUANTITY_NOT_CONFIRMED')
            applied=self.repo.apply_paper_exit(signal_id,kind,qty,fill_price,new_remaining,realized,closed)
            if not applied: return {'status':'ALREADY_FILLED'}
            if closed:
                margin=self.repo.reservation(signal_id)
                if margin:
                    self.exchange.available=max(D(0),self.exchange.available+D(margin['margin'])+realized)
                else:
                    # Legacy trades cannot supply an auditable margin receipt.
                    self.recover_paper()
            return {'status':'CLOSED_PAPER' if closed else 'PARTIAL_PAPER',
                    'kind':kind,'remaining':str(new_remaining),'realized':str(realized),
                    'price':str(fill_price)}

    def snapshot(self):
        return {'mode':'PAPER','live_enabled':False,'auto_paper':self.repo.setting('auto_paper','ON'),
                'balance_available_simulated':str(self.exchange.available),
                'recovery': self.recovery_status,
                'telegram':'NOT_CONFIGURED','bitunix':'SIMULATED_ONLY',
                'trades':self.repo.trades(),'events':self.repo.events(),
                'paper_limit_intents': __import__('punk.paper_limit',fromlist=['pending']).pending(self.repo),
                'config':__import__('punk.risk_config',fromlist=['current']).current(self.repo),
                'capacity':__import__('punk.risk_config',fromlist=['capacity']).capacity(self)}
