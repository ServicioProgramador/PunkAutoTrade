import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BUILD = 'F75'


def create_server(engine, host='127.0.0.1', port=8766, integration=None, monitor=None, feed=None, live=None, auto=None):
    html = (Path(__file__).resolve().parent.parent / 'web' / 'index.html').read_bytes()
    integration = integration if integration is not None else {}
    settings_nonce=secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/api/status':
                snapshot = engine.snapshot()
                snapshot['build'] = BUILD
                snapshot['settings_nonce'] = settings_nonce
                snapshot['telegram'] = integration.get('telegram', 'DISABLED')
                snapshot['telegram_activity'] = dict(integration.get('telegram_activity', {'received':0}))
                snapshot['bitunix'] = 'GET_ONLY' if monitor else 'DISABLED'
                snapshot['bitunix_readonly'] = monitor.snapshot() if monitor else {'status': 'DISABLED'}
                snapshot['market_feed'] = feed.snapshot() if feed else {'status':'DISABLED'}
                payload = json.dumps(snapshot).encode()
                mime = 'application/json; charset=utf-8'
            elif path == '/api/capabilities':
                # live_enabled refers to AUTOMATIC Telegram->LIVE, which stays OFF in F56.
                payload = json.dumps({'build': BUILD, 'mode': 'PAPER_AUTO_LIVE_MANUAL_ONE_SHOT', 'live_enabled': False, 'capabilities': {'telegram_paper': 'AVAILABLE', 'bitunix_readonly': 'AVAILABLE' if monitor else 'NOT_ACTIVE', 'https_write_contract': 'ACTIVE_ALLOWLIST', 'durable_write_journal': 'ACTIVE', 'real_order_submission': 'ONE_SHOT_OPERATOR_APPROVAL', 'real_protection_confirmation': 'GET_READBACK_REQUIRED', 'live_supervisor': 'ACTIVE' if hasattr(live, 'reconcile_all') else 'OFF', 'telegram_live_automation': 'WORKER_RUNNING' if auto is not None else 'DISABLED'}, 'ready_for_live': False}).encode()
                mime = 'application/json; charset=utf-8'
            elif path == '/api/live/auto':
                from punk.live_auto import auto_payload
                store = live.store if hasattr(live, 'store') else live
                data = auto_payload(store)
                data['worker'] = 'RUNNING' if auto is not None else 'NOT_STARTED'
                payload = json.dumps(data, default=str).encode()
                mime = 'application/json; charset=utf-8'
            elif path == '/api/live/report':
                from urllib.parse import parse_qs
                from punk.live_report import report
                q = parse_qs(urlsplit(self.path).query)
                store = live.store if hasattr(live, 'store') else live
                try:
                    end = int(q.get('to', [0])[0]) or int(__import__('time').time()) + 1
                    start = int(q.get('from', [0])[0]) or end - 86400
                    data = report(store, start, end) if store is not None else {'status': 'NO_LIVE_DB'}
                    code = 200
                except (ValueError, TypeError):
                    data, code = {'status': 'INVALID_RANGE'}, 400
                payload = json.dumps(data, default=str).encode()
                mime = 'application/json; charset=utf-8'
                if code != 200:
                    self.send_response(code); self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
                    return
            elif path == '/api/live':
                from punk.live_executor import dashboard_payload
                from punk.live_report import open_sessions_now
                data = dashboard_payload(live)
                data['sessions_now'] = open_sessions_now()
                data['notify'] = integration.get('notify', 'OFF')
                payload = json.dumps(data).encode()
                mime = 'application/json; charset=utf-8'
            elif path == '/api/build':
                payload = json.dumps({'build': BUILD, 'mode': 'PAPER_ONLY', 'live_enabled': False}).encode()
                mime = 'application/json; charset=utf-8'
            elif path == '/':
                payload = html
                mime = 'text/html; charset=utf-8'
            elif path == '/favicon.ico':
                self.send_response(204)
                self.end_headers()
                return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Punk-Build', BUILD)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            route = urlsplit(self.path).path
            if route not in ('/api/settings', '/api/live/disarm'):
                self.send_error(404); return
            # bind to loopback and require a per-process token from /api/status
            # plus same-origin check to reject cross-site form submission.
            if self.client_address[0] not in ('127.0.0.1','::1'):
                self.send_error(403); return
            origin=self.headers.get('Origin')
            expected=f'http://{host}:{self.server.server_address[1]}'
            if origin!=expected or self.headers.get('X-Punk-Settings-Nonce')!=settings_nonce:
                self.send_error(403); return
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                self.send_error(415); return
            if route == '/api/live/disarm':
                # Kill switch: the dashboard can only DISARM, never arm.
                store = live.store if hasattr(live, 'store') else live
                if store is None:
                    result, code = {'status': 'NO_LIVE_DB'}, 409
                else:
                    from punk.live_auto import AutoStore
                    a = AutoStore(store)
                    a.save(mode='OFF', disarm_reason='DASHBOARD')
                    a.log('-', 'DISARMED', {'reason': 'DASHBOARD'})
                    result, code = {'status': 'DISARMED'}, 200
                raw=json.dumps(result).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers();self.wfile.write(raw)
                return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 1<=size<=2048: raise ValueError('BAD_SIZE')
                payload=json.loads(self.rfile.read(size))
                from punk.risk_config import update
                updated=update(engine.repo,payload)
                result={'status':'SAVED','settings':updated,'live_enabled':False}
                code=200
            except (ValueError,TypeError,UnicodeDecodeError,json.JSONDecodeError) as exc:
                result={'status':'REJECTED','reason':str(exc)[:64]};code=400
            raw=json.dumps(result).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw)

    return ThreadingHTTPServer((host, port), Handler)


def serve(engine, host='127.0.0.1', port=8766, integration=None, monitor=None, feed=None, live=None, auto=None):
    server = create_server(engine, host, port, integration, monitor, feed, live, auto)
    print(f'PUNK_SERVER_BUILD={BUILD} host={host} port={server.server_address[1]}', flush=True)
    print(f'Dashboard PAPER + GET-only: http://{host}:{server.server_address[1]}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
