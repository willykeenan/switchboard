"""Read-only Library service projection; never enrolls staff or initializes schema.

Workflow and services share one SQLite read transaction. The separate project
catalog is revision checked. Local reads confer no human or agent write identity.
"""
from contextlib import contextmanager
import copy
import json
import sqlite3

from library_services import LibraryServices, Denied, SCHEMA
from workflow import DEFAULT, ROLES


class _SnapshotServices(LibraryServices):
    def __init__(self, workflow, db, topology, project):
        super().__init__(workflow.path, lambda: topology,
            lambda principal, op, payload, current, scope:
                principal == 'local-library-reader' and op == 'read'
                and scope.get('project') == project)
        self.db = db

    @contextmanager
    def connection(self, write=False):
        if write:
            raise Denied('Service writes are unavailable through the read adapter')
        yield self.db


class LibraryServiceView:
    def __init__(self, workflow):
        self.workflow = workflow

    def snapshot(self, project, lane='', team=''):
        if not isinstance(project, str) or not project or len(project) > 80:
            raise ValueError('Choose an existing project')
        for _ in range(3):
            workspace = self.workflow.workspace.read_source()
            path = self.workflow.path
            if not path.is_file() or path.is_symlink():
                raise ValueError('Workflow registry unavailable')
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5,
                                 isolation_level=None) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                row = db.execute('SELECT revision,body FROM workflow WHERE id=1').fetchone()
                if not row:
                    raise ValueError('Workflow registry is incomplete')
                state = {**copy.deepcopy(DEFAULT), **json.loads(row['body']), 'revision': row['revision']}
                lanes = state['laneCatalog'] if state['laneCatalog'] is not None else workspace['lanes']
                projects = [p['id'] for p in workspace['projects']]
                if project not in projects:
                    raise ValueError('Unknown project')
                lane_map = {l['id']: l for l in lanes}
                if lane and (lane not in lane_map or lane_map[lane]['projectId'] != project):
                    raise ValueError('Unknown lane or cross-project scope')
                if team and (not lane or (team not in ROLES and not any(
                        t['id'] == team and t['laneId'] == lane
                        for t in state['teams']))):
                    raise ValueError('Unknown Library service scope')
                topology = {
                    'revision': state['revision'], 'projects': projects,
                    'lanes': {l['id']: {'project': l['projectId'],
                        'state': l.get('lifecycle', 'active'),
                        'function': 'library' if l['id'] == 'project-' + l['projectId'] + '-librarian' else '',
                        'mergedInto': l.get('mergedInto')} for l in lanes},
                    'teams': {t['id']: {'lane': t['laneId'], 'role': t['role'],
                        'serviceSeat': t['id'].startswith('lib-')} for t in state['teams']},
                    # Runtime enrollment is not implemented. Card placement does
                    # not prove an authoritative provider or queue identity.
                    'agents': {},
                }
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {'library_service_meta', 'librarian_homes', 'library_services',
                            'library_service_requests', 'library_service_events'}
                present = tables & required
                if present and present != required:
                    raise ValueError('Library registry schema is incomplete')
                if present:
                    meta = db.execute('SELECT schema_version FROM library_service_meta WHERE singleton=1').fetchone()
                    if not meta or meta[0] != SCHEMA:
                        raise ValueError('Unsupported Library registry schema')
                    registry = _SnapshotServices(self.workflow, db, topology, project)
                    registry.verify()
                    result = registry.snapshot('local-library-reader', project)
                else:
                    result = {'revision': None, 'topologyRevision': state['revision'],
                        'homes': [], 'services': [], 'capacityReferences': [],
                        'runtimeReservationsCreated': 0, 'primaryPlacementsCreated': 0}
                for home in result['homes']:
                    home.update(enrollmentVerified=False, availability='Enrollment unavailable')
                if lane:
                    research_team = team == 'researcher' or any(
                        t['id'] == team and t['role'] == 'researcher' for t in state['teams'])
                    result['services'] = [s for s in result['services'] if s['lane'] == lane
                                          and (not research_team or s['team'] == team)]
                result.update(project=project, lane=lane, team=team, readOnly=True,
                    schemaReady=bool(present), workspaceRevision=workspace['revision'],
                    enrollmentAvailable=False, writesAvailable=False,
                    staffingState='enrollment-unavailable' if result['homes'] else 'unstaffed')
            if self.workflow.workspace.read_source()['revision'] == workspace['revision']:
                return result
        raise ValueError('Project catalog changed while reading Library; reload')
