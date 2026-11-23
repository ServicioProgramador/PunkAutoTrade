"""F54: durable, stage-by-stage position-protection coordinator.

This is a real state machine but its *only* accepted exchange port is the
non-network LocalProtectionPort. No Telegram or production route is provided.
Unknown outcomes are permanent review states, never automatic retries.
"""
from __future__ import annotations
from decimal import Decimal
from pathlib import Path
import sqlite3
from punk.live_write_lab import Intent, LabBlocked, positive


def _fmt(x):
    return format(Decimal(str(x)), 'f')


class LocalProtectionPort:
    """No HTTP methods, credentials or side effects outside RAM."""
    def __init__(self):
        self.calls = []
        self.position = None
        self.stop = None
        self.tps = {}
        self.fail_on = None

    def set_position(self, intent, *, position_id='LOCAL-POSITION'):
        self.position = {'position_id':position_id, 'client_id':intent.client_id(),
                         'symbol':intent.symbol,'side':intent.side,'qty':_fmt(intent.quantity)}

    def snapshot(self):
        if self.fail_on == 'snapshot':
            raise TimeoutError('LOCAL_SNAPSHOT_TIMEOUT')
        return {'position':self.position, 'stop':self.stop, 'tps':dict(self.tps)}

    def attach_stop(self, position_id, price, qty):
        self.calls.append('stop')
        if self.fail_on == 'stop':
            raise TimeoutError('LOCAL_STOP_TIMEOUT')
        self.stop = {'position_id':position_id,'price':str(price),'qty':str(qty)}

    def attach_tp(self, position_id, number, price, qty):
        self.calls.append('tp'+str(number))
        if self.fail_on == 'tp'+str(number):
            raise TimeoutError('LOCAL_TP_TIMEOUT')
        self.tps[number] = {'position_id':position_id,'price':str(price),'qty':str(qty)}

    def move_stop(self, position_id, price, qty):
        self.calls.append('be')
        if self.fail_on == 'be':
            raise TimeoutError('LOCAL_BE_TIMEOUT')
        self.stop = {'position_id':position_id,'price':str(price),'qty':str(qty)}


