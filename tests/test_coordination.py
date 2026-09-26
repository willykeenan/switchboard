import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import coordination as v2


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'board';self.root.mkdir()
        os.environ['SWITCHBOARD_ROOT']=str(self.root);os.environ['SWITCHBOARD_DISABLE_ROOM']='1'
        os.environ.pop('SWITCHBOARD_ROOMS_ROOT',None);os.environ.pop('SWITCHBOARD_ALLOW_LAUNCH',None)
        sys.modules.pop('board_core',None);self.b=importlib.import_module('board_core');self.b.init_db()
        # Legacy automatic assignment is opt-in; these tests exercise it.
        from workflow import set_routing_mode
        set_routing_mode(self.root,False,'test')
        self.agent('codex:a','a');self.agent('codex:b','b')
        self.room=self.root/'rooms'/'global';self.room.mkdir(parents=True)
        self.log=self.room/'messages.jsonl';self.log.write_text('')

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self,id,endpoint,status='ACTIVE'):
        return self.b.register_agent(agent_id=id,team='GENERAL',provider='codex',endpoint=endpoint,
                                     display_name=id,capabilities=['general'],writable_scopes=[],status=status,wake_mode='room')

    def task(self,id='task-a',owner='codex:a',scope=None,deps=None):
        artifact=self.root/(id+'.json');artifact.write_text('{"checks":{"ok":true}}')
        return v2.task_register({'task_id':id,'owner':owner,'objective':'Deliver exact tested change',
             'scope':scope or [str(self.root/id)],'dependencies':deps or [],'next_action':'run verification',
             'criteria':[{'id':'pass','kind':'json','description':'functional checks pass','path':str(artifact),'pointer':'/checks/ok','equals':True}]})

    def incident(self,team='GENERAL'):
        return self.b.create_incident(team=team,title='Failure',details='Needs repair',safe_action='Inspect locally',required_capability='specialist')

    def messages(self,n=12):
        self.log.write_text(''.join(json.dumps({'seq':i,'id':str(i),'author':'claude','recipients':['a'] if i%2 else ['b'],
                                  'kind':'question','body':'message '+str(i),'createdAt':self.b.utc_now()})+'\n' for i in range(1,n+1)))

    def test_task_contract_immutable_and_version_conflict(self):
        self.task()
        v2.task_update('task-a','codex:a',1,{'status':'WORKING'})
        for owner,version in [('codex:b',2),('codex:a',1)]:
            with self.assertRaises(ValueError):v2.task_update('task-a',owner,version,{'status':'WORKING'})
        with self.assertRaises(ValueError):v2.task_update('task-a','codex:a',2,{'objective':'different'})

    def test_scope_parent_overlap_and_stale_owner_never_transfer(self):
        self.task(scope=[str(self.root/'shared')])
        with self.b.connect() as c:c.execute("UPDATE agents SET last_seen_at='2000-01-01T00:00:00Z' WHERE agent_id='codex:a'")
        with self.assertRaises(ValueError):self.task('b','codex:b',[str(self.root/'shared/child')])
        self.assertEqual('codex:a',v2.task_get('task-a')['owner'])
        self.assertEqual('UNKNOWN',v2.presence(self.b.get_agent('codex:a'))['status'])

    def test_dependencies_block_work_until_verified(self):
        self.task();self.task('dependent','codex:b',deps=['task-a'])
        with self.assertRaises(ValueError):v2.task_update('dependent','codex:b',1,{'status':'WORKING'})
        v2.task_update('task-a','codex:a',1,{'status':'REPORTED_DONE'});v2.verify_task('task-a','codex:a',2)
        self.assertEqual('WORKING',v2.task_update('dependent','codex:b',1,{'status':'WORKING'})['status'])

    def test_reported_completion_needs_actual_evidence(self):
        self.task();v2.task_update('task-a','codex:a',1,{'status':'REPORTED_DONE'})
        artifact=self.root/'task-a.json';artifact.write_text('{"checks":{"ok":false}}')
        with self.assertRaises(ValueError):v2.verify_task('task-a','codex:a',2)
        self.assertEqual('REPORTED_DONE',v2.task_get('task-a')['status'])
        artifact.write_text('{"checks":{"ok":true}}');self.assertEqual('VERIFIED_COMPLETE',v2.verify_task('task-a','codex:a',2)['status'])

    def test_concurrent_updates_only_one_commits(self):
        self.task()
        def update(_):
            try:v2.task_update('task-a','codex:a',1,{'status':'WORKING'});return True
            except ValueError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(update,range(6)))
        self.assertEqual(1,sum(results))

    def test_scoped_inbox_cursor_no_unrelated_inventory(self):
        self.messages();brief=v2.brief('a',after=0,limit=3)
        self.assertEqual([1,3,5],[m['seq'] for m in brief['messages']]);self.assertTrue(brief['messages_more'])
        self.assertEqual(['codex:a'],[a['agent_id'] for a in brief['agents']])
        with self.assertRaises(ValueError):v2.cursor_ack('a',12)
        v2.cursor_ack('a',5);self.assertEqual([7,9,11],[m['seq'] for m in v2.brief('a')['messages']])
        self.assertLess(len(json.dumps(brief)),5000)

    def test_partial_room_append_retries_without_cursor_loss(self):
        self.messages(2)
        with self.log.open('a') as f:f.write('{"seq":3')
        self.assertEqual(2,v2.sync_room())
        with self.log.open('a') as f:f.write(',"id":"3","author":"c","recipients":["a"],"kind":"result","body":"ok","createdAt":"now"}\n')
        self.assertEqual(1,v2.sync_room());self.assertEqual(0,v2.sync_room())
        self.log.write_text('')
        with self.assertRaises(ValueError):v2.sync_room()

    def test_brief_serves_explicitly_stale_index_on_sync_failure(self):
        self.messages();v2.sync_room();self.log.write_text('')
        result=v2.brief('a',after=0)
        self.assertEqual('STALE_INDEX',result['room_freshness'])
        self.assertIn('shrank',result['sync_error']);self.assertGreater(len(result['messages']),0)

    def test_concurrent_wakes_launch_once_and_preserve_exact_owner(self):
        i=self.b.create_incident(team='GENERAL',title='Concurrent',details='Test',safe_action='Inspect')
        with self.b.connect() as c:c.execute("UPDATE agents SET status='IDLE',wake_mode='resume'")
        with mock.patch.dict(os.environ,{'SWITCHBOARD_ALLOW_LAUNCH':'1'}),mock.patch.object(self.b,'_wake_codex',return_value={'status':'WAKE_QUEUED','pid':42}) as wake:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                rows=list(pool.map(lambda _:self.b.wake_incident(i['incident_id']),range(6)))
            self.assertEqual(1,wake.call_count)
            self.assertEqual(1,len({r['assigned_agent_id'] for r in rows}))

    def test_all_legacy_room_callers_have_transactional_mirrors(self):
        i=self.b.create_incident(team='GENERAL',title='Routing',details='Test',safe_action='Inspect')
        i=self.b.wake_incident(i['incident_id'])
        self.b.acknowledge(i['incident_id'],i['assigned_agent_id'])
        self.b.resolve(i['incident_id'],actor='test',resolution='documentary closure')
        with self.b.connect() as c:
            kinds={r[0] for r in c.execute('SELECT event_type FROM room_outbox')}
        self.assertEqual({'INCIDENT_OPENED','INCIDENT_ASSIGNED','WAKE_ATTEMPT_FINISHED','INCIDENT_ACKNOWLEDGED','INCIDENT_RESOLVED'},kinds)

    def test_daily_current_state_backup_restores_pending_delivery(self):
        self.task();self.incident();v2.tick()
        import maintenance;maintenance.board=self.b
        with self.b.connect() as c:path=c.execute("SELECT value FROM coordination_meta WHERE key='current_backup_path'").fetchone()[0]
        result=maintenance.verify_current(path)
        self.assertTrue(result['ok']);self.assertEqual(1,result['tables']['room_outbox'])

    def test_unrouted_events_do_not_grow_when_state_unchanged(self):
        incident=self.incident('NO_OWNER')
        for _ in range(30):self.b.assign_incident(incident['incident_id'])
        with self.b.connect() as c:n=c.execute("SELECT count(*) FROM events WHERE event_type='INCIDENT_UNROUTED'").fetchone()[0]
        self.assertEqual(1,n)

    def alias(self):
        return self.b.register_agent(agent_id='claude:research-room',team='RESEARCH',provider='room',endpoint='claude',display_name='Room alias',capabilities=['all'],writable_scopes=[],status='IDLE',priority=0,wake_mode='room')

    def test_room_alias_cannot_receive_assignment_or_own_task(self):
        self.alias()
        i=self.b.create_incident(team='RESEARCH',title='Failure',details='Unassigned',safe_action='Inspect')
        with mock.patch.object(self.b,'_wake_codex') as wake:
            result=self.b.wake_incident(i['incident_id'])
        self.assertIsNone(result['assigned_agent_id']);wake.assert_not_called()
        with self.assertRaises(ValueError):self.task('alias-task','claude:research-room')
        with self.assertRaises(ValueError):self.b.acknowledge(i['incident_id'],'claude:research-room')

    def test_recorded_exact_owner_routes_across_team_without_fallback(self):
        self.alias();self.agent('codex:aabb0000-aaaa-4aaa-8aaa-aaaaaaaaaaaa','aabb0000-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
        i=self.b.create_incident(team='RESEARCH',title='Sample unassigned work',details='Invented sample remains pending',safe_action='Existing owneraabb0000 only. Preserve failed probe identities; no signals.')
        self.assertEqual('codex:aabb0000-aaaa-4aaa-8aaa-aaaaaaaaaaaa',self.b.assign_incident(i['incident_id'])['assigned_agent_id'])
        missing=self.b.create_incident(team='GENERAL',title='Missing owner',details='No fallback',safe_action='Inspect',requested_owner='codex:missing')
        self.assertIsNone(self.b.assign_incident(missing['incident_id'])['assigned_agent_id'])
        with self.assertRaises(ValueError):self.b.acknowledge(missing['incident_id'],'codex:a')

    def test_ambiguous_owner_prefix_fails_closed(self):
        for suffix in ['1111-1111-1111-111111111111','2222-2222-2222-222222222222']:
            self.agent('codex:aabb0000-'+suffix,'aabb0000-'+suffix)
        i=self.b.create_incident(team='GENERAL',title='Ambiguous',details='No fallback',safe_action='Existing owneraabb0000 only.')
        self.assertIsNone(self.b.assign_incident(i['incident_id'])['assigned_agent_id'])

    def test_explicit_owner_in_details_without_only_is_respected(self):
        owner='codex:aabb0000-aaaa-4aaa-8aaa-aaaaaaaaaaaa';self.agent(owner,owner[6:])
        i=self.b.create_incident(team='RESEARCH',title='Existing work',details='Exact owner aabb0000-aaaa-4aaa-8aaa-aaaaaaaaaaaa. Work remains pending.',safe_action='Review its existing receipt')
        self.assertEqual(owner,self.b.assign_incident(i['incident_id'])['assigned_agent_id'])

    def legacy_alias_handoff(self):
        self.alias();owner='codex:aabb0000-aaaa-4aaa-8aaa-aaaaaaaaaaaa';self.agent(owner,owner[6:])
        i=self.b.create_incident(team='RESEARCH',title='Sample work remains pending',details='Invented sample processes remain; pending signals are not terminal',safe_action='Existing owneraabb0000 only. Preserve failed probe identities and states; no reboot or signals.')
        with self.b.connect() as c:
            c.execute("UPDATE incidents SET assigned_agent_id='claude:research-room',status='ASSIGNED',wake_status='ROOM_PING_ONLY',wake_attempts=1 WHERE incident_id=?",(i['incident_id'],))
            c.execute("INSERT INTO handoffs VALUES(?,?,'ESCALATED',1,0,NULL,'missing acknowledgment',?)",(i['incident_id'],'claude:research-room',self.b.utc_now()))
        evidence=self.root/'owner-evidence.txt';evidence.write_text('Existing exact owner confirmed in retained room record')
        import hashlib
        args=(i['incident_id'],'claude:research-room',owner,'Recorded room alias misroute',str(evidence),hashlib.sha256(evidence.read_bytes()).hexdigest(),'test')
        return i,args

    def test_frozen_alias_misroute_reconciles_without_second_launch(self):
        i,args=self.legacy_alias_handoff()
        with mock.patch.object(self.b,'_wake_codex') as wake:
            r=v2.reconcile_placeholder(*args);self.b.wake_incident(i['incident_id'])
        wake.assert_not_called();self.assertEqual(1,r['wake_attempts']);self.assertEqual('ASSIGNED',r['status'])
        self.assertEqual(args[2],r['assigned_agent_id'])
        self.b.acknowledge(i['incident_id'],args[2],note='Retained unresolved; own six UE processes')
        with self.b.connect() as c:
            h=c.execute('SELECT * FROM handoffs WHERE incident_id=?',(i['incident_id'],)).fetchone()
            events=c.execute("SELECT count(*) FROM events WHERE event_type='INCIDENT_OWNER_RECONCILED'").fetchone()[0]
        self.assertEqual('ACKNOWLEDGED',h['stage']);self.assertEqual(args[2],h['owner']);self.assertEqual(1,events)
        with self.assertRaises(ValueError):v2.reconcile_placeholder(*args)

    def test_uncertain_launch_cannot_be_reconciled_as_alias_ping(self):
        i,args=self.legacy_alias_handoff()
        with self.b.connect() as c:c.execute('UPDATE handoffs SET pid=42 WHERE incident_id=?',(i['incident_id'],))
        with self.assertRaises(ValueError):v2.reconcile_placeholder(*args)
        self.assertEqual('claude:research-room',self.b.get_incident(i['incident_id'])['assigned_agent_id'])

    def test_acknowledge_cli_alias_updates_the_same_incident(self):
        i=self.b.create_incident(team='GENERAL',title='CLI alias',details='Exact owner',safe_action='Inspect',requested_owner='codex:a')
        self.b.assign_incident(i['incident_id'])
        cli=Path(__file__).resolve().parents[1]/'boardctl.py'
        result=subprocess.run([sys.executable,str(cli),'acknowledge','--incident-id',i['incident_id'],'--agent-id','codex:a','--note','Unresolved; acknowledgment only'],capture_output=True,text=True,timeout=5)
        self.assertEqual(0,result.returncode,result.stderr)
        actual=json.loads(result.stdout);self.assertEqual('ACKNOWLEDGED',actual['status']);self.assertIsNone(actual['resolved_at'])

    def test_consolidation_preserves_all_rows_distinct_failures_and_owners(self):
        source='monitor:guard:/exact/state.json'
        def make(fp,details='same failure'):
            return self.b.create_incident(team='GENERAL',title='Guard blocked',details=details,safe_action='Inspect only',source=source,fingerprint=fp)
        rows=[make('one'),make('two'),make('three','different failure'),make('owned')]
        self.b.acknowledge(rows[3]['incident_id'],'codex:a')
        plan=v2.consolidate_incidents(source);self.assertEqual(1,plan['linked'])
        with self.b.connect() as c:before=[dict(r) for r in c.execute('SELECT * FROM incidents ORDER BY incident_id')]
        v2.consolidate_incidents(source,True)
        with self.b.connect() as c:after=[dict(r) for r in c.execute('SELECT * FROM incidents ORDER BY incident_id')]
        self.assertEqual(before,after);self.assertEqual(0,v2.consolidate_incidents(source,True)['linked'])
        child=plan['links'][0]['incident_id']
        self.assertIsNotNone(self.b.get_incident(child)['linked_to'])
        self.assertEqual(3,len(v2.dashboard()['incidents']));self.assertEqual(4,v2.dashboard()['history']['total_reports'])
        with self.assertRaises(ValueError):self.b.acknowledge(child,'codex:a')
        self.assertIsNone(self.b.assign_incident(child)['assigned_agent_id'])

    def test_monitor_timestamp_hash_changes_do_not_create_alerts(self):
        p=self.root/'monitor.json'
        self.b.register_monitor(monitor_id='guard',team='GENERAL',source_path=str(p),title='Guard',safe_action='Inspect',severity='warning',required_capability='general',failure_statuses=['FAILED'])
        for n in range(5):
            p.write_text(json.dumps({'status':'FAILED','detail':'same condition','record_hash':str(n)}));self.b.scan_monitors()
        with self.b.connect() as c:
            self.assertEqual(1,c.execute('SELECT count(*) FROM incidents').fetchone()[0])
            obs=c.execute('SELECT * FROM monitor_observations').fetchone();self.assertEqual('4',obs['record_hash']);self.assertEqual(5,obs['observations'])
            self.assertEqual(1,c.execute("SELECT count(*) FROM events WHERE event_type='MONITOR_SCANNED'").fetchone()[0])
        p.write_text(json.dumps({'status':'FAILED','detail':'new condition','record_hash':'6'}));self.b.scan_monitors()
        with self.b.connect() as c:self.assertEqual(2,c.execute('SELECT count(*) FROM incidents').fetchone()[0])

    def test_resolved_monitor_failure_reopens_if_source_still_fails(self):
        p=self.root/'monitor.json';p.write_text('{"status":"FAILED","detail":"still failed"}')
        self.b.register_monitor(monitor_id='guard',team='GENERAL',source_path=str(p),title='Guard',safe_action='Inspect',severity='warning',required_capability='general',failure_statuses=['FAILED'])
        i=self.b.scan_monitors()[0]['incident_id'];self.b.resolve(i,'test','Reported resolved')
        j=self.b.scan_monitors()[0]['incident_id'];self.assertNotEqual(i,j)
        self.assertEqual('OPEN',self.b.get_incident(j)['status'])

    def test_monitor_reuses_linked_legacy_canonical_condition(self):
        p=self.root/'monitor.json';p.write_text('{"status":"FAILED","detail":"still failed","record_hash":"new"}')
        source='monitor:guard:'+str(p)
        ids=[self.b.create_incident(team='GENERAL',title='Guard: FAILED',details='still failed',safe_action='Inspect',source=source,fingerprint='old-'+str(n))['incident_id'] for n in range(3)]
        plan=v2.consolidate_incidents(source,True)
        self.b.register_monitor(monitor_id='guard',team='GENERAL',source_path=str(p),title='Guard',safe_action='Inspect',severity='warning',required_capability='general',failure_statuses=['FAILED'])
        actual=self.b.scan_monitors()[0]['incident_id']
        self.assertEqual(plan['links'][0]['canonical_id'],actual)
        with self.b.connect() as c:self.assertEqual(3,c.execute('SELECT count(*) FROM incidents').fetchone()[0])

    def test_event_and_outbox_commit_atomically(self):
        with self.assertRaises(RuntimeError):
            with self.b.connect() as c:
                i=self.incident()
                self.b.event(c,'INCIDENT_ASSIGNED',actor='test',incident_id=i['incident_id'])
                raise RuntimeError('crash before transaction commit')
        with self.b.connect() as c:
            self.assertEqual(0,c.execute("SELECT count(*) FROM events WHERE event_type='INCIDENT_ASSIGNED'").fetchone()[0])
            self.assertEqual(0,c.execute("SELECT count(*) FROM room_outbox WHERE body LIKE '%INCIDENT_ASSIGNED%'").fetchone()[0])

    def install_test_room(self):
        source=Path(__file__).resolve().parents[1]/'room_writer.py';target=self.room/'room_writer_copy.py';target.write_bytes(source.read_bytes());self.b.ROOM_WRITER=target
        os.environ['SWITCHBOARD_DISABLE_ROOM']='0'
        return target

    def test_room_idempotency_and_replay_after_lost_receipt(self):
        room=self.install_test_room();i=self.incident()
        real_run=subprocess.run;calls=[]
        def lose_receipt(*args,**kw):
            result=real_run(*args,**kw);calls.append(result)
            raise TimeoutError('receipt lost after append')
        with mock.patch('subprocess.run',side_effect=lose_receipt):v2.flush_outbox(1)
        with self.b.connect() as c:c.execute('UPDATE room_outbox SET next_at=0')
        v2.flush_outbox()
        records=[json.loads(r) for r in self.log.read_text().splitlines()]
        self.assertEqual(1,len(records));self.assertIn('idempotencyKey',records[0])
        legacy=subprocess.run([sys.executable,str(room),'read','--after','0'],capture_output=True,text=True,check=True)
        self.assertTrue(legacy.stdout.startswith('#1 '))

    def test_outbox_retries_bounded_and_escalates(self):
        self.install_test_room();self.incident()
        with mock.patch('subprocess.run',side_effect=TimeoutError('offline')):
            for _ in range(5):
                with self.b.connect() as c:c.execute('UPDATE room_outbox SET next_at=0')
                v2.flush_outbox(1)
        with self.b.connect() as c:row=c.execute('SELECT status,attempts FROM room_outbox').fetchone()
        self.assertEqual(('ESCALATED',5),tuple(row))

    def test_missing_ack_escalates_without_second_model_launch(self):
        i=self.b.create_incident(team='GENERAL',title='Incident',details='Details',safe_action='Inspect')
        with mock.patch.object(self.b,'_wake_codex') as wake:
            routed=self.b.wake_incident(i['incident_id'])
            with self.b.connect() as c:c.execute('UPDATE handoffs SET due_at=0')
            self.assertEqual('ESCALATED',v2.handoff_tick()[0]['stage'])
            self.b.wake_incident(i['incident_id']);wake.assert_not_called()
        self.b.acknowledge(i['incident_id'],routed['assigned_agent_id'])
        with self.b.connect() as c:self.assertEqual('ACKNOWLEDGED',c.execute('SELECT stage FROM handoffs').fetchone()[0])

    def test_crash_after_claim_before_launch_is_visible(self):
        i=self.incident()
        with self.b.connect() as c:
            c.execute("INSERT INTO handoffs(incident_id,owner,stage,due_at,detail,updated_at) VALUES(?,?,'QUEUED',0,'uncertain',?)",(i['incident_id'],'codex:a',self.b.utc_now()))
        self.assertEqual('ESCALATED',v2.handoff_tick()[0]['stage'])

    def test_backup_restore_isolated_and_version_preserved(self):
        self.task();import maintenance
        maintenance.board=self.b
        receipt=maintenance.backup(self.root/'backups')
        self.assertTrue(maintenance.verify_backup(receipt['path'])['ok'])
        self.assertEqual('task-a',v2.task_get('task-a')['task_id'])

    def test_health_reflects_service_failure_and_overview_preserves_history(self):
        outside=self.root.parent/'CURRENT.md';outside.write_text('someone else\n')
        self.assertFalse(v2.health()['ok']);v2.tick();self.assertTrue(v2.health()['ok'])
        # The overview lives in the data directory; a neighbouring file is never replaced.
        self.assertEqual('someone else\n',outside.read_text());self.assertTrue((self.root/'CURRENT.md').exists())
        self.assertEqual({'board','CURRENT.md'},{p.name for p in self.root.parent.iterdir()})

    def test_claude_startup_hook_same_session_no_duplicate_owner(self):
        import session_hook;session_hook.board=self.b
        first=session_hook.run({'session_id':'claude-test','cwd':self.tmp.name})
        second=session_hook.run({'session_id':'claude-test','cwd':self.tmp.name})
        self.assertEqual('SessionStart',first['hookSpecificOutput']['hookEventName'])
        self.assertIn('claude-test',second['hookSpecificOutput']['additionalContext'])
        with self.b.connect() as c:self.assertEqual(1,c.execute("SELECT count(*) FROM agents WHERE endpoint='claude-test'").fetchone()[0])


if __name__=='__main__':unittest.main()
