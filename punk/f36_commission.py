"""F36 — real GET-only commissioning gate; no trading write authority.

The old F14 preflight covers account state. This module couples it with a
single-trade configuration and an explicit inventory of missing LIVE controls.
No part of this module imports a write-capable HTTP transport.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from punk.live_readiness import audit as account_audit

REQUIRED_LIVE = (
    'REAL_WRITE_TRANSPORT_VALIDATED',
    'REAL_SL_TP_CONFIRMATION_VALIDATED',
    'REAL_PARTIAL_FILL_RECOVERY_VALIDATED',
    'REAL_BREAK_EVEN_CONFIRMED',
    'ONE_SHOT_MANUAL_AUTHORIZATION',
)


def _amount(value):
    try:
        d = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError):
        return None
    return d if d.is_finite() and d > 0 else None


def commission_check(reader, *, margin_usdt='5', leverage=20, symbol='BTCUSDT', now=None):
    """Sanitized inspection; fail closed if any snapshot or contract check fails.

    No auth token, order payload, or trading credentials are accepted here.
    """
    clock = time.time() if now is None else now
    if not isinstance(symbol, str) or not symbol.isascii() or not symbol.endswith('USDT') or not symbol[:-4].isalnum() or len(symbol) > 32:
        return _fail('INVALID_SYMBOL')
    margin = _amount(margin_usdt)
    if margin is None or type(leverage) is not int or not 3 <= leverage <= 200:
        return _fail('INVALID_SETTINGS')
    try:
        snapshot = reader.snapshot()
        if not isinstance(snapshot, dict):
            return _fail('INVALID_EXCHANGE_SNAPSHOT')
        report = account_audit(snapshot, {'margin': str(margin)}, now=clock)
        contract = reader.public_contract(symbol)
    except Exception:
        return _fail('GET_ONLY_UNAVAILABLE')
    checks = {k: bool(v) for k, v in report['checks'].items() if k not in (
        'LIVE_WRITE_ADAPTER_VALIDATED','EXCHANGE_PROTECTION_CONFIRMATION_VALIDATED',
        'UNKNOWN_ORDER_RECONCILIATION_VALIDATED','MANUAL_LIVE_AUTHORIZATION')}
    checks['CONTRACT_SYMBOL_CONFIRMED'] = isinstance(contract, dict) and contract.get('symbol') == symbol
    checks['CONTRACT_API_OPEN'] = checks['CONTRACT_SYMBOL_CONFIRMED'] and contract.get('symbolStatus') == 'OPEN' and contract.get('isApiSupported') is True
    try:
        lo, hi = int(contract['minLeverage']), int(contract['maxLeverage'])
        checks['LEVERAGE_BOUNDS_VALID'] = 1 <= lo <= hi <= 200 and leverage >= max(3, lo)
        # An excessive request is never automatically applied; read-only only.
        checks['LEVERAGE_WITHIN_CONTRACT'] = checks['LEVERAGE_BOUNDS_VALID'] and leverage <= hi
    except (TypeError, ValueError, KeyError):
        checks['LEVERAGE_BOUNDS_VALID'] = False
        checks['LEVERAGE_WITHIN_CONTRACT'] = False
    try:
        available = Decimal(str(snapshot['account']['available']))
        # Conservative fee buffer: 2 taker legs at 0.1% of notional each.
        estimated = margin + margin * leverage * Decimal('0.002')
        checks['FEE_BUFFER_AFFORDABLE'] = available.is_finite() and available >= estimated
    except (ValueError, InvalidOperation, TypeError, KeyError):
        checks['FEE_BUFFER_AFFORDABLE'] = False
    checks.update({k: False for k in REQUIRED_LIVE})
    return {'build': 'F36', 'mode': 'GET_ONLY_SINGLE_TRADE_GATE',
            'symbol': symbol, 'requested_leverage': leverage,
            'checks': checks,
            'blocked_reasons': sorted(k for k, valid in checks.items() if not valid),
            'write_transport_enabled': False, 'orders_sent': 0,
            'ready_for_live': False, 'live_enabled': False, 'allow_retry': False,
            'notice': 'NO_LIVE_AUTHORIZATION_NO_WRITE_TRANSPORT'}


def _fail(code):
    return {'build': 'F36', 'mode': 'GET_ONLY_SINGLE_TRADE_GATE', 'error': code,
            'blocked_reasons': [code] + list(REQUIRED_LIVE), 'write_transport_enabled': False,
            'orders_sent': 0, 'ready_for_live': False, 'live_enabled': False,
            'allow_retry': False, 'notice': 'NO_LIVE_AUTHORIZATION_NO_WRITE_TRANSPORT'}


def place_order(*args, **kwargs):
    raise PermissionError('F36_LIVE_POST_DISABLED')
