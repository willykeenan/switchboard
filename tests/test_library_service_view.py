"""Read integration and recovery tests against disposable real SQLite stores."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from library_service_view import LibraryServiceView, _SnapshotServices
from library_services import LibraryServices
from workflow import DEFAULT
from workspace import Workspace
from workflow_layout import snapshot_stores, restore_stores
from inspector.library import Library


class ReadIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root/'runtime').mkdir()
        self.path = self.root/'runtime/workflow.sqlite3'
        self.state = copy.deepcopy(DEFAULT)
        self.state['laneCatalog'] = [dict(id='project-p1-librarian',projectId='p1',lifecycle='active'),
                                    dict(id='lane1',projectId='p1',lifecycle='active'),
                                    dict(id='lane2',projectId='p2',lifecycle='active')]
        self.state['teams'] = [dict(id='home-team',laneId='project-p1-librarian',role='coordinator')]
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE workflow(id INTEGER PRIMARY KEY,revision INTEGER,body TEXT,updated_at TEXT)')
            db.execute('INSERT INTO workflow VALUES(1,7,?,NULL)',(json.dumps(self.state),))
        with sqlite3.connect(self.root/'runtime/workspaces.sqlite3') as db:
            db.execute('CREATE TABLE workspace(id INTEGER PRIMARY KEY,revision INTEGER,body TEXT,updated_at TEXT)')
            db.execute('INSERT INTO workspace VALUES(1,3,?,NULL)',(json.dumps({'projects':[{'id':'p1'},{'id':'p2'}],'lanes':[]}),))
        self.flow = SimpleNamespace(path=self.path,workspace=Workspace(self.root,None))
        self.view = LibraryServiceView(self.flow)
        self.top = {'revision':7,'projects':['p1','p2'],'lanes':{
            'project-p1-librarian':{'project':'p1','state':'active','function':'library'},
            'lane1':{'project':'p1','state':'active'}},
            'teams':{'home-team':{'lane':'project-p1-librarian','role':'coordinator'}},
            'agents':{'agent-a':{'provider':'fixture:123','queue':'fixture:q',
                'primary':{'project':'p1','lane':'project-p1-librarian','team':'home-team'}}}}
        self.registry=LibraryServices(self.path,lambda:self.top,lambda *args:True)

    def tearDown(self):
        self.temp.cleanup()

    def seed(self):
        self.registry.initialize()
        self.registry.apply('test','home',0,'register_home',dict(agent='agent-a',provider='fixture:123',queue='fixture:q',project='p1',lane='project-p1-librarian',team='home-team'),expected_topology_revision=7)
        self.registry.apply('test','bind',1,'bind',dict(id='service-a',agent='agent-a',project='p1',lane='lane1',team='researcher',kind='context'),expected_topology_revision=7)

    def test_missing_schema_read_never_migrates(self):
        before=self.path.read_bytes();result=self.view.snapshot('p1')
        self.assertFalse(result['schemaReady']);self.assertEqual(before,self.path.read_bytes())

    def test_initialized_empty_registry_is_not_staffing(self):
        self.registry.initialize();r=self.view.snapshot('p1')
        self.assertTrue(r['schemaReady']);self.assertEqual(r['staffingState'],'unstaffed')
        self.assertFalse(r['writesAvailable']);self.assertEqual(r['homes'],[])

    def test_exact_identity_without_invented_enrollment(self):
        self.seed();r=self.view.snapshot('p1','lane1','researcher')
        self.assertEqual(len(r['homes']),1);self.assertEqual(len(r['capacityReferences']),1)
        self.assertEqual(r['services'][0]['id'],'service-a');self.assertFalse(r['services'][0]['scopeUsable'])
        self.assertFalse(r['homes'][0]['enrollmentVerified']);self.assertEqual(r['runtimeReservationsCreated'],0)
        self.assertEqual(self.view.snapshot('p2')['homes'],[])

    def test_build_and_owner_read_shared_lane_services(self):
        self.seed()
        for team in ('writer','coordinator'):
            r=self.view.snapshot('p1','lane1',team)
            self.assertEqual(r['services'][0]['id'],'service-a')
            self.assertFalse(r['writesAvailable'])

    def test_unknown_and_cross_project_scope_rejected(self):
        for args in [('missing',),('p1','lane2'),('p1','','researcher'),('p1','lane1','home-team')]:
            with self.assertRaises(ValueError):self.view.snapshot(*args)

    def test_corrupt_schema_is_not_reported_as_empty(self):
        self.registry.initialize()
        with sqlite3.connect(self.path) as db:db.execute('UPDATE library_service_meta SET schema_version=999')
        with self.assertRaisesRegex(ValueError,'Unsupported'):self.view.snapshot('p1')

    def test_partial_schema_is_not_reported_as_empty(self):
        self.registry.initialize()
        with sqlite3.connect(self.path) as db:db.execute('DROP TABLE library_service_requests')
        with self.assertRaisesRegex(ValueError,'incomplete'):self.view.snapshot('p1')

    def test_topology_and_services_share_one_read_snapshot(self):
        self.registry.initialize();original=_SnapshotServices.verify
        def concurrent_write(registry):
            with sqlite3.connect(self.path) as db:db.execute('UPDATE workflow SET revision=8')
            return original(registry)
        with patch.object(_SnapshotServices,'verify',concurrent_write):r=self.view.snapshot('p1')
        self.assertEqual(r['topologyRevision'],7)
        self.assertEqual(self.view.snapshot('p1')['topologyRevision'],8)

    def test_existing_recovery_carries_all_service_history(self):
        self.seed();snapshot=snapshot_stores(self.root);dest=self.root/'restored'
        restore_stores(dest,snapshot)
        recovered=LibraryServices(dest/'runtime/workflow.sqlite3',lambda:self.top,lambda *a:True)
        self.assertEqual(recovered.verify(),self.registry.verify())
        self.assertEqual(recovered.snapshot('test','p1'),self.registry.snapshot('test','p1'))

    def test_index_not_started_by_library_query(self):
        # Existing real SQLite catalog and query code; any attempted launch fails.
        library=Library(self.root,SimpleNamespace(snapshot=lambda:{'lanes':[], 'placements':[], 'projects':[{'id':'p1'}], 'revision':7}))
        with patch('inspector.context.resolve_scope'),patch.object(library,'refresh',side_effect=AssertionError('Read started indexing')):
            r=library.query('p1')
        self.assertEqual(r['total'],0)


if __name__=='__main__':unittest.main()
