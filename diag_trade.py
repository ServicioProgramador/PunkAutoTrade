"""Diagnóstico de solo lectura: últimos eventos de una operación. Uso: python diag_trade.py DASHUSDT"""
import sqlite3
import sys

symbol = (sys.argv[1] if len(sys.argv) > 1 else 'DASHUSDT').upper()
con = sqlite3.connect('data/punk_live.db')
try:
    rows = con.execute(
        'SELECT e.event, e.detail FROM live_events e JOIN live_trades t ON t.client_id = e.client_id '
        'WHERE t.symbol = ? ORDER BY e.id DESC LIMIT 12', (symbol,)).fetchall()
    trade = con.execute('SELECT client_id, state, reason, tp_mode FROM live_trades WHERE symbol = ? '
                        'ORDER BY rowid DESC LIMIT 1', (symbol,)).fetchone()
finally:
    con.close()
print('OPERACION', trade)
for event, detail in rows:
    print(event, detail[:1500])