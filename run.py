import argparse
import asyncio
import os
import threading
from pathlib import Path
from punk.core import Engine, Repository, PaperExchange
from punk.config import load_local_env
from punk.dashboard import serve
from punk.telegram import run_listener
from punk.bitunix_readonly import ReadOnlyBitunix, Monitor


def main():
    load_local_env()
    p = argparse.ArgumentParser(description='Punk AutoTrade F58 - PAPER + GET-only + LIVE manual/automatico armado')
    p.add_argument('--market-feed', action='store_true', help='Leer ticker Futures publico de Bitunix para simbolos LIMIT PAPER; NO activa entradas')
    p.add_argument('--market-feed-paper', action='store_true', help='Autoriza activaciones simuladas con snapshots publicos; requiere --telegram --telegram-paper')
    p.add_argument('--bitunix-reconcile', nargs=2, metavar=('CLIENT_ID', 'SYMBOL'), help='Consultar identidad y exposicion de una orden mediante GET-only; NO reintenta ni modifica')
    p.add_argument('--bitunix-reconcile-demo', action='store_true', help='Estado seguro F17, sin red ni credenciales')
    p.add_argument('--bitunix-recovery-report', action='store_true', help='Informe offline F19 sin red ni escritura')
    p.add_argument('--bitunix-recovery-file', metavar='JSON', help='Auditar intención JSON identificada con Bitunix GET-only y journal local; NO envía órdenes')
    p.add_argument('--bitunix-protection-audit', action='store_true', help='Informe seguro F18 offline; sin escrituras ni consultas de cuenta')
    p.add_argument('--bitunix-write-contract', action='store_true', help='Compilar contratos F16 offline; nunca envia ordenes')
    p.add_argument('--bitunix-transport-preflight', action='store_true', help='F21 validar limite de transporte con GET real solamente; no envia ordenes')
    p.add_argument('--bitunix-transport-lab', action='store_true', help='F21 validar contratos de escritura offline, sin credenciales ni red')
    p.add_argument('--integrated-execution-lab', action='store_true', help='F20 extremo a extremo sintético; sin red, credenciales ni órdenes reales')
    p.add_argument('--live-write-lab', action='store_true', help='Rehearsal de escrituras 100% sintetico, sin API ni ordenes reales')
    g = p.add_argument_group('F56 LIVE controlado (una operacion por aprobacion)')
    g.add_argument('--live-check', metavar='SYMBOL', help='Preflight GET-only real: contrato, tiers, modo de margen, ticker, sizing y bloqueos; no envia nada')
    g.add_argument('--live-status', action='store_true', help='Estado de operaciones LIVE (SQLite local, sin red)')
    g.add_argument('--live-plan', nargs=2, metavar=('SYMBOL', 'SIDE'), help='Planificar operacion manual; requiere --sl y --tp')
    g.add_argument('--live-plan-message', type=int, metavar='ID', help='Planificar desde un mensaje del canal autorizado (no ejecuta)')
    g.add_argument('--sl', help='Stop Loss para --live-plan')
    g.add_argument('--tp', help='TPs separados por coma para --live-plan (max 5)')
    g.add_argument('--entry', default=None, help='MARKET o precio LIMIT (por defecto: el de la senal)')
    g.add_argument('--live-approve', metavar='CLIENT_ID', help='Emitir token de un solo uso para un plan')
    g.add_argument('--live-execute', metavar='CLIENT_ID', help='Enviar la orden REAL aprobada; requiere --token y confirmacion escrita')
    g.add_argument('--token', help='Token de --live-approve')
    g.add_argument('--live-reconcile', nargs='?', const='ALL', metavar='CLIENT_ID', help='Conciliar por GET y completar protecciones/BE')
    g.add_argument('--live-resolve', metavar='CLIENT_ID', help='Cerrar registro bloqueante SOLO si GET prueba ausencia de exposicion')
    g.add_argument('--live-emergency-close', metavar='CLIENT_ID', help='Contingencia: cierre flash de la posicion de esa operacion')
    g.add_argument('--live-permission-check', metavar='SYMBOL', help='Verificar permiso de trading: fija el apalancamiento ACTUAL (sin ordenes)')
    g.add_argument('--live-arm', choices=('SHADOW', 'LIVE'), help='Armar el automatico Telegram->LIVE (SHADOW no envia); pide confirmacion escrita')
    g.add_argument('--live-disarm', action='store_true', help='Desarmar el automatico (no toca posiciones abiertas)')
    g.add_argument('--live-auto-status', action='store_true', help='Estado del automatico y ultimas decisiones')
    g.add_argument('--max-open', default='2', help='Automatico: maximo simultaneas, o AUTO = segun saldo real')
    g.add_argument('--max-day', default='3', help='Automatico: maximo de entradas por dia UTC, o AUTO = sin limite')
    g.add_argument('--notify-setup', action='store_true', help='Mostrar el chat id de tu bot de notificaciones (PA_NOTIFY_BOT_TOKEN)')
    g.add_argument('--notify-test', action='store_true', help='Enviar un mensaje de prueba a tu Telegram')
    g.add_argument('--entry-policy', default='AUTO', choices=('AUTO', 'LIMIT', 'MARKET'), help='AUTO: MARKET si el precio esta a <= --max-dev %% de la entrada, si no LIMIT')
    g.add_argument('--max-dev', default='0.3', help='Automatico: desvio maximo %% para entrar a MARKET en politica AUTO')
    g.add_argument('--tp-mode', default='LAST', choices=('LAST', 'SPLIT'), help='LAST: un TP en el ultimo objetivo; SPLIT: hasta 5 TP parciales')
    g.add_argument('--live-auto', action='store_true', help='Con --telegram y --live-supervise: ejecutar senales nuevas segun el armado')
    g.add_argument('--live-supervise', action='store_true', help='Con el dashboard: supervisar operaciones LIVE existentes (protecciones y BE)')
    p.add_argument('--f55-contract-lab', action='store_true', help='F55 validar contratos SL/TP/BE y lectura GET local; sin LIVE')
    p.add_argument('--f54-lab', action='store_true', help='F54 seguimiento SL/TP/BE con lectura local y estados persistentes; cero ordenes reales')
    p.add_argument('--f52-lab', action='store_true', help='F52 permiso individual consumido antes de solicitud local; LIVE bloqueado')
    p.add_argument('--f51-lab', action='store_true', help='F51 autorizacion individual offline de un solo uso; sin LIVE')
    p.add_argument('--f50-lab', action='store_true', help='F50 auditoria pos-ACK sintetica y checkpoint persistente temporal; sin LIVE')
    p.add_argument('--f49-lab', action='store_true', help='F49 ACK sintetico a GET simulado y recuperacion; LIVE deshabilitado')
    p.add_argument('--f48-lab', action='store_true', help='F48 circuito firmado local, SQLite temporal, sin LIVE')
    p.add_argument('--f47-watch', metavar='SYMBOL', help='F47 observar cambios de posiciones/protecciones mediante GET-only; nunca envia ordenes')
    p.add_argument('--f47-samples', type=int, default=2, help='Numero de snapshots F47 (1-20; predeterminado 2)')
    p.add_argument('--f46-current-guard', metavar='SYMBOL', help='F46 auditoria GET-only de exposicion y SL/TP actuales; nunca envia ordenes')
    p.add_argument('--f45-chain-audit', metavar='SYMBOL', help='F45 cruzar ejecuciones, ordenes y posicion historica via GET-only; LIVE bloqueado')
    p.add_argument('--f44-order-audit', metavar='SYMBOL', help='F44 GET-only contrastar ejecuciones con ordenes historicas; LIVE bloqueado')
    p.add_argument('--f43-full-audit', metavar='SYMBOL', help='F43 GET-only auditar todas las posiciones historicas TP/SL; sin LIVE')
    p.add_argument('--f42-position-audit', metavar='SYMBOL', help='F42 GET-only por positionId; candidatos no prueban cierres ni LIVE')
    p.add_argument('--f41-link', metavar='SYMBOL', help='F41 comparar IDs de trades y TP/SL históricos por GET-only; no modifica SQLite')
    p.add_argument('--f40-history', metavar='SYMBOL', help='F40 leer ejecuciones Futures por GET-only, sin órdenes ni cambios en SQLite')
    p.add_argument('--f36-check', metavar='SYMBOL', help='F36 auditoria real GET-only para un contrato; no envia ordenes')
    p.add_argument('--live-preflight', action='store_true', help='Auditar preparacion LIVE usando solo GET; NO habilita trading')
    p.add_argument('--bitunix-probe', action='store_true', help='Inspeccion segura de formatos GET-only sin mostrar datos de cuenta')
    p.add_argument('--paper-tick', nargs=2, metavar=('SYMBOL','PRICE'), help='Cotización MANUAL simulada para activar LIMIT PAPER pendientes')
    p.add_argument('--demo', action='store_true', help='Señal PAPER de ejemplo')
    p.add_argument('--telegram-history', action='store_true', help='Leer ultimos 40 mensajes por canal; sin escribir ni operar')
    p.add_argument('--telegram-inspect', type=int, metavar='ID', help='Ver texto de un mensaje autorizado y su clasificacion, sin ejecutar')
    p.add_argument('--telegram-channels', action='store_true', help='Listar ID de chats/canales accesibles; no lee mensajes')
    p.add_argument('--telegram-paper', action='store_true', help='Activar conversion de mensajes nuevos a PAPER; LIVE sigue bloqueado')
    p.add_argument('--telegram-login', action='store_true', help='Autorización interactiva Telethon por única vez')
    p.add_argument('--telegram', action='store_true', help='Escuchar canales Telegram autorizados')
    p.add_argument('--bitunix-read', action='store_true', help='Consultar Bitunix GET-only')
    p.add_argument('--port', type=int, default=8766)
    args = p.parse_args()
    # Show the exact source tree for diagnosis; never print .env or credentials.
    from punk.dashboard import BUILD
    if not (args.telegram_login or args.telegram_history or args.telegram_inspect is not None or args.telegram_channels or args.bitunix_probe):
        print(f'PUNK_START_BUILD={BUILD} source={Path(__file__).resolve().parent}', flush=True)
    if args.notify_setup or args.notify_test:
        from punk.notify import TelegramNotifier, discover_chat_ids
        token = os.getenv('PA_NOTIFY_BOT_TOKEN', '')
        if not token:
            print('NOTIFY: falta PA_NOTIFY_BOT_TOKEN en .env (crealo con @BotFather)', flush=True)
            raise SystemExit(2)
        if args.notify_setup:
            chats = discover_chat_ids(token)
            if not chats:
                print('NOTIFY: no hay chats. Abri tu bot en Telegram, toca Iniciar/Start y repeti.', flush=True)
            for cid, name in chats.items():
                print('NOTIFY_CHAT_ID', cid, name, '-> agregar a .env: PA_NOTIFY_CHAT_ID=%s' % cid, flush=True)
        else:
            n = TelegramNotifier.from_env()
            ok = bool(n) and n.send('🔔 Punk AutoTrade: notificaciones funcionando')
            print('NOTIFY_TEST', 'OK' if ok else 'FALLO (revisar PA_NOTIFY_CHAT_ID y que el bot este iniciado)', flush=True)
        return
    live_cmd = (args.live_check or args.live_status or args.live_plan or args.live_plan_message or args.live_approve or args.live_execute
                or args.live_reconcile or args.live_resolve or args.live_emergency_close or args.live_permission_check
                or args.live_arm or args.live_disarm or args.live_auto_status)
    if live_cmd:
        from punk import live_cli
        repo = Repository(Path(os.environ.get('PA_DB_PATH', 'data/punk_paper.db')))
        needs_trader = bool(args.live_execute or args.live_reconcile or args.live_emergency_close
                            or args.live_permission_check)
        if args.live_arm or args.live_disarm or args.live_auto_status:
            import json
            from punk.live_auto import AutoTrader
            auto = AutoTrader(live_cli.build_executor(repo, with_trader=False))
            if args.live_disarm:
                print('LIVE_AUTO', json.dumps(auto.disarm('OPERATOR_CLI'), sort_keys=True), flush=True)
            elif args.live_auto_status:
                st = auto.status()
                # The worker runs inside the dashboard process, not in this CLI process.
                st.pop('worker', None); st.pop('queue', None)
                print('LIVE_AUTO_STATUS', json.dumps(st, sort_keys=True, default=str), flush=True)
            else:
                live_cli.cmd_arm(auto, args)
            return
        try:
            executor = live_cli.build_executor(repo, with_trader=needs_trader)
        except Exception as exc:
            print('LIVE_UNAVAILABLE', type(exc).__name__, str(exc)[:80], flush=True)
            raise SystemExit(2)
        if args.live_check:
            import json
            print('LIVE_CHECK', json.dumps(executor.check(args.live_check.upper()), sort_keys=True), flush=True)
        elif args.live_status:
            import json
            print('LIVE_STATUS', json.dumps(executor.status(), sort_keys=True), flush=True)
        elif args.live_plan:
            if not args.sl or not args.tp:
                p.error('--live-plan requiere --sl y --tp')
            import time as _t
            sig = live_cli.manual_signal(args.live_plan[0], args.live_plan[1], args.sl, args.tp, args.entry or 'MARKET')
            live_cli.cmd_plan(executor, sig, 'manual:%s:%d' % (sig.symbol, int(_t.time())))
        elif args.live_plan_message:
            from punk.telegram import fetch_message
            found = asyncio.run(fetch_message(args.live_plan_message))
            if not found:
                print('LIVE_PLAN_REFUSED {"reason": "MESSAGE_NOT_FOUND"}', flush=True)
                return
            channel, text, _date = found
            entry = args.entry.upper() if args.entry and args.entry.upper() == 'MARKET' else None
            live_cli.cmd_plan_text(executor, text, '%s:%d' % (channel, args.live_plan_message), entry)
        elif args.live_approve:
            live_cli.cmd_approve(executor, args.live_approve)
        elif args.live_execute:
            if not args.token:
                p.error('--live-execute requiere --token')
            live_cli.cmd_execute(executor, args.live_execute, args.token)
        elif args.live_reconcile:
            live_cli.cmd_reconcile(executor, None if args.live_reconcile == 'ALL' else args.live_reconcile)
        elif args.live_resolve:
            live_cli.cmd_resolve(executor, args.live_resolve)
        elif args.live_emergency_close:
            live_cli.cmd_emergency_close(executor, args.live_emergency_close)
        elif args.live_permission_check:
            live_cli.cmd_permission_check(executor, args.live_permission_check)
        return
    if args.f55_contract_lab:
        import json
        from punk.f55_protection_adapter import lab_report
        print('F55_PROTECTION_CONTRACT', json.dumps(lab_report(),sort_keys=True),flush=True)
        return
    if args.f54_lab:
        import json
        from punk.f54_protection_cycle import lab_report
        print('F54_PROTECTION_CYCLE', json.dumps(lab_report(),sort_keys=True),flush=True)
        return
    if args.f52_lab:
        import json
        from punk.f52_authorized_commission import lab_report
        print('F52_COMMISSION', json.dumps(lab_report(), sort_keys=True), flush=True)
        return
    if args.f51_lab:
        import json
        from punk.f51_one_shot_authorization import lab_report
        print('F51_APPROVAL', json.dumps(lab_report(), sort_keys=True), flush=True)
        return
    if args.f50_lab:
        import json
        from punk.f50_protection_supervisor import lab_report
        print('F50_SUPERVISOR', json.dumps(lab_report(), sort_keys=True), flush=True)
        return
    if args.f49_lab:
        import json
        from punk.f49_post_ack import lab_report
        print('F49_RECONCILIATION', json.dumps(lab_report(), sort_keys=True), flush=True)
        return
    if args.f48_lab:
        import json
        from punk.f48_one_shot import lab_report
        print('F48_COMMISSION', json.dumps(lab_report(), sort_keys=True), flush=True)
        return
    if args.f47_watch:
        import json
        from punk.f47_position_watch import watch_samples
        result = watch_samples(ReadOnlyBitunix(), args.f47_watch, samples=args.f47_samples)
        print('F47_POSITION_WATCH', json.dumps(result,sort_keys=True),flush=True)
        return
    if args.f46_current_guard:
        import json
        from punk.f46_live_guard import audit_current
        result = audit_current(ReadOnlyBitunix(), args.f46_current_guard)
        print('F46_CURRENT_GUARD',json.dumps(result,sort_keys=True),flush=True)
        return
    if args.f45_chain_audit:
        import json
        from punk.f45_execution_chain import audit_execution_chain
        print('F45_CHAIN_AUDIT', json.dumps(audit_execution_chain(ReadOnlyBitunix(), args.f45_chain_audit), sort_keys=True),flush=True)
        return
    if args.f44_order_audit:
        import json
        from punk.f44_order_evidence import audit_orders
        print('F44_ORDER_AUDIT', json.dumps(audit_orders(ReadOnlyBitunix(),args.f44_order_audit),sort_keys=True),flush=True)
        return
    if args.f43_full_audit:
        import json
        from punk.f43_full_position_audit import audit_all
        print('F43_FULL_AUDIT', json.dumps(audit_all(ReadOnlyBitunix(), args.f43_full_audit), sort_keys=True), flush=True)
        return
    if args.f42_position_audit:
        import json
        from punk.f42_position_audit import audit
        print('F42_POSITION_AUDIT', json.dumps(audit(ReadOnlyBitunix(), args.f42_position_audit), sort_keys=True), flush=True)
        return
    if args.f41_link:
        import json
        from punk.f41_linkage import report_linkage
        print('F41_LINKAGE', json.dumps(report_linkage(ReadOnlyBitunix(), args.f41_link), sort_keys=True), flush=True)
        return
    if args.f40_history:
        import json
        from punk.f40_history import report_history
        result=report_history(ReadOnlyBitunix(), args.f40_history)
        print('F40_HISTORY',json.dumps(result,sort_keys=True),flush=True)
        return
    if args.f36_check:
        import json
        from punk.f36_commission import commission_check
        from punk.risk_config import current
        repo = Repository(Path(os.environ.get('PA_DB_PATH', 'data/punk_paper.db')))
        settings = current(repo)
        try:
            leverage = int(settings['leverage'])
        except (ValueError, KeyError, TypeError):
            leverage = 0
        result = commission_check(ReadOnlyBitunix(), margin_usdt=settings.get('margin', '5'),
                                  leverage=leverage, symbol=args.f36_check)
        print('F36_COMMISSION', json.dumps(result, sort_keys=True), flush=True)
        return
    if args.bitunix_transport_lab:
        from punk.bitunix_transport_preflight import offline_report
        import json
        print('BITUNIX_F21', json.dumps(offline_report(), sort_keys=True), flush=True)
        return
    if args.bitunix_transport_preflight:
        from punk.bitunix_transport_preflight import real_get_boundary_probe
        import json
        print('BITUNIX_F21', json.dumps(real_get_boundary_probe(), sort_keys=True), flush=True)
        return
    if args.bitunix_recovery_report:
        from punk.bitunix_recovery import offline_report
        import json
        print('BITUNIX_F19', json.dumps(offline_report(), sort_keys=True), flush=True)
        return
    if args.bitunix_recovery_file:
        from punk.bitunix_recovery import RecoveryJournal, recover
        import json
        try:
            filepath = Path(args.bitunix_recovery_file)
            if filepath.stat().st_size > 8192:
                raise ValueError('INVALID_RECOVERY_INTENT_FILE')
            intent = json.loads(filepath.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            print('BITUNIX_F19', json.dumps({'state':'INVALID_RECOVERY_INTENT_FILE', 'allow_retry':False,
                  'ready_for_live':False, 'live_enabled':False}, sort_keys=True), flush=True)
            return
        # Dedicated journal, never use PAPER operational database.
        journal = RecoveryJournal(Path('data/bitunix_recovery.db'))
        result = recover(journal, intent)
        print('BITUNIX_F19', json.dumps(result, sort_keys=True), flush=True)
        return
    if args.bitunix_protection_audit:
        from punk.bitunix_protection_audit import offline_report
        import json
        print('BITUNIX_F18', json.dumps(offline_report(), sort_keys=True), flush=True)
        return
    if args.bitunix_reconcile_demo:
        from punk.bitunix_reconciliation import report
        import json
        print('BITUNIX_F17', json.dumps(report(), sort_keys=True), flush=True)
        return
    if args.bitunix_reconcile:
        from punk.bitunix_reconciliation import report
        import json
        cid, symbol = args.bitunix_reconcile
        print('BITUNIX_F17', json.dumps(report(client_id=cid, symbol=symbol), sort_keys=True), flush=True)
        return
    if args.bitunix_write_contract:
        from punk.bitunix_write_contract import offline_contract_report
        import json
        print('BITUNIX_WRITE_CONTRACT', json.dumps(offline_contract_report(), sort_keys=True), flush=True)
        return
    if args.integrated_execution_lab:
        from punk.bitunix_integrated_lab import run_lab
        import json
        print('BITUNIX_F20', json.dumps(run_lab(),sort_keys=True),flush=True)
        return
    if args.live_write_lab:
        from punk.live_write_lab import run_lab
        import json
        print('SYNTHETIC_WRITE_LAB', json.dumps(run_lab(),sort_keys=True),flush=True)
        return
    if args.bitunix_probe:
        from punk.bitunix_readonly import probe_readonly
        import json
        print('BITUNIX_GET_ONLY_PROBE',json.dumps(probe_readonly(ReadOnlyBitunix()),sort_keys=True),flush=True)
        return
    if args.telegram_login:
        from punk.telegram import login_once
        asyncio.run(login_once())
        return
    if args.telegram_inspect is not None:
        from punk.telegram import inspect_message
        asyncio.run(inspect_message(args.telegram_inspect))
        return
    if args.telegram_history:
        from punk.telegram import history_once
        try:
            asyncio.run(history_once())
        except RuntimeError as exc:
            if str(exc).startswith("CHANNEL_NOT_ACCESSIBLE"):
                print("TELEGRAM_HISTORY_UNAVAILABLE " + str(exc), flush=True)
                raise SystemExit(2)
            raise
        return
    if args.telegram_channels:
        from punk.telegram import list_channels
        asyncio.run(list_channels())
        return
    if args.market_feed_paper and not (args.market_feed and args.telegram and args.telegram_paper):
        p.error('--market-feed-paper requiere --market-feed --telegram --telegram-paper')
    if args.telegram_paper and not args.telegram:
        p.error('--telegram-paper requiere --telegram')
    repo = Repository(Path(os.environ.get('PA_DB_PATH', 'data/punk_paper.db')))
    if args.live_preflight:
        from punk.live_readiness import run_preflight
        import json
        report=run_preflight(ReadOnlyBitunix(),repo)
        print('LIVE_READINESS', json.dumps(report,sort_keys=True),flush=True)
        return
    from punk.telegram import accepted_channel_ids
    for env, key in [('PA_PAPER_MARGIN', 'margin'),
                     ('PA_PAPER_LEVERAGE', 'leverage')]:
        if os.environ.get(env):
            
            if repo.setting(key,'__UNSET__')=='__UNSET__': repo.set_setting(key, os.environ[env])
    engine = Engine(repo, PaperExchange(price=os.environ.get('PA_PAPER_PRICE', '66000')))
    if args.paper_tick:
        from punk.paper_limit import trigger
        symbol, price = args.paper_tick
        print('PAPER_TICK', trigger(engine, symbol, price), 'LIVE_LOCKED', flush=True)
        return
    integration = {'telegram': 'DISABLED'}
    if args.demo:
        channel = (repo.setting('channels', '') or 'demo').split(',')[0]
        repo.set_setting('channels', channel)
        result = engine.ingest(channel, 999999,
                               'LONG BTCUSDT\nEntrada: MARKET\nSL: 65000\nTP1: 67000\nTP2: 68000\nTP3: 69000')
        print('DEMO', result, flush=True)
    monitor = None
    if args.bitunix_read:
        monitor = Monitor(ReadOnlyBitunix())
        engine.account_monitor = monitor
        from punk.risk_config import PublicContractLimits
        engine.contract_limits=PublicContractLimits(ReadOnlyBitunix(), margin_source=lambda: repo.setting('margin','5'), leverage_source=lambda:repo.setting('leverage','20'))
        threading.Thread(target=monitor.run, daemon=True, name='bitunix-get-only').start()
    feed = None
    if args.market_feed:
        from punk.market_feed import PaperMarketFeed
        feed=PaperMarketFeed(engine, enabled=args.market_feed_paper)
        threading.Thread(target=feed.run, daemon=True, name='public-market-feed').start()
    live = None
    auto = None
    if args.live_auto and not (args.live_supervise and args.telegram):
        p.error('--live-auto requiere --telegram y --live-supervise')
    if args.live_supervise:
        from punk import live_cli
        try:
            live = live_cli.build_executor(repo, with_trader=True)
        except Exception as exc:
            print('LIVE_SUPERVISOR_UNAVAILABLE', type(exc).__name__, flush=True)
            raise SystemExit(2)
        threading.Thread(target=live_cli.supervise_forever, args=(live, 20, integration), daemon=True,
                         name='live-supervisor').start()
        print('LIVE supervisor activo: solo protege/concilia operaciones ya aprobadas; no abre entradas', flush=True)
        from punk.notify import TelegramNotifier, NotifyWatcher
        notifier = TelegramNotifier.from_env()
        if notifier is not None:
            threading.Thread(target=NotifyWatcher(live.store, notifier).run, daemon=True, name='notify').start()
            integration['notify'] = 'ACTIVE'
            print('NOTIFY activo: avisos a tu Telegram', flush=True)
        else:
            integration['notify'] = 'OFF'
        if args.live_auto:
            from punk.live_auto import AutoTrader
            auto = AutoTrader(live)
            threading.Thread(target=auto.worker, daemon=True, name='live-auto').start()
            st = auto.auto.settings()
            print('LIVE_AUTO worker activo; modo=%s max_open=%s max_day=%s entrada=%s tp=%s' % (
                st['mode'], st['max_open'], st['max_per_day'], st['entry_policy'], st['tp_mode']), flush=True)
    else:
        from punk.live_executor import LiveStore
        live_db = Path(os.environ.get('PA_LIVE_DB_PATH', 'data/punk_live.db'))
        if live_db.exists():
            live = LiveStore(live_db)
    if args.telegram:
        # Configuration is validated by the listener against accessible dialogs.
        # No persisted channel or legacy demo setting can override the allowlist.
        integration['telegram'] = 'CONNECTING'
        threading.Thread(target=run_listener, args=(engine, integration, args.telegram_paper, auto),
                         daemon=True, name='telegram-paper').start()
    serve(engine, port=args.port, integration=integration, monitor=monitor, feed=feed, live=live, auto=auto)


if __name__ == '__main__':
    main()
