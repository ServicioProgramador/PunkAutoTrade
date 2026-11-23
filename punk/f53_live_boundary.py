"""F53: production HTTPS write boundary with durable single-attempt semantics.

This module has no CLI/Telegram route and never grants live authority itself.
No API credentials are read until an explicit, separately reviewed capability is
provided by a future LIVE approval workflow. ACK never means protected.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
from punk.live_write_lab import Intent, LabBlocked
from punk.bitunix_write_contract import entry_contract, PATH_PLACE, validate_entry_ack
from punk.bitunix_https_boundary import prepare_https_post
from punk.bitunix_write_journal import WriteJournal
from punk.bitunix_ack_journal import AckJournal
from punk.f48_one_shot import BitunixProductionWire, F48Commissioning

@dataclass(frozen=True)
class OneTradeCapability:
    """Non-issuable by environment flags, run.py or Telegram in F53."""
    client_id: str
    fingerprint: str
    approved: bool
    source: str

class F53ProductionBoundary:
    def __init__(self, db_path, *, live_enabled=False):
        self.db_path=Path(db_path)
        self.journal=WriteJournal(self.db_path)
        self.acks=AckJournal(self.db_path)
        self.live_enabled=live_enabled

    def send(self, intent, preview, capability, *, api_key=None, secret=None, wire=None, nonce=None, timestamp=None):
        # No possible route from the current application: this adapter is not
        # registered in CLI, dashboard or Telegram and is OFF by default.
        if self.live_enabled is not True:
            raise LabBlocked('F53_LIVE_NOT_ENABLED')
        if type(capability) is not OneTradeCapability or capability.approved is not True or capability.source != 'FUTURE_REVIEWED_OPERATOR_GATE':
            raise LabBlocked('F53_OPERATOR_APPROVAL_REQUIRED')
        if type(intent) is not Intent or capability.client_id != intent.client_id() or capability.fingerprint != intent.fingerprint():
            raise LabBlocked('F53_CAPABILITY_INTENT_MISMATCH')
        F48Commissioning._validate_preview(intent, preview)
        if not api_key or not secret:
            raise LabBlocked('F53_EXCHANGE_CREDENTIALS_REQUIRED')
        if type(wire) is not BitunixProductionWire:
            raise LabBlocked('F53_PRODUCTION_TRANSPORT_REQUIRED')
        if not nonce or not timestamp:
            raise LabBlocked('F53_FRESH_SIGNATURE_REQUIRED')
        record=self.journal.prepare(intent)
        if record['state'] != 'PREPARED':
            return self._report('PREVIOUS_INTENT_BLOCKED_NO_RETRY')
        request=prepare_https_post(PATH_PLACE, entry_contract(intent),api_key=api_key,secret=secret,nonce=nonce,timestamp=timestamp)
        # Durable point of no automatic retry, BEFORE any possible HTTP I/O.
        armed=self.journal.arm_uncertain_offline(intent)
        if armed['state']!='ARMED_UNKNOWN':
            return self._report('PREVIOUS_INTENT_BLOCKED_NO_RETRY')
        try:
            reply=wire._transport_impl_for_review(request)
            result=self.acks.observe(intent,raw=reply.body,http_status=reply.status)
            state='ACK_ONLY_RECONCILIATION_REQUIRED' if result['state']=='ACK_OBSERVED' else 'UNKNOWN_NO_RETRY'
        except Exception:
            state='UNKNOWN_NO_RETRY'
        return self._report(state)

    @staticmethod
    def _report(state):
        return {'build':'F53','state':state,'allow_retry':False,
                'protected':False,'ready_for_live':False,
                'requires_get_reconciliation':True}

    def current_state(self, intent):
        return self.journal.read(intent)

    def submit_from_telegram(self, *a, **kw):
        raise LabBlocked('F53_AUTONOMOUS_LIVE_NOT_AUTHORIZED')
