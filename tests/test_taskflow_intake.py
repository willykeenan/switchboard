import hashlib
import json
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from taskflow import TaskFlow, Conflict
import test_taskflow as fixtures


class IntakeTests(unittest.TestCase):
    setUp=fixtures.TaskFlowTests.setUp
    tearDown=fixtures.TaskFlowTests.tearDown
    worker=fixtures.TaskFlowTests.worker

    def test_installed_python_accepts_board_utc_timestamp_format(self):
        from taskflow_intake import timestamp
        self.assertEqual(1788980400.0,timestamp('2026-09-09T19:00:00.000Z',-1))
        self.assertEqual(timestamp('2026-09-09T19:00:00+00:00',-1),timestamp('2026-09-09T19:00:00Z',-2))
        self.assertEqual(-1,timestamp('invalid',-1))

    def test_interleaved_iso_legacy_events_keep_actual_chronology(self):
        self.intake(created_at='2026-09-09T12:00:00.000Z');self.store.adopt_intake()
        with self.store.connect() as db:
            db.execute('CREATE TABLE task_events(seq INTEGER,task_id TEXT,actor TEXT,created_at TEXT,payload_json TEXT)')
            db.execute('INSERT INTO task_events VALUES(999,?,?,?,?)',('intake-1','codex:attendant','2026-09-09T13:00:00.000Z','{}'))
        self.time=1788962400.0
        task=self.store.get('intake-1')
        with self.store.connect() as db:self.store._event(db,task,'later','test')
        events=self.store.detail('intake-1')['events']
        self.assertLess(next(i for i,e in enumerate(events) if e['kind']=='legacy-contract-update'),next(i for i,e in enumerate(events) if e['kind']=='later'))
        self.assertEqual(1788955200.0,task['createdAt'])

    def legacy(self,ident='intake-1',progress=None,**extra):
        row={'task_id':ident,'objective':'Existing request','owner':'codex:attendant','status':'BLOCKED','blocker':'Dispatch not installed',
             'next_action':'Wait','version':1,'aliases_json':json.dumps(['alpha']),'scope_json':'[]','criteria_json':'[]',
             'dependencies_json':json.dumps(['attendant']),'created_at':'2026-09-09T12:00:00+00:00','updated_at':'2026-09-09T12:00:00+00:00',
             'progress_json':json.dumps(progress if progress is not None else {'recordKind':'task-intake','sourceRequest':{'text':'Build the exact requested icon'},'canonicalImplementationTaskIds':[],'acceptanceCriteria':['Opens the existing paper'],'directionAmendments':[{'text':'Another agent owns the document; link it.'}]})}
        progress_value=json.loads(row['progress_json']);progress_value.setdefault('delivery',fixtures.ARTIFACT_DELIVERY);row['progress_json']=json.dumps(progress_value)
        row.update(extra)
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks('+','.join(k+' '+('INTEGER' if k=='version' else 'TEXT') for k in row)+')')
            db.execute('DELETE FROM tasks WHERE task_id=?',(ident,))
            db.execute('INSERT INTO tasks VALUES('+','.join('?' for _ in row)+')',list(row.values()))
        return row

    def intake(self,**extra):
        self.legacy('attendant',{'individualBoardTaskIds':['intake-1']},dependencies_json='[]')
        return self.legacy(**extra)

    def test_current_blocked_intake_dispatches_same_id_without_owner_ack(self):
        original=self.intake();self.worker();a=self.store.dispatch()[0]
        self.assertEqual('intake-1',a['taskId']);self.assertEqual([],a['payload']['dependencies'])
        self.assertEqual(['attendant'],a['payload']['intake']['intakeParentIds'])
        self.assertIn('Another agent owns',a['payload']['intake']['sourceProgress']['directionAmendments'][0]['text'])
        self.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']})
        self.assertEqual('ACCEPTED',self.store.get('intake-1')['state'])
        with self.store.connect() as db:self.assertEqual(original,dict(db.execute("SELECT * FROM tasks WHERE task_id='intake-1'").fetchone()))

    def test_adoption_is_idempotent_under_concurrent_dispatch_and_restart(self):
        self.intake();self.worker()
        with ThreadPoolExecutor(2) as pool:offers=list(pool.map(lambda _:self.store.dispatch(),range(2)))
        self.assertEqual(1,sum(map(len,offers)))
        reopened=TaskFlow(self.root,self.flow,lambda:self.time);self.assertEqual([],reopened.adopt_intake())
        self.assertEqual(1,len(reopened.detail('intake-1')['assignments']))

    def test_canonical_link_prevents_duplicate_build_and_remains_inspectable(self):
        self.legacy('canonical',{},status='WORKING',owner='codex:builder',dependencies_json='[]')
        self.intake(progress={'recordKind':'task-intake','canonicalImplementationTaskIds':['canonical']})
        self.worker();self.assertEqual([],self.store.dispatch());t=self.store.get('intake-1')
        self.assertEqual('LINKED',t['state']);self.assertEqual('QUEUED',self.store.detail(t['taskId'])['linkedTasks'][0]['state'])
        with self.assertRaisesRegex(Conflict,'canonical'):self.store.ready(t['taskId'],t['version'],{})

    def test_related_document_is_context_not_a_duplicate_implementation(self):
        self.intake(progress={'recordKind':'task-intake','relatedCanonicalTaskIds':['paper-authoring'],'existingWhitepaperContext':{'owner':'another worker'}})
        self.worker();a=self.store.dispatch()[0]
        self.assertEqual('another worker',a['payload']['intake']['sourceProgress']['existingWhitepaperContext']['owner'])

    def test_real_dependency_is_retained_and_clear_wait_reason_is_shown(self):
        self.legacy('dep',{},dependencies_json='[]');self.intake(dependencies_json=json.dumps(['attendant','dep']));self.worker()
        self.assertEqual([],self.store.dispatch());t=self.store.get('intake-1');self.assertEqual(['dep'],t['dependencies']);self.assertEqual('Waiting for dependency dep',t['waitReason'])
        self.legacy('dep',{},dependencies_json='[]',status='VERIFIED_COMPLETE',version=2)
        self.assertEqual(1,len(self.store.dispatch()))

    def test_missing_policy_does_not_prevent_capture_or_erase_history(self):
        self.intake()
        with self.store.connect() as db:
            db.execute('DELETE FROM taskflow_policies')
            db.execute('CREATE TABLE task_events(seq INTEGER,task_id TEXT,actor TEXT,created_at TEXT,payload_json TEXT)')
            db.execute('INSERT INTO task_events VALUES(1,?,?,?,?)',('intake-1','codex:attendant','2026-09-09','{"request":"Original"}'))
        self.store.adopt_intake();d=self.store.detail('intake-1');self.assertEqual('CAPTURED',d['task']['state'])
        self.assertIn('legacy-contract-update',[e['kind'] for e in d['events']])

    def test_source_amendment_invalidates_unaccepted_payload_without_losing_original(self):
        original=self.intake();self.worker();a=self.store.dispatch()[0]
        p=json.loads(original['progress_json']);p['directionAmendments'].append({'text':'Use the current version'})
        self.legacy(progress=p,version=2);self.store.adopt_intake()
        with self.assertRaises(Conflict):self.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']})
        t=self.store.get('intake-1');self.assertEqual(2,len(t['intake']['sourceProgress']['directionAmendments']));self.assertEqual('Build the exact requested icon',t['request'])

    def test_bookkeeping_update_keeps_offer_valid_and_retains_history(self):
        original=self.intake();self.worker();offer=self.store.dispatch()[0]
        progress=json.loads(original['progress_json'])
        progress.update(persistedAt='2026-09-09T20:00:00Z',installed=False,
                        diagnostics={'lastCheckedAt':'2026-09-09T20:00:00Z'},
                        boardInlineDisplayVerified=True,deliveredToWorker=True)
        self.legacy(progress=progress,version=2);self.store.adopt_intake()
        task=self.store.get('intake-1')
        self.assertEqual(offer['taskVersion'],task['taskVersion'])
        self.assertEqual('OFFERED',task['state'])
        self.assertEqual(progress,task['intake']['sourceProgress'])
        self.assertEqual([],task['amendments'])
        self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})
        self.assertEqual('ACCEPTED',self.store.get('intake-1')['state'])
        self.assertIn('intake-updated',[e['kind'] for e in self.store.detail('intake-1')['events']])

    def test_bookkeeping_keeps_returned_artifact_reviewable(self):
        original=self.intake();self.worker();offer=self.store.dispatch()[0]
        self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})
        work=Path(offer['workdir']);work.mkdir(parents=True);output=work/'result.md';output.write_text('Completed result')
        self.store.transition(offer['id'],'codex:a','return',{'summary':'Complete','artifacts':[str(output)]})
        returned=self.store.get('intake-1')['result']
        progress=json.loads(original['progress_json']);progress.update(persistedAt='new observation',deliveredToWorker=True)
        self.legacy(progress=progress,version=2);self.store.adopt_intake()
        task=self.store.get('intake-1');self.assertEqual('REVIEW',task['state'])
        self.assertEqual(returned,task['result']);self.assertEqual(offer['taskVersion'],task['taskVersion'])
        self.store.audit.claim('intake-1','codex:audit')
        self.store.audit.prepare('intake-1','codex:audit',[],'Inspected exact output against the original request.')
        task=self.store.get('intake-1');evidence=self.root/'review.md';evidence.write_text('PASS exact artifact')
        reviewed=self.store.review(task['taskId'],task['version'],'codex:audit','accept','Verified',[str(evidence)])
        self.assertEqual('DONE',reviewed['state'])

    def assert_contract_change_invalidates_offer(self,field):
        original=self.intake();self.worker();offer=self.store.dispatch()[0]
        progress=json.loads(original['progress_json']);extra={}
        if field=='scope':extra['scope_json']=json.dumps([str(self.root/'new-scope')])
        else:progress['acceptanceCriteria'].append('New required outcome')
        self.legacy(progress=progress,version=2,**extra);self.store.adopt_intake()
        self.assertGreater(self.store.get('intake-1')['taskVersion'],offer['taskVersion'])
        with self.assertRaises(Conflict):self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})

    def test_acceptance_change_still_invalidates_offer(self):
        self.assert_contract_change_invalidates_offer('acceptanceCriteria')

    def test_scope_change_still_invalidates_offer(self):
        self.assert_contract_change_invalidates_offer('scope')

    def test_explicit_requirements_invalidate_an_offer(self):
        original=self.intake();self.worker();offer=self.store.dispatch()[0]
        progress=json.loads(original['progress_json']);progress['requirements']=['New user requirement']
        self.legacy(progress=progress,version=2);self.store.adopt_intake()
        self.assertGreater(self.store.get('intake-1')['taskVersion'],offer['taskVersion'])
        with self.assertRaises(Conflict):self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})

    def test_bookkeeping_keeps_offer_valid_but_retains_metadata(self):
        original=self.intake();self.worker();a=self.store.dispatch()[0]
        progress=json.loads(original['progress_json']);progress['persistedAt']='updated administrative timestamp'
        self.legacy(progress=progress,version=2);self.store.adopt_intake()
        self.assertEqual(a['taskVersion'],self.store.get('intake-1')['taskVersion'])
        self.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']})
        self.assertEqual(progress['persistedAt'],self.store.get('intake-1')['intake']['sourceProgress']['persistedAt'])

    def test_actionable_leaf_kinds_adopt_original_ids_containers_remain_visible(self):
        for kind in ('task-intake','linked-task-subelement','initiative-task','initiative'):
            self.legacy(kind,{'recordKind':kind},dependencies_json='[]')
        adopted=self.store.adopt_intake()
        self.assertEqual({'task-intake','linked-task-subelement','initiative-task'},set(adopted))
        self.assertTrue(self.store.get('initiative')['legacy'])

    def test_changed_source_file_is_never_silently_repinned(self):
        f=self.root/'request.txt';f.write_text('Original');pin=hashlib.sha256(f.read_bytes()).hexdigest()
        self.intake(progress={'recordKind':'task-intake','sourceRequest':{'text':'Original','path':str(f),'sha256':pin}})
        f.write_text('Changed');self.worker();self.assertEqual([],self.store.dispatch())
        self.assertEqual(pin,self.store.get('intake-1')['inputs'][0]['sha256']);self.assertIn('pinned input changed',self.store.get('intake-1')['waitReason'])

    def test_approved_auto_readiness_applies_to_previously_captured_queue(self):
        self.intake();p=self.store.policy('alpha');self.store.save_policy({**p,'enabled':False})
        self.store.adopt_intake();self.assertEqual('CAPTURED',self.store.get('intake-1')['state'])
        p=self.store.policy('alpha');self.store.save_policy({**p,'enabled':True});self.worker()
        self.assertEqual('intake-1',self.store.dispatch()[0]['taskId'])

    def test_project_snapshot_does_not_expose_another_projects_deliveries(self):
        self.intake();self.worker();self.store.dispatch()
        self.assertTrue(any(b['taskId'] for b in self.store.snapshot('p')['birds']))
        self.assertFalse(any(b['taskId'] for b in self.store.snapshot('other')['birds']))

    def test_audit_pool_handles_existing_contracts_with_iso_timestamps(self):
        self.legacy('legacy-review',{},status='REPORTED_DONE');self.intake();self.store.adopt_intake()
        self.assertEqual(0,self.store.audit.snapshot('p')['waiting'])
        with self.assertRaisesRegex(Conflict,'No permitted'):self.store.audit.claim(None,'codex:audit')

    def test_recorded_worker_acceptance_reserves_task_without_faking_runtime(self):
        self.intake(progress={'recordKind':'task-intake','implementationAccepted':True,'workerAcceptance':{'worker':'codex:b','dispatcherClaimVerified':False,'executionStartedVerified':False}})
        self.worker();self.assertEqual([],self.store.dispatch());t=self.store.get('intake-1')
        self.assertEqual('RESERVED',t['state']);self.assertEqual('board:alpha',t['station']);self.assertIsNone(t['assignmentId']);self.assertFalse(t['heldByWorker'])
        with self.assertRaisesRegex(Conflict,'existing worker'):self.store.ready(t['taskId'],t['version'],{})

    def test_retained_blocked_contract_stays_on_board_with_recorded_owner(self):
        self.legacy('retained',{},owner='codex:a',dependencies_json='[]')
        task=self.store.get('retained')
        self.assertEqual('board:alpha',task['station']);self.assertEqual('BLOCKED',task['state']);self.assertEqual('codex:a',task['ownerId'])
        self.assertIn('not live execution',task['locationBasis'])

    def test_adopted_reservation_has_replayable_recorded_individual_path(self):
        self.intake(progress={'recordKind':'task-intake','implementationAccepted':True,'workerAcceptance':{'worker':'codex:b'}})
        self.store.adopt_intake();event=self.store.detail('intake-1')['events'][-1]
        self.assertEqual('board:alpha',event['source']);self.assertEqual('codex:b',event['destination'])

    def test_history_is_complete_and_chronological_after_large_live_updates(self):
        self.intake();self.store.adopt_intake()
        with self.store.connect() as db:
            t=self.store.get('intake-1')
            for i in range(600):self.store._event(db,t,'worker-message','codex:a',{'text':str(i)})
        self.assertEqual(601,len(self.store.detail('intake-1')['events']))

    def test_all_executable_kinds_adopt_original_ids_and_containers_stay_visible(self):
        self.legacy('initiative',{'recordKind':'initiative','taskIds':['initiative-leaf'],'countAsTask':False},dependencies_json='[]')
        self.legacy('parent',{'recordKind':'task-intake','childTaskIds':['child']},dependencies_json='[]')
        self.legacy('child',{'recordKind':'linked-task-subelement','parentTaskId':'parent'},dependencies_json='["parent"]')
        self.legacy('initiative-leaf',{'recordKind':'initiative-task','initiativeId':'initiative'},dependencies_json='["child"]')
        self.legacy('leaf',{'recordKind':'task-intake'},dependencies_json='[]')
        self.legacy('unknown',{'recordKind':'new-unrecognized-kind'},dependencies_json='[]')
        self.assertCountEqual(['child','initiative-leaf','leaf'],self.store.adopt_intake())
        self.assertEqual([],self.store.get('child')['dependencies'])
        self.assertEqual(['parent'],self.store.get('child')['intake']['intakeParentIds'])
        self.assertEqual(['child'],self.store.get('initiative-leaf')['dependencies'])
        for ident in ('parent','initiative','unknown'):
            task=self.store.get(ident);self.assertTrue(task['legacy'])
            self.assertFalse(task['intakeDisposition']['executable'])
            self.assertIn(task['intakeDisposition']['reason'],task['waitReason'])
        self.assertEqual(6,len(self.store.list('p')))
        self.assertEqual([],self.store.adopt_intake())

    def test_child_preserves_existing_parent_implementation_custody(self):
        self.legacy('parent',{'recordKind':'task-intake','canonicalImplementationTaskIds':['canonical']},dependencies_json='[]')
        self.legacy('child',{'recordKind':'linked-task-subelement','parentTaskId':'parent'},dependencies_json='[]')
        self.store.adopt_intake();self.worker();self.assertEqual([],self.store.dispatch())
        task=self.store.get('child');self.assertEqual('LINKED',task['state'])
        self.assertEqual(['canonical'],task['intake']['canonicalTaskIds'])

    def test_new_children_do_not_duplicate_a_running_parent(self):
        self.legacy('parent',{'recordKind':'task-intake'},dependencies_json='[]');self.worker()
        offer=self.store.dispatch()[0];self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})
        self.legacy('child',{'recordKind':'linked-task-subelement','parentTaskId':'parent'},dependencies_json='[]')
        self.worker('b');self.assertEqual([],self.store.dispatch())
        self.assertEqual('ACCEPTED',self.store.get('parent')['state'])
        self.assertEqual('LINKED',self.store.get('child')['state'])
        self.assertEqual(['parent'],self.store.get('child')['intake']['canonicalTaskIds'])

    def test_subelement_then_initiative_leaf_follow_real_dependency(self):
        self.legacy('child',{'recordKind':'linked-task-subelement'},dependencies_json='[]')
        self.legacy('initiative-leaf',{'recordKind':'initiative-task'},dependencies_json='["child"]')
        self.worker();self.worker('b');offer=self.store.dispatch()[0];self.assertEqual('child',offer['taskId'])
        self.assertEqual('Waiting for dependency child',self.store.get('initiative-leaf')['waitReason'])
        self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})
        work=Path(offer['workdir']);work.mkdir(parents=True);output=work/'output.md';output.write_text('Child output')
        self.store.transition(offer['id'],'codex:a','return',{'summary':'Child complete','artifacts':[str(output)]})
        self.store.audit.claim('child','codex:audit');self.store.audit.prepare('child','codex:audit',[],'Reviewed child artifact.')
        evidence=self.root/'review.md';evidence.write_text('PASS');task=self.store.get('child')
        self.store.review('child',task['version'],'codex:audit','accept','Verified',[str(evidence)])
        self.store.idle('codex:a','codex:a');next_offer=self.store.dispatch()[0]
        self.assertEqual('initiative-leaf',next_offer['taskId'])
        self.assertEqual('initiative-task',next_offer['payload']['intake']['sourceProgress']['recordKind'])


