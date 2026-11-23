"""F60: LIVE results by date range and market session (read-only, SQLite only).

PnL source: the realizedPNL that Bitunix reports in get_history_positions, stored
with each CLOSED trade (close attribution). Trades without that evidence are
counted as "sin dato" and never estimated.

Market sessions (local exchange hours, Monday to Friday):
  Sídney 10:00-16:00 Australia/Sydney · Tokio 09:00-15:00 Asia/Tokyo
  Londres 08:00-16:30 Europe/London · Nueva York 09:30-16:00 America/New_York
Exact daylight-saving handling needs the IANA database (on Windows: pip install tzdata).
Without it, fixed standard offsets are used and the result is flagged approximate.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

SESSIONS = (
    ('SYDNEY', 'Sídney', 'Australia/Sydney', 10, 0, 16, 0, 10),
    ('TOKYO', 'Tokio', 'Asia/Tokyo', 9, 0, 15, 0, 9),
    ('LONDON', 'Londres', 'Europe/London', 8, 0, 16, 30, 0),
    ('NEWYORK', 'Nueva York', 'America/New_York', 9, 30, 16, 0, -5),
)


def _zone(name, fallback_hours):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), True
    except Exception:
        return timezone(timedelta(hours=fallback_hours)), False


def sessions_at(ts):
    """Exchange sessions open at a UTC epoch. Returns (codes, exact_tz)."""
    moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    out, exact = [], True
    for code, _label, tzname, h1, m1, h2, m2, fb in SESSIONS:
        tz, ok = _zone(tzname, fb)
        exact = exact and ok
        local = moment.astimezone(tz)
        if local.weekday() >= 5:
            continue
        minutes = local.hour * 60 + local.minute
        if h1 * 60 + m1 <= minutes < h2 * 60 + m2:
            out.append(code)
    return out, exact


def session_labels():
    return {code: label for code, label, *_ in SESSIONS}


def _dec(v):
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def closed_trades(store):
    """Closed LIVE trades with entry time, close time and the Bitunix close evidence."""
    out = []
    for r in store.all(5000):
        if r['state'] != 'CLOSED':
            continue
        events = store.events(r['client_id'], 80)
        close_ev = next((e for e in events if e['event'] == 'CLOSED'), None)
        attr = next((e for e in events if e['event'] == 'CLOSE_ATTRIBUTION'), None)
        entry_ev = next((e for e in reversed(events) if e['event'] in ('ENTRY_ACK', 'PROTECTING')), None)
        detail = {}
        for ev in (close_ev, attr):
            if ev:
                try:
                    d = json.loads(ev['detail'])
                except ValueError:
                    d = {}
                if d.get('attribution') == 'HISTORY_GET':
                    detail = d
        plan = json.loads(r['plan'])
        out.append({'client_id': r['client_id'], 'symbol': r['symbol'], 'side': r['side'],
                    'source': 'MANUAL' if r['signal_id'].startswith('manual') else 'TELEGRAM',
                    'opened_at': entry_ev['at'] if entry_ev else r['created_at'],
                    'closed_at': close_ev['at'] if close_ev else r['updated_at'],
                    'pnl': detail.get('realizedPNL'), 'fee': detail.get('fee'), 'exit': detail.get('exit'),
                    'entry': detail.get('entryPrice') or plan.get('entry'), 'close': detail.get('closePrice'),
                    'leverage': plan.get('leverage')})
    return out


def report(store, start, end):
    start, end = int(start), int(end)
    if end <= start:
        raise ValueError('INVALID_RANGE')
    labels = session_labels()
    trades = [t for t in closed_trades(store) if start <= t['closed_at'] < end]
    trades.sort(key=lambda t: t['closed_at'])
    total, gross_win, gross_loss = Decimal(0), Decimal(0), Decimal(0)
    wins = losses = unknown = 0
    by_session = {c: {'label': labels[c], 'trades': 0, 'pnl': Decimal(0), 'wins': 0} for c in labels}
    by_session['NONE'] = {'label': 'Sin bolsa abierta', 'trades': 0, 'pnl': Decimal(0), 'wins': 0}
    by_symbol, by_exit, curve, exact = {}, {}, [], True
    for t in trades:
        pnl = _dec(t['pnl'])
        codes, ok = sessions_at(t['opened_at'])
        exact = exact and ok
        t['sessions'] = codes
        t['session_labels'] = [labels[c] for c in codes] or ['Sin bolsa abierta']
        if pnl is None:
            unknown += 1
            continue
        total += pnl
        if pnl > 0:
            wins += 1
            gross_win += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += -pnl
        for c in (codes or ['NONE']):
            s = by_session[c]
            s['trades'] += 1
            s['pnl'] += pnl
            s['wins'] += pnl > 0
        sym = by_symbol.setdefault(t['symbol'], {'trades': 0, 'pnl': Decimal(0)})
        sym['trades'] += 1
        sym['pnl'] += pnl
        ex = by_exit.setdefault(t['exit'] or 'UNKNOWN', {'trades': 0, 'pnl': Decimal(0)})
        ex['trades'] += 1
        ex['pnl'] += pnl
        curve.append({'at': t['closed_at'], 'cum': str(total)})
    counted = wins + losses
    def ser(d):
        return {k: {kk: (str(vv) if isinstance(vv, Decimal) else vv) for kk, vv in v.items()} for k, v in d.items()}
    return {'from': start, 'to': end, 'trades': trades, 'count': len(trades), 'with_pnl': len(trades) - unknown,
            'unknown_pnl': unknown, 'pnl': str(total), 'wins': wins, 'losses': losses,
            'win_rate': str(round(Decimal(wins) / counted * 100, 1)) if counted else None,
            'profit_factor': str(round(gross_win / gross_loss, 2)) if gross_loss else None,
            'best': max((t['pnl'] for t in trades if _dec(t['pnl']) is not None), key=lambda x: Decimal(x), default=None),
            'worst': min((t['pnl'] for t in trades if _dec(t['pnl']) is not None), key=lambda x: Decimal(x), default=None),
            'by_session': ser(by_session), 'by_symbol': ser(by_symbol), 'by_exit': ser(by_exit),
            'curve': curve, 'sessions_exact': exact}


def open_sessions_now(now=None):
    import time as _t
    codes, exact = sessions_at(now if now is not None else _t.time())
    labels = session_labels()
    return {'open': [labels[c] for c in codes], 'exact': exact}