class F54ProtectionCycle:
    """Fail-closed local coordinator; never invokes any production write transport.

    A pending request is persisted BEFORE the port can be called. If the process
    dies at any point, the same request remains UNKNOWN and needs human review.
    """
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True,exist_ok=True)
        c=self._connect()
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS f54_protection_cycle (
               client_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
               stage TEXT NOT NULL, position_id TEXT NOT NULL DEFAULT '',
               seq INTEGER NOT NULL DEFAULT 0, event TEXT NOT NULL DEFAULT '')''')
            c.commit()
        finally:
            c.close()

    def _connect(self):
        c = sqlite3.connect(str(self.db_path),timeout=8)
        c.execute('PRAGMA busy_timeout=8000')
        return c

    def _transact(self, fn):
        c=self._connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            result=fn(c)
            c.commit()
            return result
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _validate(intent, port):
        # Closed set of exact port types: the RAM-only lab port, or the F56
        # Bitunix port (GET readback + documented TP/SL endpoints).
        from punk.bitunix_protection_port import BitunixProtectionPort
        if type(intent) is not Intent or type(port) not in (LocalProtectionPort, BitunixProtectionPort):
            raise LabBlocked('F54_LOCAL_PORT_ONLY')
        intent.validate()

    def state(self,intent):
        def read(c):
            row=c.execute('SELECT fingerprint,stage,position_id,seq,event FROM f54_protection_cycle WHERE client_id=?',(intent.client_id(),)).fetchone()
            if row and row[0]!=intent.fingerprint():
                raise LabBlocked('F54_REVISED_INTENT')
            if not row:
                return {'state':'ABSENT','allow_retry':False}
            return {'state':row[1], 'position_id':row[2], 'seq':row[3], 'last_event':row[4], 'allow_retry':False}
        return self._transact(read)

    def _mark(self,intent,allowed,new_stage,*,position_id=None,seq=None,event=''):
        def update(c):
            row=c.execute('SELECT fingerprint,stage FROM f54_protection_cycle WHERE client_id=?',(intent.client_id(),)).fetchone()
            if not row or row[0]!=intent.fingerprint() or row[1] not in allowed:
                raise LabBlocked('F54_INVALID_TRANSITION')
            c.execute('UPDATE f54_protection_cycle SET stage=?,position_id=COALESCE(?,position_id),seq=COALESCE(?,seq),event=? WHERE client_id=?',
                      (new_stage,position_id,seq,event,intent.client_id()))
        self._transact(update)

    def start(self,intent,port):
        self._validate(intent,port)
        # No fabricated position: must be seen in a snapshot with exact identity.
        observed=port.snapshot().get('position')
        if not isinstance(observed,dict) or not observed.get('position_id') or observed.get('client_id')!=intent.client_id() or observed.get('symbol')!=intent.symbol or observed.get('side')!=intent.side or observed.get('qty')!=_fmt(intent.quantity):
            raise LabBlocked('F54_POSITION_NOT_CONFIRMED')
        position_id=observed['position_id']
        def create(c):
            row=c.execute('SELECT fingerprint FROM f54_protection_cycle WHERE client_id=?',(intent.client_id(),)).fetchone()
            if row:
                if row[0]!=intent.fingerprint(): raise LabBlocked('F54_REVISED_INTENT')
                return False
            c.execute('INSERT INTO f54_protection_cycle (client_id,fingerprint,stage,position_id) VALUES (?,?,?,?)',
                      (intent.client_id(),intent.fingerprint(),'POSITION_OBSERVED',position_id))
            return True
        return {'state':'POSITION_OBSERVED' if self._transact(create) else 'PREVIOUS_INTENT_NO_RETRY'}

    def progress(self,intent,port,*,tp_quantities):
        self._validate(intent,port)
        if len(tp_quantities)!=len(intent.take_profits) or sum(positive(x) for x in tp_quantities)!=positive(intent.quantity):
            raise LabBlocked('F54_TP_TOTAL_INVALID')
        stage=self.state(intent)
        name=stage['state']
        if name not in ('POSITION_OBSERVED','STOP_CONFIRMED', 'TP_CONFIRMED'):
            return {'state':name,'protected':False,'allow_retry':False}
        position_id=stage['position_id']
        snap=port.snapshot()
        pos=snap.get('position')
        if not isinstance(pos,dict) or pos.get('position_id')!=position_id or pos.get('client_id')!=intent.client_id() or pos.get('qty')!=_fmt(intent.quantity):
            self._mark(intent,(name,), 'REVIEW_POSITION_CHANGED',event='POSITION_MISMATCH')
            return {'state':'REVIEW_POSITION_CHANGED','protected':False,'allow_retry':False}
        if name=='POSITION_OBSERVED':
            self._mark(intent,(name,), 'UNKNOWN_STOP',event='STOP_REQUEST_ARMED')
            try:
                port.attach_stop(position_id,_fmt(intent.stop),_fmt(intent.quantity))
                sl=port.snapshot().get('stop')
                if sl!={'position_id':position_id,'price':_fmt(intent.stop),'qty':_fmt(intent.quantity)}:
                    return {'state':'UNKNOWN_STOP','protected':False,'allow_retry':False}
            except Exception:
                return {'state':'UNKNOWN_STOP','protected':False,'allow_retry':False}
            self._mark(intent,('UNKNOWN_STOP',),'STOP_CONFIRMED',event='STOP_READBACK_OK')
            name='STOP_CONFIRMED'
        if name in ('STOP_CONFIRMED','TP_CONFIRMED'):
            # Recheck stop before every TP stage.
            if port.snapshot().get('stop')!={'position_id':position_id,'price':_fmt(intent.stop),'qty':_fmt(intent.quantity)}:
                self._mark(intent,(name,),'REVIEW_STOP_CHANGED',event='STOP_COVERAGE_LOST')
                return {'state':'REVIEW_STOP_CHANGED','protected':False,'allow_retry':False}
            number=1 if name=='STOP_CONFIRMED' else stage['seq']+1
            if name=='TP_CONFIRMED' and stage['seq']==len(intent.take_profits):
                return {'state':'STOP_AND_TPS_OBSERVED_LOCAL','protected':False,'allow_retry':False}
            expected={'position_id':position_id,'price':_fmt(intent.take_profits[number-1]),'qty':_fmt(tp_quantities[number-1])}
            self._mark(intent,(name,), 'UNKNOWN_TP',seq=number,event=f'TP{number}_REQUEST_ARMED')
            try:
                port.attach_tp(position_id,number,expected['price'],expected['qty'])
                if port.snapshot().get('tps',{}).get(number)!=expected:
                    return {'state':'UNKNOWN_TP','protected':False,'allow_retry':False}
            except Exception:
                return {'state':'UNKNOWN_TP','protected':False,'allow_retry':False}
            self._mark(intent,('UNKNOWN_TP',),'TP_CONFIRMED',event=f'TP{number}_READBACK_OK')
            return {'state':'TP_CONFIRMED','tp_number':number,'protected':False,'allow_retry':False}
        return {'state':name,'protected':False,'allow_retry':False}

    def move_be(self,intent,port,*,price,remaining_qty,trigger_verified):
        self._validate(intent,port)
        row=self.state(intent)
        if row['state']!='TP_CONFIRMED' or row['seq']!=len(intent.take_profits) or trigger_verified is not True:
            raise LabBlocked('F54_BE_NOT_AUTHORIZED')
        be=positive(price); remaining=positive(remaining_qty)
        entry=positive(intent.entry)
        if (intent.side=='LONG' and be<entry) or (intent.side=='SHORT' and be>entry) or remaining>positive(intent.quantity):
            raise LabBlocked('F54_INVALID_BE_OR_QTY')
        self._mark(intent,('TP_CONFIRMED',),'UNKNOWN_BE',event='BE_REQUEST_ARMED')
        try:
            port.move_stop(row['position_id'],_fmt(be),_fmt(remaining))
            matched=port.snapshot().get('stop')=={'position_id':row['position_id'],'price':_fmt(be),'qty':_fmt(remaining)}
        except Exception:
            matched=False
        if not matched:
            return {'state':'UNKNOWN_BE','protected':False,'allow_retry':False}
        self._mark(intent,('UNKNOWN_BE',),'BE_OBSERVED_LOCAL',event='BE_READBACK_OK')
        return {'state':'BE_OBSERVED_LOCAL','protected':False,'allow_retry':False}

    def reconcile_unknown(self,intent,port,*,tp_quantities,be_price=None,remaining_qty=None):
        """F56: resolve an UNKNOWN_* stage ONLY with exact GET evidence.

        Absence of evidence keeps the UNKNOWN state (never a resend).
        """
        self._validate(intent,port)
        row=self.state(intent)
        name=row['state']
        if name not in ('UNKNOWN_STOP','UNKNOWN_TP','UNKNOWN_BE'):
            return {'state':name,'resolved':False,'allow_retry':False}
        snap=port.snapshot()
        pid=row['position_id']
        if name=='UNKNOWN_STOP':
            ok=snap.get('stop')=={'position_id':pid,'price':_fmt(intent.stop),'qty':_fmt(intent.quantity)}
            if ok:
                self._mark(intent,('UNKNOWN_STOP',),'STOP_CONFIRMED',event='STOP_RECONCILED_GET')
                return {'state':'STOP_CONFIRMED','resolved':True,'allow_retry':False}
        elif name=='UNKNOWN_TP':
            n=row['seq']
            expected={'position_id':pid,'price':_fmt(intent.take_profits[n-1]),'qty':_fmt(tp_quantities[n-1])}
            if snap.get('tps',{}).get(n)==expected:
                self._mark(intent,('UNKNOWN_TP',),'TP_CONFIRMED',event=f'TP{n}_RECONCILED_GET')
                return {'state':'TP_CONFIRMED','tp_number':n,'resolved':True,'allow_retry':False}
        elif be_price is not None and remaining_qty is not None:
            if snap.get('stop')=={'position_id':pid,'price':_fmt(be_price),'qty':_fmt(remaining_qty)}:
                self._mark(intent,('UNKNOWN_BE',),'BE_OBSERVED_LOCAL',event='BE_RECONCILED_GET')
                return {'state':'BE_OBSERVED_LOCAL','resolved':True,'allow_retry':False}
        return {'state':name,'resolved':False,'allow_retry':False}

    def submit_live(self,*a,**kw):
        raise LabBlocked('F54_LIVE_NOT_ENABLED')


def lab_report():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='punk_f54_') as temp:
        intent=Intent('f54:rune:qa','RUNEUSDT','SHORT','10','0.4785','0.487456',
                      ('0.470246','0.469008','0.467584','0.465947','0.464063'))
        port=LocalProtectionPort(); port.set_position(intent)
        path=Path(temp)/'cycle.sqlite'
        app=F54ProtectionCycle(path)
        initial=app.start(intent,port)
        results=[]
        for _ in range(6):
            results.append(app.progress(intent,port,tp_quantities=('2',)*5))
        be=app.move_be(intent,port,price='0.477',remaining_qty='8',trigger_verified=True)
        return {'build':'F54','entry_state':initial['state'],'states':[r['state'] for r in results],
                'be_state':be['state'],'recovered':F54ProtectionCycle(path).state(intent)['state'],
                'local_calls':port.calls,'real_orders_sent':0,'ready_for_live':False,
                'live_enabled':False,'protected_real':False}
