"""F60: personal Telegram notifications through YOUR bot (BotFather).

Why a bot and not the listener account: messages you send to yourself do not
trigger a phone notification. A bot does.

Setup (once):
  1. In Telegram open @BotFather -> /newbot -> copy the token.
  2. Open your new bot and press Start (send any message).
  3. .env: PA_NOTIFY_BOT_TOKEN=<token>   then: python run.py --notify-setup
     It prints your chat id -> .env: PA_NOTIFY_CHAT_ID=<id>
  4. python run.py --notify-test

The watcher reads the LIVE journal (SQLite) and never touches Bitunix. On first
start it marks the current history as seen, so it never floods old events.
The token is never printed or logged.
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

API = 'https://api.telegram.org'


class TelegramNotifier:
    def __init__(self, token, chat_id, *, base=API, opener=None, timeout=8):
        if not token or not chat_id:
            raise ValueError('NOTIFY_NOT_CONFIGURED')
        if base != API and not base.startswith('http://127.0.0.1:'):
            raise ValueError('NONCANONICAL_DESTINATION')
        self._token, self.chat_id, self.base, self.timeout = token, str(chat_id), base, timeout
        self._open = opener or build_opener().open
        self.sent = 0
        self.failed = 0

    def __repr__(self):
        return '<TelegramNotifier REDACTED>'

    @classmethod
    def from_env(cls, **kw):
        token, chat = os.getenv('PA_NOTIFY_BOT_TOKEN', ''), os.getenv('PA_NOTIFY_CHAT_ID', '')
        if not token or not chat:
            return None
        base = os.getenv('PA_NOTIFY_QA_BASE')
        return cls(token, chat, **({'base': base} if base else {}), **kw)

    def _call(self, method, payload):
        body = json.dumps(payload).encode('utf-8')
        req = Request('%s/bot%s/%s' % (self.base, self._token, method), data=body, method='POST',
                      headers={'Content-Type': 'application/json'})
        try:
            resp = self._open(req, timeout=self.timeout)
            try:
                data = json.loads(resp.read(200_000).decode('utf-8'))
            finally:
                resp.close()
        except (HTTPError, URLError, OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get('ok') is True else None

    def send(self, text):
        ok = self._call('sendMessage', {'chat_id': self.chat_id, 'text': text[:3900], 'parse_mode': 'HTML',
                                        'disable_web_page_preview': True}) is not None
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        return ok


def discover_chat_ids(token, *, base=API, opener=None):
    """Chats that wrote to the bot (getUpdates). Requires pressing Start on the bot first."""
    n = TelegramNotifier(token, 'setup', base=base, opener=opener)
    data = n._call('getUpdates', {'limit': 50})
    chats = {}
    for upd in (data or {}).get('result', []):
        msg = upd.get('message') or upd.get('edited_message') or {}
        chat = msg.get('chat') or {}
        if 'id' in chat:
            chats[str(chat['id'])] = chat.get('first_name') or chat.get('title') or chat.get('username') or ''
    return chats


def _fmt(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return '-'
    return ('%+.2f' % f).replace('.', ',')


SIDE = {'LONG': 'LARGO 🟢', 'SHORT': 'CORTO 🔴'}
EXIT = {'TP_LAST': 'Take Profit 🎯', 'BE_STOP': 'Break Even 🛡️', 'SL': 'Stop Loss ⛔', 'OTHER': 'Otro'}
REASONS = {'INSUFFICIENT_AVAILABLE_MARGIN': 'margen insuficiente', 'LEVELS_INVALID_FOR_CURRENT_PRICE':
           'el precio ya pasó la entrada o TP1', 'SYMBOL_ALREADY_EXPOSED': 'ya hay posición en ese par',
           'SL_LOSS_ABOVE_LIMIT': 'la pérdida por SL supera tu límite', 'MAX_POSITIONS_REACHED':
           'límite de posiciones', 'MAX_EXPOSURE_REACHED': 'exposición máxima alcanzada',
           'MARGIN_MODE_NOT_ISOLATION': 'el par está en margen cruzado', 'QTY_BELOW_CONTRACT_MINIMUM':
           'cantidad menor al mínimo del par', 'CONTRACT_NOT_OPEN': 'par no disponible en Bitunix',
           'SYMBOL_NOT_ON_BITUNIX': 'el par no existe en Bitunix'}


def message_for_event(store, ev):
    row = store.get(ev['client_id'])
    if not row:
        return None
    plan = json.loads(row['plan'])
    head = '<b>%s</b> %s · %sx' % (row['symbol'], SIDE.get(row['side'], row['side']), plan.get('leverage'))
    try:
        d = json.loads(ev['detail'])
    except ValueError:
        d = {}
    e = ev['event']
    if e == 'PROTECTED':
        if str(row['signal_id']).startswith('adopt:'):
            return None        # F67b: a manual position already gets "🤝 Posición manual gestionada"
        mode = row.get('tp_mode') or plan.get('tp_mode')
        if mode == 'LAST':
            tp = plan['tps'][-1]
        elif mode == 'PARTIAL' and plan.get('partial_tp'):
            tp = '%s %% en %s · resto en %s' % (plan.get('partial_pct', '75'), plan['partial_tp'], plan['tps'][-1])
        else:
            tp = ', '.join(plan['tps'])
        return ('✅ Operación abierta con SL y TP\n%s\nEntrada %s · Cantidad %s\nSL %s · TP %s\nMargen %s USDT'
                % (head, plan['entry'], row['filled_qty'] or plan['qty'], plan['sl'], tp,
                   _fmt(plan.get('margin_usdt')).lstrip('+')))
    if e == 'BE_CONFIRMED':
        return '🛡️ Stop movido a Break Even\n%s\nNuevo SL %s (sin pérdida si vuelve)' % (head, row['be_price'])
    if e in ('CLOSED', 'CLOSE_ATTRIBUTION'):
        if e == 'CLOSED' and d.get('attribution') != 'HISTORY_GET':
            return '🏁 Operación cerrada\n%s\nResultado: calculando…' % head
        if e == 'CLOSE_ATTRIBUTION' or d.get('attribution') == 'HISTORY_GET':
            pnl = d.get('realizedPNL')
            icon = '💰' if (pnl and float(pnl) > 0) else ('🔻' if pnl and float(pnl) < 0 else '🏁')
            return ('%s Operación cerrada · %s\n%s\nEntrada %s → Salida %s\nResultado <b>%s USDT</b>'
                    % (icon, EXIT.get(d.get('exit'), 'Cierre'), head, d.get('entryPrice', plan['entry']),
                       d.get('closePrice', '-'), _fmt(pnl)))
    if e in ('PROTECTION_REVIEW', 'ENTRY_UNKNOWN', 'ENTRY_REJECTED_UNCONFIRMED', 'REVIEW'):
        return '⚠️ Atención: operación a revisar\n%s\nEl automático no abre nada nuevo hasta revisarla. Mirá el panel.' % head
    if e == 'EMERGENCY_CLOSE_SENT':
        return '🚨 Cierre de emergencia enviado\n%s' % head
    return None


def message_for_decision(dec):
    try:
        d = json.loads(dec['detail'])
    except ValueError:
        d = {}
    k = dec['decision']
    if k == 'DISARMED':
        return '⏸️ Automático desarmado (%s)' % {'DASHBOARD': 'desde el panel', 'OPERATOR_CLI': 'desde la consola'}.get(
            d.get('reason'), 'por seguridad: hay una operación a revisar')
    if k == 'ARMED_LIVE':
        return '▶️ Automático armado: operando señales nuevas'
    if k in ('PLAN_REFUSED', 'EXECUTE_REFUSED') and d.get('symbol'):
        reason = REASONS.get(str(d.get('reason', '')).split(':')[0], 'chequeo de seguridad')
        return '⏭️ Señal no abierta: <b>%s</b> (%s)' % (d['symbol'], reason)
    KIND = {'SL_HIT': '"Stop Loss alcanzado"', 'ALL_TP': '"Todos los objetivos alcanzados"', 'TP_HIT': '"Objetivo tocado"'}
    if k == 'CHANNEL_BE_PENDING':
        return ('📣 El canal avisó TP%s en <b>%s</b>: el SL pasa a Break Even apenas el precio lo permita'
                % (d.get('tp') or '', d.get('symbol')))
    if k == 'CHANNEL_MISMATCH':
        return ('🔎 El canal dice %s en <b>%s</b>, pero tu posición sigue abierta en Bitunix%s. Revisá el panel.'
                % (KIND.get(d.get('kind'), d.get('kind')), d.get('symbol'),
                   ' (SL en Break Even %s)' % d['be_price'] if d.get('be_price') else ''))
    if k == 'CHANNEL_CORRECTION':
        return '📣 Corrección del canal%s. Revisá si te afecta.' % (' sobre <b>%s</b>' % d['symbol'] if d.get('symbol') else '')
    if k == 'AUTO_OFF_SIGNAL' and d.get('symbol'):
        return '⏸️ Señal NO abierta: <b>%s</b> %s (el automático está desarmado)' % (d['symbol'], SIDE.get(d.get('side'), ''))
    ADOPT = {'MULTIPLE_PROTECTION_ORDERS': 'tiene varias órdenes de SL/TP', 'PARTIAL_PROTECTION_ORDER':
             'su SL o TP no cubre toda la posición', 'NO_SL_NO_SIGNAL': 'no tiene SL y no hay señal del canal para ese par',
             'NO_TP_NO_SIGNAL': 'no tiene TP y no hay señal del canal para ese par',
             'LEVELS_INVALID_FOR_ENTRY': 'su SL ya está en la entrada o a favor', 'SL_ALREADY_CROSSED':
             'el precio ya pasó el SL de la señal'}
    if k == 'ADOPTED':
        tp = ('75 %% en %s · resto en %s (de la señal)' % (d.get('partial_tp'), d.get('tp'))
              if d.get('tp_mode') == 'PARTIAL' else '%s%s' % (d.get('tp'), ' (el tuyo)' if d.get('own_tp') else ' (de la señal)'))
        return ('🤝 Posición manual gestionada: <b>%s</b> %s\nSL %s%s · TP %s\nBreak Even automático activado'
                % (d.get('symbol'), SIDE.get(d.get('side'), ''), d.get('sl'), ' (el tuyo)' if d.get('own_sl') else ' (de la señal)', tp))
    if k == 'ADOPT_SKIPPED':
        return ('✋ Posición manual <b>%s</b> sin gestionar: %s'
                % (d.get('symbol'), ADOPT.get(str(d.get('reason', '')).split(':')[0], 'chequeo de seguridad')))
    if k == 'MANUAL_SENT':
        return '✅ Abierta a mano y validada por el sistema: <b>%s</b> %s' % (d.get('symbol'), SIDE.get(d.get('side'), ''))
    if k in ('MAX_OPEN_REACHED', 'MAX_PER_DAY_REACHED'):
        return '⏭️ Señal no abierta: %s' % ('límite de operaciones a la vez' if k == 'MAX_OPEN_REACHED' else 'límite diario')
    return None


class NotifyWatcher:
    """Polls the LIVE journal and pushes human messages. Never blocks trading threads."""

    def __init__(self, store, notifier, interval=5):
        self.store, self.notifier, self.interval = store, notifier, interval
        self.store._tx(lambda c: c.execute('CREATE TABLE IF NOT EXISTS notify_state (k TEXT PRIMARY KEY, v INTEGER NOT NULL)'),
                       begin=False)
        from punk.live_auto import AutoStore
        AutoStore(store)  # ensures live_auto_log exists

    def _get(self, k):
        row = self.store._tx(lambda c: c.execute('SELECT v FROM notify_state WHERE k=?', (k,)).fetchone())
        return None if row is None else row['v']

    def _set(self, k, v):
        self.store._tx(lambda c: c.execute('INSERT INTO notify_state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, v)))

    def _max(self, table):
        return self.store._tx(lambda c: c.execute('SELECT COALESCE(MAX(id),0) FROM %s' % table).fetchone()[0])

    def poll(self):
        sent = 0
        for key, table in (('events', 'live_events'), ('decisions', 'live_auto_log')):
            last = self._get(key)
            if last is None:
                self._set(key, self._max(table))   # first start: never flood history
                continue
            rows = self.store._tx(lambda c: [dict(r) for r in c.execute(
                'SELECT * FROM %s WHERE id>? ORDER BY id LIMIT 50' % table, (last,)).fetchall()])
            for r in rows:
                text = message_for_event(self.store, r) if key == 'events' else message_for_decision(r)
                if text:
                    if not self.notifier.send(text):
                        break          # Telegram unreachable: retry this message on the next poll
                    sent += 1
                self._set(key, r['id'])
        return sent

    def run(self):
        while True:
            try:
                self.poll()
            except Exception:
                pass
            time.sleep(self.interval)
