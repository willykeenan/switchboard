"""Versioned project, workstream and team context; no provider activity inference."""
import json
import time
from urllib.parse import urlencode
from .library import connect, digest, encoded, now

ROOT_TEAMS = {
    'coordinator': 'Workstream Owner', 'researcher': 'Research',
    'writer': 'Build & Integration', 'worker': 'Execution', 'auditor': 'Audit',
}

def resolve_scope(library, project, lane='', team='', writing=False):
    if not all(isinstance(v, str) for v in (project, lane, team)):
        raise ValueError('Project, workstream and team must be identifiers')
    state = library.workflow.snapshot()
    project_record = next((p for p in state['projects'] if p['id'] == project), None)
    if not project_record:
        raise ValueError('Choose a current project')
    lane_record = next((l for l in state['lanes'] if l['id'] == lane and l['projectId'] == project), None)
    if lane and not lane_record:
        raise ValueError('Choose a workstream in this project')
    if team and not lane:
        raise ValueError('Choose a workstream before choosing a team')
    if writing and lane_record and lane_record.get('lifecycle') in ('closed','merged'):
        raise ValueError('This workstream is closed or merged; retained records remain readable')
    teams = [t for t in state.get('teams', []) if t['laneId'] == lane]
    team_record = next((t for t in teams if t['id'] == team), None)
    if team in ROOT_TEAMS:
        team_record = {'id': team, 'name': ROOT_TEAMS[team], 'role': team}
    if team and not team_record:
        raise ValueError('Choose a team in this workstream')
    # Reject broken ancestry rather than making orphaned teams look valid.
    if team_record and team not in ROOT_TEAMS:
        seen = {team}
        parent = team_record.get('parentId')
        while parent not in ROOT_TEAMS:
            ancestor = next((t for t in teams if t['id'] == parent), None)
            if not ancestor or parent in seen:
                raise ValueError('Team ancestry is invalid in this workstream')
            seen.add(parent)
            parent = ancestor.get('parentId')
    return state, project_record, lane_record, team_record

def organization(library, project, lane='', team=''):
    state, p, l, t = resolve_scope(library, project, lane, team)
    lane_ids = {lane} if lane else {x['id'] for x in state['lanes'] if x['projectId'] == project}
    ids = {team}
    if team:
        while True:
            expanded = ids | {x['id'] for x in state.get('teams', [])
                              if x['laneId'] == lane and x.get('parentId') in ids}
            if expanded == ids:
                break
            ids = expanded
    seats = [s for s in state['placements'] if s['laneId'] in lane_ids
             and (not team or (s.get('teamId') or s['role']) in ids)]
    agents = {s['agent_id']: s for s in library.workflow.catalog()['sessions']}
    members = [{'agentId': s['agentId'], 'title': agents.get(s['agentId'], {}).get('title', s['agentId']),
                'laneId': s['laneId'], 'teamId': s.get('teamId') or s['role']} for s in seats]
    tasks = [agents[s['agentId']]['latestTask'] for s in seats
             if agents.get(s['agentId'], {}).get('latestTask')]
    agent_ids = {s['agentId'] for s in seats}
    record = {
        'project': p, 'lane': l, 'team': t,
        'members': sorted(members, key=encoded),
        'tasks': sorted(tasks, key=encoded),
        'directions': sorted([n for n in state.get('notes', []) if n['agentId'] in agent_ids], key=encoded),
        'leads': sorted([r for r in state.get('teamLeads', []) if r['laneId'] in lane_ids
                         and (not team or r['teamId'] in ids)], key=encoded),
    }
    return record, state['revision']

