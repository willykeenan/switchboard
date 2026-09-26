import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import current_context as cc


class CurrentContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.code = self.root/'runner.py'; self.code.write_text('raise RuntimeError("NEVER_EXECUTE")\n')
        self.handoff = self.root/'HANDOFF.json'; self.handoff.write_text('{"review":"PENDING_DIRECTOR"}')
        self.acceptance = self.root/'ACCEPTANCE.json'; self.acceptance.write_text('{"verdict":"ACCEPT_SYNTHETIC_ONLY","profitProven":false}')
        self.manifest = self.root/'COMPONENTS.json'
        self.data = {'schemaVersion':cc.SCHEMA, 'contextId':'lnd', 'owner':'codex:a',
                     'project':'demo', 'objective':'Recover the original profitable system',
                     'summary':'Existing components and remaining qualification', 'asOf':cc.utc_now(),
                     'boundaries':['Synthetic checks do not prove profit.'], 'components':[self.component()]}
        self.pin = self.write_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def evidence(self, id, path, assertions=None):
        row = {'id':id,'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        if assertions is not None: row['assertions'] = assertions
        return row

    def component(self, id='synthetic-accounting'):
        return {'id':id,'title':'Accounting engine','purpose':'Retain implemented fee and fill mechanics',
                'states':{'implementation':{'status':'implemented','evidence':['code']},
                          'testing':{'status':'passed','evidence':['accepted'],'detail':'Fabricated inputs only'},
                          'integration':{'status':'not-integrated','evidence':[]},
                          'qualification':{'status':'unproven','evidence':[]}},
                'evidence':[self.evidence('code',self.code),self.evidence('handoff',self.handoff),
                            self.evidence('accepted',self.acceptance,[{'pointer':'/verdict','equals':'ACCEPT_SYNTHETIC_ONLY'},
                                                                    {'pointer':'/profitProven','equals':False}])],
                'latestAcceptance':'accepted',
                'historical':[{'evidence':'handoff','status':'PENDING_DIRECTOR','supersededBy':'accepted'}],
                'historicalTaskIds':['old-stage'],
                'gap':{'description':'Actual market inputs are not qualified','owner':'codex:a',
                       'nextAction':'Bind qualified historical inputs; retain the accepted synthetic engine.'}}

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.data))
        return hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def verify(self):
        return cc.verify_manifest(str(self.manifest),self.pin,'codex:a')

    def register(self, **kwargs):
        return cc.register(self.root,str(self.manifest),self.pin,'codex:a','a',**kwargs)

    def test_pending_is_historical_after_exact_acceptance_without_modifying_inputs(self):
        before = self.handoff.read_bytes()
        c = self.verify()['components'][0]
        self.assertEqual('EVIDENCE_VERIFIED',c['status'])
        self.assertEqual('HISTORICAL_SUPERSEDED',c['historical'][0]['display'])
        self.assertTrue(c['latestAcceptanceVerified'])
        self.assertEqual(before,self.handoff.read_bytes())

    def test_old_completed_component_survives_registration_and_unrelated_recency(self):
        self.data['components'] = [self.component('component-'+str(i)) for i in range(12)]
        self.pin = self.write_manifest(); self.register()
        p = cc.project_for_endpoint(self.root,'a')
        self.assertEqual(12,len(p['contexts'][0]['components']))
        self.assertEqual(['component-'+str(i) for i in range(12)],
                         [c['id'] for c in p['contexts'][0]['components']])
        self.assertEqual(12,p['contexts'][0]['coverage']['displayedComponents'])

    def test_changed_acceptance_downgrades_affected_state_and_reports_exact_path(self):
        self.register(); self.acceptance.write_text('{"verdict":"REJECT"}')
        p = cc.project_for_endpoint(self.root,'a'); c = p['contexts'][0]['components'][0]
        self.assertEqual('PARTIAL',p['status']); self.assertEqual('unknown',c['states']['testing']['status'])
        self.assertEqual('passed',c['states']['testing']['declaredStatus'])
        self.assertEqual('implemented',c['states']['implementation']['status'])
        self.assertEqual('UNRESOLVED',c['historical'][0]['display'])
        self.assertFalse(c['latestAcceptanceVerified'])
        self.assertIn(str(self.acceptance),[e['path'] for e in c['evidence'] if e['error']])

    def test_missing_evidence_cannot_claim_testing_complete(self):
        self.acceptance.unlink(); c = self.verify()['components'][0]
        self.assertEqual('unknown',c['states']['testing']['status'])
        self.assertFalse(c['latestAcceptanceVerified'])
        with self.assertRaises(cc.ContextError): self.register()

    def test_changed_manifest_preserves_ids_with_explicit_unavailable_status(self):
        self.register(); self.manifest.write_text(self.manifest.read_text()+'\n')
        p = cc.project_for_endpoint(self.root,'a')['contexts'][0]
        self.assertEqual('CONTEXT_UNAVAILABLE',p['status']); self.assertEqual([],p['components'])
        self.assertEqual('synthetic-accounting',p['registeredComponents'][0]['id'])

    def test_missing_manifest_is_visible_instead_of_omitted(self):
        self.register(); self.manifest.unlink()
        p = cc.project_for_endpoint(self.root,'a')
        self.assertEqual(1,p['coverage']['registered']); self.assertEqual('PARTIAL',p['status'])
        self.assertEqual('CONTEXT_UNAVAILABLE',p['contexts'][0]['status'])

    def test_proof_dimensions_and_authority_do_not_collapse(self):
        p = self.verify(); c = p['components'][0]
        self.assertEqual('passed',c['states']['testing']['status'])
        self.assertEqual('not-integrated',c['states']['integration']['status'])
        self.assertEqual('unproven',c['states']['qualification']['status'])
        self.assertFalse(p['authority']); self.assertFalse(p['freshness']['exhaustiveCurrentState'])

    def test_positive_test_state_requires_assertion_not_only_hash(self):
        self.data['components'][0]['states']['testing']['evidence'] = ['code']
        self.pin = self.write_manifest()
        with self.assertRaisesRegex(cc.ContextError,'explicit assertion'): self.verify()

    def test_false_and_zero_are_not_interchangeable_evidence_assertions(self):
        self.data['components'][0]['evidence'][2]['assertions'][1]['equals'] = 0
        self.pin = self.write_manifest(); c = self.verify()['components'][0]
        self.assertEqual('unknown',c['states']['testing']['status'])
        self.assertIn('assertion mismatch',c['evidence'][2]['error'])

    def test_symlink_and_fifo_are_rejected_without_following_or_blocking(self):
        self.code.unlink(); self.code.symlink_to(self.acceptance)
        self.assertEqual('unknown',self.verify()['components'][0]['states']['implementation']['status'])
        self.code.unlink(); os.mkfifo(self.code)
        self.assertEqual('unknown',self.verify()['components'][0]['states']['implementation']['status'])

    def test_document_and_manifest_bounds_are_explicit(self):
        with mock.patch.object(cc,'MAX_DOCUMENT',16):
            self.assertEqual('PARTIAL_EVIDENCE_INVALID',self.verify()['status'])
        with mock.patch.object(cc,'MAX_MANIFEST',16):
            with self.assertRaisesRegex(cc.ContextError,'byte bound'): self.verify()

    def test_duplicate_ids_and_keys_are_not_silently_overwritten(self):
        self.data['components'].append(copy.deepcopy(self.data['components'][0])); self.pin = self.write_manifest()
        with self.assertRaisesRegex(cc.ContextError,'duplicate component'): self.verify()
        with self.assertRaisesRegex(cc.ContextError,'duplicate JSON key'): cc.parsed(b'{"a":1,"a":2}')

    def test_explicit_audience_and_unregistered_notice(self):
        from workflow import set_routing_mode
        set_routing_mode(self.root,False,'test')  # explicit audiences apply outside manual routing
        self.register(audience=['b'])
        self.assertEqual('EVIDENCE_VERIFIED',cc.project_for_endpoint(self.root,'b')['status'])
        self.assertEqual('UNREGISTERED',cc.project_for_endpoint(self.root,'c')['status'])

    def test_registry_corruption_fails_visibly_without_destroying_brief(self):
        self.register(); cc.registry_path(self.root).write_text('{')
        self.assertEqual('REGISTRY_UNAVAILABLE',cc.project_for_endpoint(self.root,'a')['status'])

    def test_registration_revisions_preserve_history_and_owner(self):
        self.register()
        with self.assertRaisesRegex(cc.ContextError,'revision conflict'): self.register()
        self.register(expected_revision=1)
        h = cc.registry_path(self.root).parent/'history'
        self.assertTrue((h/'lnd.1.json').exists()); self.assertTrue((h/'lnd.2.json').exists())
        self.data['owner'] = 'codex:b'; self.pin = self.write_manifest()
        with self.assertRaisesRegex(cc.ContextError,'owner mismatch'):
            cc.register(self.root,str(self.manifest),self.pin,'codex:b','b',expected_revision=2)

    def test_historical_task_next_action_is_display_only(self):
        self.register(); p = cc.project_for_endpoint(self.root,'a')
        original = {'task_id':'old-stage','status':'VERIFIED_COMPLETE','next_action':'Director review pending'}
        result = cc.annotate_tasks([original],p)[0]
        self.assertEqual('Director review pending',original['next_action'])
        self.assertEqual(original['next_action'],result['historical_next_action'])
        self.assertIn('qualified historical inputs',result['next_action'])
        self.assertEqual(original['status'],result['status'])
        self.assertIn('Historical stage',result['recordMeaning'])

    def test_unavailable_manifest_cannot_leave_historical_action_unqualified(self):
        self.register(); self.manifest.unlink(); p = cc.project_for_endpoint(self.root,'a')
        row = cc.annotate_tasks([{'task_id':'old-stage','next_action':'Director review pending'}],p)[0]
        self.assertIn('Restore exact',row['next_action'])
        self.assertEqual('CONTEXT_UNAVAILABLE',row['currentContext'][0]['status'])

    def test_too_many_contexts_report_exact_omitted_ids(self):
        for n in range(10):
            self.data['contextId'] = 'context-'+str(n)
            # Separate retained manifest paths avoid intentional drift between registrations.
            self.manifest = self.root/('manifest-'+str(n)+'.json'); self.pin = self.write_manifest()
            self.register()
        p = cc.project_for_endpoint(self.root,'a')
        self.assertEqual(10,p['coverage']['registered']); self.assertEqual(8,p['coverage']['displayed'])
        self.assertEqual(['context-8','context-9'],p['coverage']['omittedContextIds'])
        self.assertTrue(all(c['status'] == 'EVIDENCE_VERIFIED' for c in p['contexts']))
        self.assertEqual('PARTIAL',p['status'])


if __name__ == '__main__':
    unittest.main()
