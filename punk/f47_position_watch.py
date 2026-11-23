"""F47 read-only change monitor. Fail closed on any snapshot error.

NOT a trading authorizer: current pending stop visibility cannot establish
exchange-side trigger execution. This module performs no write operations.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from .f46_live_guard import guard_snapshot


def _identity(snapshot, symbol):
    """Reject incomplete or duplicate IDs rather than quietly hiding drift."""
    selected = {}
    for key, field in (('positions', 'positionId'), ('pending_orders', 'orderId'), ('pending_tpsl', 'id')):
        values = snapshot.get(key)
        if not isinstance(values, list):
            raise ValueError('INCOMPLETE_INVENTORY')
        matched = [v for v in values if isinstance(v, dict) and v.get('symbol') == symbol]
        if any(not v.get(field) for v in matched):
            raise ValueError('MISSING_INVENTORY_ID')
        ids = [str(v[field]) for v in matched]
        if len(ids) != len(set(ids)):
            raise ValueError('DUPLICATE_INVENTORY_ID')
        selected[key] = tuple(sorted(ids))
    return selected


def watch_samples(client, symbol, samples=2, *, clock=None, pause=None, interval=5):
    """Take independent GET-only snapshots, detect drift, never authorize LIVE.

    Supports injection of a fake client, clock and pause for functional QA.
    No identifiers, balances or credentials are returned in the report.
    """
    base = {'build':'F47','symbol':symbol,'mode':'GET_ONLY_POSITION_WATCH',
            'ready_for_live':False,'write_enabled':False,'orders_sent':0,
            'allow_retry':False,'protected':False,'complete':False}
    if type(samples) is not int or not 1 <= samples <= 20:
        return {**base,'state':'INVALID_SAMPLE_COUNT','samples_read':0}
    if type(interval) not in (int, float) or not 0 <= interval <= 3600:
        return {**base,'state':'INVALID_INTERVAL','samples_read':0}
    now = clock if clock is not None else time.time
    sleep = pause if pause is not None else time.sleep
    previous = None
    changes = 0
    observed_exposure = False
    coverage_gaps_seen = 0
    for i in range(samples):
        if i:
            sleep(interval)
        try:
            snap = client.snapshot(symbol)
            audit = guard_snapshot(snap, symbol, now=now())
            if not audit.get('complete'):
                return {**base,'state':'SNAPSHOT_UNVERIFIED','guard_state':audit.get('state','GET_ONLY_FAILED'),
                        'samples_read':i + 1,'inventory_changes':changes}
            identities = _identity(snap, symbol)
            if previous is not None and identities != previous:
                changes += 1
            previous = identities
            if audit.get('positions_open', 0):
                observed_exposure = True
                if not audit.get('coverage_observed'):
                    coverage_gaps_seen += 1
            if audit.get('state') == 'ORPHAN_ORDERS_REVIEW':
                coverage_gaps_seen += 1
        except Exception:
            return {**base,'state':'GET_ONLY_FAILED','samples_read':i + 1,'inventory_changes':changes}
    state = ('EXPOSURE_OR_PROTECTION_REVIEW' if coverage_gaps_seen else
             'INVENTORY_CHANGED_AUDIT_ONLY' if changes else
             'STABLE_EXPOSURE_AUDIT_ONLY' if observed_exposure else
             'NO_OPEN_POSITION_AUDITED')
    return {**base,'state':state,'complete':True,'samples_read':samples,
            'inventory_changes':changes,'exposure_observed':observed_exposure,
            'coverage_gaps_seen':coverage_gaps_seen,
            'real_sl_tp_confirmed':False,
            'reason':'READ_ONLY_OBSERVATION_NOT_LIVE_PROTECTION'}
