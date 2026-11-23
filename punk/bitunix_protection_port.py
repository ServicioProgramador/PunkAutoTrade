"""F56: real Bitunix port for the F54 protection state machine.

Implements the exact F54 port contract (snapshot / attach_stop / attach_tp /
move_stop) with GET readback and the documented TP/SL endpoints:
  POST /api/v1/futures/tpsl/place_order              (SL or TP with quantity)
  POST /api/v1/futures/tpsl/modify_order             (quantity SL -> BE)
  POST /api/v1/futures/tpsl/position/modify_order    (position-level SL -> BE)
  GET  /api/v1/futures/position/get_pending_positions
  GET  /api/v1/futures/tpsl/get_pending_orders

Rules:
- Every write is idempotent at this level: if GET already shows the exact
  expected protection, no POST is sent.
- UNKNOWN transport outcomes raise TimeoutError (F54 keeps UNKNOWN_* state).
- Business rejections raise LabBlocked (F54 keeps UNKNOWN_* -> operator review).
- A conflicting existing SL is never overwritten.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

from punk.live_write_lab import Intent, LabBlocked


SIDE_ALIASES = {'LONG': frozenset({'LONG', 'BUY'}), 'SHORT': frozenset({'SHORT', 'SELL'})}


def canon(value):
    """Canonical decimal text: no exponent, no trailing zeros."""
    if isinstance(value, bool) or value in (None, ''):
        raise LabBlocked('INVALID_DECIMAL')
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise LabBlocked('INVALID_DECIMAL') from None
    if not d.is_finite():
        raise LabBlocked('INVALID_DECIMAL')
    text = format(d, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _positive(value):
    try:
        d = Decimal(str(value))
        return d if d.is_finite() and d > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


class BitunixProtectionPort:
    """Bound to ONE intent. Holds no credentials itself (uses the given clients)."""

    def __init__(self, reader, trader, intent, *, settle_seconds=6.0, sleep=time.sleep,
                 stop_type='MARK_PRICE'):
        if type(intent) is not Intent:
            raise LabBlocked('INVALID_INTENT')
        intent.validate()
        self.reader, self.trader, self.intent = reader, trader, intent
        self.settle_seconds = settle_seconds
        self.sleep = sleep
        self.stop_type = stop_type
        self.calls = []            # POST endpoint names actually sent
        self.outcomes = []         # public() summaries of POST outcomes
        self.stop_id = None
        self.stop_kind = None      # QTY | POSITION
        self.foreign = 0           # TP/SL records on this position we did not plan
        self.position_raw = None
        self._tp_index = {canon(p): n for n, p in enumerate(intent.take_profits, 1)}

    # ------------------------------------------------------------------ reads
    def _get_list(self, name, params):
        data = self.reader.get(name, params)
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise TimeoutError('GET_SHAPE_UNVERIFIED')
        return data

    def snapshot(self):
        symbol, side = self.intent.symbol, self.intent.side
        try:
            positions = self._get_list('positions', {'symbol': symbol})
            orders = self._get_list('tpsl', {'symbol': symbol, 'skip': 0, 'limit': 100})
        except TimeoutError:
            raise
        except Exception:
            raise TimeoutError('GET_UNAVAILABLE') from None
        if len(orders) >= 100:
            raise TimeoutError('TPSL_PAGE_INCOMPLETE')
        # LIVE evidence (2026-11-22): Bitunix returns side BUY/SELL in HEDGE mode,
        # while the docs say LONG/SHORT. Accept both encodings of the SAME direction.
        accepted = SIDE_ALIASES[side]
        mine = [p for p in positions if p.get('symbol') == symbol and str(p.get('side') or '').upper() in accepted
                and _positive(p.get('qty')) is not None]
        if len(mine) > 1:
            raise LabBlocked('AMBIGUOUS_POSITION')
        if not mine:
            self.position_raw = None
            return {'position': None, 'stop': None, 'tps': {}}
        raw = mine[0]
        pid = str(raw.get('positionId') or '')
        if not pid:
            raise LabBlocked('POSITION_ID_MISSING')
        self.position_raw = dict(raw)
        qty = canon(raw['qty'])
        position = {'position_id': pid, 'client_id': self.intent.client_id(), 'symbol': symbol,
                    'side': side, 'qty': qty}
        rel = [o for o in orders if str(o.get('positionId') or '') == pid]
        stops = [o for o in rel if _positive(o.get('slPrice')) is not None]
        tps = [o for o in rel if _positive(o.get('tpPrice')) is not None]
        combined = [o for o in rel if o in stops and o in tps]
        stop = None
        self.stop_id = self.stop_kind = None
        if len(stops) == 1 and not combined:
            o = stops[0]
            sl_qty = _positive(o.get('slQty'))
            self.stop_id = str(o.get('id') or '')
            self.stop_kind = 'QTY' if sl_qty is not None else 'POSITION'
            # A position-level SL closes the whole position: coverage == position qty.
            stop = {'position_id': pid, 'price': canon(o['slPrice']),
                    'qty': canon(sl_qty) if sl_qty is not None else qty}
        elif stops:
            stop = {'ambiguous': len(stops)}
        mapped, foreign = {}, 0
        for o in tps:
            if o in combined:
                foreign += 1
                continue
            number = self._tp_index.get(canon(o['tpPrice']))
            tq = _positive(o.get('tpQty'))
            if number is None or tq is None or number in mapped:
                foreign += 1
                continue
            mapped[number] = {'position_id': pid, 'price': canon(o['tpPrice']), 'qty': canon(tq)}
        self.foreign = foreign
        return {'position': position, 'stop': stop, 'tps': mapped}

    def _settle(self, predicate):
        deadline = time.monotonic() + self.settle_seconds
        while True:
            snap = self.snapshot()
            if predicate(snap):
                return snap
            if time.monotonic() >= deadline:
                return snap
            self.sleep(0.8)

    # ----------------------------------------------------------------- writes
    def _send(self, endpoint, payload):
        self.calls.append(endpoint)
        outcome = self.trader.post(endpoint, payload)
        self.outcomes.append(outcome.public())
        if outcome.state == 'ACK':
            return outcome
        if outcome.state == 'REJECTED':
            raise LabBlocked('EXCHANGE_REJECTED_' + str(outcome.code if outcome.code is not None else outcome.http_status))
        raise TimeoutError('WRITE_OUTCOME_UNKNOWN')

    def attach_stop(self, position_id, price, qty):
        snap = self.snapshot()
        current = snap.get('stop')
        if current is not None:
            if current.get('ambiguous'):
                raise LabBlocked('STOP_AMBIGUOUS')
            if current.get('position_id') == position_id and current.get('price') == canon(price):
                return 'ALREADY_PRESENT'      # attached with the entry order
            raise LabBlocked('STOP_CONFLICT_REVIEW')
        self._send('tpsl_place', {'symbol': self.intent.symbol, 'positionId': position_id,
                                  'slPrice': canon(price), 'slStopType': self.stop_type,
                                  'slOrderType': 'MARKET', 'slQty': canon(qty)})
        self._settle(lambda s: (s.get('stop') or {}).get('price') == canon(price))
        return 'PLACED'

    def attach_tp(self, position_id, number, price, qty):
        expected = {'position_id': position_id, 'price': canon(price), 'qty': canon(qty)}
        if self.snapshot().get('tps', {}).get(number) == expected:
            return 'ALREADY_PRESENT'
        self._send('tpsl_place', {'symbol': self.intent.symbol, 'positionId': position_id,
                                  'tpPrice': canon(price), 'tpStopType': self.stop_type,
                                  'tpOrderType': 'MARKET', 'tpQty': canon(qty)})
        self._settle(lambda s: s.get('tps', {}).get(number) == expected)
        return 'PLACED'

    def move_stop(self, position_id, price, qty):
        snap = self.snapshot()
        stop = snap.get('stop')
        if not stop or stop.get('ambiguous') or not self.stop_id or stop.get('position_id') != position_id:
            raise LabBlocked('STOP_NOT_IDENTIFIED')
        if stop.get('price') == canon(price) and stop.get('qty') == canon(qty):
            return 'ALREADY_PRESENT'
        if self.stop_kind == 'QTY':
            self._send('tpsl_modify', {'orderId': self.stop_id, 'slPrice': canon(price),
                                       'slStopType': self.stop_type, 'slOrderType': 'MARKET',
                                       'slQty': canon(qty)})
        else:
            self._send('tpsl_position_modify', {'symbol': self.intent.symbol, 'positionId': position_id,
                                                'slPrice': canon(price), 'slStopType': self.stop_type})
        self._settle(lambda s: (s.get('stop') or {}).get('price') == canon(price))
        return 'MODIFIED'
