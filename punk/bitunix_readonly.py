"""Bitunix Futures GET-only adapter: no trading/write method exists here."""
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from decimal import Decimal, InvalidOperation

class BitunixContractError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


BASE = 'https://fapi.bitunix.com'
PATHS = {
    'contracts': '/api/v1/futures/market/trading_pairs',
    'tickers': '/api/v1/futures/market/tickers',
    'account': '/api/v1/futures/account',
    'positions': '/api/v1/futures/position/get_pending_positions',
    'orders': '/api/v1/futures/trade/get_pending_orders',
    'tpsl': '/api/v1/futures/tpsl/get_pending_orders',
    'history_trades': '/api/v1/futures/trade/get_history_trades',
    'tpsl_history': '/api/v1/futures/tpsl/get_history_orders',
    'history_orders': '/api/v1/futures/trade/get_history_orders',
    'order_detail': '/api/v1/futures/trade/get_order_detail',
    'leverage_margin_mode': '/api/v1/futures/account/get_leverage_margin_mode',
    'position_tiers': '/api/v1/futures/position/get_position_tiers',
    'history_positions': '/api/v1/futures/position/get_history_positions',
}
PRIVATE = {'account', 'positions', 'orders', 'tpsl', 'history_trades', 'tpsl_history', 'history_orders',
           'order_detail', 'leverage_margin_mode', 'history_positions'}


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sign(api_key, secret_key, nonce, timestamp, params):
    canonical = ''.join(f'{k}{params[k]}' for k in sorted(params))
    return sha(sha(nonce + timestamp + api_key + canonical) + secret_key)


def validate_response(body):
    if not isinstance(body, dict) or body.get('code') != 0 or 'data' not in body:
        if isinstance(body, dict) and isinstance(body.get('code'), (int, str)):
            code = str(body['code'])
            if code.isdigit() and len(code) <= 7:
                raise BitunixContractError('API_CODE_' + code)
        raise BitunixContractError('INVALID_API_RESPONSE')
    return body['data']


def _decimal(value):
    try:
        v = Decimal(str(value))
        return str(v) if v.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _pending_total(value):
    """Bitunix may encode pending-order total as an unsigned decimal string.

    Do not coerce floats, booleans, signed values, whitespace, or other payloads:
    these are contract errors, not proof of a complete order list.
    """
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value and value.isascii() and value.isdecimal():
        result = int(value)
    else:
        raise BitunixContractError('PENDING_ORDERS_INCOMPLETE')
    if not 0 <= result <= 10000:
        raise BitunixContractError('PENDING_ORDERS_INCOMPLETE')
    return result


