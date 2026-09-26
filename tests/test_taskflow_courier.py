import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_workspace import fixture
from workflow import Workflow
from taskflow import TaskFlow, encoded

class CourierLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'board'
        self.w,self.old=fixture(self.root)
        self.flow=Workflow(self.root,self.w,Path(self.tmp.name)/'no-provider-store')
        state={k:v for k,v in self.flow.read().items() if k!='revision'}
        state.update(enabled=True,placements=[{'agentId':'codex:a','laneId':'alpha','role':'writer'},{'agentId':'codex:b','laneId':'beta','role':'worker'},{'agentId':'codex:review','laneId':'alpha','role':'auditor'}])
        self.flow.save(state,0,'fixture')
        self.now=[1.0]
        self.store=TaskFlow(self.root,self.flow,clock=lambda:self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def _put_task(self, db, **fields):
        t={'taskId':fields.get('taskId','t1'),'version':1,'taskVersion':1,'request':'do','title':'do','projectId':'demo','laneId':'alpha',
           'state':fields.get('state','ACCEPTED'),'waitReason':'','station':fields.get('station','codex:a'),
           'workerId':fields.get('workerId','codex:a'),'assignmentId':fields.get('assignmentId','as1'),
           'courierId':fields.get('courierId','bird-1'),'scopes':[],'capabilities':[],'dependencies':[],'inputs':[],
           'amendments':[],'createdAt':1,'updatedAt':1,'ruleVersion':'taskflow-3'}
        db.execute('INSERT OR REPLACE INTO taskflow_tasks VALUES(?,?)',(t['taskId'],encoded(t)))
        return t

    def _put_assignment(self, db, **fields):
        a={'id':fields.get('id','as1'),'taskId':fields.get('taskId','t1'),'workerId':fields.get('workerId','codex:a'),
           'state':fields.get('state','ACCEPTED'),'courierId':fields.get('courierId','bird-1'),
           'payload':{'inputs':[],'scopes':[]},'payloadHash':'x','at':1,'heartbeatAt':1,'taskVersion':1,'policyVersion':1}
        db.execute('INSERT INTO taskflow_assignments VALUES(?,?,?,?,?)',(a['id'],a['taskId'],a['workerId'],a['state'],encoded(a)))
        return a

    def _put_worker(self, db, idleAt=None):
        w={'workerId':'codex:a','enabled':True,'idleAt':idleAt,'mode':'cooperative','laneId':'alpha','projectId':'demo',
           'capabilities':['general'],'scopes':['/tmp'],'role':'writer','teamId':'writer'}
        db.execute('INSERT OR REPLACE INTO taskflow_workers VALUES(?,?)',(w['workerId'],encoded(w)))
        return w

    def test_accepted_assignment_roosts_bird(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._put_task(db,state='ACCEPTED');self._put_assignment(db,state='ACCEPTED')
        birds={b['id']:b for b in self.store.snapshot()['birds']}
        self.assertEqual('standby',birds['bird-1']['state'])
        self.assertIsNone(birds['bird-1']['taskId'])

    def test_review_roosts_bird(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._put_task(db,state='REVIEW',station='review:alpha');self._put_assignment(db,state='RETURNED')
        birds={b['id']:b for b in self.store.snapshot()['birds']}
        self.assertEqual('standby',birds['bird-1']['state'])

    def test_done_roosts_bird(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._put_task(db,state='DONE');self._put_assignment(db,state='RETURNED')
        birds={b['id']:b for b in self.store.snapshot()['birds']}
        self.assertEqual('standby',birds['bird-1']['state'])
        self.assertIsNone(birds['bird-1']['taskId'])

    def test_declined_frees_bird(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._put_task(db,state='QUEUED',station='board:alpha');self._put_assignment(db,state='DECLINED')
        birds={b['id']:b for b in self.store.snapshot()['birds']}
        self.assertEqual('standby',birds['bird-1']['state'])

    def test_five_busy_birds_block_sixth(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for i in range(1,6):
                self._put_task(db,taskId='t'+str(i),assignmentId='as'+str(i),courierId='bird-'+str(i),state='OFFERED')
                self._put_assignment(db,id='as'+str(i),taskId='t'+str(i),courierId='bird-'+str(i),state='OFFERED',workerId='codex:w'+str(i))
            busy=self.store._busy_couriers(db)
        self.assertEqual(5,len(busy))

    def test_review_does_not_fill_worker_execution_inventory(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._put_task(db,state='REVIEW');self._put_assignment(db,state='RETURNED');self._put_worker(db,idleAt=None)
            self.assertFalse(self.store._worker_execution_busy(db,'codex:a'))
            w=self.store._one(db,'taskflow_workers','codex:a')
        self.assertIsNone(w['idleAt'])
        shown=self.store.get('t1')
        self.assertFalse(shown['heldByWorker'])

    def test_reconcile_does_not_invent_cooperative_availability(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._put_worker(db,idleAt=None)
        self.store.reconcile()
        with self.store.connect() as db:
            w=self.store._one(db,'taskflow_workers','codex:a')
        self.assertIsNone(w['idleAt'])

    def test_superseded_assignment_does_not_keep_a_second_bird(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._put_task(db,state='OFFERED',assignmentId='as2',courierId='bird-2')
            self._put_assignment(db,state='RETURNED')
            self._put_assignment(db,id='as2',state='OFFERED',courierId='bird-2')
        birds={b['id']:b for b in self.store.snapshot()['birds']}
        self.assertEqual('standby',birds['bird-1']['state'])
        self.assertEqual('delivering',birds['bird-2']['state'])
        self.assertEqual('t1',birds['bird-2']['taskId'])

    def test_agent_boxes_and_inventory_are_in_the_room_ui(self):
        js=(Path(__file__).resolve().parents[1]/'workflow-structure.js').read_text()
        css=(Path(__file__).resolve().parents[1]/'workflow-structure.css').read_text()
        self.assertIn('wf-agent-box',js)
        self.assertIn('wf-agent-inventory',js)
        self.assertIn('heldByWorker',js)
        self.assertIn('Empty · no live assignment',js)
        self.assertIn('paintInventory',js)
        self.assertNotIn("t.state==='REVIEW'&&t.station===agentId",js)
        self.assertIn("waitingStates=['CAPTURED','LINKED','NEEDS_COORDINATION','QUEUED','READY']",js.replace(' ',''))
        self.assertIn('tf-waiting-board',js)
        self.assertIn('No waiting tasks',js)
        self.assertIn('.wf-agent-box',css)
        self.assertIn('.wf-agent-inventory',css)

    def test_container_parents_are_not_travel_attention(self):
        js=(Path(__file__).resolve().parents[1]/'taskflow.js').read_text()
        health=js.split('window.TaskFlowHealth={assess(task){')[1].split('}};')[0]
        compact=health.replace(' ','')
        self.assertIn("kind==='container'",compact)
        self.assertIn("container&&['NEEDS_COORDINATION','QUEUED','CAPTURED','READY']",compact)
        self.assertNotIn("'NEEDS_COORDINATION'",compact.split('constblocked=')[1].split('return')[0])

    def test_js_returns_birds_to_the_birdhouse(self):
        js=(Path(__file__).resolve().parents[1]/'taskflow.js').read_text()
        css=(Path(__file__).resolve().parents[1]/'taskflow.css').read_text()
        self.assertIn("leg:'home'",js)
        self.assertIn('tf-flight-bird',js)
        self.assertIn('tf-birdhouse',js)
        self.assertIn('dataset.perch',js)
        self.assertIn('elapsed>=2800',js.replace(' ',''))
        self.assertIn('.tf-flight-bird[data-phase=home] .tf-paper',css)
        self.assertIn('.tf-roost-bird[data-perch=empty]',css)

    def test_legacy_working_without_live_assignment_stays_on_board(self):
        row=next(r for r in self.store.legacy() if r['taskId']=='task-a')
        self.assertTrue(str(row['station']).startswith('board:'))
        self.assertFalse(row['heldByWorker'])
        self.assertNotEqual('STATUS_UNKNOWN', row['state'])

if __name__=='__main__':
    unittest.main()
