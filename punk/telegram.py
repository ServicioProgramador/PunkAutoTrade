"""Telegram authorized channel listener. Never calls Bitunix."""
import asyncio
import os
import time
import re
from datetime import datetime, timezone
from .core import parse


def normalized_channel_ids(csv):
    """Normalize channel peer IDs, including the IDs from t.me/c links.

    This is parsing, not authorization: membership is checked against dialogs.
    """
    if not isinstance(csv, str):
        raise ValueError('PA_CHANNELS_NOT_TEXT')
    ids = []
    for raw in csv.split(','):
        item = raw.strip().strip('\"\'')
        if not item:
            continue
        match = re.fullmatch(r'https?://t\.me/c/([0-9]{5,15})(?:/[0-9]+)?/?', item)
        if match:
            item = '-100' + match.group(1)
        elif re.fullmatch(r'[0-9]{5,15}', item):
            # A positive t.me/c channel fragment is not an already-marked peer ID.
            item = '-100' + item
        elif re.fullmatch(r'-100100[0-9]{5,15}', item):
            # Reject double-prefixed channel IDs rather than guessing authorization.
            raise ValueError('PA_CHANNELS_DOUBLE_PREFIX')
        if not re.fullmatch(r'-100[0-9]{5,15}', item, flags=re.ASCII):
            raise ValueError('PA_CHANNELS_BAD_FORMAT')
        ids.append(int(item))
    return sorted(set(ids))


def accepted_channel_ids(csv):
    """Strict legacy contract used in existing F1-F4 regression."""
    if not isinstance(csv, str):
        raise ValueError('PA_CHANNELS_NOT_TEXT')
    ids=[]
    for raw in csv.split(','):
        item=raw.strip()
        if not item:
            continue
        if not re.fullmatch(r'-100[0-9]+', item, re.ASCII):
            raise ValueError('PA_CHANNELS_BAD_FORMAT')
        ids.append(int(item))
    return sorted(set(ids))



def configured_channels(engine=None):
    """The current local configuration is authoritative, never stale SQLite."""
    raw = os.environ.get('PA_CHANNELS', '')
    if not raw.strip():
        # Explicitly do not silently re-authorize previously persisted channels.
        raise RuntimeError('CHANNELS_EMPTY')
    try:
        channels = normalized_channel_ids(raw)
    except ValueError as exc:
        raise RuntimeError('CHANNELS_BAD_FORMAT') from exc
    if not channels:
        raise RuntimeError('CHANNELS_EMPTY')
    if engine is not None:
        normalized = ','.join(map(str, channels))
        if engine.repo.setting('channels', '') != normalized:
            engine.repo.set_setting('channels', normalized)
    return channels


def marked_dialog_id(dialog):
    """Return a canonical peer ID (-100 for channels) regardless of Dialog.id style.

    The Telegram *entity* determines its type; never infer authorization from a
    positive numeric ID alone.
    """
    try:
        from telethon import utils
        return int(utils.get_peer_id(dialog.entity))
    except (ImportError, AttributeError, TypeError, ValueError):
        # Permits light-weight test doubles; real Telethon takes the path above.
        if not (getattr(dialog, 'is_channel', False) or getattr(dialog, 'is_group', False)):
            return None
        numeric = int(dialog.id)
        if getattr(dialog, 'is_channel', False) and numeric > 0:
            return int('-100' + str(numeric))
        return numeric


async def accessible_dialogs(client):
    """Use Telegram's marked peer IDs, never guesses from link/message IDs."""
    result = {}
    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            key = marked_dialog_id(dialog)
            if key is not None:
                result[key] = dialog
    return result


def missing_channel_detail(channels, dialogs):
    """Safe, actionable failure with a bounded list of accessible peer IDs."""
    missing = sorted(set(channels) - set(dialogs))
    available = sorted(dialogs)
    return ('CHANNEL_NOT_ACCESSIBLE configured=' + ','.join(map(str, missing))
            + ' available_count=' + str(len(available))
            + ' available_ids=' + ','.join(map(str, available[:20]))
            + ' (verify the Telegram account joined the private channel)')


