"""Small read-only map projections; task details and control routes stay authoritative."""
import time


def structure(flow):
    state = flow.read()
    workspace = flow.workspace.read()
    catalog = flow.identity_catalog()
    fields = ('agent_id', 'endpoint', 'provider', 'title', 'model', 'reasoning', 'cwd', 'managed')
    return {**state, 'projects': workspace['projects'], 'lanes': workspace['lanes'],
            'sessions': [{k: s.get(k) for k in fields} for s in catalog['sessions']],
            'errors': catalog.get('errors', []), 'observedAt': time.time(),
            'view': 'world', 'readOnly': True}


def tasks(store, project=None, lane=None):
    fields = ('taskId', 'title', 'request', 'state', 'waitReason', 'ownerId', 'workerId',
              'projectId', 'laneId', 'updatedAt', 'version', 'assignmentId', 'courierId',
              'station', 'heldByWorker', 'displayState', 'executionFresh', 'legacy')
    import json
    with store.connect() as db:
        workers = [json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_workers')]
        policies = [json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_policies')]
        counts = dict(db.execute('SELECT task_id, COUNT(*) FROM taskflow_assignments GROUP BY task_id'))
    matches = lambda row: (not project or row.get('projectId') == project) and (not lane or row.get('laneId') == lane)
    # No audit eligibility scans, transcript sampling, event payloads, shared
    # evidence or private worker instructions are needed to paint a map.
    def project_task(t):
        refs = [{k: r.get(k) for k in ('id', 'kind', 'world')} for r in t.get('sourceRefs', []) if isinstance(r, dict) and r.get('kind') == 'scene-report' and r.get('id')]
        return {**{k: t.get(k) for k in fields}, 'reportRefs': refs, 'assignmentCount': counts.get(t['taskId'], 0)}
    return {'tasks': [project_task(t) for t in store.list(project, lane)],
            'workers': [{k: w.get(k) for k in ('workerId', 'projectId', 'laneId', 'enabled', 'mode', 'idleAt')} for w in workers if matches(w)],
            'policies': [{**{k: p.get(k) for k in ('projectId', 'laneId', 'enabled', 'version', 'autoReady', 'allowManagedStarts')}, 'execution': {'expiresAt': (p.get('execution') or {}).get('expiresAt')}} for p in policies if matches(p)],
            'reportCatalogVersion': 1, 'dispatchDiagnosticsVersion': 1, 'recoveryHold': bool(store.recovery_hold),
            'observedAt': store.clock(), 'view': 'world', 'readOnly': True}
