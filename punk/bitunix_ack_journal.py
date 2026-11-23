"""F32: durable, fail-closed order ACK processing; no HTTP or LIVE authority.

The order-acceptance response is NOT proof of a filled or protected position.
Only exact previously armed intents may record exchange-shaped synthetic receipts.
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from punk.live_write_lab import Intent, LabBlocked
from punk.bitunix_write_journal import WriteJournal


def _parse_ack(raw, status, client_id):
    if type(status) is not int or status != 200 or type(raw) is not bytes or len(raw) > 32768 or not raw:
        return None
    try:
        response = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return None
    if type(response) is not dict or type(response.get('code')) is not int or response['code'] != 0:
        return None
    data = response.get('data')
    if type(data) is not dict or type(data.get('orderId')) is not str or type(data.get('clientId')) is not str:
        return None
    if not data['orderId'] or len(data['orderId']) > 128 or data['clientId'] != client_id:
        return None
    return data['orderId']


class AckJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.intents = WriteJournal(self.path)
        con = self._connection()
        try:
            con.execute('''CREATE TABLE IF NOT EXISTS entry_receipts (
              client_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
              order_id TEXT NOT NULL, receipt_state TEXT NOT NULL,
              observed_at INTEGER NOT NULL)''')
            con.commit()
        finally:
            con.close()

    def _connection(self):
        con = sqlite3.connect(str(self.path), timeout=8)
        con.execute('PRAGMA busy_timeout=8000')
        return con

    def observe(self, intent, *, raw, http_status):
        if type(intent) is not Intent:
            raise LabBlocked('INVALID_INTENT_TYPE')
        intent.validate()
        # An armed intent is the minimum prior state. Never turn an ACK into send authority.
        existing = self.intents.read(intent)
        if existing is None or existing['state'] == 'PREPARED':
            raise LabBlocked('INTENT_NOT_ARMED')
        order_id = _parse_ack(raw, http_status, intent.client_id())
        con = self._connection()
        try:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT fingerprint,order_id,receipt_state FROM entry_receipts WHERE client_id=?',
                              (intent.client_id(),)).fetchone()
            if row is not None and row[0] != intent.fingerprint():
                raise LabBlocked('CLIENT_ID_INTENT_CONFLICT')
            if row is not None and row[2] == 'ACK_OBSERVED':
                # An ACK already recorded cannot be overwritten by a conflicting identity.
                if order_id is not None and order_id != row[1]:
                    state = 'RECEIPT_CONFLICT'
                    con.execute('UPDATE entry_receipts SET receipt_state=? WHERE client_id=?',
                                (state, intent.client_id()))
                else:
                    state = row[2]
            elif row is not None and row[2] == 'RECEIPT_CONFLICT':
                state = 'RECEIPT_CONFLICT'
            else:
                state = 'ACK_OBSERVED' if order_id else 'OUTCOME_UNKNOWN'
                con.execute('''INSERT INTO entry_receipts(client_id,fingerprint,order_id,receipt_state,observed_at)
                               VALUES(?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET
                               order_id=excluded.order_id,receipt_state=excluded.receipt_state,
                               observed_at=excluded.observed_at''',
                            (intent.client_id(),intent.fingerprint(),order_id or '',state,int(time.time())))
            con.commit()
            return {'build':'F32','state':state,'ack_recorded':state == 'ACK_OBSERVED',
                    'protected':False,'filled':False,'allow_send':False,'allow_retry':False,
                    'ready_for_live':False,'live_enabled':False,'orders_sent':0}
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def read(self, intent):
        if type(intent) is not Intent:
            raise LabBlocked('INVALID_INTENT_TYPE')
        intent.validate()
        self.intents.read(intent) # fingerprint validation from journal
        con = self._connection()
        try:
            row = con.execute('SELECT fingerprint,order_id,receipt_state FROM entry_receipts WHERE client_id=?',
                              (intent.client_id(),)).fetchone()
            if row is None:
                return None
            if row[0] != intent.fingerprint():
                raise LabBlocked('CLIENT_ID_INTENT_CONFLICT')
            return {'state':row[2],'has_order_id':bool(row[1]),'protected':False,
                    'allow_send':False,'allow_retry':False,'ready_for_live':False}
        finally:
            con.close()

    def submit(self, *_args, **_kwargs):
        raise LabBlocked('LIVE_TRANSMISSION_NOT_AUTHORIZED_F32')