if __name__=='__main__':unittest.main()


class ParentReviewCustodyTests(unittest.TestCase):
    setUp=IntakeTests.setUp
    tearDown=IntakeTests.tearDown
    worker=IntakeTests.worker
    legacy=IntakeTests.legacy
    intake=IntakeTests.intake
    def test_new_child_preserves_parent_result_version_and_independent_claim(self):
        self.intake();self.worker();offer=self.store.dispatch()[0]
        self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})
        work=Path(offer['workdir']);work.mkdir(parents=True);output=work/'result.md';output.write_text('Parent result')
        self.store.transition(offer['id'],'codex:a','return',{'summary':'Parent complete','artifacts':[str(output)]})
        self.store.audit.claim('intake-1','codex:audit')
        before=self.store.get('intake-1')
        self.legacy('child',{'recordKind':'initiative-task','parentTaskId':'intake-1'},dependencies_json='[]')
        self.store.adopt_intake();parent=self.store.get('intake-1');child=self.store.get('child')
        self.assertEqual('REVIEW',parent['state']);self.assertEqual(before['taskVersion'],parent['taskVersion'])
        self.assertEqual(before['result'],parent['result']);self.assertFalse(parent['intakeDisposition']['executable'])
        self.assertIn('intake-1',child['intake']['canonicalTaskIds'])
        self.assertEqual([],self.store.dispatch())
        self.store.audit.prepare('intake-1','codex:audit',[],'Reviewed the original parent result; child work retains its separate task.')
        evidence=self.root/'parent-review.md';evidence.write_text('PASS original parent result')
        current=self.store.get('intake-1')
        result=self.store.review('intake-1',current['version'],'codex:audit','accept','Original result verified',[str(evidence)])
        self.assertEqual('DONE',result['state']);self.assertEqual(before['result'],result['result'])

    def test_parent_review_and_child_custody_are_identical_in_both_id_orders(self):
        for parent_id,child_id in [('aa-parent','zz-child'),('zz-parent','aa-child')]:
            with self.subTest(parent=parent_id):
                case=ParentReviewCustodyTests();case.setUp()
                try:
                    case.legacy(parent_id,{'recordKind':'task-intake'},dependencies_json='[]');case.worker()
                    offer=case.store.dispatch()[0];case.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})
                    work=Path(offer['workdir']);work.mkdir(parents=True);output=work/'parent.md';output.write_text('Parent result')
                    case.store.transition(offer['id'],'codex:a','return',{'summary':'Returned','artifacts':[str(output)]})
                    case.store.audit.claim(parent_id,'codex:audit');before=case.store.get(parent_id)
                    case.legacy(child_id,{'recordKind':'linked-task-subelement','parentTaskId':parent_id},dependencies_json='[]')
                    case.store.adopt_intake();parent=case.store.get(parent_id);child=case.store.get(child_id)
                    self.assertEqual('REVIEW',parent['state']);self.assertEqual(before['taskVersion'],parent['taskVersion']);self.assertEqual(before['result'],parent['result'])
                    self.assertEqual([parent_id],child['intake']['canonicalTaskIds']);self.assertEqual([],case.store.dispatch())
                    case.store.audit.prepare(parent_id,'codex:audit',[],'Exact returned parent result reviewed; child retains custody link')
                    evidence=case.root/'review.md';evidence.write_text('PASS parent result');current=case.store.get(parent_id)
                    self.assertEqual('DONE',case.store.review(parent_id,current['version'],'codex:audit','accept','Verified',[str(evidence)])['state'])
                finally:case.tearDown()

    def test_only_parent_custody_change_invalidates_unaccepted_child(self):
        self.legacy('parent',{'recordKind':'task-intake'},dependencies_json='[]')
        self.legacy('child',{'recordKind':'linked-task-subelement','parentTaskId':'parent'},dependencies_json='[]')
        self.worker();offer=self.store.dispatch()[0];self.assertEqual('child',offer['taskId'])
        self.legacy('canonical',{},status='WORKING',owner='codex:canonical',dependencies_json='[]')
        self.legacy('parent',{'recordKind':'task-intake','canonicalImplementationTaskIds':['canonical']},version=2,dependencies_json='[]')
        with self.assertRaises(Conflict):self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})
        child=self.store.get('child');self.assertEqual(1,child['intake']['sourceVersion']);self.assertEqual(['canonical'],child['intake']['canonicalTaskIds'])
        self.assertGreater(child['taskVersion'],offer['taskVersion'])

    def test_parent_custody_change_retains_already_accepted_child_assignment(self):
        self.legacy('parent',{'recordKind':'task-intake'},dependencies_json='[]')
        self.legacy('child',{'recordKind':'linked-task-subelement','parentTaskId':'parent'},dependencies_json='[]')
        self.worker();offer=self.store.dispatch()[0];self.store.transition(offer['id'],'codex:a','accept',{'payloadHash':offer['payloadHash']})
        self.legacy('canonical',{},status='WORKING',owner='codex:canonical',dependencies_json='[]')
        self.legacy('parent',{'recordKind':'task-intake','canonicalImplementationTaskIds':['canonical']},version=2,dependencies_json='[]')
        self.store.adopt_intake();child=self.store.get('child');self.assertEqual('ACCEPTED',child['state']);self.assertEqual(offer['id'],child['assignmentId'])
        self.assertEqual(['canonical'],child['intake']['canonicalTaskIds']);self.assertEqual([],self.store.dispatch())

