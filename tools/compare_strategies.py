"""Comparador de estrategias (solo lectura).

Toma las señales del canal que el bot guardó (desde F66) y simula, con velas de 1 minuto públicas
de Bitunix, qué habría pasado con cada estrategia sobre LAS MISMAS señales. No toca tu cuenta: no
usa la API key y no envía órdenes.

Uso (desde la carpeta del proyecto):
    python tools/compare_strategies.py                 # últimos 7 días, margen 100
    python tools/compare_strategies.py --days 3 --margin 100 --hours 24

Supuestos (conservadores):
- Entrada como el bot: a mercado si el precio está a menos de 0,3 % de la entrada; si no, límite en
  la entrada que espera hasta 2 h. Si el precio ya pasó el TP1 a mercado, no entra.
- Dentro de una misma vela, si se tocan el SL y un TP, se asume que primero tocó el SL.
- El SL pasa a Break Even recién en la vela siguiente al disparo (el bot revisa cada ~20 s).
- Comisión 0,05 % por lado (taker). BE = entrada + 2 comisiones + 0,1 %.
- Si en el horizonte no toca SL ni TP, se cierra al último precio ("abierta al final").
Lo que pasó con señales pasadas no garantiza el futuro.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from punk.core import parse  # noqa: E402

BASE = 'https://fapi.bitunix.com'
FEE = D('0.0005')
BE_PROFIT = D('0.001')
MARKET_DEV = D('0.003')
LIMIT_WAIT_MIN = 120


@dataclass(frozen=True)
class Strategy:
    name: str
    mode: str                 # LAST | PARTIAL | FIRST
    tp_pct: D = D('1')        # first TP level (% in favour), the signal TP1 is used if it comes first
    be_pct: D | None = None   # BE trigger (% in favour); None = no BE by move
    be_on_tp1: bool = False   # BE when TP1 is touched (LAST)
    sl_cap: D | None = None   # max SL distance (%), None = signal SL
    partial: D = D('0.75')


STRATEGIES = [
    Strategy('Actual: 75 % al TP1/1 %, 25 % al final, BE tras el parcial', 'PARTIAL'),
    Strategy('100 % al TP1/1 %, sin BE', 'FIRST'),
    Strategy('100 % al TP1/1 % + BE al 0,5 %', 'FIRST', be_pct=D('0.5')),
    Strategy('100 % al TP1/1 % + BE al 0,65 %', 'FIRST', be_pct=D('0.65')),
    Strategy('100 % al TP1/1 % + SL máx 1 %', 'FIRST', sl_cap=D('1')),
    Strategy('100 % al TP1/1 % + SL máx 0,75 %', 'FIRST', sl_cap=D('0.75')),
    Strategy('100 % al TP1/1 % + SL máx 1 % + BE 0,5 %', 'FIRST', be_pct=D('0.5'), sl_cap=D('1')),
    Strategy('Un TP al último objetivo + BE al TP1/1 %', 'LAST', be_pct=D('1'), be_on_tp1=True),
]


# ------------------------------------------------------------------ data
class Klines:
    """1m candles from Bitunix public market data, cached in data/kline_cache.db."""

    def __init__(self, cache_path, fetch=None):
        self.con = sqlite3.connect(str(cache_path))
        self.con.execute('CREATE TABLE IF NOT EXISTS k (symbol TEXT, t INTEGER, o TEXT, h TEXT, l TEXT, c TEXT,'
                         ' PRIMARY KEY(symbol, t))')
        self.fetch = fetch or self._http

    def close(self):
        self.con.close()

    @staticmethod
    def _http(symbol, end_ms, limit=200):
        q = urllib.parse.urlencode({'symbol': symbol, 'interval': '1m', 'endTime': end_ms, 'limit': limit})
        with urllib.request.urlopen(BASE + '/api/v1/futures/market/kline?' + q, timeout=15) as r:
            body = json.loads(r.read().decode())
        if body.get('code') != 0:
            raise RuntimeError('Bitunix %s: %s' % (body.get('code'), body.get('msg')))
        return body.get('data') or []

    def get(self, symbol, start_ms, end_ms):
        start_ms -= start_ms % 60000
        have = {r[0] for r in self.con.execute('SELECT t FROM k WHERE symbol=? AND t>=? AND t<?',
                                               (symbol, start_ms, end_ms))}
        need = (end_ms - start_ms) // 60000
        if len(have) < need - 2:
            cursor = start_ms + 200 * 60000
            while cursor <= end_ms + 200 * 60000:
                rows = self.fetch(symbol, min(cursor, end_ms), 200)
                self.con.executemany('INSERT OR REPLACE INTO k VALUES (?,?,?,?,?,?)',
                                     [(symbol, int(x['time']), str(x['open']), str(x['high']), str(x['low']),
                                       str(x['close'])) for x in rows])
                self.con.commit()
                time.sleep(0.12)                     # public rate limit: 10 req/s
                if cursor >= end_ms:
                    break
                cursor += 200 * 60000
        return [(t, D(o), D(h), D(lo), D(c)) for t, o, h, lo, c in self.con.execute(
            'SELECT t,o,h,l,c FROM k WHERE symbol=? AND t>=? AND t<? ORDER BY t', (symbol, start_ms, end_ms))]


# ------------------------------------------------------------------ simulation
def simulate(sig, candles, st: Strategy, margin=D('100'), default_lev=20):
    if not candles:
        return {'result': 'SIN_DATOS'}
    long = sig.side == 'LONG'
    sgn = 1 if long else -1
    tp1, last = sig.tps[0], sig.tps[-1]
    entry = sig.entry_price or candles[0][1]
    p0 = candles[0][1]
    # ---- entry (mirrors the bot: market near the entry, otherwise a resting limit at the entry)
    if abs(p0 - entry) / entry <= MARKET_DEV:
        fill, start = p0, 0
        if not ((sig.sl < p0 < tp1) if long else (sig.sl > p0 > tp1)):
            return {'result': 'NO_ENTRA'}
    else:
        waiting = (p0 > entry) if long else (p0 < entry)
        if not waiting and not ((sig.sl < p0 < tp1) if long else (sig.sl > p0 > tp1)):
            return {'result': 'NO_ENTRA'}
        fill, start = None, None
        for i, (_, o, h, lo, c) in enumerate(candles[:LIMIT_WAIT_MIN]):
            if (lo <= entry) if long else (h >= entry):
                fill, start = entry, i
                break
        if fill is None:
            return {'result': 'NO_LLENA'}
    lev = D(sig.leverage or default_lev)
    notional = margin * lev
    # ---- levels
    sl = sig.sl
    if st.sl_cap is not None:
        cap = fill * (1 - sgn * st.sl_cap / 100)
        sl = max(sl, cap) if long else min(sl, cap)
    pct_level = fill * (1 + sgn * st.tp_pct / 100)
    cover = fill * (1 + sgn * (2 * FEE + BE_PROFIT))
    tp1_ok = (tp1 > cover) if long else (tp1 < cover)
    first = tp1 if tp1_ok and ((tp1 < pct_level) if long else (tp1 > pct_level)) else pct_level
    be_price = fill * (1 + sgn * (2 * FEE + BE_PROFIT))
    be_trig = fill * (1 + sgn * st.be_pct / 100) if st.be_pct is not None else None
    # ---- path
    remaining, stop, pnl, fees = D(1), sl, D(0), notional * FEE
    partial_done, be_on, be_next, outcome = False, False, False, None

    def close(frac, price):
        nonlocal remaining, pnl, fees
        pnl += frac * notional * (price - fill) / fill * sgn
        fees += frac * notional * FEE
        remaining -= frac

    for _, o, h, lo, c in candles[start:]:
        if be_next and not be_on:
            if (be_price > stop) if long else (be_price < stop):
                stop = be_price
            be_on, be_next = True, False
        hit_stop = (lo <= stop) if long else (h >= stop)
        if hit_stop:
            px = (min(o, stop) if long else max(o, stop))
            close(remaining, px)
            outcome = 'BE' if be_on and not partial_done else ('PARCIAL+BE' if partial_done else 'SL')
            break
        reach = (lambda lvl: h >= lvl) if long else (lambda lvl: lo <= lvl)
        if st.mode in ('PARTIAL', 'FIRST') and not partial_done and reach(first):
            close(D(1) if st.mode == 'FIRST' else st.partial, first)
            partial_done = True
            if st.mode == 'FIRST':
                outcome = 'TP'
                break
            be_next = True
        if reach(last) and remaining > 0:
            close(remaining, last)
            outcome = 'PARCIAL+TP' if partial_done else 'TP'
            break
        if be_trig is not None and not be_on and (reach(be_trig) or (st.be_on_tp1 and reach(tp1))):
            be_next = True
        if st.mode == 'PARTIAL' and not partial_done and reach(fill * (1 + sgn * 2 * st.tp_pct / 100)):
            be_next = True                                   # bot safety net (2 x level)
    if outcome is None:
        close(remaining, candles[-1][4])
        outcome = 'ABIERTA_AL_FINAL'
    return {'result': outcome, 'pnl': pnl - fees, 'fill': fill, 'sl': sl, 'tp': first}


def summarize(rows):
    done = [r for r in rows if 'pnl' in r]
    wins = [r['pnl'] for r in done if r['pnl'] > D('0.5')]
    losses = [r['pnl'] for r in done if r['pnl'] < D('-0.5')]
    total = sum((r['pnl'] for r in done), D(0))
    peak = dd = run = D(0)
    for r in done:
        run += r['pnl']
        peak = max(peak, run)
        dd = max(dd, peak - run)
    gw, gl = sum(wins, D(0)), -sum(losses, D(0))
    count = lambda k: sum(1 for r in done if r['result'] == k)
    return {'ops': len(done), 'tp': count('TP') + count('PARCIAL+TP') + count('PARCIAL+BE'),
            'be': count('BE'), 'sl': count('SL'), 'open': count('ABIERTA_AL_FINAL'), 'total': total,
            'avg_win': gw / len(wins) if wins else D(0), 'avg_loss': -gl / len(losses) if losses else D(0),
            'pf': (gw / gl) if gl else None, 'hit': (len(wins) * 100 / (len(wins) + len(losses))) if wins or losses else None,
            'max_dd': dd}


def load_signals(db, days):
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute('SELECT signal_id, at, symbol, side, text FROM live_signal_cache WHERE at>=? ORDER BY at',
                           (int(time.time()) - days * 86400,)).fetchall()
    finally:
        con.close()
    out = []
    for sid, at, symbol, side, text in rows:
        sig, status, _ = parse(text)
        if status == 'VALIDA':
            out.append((sid, at, symbol, sig))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='Compara estrategias sobre tus señales (solo lectura).')
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--hours', type=int, default=24, help='horizonte de cada operación')
    ap.add_argument('--margin', default='100')
    ap.add_argument('--db', default=str(ROOT / 'data' / 'punk_live.db'))
    a = ap.parse_args(argv)
    signals = load_signals(a.db, a.days)
    print('Señales a simular: %d (últimos %d días)' % (len(signals), a.days), flush=True)
    kl = Klines(ROOT / 'data' / 'kline_cache.db')
    per = {s.name: [] for s in STRATEGIES}
    detail = []
    try:
        for n, (sid, at, symbol, sig) in enumerate(signals, 1):
            t0 = at * 1000
            try:
                candles = kl.get(symbol, t0, t0 + a.hours * 3600 * 1000)
            except Exception as exc:                          # one bad symbol must not stop the report
                print('  %s %s: sin velas (%s)' % (sid, symbol, exc), flush=True)
                continue
            line = {'signal': sid, 'symbol': symbol, 'side': sig.side}
            for st in STRATEGIES:
                r = simulate(sig, candles, st, margin=D(a.margin))
                per[st.name].append(r)
                line[st.name] = '%s %s' % (r['result'], ('%.2f' % r['pnl']) if 'pnl' in r else '')
            detail.append(line)
            if n % 10 == 0:
                print('  %d/%d...' % (n, len(signals)), flush=True)
    finally:
        kl.close()
    print()
    print('%-58s %4s %4s %4s %4s %10s %8s %8s %6s %7s %8s' % (
        'Estrategia', 'Ops', 'TP', 'BE', 'SL', 'Resultado', 'ProGan', 'ProPer', 'Factor', 'Acierto', 'MaxDD'))
    for st in STRATEGIES:
        s = summarize(per[st.name])
        print('%-58s %4d %4d %4d %4d %+10.2f %+8.2f %+8.2f %6s %6s%% %8.2f' % (
            st.name[:58], s['ops'], s['tp'], s['be'], s['sl'], s['total'], s['avg_win'], s['avg_loss'],
            ('%.2f' % s['pf']) if s['pf'] is not None else '-', ('%.0f' % s['hit']) if s['hit'] is not None else '-',
            s['max_dd']))
    out = ROOT / 'data' / ('comparacion_%s.csv' % time.strftime('%Y%m%d_%H%M'))
    if detail:
        with open(out, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(detail[0].keys()))
            w.writeheader()
            w.writerows(detail)
        print('\nDetalle por señal: %s' % out)
    return per


if __name__ == '__main__':
    main()
