"""Counterexamples from independent review; all provider writes are fake."""
import contextlib,json,unittest
from unittest.mock import patch
from test_handoff_unloaded import RPC,NoOwner,TASK,TURN,REQUEST
import test_workflow_handoffs as fixtures
from workflow_handoff_transport import DesktopTransport,qualified_resume_command
from workflow_handoffs import Handoffs
from pathlib import Path
TARGET=dict(provider='codex',endpoint=TASK,cwd='/tmp',model='gpt-6-astra',reasoning='ultra')
FLOW=type('Flow',(),{'codex_home':Path('/tmp')})()

@contextlib.contextmanager
def manager(transport):
    case=fixtures.HandoffTests();case.setUp()
    try:
        original=case.f.identity_catalog
        def catalog():
            data=original()
            for s in data['sessions']:
                if s['agent_id']=='codex:review':s.update(TARGET)
            return data
        case.f.identity_catalog=catalog;case.h.transport=transport
        yield case
    finally:case.tearDown()

class StaleOwner:
    def __init__(self):self.calls=[];self.failure=None
    def request(self,method,*args,**kwargs):
        self.calls.append(method)
        if method=='thread-owner-discovery':return dict(resultType='success',handledByClientId=TURN)
        if self.failure:raise self.failure
        return dict(resultType='error',error='no-client-found')
    def close(self):pass

class RecoveryTests(unittest.TestCase):
    def test_stale_owner_revalidates_and_keeps_original_request(self):
        ipc=StaleOwner();rpc=RPC();t=DesktopTransport(FLOW,lambda _:ipc,lambda:rpc)
        with manager(t) as c:
            mid=c.send();c.tick();first=c.h.get(mid)
            self.assertEqual('OWNER_REJECTED',first['status']);self.assertEqual(1,first['attempts'])
            c.h=Handoffs(c.f,t,lambda:c.clock[0]);c.tick();second=c.h.get(mid)
            self.assertEqual('ACCEPTED',second['status']);self.assertEqual(2,second['attempts'])
            self.assertEqual(first['request_id'],second['request_id'])
            self.assertEqual(['thread-owner-discovery','thread-follower-start-turn'],ipc.calls)
            self.assertEqual(1,sum(m=='turn/start' for m,p in rpc.calls))
            self.assertEqual(first['request_id'],rpc.calls[-1][1]['clientUserMessageId'])
    def test_desktop_lost_ack_never_uses_resume(self):
        ipc=StaleOwner();ipc.failure=TimeoutError('lost ack');rpc=RPC()
        t=DesktopTransport(FLOW,lambda _:ipc,lambda:rpc);t.observe=lambda *args:{}
        with manager(t) as c:
            mid=c.send();c.tick();c.tick()
            self.assertEqual('UNCERTAIN',c.h.get(mid)['status']);self.assertEqual([],rpc.calls)
    def test_rejected_then_busy_or_paused_does_not_start(self):
        for cause in ['busy','paused']:
            ipc=StaleOwner();rpc=RPC();t=DesktopTransport(FLOW,lambda _:ipc,lambda:rpc)
            with manager(t) as c:
                mid=c.send();c.tick()
                if cause=='busy':c.activities['codex:review']={'turnStatus':'open'}
                else:
                    p=c.h.policy();p['enabled']=False;c.h.policy_path.write_text(json.dumps(p))
                c.tick();self.assertFalse(rpc.calls)
    def test_registry_failure_is_recoverable_without_attempt_or_retained_rpc(self):
        rpc=RPC();t=DesktopTransport(FLOW,lambda _:NoOwner(),lambda:rpc)
        with manager(t) as c:
            mid=c.send()
            with patch.object(t,'publish_process',side_effect=OSError('disk unavailable')):c.tick()
            row=c.h.get(mid);self.assertEqual('UNAVAILABLE',row['status']);self.assertEqual(0,row['attempts'])
            self.assertFalse(any(m=='turn/start' for m,p in rpc.calls));self.assertTrue(rpc.closed);self.assertFalse(t.sessions)
            t.rpc_factory=lambda:RPC();c.tick();self.assertEqual('ACCEPTED',c.h.get(mid)['status'])
    def test_resume_rejection_or_timeout_closes_without_turn(self):
        for error in [ValueError('already has an active writer'),TimeoutError('acquisition unknown')]:
            rpc=RPC();original=rpc.call
            def call(method,params,timeout):
                if method=='thread/resume':raise error
                return original(method,params,timeout)
            rpc.call=call;t=DesktopTransport(FLOW,lambda _:NoOwner(),lambda:rpc)
            with manager(t) as c:
                mid=c.send();c.tick();self.assertEqual(0,c.h.get(mid)['attempts'])
                self.assertTrue(rpc.closed);self.assertFalse(t.sessions)
    def test_registry_error_cannot_prevent_terminal_reconciliation_cleanup(self):
        rpc=RPC();t=DesktopTransport(FLOW,lambda _:NoOwner(),lambda:rpc)
        with t.prepare(TARGET) as p:receipt=t.start(p,'HANDOFF '+REQUEST+'\nReview',REQUEST)
        rpc.turns=[{'id':TURN,'status':'completed'}]
        with patch.object(t,'publish_process',side_effect=OSError('disk unavailable')):
            result=t.observe(TARGET,REQUEST,receipt)
        self.assertEqual('RETURNED',result['status']);self.assertTrue(result['receipt']['registryWarning'])
        self.assertTrue(rpc.closed);self.assertFalse(t.sessions)
    def test_runtime_drift_fails_closed(self):
        with patch.dict('os.environ',{'SWITCHBOARD_CODEX':'/bin/sh'}),self.assertRaisesRegex(ValueError,'requalification'):
            qualified_resume_command()

