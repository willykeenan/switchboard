import concurrent.futures
import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workspace import Workspace, Conflict, workspace_reader
from room_reader import RoomStore

def fixture(root):
    root.mkdir(exist_ok=True)
    room=root/'rooms'/'global';room.mkdir(parents=True,exist_ok=True)
    (room/'messages.jsonl').write_text('')
    with sqlite3.connect(root/'board.sqlite3') as db:
        db.execute('CREATE TABLE agents(agent_id,endpoint,provider,display_name,status,last_seen_at)')
        db.execute('CREATE TABLE tasks(task_id,owner,objective,status,next_action,blocker,updated_at,aliases_json,dependencies_json)')
        for a in ('a','b','review'):
            db.execute('INSERT INTO agents VALUES(?,?,?,?,?,?)',('codex:'+a,a,'codex','Agent '+a,'ACTIVE','2000-01-01T00:00:00Z'))
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)',('task-a','codex:a','Deliver A','WORKING','Verify evidence','','2026-09-07T00:00:00Z','[]','[]'))
    d={'projects':[{'id':'demo','name':'Demo','objective':'One shared outcome'}],
       'lanes':[{'id':'alpha','projectId':'demo','name':'Alpha','objective':'Produce A','aliases':['codex-alpha']},{'id':'beta','projectId':'demo','name':'Beta','objective':'Produce B','aliases':['codex-beta']}],
       'members':[{'laneId':'alpha','agentId':'codex:a','role':'writer'},{'laneId':'alpha','agentId':'codex:review','role':'auditor'},{'laneId':'beta','agentId':'codex:review','role':'auditor'}],
       'taskLinks':[{'laneId':'alpha','taskId':'task-a'}], 'dependencies':[]}
    w=Workspace(root,RoomStore(root/'rooms'));w.save(d,0,'fixture')
    return w,d

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'board';self.w,self.d=fixture(self.root)
        # These tests cover the legacy workspace view, used only when manual routing is switched off.
        from workflow import set_routing_mode
        set_routing_mode(self.root,False,'fixture')
    def tearDown(self):self.tmp.cleanup()
    def test_read_only_snapshot_preserves_existing_board(self):
        original=(self.root/'board.sqlite3').read_bytes()
        s=self.w.snapshot();self.assertEqual('WORKING',s['lanes'][0]['status']);self.assertEqual('UNLINKED',s['lanes'][1]['status'])
        self.assertTrue(all(a['presence']=='UNKNOWN' for a in s['agents']))
        self.assertEqual(original,(self.root/'board.sqlite3').read_bytes())
    def test_observation_connection_cannot_write_or_create_missing_database(self):
        before=self.w.path.read_bytes()
        with workspace_reader(self.w.path) as db:
            self.assertEqual(1,db.execute('SELECT revision FROM workspace').fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM workspace')
        with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
        self.assertEqual(before,self.w.path.read_bytes())
        missing=self.root/'absent.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):workspace_reader(missing)
        self.assertFalse(missing.exists())

    def test_concurrent_writers_compare_revision_and_keep_history(self):
        def save(_):
            try:self.w.save(self.d,1,'concurrent');return True
            except Conflict:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as p:result=list(p.map(save,range(6)))
        self.assertEqual(1,sum(result));self.assertEqual(2,self.w.read()['revision'])
        with sqlite3.connect(self.w.path) as c:self.assertEqual(2,c.execute('SELECT count(*) FROM workspace_history').fetchone()[0])
    def test_reject_unknown_owner_and_self_auditing(self):
        for change in ('owner','unknown','auditor'):
            d=copy.deepcopy(self.d)
            if change=='owner':d['taskLinks'][0]['laneId']='beta'
            if change=='unknown':d['members'][0]['agentId']='codex:missing'
            if change=='auditor':d['members'].append({'laneId':'alpha','agentId':'codex:a','role':'auditor'})
            with self.assertRaises(ValueError):self.w.save(d,1,'test')
        self.assertEqual(1,self.w.read()['revision'])
    def test_dependency_cycles_and_generic_aliases_rejected(self):
        d=copy.deepcopy(self.d);d['dependencies']=[{'laneId':'alpha','needsLaneId':'beta','reason':'Needs output'},{'laneId':'beta','needsLaneId':'alpha','reason':'Needs output'}]
        with self.assertRaisesRegex(ValueError,'cycle'):self.w.save(d,1,'test')
        d=copy.deepcopy(self.d);d['lanes'][0]['aliases']=['claude']
        with self.assertRaises(ValueError):self.w.save(d,1,'test')
    def test_explicit_multilane_post_is_one_original_and_idempotent(self):
        m=self.w.post('demo',['alpha','beta'],'codex-alpha','One shared handoff',key='once')
        again=self.w.post('demo',['beta','alpha'],'codex-alpha','One shared handoff',key='once')
        self.assertEqual(m,again)
        self.assertEqual(1,self.w.rooms.query('global')['total'])
        self.assertEqual(1,self.w.feed(project='demo')['matched'])
        for lane in ['alpha','beta']:self.assertEqual('Explicit lane',self.w.feed(lane=lane)['messages'][0]['laneMatches'][0]['basis'])
        with self.assertRaises(SystemExit):self.w.post('demo',['alpha'],'codex-alpha','One shared handoff',key='once')
    def test_legacy_matching_never_broadcasts_shared_auditor(self):
        messages=[{'seq':1,'author':'codex-review','recipients':['all'],'body':'Unrelated review'}, {'seq':2,'author':'codex-alpha','recipients':['all'],'body':'Legacy alpha'}, {'seq':3,'author':'codex-alpha','recipients':['all'],'body':'Beta explicit','workspace':{'projectId':'demo','laneIds':['beta']}}]
        log=self.root/'rooms/global/messages.jsonl';log.write_text(''.join(json.dumps(m)+'\n' for m in messages))
        self.assertEqual([2],[m['seq'] for m in self.w.feed(lane='alpha')['messages']]);self.assertEqual([3],[m['seq'] for m in self.w.feed(lane='beta')['messages']])
        self.assertEqual(2,self.w.feed(project='demo')['matched'])
    def test_filters_pagination_and_new_messages(self):
        for i in range(35):self.w.post('demo',['alpha'],'author-a' if i%2 else 'author-b','Needle '+str(i))
        first=self.w.feed(lane='alpha');second=self.w.feed(lane='alpha',before=first['nextBefore'])
        self.assertEqual(30,len(first['messages']));self.assertEqual(5,len(second['messages']))
        self.assertEqual(1,self.w.feed(lane='alpha',q='Needle 34',author='author-b')['matched'])
        self.w.post('demo',['alpha'],'author-b','New arrival');self.assertEqual(36,self.w.feed(lane='alpha')['latestSeq'])
    def test_mutations_and_own_task_unlink_guard(self):
        with self.assertRaises(ValueError):self.w.mutate({'operation':'unlink-member','revision':1,'item':self.d['members'][0]})
        self.w.mutate({'operation':'task','revision':1,'item':{'taskId':'task-a','laneId':''}})
        self.w.mutate({'operation':'unlink-member','revision':2,'item':self.d['members'][0]})
        self.assertFalse(self.w.read()['taskLinks'])
        self.assertEqual(3,self.w.read()['revision'])
    def test_empty_context_and_linked_context(self):
        self.assertEqual([],self.w.context('nobody')['lanes'])
        self.assertEqual('alpha',self.w.context('a')['lanes'][0]['id'])
    def test_http_same_origin_token_conflict_and_assets(self):
        os.environ['SWITCHBOARD_ROOT']=str(self.root);os.environ.pop('SWITCHBOARD_ROOMS_ROOT',None)
        sys.modules.pop('board_core',None);sys.modules.pop('server',None)
        server=importlib.import_module('server');server.WORKSPACE=self.w
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        origin='http://127.0.0.1:'+str(http.server_port)
        try:
            with urlopen(origin+'/api/workspace') as r:d=json.load(r)
            body=json.dumps({'operation':'project','revision':d['revision'],'item':{'id':'new','name':'New','objective':'New outcome'}}).encode()
            headers={'Content-Type':'application/json','X-KE-Board-Token':d['controlToken'],'Origin':origin}
            for changes in [{'Origin':'https://evil.example'},{'X-KE-Board-Token':'bad'},{'Host':'evil.example'}]:
                with self.assertRaises(HTTPError) as err:urlopen(Request(origin+'/api/workspace',body,{**headers,**changes}))
                self.assertEqual(403,err.exception.code)
            with urlopen(Request(origin+'/api/workspace',body,headers)) as r:self.assertTrue(json.load(r)['ok'])
            with self.assertRaises(HTTPError) as err:urlopen(Request(origin+'/api/workspace',body,headers))
            self.assertEqual(409,err.exception.code)
            for path in ['/rooms?project=demo','/workspaces.js','/rooms.js','/rooms.css']:
                with urlopen(origin+path) as r:self.assertEqual(200,r.status)
        finally:http.shutdown();http.server_close();thread.join()

if __name__=='__main__':unittest.main()
