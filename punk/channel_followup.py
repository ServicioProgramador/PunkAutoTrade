"""F62: follow-up messages of the signal channel (not signals).

Recognised (Punkchainer VIP formats, observed 2026-11-21/22):
  "#XXX/USDT ... 🎯 Objetivo 1 Tocado ✅ ... Mueve tu SL a la entrada (Break Even)"  -> TP_HIT
  "#XXX/USDT ... Stop Loss Alcanzado ⛔ ... Pérdida: -76.09%"                       -> SL_HIT
  "#XXX/USDT ... 🏁 Todos los objetivos alcanzados ... Beneficio: 196.63%"          -> ALL_TP
  "Hype no ha tocado SL! Ha sido un error!"                                         -> CORRECTION

The channel never opens or closes anything by itself. The only action it can
trigger is moving an EXISTING protected SL to Break Even (protective), and only
through the normal supervisor path (fee-inclusive BE, feasibility, GET readback).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FollowUp:
    kind: str                 # TP_HIT | SL_HIT | ALL_TP | CORRECTION
    symbol: str | None
    tp_number: int | None = None
    be_instruction: bool = False
    pct: str | None = None


def classify(text):
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.upper()
    sym = re.search(r'[#$]\s*([A-Z0-9]{2,20})\s*/\s*USDT', raw)
    symbol = sym.group(1) + 'USDT' if sym else None
    pct = re.search(r'(?:BENEFICIO|P[ÉE]RDIDA)\s*:\s*(-?\d+(?:[.,]\d+)?)\s*%', raw)
    pct = pct.group(1).replace(',', '.') if pct else None
    be = bool(re.search(r'MUEVE\s+TU\s+SL|SL\s+A\s+LA\s+ENTRADA|BREAK\s*-?\s*EVEN', raw))
    if re.search(r'STOP\s*LOSS\s+ALCANZADO', raw):
        return FollowUp('SL_HIT', symbol, pct=pct)
    if re.search(r'TODOS\s+LOS\s+OBJETIVOS\s+ALCANZADOS', raw):
        return FollowUp('ALL_TP', symbol, pct=pct)
    tp = re.search(r'OBJETIVO\s+(\d)\s+TOCADO', raw)
    if tp:
        return FollowUp('TP_HIT', symbol, int(tp.group(1)), be, pct)
    if re.search(r'NO\s+HA\s+TOCADO|HA\s+SIDO\s+UN\s+ERROR', raw):
        if not symbol:
            word = re.match(r'^[^\w]*([A-Z0-9]{2,15})\b', raw)
            symbol = word.group(1) + 'USDT' if word else None
        return FollowUp('CORRECTION', symbol)
    return None
