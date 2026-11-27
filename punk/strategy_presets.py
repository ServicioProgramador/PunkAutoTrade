"""F77: trading-strategy presets chosen from the dashboard (Configuración -> Cómo operar).

A preset sets ONLY how a trade is managed (TP mode, first-TP level, BE trigger). It never arms the
automatic mode, never changes capital limits and never touches an open position: it applies to the
next entries. Values are stored in the shared settings table and win over the .env defaults.
The figures in `evidence` come from tools/compare_strategies.py on 91 real channel signals
(24-27/11/2026, 100 USDT x20, 24 h horizon); past results do not guarantee future ones.
"""
from decimal import Decimal

PRESETS = {
    'LAST_BE1': {
        'label': 'Último objetivo + BE al TP1 o 1 %',
        'tp_mode': 'LAST', 'tp_pct': '1', 'be_move_pct': '1',
        'description': 'Un TP en el último objetivo. Al tocar el TP1 o el 1 % a favor, el SL pasa a BE.',
        'evidence': 'Comparador 24-27/11: +792 USDT, factor 2,07, caída máx. 111 (el mejor de los 8).'},
    'PARTIAL': {
        'label': 'Parcial 75/25',
        'tp_mode': 'PARTIAL', 'tp_pct': '1', 'be_move_pct': '1',
        'description': '75 % al TP1 o al 1 % (lo que llegue primero), 25 % al último objetivo, BE tras el parcial.',
        'evidence': 'Comparador 24-27/11: +506 USDT, factor 1,68, caída máx. 176.'},
    'FIRST_BE05': {
        'label': 'TP1 al 100 % + BE al 0,5 %',
        'tp_mode': 'FIRST', 'tp_pct': '1', 'be_move_pct': '0.5',
        'description': 'Todo se cierra en el TP1 o al 1 %. Con 0,5 % a favor el SL pasa a BE. La más defensiva.',
        'evidence': 'Comparador 24-27/11: +324 USDT, factor 2,15, caída máx. 59 (la menor).'},
    'FIRST': {
        'label': 'TP1 al 100 %, sin BE',
        'tp_mode': 'FIRST', 'tp_pct': '1', 'be_move_pct': '1000',
        'description': 'Todo se cierra en el TP1 o al 1 %. El SL no se mueve.',
        'evidence': 'Comparador 24-27/11: +410 USDT, factor 1,55, caída máx. 197.'},
}

KEYS = ('live_tp_mode', 'live_tp_pct', 'live_be_move_pct', 'live_strategy')


def current(repo):
    get = lambda k: repo.setting(k, '')
    return {'preset': get('live_strategy'), 'tp_mode': get('live_tp_mode'), 'tp_pct': get('live_tp_pct'),
            'be_move_pct': get('live_be_move_pct')}


def apply(repo, auto_store, preset_id):
    """Store the preset; returns the stored values. Raises ValueError for an unknown preset."""
    p = PRESETS.get(str(preset_id))
    if p is None:
        raise ValueError('UNKNOWN_PRESET')
    for k in ('tp_pct', 'be_move_pct'):
        Decimal(p[k])                                    # sanity
    repo.set_setting('live_tp_mode', p['tp_mode'])
    repo.set_setting('live_tp_pct', p['tp_pct'])
    repo.set_setting('live_be_move_pct', p['be_move_pct'])
    repo.set_setting('live_strategy', preset_id)
    if auto_store is not None:
        auto_store.save(tp_mode=p['tp_mode'])            # the automatic mode plans with this TP mode
        auto_store.log('-', 'STRATEGY_SET', {'preset': preset_id, 'label': p['label']})
    return current(repo)


def public():
    return [{'id': k, **{f: v[f] for f in ('label', 'description', 'evidence', 'tp_mode', 'tp_pct', 'be_move_pct')}}
            for k, v in PRESETS.items()]
