"""F56 operator commands for the controlled LIVE stage (one operation per approval).

All output is bounded JSON without credentials. Every entry requires:
  1) a fresh plan (GET preflight), 2) a one-shot token (TTL), 3) typed confirmation
     of the exact symbol/side/quantity, 4) a fresh preflight at execution time.
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from punk.core import Signal, parse
from punk.live_write_lab import LabBlocked

LIVE_DB = 'data/punk_live.db'


def _out(tag, payload):
    print(tag, json.dumps(payload, sort_keys=True, default=str), flush=True)


def build_executor(repo, *, with_trader=True):
    from punk.bitunix_readonly import ReadOnlyBitunix
    from punk.bitunix_trade import BitunixTradeClient
    from punk.live_executor import LiveConfig, LiveExecutor
    # QA only: loopback fake exchange. Constructors refuse any non-loopback override.
    base = os.environ.get('PA_BITUNIX_QA_BASE')
    kw = {'base': base} if base else {}
    reader = ReadOnlyBitunix(**kw)
    trader = BitunixTradeClient.from_env(**kw) if with_trader else None
    return LiveExecutor(Path(os.environ.get('PA_LIVE_DB_PATH', LIVE_DB)), reader, trader,
                        config=LiveConfig(repo))


def manual_signal(symbol, side, sl, tps, entry):
    symbol, side = symbol.upper(), side.upper()
    try:
        tp_list = tuple(Decimal(x) for x in tps.split(',') if x.strip())
        stop = Decimal(sl)
        price = None if entry.upper() == 'MARKET' else Decimal(entry)
    except (InvalidOperation, AttributeError):
        raise LabBlocked('INVALID_MANUAL_SIGNAL') from None
    return Signal(symbol, side, 'MARKET' if price is None else 'LIMIT', price, stop, tp_list)


def plan_summary(plan):
    keys = ('client_id', 'signal_id', 'symbol', 'side', 'entry_type', 'entry', 'market_at_plan', 'qty',
            'sl', 'tps', 'tp_qtys', 'tp_dropped', 'leverage', 'leverage_requested', 'margin_mode',
            'margin_usdt', 'notional_usdt', 'fee_reserve_usdt', 'required_usdt', 'max_loss_usdt',
            'available_usdt', 'tp_mode')
    return {k: plan.get(k) for k in keys}


def cmd_plan(executor, signal, signal_id, entry_type=None):
    try:
        plan = executor.plan(signal, signal_id=signal_id, entry_type=entry_type)
    except LabBlocked as exc:
        _out('LIVE_PLAN_REFUSED', {'reason': str(exc), 'orders_sent': 0})
        return None
    _out('LIVE_PLAN', {**plan_summary(plan), 'orders_sent': 0,
                       'next': 'python run.py --live-approve ' + plan['client_id']})
    return plan


def cmd_plan_text(executor, text, signal_id, entry_type=None):
    signal, status, reason = parse(text)
    if status != 'VALIDA':
        _out('LIVE_PLAN_REFUSED', {'reason': 'SIGNAL_' + status, 'detail': reason, 'orders_sent': 0})
        return None
    return cmd_plan(executor, signal, signal_id, entry_type)


def cmd_approve(executor, client_id):
    try:
        token = executor.approve(client_id)
    except LabBlocked as exc:
        _out('LIVE_APPROVE_REFUSED', {'reason': str(exc)})
        return None
    st = executor.status(client_id)
    _out('LIVE_APPROVED', {'client_id': client_id, 'symbol': st['symbol'], 'side': st['side'],
                           'qty': st['qty'], 'sl': st['sl'], 'tps': st['tps'], 'leverage': st['leverage'],
                           'expires_in_seconds': executor.config.approval_ttl, 'orders_sent': 0})
    print('LIVE_TOKEN (un solo uso, no compartir):', token, flush=True)
    print('Ejecutar: python run.py --live-execute', client_id, '--token', token, flush=True)
    return token


def confirmation_text(st):
    return 'EJECUTAR %s %s %s' % (st['symbol'], st['side'], st['qty'])


def cmd_execute(executor, client_id, token, ask=input):
    st = executor.status(client_id)
    if st.get('state') != 'APPROVED':
        _out('LIVE_EXECUTE_REFUSED', {'reason': 'NOT_APPROVED', 'state': st.get('state')})
        return None
    expected = confirmation_text(st)
    print('Operacion REAL en Bitunix:', json.dumps({k: st[k] for k in ('symbol', 'side', 'entry_type', 'qty',
          'entry', 'sl', 'tps', 'tp_qtys', 'leverage')}), flush=True)
    typed = ask('Escribi exactamente "%s" para enviar: ' % expected)
    if typed.strip() != expected:
        _out('LIVE_EXECUTE_REFUSED', {'reason': 'CONFIRMATION_MISMATCH', 'orders_sent': 0})
        return None
    try:
        result = executor.execute(client_id, token)
    except LabBlocked as exc:
        _out('LIVE_EXECUTE_REFUSED', {'reason': str(exc)})
        return None
    _out('LIVE_EXECUTE', result)
    return result


def cmd_reconcile(executor, client_id=None):
    try:
        result = executor.reconcile(client_id) if client_id else executor.reconcile_all()
    except LabBlocked as exc:
        result = {'reason': str(exc)}
    _out('LIVE_RECONCILE', result)
    return result


def cmd_resolve(executor, client_id):
    try:
        result = executor.resolve_no_exposure(client_id)
    except LabBlocked as exc:
        result = {'refused': str(exc)}
    _out('LIVE_RESOLVE', result)
    return result


def cmd_emergency_close(executor, client_id, ask=input):
    st = executor.status(client_id)
    if st.get('state') == 'ABSENT':
        _out('LIVE_EMERGENCY_REFUSED', {'reason': 'UNKNOWN_CLIENT_ID'})
        return None
    typed = ask('Cierre a mercado de %s. Escribi "CLOSE-%s": ' % (st['symbol'], st['symbol']))
    try:
        result = executor.emergency_close(client_id, typed.strip())
    except LabBlocked as exc:
        result = {'refused': str(exc)}
    _out('LIVE_EMERGENCY', result)
    return result


def cmd_permission_check(executor, symbol):
    """Harmless write: set leverage to its CURRENT value. ACK proves trade permission."""
    symbol = symbol.upper()
    snap = executor.reader.snapshot()
    if any(p.get('symbol') == symbol for p in snap.get('positions') or []) or \
       any(o.get('symbol') == symbol for o in snap.get('pending_orders') or []):
        _out('LIVE_PERMISSION_CHECK', {'refused': 'SYMBOL_HAS_EXPOSURE'})
        return None
    current = executor.reader.get('leverage_margin_mode', {'symbol': symbol, 'marginCoin': 'USDT'})
    lev = int(current['leverage'])
    out = executor.trader.post('change_leverage', {'symbol': symbol, 'leverage': lev, 'marginCoin': 'USDT'})
    after = executor.reader.get('leverage_margin_mode', {'symbol': symbol, 'marginCoin': 'USDT'})
    result = {**out.public(), 'leverage_before': lev, 'leverage_after': after.get('leverage'),
              'margin_mode': after.get('marginMode'), 'trade_permission': out.state == 'ACK',
              'orders_sent': 0}
    _out('LIVE_PERMISSION_CHECK', result)
    return result


def supervise_forever(executor, interval=20, state=None):
    from punk.live_auto import AutoStore, adopt_cycle
    auto_store, seen = AutoStore(executor.store), {}
    while True:
        try:
            adopt_cycle(executor, auto_store, seen)          # F66: manual positions on Bitunix
        except Exception as exc:
            if state is not None:
                state['live_adopt'] = {'last_run': int(time.time()), 'error': type(exc).__name__}
        try:
            results = executor.reconcile_all()
            if state is not None:
                state['live_supervisor'] = {'last_run': int(time.time()), 'checked': len(results)}
        except Exception as exc:
            if state is not None:
                state['live_supervisor'] = {'last_run': int(time.time()), 'error': type(exc).__name__}
        time.sleep(interval)


def cmd_arm(auto, args, ask=input):
    """Arm the Telegram->LIVE automatic mode. LIVE requires typed confirmation of the limits."""
    def lim(v):
        return 0 if str(v).upper() in ('AUTO', '0', 'SIN-LIMITE', 'SINLIMITE') else int(v)
    try:
        from punk.live_auto import validate_arm
        args.max_open, args.max_day = lim(args.max_open), lim(args.max_day)
        validate_arm(args.live_arm, args.max_open, args.max_day, args.entry_policy, args.max_dev, args.tp_mode)
    except (LabBlocked, ValueError, InvalidOperation) as exc:
        _out('LIVE_ARM_REFUSED', {'reason': str(exc)})
        return None
    cfg = auto.ex.config
    summary = {'mode': args.live_arm,
               'max_open': args.max_open or 'AUTO (segun saldo real; tope global %s posiciones, exposicion %s USDT)' % (cfg.max_positions, cfg.max_exposure),
               'max_per_day_utc': args.max_day or 'sin limite',
               'entry_policy': args.entry_policy, 'max_entry_dev_pct': args.max_dev, 'tp_mode': args.tp_mode,
               'margin_usdt': str(cfg.margin), 'leverage': 'senal (x20 en el mensaje), limitado por contrato/tier',
               'max_loss_per_sl_usdt': str(cfg.max_loss), 'be': 'TP1 tocado o precio %s%% a favor (= %s%% x apalancamiento de ROI)' % (cfg.be_move_pct, cfg.be_move_pct)}
    print('Armado automatico:', json.dumps(summary), flush=True)
    if args.live_arm == 'LIVE':
        expected = 'ARMAR LIVE %s %s' % (args.max_open or 'AUTO', args.max_day or 'AUTO')
        typed = ask('Escribi exactamente "%s" para armar: ' % expected)
        if typed.strip() != expected:
            _out('LIVE_ARM_REFUSED', {'reason': 'CONFIRMATION_MISMATCH'})
            return None
    try:
        result = auto.arm(args.live_arm, max_open=args.max_open, max_per_day=args.max_day,
                          entry_policy=args.entry_policy, max_entry_dev_pct=args.max_dev, tp_mode=args.tp_mode)
    except LabBlocked as exc:
        _out('LIVE_ARM_REFUSED', {'reason': str(exc)})
        return None
    _out('LIVE_ARMED', result)
    return result


def cmd_open(trader, message_id, max_loss=None, ask=input):
    """F66: open a channel signal by hand THROUGH the system: same checks and entry policy as the
    automatic mode (price still between SL and TP1, margin, exposure, isolation...), optional one-off
    loss limit, typed confirmation, then the normal GET-confirmed protection and BE."""
    from punk.live_auto import manual_open_plan
    try:
        plan = manual_open_plan(trader, message_id, max_loss)
    except LabBlocked as exc:
        _out('LIVE_OPEN_REFUSED', {'reason': str(exc), 'orders_sent': 0})
        return None
    ex = trader.ex
    print('Senal #%s validada por el sistema:' % message_id, json.dumps({
        k: plan[k] for k in ('symbol', 'side', 'entry_type', 'entry', 'market_at_plan', 'qty', 'sl', 'tps',
                             'leverage', 'margin_usdt', 'max_loss_usdt')}, sort_keys=True), flush=True)
    expected = 'ABRIR %s %s %s' % (plan['symbol'], plan['side'], plan['qty'])
    typed = ask('Escribi exactamente "%s" para enviar: ' % expected)
    if typed.strip() != expected:
        ex.store.transition(plan['client_id'], ('PLANNED',), 'PLAN_EXPIRED', reason='CONFIRMATION_MISMATCH')
        _out('LIVE_OPEN_REFUSED', {'reason': 'CONFIRMATION_MISMATCH', 'orders_sent': 0})
        return None
    try:
        token = ex.approve(plan['client_id'])
        result = ex.execute(plan['client_id'], token)
    except LabBlocked as exc:
        _out('LIVE_OPEN_REFUSED', {'reason': str(exc)})
        return None
    trader.auto.log(plan['signal_id'], 'MANUAL_SENT', {k: plan[k] for k in (
        'client_id', 'symbol', 'side', 'entry_type', 'entry', 'qty', 'sl', 'leverage', 'max_loss_usdt')}
        | {'state': result.get('state')})
    _out('LIVE_OPEN', result)
    return result
