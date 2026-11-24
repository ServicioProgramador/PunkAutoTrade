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

BUILD = 'F65'
# F64: channel tickers that are named differently on Bitunix (LIVE 2026-11-24: #RAYSOL/USDT).
SYMBOL_ALIASES = {'RAYSOLUSDT': 'RAYUSDT'}
TERMINAL = frozenset({'CLOSED', 'CANCELED', 'REJECTED_RESOLVED', 'ABORTED_BEFORE_ENTRY', 'PLAN_EXPIRED',
                      'SHADOW_ONLY'})
# Known, reconciled-or-progressing states: they do NOT block new entries nor disarm the auto mode.
# (LIVE 2026-11-22: a LIMIT entry ACK whose order was not yet visible by GET disarmed the auto mode.)
SAFE_ACTIVE = frozenset({'ENTRY_PENDING', 'PROTECTED', 'BE_CONFIRMED', 'ENTRY_ACK', 'PROTECTING',
                         'BE_UNKNOWN', 'ENTRY_PARTIAL', 'CANCEL_REQUESTED'})
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
        return self

    def __init__(self, repo=None, **over):
        get = (lambda k, d: repo.setting(k, d)) if repo is not None else (lambda k, d: d)
        env = os.environ.get
        self._repo, self._over = repo, dict(over)
        self.reload_capital()
        self.fee_rate = D(over.get('fee_rate', env('PA_LIVE_FEE_RATE', get('live_fee_rate', '0.0006'))))
        # BE trigger = favorable PRICE move from the average entry, in % (user rule:
        # 1% move == 1% x leverage ROI, e.g. 20% ROI at 20x), or TP1 touched.
        self.be_move_pct = D(over.get('be_move_pct', env('PA_LIVE_BE_MOVE_PCT', get('live_be_move_pct', '1'))))
        # F63: extra profit locked by the BE stop, in % of price, ON TOP of the fees already
        # paid (open fee + funding, read from Bitunix) and the estimated close fee.
        self.be_profit_pct = D(over.get('be_profit_pct', env('PA_LIVE_BE_PROFIT_PCT', get('live_be_profit_pct', '0.1'))))
        # F63: the BE stop triggers on LAST price (the initial SL keeps MARK price against wicks).
        # LIVE evidence 2026-11-23: MARK-triggered BE stops filled several ticks below BE on fast dumps.
        self.be_stop_type = str(over.get('be_stop_type', env('PA_LIVE_BE_STOP_TYPE', 'LAST_PRICE'))).upper()
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
        if self.tp_mode not in ('SPLIT', 'LAST'):
            raise LabBlocked('INVALID_TP_MODE')
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


def protection_split(plan, filled, tp_mode='SPLIT'):
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
        return {'step': Decimal(1).scaleb(-base_p), 'tick': Decimal(1).scaleb(-quote_p), 'min_qty': min_qty}

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
            return {k: canon(v) for k, v in ctx['spec'].items()}
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
    def plan(self, signal, *, signal_id, entry_type=None, leverage=None, tp_mode=None):
        cfg = self.config
        if hasattr(cfg, 'reload_capital'):
            cfg.reload_capital()          # F65: always plan with the limits saved right now
        requested_lev = int(leverage if leverage is not None else cfg.leverage)
        if not 3 <= requested_lev <= 200:
            raise LabBlocked('INVALID_LEVERAGE')
        mode_tp = (tp_mode or cfg.tp_mode).upper()
        if mode_tp not in ('SPLIT', 'LAST'):
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
        levels_ok = (D(sl) < ref < D(tps[0]) and D(sl) < market < D(tps[0])) if long else \
                    (D(sl) > ref > D(tps[0]) and D(sl) > market > D(tps[0]))
        if not levels_ok:
            raise LabBlocked('LEVELS_INVALID_FOR_CURRENT_PRICE')
        qty = (cfg.margin * leverage / ref / step).to_integral_value(rounding=ROUND_DOWN) * step
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
        if loss > cfg.max_loss:
            raise LabBlocked('SL_LOSS_ABOVE_LIMIT')
        if mode_tp == 'LAST':
            tp_qtys = [canon(qty)]
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
        if not ((D(plan['sl']) < market < D(plan['tps'][0])) if long else (D(plan['sl']) > market > D(plan['tps'][0]))):
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
        if state in ('PROTECTING', 'PROTECTION_REVIEW'):
            self._protect(row, plan)
            row = self.store.get(client_id)
            state = row['state']
        if state in ('PROTECTED', 'BE_CONFIRMED', 'BE_UNKNOWN'):
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
            long = plan['side'] == 'LONG'
            if (long and close >= tp_last) or (not long and close <= tp_last):
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
            self.store.transition(cid, None, 'PROTECTION_REVIEW', reason='STOP_LOST_WITH_OPEN_POSITION')
            return
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
        if not trigger:
            return
        tick = D(plan['tick'])
        be, be_parts = self._be_target(raw, avg, qty_now, long, tick)
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
                    'blocking': row['state'] not in TERMINAL | SAFE_ACTIVE | IDLE}
        rows = self.store.all(200)
        return {'build': BUILD, 'trades': [self.status(r['client_id']) for r in rows[:50]],
                'blocking': [r['client_id'] for r in self.store.blocking()]}


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
                        'wins_today': wins, 'losses_today': losses, 'draws_today': draws}}