async def history_once(limit=40):
    """Read recent messages in authorized channels only. No SQLite or PAPER writes."""
    from telethon import TelegramClient
    channels = configured_channels()
    api_id, api_hash = os.getenv('PA_TG_API_ID'), os.getenv('PA_TG_API_HASH')
    if not api_id or not api_hash:
        raise RuntimeError('TELEGRAM_CREDENTIALS_MISSING')
    session = os.getenv('PA_TG_SESSION', 'data/punk_telegram')
    async with TelegramClient(session, int(api_id), api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError('SESSION_NOT_AUTHORIZED')
        dialogs = await accessible_dialogs(client)
        missing = set(channels) - set(dialogs)
        if missing:
            raise RuntimeError(missing_channel_detail(channels, dialogs))
        count = 0
        for peer_id in channels:
            dialog = dialogs[peer_id]
            print(f'TELEGRAM_HISTORY channel={peer_id} limit={limit} mode=READ_ONLY', flush=True)
            async for msg in client.iter_messages(dialog.entity, limit=limit):
                if not msg or not (msg.raw_text or '').strip():
                    continue
                signal, classification, reason = parse(msg.raw_text)
                symbol = getattr(signal, 'symbol', None) if signal else None
                if not symbol:
                    match = re.search(r'(?i)\b(RUNE|[A-Z]{2,15}USDT)\b', msg.raw_text)
                    symbol = match.group(0).upper() if match else '-'
                print(f'HISTORY id={msg.id} utc={msg.date.isoformat()} classification={classification} symbol={symbol}', flush=True)
                count += 1
        print(f'TELEGRAM_HISTORY_COMPLETE messages={count} orders=0 database_changes=0', flush=True)
        return count



async def inspect_message(message_id):
    """Read one authorized Telegram message verbatim for parser validation.

    This function never touches SQLite, Bitunix or the PAPER executor.
    The content is printed only in the local terminal; users should redact
    private information before sharing it.
    """
    if not isinstance(message_id, int) or message_id < 1:
        raise ValueError('MESSAGE_ID_INVALID')
    from telethon import TelegramClient
    channels = configured_channels()
    api_id, api_hash = os.getenv('PA_TG_API_ID'), os.getenv('PA_TG_API_HASH')
    if not api_id or not api_hash:
        raise RuntimeError('TELEGRAM_CREDENTIALS_MISSING')
    session = os.getenv('PA_TG_SESSION', 'data/punk_telegram')
    async with TelegramClient(session, int(api_id), api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError('SESSION_NOT_AUTHORIZED')
        dialogs = await accessible_dialogs(client)
        missing = set(channels) - set(dialogs)
        if missing:
            raise RuntimeError(missing_channel_detail(channels, dialogs))
        for channel in channels:
            message = await client.get_messages(dialogs[channel].entity, ids=message_id)
            if not message or not (message.raw_text or '').strip():
                print(f'TELEGRAM_INSPECT_NOT_FOUND channel={channel} id={message_id}', flush=True)
                continue
            text = message.raw_text
            _signal, classification, reason = parse(text)
            print(f'TELEGRAM_INSPECT channel={channel} id={message_id} classification={classification} reason={reason}', flush=True)
            print('----- MESSAGE BEGIN (LOCAL ONLY) -----', flush=True)
            print(text[:12000], flush=True)
            print('----- MESSAGE END -----', flush=True)
            print('TELEGRAM_INSPECT_COMPLETE orders=0 database_changes=0', flush=True)

async def fetch_message(message_id):
    """F56: read ONE authorized message for a LIVE plan. No SQLite/PAPER/Bitunix access.

    Returns (channel_id, text, utc_datetime) or None. History is never executed:
    the caller only builds a plan that still needs operator approval.
    """
    if not isinstance(message_id, int) or message_id < 1:
        raise ValueError('MESSAGE_ID_INVALID')
    from telethon import TelegramClient
    channels = configured_channels()
    api_id, api_hash = os.getenv('PA_TG_API_ID'), os.getenv('PA_TG_API_HASH')
    if not api_id or not api_hash:
        raise RuntimeError('TELEGRAM_CREDENTIALS_MISSING')
    session = os.getenv('PA_TG_SESSION', 'data/punk_telegram')
    async with TelegramClient(session, int(api_id), api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError('SESSION_NOT_AUTHORIZED')
        dialogs = await accessible_dialogs(client)
        for channel in channels:
            if channel not in dialogs:
                continue
            message = await client.get_messages(dialogs[channel].entity, ids=message_id)
            if message and (message.raw_text or '').strip():
                return channel, message.raw_text, message.date
    return None


def event_age(date, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return max(0, (now_utc - date.astimezone(timezone.utc)).total_seconds())


def ingest_event(engine, channel_id, message_id, message, date, edited=False, paper_enabled=True):
    if not message or not message.strip():
        return {'status': 'INFORMATIVA', 'reason': 'Mensaje vacio'}
    if not paper_enabled:
        channel = str(channel_id)
        allowed = {v.strip() for v in engine.repo.setting('channels','').split(',') if v.strip()}
        if channel not in allowed:
            return {'status': 'RECHAZADA', 'reason': 'Canal no autorizado'}
        _signal, classification, reason = parse(message)
        if event_age(date) > int(engine.repo.setting('max_age_seconds', '120')):
            classification, reason = 'EXPIRADA', 'Mensaje fuera de ventana temporal'
        if not engine.repo.register(channel, message_id, message, classification, reason):
            return {'status': 'DUPLICADA', 'reason': 'Revision ya procesada'}
        return {'status': classification, 'reason': reason, 'mode': 'OBSERVE_ONLY'}
    return engine.ingest(channel_id, message_id, message, age_seconds=event_age(date), edited=edited)


def ingest_realtime_event(engine, channel_id, message_id, message, date, edited=False, paper_enabled=False):
    """Runtime safety gate: only real fresh LIMIT messages enter PAPER."""
    if not paper_enabled:
        return ingest_event(engine, channel_id, message_id, message, date, edited, paper_enabled=False)
    # F9: never interpret the fixed PAPER demo price as an executable quote
    # for incoming Telegram MARKET signals. Require future live quote-aware path.
    signal, classification, reason = parse(message)
    if classification == 'VALIDA' and signal.entry_type == 'MARKET':
        channel = str(channel_id)
        allowed = {v.strip() for v in engine.repo.setting('channels', '').split(',') if v.strip()}
        if channel not in allowed:
            return {'status': 'RECHAZADA', 'reason': 'Canal no autorizado'}
        if event_age(date) > int(engine.repo.setting('max_age_seconds', '120')):
            return engine.ingest(channel_id, message_id, message, age_seconds=event_age(date), edited=edited)
        if not engine.repo.register(channel, message_id, message, 'REQUIERE_REVISION', 'MARKET_NEEDS_LIVE_PAPER_QUOTE'):
            return {'status':'DUPLICADA','reason':'Revision ya procesada'}
        return {'status':'REQUIERE_REVISION','reason':'MARKET_NEEDS_LIVE_PAPER_QUOTE'}
    return engine.ingest(channel_id, message_id, message, age_seconds=event_age(date), edited=edited)


async def listen(engine, state=None, paper_enabled=False, auto=None):
    try:
        from telethon import TelegramClient, events
    except ImportError as exc:
        raise RuntimeError('Telethon no instalado') from exc
    api_id = os.environ.get('PA_TG_API_ID')
    api_hash = os.environ.get('PA_TG_API_HASH')
    if not api_id or not api_hash:
        raise RuntimeError('Credenciales Telegram no configuradas')
    channels = configured_channels(engine)
    session = os.environ.get('PA_TG_SESSION', 'data/punk_telegram')
    from pathlib import Path
    Path(session).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session, int(api_id), api_hash, auto_reconnect=True,
                            request_retries=3, receive_updates=True, catch_up=False)

    async def handler(event, edited):
        # Explicit secondary authorization: no unexpected chat can reach PAPER.
        if event.chat_id not in channels:
            return
        msg = event.message
        # For edited messages use the original message date, not edit_date: old signals
        # must not become new trades just because an old post was edited.
        result = ingest_realtime_event(engine, event.chat_id, event.id, event.raw_text or '', msg.date, edited, paper_enabled=paper_enabled)
        if state is not None:
            # Only bounded metadata, no message bodies or credentials in dashboard.
            activity = state.setdefault('telegram_activity', {'received': 0})
            activity['received'] = int(activity.get('received', 0)) + 1
            activity['last_channel'] = str(event.chat_id)
            activity['last_message_id'] = int(event.id)
            activity['last_status'] = str(result.get('status', 'UNKNOWN'))[:60]
            activity['last_event_type'] = 'EDITED' if edited else 'NEW'
            activity['last_received_utc'] = datetime.now(timezone.utc).isoformat()
        print('TELEGRAM', event.chat_id, event.id, result['status'], flush=True)
        if auto is not None:
            # F58: hand off to the LIVE worker queue; Telegram never waits for Bitunix.
            queued = auto.submit(event.chat_id, event.id, event.raw_text or '', msg.date, edited,
                                 reply_to=getattr(msg, 'reply_to_msg_id', None))
            print('LIVE_AUTO', event.chat_id, event.id, queued, flush=True)

    async def on_new(event):
        await handler(event, False)

    async def on_edit(event):
        await handler(event, True)

    # Do not pass numeric chats to Telethon's event builder: it can fail with
    # ValueError when peer metadata is not yet present in the session cache.
    # The allowlist is enforced INSIDE handler, before any processing.
    client.add_event_handler(on_new, events.NewMessage())
    client.add_event_handler(on_edit, events.MessageEdited())
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError('Sesion Telegram no autorizada: ejecutar --telegram-login primero')
    # Check membership through dialogs (which populates the entity cache) and
    # fail closed if the configured source is not accessible to this account.
    dialogs = await accessible_dialogs(client)
    missing = set(channels) - set(dialogs)
    if missing:
        await client.disconnect()
        raise RuntimeError(missing_channel_detail(channels, dialogs))
    if state is not None:
        state['telegram'] = 'CONNECTED_PAPER' if paper_enabled else 'CONNECTED_OBSERVE_ONLY'
    print('Telegram listening (authorized only); ' + ('PAPER' if paper_enabled else 'OBSERVE_ONLY'), flush=True)
    try:
        await client.run_until_disconnected()
    finally:
        if state is not None:
            state['telegram'] = 'DISCONNECTED'
        await client.disconnect()


def run_listener(engine, state=None, paper_enabled=False, auto=None):
    while True:
        try:
            asyncio.run(listen(engine, state, paper_enabled=paper_enabled, auto=auto))
        except KeyboardInterrupt:
            return
        except Exception as exc:
            reason = safe_telegram_error(exc)
            if state is not None:
                state['telegram'] = 'ERROR: ' + reason
            print('Telegram disconnected:', reason, flush=True)
            if 'CHANNEL_NOT_ACCESSIBLE' in str(exc):
                print(str(exc), flush=True)
            time.sleep(15)


async def login_once():
    """Interactive first login, must run in main thread; creates a local session."""
    from telethon import TelegramClient
    from pathlib import Path
    api_id = os.environ.get('PA_TG_API_ID')
    api_hash = os.environ.get('PA_TG_API_HASH')
    if not api_id or not api_hash:
        raise RuntimeError('Configurar PA_TG_API_ID y PA_TG_API_HASH en entorno')
    session = os.environ.get('PA_TG_SESSION', 'data/punk_telegram')
    Path(session).parent.mkdir(parents=True, exist_ok=True)
    async with TelegramClient(session, int(api_id), api_hash) as client:
        if not await client.is_user_authorized():
            await client.start()
        print('Telegram session authorized (local, private)', flush=True)


async def list_channels():
    """Solamente enumera chats existentes; no descarga mensajes ni publica nada."""
    from telethon import TelegramClient
    api_id, api_hash = os.getenv('PA_TG_API_ID'), os.getenv('PA_TG_API_HASH')
    if not api_id or not api_hash:
        raise RuntimeError('Faltan variables PA_TG_API_ID / PA_TG_API_HASH')
    session = os.getenv('PA_TG_SESSION', 'data/punk_telegram')
    async with TelegramClient(session, int(api_id), api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError('Iniciar sesion antes con --telegram-login')
        async for dialog in client.iter_dialogs():
            if dialog.is_channel or dialog.is_group:
                print(f'{marked_dialog_id(dialog)} | {dialog.name}', flush=True)


def safe_telegram_error(exc):
    """Actionable, bounded errors. Never emit private Telegram data or secrets."""
    msg = str(exc)
    if 'CHANNEL_NOT_ACCESSIBLE' in msg:
        return 'CHANNEL_NOT_ACCESSIBLE'
    if 'CHANNELS_EMPTY' in msg:
        return 'CHANNELS_EMPTY'
    if 'CHANNELS_BAD_FORMAT' in msg:
        return 'CHANNELS_BAD_FORMAT'
    if 'PA_CHANNELS' in msg or 'IDs de canales' in msg:
        return 'INVALID_CHANNELS'
    if 'Sesion Telegram no autorizada' in msg:
        return 'SESSION_NOT_AUTHORIZED'
    if isinstance(exc, ValueError):
        return 'VALUE_ERROR_CHECK_API_ID_HASH_SESSION'
    if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
        return 'CONNECTION_ERROR'
    return type(exc).__name__
