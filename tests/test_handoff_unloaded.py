import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from workflow_handoff_transport import DesktopTransport

TASK='11111111-1111-1111-1111-111111111111'
TURN='22222222-2222-2222-2222-222222222222'
REQUEST='33333333-3333-3333-3333-333333333333'

class NoOwner:
    def request(self,*a,**kw):return {'resultType':'error','error':'no-client-found'}
    def close(self):pass

class RPC:
    def __init__(self):
        self.calls=[];self.closed=False;self.failure=None;self.status='notLoaded';self.turns=[]
        self.result={'thread':{'id':TASK,'status':{'type':'idle'}},'cwd':'/tmp','model':'gpt-6-astra',
          'reasoningEffort':'ultra','modelProvider':'openai','approvalPolicy':'never','sandbox':{'type':'dangerFullAccess'}}
    def call(self,method,params,timeout=30):
        self.calls.append((method,copy.deepcopy(params)))
        if method=='thread/read':return {'thread':{'id':TASK,'cwd':'/tmp','modelProvider':'openai','status':{'type':self.status}}}
        if method=='thread/resume':return self.result
        if method=='turn/start':
            if self.failure:raise self.failure
            return {'turn':{'id':TURN}}
        if method=='thread/turns/list':return {'data':self.turns}
        raise AssertionError(method)
    def close(self):self.closed=True

class UnloadedTests(unittest.TestCase):
    def setUp(self):
        self.rpc=RPC()
        self.flow=type('Flow',(),{'codex_home':Path('/tmp')})()
        self.t=DesktopTransport(self.flow,lambda p:NoOwner(),lambda:self.rpc)
        self.target={'provider':'codex','endpoint':TASK,'cwd':'/tmp','model':'gpt-6-astra','reasoning':'ultra'}
    def test_prepare_reopens_exact_task_without_starting_turn(self):
        with self.t.prepare(self.target):pass
        self.assertEqual(['thread/read','thread/resume'],[x[0] for x in self.rpc.calls])
        self.assertEqual({'threadId':TASK,'excludeTurns':True},self.rpc.calls[1][1])
        self.assertTrue(self.rpc.closed)
    def test_idle_start_keeps_connection_and_inherits_settings(self):
        with self.t.prepare(self.target) as p:
            receipt=self.t.start(p,'HANDOFF '+REQUEST+'\nReview',REQUEST)
        self.assertFalse(self.rpc.closed);self.assertEqual(TURN,receipt['turnId'])
        self.assertEqual(TASK,receipt['conversationId'])
        self.assertEqual({'threadId','input','clientUserMessageId'},set(self.rpc.calls[-1][1]))
        self.assertNotIn('thread/start',[x[0] for x in self.rpc.calls])
        self.rpc.turns=[{'id':TURN,'status':'completed','items':[]}]
        self.assertEqual('RETURNED',self.t.observe(self.target,REQUEST,receipt)['status'])
        self.assertTrue(self.rpc.closed);self.assertFalse(self.t.sessions)
    def test_loaded_or_active_task_is_not_taken_over(self):
        for status in ['active','idle','systemError']:
            self.rpc.status=status
            with self.assertRaisesRegex(ValueError,'loaded or active'):
                with self.t.prepare(self.target):pass
        self.assertFalse(any(x[0]=='thread/resume' for x in self.rpc.calls))
    def test_settings_or_identity_drift_refuses_start(self):
        for field,value in [('model','gpt-5.6-sol'),('reasoningEffort','low'),('modelProvider','other'),('cwd','/'),('approvalPolicy','on-request'),('sandbox',{'type':'workspaceWrite'})]:
            with self.subTest(field=field):
                old=self.rpc.result[field];self.rpc.result[field]=value
                with self.assertRaises(ValueError):
                    with self.t.prepare(self.target):pass
                self.rpc.result[field]=old
        self.rpc.result['thread']['id']=TURN
        with self.assertRaisesRegex(ValueError,'exact idle task'):
            with self.t.prepare(self.target):pass
        self.assertFalse(any(x[0]=='turn/start' for x in self.rpc.calls))
    def test_missing_saved_settings_refuses_resume(self):
        self.target.pop('model')
        with self.assertRaisesRegex(ValueError,'Saved model'):
            with self.t.prepare(self.target):pass
        self.assertFalse(self.rpc.calls)
    def test_discovery_timeout_can_recover_but_protocol_error_cannot(self):
        class Failure(Exception):pass
        err=Failure('Timed out');err.code='desktop_ipc_timeout'
        with patch.object(NoOwner,'request',side_effect=err):
            with self.t.prepare(self.target):pass
        err.code='desktop_ipc_insecure';self.rpc.calls=[]
        with patch.object(NoOwner,'request',side_effect=err),self.assertRaises(Failure):
            with self.t.prepare(self.target):pass
        self.assertFalse(self.rpc.calls)
    def test_uncertain_start_retains_connection_no_second_resume(self):
        self.rpc.failure=TimeoutError('reply lost')
        with self.t.prepare(self.target) as p:
            proof=self.t.start_receipt(p,REQUEST)
            with self.assertRaises(TimeoutError):self.t.start(p,'HANDOFF '+REQUEST+'\nReview',REQUEST)
        self.assertFalse(self.rpc.closed)
        with self.assertRaisesRegex(ValueError,'retains this recipient'):
            with self.t.prepare(self.target):pass
        self.assertEqual(1,sum(x[0]=='turn/start' for x in self.rpc.calls))
        self.assertEqual('codex-app-server-exact-idle-resume-v1',proof['transport'])
    def test_restart_reconciles_marker_only_in_real_user_item(self):
        receipt={'transport':'codex-app-server-exact-idle-resume-v1'}
        for kind,expected in [('agentMessage',{}),('userMessage','RETURNED')]:
            self.rpc.turns=[{'id':TURN,'status':'completed','items':[{'type':kind,'content':[{'type':'text','text':'HANDOFF '+REQUEST+'\nReview'}]}]}]
            obs=self.t.observe(self.target,REQUEST,receipt)
            self.assertEqual(expected,obs.get('status',{}))
        self.assertFalse(any(x[0] in ['turn/start','thread/resume'] for x in self.rpc.calls))
    def test_exact_turn_survives_a_newer_unrelated_turn(self):
        self.rpc.turns=[{'id':REQUEST,'status':'inProgress'},{'id':TURN,'status':'completed'}]
        obs=self.t.observe(self.target,REQUEST,{'turnId':TURN,'transport':'codex-app-server-exact-idle-resume-v1'})
        self.assertEqual('RETURNED',obs['status']);self.assertEqual(TURN,obs['receipt']['turnId'])

if __name__=='__main__':unittest.main()
