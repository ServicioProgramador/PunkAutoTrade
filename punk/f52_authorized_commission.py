"""F52: one-shot OFFLINE approval wired into F48 commissioning.

No production transport, Telegram live route or exchange credential access.
"""
from __future__ import annotations

from pathlib import Path
from punk.f51_one_shot_authorization import OneShotApproval
from punk.f48_one_shot import F48Commissioning, F48LocalWire
from punk.live_write_lab import Intent, LabBlocked


class F52AuthorizedCommission:
    def __init__(self, path):
        self.path = Path(path)
        self.approvals = OneShotApproval(self.path)
        self.commissioning = F48Commissioning(self.path)

    @staticmethod
    def _report(state, *, wire_calls=0):
        return {'build':'F52', 'state':state, 'mode':'AUTHORIZED_OFFLINE_COMMISSION',
                'approval_reusable':False, 'real_orders_sent':0,
                'qa_wire_calls':wire_calls, 'live_enabled':False, 'write_enabled':False,
                'ready_for_live':False, 'allow_retry':False, 'protected':False}

    def authorize_and_commission(self, intent, preview, token, wire, *, now=None):
        # Before burning an approval, check exact input and reject production wires.
        if type(wire) is not F48LocalWire:
            raise LabBlocked('F52_QA_WIRE_ONLY')
        F48Commissioning._validate_preview(intent, preview)
        receipt = self.approvals.consume_offline(intent, token, now=now)
        if receipt['state'] != 'CONSUMED_OFFLINE_NO_SEND':
            return self._report('APPROVAL_' + receipt['state'], wire_calls=wire.calls)
        # Permission burned durably before calling the F48 journal or QA wire.
        outcome = self.commissioning.commission(intent, preview, wire)
        return self._report(outcome['state'], wire_calls=wire.calls)

    def submit_live(self, *args, **kwargs):
        raise LabBlocked('F52_LIVE_CAPABILITY_NOT_ISSUED')


def lab_report():
    from tempfile import TemporaryDirectory
    from punk.bitunix_write_contract import entry_contract, protection_plan
    with TemporaryDirectory(prefix='punk_f52_') as root:
        intent=Intent('f52:lab:rune','RUNEUSDT','SHORT','10','0.4785','0.487456',
                      ('0.470246','0.469008','0.467584','0.465947','0.464063'))
        preview={'state':'PRETRADE_PREVIEW_ONLY', 'entry_payload':entry_contract(intent),
                 'symbol':intent.symbol, 'snapshot_get_only_verified':True,
                 'sl_required':True, 'tp_count':5,
                 'tp_payloads':protection_plan(intent, position_id='F52LAB',
                                               tp_weights=('0.2',)*5,quantity_step='0.01'),
                 'required_reserve_usdt':'5.1','request_sent':False,
                 'write_transport_enabled':False,'live_enabled':False,
                 'ready_for_live':False,'allow_retry':False}
        review={'state':'PRETRADE_PREVIEW_ONLY','client_id':intent.client_id(),
                'fingerprint':intent.fingerprint(), 'snapshot_get_only_verified':True,
                'sl_required':True, 'live_enabled':False,'write_transport_enabled':False,
                'ready_for_live':False}
        db=Path(root)/'qa.sqlite'
        app=F52AuthorizedCommission(db)
        token=app.approvals.approve_offline(intent,review=review)
        wire=F48LocalWire()
        first=app.authorize_and_commission(intent,preview,token,wire)
        replay=F52AuthorizedCommission(db).authorize_and_commission(intent,preview,token,wire)
        return {**first,'replay_state':replay['state'],
                'checkpoint_recovered':F52AuthorizedCommission(db).approvals.state(intent)=='CONSUMED_OFFLINE',
                'qa_wire_calls':wire.calls}
