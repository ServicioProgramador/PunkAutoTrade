"""F16 Bitunix Futures WRITE contract compiler. OFFLINE ONLY.

This module deliberately has NO HTTP transport or exchange credential lookup.
Preparing a signed envelope is not permission to send it. Nothing here can
transmit, change account state, or turn on LIVE.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Mapping

from punk.live_write_lab import Intent, LabBlocked, positive

PATH_PLACE = '/api/v1/futures/trade/place_order'
PATH_TPSL = '/api/v1/futures/tpsl/place_order'
PATH_LOOKUP = '/api/v1/futures/trade/get_order_detail'
PATH_PENDING = '/api/v1/futures/trade/get_pending_orders'
PATH_PROTECTIONS = '/api/v1/futures/tpsl/get_pending_orders'


def _amount(raw):
    d = positive(raw)
    return format(d, 'f')


def _canonical(data: Mapping):
    if not isinstance(data, dict) or any(not isinstance(k, str) or not k for k in data):
        raise LabBlocked('INVALID_PAYLOAD')
    # JSON serialization must be IDENTICAL to bytes signed and sent (if a future
    # separately-reviewed transport is ever implemented).
    return json.dumps(data, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def signature(api_key, secret, nonce, timestamp, body='', params=None):
    if not all(isinstance(x, str) and x for x in (api_key, secret, nonce, timestamp)):
        raise LabBlocked('MISSING_SIGNING_INPUT')
    if not re.fullmatch(r'\d{13}', timestamp):
        raise LabBlocked('INVALID_TIMESTAMP')
    if len(nonce) >= 64:
        raise LabBlocked('INVALID_NONCE')
    if not isinstance(body, str):
        raise LabBlocked('INVALID_BODY')
    params = params or {}
    if not isinstance(params, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in params.items()):
        raise LabBlocked('INVALID_PARAMS')
    query = ''.join(k + params[k] for k in sorted(params))
    return _sha(_sha(nonce + timestamp + api_key + query + body) + secret)


@dataclass(frozen=True)
class OfflineEnvelope:
    method: str
    path: str
    body: str
    signed_digest: str
    # Intentionally does not retain the api key, secret, or full headers.
    # No send/request/submit method is implemented.


def compile_offline(method, path, data, *, api_key, secret, nonce, timestamp):
    if (method, path) not in {('POST', PATH_PLACE), ('POST', PATH_TPSL), ('GET', PATH_LOOKUP),
                              ('GET', PATH_PENDING), ('GET', PATH_PROTECTIONS)}:
        raise LabBlocked('ENDPOINT_FORBIDDEN')
    if method == 'POST':
        body = _canonical(data)
        params = {}
    else:
        if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
            raise LabBlocked('INVALID_PARAMS')
        body, params = '', data
    digest = signature(api_key, secret, nonce, timestamp, body, params)
    return OfflineEnvelope(method, path, body, digest)


def _validate_signal(intent):
    if not isinstance(intent, Intent):
        raise LabBlocked('INVALID_INTENT')
    intent.validate()
    if not re.fullmatch(r'[A-Z0-9]{2,30}USDT', intent.symbol):
        raise LabBlocked('INVALID_SYMBOL')
    cid = intent.client_id()
    if len(cid) > 64:
        raise LabBlocked('INVALID_CLIENT_ID')
    return cid


def entry_contract(intent, *, order_type='LIMIT'):
    """One entry with an attached mandatory full-size SL; TPs follow separately.

    The position ID, fill status and exchange-confirmed protections are NOT known
    here and are therefore never fabricated.
    """
    client_id = _validate_signal(intent)
    if order_type not in ('LIMIT', 'MARKET'):
        raise LabBlocked('INVALID_ORDER_TYPE')
    payload = {
        'symbol': intent.symbol, 'side': 'BUY' if intent.side == 'LONG' else 'SELL',
        'tradeSide': 'OPEN', 'orderType': order_type, 'qty': _amount(intent.quantity),
        'clientId': client_id, 'reduceOnly': False,
        'slPrice': _amount(intent.stop), 'slStopType': 'MARK_PRICE', 'slOrderType': 'MARKET',
    }
    if order_type == 'LIMIT':
        payload.update({'price': _amount(intent.entry), 'effect': 'GTC'})
    return payload


def protection_plan(intent, *, position_id, tp_weights=None, quantity_step=None):
    """Compile offline TP orders; SL is attached to entry and remains mandatory.

    Quantity quantization must be verified from the real contract. Refuses
    default precision assumptions, over-allocation or zero TP quantities.
    """
    _validate_signal(intent)
    if not isinstance(position_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', position_id):
        raise LabBlocked('POSITION_ID_REQUIRED')
    if quantity_step is None:
        raise LabBlocked('CONTRACT_QTY_STEP_REQUIRED')
    step = positive(quantity_step)
    quantity = positive(intent.quantity)
    if quantity % step != 0:
        raise LabBlocked('QUANTITY_PRECISION_INVALID')
    tps = tuple(intent.take_profits)
    if tp_weights is None:
        raise LabBlocked('TP_WEIGHTS_REQUIRED')
    if len(tp_weights) != len(tps):
        raise LabBlocked('INVALID_TP_WEIGHTS')
    weights = tuple(positive(v) for v in tp_weights)
    if sum(weights) != Decimal('1'):
        raise LabBlocked('INVALID_TP_WEIGHTS')
    units = int(quantity / step)
    portions = [int(Decimal(units) * w) for w in weights[:-1]]
    portions.append(units - sum(portions))
    if any(v <= 0 for v in portions):
        raise LabBlocked('TP_QUANTITY_TOO_SMALL')
    if sum(portions) != units:
        raise LabBlocked('OVERALLOCATION')
    return tuple({
        'symbol': intent.symbol, 'positionId': position_id,
        'tpPrice': _amount(price), 'tpStopType': 'MARK_PRICE',
        'tpOrderType': 'MARKET', 'tpQty': _amount(step * units_part)
    } for price, units_part in zip(tps, portions))


def lookup_contract(client_id):
    if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', client_id):
        raise LabBlocked('INVALID_CLIENT_ID')
    return {'clientId': client_id}


def validate_entry_ack(reply, expected_client_id):
    if not isinstance(reply, dict) or type(reply.get('code')) is not int or reply['code'] != 0:
        raise LabBlocked('ENTRY_UNCONFIRMED')
    payload = reply.get('data')
    if not isinstance(payload, dict) or not str(payload.get('orderId', '')).strip():
        raise LabBlocked('ENTRY_UNCONFIRMED')
    if payload.get('clientId') != expected_client_id:
        raise LabBlocked('CLIENT_ID_MISMATCH')
    # Ack != fill or position or confirmed protections.
    return {'order_id': str(payload['orderId']), 'state': 'ACK_ONLY', 'protected': False}


def validate_lookup(reply, expected_client_id, symbol):
    if not isinstance(reply, dict) or type(reply.get('code')) is not int or reply['code'] != 0:
        return {'state': 'UNKNOWN', 'allow_retry': False}
    data = reply.get('data')
    if not isinstance(data, dict) or data.get('clientId') != expected_client_id or data.get('symbol') != symbol:
        return {'state': 'UNKNOWN', 'allow_retry': False}
    if not data.get('orderId') or data.get('status') not in ('NEW', 'PART_FILLED', 'FILLED', 'CANCELED'):
        return {'state': 'UNKNOWN', 'allow_retry': False}
    return {'state': data['status'], 'order_id': str(data['orderId']), 'allow_retry': False,
            'protected': False}


def offline_contract_report():
    sample = Intent('offline-f16', 'RUNEUSDT', 'SHORT', '10', '0.4785', '0.487456',
                    ('0.470246', '0.469008', '0.467584', '0.465947', '0.464063'))
    entry = entry_contract(sample)
    tps = protection_plan(sample, position_id='SYNTHETIC_POSITION',
                          tp_weights=('0.2',)*5, quantity_step='0.01')
    return {'build': 'F16', 'mode': 'OFFLINE_CONTRACT_ONLY', 'entry_fields': sorted(entry),
            'tp_plan_count': len(tps), 'contains_sl': 'slPrice' in entry,
            'can_send_orders': False, 'network_calls': 0, 'live_enabled': False,
            'ready_for_live': False}
