"""F56: integrated Bitunix LIVE executor (controlled, one operation per approval).

Circuit:
  signal -> plan (GET preflight, contract, tiers, capital, fees, rounding)
         -> F51 one-shot approval (exact fingerprint, TTL)
         -> fresh preflight -> consume approval -> leverage set + GET verify
         -> F22 journal ARMED (persisted BEFORE the POST) -> POST place_order (SL attached)
         -> F32 ACK journal -> GET order detail (clientId)
         -> F54 state machine with the real Bitunix port: SL -> TP1..TPn, GET readback each
         -> PROTECTED -> supervision: TP1 fill or ROI >= threshold -> BE (F54 move_be)
         -> position absent -> CLOSED

Invariants:
- No automatic resend of any write whose outcome is not proven.
- clientId is deterministic per signal; a changed signal with the same id is refused.
- Any uncertain/critical state blocks NEW entries until reconciled.
- PROTECTED only after GET readback of SL and every planned TP on the position.
- Telegram automation is NOT enabled here: every entry needs an operator token.
"""
from __future__ import annotations

import json
import re
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, InvalidOperation
from pathlib import Path

from punk.bitunix_ack_journal import AckJournal
from punk.bitunix_protection_port import BitunixProtectionPort, canon
from punk.bitunix_readonly import BitunixContractError
from punk.bitunix_write_contract import entry_contract
from punk.bitunix_write_journal import WriteJournal
from punk.f46_live_guard import guard_snapshot
from punk.f51_one_shot_authorization import OneShotApproval
from punk.f54_protection_cycle import F54ProtectionCycle
from punk.live_write_lab import Intent, LabBlocked
from punk.risk_config import ConfigurationError, PublicContractLimits, effective_leverage

BUILD = 'F77'
# F64: channel tickers that are named differently on Bitunix (LIVE 2026-11-24: #RAYSOL/USDT).
SYMBOL_ALIASES = {'RAYSOLUSDT': 'RAYUSDT'}
TERMINAL = frozenset({'CLOSED', 'CANCELED', 'REJECTED_RESOLVED', 'ABORTED_BEFORE_ENTRY', 'PLAN_EXPIRED',
                      'SHADOW_ONLY'})
# Known, reconciled-or-progressing states: they do NOT block new entries nor disarm the auto mode.
# (LIVE 2026-11-22: a LIMIT entry ACK whose order was not yet visible by GET disarmed the auto mode.)
SAFE_ACTIVE = frozenset({'ENTRY_PENDING', 'PROTECTED', 'BE_CONFIRMED', 'ENTRY_ACK', 'PROTECTING',
                         'BE_UNKNOWN', 'ENTRY_PARTIAL', 'CANCEL_REQUESTED', 'SL_ONLY'})
# F71: Bitunix answers code 710002 "This trading pair does not currently support trading via OpenAPI" on
# tpsl endpoints for some pairs (LIVE 2026-11-25 DASHUSDT) while the entry with its attached SL works.
API_UNSUPPORTED_CODES = {710002}
SYMBOL_FLAG_TTL = 7 * 86400
# BLOCKING (critical, needs evidence or operator): ENTRY_ARMED, ENTRY_UNKNOWN,
# ENTRY_REJECTED_UNCONFIRMED, PROTECTION_REVIEW, REVIEW, EMERGENCY_CLOSE_SENT.
ACK_LOOKUP_GRACE = 600
POSITION_VISIBILITY_GRACE = 120
IDLE = frozenset({'PLANNED', 'APPROVED'})
# Everything else (ENTRY_ARMED, ENTRY_ACK, ENTRY_UNKNOWN, ENTRY_REJECTED_UNCONFIRMED,
# ENTRY_PARTIAL, CANCEL_REQUESTED, PROTECTING, PROTECTION_REVIEW, BE_UNKNOWN,
# EMERGENCY_CLOSE_SENT, REVIEW) is BLOCKING.


