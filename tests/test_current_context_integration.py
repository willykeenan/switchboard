import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import gzip
import sqlite3

import test_current_context as fixtures


class BoardContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CurrentContextTests(); self.fixture.setUp()
        self.root = self.fixture.root/'board'
        self.cli = Path(__file__).resolve().parents[1]/'boardctl.py'
        self.assertTrue(self.cli.is_file(), 'run integration tests in the assembled board candidate or installed board')
        self.env = {**os.environ,'SWITCHBOARD_ROOT':str(self.root),'SWITCHBOARD_DISABLE_ROOM':'1',
                    'PYTHONDONTWRITEBYTECODE':'1'}
        self.call('check-in','--provider','codex','--endpoint','a','--team','GENERAL')

    def tearDown(self):
        self.fixture.tearDown()

    def call(self,*args):
        r = subprocess.run([sys.executable,'-B',str(self.cli),*args],env=self.env,
                           capture_output=True,text=True,timeout=10)
        self.assertEqual(0,r.returncode,r.stderr)
        return json.loads(r.stdout)

    def register(self):
        return self.call('context-register','--manifest',str(self.fixture.manifest),
                         '--sha256',self.fixture.pin,'--owner','codex:a')

    def task(self,identity):
        contract = self.fixture.root/(identity+'-contract.json')
        contract.write_text(json.dumps({'task_id':identity,'owner':'codex:a',
          'objective':'Retain completed component history',
          'scope':[str(self.fixture.root/'tasks'/identity)],'dependencies':[],
          'next_action':'Director review pending',
          'criteria':[{'id':'evidence','kind':'json','description':'retained acceptance',
            'path':str(self.fixture.acceptance),'pointer':'/verdict','equals':'ACCEPT_SYNTHETIC_ONLY'}]}))
        return self.call('task-register','--file',str(contract))

    def test_fresh_checkin_and_brief_load_older_component_outside_recent_tasks(self):
        self.task('old-stage')
        for n in range(9): self.task('newer-'+str(n))
        self.register()
        for result in [self.call('check-in','--provider','codex','--endpoint','a','--team','AUTO')['briefing'],
                       self.call('brief','--endpoint','a')]:
            self.assertNotIn('old-stage',[t['task_id'] for t in result['tasks']])
            self.assertTrue(result['tasks_more'])
            context = result['currentContext']['contexts'][0]
            self.assertEqual('synthetic-accounting',context['components'][0]['id'])
            self.assertEqual('passed',context['components'][0]['states']['testing']['status'])
            self.assertEqual('unproven',context['components'][0]['states']['qualification']['status'])

    def test_display_overlay_does_not_rewrite_stored_task_and_drift_stays_visible(self):
        before = self.task('old-stage'); self.register()
        brief = self.call('brief','--endpoint','a')
        row = next(t for t in brief['tasks'] if t['task_id']=='old-stage')
        self.assertEqual('Director review pending',row['historical_next_action'])
        self.assertIn('qualified historical inputs',row['next_action'])
        self.assertEqual(before,self.call('task-get','--task-id','old-stage'))

        self.fixture.acceptance.unlink()
        brief = self.call('check-in','--provider','codex','--endpoint','a','--team','AUTO')['briefing']
        self.assertEqual('PARTIAL',brief['currentContext']['status'])
        state = brief['currentContext']['contexts'][0]['components'][0]['states']['testing']
        self.assertEqual('unknown',state['status'])
        self.assertEqual(before,self.call('task-get','--task-id','old-stage'))

    def test_cross_session_context_obeys_connection_revocation(self):
        self.call('check-in','--provider','codex','--endpoint','b','--team','GENERAL')
        self.call('context-register','--manifest',str(self.fixture.manifest),
                  '--sha256',self.fixture.pin,'--owner','codex:a','--endpoint','b')
        policy = {'enabled':True,'defaultCommunication':'explicit-only','placements':[],
                  'connections':[],'notes':[],'positions':[]}
        path = self.root/'runtime'/'workflow.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE workflow(id INTEGER PRIMARY KEY,revision INTEGER,body TEXT,updated_at TEXT)')
            db.execute('INSERT INTO workflow VALUES(1,1,?,?)',(json.dumps(policy),'fixture'))
        self.assertEqual([],self.call('brief','--endpoint','b')['currentContext']['contexts'])
        self.assertEqual(1,len(self.call('brief','--endpoint','a')['currentContext']['contexts']))
        policy['connections']=[{'from':'codex:a','to':'codex:b','allow':True}]
        with sqlite3.connect(path) as db:db.execute('UPDATE workflow SET body=?',(json.dumps(policy),))
        self.assertEqual(1,len(self.call('brief','--endpoint','b')['currentContext']['contexts']))
        with sqlite3.connect(path) as db:db.execute("UPDATE workflow SET body='corrupt'")
        self.assertEqual([],self.call('brief','--endpoint','b')['currentContext']['contexts'])

    def test_current_backup_restores_registry_history_and_checks_digest(self):
        self.register()
        maintenance=self.cli.parent/'maintenance.py'
        def run(*args):
            return subprocess.run([sys.executable,'-B',str(maintenance),*args],env=self.env,
                                  capture_output=True,text=True,timeout=10)
        original=fixtures.cc.snapshot_registry(self.root)
        r=run('backup-current');self.assertEqual(0,r.returncode,r.stderr)
        path=Path(json.loads(r.stdout)['path'])
        r=run('verify-current',str(path));self.assertEqual(0,r.returncode,r.stderr)
        result=json.loads(r.stdout);self.assertFalse(result['live_database_touched'])
        self.assertEqual(2,result['currentContextRegistry']['files'])
        self.assertEqual(original,fixtures.cc.snapshot_registry(self.root))
        with gzip.open(path,'rb') as f: data=json.load(f)
        data['currentContextRegistry']['files'][0]['sha256']='0'*64
        bad=self.fixture.root/'bad-recovery.json.gz'
        with gzip.open(bad,'wt') as f:json.dump(data,f)
        self.assertNotEqual(0,run('verify-current',str(bad)).returncode)
        self.assertEqual(original,fixtures.cc.snapshot_registry(self.root))


if __name__ == '__main__': unittest.main()
