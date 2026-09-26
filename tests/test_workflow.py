import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_workspace import fixture
from workflow import Workflow, allowed, manual_routing
from workspace import Conflict

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'board';self.w,self.old=fixture(self.root)
        self.flow=Workflow(self.root,self.w,Path(self.tmp.name)/'no-provider-store')
        self.state={k:v for k,v in self.flow.read().items() if k!='revision'}
        self.state.update(enabled=True,placements=[{'agentId':'codex:a','laneId':'alpha','role':'writer'},{'agentId':'codex:b','laneId':'beta','role':'worker'},{'agentId':'codex:review','laneId':'alpha','role':'auditor'}])
        self.flow.save(self.state,0,'fixture')
    def tearDown(self):self.tmp.cleanup()
    def test_directional_rules_and_explicit_denial(self):
        self.assertTrue(allowed(self.flow.read(),'codex:a','codex:review'))
        self.assertFalse(allowed(self.flow.read(),'codex:a','codex:b'))
        self.flow.mutate({'operation':'connection','revision':1,'item':{'from':'codex:a','to':'codex:b','allow':True}})
        self.assertTrue(allowed(self.flow.read(),'codex:a','codex:b'));self.assertFalse(allowed(self.flow.read(),'codex:b','codex:a'))
        self.flow.mutate({'operation':'connection','revision':2,'item':{'from':'codex:a','to':'codex:review','allow':False}})
        self.assertFalse(allowed(self.flow.read(),'codex:a','codex:review'))
    def test_move_preserves_provider_and_board_rows_and_one_seat(self):
        before=(self.root/'board.sqlite3').read_bytes()
        self.flow.mutate({'operation':'place','revision':1,'item':{'agentId':'codex:a','laneId':'beta','role':'researcher'}})
        positions=[p for p in self.flow.read()['placements'] if p['agentId']=='codex:a']
        self.assertEqual([{'agentId':'codex:a','laneId':'beta','role':'researcher'}],positions)
        self.assertEqual(before,(self.root/'board.sqlite3').read_bytes())
        self.assertEqual('beta',Workflow(self.root,self.w,Path(self.tmp.name)/'empty').context('a')['assignment']['laneId'])
    def test_conflict_and_unknown_session_never_overwrite(self):
        self.flow.mutate({'operation':'note','revision':1,'item':{'agentId':'codex:a','text':'Keep working'}})
        with self.assertRaises(Conflict):self.flow.mutate({'operation':'place','revision':1,'item':{'agentId':'codex:a','laneId':'beta','role':'worker'}})
        with self.assertRaises(ValueError):self.flow.mutate({'operation':'place','revision':2,'item':{'agentId':'invented','laneId':'beta','role':'worker'}})
        self.assertEqual('Keep working',self.flow.context('a')['assignmentNote'])
    def test_passive_delivery_dedupe_revocation_and_held_request(self):
        r=self.flow.send('codex:a','codex:review','Evidence ready','key1');self.assertEqual('QUEUED',r['status']);self.assertFalse(r['launched']);self.assertFalse(r['interrupted'])
        self.assertEqual(r,self.flow.send('codex:a','codex:review','Evidence ready','key1'))
        with self.assertRaises(ValueError):self.flow.send('codex:a','codex:review','Changed','key1')
        self.assertEqual(1,len(self.flow.context('review')['inbox']))
        self.flow.mutate({'operation':'connection','revision':1,'item':{'from':'codex:a','to':'codex:review','allow':False}})
        self.assertEqual([],self.flow.context('review')['inbox']);self.assertEqual('HELD',self.flow.messages()[0]['status'])
        self.assertEqual('HELD',self.flow.send('codex:a','codex:b','Please review','key2')['status'])
    def test_default_explicit_only_undo_and_position(self):
        self.flow.mutate({'operation':'default','revision':1,'item':{'value':'explicit-only'}})
        self.assertFalse(allowed(self.flow.read(),'codex:a','codex:review'))
        self.flow.mutate({'operation':'undo','revision':2,'item':{}})
        self.assertTrue(allowed(self.flow.read(),'codex:a','codex:review'))
        self.flow.mutate({'operation':'position','revision':3,'item':{'laneId':'alpha','x':57,'y':100}})
        self.assertEqual(57,self.flow.read()['positions'][0]['x'])
        with self.assertRaises(ValueError):self.flow.mutate({'operation':'position','revision':4,'item':{'laneId':'alpha','x':float('nan'),'y':1}})
    def test_corrupt_control_fails_closed(self):
        self.assertTrue(manual_routing(self.root))
        with sqlite3.connect(self.flow.path) as db:db.execute("UPDATE workflow SET body='corrupt'")
        self.assertTrue(manual_routing(self.root))
    def test_provider_name_and_delegated_root_with_no_user_flag(self):
        home=Path(self.tmp.name)/'provider';home.mkdir()
        dbpath=home/'state_5.sqlite'
        with sqlite3.connect(dbpath) as db:
            db.execute('CREATE TABLE threads(id,name,title,cwd,model,reasoning_effort,updated_at,rollout_path,source,archived,has_user_event)')
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)',('a','Actual task name','Long initial prompt','/tmp','model','ultra',1,'/missing','vscode',0,0))
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)',('internal','Internal','Internal','/tmp','model','low',1,'/missing','{"subagent":{}}',0,1))
        before=dbpath.read_bytes()
        catalog=Workflow(self.root,self.w,home).catalog()['sessions']
        self.assertEqual('Actual task name',next(s['title'] for s in catalog if s['agent_id']=='codex:a'))
        self.assertFalse(any(s['agent_id']=='codex:internal' for s in catalog))
        self.assertEqual(before,dbpath.read_bytes())
    def test_all_board_wake_entrypoints_fail_closed_without_process(self):
        import board_core as b
        with patch.object(b,'ROOT',self.root),patch.object(b,'get_incident',return_value={'incident_id':'x'}),patch.object(b.subprocess,'Popen') as proc:
            self.assertEqual([],b.dispatch_open())
            self.assertEqual('HELD_FOR_OPERATOR',b.assign_incident('x')['routing_status'])
            self.assertEqual('HELD_FOR_OPERATOR',b.wake_incident('x')['routing_status'])
            self.assertEqual('HELD_FOR_OPERATOR',b._wake_codex({}, {})['status'])
            self.assertEqual('HELD_FOR_OPERATOR',b._wake_claude({}, {})['status'])
            proc.assert_not_called()
    def test_human_room_mirror_retries_without_duplicate_or_agent_delivery(self):
        r=self.flow.send('codex:a','codex:b','Review the candidate','mirror-key')
        self.assertEqual('HELD',r['status'])
        rows=self.w.rooms.query('global')['messages']
        self.assertEqual(1,len(rows));self.assertEqual(['operator'],rows[0]['recipients'])
        self.assertIn('codex:a',rows[0]['body']);self.assertIn('codex:b',rows[0]['body'])
        with sqlite3.connect(self.flow.path) as db:db.execute('DELETE FROM workflow_mirrors')
        self.flow.mirror_pending()
        self.assertEqual(1,self.w.rooms.query('global')['total'])
        self.assertEqual([],self.flow.context('b')['inbox'])

if __name__=='__main__':unittest.main()
