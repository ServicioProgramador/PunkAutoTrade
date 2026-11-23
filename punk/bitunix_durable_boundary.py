"""F29 — durable, no-send HTTPS intent boundary for future Bitunix execution.

Does not expose a network writer. A signed envelope is constructed only with
caller-supplied synthetic test credentials. The persistent journal never stores
headers or credentials; an armed intent can never be sent by this component.
"""
from __future__ import annotations

import json
from punk.bitunix_https_boundary import prepare_https_post, BitunixHttpsBoundary
from punk.bitunix_write_contract import PATH_PLACE, entry_contract
from punk.bitunix_write_journal import WriteJournal
from punk.live_write_lab import Intent, LabBlocked


class DurableHttpsBoundary:
    def __init__(self, journal_path):
        self.journal = WriteJournal(journal_path)
        self.transport = BitunixHttpsBoundary()  # physical default-deny transport

    @staticmethod
    def _check_intent(intent):
        if type(intent) is not Intent:
            raise LabBlocked('INVALID_INTENT_TYPE')
        intent.validate()

    def stage(self, intent, *, pretrade_preview, api_key, secret, nonce, timestamp):
        """Prepare signed entry *offline* after a verified pretrade preview.

        Requires an exact match to the pretrade payload; stale / swapped
        payloads cannot be laundered into the durable journal.
        """
        self._check_intent(intent)
        expected = entry_contract(intent)
        if type(pretrade_preview) is not dict or pretrade_preview.get('state') != 'PRETRADE_PREVIEW_ONLY':
            raise LabBlocked('PRETRADE_NOT_VERIFIED')
        if pretrade_preview.get('entry_payload') != expected or pretrade_preview.get('symbol') != intent.symbol:
            raise LabBlocked('PRETRADE_INTENT_MISMATCH')
        if pretrade_preview.get('snapshot_get_only_verified') is not True or pretrade_preview.get('sl_required') is not True:
            raise LabBlocked('PRETRADE_PROTECTION_UNVERIFIED')
        if type(pretrade_preview.get('tp_count')) is not int or pretrade_preview['tp_count'] != len(intent.take_profits):
            raise LabBlocked('PRETRADE_TP_MISMATCH')
        if any(pretrade_preview.get(k) is not False for k in
               ('request_sent', 'write_transport_enabled', 'live_enabled', 'ready_for_live', 'allow_retry')):
            raise LabBlocked('PRETRADE_LIVE_FLAG_FORBIDDEN')
        envelope = prepare_https_post(PATH_PLACE, expected, api_key=api_key, secret=secret,
                                      nonce=nonce, timestamp=timestamp)
        if json.loads(envelope.body)['clientId'] != intent.client_id():
            raise LabBlocked('WIRE_CLIENT_ID_MISMATCH')
        persisted = self.journal.prepare(intent)
        return {'build': 'F29', 'state': persisted['state'], 'signed': True,
                'client_id': intent.client_id(), 'path': envelope.path,
                'ready_for_live': False, 'allow_send': False, 'allow_retry': False,
                'orders_sent': 0, 'note': 'OFFLINE_ONLY'}

    def arm_for_audit(self, intent):
        """Durably mark unknown *before* any conceivable later transport attempt.

        In F29 this is only a no-send simulation of the pre-send transition.
        """
        self._check_intent(intent)
        state = self.journal.arm_uncertain_offline(intent)
        return {'build': 'F29', 'state': state['state'], 'orders_sent': 0,
                'allow_send': False, 'allow_retry': False, 'ready_for_live': False}

    def reconcile_observation(self, intent, *, order_observed=False,
                              protection_coverage=False, snapshot_verified=False):
        self._check_intent(intent)
        data = self.journal.observe(intent, order_observed=order_observed,
                                    protection_coverage=protection_coverage,
                                    snapshot_verified=snapshot_verified)
        return {'build': 'F29', 'state': data['state'], 'observations': data['observation_count'],
                'allow_send': False, 'allow_retry': False, 'ready_for_live': False}

    def read(self, intent):
        return self.journal.read(intent)

    def submit(self, *args, **kwargs):
        raise LabBlocked('LIVE_TRANSMISSION_NOT_AUTHORIZED_F29')


def boundary_report():
    return {'build': 'F29', 'mode': 'DURABLE_HTTPS_NO_SEND',
            'write_transport_enabled': False, 'orders_sent': 0,
            'allow_retry': False, 'live_enabled': False, 'ready_for_live': False}
