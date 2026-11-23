"""F14: fail-closed LIVE readiness audit. No trading endpoints and no side effects.

This report is deliberately NOT an authorization for real trading.
"""
import time
from decimal import Decimal, InvalidOperation
from punk.risk_config import current


def _unsigned_count(value):
    return isinstance(value,list) and all(isinstance(x,dict) for x in value)


def audit(snapshot, settings, *, now=None):
    """Produce a sanitized report from a GET-only snapshot (no account identifiers)."""
    now=time.time() if now is None else now
    checks={}
    checks['GET_ONLY_VERIFIED']=snapshot.get('status')=='GET_ONLY_VERIFIED'
    ts=snapshot.get('checked_at')
    checks['FRESH_ACCOUNT_SNAPSHOT']=(type(ts) in (int,float) and 0<=now-ts<=90)
    positions=snapshot.get('positions')
    orders=snapshot.get('pending_orders')
    tpsl=snapshot.get('pending_tpsl')
    checks['POSITIONS_COMPLETE']=_unsigned_count(positions)
    checks['ORDERS_COMPLETE']=_unsigned_count(orders)
    checks['PROTECTIONS_COMPLETE']=_unsigned_count(tpsl)
    checks['NO_EXISTING_EXPOSURE']=checks['POSITIONS_COMPLETE'] and not positions
    checks['NO_PENDING_ORDERS']=checks['ORDERS_COMPLETE'] and not orders
    checks['NO_PENDING_PROTECTIONS']=checks['PROTECTIONS_COMPLETE'] and not tpsl
    account=snapshot.get('account')
    try:
        value=Decimal(str(account['available']))
        margin=Decimal(str(settings['margin']))
        balance_ok=value.is_finite() and value>=0
        margin_ok=margin.is_finite() and margin>0
    except (KeyError,TypeError,ValueError,InvalidOperation):
        balance_ok=margin_ok=False
        value=margin=Decimal(0)
    checks['AVAILABLE_BALANCE_VERIFIED']=balance_ok
    checks['MARGIN_CONFIGURATION_VALID']=margin_ok
    checks['ONE_TRADE_AFFORDABLE']=balance_ok and margin_ok and value>=margin
    # Deliberately unresolved until write adapter and end-to-end exchange confirmation exist.
    checks['LIVE_WRITE_ADAPTER_VALIDATED']=False
    checks['EXCHANGE_PROTECTION_CONFIRMATION_VALIDATED']=False
    checks['UNKNOWN_ORDER_RECONCILIATION_VALIDATED']=False
    checks['MANUAL_LIVE_AUTHORIZATION']=False
    return {'build':'F14','mode':'GET_ONLY_PREFLIGHT','live_enabled':False,
            'ready_for_live':False,'checks':checks,
            'blocked_reasons':[k for k,v in checks.items() if not v],
            'notice':'READ_ONLY_REPORT_NOT_LIVE_AUTHORIZATION'}


def run_preflight(client, repo, *, now=None):
    # Exactly one read-only snapshot; never print snapshot payloads.
    try:
        snapshot=client.snapshot()
    except Exception:
        snapshot={'status':'ERROR'}
    return audit(snapshot,current(repo),now=now)
