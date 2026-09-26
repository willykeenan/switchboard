import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_taskflow as fixtures
from taskflow import Conflict
from taskflow_delivery import pins
import taskflow_waiver as waiver


class WaiverTests(unittest.TestCase):
    tearDown = fixtures.TaskFlowTests.tearDown
    worker = fixtures.TaskFlowTests.worker

    def setUp(self):
        fixtures.TaskFlowTests.setUp(self)
        self.t = self.store.capture({'request':'Correct one fixture route', 'key':'waiver',
            'projectId':'p', 'laneId':'alpha', 'ownerId':'codex:owner',
            'delivery':fixtures.ARTIFACT_DELIVERY})
        self.log = self.root/'transcript.jsonl'
        self.receipt = self.root/'approval.json'
        self.body = '/approved'
        self.write_approval()
        self.guard = patch.object(waiver, 'TRANSCRIPT_ROOT', self.root)
        self.guard.start();self.addCleanup(self.guard.stop)

    def write_approval(self, **extra):
        self.log.write_text(json.dumps({'type':'session_meta','payload':{'id':'session'}})+'\n'+
            json.dumps({'type':'response_item','payload':{'id':'msg','role':'user',
                'content':[{'type':'input_text','text':self.body}]}})+'\n')
        self.document = {'source':{'role':'user','provider':'codex','sessionId':'session',
            'messageId':'msg','text':self.body,'reference':str(self.log)+'#msg'},
            'scope':{'taskId':self.t['taskId'],'revision':'a'*40,'artifacts':[]},
            'auditRequired':False,'auditDisposition':'WAIVED_BY_USER','auditPassed':False, **extra}
        self.receipt.write_text(json.dumps(self.document))

    def invoke(self, op, actor='codex:owner', **values):
        t = self.store.get(self.t['taskId'])
        return self.store.mutate({'operation':op,'item':{'taskId':t['taskId'],
            'version':t['version'], **values}},actor)

    def record(self):
        return self.invoke('waiver-record',receipt=str(self.receipt))

    def returned(self):
        self.worker(); a = self.store.dispatch()[0]
        self.store.transition(a['id'],a['workerId'],'accept',{'payloadHash':a['payloadHash']})
        p = Path(a['workdir']);p.mkdir(parents=True)
        out=p/'result.md';out.write_text('Verified fixture result')
        self.store.transition(a['id'],a['workerId'],'return',{'summary':'Fixture returned', 'artifacts':[str(out)]})
        return self.store.get(self.t['taskId'])

    def functional(self,t):
        p = self.root/'checks.md';p.write_text('Fixture route exercised successfully')
        return {'pins':pins(t),'summary':'Fixture result tested','checks':[{'id':'result','passed':True}], 'evidence':[str(p)]}

    def test_record_preserves_task_id_and_no_review_or_provider_start(self):
        t=self.record()
        self.assertEqual(t['taskId'],self.t['taskId'])
        self.assertEqual('READY',t['state']);self.assertIsNone(t['assignmentId'])
        self.assertNotIn('review',t);self.assertFalse(t['auditPassed'])
        self.assertEqual([],self.store.detail(t['taskId'])['assignments'])

    def test_exact_retry_is_idempotent(self):
        t=self.record();self.assertEqual(t,self.record())

    def test_readiness_and_waiver_are_one_transaction(self):
        t=self.invoke('waiver-record',receipt=str(self.receipt),ready={
            'definitionOfDone':'Bound new scope', 'scopes':[str(self.root)],
            'capabilities':['general'],'dependencies':[]})
        self.assertEqual('Bound new scope',t['definitionOfDone']);waiver.valid(t)
        self.assertEqual([],self.store.detail(t['taskId'])['assignments'])

    def test_invalid_ready_rolls_back_waiver_together(self):
        before=self.store.get(self.t['taskId'])
        with self.assertRaises((ValueError,Conflict)):
            self.invoke('waiver-record',receipt=str(self.receipt),ready={'capabilities':['unapproved']})
        self.assertEqual(before,self.store.get(self.t['taskId']))

    def test_wrong_actor_rejected(self):
        with self.assertRaisesRegex(Conflict,'exact task owner'):
            self.invoke('waiver-record',actor='codex:other',receipt=str(self.receipt))

    def test_attachment_is_not_user_approval(self):
        self.body='<skill>approved</skill>';self.write_approval()
        with self.assertRaisesRegex(Conflict,'not an approval command'):self.record()

    def test_wrong_original_message_rejected(self):
        self.document['source']['text']='/approved changed';self.receipt.write_text(json.dumps(self.document))
        with self.assertRaisesRegex(Conflict,'identity or text'):self.record()

    def test_wrong_task_scope_rejected(self):
        self.document['scope']['taskId']='another';self.receipt.write_text(json.dumps(self.document))
        with self.assertRaisesRegex(Conflict,'exact original task'):self.record()

    def test_pinned_renewal_scope_names_original_task(self):
        proposal=self.root/'proposal.json';proposal.write_text(json.dumps({'executionRenewal':{'scope':'Original report '+self.t['taskId']+' verification only'}}))
        self.document['scope']={'taskId':'controller','artifacts':[{'path':str(proposal),'sha256':hashlib.sha256(proposal.read_bytes()).hexdigest()}]}
        self.receipt.write_text(json.dumps(self.document));self.record()

    def test_changed_approved_artifact_rejected(self):
        self.test_pinned_renewal_scope_names_original_task()
        (self.root/'proposal.json').write_text('changed')
        with self.assertRaisesRegex(Conflict,'evidence changed'):waiver.valid(self.store.get(self.t['taskId']))

    def test_audit_dispatch_and_manual_claim_are_blocked(self):
        self.record();t=self.returned()
        with self.store.connect() as db:self.assertFalse(self.store.audit.eligible(db,t,'codex:audit'))
        with self.assertRaises(Conflict):self.store.audit.claim(t['taskId'],'codex:audit')
        self.assertEqual('REVIEW',self.store.get(t['taskId'])['state'])

    def test_managed_audit_and_verify_are_not_launched(self):
        t=self.record()
        for phase in ['audit','verify']:
            with self.store.connect() as db:
                self.assertIn('waived',self.store.cycle.eligible(db,t,None,None,{},phase=phase))

    def test_cannot_accept_without_real_return(self):
        t=self.record()
        with self.assertRaisesRegex(Conflict,'required real work'):
            self.invoke('waiver-accept',**self.functional(t))

    def test_waived_artifact_completes_with_real_return_and_checks(self):
        self.record();t=self.returned();t=self.invoke('waiver-accept',**self.functional(t))
        self.assertEqual('DONE',t['state']);self.assertNotIn('review',t['approval'])
        self.assertEqual('WAIVED_BY_USER',t['approval']['auditDisposition'])
        self.assertFalse(t['approval']['auditPassed']);self.store.delivery.valid_approval(t)

    def test_bad_checks_or_wrong_result_pins_do_not_complete(self):
        self.record();t=self.returned()
        for key,value in [('evidence',[]),('pins',{}),('checks',[{'id':'result','passed':False}])]:
            v=self.functional(t);v[key]=value
            with self.assertRaises(Conflict):self.invoke('waiver-accept',**v)
        self.assertEqual('REVIEW',self.store.get(t['taskId'])['state'])

    def test_changed_result_bytes_rejected(self):
        self.record();t=self.returned();Path(t['result']['artifacts'][0]['path']).write_text('changed')
        with self.assertRaisesRegex(Conflict,'evidence changed'):self.invoke('waiver-accept',**self.functional(t))

    def test_amended_requirements_do_not_inherit_waiver(self):
        t=self.record();self.store.amend(t['taskId'],t['version'],'different request','codex:owner')
        with self.assertRaisesRegex(Conflict,'current task requirements'):waiver.valid(self.store.get(t['taskId']))

    def test_local_app_waits_for_actual_installer(self):
        d={'kind':'local-app','destination':{'kind':'local-app','path':str(self.root/'Game.app'),'bundleId':'test.game'},
            'actorId':'codex:b','criteria':[{'id':'result','description':'Exact route'}]}
        t=self.store.delivery.configure(self.t['taskId'],self.t['version'],d,'codex:owner')
        self.record();self.worker();a=self.store.dispatch()[0]
        self.store.transition(a['id'],a['workerId'],'accept',{'payloadHash':a['payloadHash']})
        p=Path(a['workdir']);p.mkdir(parents=True);f=p/'result.md';f.write_text('fixture')
        self.store.transition(a['id'],a['workerId'],'return',{'summary':'tested','artifacts':[str(f)],'sourceCommit':'a'*40})
        t=self.store.get(t['taskId']);t=self.invoke('waiver-accept',**self.functional(t))
        self.assertEqual('DELIVERY_QUEUED',t['state']);self.assertNotIn('completion',t)
        with self.assertRaisesRegex(Conflict,'required real work'):self.invoke('waiver-finish',**self.functional(t))


