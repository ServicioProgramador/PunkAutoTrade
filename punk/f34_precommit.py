"""F34: single-entry GET-only preflight -> signed offline durable intent.

This is deliberately NOT the missing LIVE HTTPS POST transport. It connects
F24/F29 with one fresh reader pass and a real SQLite durability boundary.
No production credentials are loaded, and no sender is exposed.
"""
from __future__ import annotations

from punk.bitunix_pretrade_gate import compile_pretrade
from punk.bitunix_durable_boundary import DurableHttpsBoundary
from punk.live_write_lab import Intent, LabBlocked


class PrecommitF34:
    def __init__(self, journal_path):
        self._boundary = DurableHttpsBoundary(journal_path)

    def prepare(self, intent, reader, *, margin_usdt='5', requested_leverage=20,
                now=None, nonce='F34_OFFLINE_NONCE', timestamp='1800000001000',
                tp_weights=None):
        """Read account/contract/tiers; stage the exact resulting signed order offline.

        Synthetic signing keys are fixed internally and deliberately unrelated to
        real .env credentials. Caller cannot provide an alternative key or host.
        """
        if type(intent) is not Intent:
            raise LabBlocked('INVALID_INTENT_TYPE')
        # Prevent staging another intent if a prior revision was already armed.
        previous = self._boundary.read(intent)
        if previous is not None and previous['state'] != 'PREPARED':
            raise LabBlocked('INTENT_ALREADY_ARMED_NO_RESEND')
        preview = compile_pretrade(intent, reader, margin_usdt=margin_usdt,
                                   requested_leverage=requested_leverage,
                                   tp_weights=tp_weights, now=now)
        result = self._boundary.stage(
            intent, pretrade_preview=preview,
            api_key='F34_SYNTHETIC_KEY', secret='F34_SYNTHETIC_SECRET',
            nonce=nonce, timestamp=timestamp)
        return {
            'build': 'F34', 'mode': 'GET_ONLY_OFFLINE_PRECOMMIT',
            'state': result['state'], 'client_id': intent.client_id(),
            'symbol': intent.symbol, 'effective_leverage': preview['effective_leverage'],
            'margin_usdt': preview['margin_usdt'],
            'required_reserve_usdt': preview['required_reserve_usdt'],
            'sl_required': preview['sl_required'], 'tp_count': preview['tp_count'],
            'durable_intent': True, 'orders_sent': 0, 'allow_send': False,
            'allow_retry': False, 'live_enabled': False, 'ready_for_live': False,
        }

    def read(self, intent):
        return self._boundary.read(intent)

    def submit(self, *args, **kwargs):
        raise LabBlocked('F34_PRODUCTION_WRITE_NOT_IMPLEMENTED')


def status():
    return {'build': 'F34', 'write_transport_enabled': False,
            'live_enabled': False, 'ready_for_live': False, 'orders_sent': 0}