class ReadOnlyBitunix:
    def __init__(self, api_key=None, secret_key=None, opener=None, base=BASE):
        if base != BASE and not base.startswith(('http://127.0.0.1:', 'https://127.0.0.1:')):
            raise ValueError('Destino no canonico')
        self.base = base
        self.api_key = api_key if api_key is not None else os.getenv('PA_BITUNIX_API_KEY', '')
        self.secret_key = secret_key if secret_key is not None else (os.getenv('PA_BITUNIX_API_SECRET') or os.getenv('PA_BITUNIX_SECRET_KEY', ''))
        self.opener = opener or build_opener(NoRedirect).open

    def get(self, endpoint, params=None):
        if endpoint not in PATHS:
            raise ValueError('Endpoint fuera de allowlist GET-only')
        params = dict(params or {})
        if any(not isinstance(k, str) or not isinstance(v, (str, int)) for k, v in params.items()):
            raise ValueError('Parametros invalidos')
        if endpoint in PRIVATE and not (self.api_key and self.secret_key):
            raise RuntimeError('Credenciales GET-only sin configurar')
        query = urlencode(sorted(params.items()))
        url = self.base + PATHS[endpoint] + ('?' + query if query else '')
        headers = {'Content-Type': 'application/json', 'language': 'en-US'}
        if endpoint in PRIVATE:
            nonce = secrets.token_hex(8)
            timestamp = str(int(time.time() * 1000))
            headers.update({'api-key': self.api_key, 'nonce': nonce,
                            'timestamp': timestamp,
                            'sign': sign(self.api_key, self.secret_key, nonce, timestamp, params)})
        request = Request(url, headers=headers, method='GET')
        # redirects disabled: avoid credentials being forwarded to an unrelated host
        response = self.opener(request, timeout=8)
        try:
            if response.geturl() != url:
                raise RuntimeError('Redireccion inesperada bloqueada')
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RuntimeError('Respuesta excede limite')
            data = json.loads(raw)
        finally:
            response.close()
        return validate_response(data)

    def public_contract(self, symbol='BTCUSDT'):
        data = self.get('contracts', {'symbols': symbol})
        if not isinstance(data, list):
            raise ValueError('Lista de contratos invalida')
        matches = [x for x in data if isinstance(x, dict) and x.get('symbol') == symbol]
        if not matches:
            raise ValueError('Contrato no encontrado')
        x = matches[0]
        return {k: x.get(k) for k in ('symbol', 'minTradeVolume', 'basePrecision', 'quotePrecision',
                                      'minLeverage', 'maxLeverage', 'symbolStatus', 'isApiSupported',
                                      'maxMarketOrderVolume', 'maxLimitOrderVolume')}

    def snapshot(self, symbol='BTCUSDT'):
        # Fail-closed: never report a partial set as reconciled.
        account = self.get('account', {'marginCoin': 'USDT'})
        if not isinstance(account, (list, dict)):
            raise BitunixContractError('INVALID_ACCOUNT_OR_POSITIONS')
        positions = self.get('positions')
        if not isinstance(positions, list):
            raise BitunixContractError('INVALID_POSITIONS_SHAPE')
        # Page until complete, using the official skip/limit parameters.
        # Any inconsistent page fails closed rather than showing a partial account as verified.
        order_list = []
        offset = 0
        for _page in range(100):
            page = self.get('orders', {'skip': offset, 'limit': 100})
            if not isinstance(page, dict) or not isinstance(page.get('orderList'), list):
                raise BitunixContractError('INVALID_PENDING_ORDERS')
            total = _pending_total(page.get('total'))
            batch = page['orderList']
            if len(batch) > 100 or offset + len(batch) > total:
                raise BitunixContractError('PENDING_ORDERS_INCOMPLETE')
            if total == 0 and batch:
                raise BitunixContractError('PENDING_ORDERS_INCOMPLETE')
            if any(not isinstance(x, dict) or not x.get('orderId') for x in batch):
                raise BitunixContractError('INVALID_PENDING_ORDERS')
            order_list.extend(batch)
            offset += len(batch)
            if offset == total:
                break
            if not batch:
                raise BitunixContractError('PENDING_ORDERS_INCOMPLETE')
        else:
            raise BitunixContractError('PENDING_ORDERS_INCOMPLETE')
        if len({str(x['orderId']) for x in order_list}) != len(order_list):
            raise BitunixContractError('PENDING_ORDERS_INCOMPLETE')

        tp_list = []
        offset = 0
        for _page in range(100):
            page = self.get('tpsl', {'skip': offset, 'limit': 100})
            if not isinstance(page, list):
                raise BitunixContractError('INVALID_PENDING_TPSL')
            if len(page) > 100 or any(not isinstance(x, dict) or not x.get('id') for x in page):
                raise BitunixContractError('INVALID_PENDING_TPSL')
            tp_list.extend(page)
            offset += len(page)
            if len(page) < 100:
                break
        else:
            raise BitunixContractError('PENDING_TPSL_INCOMPLETE')
        if len({str(x['id']) for x in tp_list}) != len(tp_list):
            raise BitunixContractError('PENDING_TPSL_INCOMPLETE')
        if isinstance(account, dict):
            # Some account representations wrap the account list; accept only
            # explicitly identified variants, never fabricate a balance.
            if isinstance(account.get('list'), list):
                account = account['list']
            elif isinstance(account.get('accounts'), list):
                account = account['accounts']
            elif account.get('marginCoin') == 'USDT':
                account = [account]
            else:
                raise BitunixContractError('INVALID_ACCOUNT_OR_POSITIONS')
        usdt = next((x for x in account if isinstance(x, dict) and x.get('marginCoin') == 'USDT'), None)
        if usdt is None or _decimal(usdt.get('available')) is None:
            raise BitunixContractError('USDT_BALANCE_UNAVAILABLE')
        return {
            'account': {'marginCoin': 'USDT', 'available': _decimal(usdt.get('available')),
                        'frozen': _decimal(usdt.get('frozen')), 'margin': _decimal(usdt.get('margin'))},
            'positions': [{k: x.get(k) for k in ('positionId','symbol','side','qty','margin','leverage','unrealizedPNL',
                                                 'avgOpenPrice','marginMode','positionMode')} for x in positions if isinstance(x,dict)],
            'pending_orders': [{k: x.get(k) for k in ('orderId','clientId','symbol','status','qty','reduceOnly')} for x in order_list if isinstance(x,dict)],
            'pending_tpsl': [{k: x.get(k) for k in ('id','positionId','symbol','tpPrice','slPrice','tpQty','slQty','slStopType','slOrderType','tpStopType','tpOrderType')} for x in tp_list if isinstance(x,dict)],
            'checked_at': int(time.time()), 'status': 'GET_ONLY_VERIFIED', 'trading_enabled': False,
        }