def D(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise LabBlocked('INVALID_DECIMAL') from None
    if not d.is_finite():
        raise LabBlocked('INVALID_DECIMAL')
    return d


def quantize(value, tick, rounding):
    return canon((D(value) / tick).to_integral_value(rounding=rounding) * tick)


class LiveConfig:
    """Live-only parameters. Capital limits come from the shared settings table."""

    def reload_capital(self):
        """F65: re-read the capital limits saved from the dashboard (Configuración).
        LIVE 2026-11-24: margin changed 5 -> 10 USDT in the panel, but the running process kept
        planning 5 USDT because the limits were only read at start-up."""
        repo, over = self._repo, self._over
        get = (lambda k, d: repo.setting(k, d)) if repo is not None else (lambda k, d: d)
        self.margin = D(over.get('margin', get('margin', '5')))
        self.leverage = int(over.get('leverage', get('leverage', '20')))
        self.max_positions = int(over.get('max_positions', get('max_positions', '3')))
        self.max_exposure = D(over.get('max_exposure_usdt', get('max_exposure_usdt', '300')))
        self.max_loss = D(over.get('max_loss_usdt', get('max_loss_usdt', '5')))
        if hasattr(self, 'tp_mode'):
            self._load_strategy()        # F77: the dashboard preset applies without restarting
        return self

    def _load_strategy(self):
        """BE trigger and first-TP level. F77 precedence: explicit override > dashboard preset (settings
        table) > .env > default. BE trigger = favorable PRICE move in % (1 % = 20 % ROI at 20x)."""
        repo, over, env = self._repo, self._over, os.environ.get
        get = (lambda k: repo.setting(k, '')) if repo is not None else (lambda k: '')
        self.be_move_pct = D(over.get('be_move_pct') or get('live_be_move_pct') or env('PA_LIVE_BE_MOVE_PCT') or '1')
        # F76: first TP level (PARTIAL 75 % / FIRST 100 %), separate from the BE trigger.
        self.tp_pct = D(over.get('tp_pct') or get('live_tp_pct') or env('PA_LIVE_TP_PCT') or self.be_move_pct)

    def __init__(self, repo=None, **over):
        get = (lambda k, d: repo.setting(k, d)) if repo is not None else (lambda k, d: d)
        env = os.environ.get
        self._repo, self._over = repo, dict(over)
        self.reload_capital()
        self.fee_rate = D(over.get('fee_rate', env('PA_LIVE_FEE_RATE', get('live_fee_rate', '0.0006'))))
        # BE trigger = favorable PRICE move from the average entry, in % (user rule:
        # 1% move == 1% x leverage ROI, e.g. 20% ROI at 20x), or TP1 touched.
        self._load_strategy()
        # F63: extra profit locked by the BE stop, in % of price, ON TOP of the fees already
        # paid (open fee + funding, read from Bitunix) and the estimated close fee.
        self.be_profit_pct = D(over.get('be_profit_pct', env('PA_LIVE_BE_PROFIT_PCT', get('live_be_profit_pct', '0.1'))))
        # F63: the BE stop triggers on LAST price (the initial SL keeps MARK price against wicks).
        # LIVE evidence 2026-11-23: MARK-triggered BE stops filled several ticks below BE on fast dumps.
        self.be_stop_type = str(over.get('be_stop_type', env('PA_LIVE_BE_STOP_TYPE', 'LAST_PRICE'))).upper()
        # F68: LIMIT entries waiting on the far side of the market are allowed (pending setups).
        self.pending_limit = str(over.get('pending_limit', env('PA_LIVE_PENDING_LIMIT', 'YES'))).upper() == 'YES'
        # F66: take over positions opened by hand on Bitunix (SL/TP from the channel signal + BE).
        self.adopt_manual = str(over.get('adopt_manual', env('PA_LIVE_ADOPT_MANUAL', 'YES'))).upper() == 'YES'
        self.adopt_grace = int(over.get('adopt_grace', 60))
        # F64: channel ticker aliases. Built-in + PA_SYMBOL_ALIASES="RAYSOL=RAY,FOO=BAR".
        self.symbol_aliases = dict(SYMBOL_ALIASES)
        for item in str(over.get('symbol_aliases', env('PA_SYMBOL_ALIASES', ''))).split(','):
            if '=' in item:
                a, b = (x.strip().upper() for x in item.split('=', 1))
                if a and b:
                    a = a if a.endswith('USDT') else a + 'USDT'
                    b = b if b.endswith('USDT') else b + 'USDT'
                    self.symbol_aliases[a] = b
        self.be_on_tp1 = str(over.get('be_on_tp1', get('live_be_on_tp1', 'YES'))).upper() == 'YES'
        self.limit_ttl = int(over.get('limit_ttl_seconds', get('live_limit_ttl_seconds', '7200')))
        self.plan_ttl = int(over.get('plan_ttl_seconds', 300))
        self.approval_ttl = int(over.get('approval_ttl_seconds', 180))
        self.tp_mode = str(over.get('tp_mode', env('PA_LIVE_TP_MODE', get('live_tp_mode', 'SPLIT')))).upper()
        self.max_slippage_pct = D(over.get('max_slippage_pct', get('live_max_slippage_pct', '0.5')))
        self.require_isolation = str(over.get('allow_cross', env('PA_LIVE_ALLOW_CROSS', 'NO'))).upper() != 'YES'
        if self.margin <= 0 or not 3 <= self.leverage <= 200 or self.max_positions < 1:
            raise LabBlocked('INVALID_LIVE_CONFIG')
        if not D('0') <= self.fee_rate < D('0.01'):
            raise LabBlocked('INVALID_FEE_RATE')
        if self.tp_mode not in ('SPLIT', 'LAST', 'PARTIAL', 'FIRST'):
            raise LabBlocked('INVALID_TP_MODE')
        # F67: PARTIAL = close partial_pct % at be_move_pct % in favour (exchange TP), rest to the last target.
        self.partial_pct = D(over.get('partial_pct', env('PA_LIVE_PARTIAL_PCT', get('live_partial_pct', '75'))))
        if not D('0.05') <= self.tp_pct <= D('1000'):
            raise LabBlocked('INVALID_TP_PCT')
        if not D('10') <= self.partial_pct <= D('95'):
            raise LabBlocked('INVALID_PARTIAL_PCT')
        if not D('0') <= self.be_profit_pct <= D('2'):
            raise LabBlocked('INVALID_BE_PROFIT_PCT')
        if self.be_stop_type not in ('LAST_PRICE', 'MARK_PRICE'):
            raise LabBlocked('INVALID_BE_STOP_TYPE')

    def public(self):
        return {k: str(v) for k, v in vars(self).items()}


class LiveStore:
    """Operational LIVE SQLite. Connections always closed (Windows-safe)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tx(lambda c: c.executescript('''
            CREATE TABLE IF NOT EXISTS live_trades (
              client_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
              side TEXT NOT NULL, entry_type TEXT NOT NULL, fingerprint TEXT NOT NULL,
              plan TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
              entry_order_id TEXT NOT NULL DEFAULT '', position_id TEXT NOT NULL DEFAULT '',
              filled_qty TEXT NOT NULL DEFAULT '', be_state TEXT NOT NULL DEFAULT 'PENDING',
              be_price TEXT NOT NULL DEFAULT '', tp_mode TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS live_symbol_limits (
              symbol TEXT PRIMARY KEY, max_qty TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS live_symbol_flags (
              symbol TEXT PRIMARY KEY, flag TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS live_leases (
              client_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS live_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
              at INTEGER NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
        '''), begin=False)
        cols = self._tx(lambda c: [r['name'] for r in c.execute('PRAGMA table_info(live_trades)')], begin=False)
        if 'tp_mode' not in cols:
            self._tx(lambda c: c.execute("ALTER TABLE live_trades ADD COLUMN tp_mode TEXT NOT NULL DEFAULT ''"), begin=False)

    def _tx(self, fn, begin=True):
        con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA busy_timeout=10000')
            if begin:
                con.execute('BEGIN IMMEDIATE')
            out = fn(con)
            if begin:
                con.execute('COMMIT')
            return out
        except BaseException:
            if begin:
                try:
                    con.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
            raise
        finally:
            con.close()

    def get(self, client_id):
        row = self._tx(lambda c: c.execute('SELECT * FROM live_trades WHERE client_id=?', (client_id,)).fetchone())
        return dict(row) if row else None

    def by_signal(self, signal_id):
        row = self._tx(lambda c: c.execute('SELECT * FROM live_trades WHERE signal_id=?', (signal_id,)).fetchone())
        return dict(row) if row else None

    def all(self, limit=200):
        return [dict(r) for r in self._tx(lambda c: c.execute(
            'SELECT * FROM live_trades ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall())]

    def insert(self, row):
        now = int(time.time())
        def fn(c):
            c.execute('''INSERT INTO live_trades(client_id,signal_id,symbol,side,entry_type,fingerprint,
                         plan,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                      (row['client_id'], row['signal_id'], row['symbol'], row['side'], row['entry_type'],
                       row['fingerprint'], json.dumps(row['plan'], sort_keys=True), 'PLANNED', now, now))
            c.execute('INSERT INTO live_events(client_id,at,event,detail) VALUES(?,?,?,?)',
                      (row['client_id'], now, 'PLANNED', '{}'))
        self._tx(fn)

    def transition(self, client_id, allowed, state, *, event=None, detail=None, **fields):
        ok = {'reason', 'entry_order_id', 'position_id', 'filled_qty', 'be_state', 'be_price', 'tp_mode'}
        if set(fields) - ok:
            raise ValueError('INVALID_FIELDS')
        now = int(time.time())
        def fn(c):
            cur = c.execute('SELECT state FROM live_trades WHERE client_id=?', (client_id,)).fetchone()
            if cur is None or (allowed is not None and cur['state'] not in allowed):
                raise LabBlocked('ILLEGAL_LIVE_TRANSITION')
            sets = ['state=?', 'updated_at=?'] + [k + '=?' for k in fields]
            c.execute('UPDATE live_trades SET ' + ','.join(sets) + ' WHERE client_id=?',
                      (state, now, *[str(v) for v in fields.values()], client_id))
            c.execute('INSERT INTO live_events(client_id,at,event,detail) VALUES(?,?,?,?)',
                      (client_id, now, event or state, json.dumps(detail or {}, sort_keys=True, default=str)[:2000]))
        self._tx(fn)

    def event(self, client_id, event, detail=None):
        now = int(time.time())
        self._tx(lambda c: c.execute('INSERT INTO live_events(client_id,at,event,detail) VALUES(?,?,?,?)',
                 (client_id, now, event, json.dumps(detail or {}, sort_keys=True, default=str)[:2000])))

    def events(self, client_id=None, limit=100):
        if client_id:
            rows = self._tx(lambda c: c.execute('SELECT * FROM live_events WHERE client_id=? ORDER BY id DESC LIMIT ?',
                                                (client_id, limit)).fetchall())
        else:
            rows = self._tx(lambda c: c.execute('SELECT * FROM live_events ORDER BY id DESC LIMIT ?', (limit,)).fetchall())
        return [dict(r) for r in rows]

    def acquire(self, client_id, owner, ttl=90.0):
        now = time.time()
        def fn(c):
            row = c.execute('SELECT owner,expires_at FROM live_leases WHERE client_id=?', (client_id,)).fetchone()
            if row and row['owner'] != owner and row['expires_at'] > now:
                return False
            c.execute('INSERT INTO live_leases(client_id,owner,expires_at) VALUES(?,?,?) ON CONFLICT(client_id) '
                      'DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at', (client_id, owner, now + ttl))
            return True
        return self._tx(fn)

    def release(self, client_id, owner):
        self._tx(lambda c: c.execute('DELETE FROM live_leases WHERE client_id=? AND owner=?', (client_id, owner)))

    def last_event(self, client_id, name):
        """F73b: the latest event of one kind, however many other events came after it."""
        row = self._tx(lambda c: c.execute('SELECT * FROM live_events WHERE client_id=? AND event=? ORDER BY id DESC LIMIT 1',
                                           (client_id, name)).fetchone())
        return dict(row) if row else None

    def set_max_qty(self, symbol, qty):
        self._tx(lambda c: c.execute('INSERT OR REPLACE INTO live_symbol_limits(symbol,max_qty,at) VALUES(?,?,?)',
                                     (symbol, str(qty), int(time.time()))))

    def max_qty(self, symbol):
        row = self._tx(lambda c: c.execute('SELECT max_qty FROM live_symbol_limits WHERE symbol=?', (symbol,)).fetchone())
        try:
            return Decimal(row['max_qty']) if row else None
        except (InvalidOperation, ValueError):
            return None

    def flag_symbol(self, symbol, flag):
        self._tx(lambda c: c.execute('INSERT OR REPLACE INTO live_symbol_flags(symbol,flag,at) VALUES(?,?,?)',
                                     (symbol, flag, int(time.time()))))

    def symbol_flag(self, symbol):
        row = self._tx(lambda c: c.execute('SELECT * FROM live_symbol_flags WHERE symbol=?', (symbol,)).fetchone())
        if row and time.time() - row['at'] < SYMBOL_FLAG_TTL:
            return row['flag']
        return None

    def blocking(self, exclude=None):
        return [r for r in self.all(1000) if r['state'] not in TERMINAL | SAFE_ACTIVE | IDLE
                and r['client_id'] != exclude]

    def active(self):
        return [r for r in self.all(1000) if r['state'] not in TERMINAL | IDLE]


def _intent_from_plan(plan, *, protection=False, filled=None, tp_mode='SPLIT'):
    if not protection:
        return Intent(plan['signal_id'], plan['symbol'], plan['side'], plan['qty'], plan['entry'],
                      plan['sl'], tuple(plan['tps']))
    tps, qtys = protection_split(plan, filled, tp_mode)
    return Intent(plan['signal_id'] + '#P', plan['symbol'], plan['side'], canon(filled), plan['entry'],
                  plan['sl'], tuple(tps))


def _qty_or_zero(value):
    if value in (None, '', 0, '0'):
        return Decimal(0)
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d >= 0 else None


def split_units(total, step, min_qty, n):
    """Equal TP split in contract units, remainder on the last TP; drop far TPs if too small."""
    units = int(D(total) / step)
    min_units = max(1, int((min_qty / step).to_integral_value(rounding=ROUND_CEILING)))
    k = min(n, units // min_units)
    if k < 1:
        raise LabBlocked('TP_QUANTITY_TOO_SMALL')
    base = units // k
    parts = [base] * k
    parts[-1] += units - base * k
    return [canon(step * p) for p in parts]


def levels_ok_for(long, sl, ref, tp1, market, entry_type, pending=True):
    """Price checks before an entry.
    The levels must be coherent (SL < entry < TP1 for a LONG) and the market must still be between SL
    and TP1. F68: a LIMIT entry that is still WAITING on the far side of the market (LONG buy below the
    price, SHORT sell above it, e.g. "Entrada: 61450 - 61600" with BTC at 60 000) is a valid pending
    setup even if the price is already beyond TP1: it fills only if the price comes back to the entry."""
    sl, ref, tp1, market = D(sl), D(ref), D(tp1), D(market)
    if long:
        coherent = sl < ref < tp1
        live = sl < market < tp1
        waiting = entry_type == 'LIMIT' and pending and market > ref
    else:
        coherent = sl > ref > tp1
        live = sl > market > tp1
        waiting = entry_type == 'LIMIT' and pending and market < ref
    return coherent and (live or waiting)


def partial_level(long, ref, tp1, cfg, tick):
    """F69: the partial closes at whatever comes FIRST: the signal's TP1 or be_move_pct % in favour.
    TP1 is only used if it is beyond the fee cover (2 x fee + BE profit %), so the 75 % never closes
    at a loss or flat."""
    move = getattr(cfg, 'tp_pct', cfg.be_move_pct) / 100
    pct_level = D(quantize(ref * (1 + move) if long else ref * (1 - move), tick, ROUND_FLOOR if long else ROUND_CEILING))
    cover = 2 * cfg.fee_rate + cfg.be_profit_pct / 100
    floor_ = ref * (1 + cover) if long else ref * (1 - cover)
    tp1 = D(tp1)
    tp1_usable = (tp1 > floor_) if long else (tp1 < floor_)
    if tp1_usable and ((tp1 < pct_level) if long else (tp1 > pct_level)):
        return canon(tp1), 'TP1'
    return canon(pct_level), 'MOVE_PCT'


def partial_split(plan, filled):
    """F67 PARTIAL: (partial_tp, last_tp), (pct % of filled, rest) in contract units.
    None when the quantity cannot be split above the contract minimum."""
    step, min_qty = D(plan['qty_step']), D(plan['min_qty'])
    filled = D(filled)
    first = (filled * D(plan.get('partial_pct', '75')) / 100 / step).to_integral_value(rounding=ROUND_FLOOR) * step
    rest = filled - first
    if first < min_qty or rest < min_qty:
        return None
    return [plan['partial_tp'], plan['tps'][-1]], [canon(first), canon(rest)]


def protection_split(plan, filled, tp_mode='SPLIT'):
    if tp_mode == 'FIRST':
        if plan.get('partial_tp'):
            return [plan['partial_tp']], [canon(D(filled))]
        tp_mode = 'LAST'
    if tp_mode == 'PARTIAL':
        split = partial_split(plan, filled) if plan.get('partial_tp') else None
        if split is not None:
            return split
        tp_mode = 'LAST'                   # too small to split: one TP at the last target
    if tp_mode == 'LAST':
        # One exchange TP at the LAST target for the whole filled quantity.
        return [plan['tps'][-1]], [canon(filled)]
    step, min_qty = D(plan['qty_step']), D(plan['min_qty'])
    qtys = split_units(filled, step, min_qty, len(plan['tps']))
    return plan['tps'][:len(qtys)], qtys


class LiveExecutor:
    def __init__(self, db_path, reader, trader=None, *, config=None, clock=time.time, sleep=time.sleep,
                 settle_seconds=6.0):
        self.db_path = Path(db_path)
        self.store = LiveStore(self.db_path)
        self.journal = WriteJournal(self.db_path)
        self.acks = AckJournal(self.db_path)
        self.approvals = OneShotApproval(self.db_path)
        self.cycle = F54ProtectionCycle(self.db_path)
        self.reader = reader
        self.trader = trader
        self.config = config or LiveConfig()
        self.clock = clock
        self.sleep = sleep
        self.settle_seconds = settle_seconds
        self.owner = 'pid%d-%s' % (os.getpid(), secrets.token_hex(4))
        self._lock = threading.RLock()

    @contextmanager
    def _leased(self, client_id):
        with self._lock:
            if not self.store.acquire(client_id, self.owner):
                raise LabBlocked('OPERATION_BUSY')
            try:
                yield
            finally:
                self.store.release(client_id, self.owner)

    # ------------------------------------------------------------- helpers
    def _snapshot(self):
        snap = self.reader.snapshot()
        if not isinstance(snap, dict) or snap.get('status') != 'GET_ONLY_VERIFIED':
            raise LabBlocked('SNAPSHOT_NOT_VERIFIED')
        return snap

    def _price(self, symbol):
        data = self.reader.get('tickers', {'symbols': symbol})
        rows = [x for x in data if isinstance(x, dict) and x.get('symbol') == symbol] if isinstance(data, list) else []
        if len(rows) != 1:
            raise LabBlocked('TICKER_UNAVAILABLE')
        price = D(rows[0].get('lastPrice') or rows[0].get('markPrice') or 0)
        if price <= 0:
            raise LabBlocked('TICKER_UNAVAILABLE')
        return price

    def resolve_symbol(self, symbol):
        """F64: channel ticker -> Bitunix ticker (e.g. RAYSOLUSDT -> RAYUSDT). A wrong alias cannot
        open a bad trade: plan() still requires SL < price < TP1 (LONG) on the real market price."""
        return self.config.symbol_aliases.get(symbol, symbol)

    def _contract(self, symbol):
        try:
            c = self.reader.public_contract(symbol)
        except BitunixContractError as exc:
            raise LabBlocked('SYMBOL_NOT_ON_BITUNIX:' + str(exc)[:40]) from None
        except ValueError:
            raise LabBlocked('SYMBOL_NOT_ON_BITUNIX') from None
        if c.get('symbolStatus') != 'OPEN' or c.get('isApiSupported') is not True:
            raise LabBlocked('CONTRACT_NOT_OPEN')
        try:
            base_p, quote_p = int(c['basePrecision']), int(c['quotePrecision'])
            min_qty = D(c['minTradeVolume'])
        except (KeyError, TypeError, ValueError):
            raise LabBlocked('CONTRACT_METADATA_INVALID') from None
        if not (0 <= base_p <= 12 and 0 <= quote_p <= 12) or min_qty <= 0:
            raise LabBlocked('CONTRACT_METADATA_INVALID')
        caps = {}
        for kind, key in (('MARKET', 'maxMarketOrderVolume'), ('LIMIT', 'maxLimitOrderVolume')):
            try:
                v = Decimal(str(c.get(key)))
                if v.is_finite() and v > 0:
                    caps[kind] = v
            except (InvalidOperation, ValueError, TypeError):
                pass
        return {'step': Decimal(1).scaleb(-base_p), 'tick': Decimal(1).scaleb(-quote_p), 'min_qty': min_qty,
                'max_qty': caps}

    def _account_guard(self, snap, symbol, exclude=None):
        """Capital/exposure/slots + no unprotected foreign exposure."""
        cfg = self.config
        positions = snap.get('positions') or []
        orders = [o for o in (snap.get('pending_orders') or []) if not o.get('reduceOnly')]
        if any(p.get('symbol') == symbol for p in positions) or any(o.get('symbol') == symbol for o in orders):
            raise LabBlocked('SYMBOL_ALREADY_EXPOSED')
        for sym in sorted({p.get('symbol') for p in positions if isinstance(p.get('symbol'), str)}):
            g = guard_snapshot(snap, sym, now=snap.get('checked_at'))
            if g.get('state') != 'STOP_COVERAGE_OBSERVED_AUDIT_ONLY':
                raise LabBlocked('UNPROTECTED_EXPOSURE_' + sym)
        approved = [r for r in self.store.all(1000) if r['state'] == 'APPROVED' and r['client_id'] != exclude]
        slots_used = len(positions) + len(orders) + len(approved)
        if slots_used >= cfg.max_positions:
            raise LabBlocked('MAX_POSITIONS_REACHED')
        exposure = Decimal(0)
        for p in positions:
            try:
                exposure += D(p.get('qty')) * D(p.get('avgOpenPrice'))
            except LabBlocked:
                raise LabBlocked('EXPOSURE_UNVERIFIED') from None
        reserved = sum((D(json.loads(r['plan'])['required_usdt']) for r in approved), Decimal(0))
        available = D(snap['account']['available']) - reserved
        return available, exposure

    # ---------------------------------------------------------------- check
    def check(self, symbol):
        """GET-only preflight used before the first LIVE trade. Persists nothing, sends nothing."""
        cfg = self.config
        out = {'build': BUILD, 'symbol': symbol, 'orders_sent': 0, 'steps': {}}
        def step(name, fn):
            try:
                out['steps'][name] = {'ok': True, **fn()}
            except Exception as exc:
                out['steps'][name] = {'ok': False, 'error': str(exc)[:80] if isinstance(exc, (LabBlocked, ConfigurationError, BitunixContractError)) else type(exc).__name__}
        ctx = {}
        def snap():
            ctx['snap'] = self._snapshot()
            a = ctx['snap']['account']
            return {'available_usdt': a['available'], 'positions': len(ctx['snap']['positions']),
                    'pending_orders': len(ctx['snap']['pending_orders']), 'pending_tpsl': len(ctx['snap']['pending_tpsl'])}
        def contract():
            ctx['spec'] = self._contract(symbol)
            return {k: ({kk: canon(vv) for kk, vv in v.items()} if isinstance(v, dict) else canon(v))
                    for k, v in ctx['spec'].items()}
        def leverage():
            limits = PublicContractLimits(self.reader, margin_source=lambda: cfg.margin,
                                          leverage_source=lambda: cfg.leverage)(symbol)
            ctx['lev'] = effective_leverage(cfg.leverage, limits)
            return {'requested': cfg.leverage, 'effective': ctx['lev'], 'contract_max': limits['max_leverage'],
                    'tier_max': limits.get('tier_max_leverage')}
        def mode():
            m = self.reader.get('leverage_margin_mode', {'symbol': symbol, 'marginCoin': 'USDT'})
            if not isinstance(m, dict):
                raise LabBlocked('MARGIN_MODE_UNVERIFIED')
            ok = m.get('marginMode') == 'ISOLATION' or not cfg.require_isolation
            if not ok:
                raise LabBlocked('MARGIN_MODE_NOT_ISOLATION')
            return {'account_leverage': m.get('leverage'), 'margin_mode': m.get('marginMode')}
        def price():
            ctx['price'] = self._price(symbol)
            return {'last': canon(ctx['price'])}
        def sizing():
            spec, lev, px = ctx['spec'], ctx['lev'], ctx['price']
            qty = (cfg.margin * lev / px / spec['step']).to_integral_value(rounding=ROUND_DOWN) * spec['step']
            if qty < spec['min_qty']:
                raise LabBlocked('QTY_BELOW_CONTRACT_MINIMUM')
            notional = qty * px
            fees = notional * cfg.fee_rate * 2
            tps = split_units(qty, spec['step'], spec['min_qty'], 5)
            return {'qty': canon(qty), 'notional_usdt': canon(notional), 'margin_usdt': canon(notional / lev),
                    'fee_reserve_usdt': canon(fees), 'required_usdt': canon(notional / lev + fees),
                    'max_tps_with_valid_qty': len(tps), 'tp_qtys_if_5': tps}
        def guard():
            available, exposure = self._account_guard(ctx['snap'], symbol)
            return {'available_after_reservations': canon(available), 'exposure_usdt': canon(exposure),
                    'blocking_records': [r['client_id'] for r in self.store.blocking()]}
        for name, fn in (('snapshot', snap), ('contract', contract), ('leverage', leverage),
                         ('margin_mode', mode), ('ticker', price)):
            step(name, fn)
        if all(out['steps'][k]['ok'] for k in ('contract', 'leverage', 'ticker')):
            step('sizing', sizing)
        if out['steps']['snapshot']['ok']:
            step('account_guard', guard)
        out['ready_for_plan'] = all(v['ok'] for v in out['steps'].values())
        return out

    # ----------------------------------------------------------------- plan
    def plan(self, signal, *, signal_id, entry_type=None, leverage=None, tp_mode=None, max_loss=None):
        cfg = self.config
        if hasattr(cfg, 'reload_capital'):
            cfg.reload_capital()          # F65: always plan with the limits saved right now
        # F66: one-off loss limit, ONLY for an operator-confirmed manual open (never the automatic mode).
        loss_limit = cfg.max_loss if max_loss is None else D(max_loss)
        if not D('0') < loss_limit <= D('1000'):
            raise LabBlocked('INVALID_MAX_LOSS')
        requested_lev = int(leverage if leverage is not None else cfg.leverage)
        if not 3 <= requested_lev <= 200:
            raise LabBlocked('INVALID_LEVERAGE')
        mode_tp = (tp_mode or cfg.tp_mode).upper()
        if mode_tp not in ('SPLIT', 'LAST', 'PARTIAL', 'FIRST'):
            raise LabBlocked('INVALID_TP_MODE')
        if not isinstance(signal_id, str) or not 1 <= len(signal_id) <= 80:
            raise LabBlocked('INVALID_SIGNAL_ID')
        blockers = self.store.blocking()
        if blockers:
            raise LabBlocked('BLOCKED_BY_UNRECONCILED:' + ','.join(r['client_id'] for r in blockers[:5]))
        existing = self.store.by_signal(signal_id)
        if existing:
            raise LabBlocked('SIGNAL_ALREADY_PLANNED:' + existing['state'])
        symbol, side = self.resolve_symbol(signal.symbol), signal.side
        if self.store.symbol_flag(symbol) == 'TPSL_API_UNSUPPORTED':
            raise LabBlocked('PAIR_TPSL_NOT_SUPPORTED_BY_API')
        entry_type = entry_type or signal.entry_type
        if entry_type not in ('MARKET', 'LIMIT') or side not in ('LONG', 'SHORT'):
            raise LabBlocked('INVALID_SIGNAL')
        if not 1 <= len(signal.tps) <= 5:
            raise LabBlocked('PROTECTION_REQUIRED')
        snap = self._snapshot()
        spec = self._contract(symbol)
        tick, step, min_qty = spec['tick'], spec['step'], spec['min_qty']
        limits = PublicContractLimits(self.reader, margin_source=lambda: cfg.margin,
                                      leverage_source=lambda: requested_lev)(symbol)
        try:
            leverage = effective_leverage(requested_lev, limits)
        except ConfigurationError as exc:
            raise LabBlocked(str(exc)) from None
        mode = self.reader.get('leverage_margin_mode', {'symbol': symbol, 'marginCoin': 'USDT'})
        margin_mode = (mode or {}).get('marginMode') if isinstance(mode, dict) else None
        if margin_mode not in ('ISOLATION', 'CROSS'):
            raise LabBlocked('MARGIN_MODE_UNVERIFIED')
        if cfg.require_isolation and margin_mode != 'ISOLATION':
            raise LabBlocked('MARGIN_MODE_NOT_ISOLATION')
        available, exposure = self._account_guard(snap, symbol)
        market = self._price(symbol)
        long = side == 'LONG'
        if entry_type == 'LIMIT':
            if signal.entry_price is None:
                raise LabBlocked('LIMIT_PRICE_REQUIRED')
            ref = D(quantize(signal.entry_price, tick, ROUND_HALF_EVEN))
        else:
            ref = market
        # Rounding toward the entry: tighter SL, TPs slightly easier to reach.
        sl = quantize(signal.sl, tick, ROUND_CEILING if long else ROUND_FLOOR)
        tps = [quantize(t, tick, ROUND_FLOOR if long else ROUND_CEILING) for t in signal.tps]
        if len(set(tps)) != len(tps):
            raise LabBlocked('TP_LEVELS_COLLAPSE_AFTER_ROUNDING')
        if not levels_ok_for(long, sl, ref, tps[0], market, entry_type, cfg.pending_limit):
            raise LabBlocked('LEVELS_INVALID_FOR_CURRENT_PRICE')
        qty = (cfg.margin * leverage / ref / step).to_integral_value(rounding=ROUND_DOWN) * step
        # F74: maximum quantity per order (LIVE 2026-11-25 DRIFTUSDT: 30039 "maximum order quantity: 50000").
        # From the contract when Bitunix publishes it, or learned from a previous 30039 rejection.
        caps = [c for c in (spec.get('max_qty', {}).get(entry_type), self.store.max_qty(symbol)) if c]
        qty_capped_from = None
        if caps and qty > min(caps):
            qty_capped_from = qty
            qty = (min(caps) / step).to_integral_value(rounding=ROUND_DOWN) * step
        if qty < min_qty:
            raise LabBlocked('QTY_BELOW_CONTRACT_MINIMUM')
        notional = qty * ref
        fee_reserve = notional * cfg.fee_rate * 2
        required = notional / leverage + fee_reserve
        if available < required:
            raise LabBlocked('INSUFFICIENT_AVAILABLE_MARGIN')
        if exposure + notional > cfg.max_exposure:
            raise LabBlocked('MAX_EXPOSURE_REACHED')
        loss = qty * abs(ref - D(sl)) + fee_reserve
        if loss > loss_limit:
            raise LabBlocked('SL_LOSS_ABOVE_LIMIT')
        partial_tp, partial_note = None, ''
        if mode_tp == 'PARTIAL':
            long_ = side == 'LONG'
            partial_tp, partial_src = partial_level(long_, ref, tps[0], cfg, tick)
            last = D(tps[-1])
            inside = (ref < D(partial_tp) < last) if long_ else (last < D(partial_tp) < ref)
            probe = {'qty_step': canon(step), 'min_qty': canon(min_qty), 'partial_pct': canon(cfg.partial_pct),
                     'partial_tp': partial_tp, 'tps': list(tps)}
            if not inside or partial_split(probe, qty) is None:
                partial_note = 'PARTIAL_NOT_POSSIBLE:' + ('LAST_TP_TOO_CLOSE' if not inside else 'QTY_TOO_SMALL')
                mode_tp, partial_tp = 'LAST', None
        if mode_tp == 'FIRST':
            # F76: 100 % at whatever comes first: the signal TP1 or tp_pct % in favour.
            long_ = side == 'LONG'
            partial_tp, partial_src = partial_level(long_, ref, tps[0], cfg, tick)
            last = D(tps[-1])
            if not ((ref < D(partial_tp) <= last) if long_ else (last <= D(partial_tp) < ref)):
                partial_note = 'FIRST_NOT_POSSIBLE:LAST_TP_TOO_CLOSE'
                mode_tp, partial_tp = 'LAST', None
        if mode_tp in ('LAST', 'FIRST'):
            tp_qtys = [canon(qty)]
        elif mode_tp == 'PARTIAL':
            tp_qtys = partial_split(probe, qty)[1]
        else:
            tp_qtys = split_units(qty, step, min_qty, len(tps))
            tps = tps[:len(tp_qtys)]
        intent = Intent(signal_id, symbol, side, canon(qty), canon(ref), sl, tuple(tps))
        intent.validate()
        payload = entry_contract(intent, order_type=entry_type)
        plan = {'build': BUILD, 'signal_id': signal_id, 'client_id': intent.client_id(),
                'fingerprint': intent.fingerprint(), 'symbol': symbol, 'side': side,
                'entry_type': entry_type, 'qty': intent.quantity, 'entry': intent.entry,
                'market_at_plan': canon(market), 'sl': sl, 'tps': list(tps), 'tp_qtys': tp_qtys,
                'leverage': leverage, 'leverage_requested': requested_lev, 'margin_mode': margin_mode,
                'margin_usdt': canon(notional / leverage), 'notional_usdt': canon(notional),
                'fee_reserve_usdt': canon(fee_reserve), 'required_usdt': canon(required),
                'max_loss_usdt': canon(loss), 'available_usdt': canon(available),
                'qty_step': canon(step), 'tick': canon(tick), 'min_qty': canon(min_qty),
                'payload': payload, 'planned_at': int(self.clock()), 'tp_mode': mode_tp,
                'tp_dropped': len(signal.tps) - len(tps)}
        if qty_capped_from is not None:
            plan['qty_capped_from'] = canon(qty_capped_from)
        if partial_tp:
            plan.update({'partial_tp': partial_tp, 'partial_at': partial_src,
                         'partial_pct': '100' if mode_tp == 'FIRST' else canon(cfg.partial_pct)})
        if partial_note:
            plan['partial_note'] = partial_note
        existing = self.store.get(intent.client_id())
        if existing:
            if existing['fingerprint'] != intent.fingerprint():
                raise LabBlocked('CLIENT_ID_INTENT_CONFLICT')
            raise LabBlocked('SIGNAL_ALREADY_PLANNED:' + existing['state'])
        if self.store.by_signal(signal_id):
            raise LabBlocked('SIGNAL_ALREADY_PLANNED')
        self.store.insert({'client_id': intent.client_id(), 'signal_id': signal_id, 'symbol': symbol,
                           'side': side, 'entry_type': entry_type, 'fingerprint': intent.fingerprint(),
                           'plan': plan})
        return plan

    # -------------------------------------------------------------- approve
    def approve(self, client_id):
        row = self.store.get(client_id)
        if not row or row['state'] != 'PLANNED':
            raise LabBlocked('PLAN_NOT_AVAILABLE')
        plan = json.loads(row['plan'])
        if self.clock() - plan['planned_at'] > self.config.plan_ttl:
            self.store.transition(client_id, ('PLANNED',), 'PLAN_EXPIRED')
            raise LabBlocked('PLAN_EXPIRED')
        intent = _intent_from_plan(plan)
        review = {'state': 'PRETRADE_PREVIEW_ONLY', 'client_id': intent.client_id(),
                  'fingerprint': intent.fingerprint(), 'snapshot_get_only_verified': True,
                  'sl_required': 'slPrice' in plan['payload'], 'live_enabled': False,
                  'write_transport_enabled': False, 'ready_for_live': False}
        token = self.approvals.approve_offline(intent, review=review, ttl_seconds=self.config.approval_ttl,
                                               now=int(self.clock()))
        self.store.transition(client_id, ('PLANNED',), 'APPROVED')
        return token

    # -------------------------------------------------------------- execute
    def execute(self, client_id, token):
        with self._leased(client_id):
            return self._execute(client_id, token)

    def _execute(self, client_id, token):
        if self.trader is None:
            raise LabBlocked('TRADE_CLIENT_REQUIRED')
        row = self.store.get(client_id)
        if not row or row['state'] != 'APPROVED':
            raise LabBlocked('NOT_APPROVED')
        plan = json.loads(row['plan'])
        intent = _intent_from_plan(plan)
        if intent.fingerprint() != row['fingerprint'] or plan['payload'] != entry_contract(intent, order_type=plan['entry_type']):
            raise LabBlocked('PLAN_TAMPERED')
        # Fresh preflight BEFORE burning the approval.
        blockers = self.store.blocking(exclude=client_id)
        if blockers:
            raise LabBlocked('BLOCKED_BY_UNRECONCILED')
        snap = self._snapshot()
        available, exposure = self._account_guard(snap, plan['symbol'], exclude=client_id)
        if available < D(plan['required_usdt']):
            raise LabBlocked('INSUFFICIENT_AVAILABLE_MARGIN')
        if exposure + D(plan['notional_usdt']) > self.config.max_exposure:
            raise LabBlocked('MAX_EXPOSURE_REACHED')
        market = self._price(plan['symbol'])
        long = plan['side'] == 'LONG'
        if not levels_ok_for(long, plan['sl'], plan['entry'], plan['tps'][0], market, plan['entry_type'],
                             self.config.pending_limit):
            raise LabBlocked('LEVELS_INVALID_FOR_CURRENT_PRICE')
        if plan['entry_type'] == 'MARKET':
            drift = abs(market - D(plan['entry'])) / D(plan['entry']) * 100
            if drift > self.config.max_slippage_pct:
                raise LabBlocked('PRICE_MOVED_SINCE_PLAN')
        receipt = self.approvals.consume_offline(intent, token, now=int(self.clock()))
        if receipt['state'] != 'CONSUMED_OFFLINE_NO_SEND':
            raise LabBlocked('APPROVAL_' + receipt['state'])
        # Leverage: idempotent account setting, verified by GET before any entry.
        lev = plan['leverage']
        current = self.reader.get('leverage_margin_mode', {'symbol': plan['symbol'], 'marginCoin': 'USDT'})
        if not isinstance(current, dict) or int(current.get('leverage', -1)) != lev:
            out = self.trader.post('change_leverage', {'symbol': plan['symbol'], 'leverage': lev, 'marginCoin': 'USDT'})
            self.store.event(client_id, 'LEVERAGE_POST', out.public())
            current = self.reader.get('leverage_margin_mode', {'symbol': plan['symbol'], 'marginCoin': 'USDT'})
            if not isinstance(current, dict) or int(current.get('leverage', -1)) != lev:
                self.store.transition(client_id, ('APPROVED',), 'ABORTED_BEFORE_ENTRY',
                                      reason='LEVERAGE_NOT_VERIFIED', detail=out.public())
                return self.status(client_id)
        # Durable point of no automatic retry, BEFORE the entry POST.
        self.store.transition(client_id, ('APPROVED',), 'ENTRY_ARMED')
        if self.journal.prepare(intent)['state'] != 'PREPARED':
            self.store.transition(client_id, ('ENTRY_ARMED',), 'REVIEW', reason='JOURNAL_ALREADY_ARMED')
            return self.status(client_id)
        self.journal.arm_uncertain_offline(intent)
        outcome = self.trader.post('place_order', plan['payload'])
        try:
            self.acks.observe(intent, raw=outcome.raw or b'', http_status=outcome.http_status)
        except Exception:
            pass
        detail = outcome.public()
        if outcome.state == 'ACK':
            data = outcome.data if isinstance(outcome.data, dict) else {}
            if data.get('clientId') not in (None, client_id):
                self.store.transition(client_id, ('ENTRY_ARMED',), 'REVIEW', reason='ACK_CLIENT_ID_MISMATCH', detail=detail)
                return self.status(client_id)
            self.store.transition(client_id, ('ENTRY_ARMED',), 'ENTRY_ACK', detail=detail,
                                  entry_order_id=str(data.get('orderId') or ''))
        elif outcome.state == 'REJECTED':
            self.store.transition(client_id, ('ENTRY_ARMED',), 'ENTRY_REJECTED_UNCONFIRMED', detail=detail,
                                  reason='EXCHANGE_REJECTED_' + str(outcome.code if outcome.code is not None else outcome.http_status))
        else:
            self.store.transition(client_id, ('ENTRY_ARMED',), 'ENTRY_UNKNOWN', detail=detail,
                                  reason=outcome.message or 'UNKNOWN')
        self.sleep(1.5)   # let Bitunix index the order (MARKET and LIMIT)
        return self._reconcile(client_id)

    # ------------------------------------------------------------ reconcile
    def backfill_close_attribution(self, limit=10):
        """GET-only: attach close price / PnL to CLOSED trades closed before F58. Once per trade."""
        done = 0
        for row in self.store.all(200):
            if row['state'] != 'CLOSED' or not row['position_id'] or done >= limit:
                continue
            events = self.store.events(row['client_id'], 60)
            if any(e['event'] == 'CLOSE_ATTRIBUTION' for e in events):
                continue
            closed = next((e for e in events if e['event'] == 'CLOSED'), None)
            try:
                detail = json.loads(closed['detail']) if closed else {}
            except ValueError:
                detail = {}
            if detail.get('attribution') == 'HISTORY_GET':
                continue
            info = self._close_attribution(row, json.loads(row['plan']))
            if info.get('attribution') == 'HISTORY_GET':
                self.store.event(row['client_id'], 'CLOSE_ATTRIBUTION', info)
                done += 1
        return done

    def reconcile_all(self):
        out = []
        if hasattr(self.config, 'reload_capital'):
            try:
                self.config.reload_capital()     # F77: BE trigger / TP level from the dashboard preset
            except LabBlocked:
                pass
        try:
            self.backfill_close_attribution()
        except Exception:
            pass
        for row in self.store.all(1000):
            if row['state'] == 'APPROVED':
                plan = json.loads(row['plan'])
                if self.clock() - plan['planned_at'] > self.config.plan_ttl + self.config.approval_ttl:
                    self.store.transition(row['client_id'], ('APPROVED',), 'PLAN_EXPIRED')
                continue
            if row['state'] in TERMINAL | IDLE:
                continue
            try:
                out.append(self.reconcile(row['client_id']))
            except Exception as exc:
                self.store.event(row['client_id'], 'RECONCILE_ERROR', {'error': type(exc).__name__, 'reason': str(exc)[:80]})
        return out

    def reconcile(self, client_id):
        with self._leased(client_id):
            return self._reconcile(client_id)

    def _reconcile(self, client_id):
        row = self.store.get(client_id)
        if not row:
            raise LabBlocked('UNKNOWN_CLIENT_ID')
        state = row['state']
        if state in TERMINAL | IDLE:
            return self.status(client_id)
        plan = json.loads(row['plan'])
        if state in ('ENTRY_ARMED', 'ENTRY_ACK', 'ENTRY_UNKNOWN', 'ENTRY_REJECTED_UNCONFIRMED',
                     'ENTRY_PENDING', 'ENTRY_PARTIAL', 'CANCEL_REQUESTED'):
            self._reconcile_entry(row, plan)
            row = self.store.get(client_id)
            state = row['state']
        if state == 'PROTECTION_REVIEW' and row['position_id'] and self._closed_while_in_review(row, plan):
            return self.status(client_id)
        if state in ('PROTECTING', 'PROTECTION_REVIEW'):
            self._protect(row, plan)
            row = self.store.get(client_id)
            state = row['state']
        if state in ('PROTECTED', 'BE_CONFIRMED', 'BE_UNKNOWN', 'SL_ONLY'):
            self._supervise(row, plan)
        if state in ('EMERGENCY_CLOSE_SENT', 'REVIEW') and row['filled_qty']:
            try:
                snap = self._port(_intent_from_plan(plan, protection=True, filled=row['filled_qty'],
                                                    tp_mode=row.get('tp_mode') or 'SPLIT')).snapshot()
                if snap.get('position') is None:
                    self.store.transition(client_id, (state,), 'CLOSED', reason='POSITION_ABSENT_GET')
            except (TimeoutError, LabBlocked):
                pass
        return self.status(client_id)

    def _reconcile_entry(self, row, plan):
        cid, state = row['client_id'], row['state']
        try:
            detail = self.reader.get('order_detail', {'clientId': cid})
        except BitunixContractError as exc:
            self.store.event(cid, 'ORDER_LOOKUP_NOT_CONFIRMED', {'reason': exc.reason, 'state': state})
            if state == 'ENTRY_ACK' and self.clock() - self._since(cid, 'ENTRY_ACK') > ACK_LOOKUP_GRACE:
                self.store.transition(cid, ('ENTRY_ACK',), 'REVIEW', reason='ORDER_NOT_FOUND_AFTER_ACK')
            elif state == 'ENTRY_REJECTED_UNCONFIRMED':
                self._resolve_definitive_reject(row, plan)
            return
        except Exception as exc:
            self.store.event(cid, 'ORDER_LOOKUP_UNAVAILABLE', {'error': type(exc).__name__})
            return
        expected_side = 'BUY' if plan['side'] == 'LONG' else 'SELL'
        if (not isinstance(detail, dict) or detail.get('clientId') != cid or detail.get('symbol') != plan['symbol']
                or detail.get('side') != expected_side or canon(detail.get('qty')) != plan['qty']
                or not str(detail.get('orderId') or '')):
            self.store.transition(cid, None, 'REVIEW', reason='ORDER_IDENTITY_CONFLICT',
                                  detail={'status': (detail or {}).get('status') if isinstance(detail, dict) else None})
            return
        status = detail.get('status')
        filled = _qty_or_zero(detail.get('tradeQty'))
        if filled is None:
            self.store.event(cid, 'FILL_QTY_UNVERIFIED', {'status': status})
            return
        oid = str(detail['orderId'])
        expired = plan['entry_type'] == 'LIMIT' and self.clock() - row['created_at'] > self.config.limit_ttl
        if status in ('INIT', 'NEW') and filled == 0:
            if expired and state != 'CANCEL_REQUESTED':
                self._cancel(row, plan)
            elif state not in ('CANCEL_REQUESTED', 'ENTRY_PENDING'):
                self.store.transition(cid, None, 'ENTRY_PENDING', entry_order_id=oid)
            return
        if status in ('NEW', 'PART_FILLED') and filled > 0:
            if expired and state != 'CANCEL_REQUESTED':
                self._cancel(row, plan)
            elif state not in ('ENTRY_PARTIAL', 'CANCEL_REQUESTED'):
                self.store.transition(cid, None, 'ENTRY_PARTIAL', entry_order_id=oid, filled_qty=canon(filled))
            return
        if status == 'CANCELED' and filled == 0:
            self.store.transition(cid, None, 'CANCELED', entry_order_id=oid, reason='ORDER_CANCELED_NO_FILL')
            return
        if status in ('FILLED', 'CANCELED') and filled > 0:
            if filled > D(plan['qty']):
                self.store.transition(cid, None, 'REVIEW', reason='FILL_ABOVE_INTENT')
                return
            self.store.transition(cid, None, 'PROTECTING', entry_order_id=oid, filled_qty=canon(filled),
                                  detail={'status': status, 'filled': canon(filled)})
            return
        self.store.event(cid, 'ORDER_STATE_UNHANDLED', {'status': status})

    def _resolve_definitive_reject(self, row, plan):
        """F73: an entry REJECTED with HTTP 200 + a business code (LIVE 2026-11-25 DRIFTUSDT 30039) never
        created an order. Confirmed by GET (Bitunix does not know our clientId and no open order carries
        it) it is resolved at once, so it does not block nor disarm the automatic mode.
        Transport failures (no JSON code, 4xx/5xx) are NOT definitive and keep the old path."""
        cid = row['client_id']
        ev = self.store.last_event(cid, 'ENTRY_REJECTED_UNCONFIRMED')
        try:
            out = json.loads(ev['detail']) if ev else {}
        except ValueError:
            out = {}
        if out.get('http_status') != 200 or out.get('code') in (None, 0, '0'):
            return
        try:
            snap = self._snapshot()
        except Exception:
            return
        if any(str(o.get('clientId') or '') == cid for o in snap.get('pending_orders') or []):
            return
        m = re.search(r'maximum order quantity:\s*([0-9][0-9.]*)', str(out.get('message') or ''))
        if str(out.get('code')) == '30039' and m:
            self.store.set_max_qty(plan['symbol'], m.group(1))      # F74: next plan stays under it
        self.store.transition(cid, ('ENTRY_REJECTED_UNCONFIRMED',), 'REJECTED_RESOLVED',
                              reason='EXCHANGE_REJECTED:%s' % out.get('code'),
                              detail={'code': out.get('code'), 'message': str(out.get('message') or '')[:160]})

    def _cancel(self, row, plan):
        out = self.trader.post('cancel_orders', {'symbol': plan['symbol'], 'orderList': [{'clientId': row['client_id']}]})
        self.store.transition(row['client_id'], None, 'CANCEL_REQUESTED', reason='LIMIT_TTL_EXPIRED', detail=out.public())

    def _close_attribution(self, row, plan):
        """Best effort GET of the closed position (history). Never blocks the CLOSED state."""
        pid = row.get('position_id')
        if not pid:
            return {'attribution': 'NO_POSITION_ID'}
        try:
            data = self.reader.get('history_positions', {'symbol': plan['symbol'], 'positionId': pid, 'skip': 0, 'limit': 10})
            items = data.get('positionList') if isinstance(data, dict) else None
            item = next((x for x in items or [] if isinstance(x, dict) and str(x.get('positionId')) == pid), None)
        except Exception as exc:
            return {'attribution': 'HISTORY_UNAVAILABLE', 'error': type(exc).__name__}
        if not item:
            return {'attribution': 'HISTORY_NOT_FOUND'}
        out = {k: item.get(k) for k in ('entryPrice', 'closePrice', 'maxQty', 'realizedPNL', 'fee', 'funding')}
        try:
            close = D(item.get('closePrice'))
            be = D(row['be_price']) if row.get('be_price') else None
            sl, tp_last = D(plan['sl']), D(plan['tps'][-1])
            if (row.get('tp_mode') or plan.get('tp_mode')) == 'FIRST' and plan.get('partial_tp'):
                tp_last = D(plan['partial_tp'])          # F76: the only TP
            long = plan['side'] == 'LONG'
            partial = self._partial_filled(row, plan)
            if partial is not None:
                # F70: 75 % already closed at the partial TP. Bitunix may report the last close or the
                # average close: both are checked. A partial trade is judged by its money, never a draw.
                pq, rq, ptp = partial
                near = lambda a, b: abs(a - b) / b < D('0.002')
                avg_tp = (pq * ptp + rq * tp_last) / (pq + rq)
                avg_be = (pq * ptp + rq * be) / (pq + rq) if be is not None else None
                if near(close, tp_last) or near(close, avg_tp) or (long and close >= tp_last) or (not long and close <= tp_last):
                    out['exit'] = 'PARTIAL_TP'
                elif be is not None and (near(close, be) or near(close, avg_be)):
                    out['exit'] = 'PARTIAL_BE'
                else:
                    out['exit'] = 'PARTIAL_OTHER'
            elif (long and close >= tp_last) or (not long and close <= tp_last):
                out['exit'] = 'TP_LAST'
            elif be is not None and abs(close - be) / be < D('0.002'):
                out['exit'] = 'BE_STOP'
            elif abs(close - sl) / sl < D('0.003'):
                out['exit'] = 'SL'
            else:
                out['exit'] = 'OTHER'
        except LabBlocked:
            out['exit'] = 'UNKNOWN'
        out['attribution'] = 'HISTORY_GET'
        return out

    def _partial_filled(self, row, plan):
        """(partial qty, rest qty, partial price) if the PARTIAL TP of this trade was filled, else None."""
        if (row.get('tp_mode') or plan.get('tp_mode')) != 'PARTIAL' or not plan.get('partial_tp'):
            return None
        filled = any(e['event'] == 'BE_UNKNOWN' and '"TP1_FILL"' in (e.get('detail') or '')
                     for e in self.store.events(row['client_id'], 200))
        if not filled:
            return None
        split = partial_split(plan, row['filled_qty'] or plan['qty'])
        if split is None:
            return None
        return D(split[1][0]), D(split[1][1]), D(plan['partial_tp'])

    def _tp_mode(self, row, plan):
        """Approved plan mode wins; legacy plans (pre-F57) use the current config.

        The mode is persisted when protection actually starts and never changes mid-trade.
        """
        return row.get('tp_mode') or plan.get('tp_mode') or self.config.tp_mode

    def _fix_tp_mode(self, row, mode):
        if not row.get('tp_mode'):
            self.store.transition(row['client_id'], None, row['state'], event='TP_MODE_FIXED',
                                  detail={'tp_mode': mode}, tp_mode=mode)
            row['tp_mode'] = mode

    def _since(self, cid, event):
        ev = next((e for e in self.store.events(cid, 80) if e['event'] == event), None)
        return ev['at'] if ev else 0

    def _port(self, pintent):
        return BitunixProtectionPort(self.reader, self.trader, pintent, settle_seconds=self.settle_seconds,
                                     sleep=self.sleep)

    def _protect(self, row, plan):
        cid = row['client_id']
        filled = row['filled_qty']
        mode = self._tp_mode(row, plan)
        tps, qtys = protection_split(plan, filled, mode)
        pintent = _intent_from_plan(plan, protection=True, filled=filled, tp_mode=mode)
        port = self._port(pintent)
        try:
            st = self.cycle.state(pintent)
            if st['state'] == 'ABSENT':
                self.cycle.start(pintent, port)
                self.store.event(cid, 'POSITION_OBSERVED', {'qty': filled})
            self._fix_tp_mode(row, mode)
            final = None
            for _ in range(2 * len(qtys) + 4):
                st = self.cycle.state(pintent)['state']
                if st.startswith('UNKNOWN_'):
                    r = self.cycle.reconcile_unknown(pintent, port, tp_quantities=tuple(qtys))
                    if not r['resolved']:
                        final = st
                        break
                    continue
                r = self.cycle.progress(pintent, port, tp_quantities=tuple(qtys))
                self.store.event(cid, 'F54_' + r['state'], {'tp': r.get('tp_number'), 'posts': port.outcomes[-1:]})
                if r['state'] == 'STOP_AND_TPS_OBSERVED_LOCAL' or r['state'].startswith('REVIEW_'):
                    final = r['state']
                    break
                # UNKNOWN_*: next iteration tries exact GET evidence once, never a resend.
            if final == 'STOP_AND_TPS_OBSERVED_LOCAL':
                snap = port.snapshot()
                if port.foreign == 0 and snap.get('stop') and not snap['stop'].get('ambiguous'):
                    self.store.transition(cid, ('PROTECTING', 'PROTECTION_REVIEW'), 'PROTECTED',
                                          position_id=snap['position']['position_id'], reason='',
                                          detail={'stop_kind': port.stop_kind, 'tps': len(qtys)})
                    return
                final = 'FOREIGN_OR_AMBIGUOUS_PROTECTION'
            if final == 'UNKNOWN_TP' and self._api_unsupported(cid, port):
                # Definitive REJECTED (nothing created): the pair has no tpsl via API. The SL attached to
                # the entry is confirmed by GET; keep watching it, the operator sets the TP by hand.
                snap = port.snapshot()
                stop = snap.get('stop')
                if snap.get('position') and stop and not stop.get('ambiguous'):
                    self.store.flag_symbol(plan['symbol'], 'TPSL_API_UNSUPPORTED')
                    self.store.transition(cid, ('PROTECTING', 'PROTECTION_REVIEW'), 'SL_ONLY',
                                          reason='TPSL_API_UNSUPPORTED', position_id=snap['position']['position_id'],
                                          detail={'posts': port.outcomes[-1:]})
                    return
            if row['state'] != 'PROTECTION_REVIEW' or row['reason'] != str(final):
                self.store.transition(cid, ('PROTECTING', 'PROTECTION_REVIEW'), 'PROTECTION_REVIEW',
                                      reason=str(final), detail={'posts': port.outcomes})
        except LabBlocked as exc:
            if str(exc) == 'F54_POSITION_NOT_CONFIRMED' and row['state'] == 'PROTECTING' \
                    and self.cycle.state(pintent)['state'] == 'ABSENT' \
                    and self.clock() - self._since(cid, 'PROTECTING') < POSITION_VISIBILITY_GRACE:
                # Exchange lag: the filled position may take a moment to appear in GET. Retry, no write.
                self.store.event(cid, 'POSITION_NOT_VISIBLE_YET', {})
                return
            if row['state'] != 'PROTECTION_REVIEW' or row['reason'] != str(exc):
                self.store.transition(cid, ('PROTECTING', 'PROTECTION_REVIEW'), 'PROTECTION_REVIEW',
                                      reason=str(exc)[:80], detail={'posts': port.outcomes})
        except TimeoutError as exc:
            self.store.event(cid, 'PROTECTION_GET_UNAVAILABLE', {'reason': str(exc)[:80]})

    def _api_unsupported(self, cid, port):
        posts = list(port.outcomes)
        for e in [self.store.last_event(cid, 'F54_UNKNOWN_TP'), self.store.last_event(cid, 'PROTECTION_REVIEW')]:
            if e:
                try:
                    posts += json.loads(e['detail']).get('posts') or []
                except ValueError:
                    pass
        return any(isinstance(p, dict) and p.get('state') == 'REJECTED' and p.get('code') in API_UNSUPPORTED_CODES
                   for p in posts)

    def _be_target(self, raw, avg, qty, long, tick):
        """F63 BE price that closes in PROFIT, not at zero.

        Covers, per unit of the open quantity:
          * what Bitunix already charged on this position (realizedPNL < 0: open fee + funding),
            or the estimated open fee when Bitunix does not report it;
          * the estimated close fee (fee_rate on the BE price);
          * be_profit_pct of extra profit.
        Rounded AWAY from the entry so the cover is never lost to the tick."""
        fee = self.config.fee_rate
        paid, source = None, 'ESTIMATED'
        try:
            realized = Decimal(str(raw.get('realizedPNL')))
            if realized.is_finite():
                paid, source = max(Decimal(0), -realized), 'BITUNIX_REALIZED'
        except (InvalidOperation, ValueError, TypeError):
            pass
        if paid is None:
            paid = avg * qty * fee
        per_unit = paid / qty + avg * fee + avg * self.config.be_profit_pct / 100
        raw_be = avg + per_unit if long else avg - per_unit
        be = D(quantize(raw_be, tick, ROUND_CEILING if long else ROUND_FLOOR))
        return be, {'paid_usdt': canon(paid), 'paid_source': source, 'close_fee_rate': canon(fee),
                    'profit_pct': canon(self.config.be_profit_pct), 'stop_type': self.config.be_stop_type,
                    'expected_net_usdt': canon((abs(be - avg) - avg * fee) * qty - paid)}

    def _stop_ok_marked(self, cid):
        a, b = self.store.last_event(cid, 'STOP_MISSING_SEEN'), self.store.last_event(cid, 'STOP_SEEN_OK')
        return b is not None and a is not None and b['id'] > a['id']

    def _closed_while_in_review(self, row, plan):
        """F75: a reviewed trade whose position is gone on Bitunix is CLOSED (GET-confirmed), with its result."""
        try:
            snap = self._port(_intent_from_plan(plan, protection=True, filled=row['filled_qty'] or plan['qty'],
                                                tp_mode=row.get('tp_mode') or 'SPLIT')).snapshot()
        except (TimeoutError, LabBlocked):
            return False
        if snap.get('position') is not None:
            return False
        self.store.transition(row['client_id'], ('PROTECTION_REVIEW',), 'CLOSED', reason='POSITION_ABSENT_GET',
                              detail=self._close_attribution(row, plan))
        return True

    def _supervise(self, row, plan):
        cid = row['client_id']
        mode = self._tp_mode(row, plan)
        pintent = _intent_from_plan(plan, protection=True, filled=row['filled_qty'], tp_mode=mode)
        port = self._port(pintent)
        try:
            snap = port.snapshot()
        except (TimeoutError, LabBlocked) as exc:
            self.store.event(cid, 'SUPERVISE_GET_UNAVAILABLE', {'reason': str(exc)[:80]})
            return
        pos = snap.get('position')
        if pos is None:
            self.store.transition(cid, None, 'CLOSED', reason='POSITION_ABSENT_GET',
                                  detail=self._close_attribution(row, plan))
            return
        if pos['position_id'] != row['position_id']:
            self.store.transition(cid, None, 'REVIEW', reason='POSITION_ID_CHANGED')
            return
        stop = snap.get('stop')
        if not stop or stop.get('ambiguous'):
            # F75: while the SL itself is executing, Bitunix removes the stop a moment before the position
            # (LIVE 2026-11-27 AVAXUSDT: closed in profit at BE, flagged "a revisar", auto disarmed).
            # Confirm on the next cycle; nothing could be done automatically in between anyway.
            last = self.store.last_event(cid, 'STOP_MISSING_SEEN')
            last_ok = self.store.last_event(cid, 'STOP_SEEN_OK')
            if last is None or (last_ok is not None and last_ok['id'] > last['id']):
                self.store.event(cid, 'STOP_MISSING_SEEN', {'qty': pos['qty']})
                return
            self.store.transition(cid, None, 'PROTECTION_REVIEW', reason='STOP_LOST_WITH_OPEN_POSITION')
            return
        if self.store.last_event(cid, 'STOP_MISSING_SEEN') and not self._stop_ok_marked(cid):
            self.store.event(cid, 'STOP_SEEN_OK', {})
        if row['state'] == 'BE_CONFIRMED':
            return
        raw = port.position_raw or {}
        qty_now = D(pos['qty'])
        tp1_hit = qty_now < D(pintent.quantity)
        try:
            margin = D(raw.get('margin'))
            pnl = Decimal(str(raw.get('unrealizedPNL')))
            avg = D(raw.get('avgOpenPrice'))
        except (LabBlocked, InvalidOperation):
            self.store.event(cid, 'BE_DATA_UNAVAILABLE', {})
            return
        roi = pnl / margin * 100
        long = plan['side'] == 'LONG'
        mark = avg + pnl / qty_now if long else avg - pnl / qty_now
        # TP1 of the SIGNAL is watched by the system (it may not exist as an exchange order).
        tp1 = D(plan['tps'][0])
        tp1_touched = mark >= tp1 if long else mark <= tp1
        move_pct = ((mark - avg) / avg * 100) if long else ((avg - mark) / avg * 100)
        channel_req = any(e['event'] == 'CHANNEL_BE_REQUESTED' for e in self.store.events(cid, 80))
        trigger = ((tp1_hit or tp1_touched) and self.config.be_on_tp1) or move_pct >= self.config.be_move_pct \
            or channel_req
        if mode == 'PARTIAL' and len(pintent.take_profits) == 2 and row['state'] != 'SL_ONLY':
            # F67: BE right AFTER the exchange closed the partial (SL then covers only the rest).
            # Safety net: price far beyond the partial level without a fill, or the channel asks for BE.
            trigger = tp1_hit or channel_req or move_pct >= 2 * self.config.be_move_pct
        if not trigger:
            return
        tick = D(plan['tick'])
        be, be_parts = self._be_target(raw, avg, qty_now, long, tick)
        if row['state'] == 'SL_ONLY':
            # F72: Bitunix refuses tpsl writes by API on this pair: tell the operator, once, the exact BE
            # price to set by hand. No write is attempted.
            if not any(e['event'] == 'BE_MANUAL_ALERT' for e in self.store.events(cid, 200)):
                entry_ = D(pintent.entry)
                be = max(be, entry_) if long else min(be, entry_)
                self.store.event(cid, 'BE_MANUAL_ALERT', {'be': canon(be), 'move_pct': canon(move_pct),
                                                          'roi_pct': canon(roi), 'mark_est': canon(mark)})
            return
        entry = D(pintent.entry)
        be = max(be, entry) if long else min(be, entry)
        feasible = mark > be + tick if long else mark < be - tick
        current_sl = D(stop['price'])
        if (long and be <= current_sl) or (not long and be >= current_sl):
            self.store.transition(cid, None, 'BE_CONFIRMED', be_state='ALREADY_BETTER', be_price=canon(current_sl))
            return
        if not feasible:
            self.store.event(cid, 'BE_WAITING_PRICE', {'roi_pct': canon(roi), 'be': canon(be), 'mark_est': canon(mark)})
            return
        be_txt, rem_txt = canon(be), canon(qty_now)
        port.stop_type = self.config.be_stop_type
        if row['state'] == 'BE_UNKNOWN':
            r = self.cycle.reconcile_unknown(pintent, port, tp_quantities=tuple(protection_split(plan, row['filled_qty'], mode)[1]),
                                             be_price=row['be_price'] or be_txt, remaining_qty=rem_txt)
            if r['resolved']:
                self.store.transition(cid, ('BE_UNKNOWN',), 'BE_CONFIRMED', be_state='CONFIRMED_GET')
            return
        self.store.transition(cid, ('PROTECTED',), 'BE_UNKNOWN', be_price=be_txt,
                              detail={'trigger': 'TP1_FILL' if tp1_hit else ('TP1_TOUCH' if tp1_touched else
                                                  ('MOVE_PCT' if move_pct >= self.config.be_move_pct else 'CHANNEL')),
                                      'move_pct': canon(move_pct), 'roi_pct': canon(roi), 'mark_est': canon(mark),
                                      'be': be_parts})
        try:
            r = self.cycle.move_be(pintent, port, price=be_txt, remaining_qty=rem_txt, trigger_verified=True)
        except (LabBlocked, TimeoutError) as exc:
            self.store.event(cid, 'BE_NOT_CONFIRMED', {'reason': str(exc)[:80], 'posts': port.outcomes})
            return
        if r['state'] == 'BE_OBSERVED_LOCAL':
            self.store.transition(cid, ('BE_UNKNOWN',), 'BE_CONFIRMED', be_state='CONFIRMED_GET',
                                  detail={'posts': port.outcomes})
        else:
            self.store.event(cid, 'BE_NOT_CONFIRMED', {'state': r['state'], 'posts': port.outcomes})

    # ------------------------------------------------------------- F66 adopt
    def adopt_manual(self, lookup, seen, tp_mode='LAST'):
        """Take over positions opened BY HAND on Bitunix so they get the same management as bot trades:
        SL (the operator's own, or the channel signal's), TP (own, or the signal's last target),
        and Break Even at TP1 touch / be_move_pct.

        Safety: GET-only discovery; the position must be stable for adopt_grace seconds; never touches a
        position that has several SL/TP orders, partial ones, or whose SL is already at/after the entry;
        never invents a SL without a signal; the usual F54 cycle (GET-confirmed, no resend) protects it.
        `lookup(symbol, side)` -> (signal_id, Signal) | None.  `seen` is the caller's {positionId: first_ts}.
        Returns [(decision, detail)] for NEW decisions only."""
        if not self.config.adopt_manual or self.trader is None:
            return []
        snap = self._snapshot()
        now = self.clock()
        out = []
        rows = self.store.all(1000)
        busy_symbols = {r['symbol'] for r in rows if r['state'] not in TERMINAL | IDLE}
        live_pids = {r['position_id'] for r in rows if r['position_id']}
        current = set()
        for p in snap.get('positions') or []:
            pid, symbol = str(p.get('positionId') or ''), p.get('symbol')
            side = {'BUY': 'LONG', 'SELL': 'SHORT', 'LONG': 'LONG', 'SHORT': 'SHORT'}.get(str(p.get('side') or '').upper())
            if not pid or not isinstance(symbol, str) or side is None:
                continue
            current.add(pid)
            if symbol in busy_symbols or pid in live_pids:
                continue
            first = seen.setdefault(pid, now)
            if now - first < self.config.adopt_grace:
                continue
            # F69: a skipped position is looked at again when its SL/TP orders change (e.g. the operator
            # removes his own TPs so the bot can apply the 75/25). The same reason is reported only once.
            sig = '|'.join(sorted('%s:%s:%s:%s' % (o.get('id'), o.get('slPrice'), o.get('tpPrice'),
                                                   o.get('slQty') or o.get('tpQty'))
                                  for o in snap.get('pending_tpsl') or [] if str(o.get('positionId') or '') == pid))
            if seen.get(pid + '#done') or seen.get(pid + '#sig') == sig:
                continue
            try:
                decision, detail = self._adopt_one(p, pid, symbol, side, snap, lookup, tp_mode)
            except (LabBlocked, TimeoutError) as exc:
                decision, detail = 'ADOPT_SKIPPED', {'reason': str(exc)[:80]}
            detail.update({'symbol': symbol, 'side': side, 'position_id': pid})
            if decision == 'ADOPTED':
                seen[pid + '#done'] = 1
            else:
                seen[pid + '#sig'] = sig
                if seen.get(pid + '#reason') == detail.get('reason'):
                    continue
                seen[pid + '#reason'] = detail.get('reason')
            out.append((decision, detail))
        for k in [k for k in seen if k.split('#')[0] not in current]:
            seen.pop(k, None)              # position gone: forget it
        return out

    def _adopt_one(self, p, pid, symbol, side, snap, lookup, tp_mode='LAST'):
        long = side == 'LONG'
        qty, avg = D(p.get('qty')), D(p.get('avgOpenPrice'))
        orders = [o for o in snap.get('pending_tpsl') or [] if str(o.get('positionId') or '') == pid]
        stops = [o for o in orders if o.get('slPrice') not in (None, '', '0')]
        tps = [o for o in orders if o.get('tpPrice') not in (None, '', '0')]
        if len(stops) > 1 or len(tps) > 1 or any(o in stops for o in tps):
            return 'ADOPT_SKIPPED', {'reason': 'MULTIPLE_PROTECTION_ORDERS'}
        for o, key in ((stops[0] if stops else None, 'slQty'), (tps[0] if tps else None, 'tpQty')):
            if o is not None and o.get(key) not in (None, '', '0') and D(o.get(key)) != qty:
                return 'ADOPT_SKIPPED', {'reason': 'PARTIAL_PROTECTION_ORDER'}
        spec = self._contract(symbol)
        tick, step, min_qty = spec['tick'], spec['step'], spec['min_qty']
        found = lookup(symbol, side)
        sig_id, sig = found if found else (None, None)
        if stops:
            sl = canon(D(stops[0]['slPrice']))
        elif sig is not None:
            sl = quantize(sig.sl, tick, ROUND_CEILING if long else ROUND_FLOOR)
        else:
            return 'ADOPT_SKIPPED', {'reason': 'NO_SL_NO_SIGNAL'}
        if sig is not None:
            levels = [quantize(t, tick, ROUND_FLOOR if long else ROUND_CEILING) for t in sig.tps]
            if tps:
                levels[-1] = canon(D(tps[0]['tpPrice']))
        elif tps:
            levels = [canon(D(tps[0]['tpPrice']))]
        else:
            return 'ADOPT_SKIPPED', {'reason': 'NO_TP_NO_SIGNAL'}
        market = self._price(symbol)
        intent = Intent('adopt:' + pid, symbol, side, canon(qty), canon(avg), sl, tuple(levels))
        try:
            intent.validate()
        except LabBlocked:
            return 'ADOPT_SKIPPED', {'reason': 'LEVELS_INVALID_FOR_ENTRY'}
        if (long and not D(sl) < market) or (not long and not D(sl) > market):
            return 'ADOPT_SKIPPED', {'reason': 'SL_ALREADY_CROSSED'}
        lev = int(D(p.get('leverage') or 0) or self.config.leverage)
        loss = qty * abs(avg - D(sl))
        # F68: PARTIAL for manual positions too, when the operator did not set his own TP.
        mode, extra, tp_qtys = 'LAST', {}, [canon(qty)]
        if tp_mode == 'FIRST' and not tps and sig is not None:
            ptp, psrc = partial_level(long, avg, levels[0], self.config, tick)
            if (market < D(ptp) <= D(levels[-1])) if long else (D(levels[-1]) <= D(ptp) < market):
                mode = 'FIRST'
                extra = {'partial_tp': ptp, 'partial_pct': '100', 'partial_at': psrc}
        if tp_mode == 'PARTIAL' and not tps and sig is not None:
            ptp, psrc = partial_level(long, avg, levels[0], self.config, tick)
            ahead = (market < D(ptp) < D(levels[-1])) if long else (D(levels[-1]) < D(ptp) < market)
            probe = {'qty_step': canon(step), 'min_qty': canon(min_qty), 'partial_pct': canon(self.config.partial_pct),
                     'partial_tp': ptp, 'tps': list(levels)}
            split = partial_split(probe, qty) if ahead else None
            if split is not None:
                mode, tp_qtys = 'PARTIAL', split[1]
                extra = {'partial_tp': ptp, 'partial_pct': canon(self.config.partial_pct), 'partial_at': psrc}
        plan = {'build': BUILD, 'signal_id': intent.signal_id, 'client_id': intent.client_id(),
                'fingerprint': intent.fingerprint(), 'symbol': symbol, 'side': side, 'entry_type': 'MARKET',
                'qty': intent.quantity, 'entry': intent.entry, 'market_at_plan': canon(market), 'sl': sl,
                'tps': list(levels), 'tp_qtys': tp_qtys, 'leverage': lev, 'leverage_requested': lev,
                'margin_mode': p.get('marginMode') or '', 'margin_usdt': canon(D(p.get('margin') or 0)),
                'notional_usdt': canon(qty * avg), 'fee_reserve_usdt': '0', 'required_usdt': '0',
                'max_loss_usdt': canon(loss), 'available_usdt': '0', 'qty_step': canon(step),
                'tick': canon(tick), 'min_qty': canon(min_qty), 'payload': {}, 'planned_at': int(self.clock()),
                'tp_mode': mode, 'tp_dropped': 0, 'origin': 'ADOPTED', 'adopted_signal': sig_id,
                'adopted_own_sl': bool(stops), 'adopted_own_tp': bool(tps), **extra}
        cid = intent.client_id()
        if self.store.get(cid):
            return 'ADOPT_SKIPPED', {'reason': 'ALREADY_ADOPTED'}
        self.store.insert({'client_id': cid, 'signal_id': intent.signal_id, 'symbol': symbol, 'side': side,
                           'entry_type': 'MARKET', 'fingerprint': intent.fingerprint(), 'plan': plan})
        self.store.transition(cid, ('PLANNED',), 'PROTECTING', event='ADOPTED', filled_qty=canon(qty),
                              tp_mode=mode, detail={'signal': sig_id, 'own_sl': bool(stops), 'own_tp': bool(tps)})
        st = self.reconcile(cid)
        return 'ADOPTED', {'client_id': cid, 'signal': sig_id, 'sl': sl,
                           'tp': extra.get('partial_tp') if mode == 'FIRST' else levels[-1], 'tp_mode': mode,
                           'partial_tp': extra.get('partial_tp', ''),
                           'own_sl': bool(stops), 'own_tp': bool(tps), 'state': st.get('state')}

    def request_channel_be(self, client_id):
        """Channel said 'TP hit / move SL to entry': ask the supervisor for BE now (protective only)."""
        row = self.store.get(client_id)
        if not row or row['state'] not in ('PROTECTED', 'BE_CONFIRMED', 'BE_UNKNOWN'):
            raise LabBlocked('NO_PROTECTED_POSITION')
        if not any(e['event'] == 'CHANNEL_BE_REQUESTED' for e in self.store.events(client_id, 80)):
            self.store.event(client_id, 'CHANNEL_BE_REQUESTED', {})
        return self.reconcile(client_id)

    # ------------------------------------------------------- operator tools
    def resolve_no_exposure(self, client_id):
        """Close a blocking record ONLY if GET proves no order fill and no exposure."""
        row = self.store.get(client_id)
        if not row or row['state'] in TERMINAL | IDLE | SAFE_ACTIVE:
            raise LabBlocked('NOTHING_TO_RESOLVE')
        plan = json.loads(row['plan'])
        snap = self._snapshot()
        sym = plan['symbol']
        if any(p.get('symbol') == sym for p in snap.get('positions') or []) or \
           any(o.get('symbol') == sym for o in snap.get('pending_orders') or []) or \
           any(t.get('symbol') == sym for t in snap.get('pending_tpsl') or []):
            raise LabBlocked('EXPOSURE_STILL_PRESENT')
        try:
            detail = self.reader.get('order_detail', {'clientId': client_id})
        except BitunixContractError:
            detail = None  # order never registered; snapshot proves no exposure
        if isinstance(detail, dict):
            if detail.get('status') != 'CANCELED':
                raise LabBlocked('ORDER_NOT_TERMINAL')
            filled = _qty_or_zero(detail.get('tradeQty'))
            if filled is None:
                raise LabBlocked('FILL_QTY_UNVERIFIED')
            state = 'CLOSED' if filled > 0 else 'REJECTED_RESOLVED'
        else:
            state = 'REJECTED_RESOLVED'
        self.store.transition(client_id, None, state, reason='OPERATOR_RESOLVED_GET_NO_EXPOSURE')
        return self.status(client_id)

    def emergency_close(self, client_id, confirm):
        with self._leased(client_id):
            return self._emergency_close(client_id, confirm)

    def _emergency_close(self, client_id, confirm):
        """Documented contingency: flash-close the position of THIS trade. One shot."""
        row = self.store.get(client_id)
        if not row or confirm != 'CLOSE-' + row['symbol']:
            raise LabBlocked('CONFIRMATION_REQUIRED')
        if row['state'] in TERMINAL or row['state'] == 'EMERGENCY_CLOSE_SENT':
            raise LabBlocked('NOT_CLOSABLE')
        plan = json.loads(row['plan'])
        filled = row['filled_qty'] or plan['qty']
        port = self._port(_intent_from_plan(plan, protection=True, filled=filled, tp_mode=row.get('tp_mode') or 'SPLIT'))
        snap = port.snapshot()
        if not snap.get('position'):
            raise LabBlocked('NO_POSITION_FOUND')
        pid = snap['position']['position_id']
        self.store.transition(client_id, None, 'EMERGENCY_CLOSE_SENT', position_id=pid)
        out = self.trader.post('flash_close', {'positionId': pid})
        self.store.event(client_id, 'EMERGENCY_CLOSE_OUTCOME', out.public())
        return {**self.status(client_id), 'close_outcome': out.state}

    def status(self, client_id=None):
        if client_id:
            row = self.store.get(client_id)
            if not row:
                return {'state': 'ABSENT'}
            plan = json.loads(row['plan'])
            return {'client_id': client_id, 'signal_id': row['signal_id'], 'symbol': row['symbol'],
                    'side': row['side'], 'entry_type': row['entry_type'], 'state': row['state'],
                    'reason': row['reason'], 'qty': plan['qty'], 'filled_qty': row['filled_qty'],
                    'entry': plan['entry'], 'sl': plan['sl'], 'tps': plan['tps'],
                    'tp_qtys': ([row['filled_qty'] or plan['qty']] if (row.get('tp_mode') or plan.get('tp_mode')) == 'LAST'
                                else plan['tp_qtys']),
                    'leverage': plan['leverage'], 'position_id': row['position_id'],
                    'be_state': row['be_state'], 'be_price': row['be_price'], 'tp_mode': row.get('tp_mode') or plan.get('tp_mode', ''),
                    'partial_tp': plan.get('partial_tp', ''), 'partial_pct': plan.get('partial_pct', ''),
                    'blocking': row['state'] not in TERMINAL | SAFE_ACTIVE | IDLE}
        rows = self.store.all(200)
        return {'build': BUILD, 'trades': [self.status(r['client_id']) for r in rows[:50]],
                'blocking': [r['client_id'] for r in self.store.blocking()]}


def _strategy_view(live):
    try:
        from punk.strategy_presets import current, public
        repo = getattr(live.config, '_repo', None)
        return {'presets': public(), 'current': current(repo) if repo is not None else {}}
    except Exception:
        return {'presets': [], 'current': {}}


def dashboard_payload(live):
    """Read-only view for the dashboard (SQLite only, no network)."""
    if live is None:
        return {'build': BUILD, 'status': 'NO_LIVE_DB', 'trades': [], 'blocking': [], 'summary': {}}
    store = live.store if hasattr(live, 'store') else live
    from datetime import datetime, timezone
    day0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    trades, pnl_today, closed_today, wins, losses, draws = [], Decimal(0), 0, 0, 0, 0
    for i, r in enumerate(store.all(60)):
        plan = json.loads(r['plan'])
        events = store.events(r['client_id'], 40) if i < 25 else []
        close = {}
        later = next((e for e in events if e['event'] == 'CLOSE_ATTRIBUTION'), None)
        for e in events:
            if e['event'] == 'CLOSED':
                try:
                    close = json.loads(e['detail'])
                    if close.get('attribution') != 'HISTORY_GET' and later:
                        close = json.loads(later['detail'])
                except ValueError:
                    close = {}
                if r['updated_at'] >= day0:
                    closed_today += 1
                    try:
                        pnl = Decimal(str(close.get('realizedPNL')))
                        pnl_today += pnl
                        if close.get('exit') == 'BE_STOP':
                            draws += 1      # F63: a Break Even close is a draw, not a loss
                        else:
                            wins += pnl > 0
                            losses += pnl < 0
                    except (InvalidOperation, ValueError, TypeError):
                        pass
                break
        trades.append({'client_id': r['client_id'], 'signal_id': r['signal_id'], 'symbol': r['symbol'],
                       'side': r['side'], 'entry_type': r['entry_type'], 'state': r['state'],
                       'reason': r['reason'], 'qty': plan.get('qty'), 'filled_qty': r['filled_qty'],
                       'entry': plan.get('entry'), 'sl': plan.get('sl'), 'tps': plan.get('tps', []),
                       'tp_mode': r.get('tp_mode') or plan.get('tp_mode', ''),
                       'partial_tp': plan.get('partial_tp', ''), 'partial_pct': plan.get('partial_pct', ''),
                       'leverage': plan.get('leverage'), 'margin_usdt': plan.get('margin_usdt'),
                       'max_loss_usdt': plan.get('max_loss_usdt'), 'be_state': r['be_state'],
                       'be_price': r['be_price'], 'position_id': r['position_id'],
                       'blocking': r['state'] not in TERMINAL | SAFE_ACTIVE | IDLE,
                       'created_at': r['created_at'], 'updated_at': r['updated_at'], 'close': close,
                       'timeline': [{'at': e['at'], 'event': e['event']} for e in reversed(events)]})
    active = [t for t in trades if t['state'] not in TERMINAL | IDLE]
    return {'build': BUILD, 'status': 'OK', 'supervised': hasattr(live, 'reconcile_all'),
            'trades': trades, 'blocking': [r['client_id'] for r in store.blocking()],
            'summary': {'active': len(active), 'closed_today': closed_today, 'pnl_today': str(pnl_today),
                        'wins_today': wins, 'losses_today': losses, 'draws_today': draws},
            'strategy': _strategy_view(live),
            'config': {'be_move_pct': canon(live.config.be_move_pct), 'tp_pct': canon(live.config.tp_pct),
                       'fee_rate': canon(live.config.fee_rate), 'be_profit_pct': canon(live.config.be_profit_pct)}}
