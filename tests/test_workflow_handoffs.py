import contextlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from test_workspace import fixture
from workflow import Workflow
from workflow_handoffs import Handoffs
from workflow_handoff_transport import DesktopTransport

class Transport:
    def __init__(self):self.starts=[];self.observations={};self.failure=None;self.before=None
    @contextlib.contextmanager
    def prepare(self,t):
        if self.before:self.before()
        yield t
    def start(self,t,body,rid):
        self.starts.append((t,body,rid))
        if self.failure:raise self.failure
        return {'turnId':'11111111-1111-1111-1111-111111111111','clientUserMessageId':rid}
    def observe(self,t,rid,receipt):return self.observations.get(rid,{})

class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'board';self.w,_=fixture(self.root)
        self.f=Workflow(self.root,self.w,Path(self.tmp.name)/'no-provider-store')
        state={k:v for k,v in self.f.read().items() if k!='revision'}
        state.update(enabled=True,placements=[{'agentId':'codex:a','laneId':'alpha','role':'writer'}, {'agentId':'codex:review','laneId':'alpha','role':'auditor'},{'agentId':'codex:b','laneId':'beta','role':'worker'}])
        self.f.save(state,0,'fixture')
        self.activities={'codex:review':{'turnStatus':'finished'},'codex:b':{'turnStatus':'finished'}}
        self.f.activity_snapshot=lambda ids:{'activities':self.activities}
        self.t=Transport();self.clock=[1000.0];self.h=Handoffs(self.f,self.t,lambda:self.clock[0])
        self.h.enable('Operator requested exact-recipient idle wakeup')
    def tearDown(self):self.tmp.cleanup()
    def send(self,key='one',recipient='codex:review',intent='handoff'):
        return self.f.send('codex:a',recipient,'Review the exact candidate '+key+'.',key,intent=intent)['id']
    def tick(self):self.clock[0]+=31;self.h.tick()
    def test_idle_exact_address_and_single_turn_duplicate_send(self):
        mid=self.send();self.tick();self.assertEqual('ACCEPTED',self.h.get(mid)['status'])
        self.assertEqual(mid,self.send());self.tick();self.assertEqual(1,len(self.t.starts))
        body=self.t.starts[0][1];self.assertIn('Exact recipient: codex:review',body);self.assertIn('Exact sender: codex:a',body)
        self.assertEqual('codex:review',self.t.starts[0][0]['agent_id'])
    def test_busy_waits_then_starts(self):
        self.activities['codex:review']={'turnStatus':'open','active':True}
        mid=self.send();self.tick();self.assertEqual('BUSY',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
        self.activities['codex:review']={'turnStatus':'finished'};self.tick();self.assertEqual(1,len(self.t.starts))
    def test_different_idle_recipient_not_blocked_by_busy_agent(self):
        self.f.mutate({'operation':'connection','revision':1,'item':{'from':'codex:a','to':'codex:b','allow':True}})
        self.activities['codex:review']={'turnStatus':'open'};self.send();mid=self.send('other','codex:b');self.tick()
        self.assertEqual('ACCEPTED',self.h.get(mid)['status']);self.assertEqual(1,len(self.t.starts))
    def test_denied_route_stays_held(self):
        mid=self.send(recipient='codex:b');self.tick();self.assertEqual('HELD',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
    def test_revocation_at_send_boundary(self):
        self.t.before=lambda:self.f.mutate({'operation':'connection','revision':1,'item':{'from':'codex:a','to':'codex:review','allow':False}})
        mid=self.send();self.tick();self.assertEqual('HELD',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
    def test_becomes_busy_during_preflight(self):
        self.t.before=lambda:self.activities.update({'codex:review':{'turnStatus':'open'}})
        mid=self.send();self.tick();self.assertEqual('BUSY',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
    def test_unknown_activity_visible_not_inferred_idle(self):
        self.activities['codex:review']={'turnStatus':'unknown'};mid=self.send();self.tick()
        self.assertEqual('UNAVAILABLE',self.h.get(mid)['status']);self.assertEqual(1,self.h.snapshot()['attention']);self.assertFalse(self.t.starts)
    def test_uncertain_send_not_retried_after_restart(self):
        self.t.failure=TimeoutError('lost acknowledgement');mid=self.send();self.tick();row=self.h.get(mid)
        self.assertEqual('UNCERTAIN',row['status']);self.h=Handoffs(self.f,self.t,lambda:self.clock[0]);self.tick();self.assertEqual(1,len(self.t.starts))
        self.t.observations[row['request_id']]={'status':'RUNNING','receipt':{'turnId':'reconciled'},'detail':'Exact request observed'}
        self.tick();self.assertEqual('RUNNING',self.h.get(mid)['status']);self.assertEqual(1,len(self.t.starts))
    def test_crash_after_reservation_never_resends(self):
        mid=self.send();self.h.enroll(mid)
        with self.h.connect() as db:db.execute("UPDATE handoffs SET status='SENDING',attempts=1 WHERE id=?",(mid,))
        self.clock[0]+=100;self.tick();self.assertEqual('UNCERTAIN',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
    def test_original_read_ack_prevents_extra_wake(self):
        mid=self.send();self.h.enroll(mid)
        with self.f._connect() as db:db.execute("UPDATE workflow_messages SET read_at='read' WHERE id=?",(mid,))
        self.tick();self.assertEqual('ACKNOWLEDGED',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
    def test_notification_never_starts_and_intent_cannot_change(self):
        mid=self.send(intent='notification');self.tick();self.assertFalse(self.h.snapshot()['handoffs']);self.assertFalse(self.t.starts)
        with self.assertRaisesRegex(ValueError,'different intent'):self.send()
    def test_history_excluded_unless_explicitly_selected(self):
        mid=self.send()
        with self.f._connect() as db:db.execute("UPDATE workflow_messages SET created_at='2000-01-01' WHERE id=?",(mid,))
        self.tick();self.assertFalse(self.t.starts);self.h.enroll(mid);self.tick();self.assertEqual(1,len(self.t.starts))
    def test_returned_turn_unblocks_next_handoff_not_task_completion(self):
        first=self.send();second=self.send('two');self.tick();self.assertEqual(1,len(self.t.starts))
        rid=self.h.get(first)['request_id'];self.t.observations[rid]={'status':'RETURNED','detail':'Turn ended'}
        self.tick();self.assertEqual('RETURNED',self.h.get(first)['status']);self.assertEqual(2,len(self.t.starts))
    def test_concurrent_daemons_only_one_provider_write(self):
        mid=self.send();self.clock[0]+=31
        with patch.object(self.h,'scan',side_effect=self.h.scan):
            threads=[threading.Thread(target=self.h.tick) for _ in range(6)]
            for t in threads:t.start()
            for t in threads:t.join()
        self.assertEqual(1,len(self.t.starts));self.assertEqual(1,self.h.get(mid)['attempts'])
    def test_managed_workers_require_original_runtime(self):
        original=self.f.identity_catalog
        def identity():
            data=original()
            for s in data['sessions']:
                if s['agent_id']=='codex:review':s['managed']=True
            return data
        self.f.identity_catalog=identity;mid=self.send();self.tick();self.assertEqual('MANAGED_POLICY',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
    def test_recipient_provider_rebind_rejected(self):
        mid=self.send();self.h.enroll(mid);original=self.f.identity_catalog
        def identity():
            data=original()
            for s in data['sessions']:
                if s['agent_id']=='codex:review':s['endpoint']='changed'
            return data
        self.f.identity_catalog=identity;self.tick();self.assertEqual('UNAVAILABLE',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
    def test_identical_body_with_new_key_does_not_wake_again(self):
        first=self.f.send('codex:a','codex:review','Exact same action','first')['id']
        second=self.f.send('codex:a','codex:review','Exact same action','second')['id']
        self.tick();self.assertEqual('DUPLICATE',self.h.get(second)['status']);self.assertEqual(1,len(self.t.starts))
    def test_pause_during_preflight_prevents_write(self):
        def pause():
            p=self.h.policy();p['enabled']=False;self.h.policy_path.write_text(json.dumps(p))
        self.t.before=pause;mid=self.send();self.tick()
        self.assertEqual('HELD',self.h.get(mid)['status']);self.assertFalse(self.t.starts)
    def test_unavailable_is_not_reported_busy_or_delivery_healthy(self):
        self.activities['codex:review']={'turnStatus':'unknown'};self.send();self.tick()
        snap=self.h.snapshot();self.assertEqual(1,snap['unavailableRecipients']);self.assertEqual(0,snap['busyRecipients'])
        self.assertFalse(snap['deliveryHealthy']);self.assertTrue(snap['daemonHealthy'])
    def test_pause_preserves_queue(self):
        mid=self.send();self.h.enroll(mid);p=self.h.policy();p['enabled']=False;self.h.policy_path.write_text(json.dumps(p));self.tick();self.assertFalse(self.t.starts)

class AdapterTests(unittest.TestCase):
    def test_start_only_and_inherited_settings(self):
        owner='11111111-1111-1111-1111-111111111111';turn='22222222-2222-2222-2222-222222222222';calls=[]
        class Client:
            def request(self,method,params,**kw):
                calls.append((method,params,kw));return {'resultType':'success','handledByClientId':owner,'result':{'turn':{'id':turn}}}
            def close(self):pass
        class Flow:codex_home=Path('/tmp')
        transport=DesktopTransport(Flow(),lambda p:Client())
        with transport.prepare({'provider':'codex','endpoint':turn,'cwd':'/tmp'}) as prepared:
            receipt=transport.start(prepared,'message',owner)
        self.assertEqual(['thread-owner-discovery','thread-follower-start-turn'],[c[0] for c in calls])
        self.assertEqual(2,calls[1][2]['version']);self.assertTrue(calls[1][1]['turnStart']['context']['inheritThreadSettings'])
        self.assertNotIn('model',calls[1][1]['turnStart']['request']);self.assertNotIn('effort',calls[1][1]['turnStart']['request']);self.assertEqual(turn,receipt['turnId'])
    def test_unavailable_owner_has_no_fallback(self):
        class Client:
            def request(self,*a,**k):return {'resultType':'error'}
            def close(self):pass
        class Flow:codex_home=Path('/tmp')
        with self.assertRaisesRegex(ValueError,'no available desktop owner'):
            with DesktopTransport(Flow(),lambda p:Client()).prepare({'provider':'codex','endpoint':'11111111-1111-1111-1111-111111111111','cwd':'/tmp'}):pass
class ReconciliationTests(unittest.TestCase):
    def observe(self,event_type,role='user'):
        import sqlite3
        from activity import ActivityReader
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);log=home/'rollout.jsonl';tid='22222222-2222-2222-2222-222222222222'
            events=[{'type':'event_msg','timestamp':'2026-09-14T10:00:00Z','payload':{'type':'task_started','turn_id':tid}},
              {'type':'response_item','timestamp':'2026-09-14T10:00:01Z','payload':{'type':'message','role':role,'content':[{'type':'input_text','text':'HANDOFF exact-request\nTo: Agent [codex:exact]'}]}},
              {'type':'event_msg','timestamp':'2026-09-14T10:00:02Z','payload':{'type':event_type,'turn_id':tid}},
              {'type':'event_msg','timestamp':'2026-09-14T11:00:00Z','payload':{'type':'task_started','turn_id':'later-unrelated-turn'}}]
            log.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            with sqlite3.connect(home/'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads(id,rollout_path)');db.execute('INSERT INTO threads VALUES(?,?)',('exact',str(log)))
            class Flow:pass
            f=Flow();f.codex_home=home;f.activity_reader=ActivityReader()
            return DesktopTransport(f).observe({'provider':'codex','endpoint':'exact'},'exact-request',{})
    def test_lost_ack_reconciles_original_completed_turn_after_another_turn(self):
        self.assertEqual('RETURNED',self.observe('task_complete')['status'])
    def test_lost_ack_reconciles_original_failed_turn_after_another_turn(self):
        self.assertEqual('FAILED',self.observe('task_failed')['status'])
    def test_assistant_quote_is_not_delivery_evidence(self):
        self.assertEqual({},self.observe('task_complete','assistant'))

if __name__=='__main__':unittest.main()