class PagedRPC(RPC):
    def __init__(self,marker):super().__init__();self.marker=marker
    def call(self,method,params,timeout=30):
        if method!='thread/turns/list':return super().call(method,params,timeout)
        self.calls.append((method,dict(params)))
        if not params.get('cursor'):
            return {'data':[{'id':'newer-'+str(i),'status':'completed','items':[]} for i in range(10)],'nextCursor':'page2'}
        return {'data':[{'id':TURN,'status':'completed','items':[{'type':'userMessage','content':[{'type':'text','text':self.marker}]}]}]}

class PaginationTests(unittest.TestCase):
    def test_known_and_unknown_turn_persist_cursor_across_restart(self):
        for known in [True,False]:
            first=RPC();first.failure=TimeoutError('lost ack')
            t=DesktopTransport(FLOW,lambda _:NoOwner(),lambda:first)
            with manager(t) as c:
                mid=c.send();c.tick();row=c.h.get(mid);receipt=json.loads(row['receipt'])
                if known:
                    receipt['turnId']=TURN;c.h.update(mid,'UNCERTAIN','lost ack',receipt,delay=0)
                later=PagedRPC('HANDOFF '+row['request_id']+'\nReview')
                c.h=Handoffs(c.f,DesktopTransport(FLOW,rpc_factory=lambda:later),lambda:c.clock[0]);c.tick()
                progress=json.loads(c.h.get(mid)['receipt']);self.assertEqual('page2',progress['historySearch']['cursor'])
                # New manager and transport simulate daemon restart. Only reads occur.
                c.h=Handoffs(c.f,DesktopTransport(FLOW,rpc_factory=lambda:later),lambda:c.clock[0]);c.tick()
                self.assertEqual('RETURNED',c.h.get(mid)['status']);self.assertEqual(1,c.h.get(mid)['attempts'])
                self.assertEqual(['thread/turns/list']*2,[m for m,p in later.calls])
                self.assertEqual('page2',later.calls[1][1]['cursor'])
    def test_exhaustion_and_assistant_quotes_never_claim_delivery(self):
        rpc=RPC();rpc.turns=[{'id':TURN,'status':'completed','items':[{'type':'agentMessage','content':[{'text':'HANDOFF '+REQUEST+'\nReview'}]}]}]
        t=DesktopTransport(FLOW,rpc_factory=lambda:rpc);receipt={}
        for _ in range(2):
            obs=t.observe_paged(TARGET,REQUEST,receipt);self.assertNotIn('status',obs)
            receipt.update(obs['receipt']);self.assertTrue(receipt['historySearch']['exhausted'])
        self.assertFalse(any(m=='turn/start' for m,p in rpc.calls))
    def test_stuck_cursor_fails_without_resending(self):
        rpc=RPC();rpc.call=lambda *args:{'data':[],'nextCursor':'same'}
        t=DesktopTransport(FLOW,rpc_factory=lambda:rpc)
        with self.assertRaisesRegex(ValueError,'did not advance'):
            t.observe_paged(TARGET,REQUEST,{'historySearch':{'requestId':REQUEST,'cursor':'same'}})

if __name__=='__main__':unittest.main()
