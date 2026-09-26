import copy
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from taskflow import TaskFlow, Conflict, digest


ARTIFACT_DELIVERY={'kind':'artifact','destination':{'kind':'task-result'},'criteria':[{'id':'result','description':'The requested result is independently verified in this task'}]}

class Flow:
    def __init__(self):
        self.state={'revision':1,'enabled':True,'defaultCommunication':'same-lane','connections':[],
                    'placements':[{'agentId':'codex:'+x,'laneId':'alpha','role':'auditor' if x=='audit' else 'worker'} for x in ['a','b','audit']],
                    'laneCatalog':[{'id':'alpha','projectId':'p','name':'Test','status':'active'}]}
        self.workspace=self
    def read(self):
        return {**copy.deepcopy(self.state),'lanes':copy.deepcopy(self.state['laneCatalog'])}
    def catalog(self):
        return {'sessions':[{'agent_id':p['agentId'],'title':p['agentId'],'endpoint':p['agentId'].split(':',1)[1],'model':'fixture','reasoning':'ultra','activity':{'source':'Codex event log','turnId':'fixture-audit-turn','observedAt':getattr(self,'clock',lambda:1000)(),'fresh':True,'active':True,'turnStatus':'open'}} for p in self.state['placements']]}


class TaskFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.time=1000.0;self.flow=Flow();self.flow.clock=lambda:self.time
        self.store=TaskFlow(self.root,self.flow,lambda:self.time)
        self.store.save_policy({'projectId':'p','laneId':'alpha','enabled':True,'autoReady':True,'scopes':[str(self.root)],'capabilities':['general'],'maxMinutes':5})
    def tearDown(self):self.tmp.cleanup()
    def worker(self,name='a',**extra):
        w=self.store.enroll({'workerId':'codex:'+name,'projectId':'p','laneId':'alpha','scopes':[str(self.root)],**extra})
        self.store.idle(w['workerId'],w['workerId']);return w
    def task(self,key='one',**extra):
        return self.store.capture({'request':'Write the requested result','key':key,'projectId':'p','laneId':'alpha','delivery':ARTIFACT_DELIVERY,**extra})
    def offer(self):
        self.worker();t=self.task();a=self.store.dispatch()[0];return t,a
    def accepted(self):
        t,a=self.offer();self.store.transition(a['id'],a['workerId'],'accept',{'payloadHash':a['payloadHash']});return t,a
    def returned(self):
        t,a=self.accepted();p=Path(a['workdir']);p.mkdir(parents=True);f=p/'result.md';f.write_text('A concrete result')
        self.store.transition(a['id'],a['workerId'],'return',{'summary':'Result ready','artifacts':[str(f)]});return self.store.get(t['taskId']),a

    def test_named_courier_history_survives_return_reuse_and_restart(self):
        task,first=self.returned()
        bird=next(b for b in self.store.snapshot('p')['birds'] if b['id']==first['courierId'])
        self.assertEqual('Finch',bird['name']);self.assertEqual('standby',bird['state'])
        self.assertEqual('RETURNED',bird['history'][0]['state'])
        self.assertEqual('board:alpha',bird['history'][0]['source'])
        self.assertEqual(first['payloadHash'],bird['history'][0]['payloadHash'])
        self.assertNotIn('receiptSecret',str(bird))
        self.store.idle('codex:a','codex:a');secondTask=self.task('second');second=self.store.dispatch()[0]
        self.assertEqual(first['courierId'],second['courierId'])
        reopened=TaskFlow(self.root,self.flow,lambda:self.time)
        bird=next(b for b in reopened.snapshot('p')['birds'] if b['id']==first['courierId'])
        self.assertEqual(secondTask['taskId'],bird['taskId']);self.assertEqual(2,len(bird['history']))
        secondBird=next(b for b in reopened.snapshot('p')['birds'] if b['id']==second['courierId'])
        self.assertEqual(secondTask['taskId'],secondBird['taskId'])
        self.assertIn(task['taskId'],[h['taskId'] for h in bird['history']])
        self.assertTrue(all(not b['history'] for b in reopened.snapshot('foreign')['birds']))

    def test_capture_persists_without_owner_or_workers(self):
        a=self.task();b=self.task('two');self.assertNotEqual(a['taskId'],b['taskId'])
        reopened=TaskFlow(self.root,self.flow,lambda:self.time)
        self.assertEqual(2,len(reopened.list('p','alpha')));self.assertIsNone(a['ownerId'])

    def test_cancelled_ack_requires_recorded_request(self):
        task,assignment=self.accepted()
        with self.assertRaisesRegex(Conflict,'No cancellation was requested'):
            self.store.transition(assignment['id'],'codex:a','cancelled',{'reason':'Cannot finish'})
        self.assertEqual('ACCEPTED',self.store.get(task['taskId'])['state'])
        task=self.store.get(task['taskId']);self.store.cancel(task['taskId'],task['version'])
        self.store.transition(assignment['id'],'codex:a','cancelled',{'reason':'Acknowledged requested cancellation'})
        self.assertEqual('CANCELLED',self.store.get(task['taskId'])['state'])

    def test_worker_failure_retains_request_at_board_for_recovery(self):
        task,assignment=self.accepted()
        self.store.transition(assignment['id'],'codex:a','fail',{'reason':'Required compiler unavailable'})
        failed=self.store.get(task['taskId'])
        self.assertEqual('BLOCKED',failed['state']);self.assertEqual('board:alpha',failed['station'])
        self.assertEqual('codex:a',failed['blockedAtStation'])
        self.assertEqual(task['request'],failed['request']);self.assertEqual([],self.store.active())
        self.store.ready(failed['taskId'],failed['version'],{})
        self.store.idle('codex:a','codex:a')
        self.assertEqual(task['taskId'],self.store.dispatch()[0]['taskId'])
    def test_lost_capture_ack_is_idempotent_but_changed_body_is_rejected(self):
        a=self.task();self.assertEqual(a,self.task())
        with self.assertRaises(Conflict):self.task(request='different')
        self.assertEqual(1,len(self.store.list()))
    def test_owner_unavailable_does_not_block_dispatch(self):
        self.worker();t=self.task(ownerId='codex:absent-owner');a=self.store.dispatch()[0]
        self.assertEqual(t['taskId'],a['taskId']);self.assertEqual('codex:a',a['workerId'])
    def test_two_dispatchers_cannot_duplicate_task_or_worker(self):
        self.worker();self.task();self.task('two')
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.store.dispatch(),range(2)))
        self.assertEqual(1,sum(map(len,results)));self.assertEqual(1,len(self.store.offers('codex:a')))
    def test_scope_conflict_blocks_second_worker(self):
        self.worker();self.worker('b');self.task();self.task('two')
        self.assertEqual(1,len(self.store.dispatch()))
    def test_disjoint_work_can_dispatch_concurrently(self):
        self.worker();self.worker('b');a=self.task();b=self.task('two')
        for t,n in [(a,'a'),(b,'b')]:self.store.ready(t['taskId'],t['version'],{'scopes':[str(self.root/n)],'capabilities':['general'],'definitionOfDone':'Complete scoped work'})
        self.assertEqual(2,len(self.store.dispatch()))
    def test_stale_availability_is_not_idle(self):
        self.worker();self.task();self.time+=61
        self.assertEqual([],self.store.dispatch());self.assertIn('stale',self.store.list()[0]['waitReason'])
    def test_placement_change_prevents_dispatch(self):
        self.worker();self.task();self.flow.state['placements']=[]
        self.assertEqual([],self.store.dispatch())
    def test_imported_worker_cannot_be_auto_resumed(self):
        with self.assertRaises(Conflict):self.worker(mode='managed')
    def test_exact_payload_hash_and_identity_required(self):
        t,a=self.offer()
        with self.assertRaises(Conflict):self.store.transition(a['id'],'codex:a','accept',{'payloadHash':'wrong'})
        with self.assertRaises(ValueError):self.store.transition(a['id'],'codex:b','accept',{'payloadHash':a['payloadHash']})
        self.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']})
        self.assertEqual('ACCEPTED',self.store.get(t['taskId'])['state'])
    def test_amendment_invalidates_not_started_offer(self):
        t,a=self.offer();t=self.store.get(t['taskId']);self.store.amend(t['taskId'],t['version'],'Use the new constraints')
        with self.assertRaises(Conflict):self.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']})
    def test_policy_revocation_prevents_accept(self):
        t,a=self.offer();p=self.store.policy('alpha');self.store.save_policy({**p,'enabled':False})
        with self.assertRaises(Conflict):self.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']})
    def test_restart_retains_uncertain_owner(self):
        t,a=self.accepted();self.store.transition(a['id'],'codex:a','uncertain',{'reason':'Lost provider response'})
        other=TaskFlow(self.root,self.flow,lambda:self.time);self.assertEqual('UNCERTAIN',other.get(t['taskId'])['state'])
        self.assertEqual([],other.dispatch())
        with self.assertRaises(Conflict):other.idle('codex:a','codex:a')
    def test_inputs_are_pinned_and_changed_input_blocks_offer(self):
        f=self.root/'input.txt';f.write_text('original');self.worker();t=self.task(inputs=[str(f)]);f.write_text('changed')
        self.assertEqual([],self.store.dispatch());self.assertIn('input changed',self.store.get(t['taskId'])['waitReason'])
    def test_return_is_review_not_completion_and_self_review_is_rejected(self):
        t,a=self.returned();self.assertEqual('REVIEW',t['state']);review=self.root/'review.md';review.write_text('Checked result')
        with self.assertRaises(ValueError):self.store.review(t['taskId'],t['version'],'codex:a','accept','Pass',[str(review)])
        self.store.audit.claim(t['taskId'],'codex:audit')
        self.store.audit.prepare(t['taskId'],'codex:audit',[],'Fixture context is the exact task and returned artifacts; no Library records exist.')
        t=self.store.get(t['taskId'])
        result=self.store.review(t['taskId'],t['version'],'codex:audit','accept','Pass',[str(review)])
        self.assertEqual('DONE',result['state'])
    def test_rejected_review_preserves_result_and_history(self):
        t,a=self.returned();review=self.root/'review.md';review.write_text('Needs correction')
        self.store.audit.claim(t['taskId'],'codex:audit')
        self.store.audit.prepare(t['taskId'],'codex:audit',[],'Fixture context is the exact task and returned artifacts; no Library records exist.')
        t=self.store.get(t['taskId'])
        result=self.store.review(t['taskId'],t['version'],'codex:audit','revise','Missing requested detail',[str(review)])
        self.assertEqual('READY',result['state']);self.assertTrue(result['result']['artifacts'])
        self.assertEqual(['captured','offered','accepted','returned','audit-claimed','audit-context','review-revise'],[e['kind'] for e in self.store.detail(t['taskId'])['events']])
    def test_changed_result_cannot_be_accepted(self):
        t,a=self.returned();Path(t['result']['artifacts'][0]['path']).write_text('tampered');review=self.root/'review.md';review.write_text('review')
        with self.assertRaises(Conflict):self.store.review(t['taskId'],t['version'],'codex:audit','accept','Pass',[str(review)])
    def test_handoff_includes_prior_artifact_provenance(self):
        t,a=self.returned();next_task=self.store.handoff(t['taskId'],t['version'],{'definitionOfDone':'Review and extend result','scopes':t['scopes'],'capabilities':['general']},'operator-ui')
        self.worker('b');offered=self.store.dispatch('codex:b')[0]
        self.assertEqual(t['taskId'],offered['taskId']);self.assertEqual(t['result'],offered['payload']['priorResults'][0])
        self.assertEqual('codex:a',offered['payload']['returnTo'])
    def test_handoff_respects_blocked_peer_connection(self):
        t,a=self.returned();self.store.handoff(t['taskId'],t['version'],{'definitionOfDone':'Next','scopes':t['scopes'],'capabilities':['general']},'operator-ui')
        self.worker('b');self.flow.state['connections']=[{'from':'codex:a','to':'codex:b','allow':False}]
        self.assertEqual([],self.store.dispatch('codex:b'))
    def test_task_detail_never_exposes_receipt_secrets(self):
        t,a=self.offer();self.assertNotIn(a['receiptSecret'],json.dumps(self.store.detail(t['taskId'])))
    def test_dependency_cycle_is_refused(self):
        a=self.task();b=self.task('two');self.store.ready(a['taskId'],a['version'],{'definitionOfDone':'A','capabilities':['general'],'dependencies':[b['taskId']]})
        with self.assertRaises(ValueError):self.store.ready(b['taskId'],b['version'],{'definitionOfDone':'B','capabilities':['general'],'dependencies':[a['taskId']]})


if __name__=='__main__':unittest.main()