def schema(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required={'team_context','context_versions','review_scopes','note_scopes','write_keys','record_scopes'}
    if required <= tables:
        missing=db.execute("SELECT 1 FROM notes n WHERE NOT EXISTS (SELECT 1 FROM record_scopes s WHERE s.id=n.id) UNION ALL SELECT 1 FROM reviews r WHERE NOT EXISTS (SELECT 1 FROM record_scopes s WHERE s.id=r.id) LIMIT 1").fetchone()
        if not missing:return
    db.executescript('''CREATE TABLE IF NOT EXISTS team_context(project TEXT,lane TEXT,team TEXT,version INTEGER,body TEXT,updated TEXT,PRIMARY KEY(project,lane,team));
    CREATE TABLE IF NOT EXISTS context_versions(project TEXT,lane TEXT,team TEXT,version INTEGER,body TEXT,updated TEXT,PRIMARY KEY(project,lane,team,version));
    CREATE TABLE IF NOT EXISTS review_scopes(review_id TEXT PRIMARY KEY,team TEXT,context_fingerprint TEXT);
    CREATE TABLE IF NOT EXISTS note_scopes(note_id TEXT PRIMARY KEY,team TEXT);
    CREATE TABLE IF NOT EXISTS write_keys(key TEXT PRIMARY KEY,result TEXT,payload_sha TEXT);
    CREATE TABLE IF NOT EXISTS record_scopes(id TEXT PRIMARY KEY,project TEXT,lane TEXT,team TEXT);
    INSERT OR IGNORE INTO record_scopes
      SELECT n.id,n.project,n.lane,CASE WHEN n.lane='' THEN '' ELSE COALESCE(s.team,'') END
      FROM notes n LEFT JOIN note_scopes s ON s.note_id=n.id;
    INSERT OR IGNORE INTO record_scopes
      SELECT r.id,r.project,r.lane,CASE WHEN r.lane='' THEN '' ELSE COALESCE(s.team,'') END
      FROM reviews r LEFT JOIN review_scopes s ON s.review_id=r.id;
    ''')
    for row in db.execute('SELECT project,lane,team FROM team_context').fetchall():
        key = 'context:' + digest('\0'.join(row))
        db.execute('INSERT OR IGNORE INTO record_scopes VALUES(?,?,?,?)', (key, *row))

def read_context(db, library, project, lane, team):
    record, revision = organization(library, project, lane, team)
    row = db.execute('SELECT * FROM team_context WHERE project=? AND lane=? AND team=?',
                     (project, lane, team)).fetchone()
    current = dict(row) if row else {'version': 0, 'body': {}, 'updated': None}
    if isinstance(current['body'], str):
        current['body'] = json.loads(current['body'])
    fingerprint = digest(encoded({'organization': record, 'contextVersion': current['version']}))
    reviews = [dict(r) for r in db.execute('''SELECT r.*,s.context_fingerprint FROM reviews r
        JOIN review_scopes s ON s.review_id=r.id
        WHERE r.project=? AND r.lane=? AND s.team=? ORDER BY r.created DESC LIMIT 10''',
        (project, lane, team))]
    for review in reviews:
        review['evidence'] = json.loads(review['evidence'])
        review['contextCurrent'] = review['context_fingerprint'] == fingerprint
    reviews=[r for r in reviews if library.readable_ids(r['evidence'])]
    return {
        'project': project, 'lane': lane, 'team': team, 'organization': record,
        'workflowRevision': revision, 'fingerprint': fingerprint, 'current': current, 'reviews': reviews,
        'libraryUrl': '/library?' + urlencode({'project': project, 'lane': lane, 'team': team}),
        'agentActivity': {'state': 'unmeasured', 'basis': 'Library records do not measure provider runtime.'},
        'services': {'context': 'Versioned questions, decisions, work and handoffs',
                     'research': 'Shared evidence search, source versions and library reviews'},
    }

def context(library, project, lane='', team=None):
    team = ('researcher' if lane else '') if team is None else team
    resolve_scope(library, project, lane, team)
    with connect(library.dbpath) as db:
        return read_context(db, library, project, lane, team)

def save(library, item):
    project, lane = item.get('project'), item.get('lane', '')
    team = item.get('team', 'researcher' if lane else '')
    resolve_scope(library, project, lane, team, writing=True)
    body = {k: str(item.get(k, '')).strip() for k in ('question', 'decisions', 'uncertainties', 'nextStep')}
    if not body['question'] or any(len(v) > 12000 for v in body.values()):
        raise ValueError('A current question is required; each field must be under 12000 characters')
    request_key, payload_sha = library.request_identity('context', item)
    with connect(library.dbpath) as db:
        db.execute('BEGIN IMMEDIATE')
        prior = library.prior_write(db, request_key, payload_sha, 'context', item)
        if prior:
            return prior
        # Scope may have changed while BEGIN IMMEDIATE waited on the index.
        # Exact retries above return their existing receipt without a new write.
        state, _, _, _ = resolve_scope(library, project, lane, team, writing=True)
        current = read_context(db, library, project, lane, team)
        if current['workflowRevision'] != state['revision']:
            raise ValueError('Workflow context changed. Reload before saving your revision.')
        version = current['current']['version']
        if type(item.get('version')) is not int or item['version'] != version:
            raise ValueError('Context changed. Reload before saving your revision.')
        if item.get('contextFingerprint') is not None and item['contextFingerprint'] != current['fingerprint']:
            raise ValueError('Workflow context changed. Reload before saving your revision.')
        raw, stamp = encoded(body), now()
        db.execute('INSERT OR REPLACE INTO team_context VALUES(?,?,?,?,?,?)',
                   (project, lane, team, version + 1, raw, stamp))
        db.execute('INSERT INTO context_versions VALUES(?,?,?,?,?,?)',
                   (project, lane, team, version + 1, raw, stamp))
        key, sha = 'context:' + digest('\0'.join((project, lane, team))), digest(raw)
        kind = 'Team context' if team else 'Workstream context' if lane else 'Project context'
        db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?)', (key, sha, raw, stamp))
        db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (key, project, lane, '', body['question'][:200], kind, sha, raw,
                    time.time(), len(raw), stamp, 'Recorded', 'Versioned library context'))
        db.execute('INSERT OR REPLACE INTO record_scopes VALUES(?,?,?,?)', (key, project, lane, team))
        db.execute('DELETE FROM search WHERE id=?', (key,))
        db.execute('INSERT INTO search VALUES(?,?,?)', (key, body['question'][:200], raw))
        result = {'ok': True, 'version': version + 1, 'id': key}
        library.record_write(db, request_key, payload_sha, result)
        # A failed final admission rolls back context, history and retry receipt.
        latest, _, _, _ = resolve_scope(library, project, lane, team, writing=True)
        if latest['revision'] != state['revision']:
            raise ValueError('Workflow context changed. Reload before saving your revision.')
        return result
