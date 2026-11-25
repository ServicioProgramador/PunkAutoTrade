"""F58: Telegram -> LIVE automatic execution with explicit arming and hard limits.

Authorization model: arming (CLI only, typed confirmation) is the operator's
standing authorization for NEW signals of the authorized channel, bounded by the
limits stored with the arming. Each accepted signal still goes through the full
F56 path: fresh GET plan -> one-shot token (issued internally) -> fresh preflight
-> single entry -> GET protection -> BE supervision.

Hard rules:
- Modes: OFF | SHADOW (plan + log, never sends) | LIVE.
- Only NEW messages: edits are ignored, messages older than max_age are ignored.
- One attempt per message (signal_id = channel:message_id). Never retried.
- Any blocking record (uncertain/review state) => no new entries AND auto-disarm.
- Limits: max simultaneous LIVE trades, max new trades per UTC day.
- Telegram never waits for Bitunix: signals go to a bounded queue + worker thread.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from punk.core import parse
from punk.live_executor import BUILD, IDLE, TERMINAL, D
from punk.live_write_lab import LabBlocked

MODES = ('OFF', 'SHADOW', 'LIVE')
POLICIES = ('AUTO', 'LIMIT', 'MARKET')
DEFAULTS = {'mode': 'OFF', 'max_open': '2', 'max_per_day': '3', 'entry_policy': 'AUTO',
            'max_entry_dev_pct': '0.3', 'tp_mode': 'LAST', 'max_age_seconds': '120',
            'use_signal_leverage': 'YES', 'armed_at': '', 'disarm_reason': '', 'follow_channel_be': 'YES'}


class AutoStore:
    """Arming state + decision log inside the LIVE database."""

    def __init__(self, live_store):
        self.store = live_store
        self.store._tx(lambda c: c.executescript('''
            CREATE TABLE IF NOT EXISTS live_auto_settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_auto_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, signal_id TEXT NOT NULL,
              decision TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_signal_cache (
              signal_id TEXT PRIMARY KEY, at INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
              text TEXT NOT NULL);'''), begin=False)

    # F66: every valid channel signal is kept (any mode), so a manual open can be validated by the
    # system and a manual position can be matched with its signal's SL/TP.
    def cache_signal(self, signal_id, symbol, side, text):
        self.store._tx(lambda c: c.execute(
            'INSERT OR IGNORE INTO live_signal_cache(signal_id,at,symbol,side,text) VALUES(?,?,?,?,?)',
            (signal_id, int(time.time()), symbol, side, text[:4000])))

    def cached_message(self, message_id):
        row = self.store._tx(lambda c: c.execute(
            "SELECT * FROM live_signal_cache WHERE signal_id LIKE ? ORDER BY at DESC LIMIT 1",
            ('%:' + str(int(message_id)),)).fetchone())
        return dict(row) if row else None

    def latest_signal(self, symbol, side, max_age=86400):
        row = self.store._tx(lambda c: c.execute(
            'SELECT * FROM live_signal_cache WHERE symbol=? AND side=? AND at>=? ORDER BY at DESC LIMIT 1',
            (symbol, side, int(time.time()) - max_age)).fetchone())
        return dict(row) if row else None

    def settings(self):
        rows = self.store._tx(lambda c: c.execute('SELECT k,v FROM live_auto_settings').fetchall())
        out = dict(DEFAULTS)
        out.update({r['k']: r['v'] for r in rows})
        return out

    def save(self, **values):
        def fn(c):
            for k, v in values.items():
                if k not in DEFAULTS:
                    raise ValueError('INVALID_AUTO_SETTING')
                c.execute('INSERT INTO live_auto_settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                          (k, str(v)))
        self.store._tx(fn)

    def log(self, signal_id, decision, detail=None):
        now = int(time.time())
        self.store._tx(lambda c: c.execute('INSERT INTO live_auto_log(at,signal_id,decision,detail) VALUES(?,?,?,?)',
                       (now, str(signal_id)[:80], decision[:60],
                        json.dumps(detail or {}, sort_keys=True, default=str)[:1500])))

    def recent(self, limit=50):
        return [dict(r) for r in self.store._tx(lambda c: c.execute(
            'SELECT * FROM live_auto_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall())]

    def executed_today(self):
        start = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return self.store._tx(lambda c: c.execute(
            "SELECT COUNT(*) FROM live_auto_log WHERE at>=? AND decision='LIVE_SENT'", (start,)).fetchone()[0])


def validate_arm(mode, max_open, max_per_day, entry_policy, max_entry_dev_pct, tp_mode):
    if mode not in MODES:
        raise LabBlocked('INVALID_MODE')
    # 0 = AUTO: as many as the real available balance allows (plus the global settings caps).
    if not (0 <= int(max_open) <= 100 and 0 <= int(max_per_day) <= 500):
        raise LabBlocked('INVALID_LIMITS')
    if entry_policy not in POLICIES:
        raise LabBlocked('INVALID_ENTRY_POLICY')
    if not D('0') < D(max_entry_dev_pct) <= D('3'):
        raise LabBlocked('INVALID_ENTRY_DEVIATION')
    if tp_mode not in ('SPLIT', 'LAST', 'PARTIAL'):
        raise LabBlocked('INVALID_TP_MODE')


class AutoTrader:
    def __init__(self, executor, *, clock=time.time):
        self.ex = executor
        self.auto = AutoStore(executor.store)
        self.clock = clock
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=50)
        self.state = {'worker': 'STOPPED'}

    # ------------------------------------------------------------ arming
    def arm(self, mode, *, max_open=2, max_per_day=3, entry_policy='AUTO', max_entry_dev_pct='0.3',
            tp_mode='LAST'):
        validate_arm(mode, max_open, max_per_day, entry_policy, max_entry_dev_pct, tp_mode)
        blockers = self.ex.store.blocking()
        if mode != 'OFF' and blockers:
            raise LabBlocked('BLOCKED_BY_UNRECONCILED')
        self.auto.save(mode=mode, max_open=int(max_open), max_per_day=int(max_per_day),
                       entry_policy=entry_policy, max_entry_dev_pct=str(D(max_entry_dev_pct)),
                       tp_mode=tp_mode, armed_at=int(self.clock()), disarm_reason='')
        self.auto.log('-', 'ARMED_' + mode, self.auto.settings())
        return self.auto.settings()

    def disarm(self, reason='OPERATOR'):
        self.auto.save(mode='OFF', disarm_reason=reason[:60])
        self.auto.log('-', 'DISARMED', {'reason': reason})
        return self.auto.settings()

    # ------------------------------------------------------------ intake
    def submit(self, channel, message_id, text, date, edited=False, reply_to=None):
        """Called from the Telegram thread: never blocks on Bitunix."""
        try:
            self.queue.put_nowait((str(channel), int(message_id), text or '', date, bool(edited),
                                   int(reply_to) if reply_to else None))
            return 'QUEUED'
        except queue.Full:
            self.auto.log('%s:%s' % (channel, message_id), 'QUEUE_FULL_IGNORED')
            return 'QUEUE_FULL'

    def worker(self):
        self.state['worker'] = 'RUNNING'
        while True:
            item = self.queue.get()
            if item is None:
                self.state['worker'] = 'STOPPED'
                return
            try:
                self.handle(*item)
            except Exception as exc:  # the worker must survive any single signal
                self.auto.log('%s:%s' % (item[0], item[1]), 'WORKER_ERROR',
                              {'error': type(exc).__name__, 'message': str(exc)[:160]})

    # ------------------------------------------------------------ decision
    def handle(self, channel, message_id, text, date, edited=False, reply_to=None):
        sid = '%s:%d' % (channel, message_id)
        cfg = self.auto.settings()
        mode = cfg['mode']
        if not edited:
            sig0, st0, _ = parse(text)
            if st0 == 'VALIDA':
                try:
                    self.auto.cache_signal(sid, self.ex.resolve_symbol(sig0.symbol), sig0.side, text)
                except Exception:
                    pass
        if not edited and parse(text)[1] != 'VALIDA':
            from punk.channel_followup import classify
            fu = classify(text)
            if fu is not None:
                return self.handle_followup(channel, sid, fu, date, reply_to, cfg)
        if mode == 'OFF':
            # Make missed signals visible (dashboard + Telegram) instead of silently ignoring them.
            if not edited and parse(text)[1] == 'VALIDA':
                sig = parse(text)[0]
                return self._skip(sid, 'AUTO_OFF_SIGNAL', {'symbol': sig.symbol, 'side': sig.side})
            return self._skip(sid, 'AUTO_OFF', log=False)
        if edited:
            return self._skip(sid, 'EDIT_IGNORED')
        signal, status, reason = parse(text)
        if status != 'VALIDA':
            return self._skip(sid, 'NOT_A_SIGNAL', {'status': status, 'reason': reason}, log=status != 'INFORMATIVA')
        age = self._age(date)
        if age is None or age > int(cfg['max_age_seconds']):
            return self._skip(sid, 'SIGNAL_TOO_OLD', {'age_seconds': age})
        with self.lock:
            if self.ex.store.by_signal(sid):
                return self._skip(sid, 'ALREADY_HANDLED')
            blockers = self.ex.store.blocking()
            if blockers:
                if mode == 'LIVE':
                    self.disarm('BLOCKING_RECORD_' + blockers[0]['client_id'][-8:])
                return self._skip(sid, 'BLOCKED_BY_UNRECONCILED', {'records': [r['client_id'] for r in blockers]})
            open_live = [r for r in self.ex.store.all(1000) if r['state'] not in TERMINAL | IDLE]
            if int(cfg['max_open']) > 0 and len(open_live) >= int(cfg['max_open']):
                return self._skip(sid, 'MAX_OPEN_REACHED', {'open': len(open_live)})
            if mode == 'LIVE' and int(cfg['max_per_day']) > 0 and self.auto.executed_today() >= int(cfg['max_per_day']):
                return self._skip(sid, 'MAX_PER_DAY_REACHED')
            entry_type = self._entry_type(signal, cfg)
            leverage = signal.leverage if (cfg['use_signal_leverage'] == 'YES' and signal.leverage) else None
            if leverage is not None and leverage < 3:
                leverage = None
            try:
                plan = self.ex.plan(signal, signal_id=sid, entry_type=entry_type, leverage=leverage,
                                    tp_mode=cfg['tp_mode'])
            except LabBlocked as exc:
                return self._skip(sid, 'PLAN_REFUSED', {'reason': str(exc), 'symbol': self.ex.resolve_symbol(signal.symbol),
                                                        'signal_symbol': signal.symbol})
            summary = {k: plan[k] for k in ('client_id', 'symbol', 'side', 'entry_type', 'entry', 'qty', 'sl',
                                            'leverage', 'leverage_requested', 'tp_mode', 'required_usdt',
                                            'max_loss_usdt')}
            if plan['symbol'] != signal.symbol:
                summary['signal_symbol'] = signal.symbol      # F64: alias applied (e.g. RAYSOL -> RAY)
            if mode == 'SHADOW':
                self.ex.store.transition(plan['client_id'], ('PLANNED',), 'SHADOW_ONLY', reason='SHADOW_MODE')
                self.auto.log(sid, 'SHADOW_PLANNED', summary)
                return {'decision': 'SHADOW_PLANNED', **summary}
            # LIVE: the arming is the standing approval; the token keeps the one-shot semantics.
            try:
                token = self.ex.approve(plan['client_id'])
                result = self.ex.execute(plan['client_id'], token)
            except LabBlocked as exc:
                self.auto.log(sid, 'EXECUTE_REFUSED', {**summary, 'reason': str(exc)})
                return {'decision': 'EXECUTE_REFUSED', 'reason': str(exc)}
            self.auto.log(sid, 'LIVE_SENT', {**summary, 'state': result.get('state'), 'reason': result.get('reason')})
            if result.get('blocking'):
                self.disarm('BLOCKING_AFTER_' + plan['client_id'][-8:])
            return {'decision': 'LIVE_SENT', **result}

    def _find_trade(self, channel, symbol, reply_to):
        if reply_to:
            row = self.ex.store.by_signal('%s:%d' % (channel, reply_to))
            if row:
                return row
        if not symbol:
            return None
        rows = [r for r in self.ex.store.all(500) if r['symbol'] == symbol]
        live = [r for r in rows if r['state'] not in TERMINAL | IDLE]
        if live:
            return live[0]
        recent = [r for r in rows if r['state'] == 'CLOSED' and self.clock() - r['updated_at'] < 86400]
        return recent[0] if recent else None

    def handle_followup(self, channel, sid, fu, date, reply_to, cfg):
        """Channel follow-up: compare with the real trade; optionally request BE. Never opens/closes."""
        age = self._age(date)
        if age is None or age > 900:
            return self._skip(sid, 'FOLLOWUP_TOO_OLD', log=False)
        base = {'kind': fu.kind, 'symbol': fu.symbol, 'tp': fu.tp_number, 'pct': fu.pct}
        if fu.kind == 'CORRECTION':
            return self._skip(sid, 'CHANNEL_CORRECTION', base)
        with self.lock:
            row = self._find_trade(channel, self.ex.resolve_symbol(fu.symbol) if fu.symbol else fu.symbol, reply_to)
            if not row:
                return self._skip(sid, 'CHANNEL_NO_TRADE', base)
            cid = row['client_id']
            base.update({'symbol': row['symbol'], 'client_id': cid})
            if fu.kind == 'TP_HIT':
                if cfg.get('follow_channel_be') != 'YES':
                    return self._skip(sid, 'CHANNEL_TP_HIT', {**base, 'state': row['state']})
                if row['state'] == 'BE_CONFIRMED':
                    return self._skip(sid, 'CHANNEL_BE_ALREADY', base)
                try:
                    st = self.ex.request_channel_be(cid)['state']
                except LabBlocked as exc:
                    return self._skip(sid, 'CHANNEL_TP_HIT', {**base, 'state': row['state'], 'reason': str(exc)})
                return self._skip(sid, 'CHANNEL_BE_APPLIED' if st == 'BE_CONFIRMED' else 'CHANNEL_BE_PENDING',
                                  {**base, 'state': st})
            # SL_HIT / ALL_TP: refresh by GET, then compare with reality.
            if row['state'] not in TERMINAL:
                try:
                    self.ex.reconcile(cid)
                except Exception:
                    pass
                row = self.ex.store.get(cid)
            decision = 'CHANNEL_MATCH' if row['state'] == 'CLOSED' else 'CHANNEL_MISMATCH'
            return self._skip(sid, decision, {**base, 'state': row['state'], 'be_price': row.get('be_price')})

    def _skip(self, sid, decision, detail=None, log=True):
        if log:
            self.auto.log(sid, decision, detail)
        return {'decision': decision, **(detail or {})}

    def _age(self, date):
        if date is None:
            return None
        if isinstance(date, (int, float)):
            return max(0.0, self.clock() - float(date))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, self.clock() - date.timestamp())

    def _entry_type(self, signal, cfg):
        policy = cfg['entry_policy']
        if signal.entry_price is None:
            return 'MARKET'
        if policy in ('LIMIT', 'MARKET'):
            return policy
        try:
            market = self.ex._price(self.ex.resolve_symbol(signal.symbol))
        except Exception:
            return 'LIMIT'
        dev = abs(market - signal.entry_price) / signal.entry_price * 100
        return 'MARKET' if dev <= D(cfg['max_entry_dev_pct']) else 'LIMIT'

    def status(self):
        cfg = self.auto.settings()
        return {'build': BUILD, 'settings': cfg, 'executed_today': self.auto.executed_today(),
                'worker': self.state.get('worker'), 'queue': self.queue.qsize(),
                'recent': self.auto.recent(20)}


def auto_payload(store):
    """Read-only dashboard view from a LiveStore (no network)."""
    if store is None:
        return {'status': 'NO_LIVE_DB'}
    a = AutoStore(store)
    return {'status': 'OK', 'settings': a.settings(), 'executed_today': a.executed_today(), 'recent': a.recent(20)}


# ------------------------------------------------------------------ F66
def signal_lookup(auto_store, resolve=lambda s: s):
    """(symbol, side) -> (signal_id, Signal) from the last 24 h of channel signals, or None."""
    def lookup(symbol, side):
        row = auto_store.latest_signal(symbol, side)
        if not row:
            return None
        sig, status, _ = parse(row['text'])
        if status != 'VALIDA' or resolve(sig.symbol) != symbol or sig.side != side:
            return None
        return row['signal_id'], sig
    return lookup


def adopt_cycle(executor, auto_store, seen):
    """Supervisor hook: adopt manual positions and log the decisions (dashboard + Telegram)."""
    for decision, detail in executor.adopt_manual(signal_lookup(auto_store, executor.resolve_symbol), seen):
        auto_store.log('adopt:' + detail.get('position_id', '-'), decision, detail)


def manual_open_plan(trader, message_id, max_loss=None):
    """F66: the operator opens a channel signal by hand THROUGH the system (all checks, own entry policy).
    Only the per-trade loss limit may be raised, explicitly, for this one trade."""
    row = trader.auto.cached_message(message_id)
    if not row:
        raise LabBlocked('MESSAGE_NOT_IN_CACHE')
    signal, status, reason = parse(row['text'])
    if status != 'VALIDA':
        raise LabBlocked('NOT_A_SIGNAL')
    cfg = trader.auto.settings()
    sid = 'manual:' + row['signal_id']
    entry_type = trader._entry_type(signal, cfg)
    leverage = signal.leverage if (cfg['use_signal_leverage'] == 'YES' and signal.leverage and signal.leverage >= 3) else None
    return trader.ex.plan(signal, signal_id=sid, entry_type=entry_type, leverage=leverage,
                          tp_mode=cfg['tp_mode'], max_loss=max_loss)