class LateAcceptanceObservationTests(unittest.TestCase):
    setUp=IntakeTests.setUp
    tearDown=IntakeTests.tearDown
    worker=IntakeTests.worker
    legacy=IntakeTests.legacy
    def test_late_acceptance_preserves_review_and_real_verdict_for_same_or_other_worker(self):
        for worker in ('codex:a','codex:b'):
            for verdict in ('accept','revise'):
                with self.subTest(worker=worker,verdict=verdict):
                    case=IntakeTests();case.setUp()
                    try:
                        original=case.legacy('late',dependencies_json='[]');case.worker();a=case.store.dispatch()[0]
                        case.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']});work=Path(a['workdir']);work.mkdir(parents=True);f=work/'result.md';f.write_text('Exact returned result')
                        case.store.transition(a['id'],'codex:a','return',{'summary':'Ready','artifacts':[str(f)]});case.store.audit.claim('late','codex:audit');case.store.audit.prepare('late','codex:audit',[],'Reviewed exact original result')
                        before=case.store.get('late');claim=case.store.audit.detail('late')['claims'];progress=json.loads(original['progress_json']);progress.update(implementationAccepted=True,workerAcceptance={'worker':worker})
                        case.legacy('late',progress,version=2,dependencies_json='[]');case.store.adopt_intake();after=case.store.get('late')
                        for key in ('state','taskVersion','workerId','assignmentId','result','audit','station'):case.assertEqual(before.get(key),after.get(key),key)
                        case.assertEqual(claim,case.store.audit.detail('late')['claims']);case.assertEqual(worker,after['intake']['acceptanceObservation']['reportedWorkerId']);case.assertEqual('codex:a',after['intake']['acceptanceObservation']['authoritativeWorkerId'])
                        e=case.root/'findings.md';e.write_text('Exact result assessed');result=case.store.review('late',after['version'],'codex:audit',verdict,'Criteria assessed',[str(e)])
                        case.assertEqual('DONE' if verdict=='accept' else 'READY',result['state']);case.assertEqual('late',result['taskId'])
                    finally:case.tearDown()

    def test_late_acceptance_does_not_hide_real_changed_criteria(self):
        original=self.legacy('late',dependencies_json='[]');self.worker();a=self.store.dispatch()[0];self.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']});before=self.store.get('late')
        progress=json.loads(original['progress_json']);progress.update(implementationAccepted=True,workerAcceptance={'worker':'codex:b'},acceptanceCriteria=['A changed result is required'])
        self.legacy('late',progress,version=2,dependencies_json='[]');self.store.adopt_intake();after=self.store.get('late')
        self.assertEqual('ACCEPTED',after['state']);self.assertEqual('codex:a',after['workerId']);self.assertGreater(after['taskVersion'],before['taskVersion']);self.assertTrue(self.store.context('codex:a')['assignments'][0]['instructionsChanged'])

    def test_late_conflicting_acceptance_preserves_all_typed_delivery_stages(self):
        import test_taskflow_delivery as delivery_fixtures
        for stage in ('DELIVERY_QUEUED','DELIVERY_OFFERED','DELIVERING','VERIFY'):
            with self.subTest(stage=stage):
                case=delivery_fixtures.DeliveryTests();case.setUp()
                try:
                    progress={'recordKind':'task-intake','acceptanceCriteria':['Exact destination contents'],'delivery':case.contract};original=IntakeTests.legacy(case,'late',progress,dependencies_json='[]');case.worker();case.worker('b');a=case.store.dispatch('codex:a')[0]
                    case.store.transition(a['id'],'codex:a','accept',{'payloadHash':a['payloadHash']});work=Path(a['workdir']);work.mkdir(parents=True);f=work/'result.md';f.write_text('Exact document result');case.store.transition(a['id'],'codex:a','return',{'summary':'Ready','artifacts':[str(f)]});case.review(case.store.get('late'))
                    if stage!='DELIVERY_QUEUED':case.store.delivery.available('codex:b');d=case.store.delivery.context('codex:b')['attempts'][0]
                    if stage in ('DELIVERING','VERIFY'):
                        case.store.delivery.transition(d['id'],'codex:b','accept',{'payloadHash':d['payloadHash']});case.store.delivery.transition(d['id'],'codex:b','start',{'payloadHash':d['payloadHash'],'threadId':'b','turnId':'fixture-audit-turn'})
                    if stage=='VERIFY':case.store.delivery.transition(d['id'],'codex:b','return',case.receipt(case.store.get('late')))
                    before=case.store.get('late');attempts=case.store.delivery.detail('late');progress.update(implementationAccepted=True,workerAcceptance={'worker':'codex:b'});IntakeTests.legacy(case,'late',progress,version=2,dependencies_json='[]');case.store.adopt_intake();after=case.store.get('late')
                    case.assertEqual(stage,after['state']);case.assertEqual(attempts,case.store.delivery.detail('late'))
                    for key in ('taskVersion','workerId','assignmentId','result','approval','deliveryResult','station'):case.assertEqual(before.get(key),after.get(key),key)
                finally:case.tearDown()
