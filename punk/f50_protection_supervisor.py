"""F50 — post-ACK protective-state supervisor (read-only, fail closed).

No Bitunix POST, leverage changes, stop modifications or LIVE authority. Keeps a
separate durable safety verdict. A GET-observed SL is never an execution guarantee.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from punk.f46_live_guard import guard_snapshot

VALID = {'NO_EXPOSURE', 'ESCALATE_UNPROTECTED', 'ESCALATE_CHANGED',
         'ESCALATE_UNVERIFIED', 'MONITOR_AUDIT_ONLY'}


def _safe_report(state, *, symbol, ack_match=False, stop_observed=False):
    return {'build':'F50', 'mode':'POST_ACK_READ_ONLY_SUPERVISION',
            'state': state, 'symbol':symbol, 'ack_match':bool(ack_match),
            'stop_observed':bool(stop_observed),
            'ready_for_live':False, 'live_enabled':False, 'protected':False,
            'orders_sent':0, 'allow_retry':False, 'write_enabled':False,
            'requires_operator_review':state != 'NO_EXPOSURE'}


def evaluate(symbol, ack_report, first, second, *, now=None):
    """Evaluate two independent account snapshots after exact ACK identity check.

    A successful check is *observation only*. NEVER grant send/repair authority.
    """
    if type(ack_report) is not dict or not (ack_report.get('ack_snapshot_match') is True
            and ack_report.get('state') == 'ACK_FILL_STOP_GET_AUDIT_ONLY'):
        return _safe_report('ESCALATE_UNVERIFIED', symbol=symbol)
    clock = time.time() if now is None else now
    a=guard_snapshot(first, symbol, now=clock)
    b=guard_snapshot(second, symbol, now=clock)
    if not a.get('complete') or not b.get('complete'):
        return _safe_report('ESCALATE_UNVERIFIED', symbol=symbol, ack_match=True)
    if a.get('state')=='NO_OPEN_POSITION_AUDITED' and b.get('state')=='NO_OPEN_POSITION_AUDITED':
        return _safe_report('NO_EXPOSURE',symbol=symbol,ack_match=True)
    # A changed inventory is an escalation even if both snapshots show some SL.
    def inventory(s):
        return json.dumps({'p':s.get('positions'),'o':s.get('pending_orders'),
                           't':s.get('pending_tpsl')},sort_keys=True,separators=(',',':'))
    if inventory(first)!=inventory(second):
        return _safe_report('ESCALATE_CHANGED',symbol=symbol,ack_match=True)
    if a['state'] != 'STOP_COVERAGE_OBSERVED_AUDIT_ONLY' or b['state'] != 'STOP_COVERAGE_OBSERVED_AUDIT_ONLY':
        return _safe_report('ESCALATE_UNPROTECTED',symbol=symbol,ack_match=True)
    return _safe_report('MONITOR_AUDIT_ONLY',symbol=symbol,ack_match=True,stop_observed=True)


class SafetyCheckpoint:
    """Persist a conservative safety verdict in its own SQLite database.

    No changes to PAPER database. SQLite connections are always closed, including
    error paths, to avoid WinError 32 on Windows QA cleanup.
    """
    def __init__(self, db_path):
        self.db_path = Path(db_path)

    def record(self, key, report):
        if not isinstance(key,str) or not key or len(key)>128 or report.get('state') not in VALID:
            raise ValueError('INVALID_CHECKPOINT')
        self.db_path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(str(self.db_path),timeout=5)
        try:
            with conn:
                conn.execute('CREATE TABLE IF NOT EXISTS f50_checkpoints (id TEXT PRIMARY KEY, state TEXT NOT NULL, report TEXT NOT NULL)')
                old=conn.execute('SELECT state FROM f50_checkpoints WHERE id=?',(key,)).fetchone()
                if old and old[0].startswith('ESCALATE_'):
                    # Escalation is sticky until explicit human review; a later
                    # good GET must not clear it or authorize a resubmission.
                    return self.read_from(conn,key)
                conn.execute('INSERT INTO f50_checkpoints(id,state,report) VALUES(?,?,?) '
                             'ON CONFLICT(id) DO UPDATE SET state=excluded.state,report=excluded.report',
                             (key,report['state'],json.dumps(report,sort_keys=True)))
                return dict(report)
        finally:
            conn.close()

    @staticmethod
    def read_from(conn,key):
        row=conn.execute('SELECT report FROM f50_checkpoints WHERE id=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def read(self,key):
        if not self.db_path.exists(): return None
        conn=sqlite3.connect(str(self.db_path),timeout=5)
        try:return self.read_from(conn,key)
        finally:conn.close()


def lab_report():
    """Disposable synthetic observation; no production network or credentials."""
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='punk_f50_') as t:
        now=time.time()
        snap={'status':'GET_ONLY_VERIFIED','checked_at':now,'positions':[
            {'symbol':'RUNEUSDT','positionId':'LAB_POS','qty':'10','side':'SHORT'}],
            'pending_orders':[], 'pending_tpsl':[
            {'symbol':'RUNEUSDT','positionId':'LAB_POS','slPrice':'0.487456','slQty':'10'}]}
        ack={'state':'ACK_FILL_STOP_GET_AUDIT_ONLY','ack_snapshot_match':True}
        result=evaluate('RUNEUSDT',ack,snap,snap,now=now)
        store=SafetyCheckpoint(Path(t)/'safety.sqlite')
        store.record('LAB:RUNE',result)
        return {**store.read('LAB:RUNE'), 'checkpoint_recovered':True, 'real_orders_sent':0}
