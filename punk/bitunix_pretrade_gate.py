"""F24: verified GET-only pretrade compiler. No POST implementation or LIVE switch.

It consumes a fresh, complete exchange snapshot and contract metadata, validates
an offline order against account/contract/risk constraints, and builds a preview.
The result is never an authorization to place a real order.
"""
from __future__ import annotations

from decimal import Decimal
import time

from punk.bitunix_write_contract import entry_contract, protection_plan
from punk.live_write_lab import Intent, LabBlocked, positive


def _integer(value, code):
    if type(value) is int and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise LabBlocked(code)


def compile_pretrade(intent: Intent, reader, *, margin_usdt='5', requested_leverage=20,
                     tp_weights=None, max_age_seconds=20, now=None,
                     estimated_fee_rate='0.0006'):
    """Consult GET-only APIs, fail closed and return an unsigned, unsent preview.

    `reader` must implement snapshot(), public_contract(symbol), get('position_tiers').
    This never changes leverage, submits a trade, or marks protections confirmed.
    """
    if type(intent) is not Intent:
        raise LabBlocked('INVALID_INTENT_TYPE')
    intent.validate()
    if not all(callable(getattr(reader, name, None)) for name in ('snapshot', 'public_contract', 'get')):
        raise LabBlocked('GET_ONLY_READER_REQUIRED')
    margin = positive(margin_usdt)
    fees = positive(estimated_fee_rate)
    requested = _integer(requested_leverage, 'INVALID_LEVERAGE')
    if not 3 <= requested <= 200 or fees > Decimal('0.01'):
        raise LabBlocked('INVALID_SETTINGS')
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 120:
        raise LabBlocked('INVALID_SNAPSHOT_WINDOW')
    clock = time.time() if now is None else now
    if not isinstance(clock, (int, float)) or isinstance(clock, bool):
        raise LabBlocked('INVALID_CLOCK')

    try:
        snapshot = reader.snapshot()
    except Exception:
        raise LabBlocked('EXCHANGE_SNAPSHOT_UNAVAILABLE') from None
    if not isinstance(snapshot, dict) or snapshot.get('status') != 'GET_ONLY_VERIFIED':
        raise LabBlocked('EXCHANGE_SNAPSHOT_UNVERIFIED')
    updated = snapshot.get('checked_at')
    if type(updated) is not int or not 0 <= clock - updated <= max_age_seconds:
        raise LabBlocked('STALE_EXCHANGE_SNAPSHOT')
    if any(not isinstance(snapshot.get(k), list) for k in ('positions', 'pending_orders', 'pending_tpsl')):
        raise LabBlocked('INCOMPLETE_EXCHANGE_SNAPSHOT')
    if any(snapshot[k] for k in ('positions', 'pending_orders', 'pending_tpsl')):
        raise LabBlocked('EXISTING_EXPOSURE_REQUIRES_RECONCILIATION')
    account = snapshot.get('account')
    if not isinstance(account, dict) or account.get('marginCoin') != 'USDT':
        raise LabBlocked('ACCOUNT_UNVERIFIED')
    try:
        available = positive(account.get('available'))
    except LabBlocked:
        raise LabBlocked('AVAILABLE_BALANCE_UNVERIFIED') from None

    try:
        contract = reader.public_contract(intent.symbol)
    except Exception:
        raise LabBlocked('CONTRACT_UNAVAILABLE') from None
    if not isinstance(contract, dict) or contract.get('symbol') != intent.symbol or \
       contract.get('symbolStatus') != 'OPEN' or contract.get('isApiSupported') is not True:
        raise LabBlocked('CONTRACT_NOT_TRADABLE')
    try:
        minimum_leverage = _integer(contract['minLeverage'], 'INVALID_CONTRACT_LEVERAGE')
        maximum_leverage = _integer(contract['maxLeverage'], 'INVALID_CONTRACT_LEVERAGE')
        precision = _integer(contract['basePrecision'], 'INVALID_CONTRACT_PRECISION')
        minimum_qty = positive(contract['minTradeVolume'])
    except (KeyError, TypeError, LabBlocked):
        raise LabBlocked('CONTRACT_LIMITS_UNVERIFIED') from None
    if not (1 <= minimum_leverage <= maximum_leverage <= 200 and 0 <= precision <= 12):
        raise LabBlocked('CONTRACT_LIMITS_UNVERIFIED')
    quantity = positive(intent.quantity)
    step = Decimal(1).scaleb(-precision)
    if quantity < minimum_qty or quantity % step:
        raise LabBlocked('CONTRACT_QUANTITY_INVALID')

    notional = quantity * positive(intent.entry)
    try:
        tiers = reader.get('position_tiers', {'symbol': intent.symbol})
    except Exception:
        raise LabBlocked('POSITION_TIERS_UNAVAILABLE') from None
    if not isinstance(tiers, list) or not tiers:
        raise LabBlocked('POSITION_TIERS_UNVERIFIED')
    eligible = []
    try:
        for tier in tiers:
            if not isinstance(tier, dict) or tier.get('symbol') != intent.symbol:
                raise ValueError()
            lower = Decimal(str(tier['startValue']))
            upper = Decimal(str(tier['endValue']))
            cap = _integer(tier['leverage'], 'INVALID_TIER_LEVERAGE')
            if not (lower.is_finite() and upper.is_finite() and Decimal('0') <= lower < upper and 1 <= cap <= 200):
                raise ValueError()
            if lower <= notional < upper:
                eligible.append(cap)
    except (ValueError, KeyError, TypeError, LabBlocked):
        raise LabBlocked('POSITION_TIERS_UNVERIFIED') from None
    if len(eligible) != 1:
        raise LabBlocked('POSITION_TIER_NOT_UNIQUE')
    applied_leverage = min(requested, maximum_leverage, eligible[0])
    if applied_leverage < max(3, minimum_leverage):
        raise LabBlocked('LEVERAGE_NOT_SUPPORTED')
    if notional > margin * applied_leverage:
        raise LabBlocked('NOTIONAL_EXCEEDS_CONFIGURED_MARGIN')
    # Fee buffer protects against admitting trades whose margin alone is affordable.
    reserve = margin + notional * fees * 2
    if available < reserve:
        raise LabBlocked('INSUFFICIENT_AVAILABLE_USDT_WITH_FEE_BUFFER')
    if tp_weights is None:
        count = len(intent.take_profits)
        base = Decimal(1) / Decimal(count)
        tp_weights = tuple(str(base) for _ in range(count - 1)) + (str(Decimal(1) - base * (count - 1)),)
    # Validate complete contract before returning a readiness preview.
    entry = entry_contract(intent)
    protections = protection_plan(intent, position_id='UNSENT_PREVIEW',
                                  tp_weights=tp_weights, quantity_step=str(step))
    return {'build': 'F24', 'state': 'PRETRADE_PREVIEW_ONLY',
            'symbol': intent.symbol, 'side': intent.side,
            'requested_leverage': requested, 'effective_leverage': applied_leverage,
            'margin_usdt': str(margin), 'required_reserve_usdt': format(reserve, 'f'),
            'entry_payload': entry, 'tp_payloads': protections,
            'sl_required': bool(entry.get('slPrice')), 'tp_count': len(protections),
            'snapshot_get_only_verified': True, 'request_sent': False,
            'write_transport_enabled': False, 'live_enabled': False,
            'real_protection_confirmed': False, 'ready_for_live': False,
            'allow_retry': False}