class Monitor:
    """Polls exchange independently from the PAPER engine and never sends orders."""
    def __init__(self, client, interval=30):
        self.client = client
        self.interval = max(15, int(interval))
        self.lock = threading.Lock()
        self.state = {'status': 'NOT_CHECKED', 'trading_enabled': False}

    def update(self):
        try:
            state = self.client.snapshot()
        except Exception as exc:
            # No secrets, URLs, request headers or response bodies in errors.
            state = {'status': 'ERROR', 'error_type': 'ValueError' if isinstance(exc, BitunixContractError) else type(exc).__name__,
                     'error_code': exc.reason if isinstance(exc, BitunixContractError) else
                                   ('AUTH_CONFIGURATION' if isinstance(exc, RuntimeError) else
                                    'NETWORK_OR_SCHEMA_ERROR'),
                     'trading_enabled': False}
        with self.lock:
            self.state = state
        return state

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
        if state.get('checked_at') and time.time() - state['checked_at'] > self.interval * 3:
            return {'status': 'STALE', 'trading_enabled': False}
        return state

    def run(self):
        while True:
            self.update()
            time.sleep(self.interval)


def probe_readonly(client):
    """Read-only per-endpoint shape inspection. No balances, IDs or payloads printed."""
    output={}
    checks=(('account',{'marginCoin':'USDT'}),('positions',{}),
            ('orders',{'skip':0,'limit':100}),('tpsl',{'skip':0,'limit':100}))
    for name,params in checks:
        try:
            data=client.get(name,params)
            status={'status':'OK','shape':type(data).__name__}
            if name=='orders' and isinstance(data,dict):
                status['total_type']=type(data.get('total')).__name__
                
                try:
                    _pending_total(data.get('total'))
                    status['total_valid']=True
                except BitunixContractError:
                    status['total_valid']=False
                status['page_size']=len(data['orderList']) if isinstance(data.get('orderList'),list) else None
                status['list_key_present']='orderList' in data
            if name in ('positions','tpsl') and isinstance(data,list):
                status['page_size']=len(data)
            if name=='account' and isinstance(data,dict):
                status['account_keys_present']={k:k in data for k in ('marginCoin','available','list','accounts')}
            output[name]=status
        except BitunixContractError as exc:
            output[name]={'status':'ERROR','code':exc.reason}
        except Exception as exc:
            output[name]={'status':'ERROR','code':type(exc).__name__}
    return output
