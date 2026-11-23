"""F19: persistent fail-closed GET-only recovery for externally identified orders.

This module never writes to Bitunix, never authorizes a retry, and does not
represent an exchange position as protected. An isolated SQLite journal stores
only the fingerprint and code of each observation, never credentials/payloads.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from punk.bitunix_protection_audit import audit
from punk.bitunix_reconciliation import ReadOnlyBoundary, _CLIENT, _SYMBOL

REQUIRED = frozenset(('client_id', 'symbol', 'side', 'quantity', 'entry_price', 'sl_price', 'tp_levels'))
OPTIONAL = frozenset(('stop_trigger', 'tp_trigger'))


def _result(code, *, coverage=False, observed=False):
    return {'state': code, 'snapshot_coverage_verified': bool(coverage),
            'order_observed': bool(observed), 'protected': False, 'allow_retry': False,
            'live_enabled': False, 'ready_for_live': False, 'write_transport_enabled': False}


def _canonical(intent):
    if (not isinstance(intent, dict) or not REQUIRED <= intent.keys()
            or not set(intent) <= REQUIRED | OPTIONAL):
        raise ValueError('INVALID_RECOVERY_INTENT')
    if (not isinstance(intent['client_id'], str) or not _CLIENT.fullmatch(intent['client_id'])
            or not isinstance(intent['symbol'], str) or not _SYMBOL.fullmatch(intent['symbol'])):
        raise ValueError('INVALID_RECOVERY_INTENT')
    # No float conversion: preserve exact decimal strings for F18 audit.
    value = json.dumps(intent, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                       allow_nan=False)
    return hashlib.sha256(value.encode('ascii')).hexdigest()


class RecoveryJournal:
    """Isolation: NOT the operational PAPER database; no external file handles retained."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=8)
        con.execute('PRAGMA busy_timeout=8000')
        return con

    def _init(self):
        con = self._connect()
        try:
            con.execute('''CREATE TABLE IF NOT EXISTS recovery (
                client_id TEXT PRIMARY KEY, intent_hash TEXT NOT NULL,
                state TEXT NOT NULL, observed INTEGER NOT NULL DEFAULT 0,
                coverage INTEGER NOT NULL DEFAULT 0, checked_at INTEGER NOT NULL,
                checks INTEGER NOT NULL DEFAULT 0)''')
            con.execute('''CREATE TABLE IF NOT EXISTS recovery_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
                state TEXT NOT NULL, checked_at INTEGER NOT NULL)''')
            con.commit()
        finally:
            con.close()

    def read(self, client_id):
        if not isinstance(client_id, str) or not _CLIENT.fullmatch(client_id):
            raise ValueError('INVALID_CLIENT_ID')
        con = self._connect()
        try:
            row = con.execute('SELECT intent_hash,state,observed,coverage,checked_at,checks FROM recovery WHERE client_id=?', (client_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return {'intent_hash': row[0], 'state': row[1], 'order_observed': bool(row[2]),
                'snapshot_coverage_verified': bool(row[3]), 'checked_at': row[4],
                'checks': row[5], 'allow_retry': False, 'protected': False,
                'ready_for_live': False, 'live_enabled': False}

    def record(self, client_id, digest, result):
        # Atomic compare-and-update; a conflicting intent never replaces prior evidence.
        con = self._connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT intent_hash FROM recovery WHERE client_id=?', (client_id,)).fetchone()
            if existing is not None and existing[0] != digest:
                con.rollback()
                return False
            now = int(time.time())
            con.execute('''INSERT INTO recovery(client_id,intent_hash,state,observed,coverage,checked_at,checks)
                VALUES(?,?,?,?,?,?,1) ON CONFLICT(client_id) DO UPDATE SET
                state=excluded.state,observed=excluded.observed,coverage=excluded.coverage,
                checked_at=excluded.checked_at,checks=checks+1''',
                (client_id, digest, result['state'], int(result['order_observed']),
                 int(result['snapshot_coverage_verified']), now))
            con.execute('INSERT INTO recovery_history(client_id,state,checked_at) VALUES(?,?,?)',
                        (client_id, result['state'], now))
            con.commit()
            return True
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def status(self):
        con = self._connect()
        try:
            count, _ = con.execute('SELECT COUNT(*), COALESCE(SUM(checks),0) FROM recovery').fetchone()
        finally:
            con.close()
        return {'build': 'F19', 'mode': 'GET_ONLY_RECOVERY_JOURNAL',
                'registered_intents': count, 'live_enabled': False,
                'allow_retry': False, 'ready_for_live': False}


def recover(journal, intent, boundary=None):
    """Audits only the supplied intent. No trading, no automatic retries.

    A changed intent under an existing clientId is denied before ANY GET request.
    All exceptions are sanitized: no secrets, payloads, or HTTP bodies returned.
    """
    try:
        digest = _canonical(intent)
    except (ValueError, TypeError, OverflowError):
        return _result('INVALID_RECOVERY_INTENT')
    cid = intent['client_id']
    existing = journal.read(cid)
    if existing is not None and existing['intent_hash'] != digest:
        return _result('CLIENT_ID_INTENT_CONFLICT')
    try:
        data = audit(boundary if boundary is not None else ReadOnlyBoundary(), **intent)
        result = _result(data['state'], coverage=data.get('snapshot_coverage_verified', False),
                         observed=data['state'] not in ('ORDER_LOOKUP_UNKNOWN', 'ORDER_IDENTITY_UNCONFIRMED', 'SNAPSHOT_UNAVAILABLE'))
    except Exception:
        result = _result('RECOVERY_GET_UNKNOWN')
    try:
        if not journal.record(cid, digest, result):
            return _result('CLIENT_ID_INTENT_CONFLICT')
    except Exception:
        return _result('RECOVERY_JOURNAL_UNAVAILABLE')
    return result


def offline_report():
    return {'build': 'F19', 'mode': 'GET_ONLY_RECOVERY', 'write_transport_enabled': False,
            'allow_retry': False, 'ready_for_live': False, 'live_enabled': False,
            'notice': 'RECONCILIATION_REQUIRES_EXPLICIT_INTENT_NO_AUTOMATIC_RETRY'}
