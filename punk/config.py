"""Carga mínima de .env local; nunca imprime secretos ni sobrescribe variables existentes."""
import os
import re
from pathlib import Path

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def load_local_env(path='.env'):
    source = Path(path)
    if not source.is_file():
        return 0
    count = 0
    for raw in source.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if not NAME.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        # PA_CHANNELS is a local project allowlist; stale Windows process values
        # must never silently override the visible .env allowlist.
        if key == 'PA_CHANNELS' or key not in os.environ:
            os.environ[key] = value
            count += 1
    return count
