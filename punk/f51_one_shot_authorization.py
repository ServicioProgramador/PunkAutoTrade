"""F51 — durable one-shot operator authorization laboratory.

An approval is bound to one exact trade intent and an explicit offline review.
This is NOT a live trading capability. No network, exchange credentials, or send API.
Every consume attempt burns approval irrevocably before returning an audit receipt.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path

from punk.live_write_lab import Intent, LabBlocked


class OneShotApproval:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = self._open()
        try:
            con.execute('''CREATE TABLE IF NOT EXISTS f51_approvals (
                client_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                token_hash TEXT NOT NULL, state TEXT NOT NULL,
                expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
                consumed_at INTEGER)''')
            con.commit()
        finally:
            con.close()

    def _open(self):
        c = sqlite3.connect(str(self.db_path), timeout=8)
        c.execute('PRAGMA busy_timeout=8000')
        return c

    def _atomic(self, fn):
        c = self._open()
        try:
            c.execute('BEGIN IMMEDIATE')
            value = fn(c)
            c.commit()
            return value
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _identity(intent):
        if type(intent) is not Intent:
            raise LabBlocked('F51_INVALID_INTENT')
        intent.validate()
        return intent.client_id(), intent.fingerprint()

    @staticmethod
    def _report(state):
        return {'build': 'F51', 'state': state,
                'mode': 'LOCAL_ONE_SHOT_AUTHORIZATION_AUDIT',
                'live_enabled': False, 'write_enabled': False,
                'ready_for_live': False, 'allow_retry': False, 'real_orders_sent': 0}

    def approve_offline(self, intent, *, review, ttl_seconds=120, now=None):
        """Requires a reviewed, typed offline preflight. NEVER approves a live send.

        The secret token is returned once, never logged/stored in plaintext.
        """
        cid, fingerprint = self._identity(intent)
        if type(review) is not dict or review.get('state') != 'PRETRADE_PREVIEW_ONLY':
            raise LabBlocked('F51_REVIEW_REQUIRED')
        if review.get('fingerprint') != fingerprint or review.get('client_id') != cid:
            raise LabBlocked('F51_INTENT_MISMATCH')
        if review.get('snapshot_get_only_verified') is not True or review.get('sl_required') is not True:
            raise LabBlocked('F51_MISSING_PREFLIGHT_OR_STOP')
        if any(review.get(k) is not False for k in ('live_enabled', 'write_transport_enabled', 'ready_for_live')):
            raise LabBlocked('F51_LIVE_REVIEW_FORBIDDEN')
        if type(ttl_seconds) is not int or not 10 <= ttl_seconds <= 300:
            raise LabBlocked('F51_TTL_INVALID')
        clock = int(time.time()) if now is None else now
        if type(clock) is not int or clock <= 0:
            raise LabBlocked('F51_CLOCK_INVALID')
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        def write(c):
            old = c.execute('SELECT fingerprint,state FROM f51_approvals WHERE client_id=?', (cid,)).fetchone()
            if old is not None:
                if old[0] != fingerprint:
                    raise LabBlocked('F51_INTENT_CHANGED')
                raise LabBlocked('F51_APPROVAL_ALREADY_EXISTS')
            c.execute('''INSERT INTO f51_approvals
                       (client_id,fingerprint,token_hash,state,expires_at,created_at)
                       VALUES(?,?,?,'APPROVED_OFFLINE',?,?)''',
                      (cid, fingerprint, digest, clock + ttl_seconds, clock))
        self._atomic(write)
        return token

    def consume_offline(self, intent, token, *, now=None):
        """Atomically burn approval, return only audit receipt; no send capability."""
        cid, fingerprint = self._identity(intent)
        clock = int(time.time()) if now is None else now
        if type(clock) is not int or clock <= 0:
            raise LabBlocked('F51_CLOCK_INVALID')
        if type(token) is not str or not 16 <= len(token) <= 256:
            return self._report('TOKEN_INVALID')
        digest = hashlib.sha256(token.encode('utf8')).hexdigest()
        def consume(c):
            row = c.execute('SELECT fingerprint,token_hash,state,expires_at FROM f51_approvals WHERE client_id=?', (cid,)).fetchone()
            if not row:
                return self._report('NO_APPROVAL')
            if row[0] != fingerprint:
                return self._report('INTENT_CHANGED')
            if row[2] != 'APPROVED_OFFLINE':
                return self._report('ALREADY_CONSUMED_OR_EXPIRED')
            if clock > row[3]:
                c.execute('UPDATE f51_approvals SET state=? WHERE client_id=?', ('EXPIRED', cid))
                return self._report('EXPIRED')
            if not hmac.compare_digest(row[1], digest):
                return self._report('TOKEN_MISMATCH')
            c.execute('UPDATE f51_approvals SET state=?,consumed_at=? WHERE client_id=?', ('CONSUMED_OFFLINE',clock,cid))
            return self._report('CONSUMED_OFFLINE_NO_SEND')
        return self._atomic(consume)

    def state(self, intent):
        cid, fingerprint = self._identity(intent)
        def read(c):
            row = c.execute('SELECT fingerprint,state FROM f51_approvals WHERE client_id=?', (cid,)).fetchone()
            return 'ABSENT' if row is None else ('INTENT_CHANGED' if row[0] != fingerprint else row[1])
        return self._atomic(read)


def lab_report():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='punk_f51_') as t:
        intent = Intent('f51:qa:rune','RUNEUSDT','SHORT','10','0.4785','0.487456',
                        ('0.470246','0.469008','0.467584','0.465947','0.464063'))
        review = {'state':'PRETRADE_PREVIEW_ONLY','client_id':intent.client_id(),
                  'fingerprint':intent.fingerprint(), 'snapshot_get_only_verified':True,
                  'sl_required':True,'live_enabled':False,'write_transport_enabled':False,
                  'ready_for_live':False}
        db=Path(t)/'approval.sqlite'
        token=OneShotApproval(db).approve_offline(intent, review=review)
        first=OneShotApproval(db).consume_offline(intent,token)
        second=OneShotApproval(db).consume_offline(intent,token)
        return {**first,'replay_state':second['state'],
                'checkpoint_recovered':OneShotApproval(db).state(intent)=='CONSUMED_OFFLINE',
                'approval_reusable':False}
