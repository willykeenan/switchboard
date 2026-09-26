"""Opt-in work handoffs. Delivery, pickup, return and sender acceptance stay distinct.

This never starts a provider, scans project folders or grants execution rights.
An explicit work contract shares the original message transaction. The existing
handoff daemon transports its request and one exact evidence return.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import time
import uuid

SCHEMA = 'ke.work-handoff.v1'
ROUTER_SENDER = 'service:router'
TABLE = '''CREATE TABLE IF NOT EXISTS workflow_work(
 message_id TEXT PRIMARY KEY, contract TEXT NOT NULL, created REAL NOT NULL,
 accepted REAL, returned REAL, result TEXT, return_message_id TEXT UNIQUE,
 closed REAL, closure TEXT)'''

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))

def bounded_text(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError('Bounded ' + name + ' required')
    return value

def routed_pair(flow, state, sender, recipient):
    """Explicit routes in one project, including its independent audit lane."""
    seats = {p['agentId']: p['laneId'] for p in state['placements']}
    notes = {n['agentId']: n['text'] for n in state.get('notes', [])}
    lanes = {l['id']: l for l in flow.workspace.read()['lanes']}
    selected = [lanes.get(seats.get(a)) for a in (sender, recipient)]
    return bool(all(selected) and selected[0]['projectId'] == selected[1]['projectId'] and
                all(l.get('lifecycle', 'active') == 'active' for l in selected) and
                all('[codex-route]' in notes.get(a, '') and '[/codex-route]' in notes.get(a, '')
                    for a in (sender, recipient)))

def prepare(flow, sender, recipient, contract):
    if not isinstance(contract, dict) or set(contract) != {
        'schema', 'assignmentId', 'projectId', 'laneId', 'resultPath', 'dueSeconds'}:
        raise ValueError('Use the exact work contract fields')
    if contract['schema'] != SCHEMA:
        raise ValueError('Unknown work contract')
    for key in ('assignmentId', 'projectId', 'laneId'):
        bounded_text(contract[key], key)
    if type(contract['dueSeconds']) is not int or not 60 <= contract['dueSeconds'] <= 86400:
        raise ValueError('Work deadline must be 60..86400 seconds')
    state = flow.read()
    from workflow import allowed
    if sender == ROUTER_SENDER:
        policy = router_policy(flow.root)
        if not state.get('enabled') or not policy or contract['projectId'] != policy.get('project') or not router_peer_in_project(flow, state, recipient, contract['projectId']):
            raise ValueError('Work handoff requires permitted request and return routes')
    elif not state.get('enabled') or not allowed(state, sender, recipient) or not allowed(state, recipient, sender):
        raise ValueError('Work handoff requires permitted request and return routes')
    workspace = flow.workspace.read()
    lane = next((l for l in workspace['lanes'] if l['id'] == contract['laneId']), None)
    if not lane or lane['projectId'] != contract['projectId'] or lane.get('lifecycle', 'active') != 'active':
        raise ValueError('Exact active project lane required')
    for actor in (sender, recipient):
        if actor == ROUTER_SENDER:
            continue
        seat = next((s for s in state['placements'] if s['agentId'] == actor), None)
        note = next((n['text'] for n in state.get('notes', []) if n['agentId'] == actor), '')
        actor_lane = next((l for l in workspace['lanes'] if seat and l['id'] == seat['laneId']), None)
        if not actor_lane or actor_lane['projectId'] != contract['projectId'] or actor_lane.get('lifecycle', 'active') != 'active' or (actor == sender and actor_lane['id'] != lane['id']) or '[codex-route]' not in note or '[/codex-route]' not in note:
            raise ValueError('Both tasks must explicitly opt into this flow; folder or automatic placement is insufficient')
    sessions = flow.identity_catalog()['sessions']
    target = next((s for s in sessions if s['agent_id'] == recipient), None)
    if not target or not target.get('cwd'):
        raise ValueError('Exact recipient workspace unavailable')
    result = Path(bounded_text(contract['resultPath'], 'result path', 4096))
    cwd = Path(target['cwd']).resolve()
    if not result.is_absolute() or '..' in result.parts or result.resolve() == cwd or cwd not in result.resolve().parents:
        raise ValueError('Result must be inside the exact recipient workspace')
    return dict(contract)

def record(db, mid, contract, at=None):
    db.execute(TABLE)
    previous = db.execute('SELECT contract FROM workflow_work WHERE message_id=?', (mid,)).fetchone()
    if previous and previous[0] != encoded(contract):
        raise ValueError('Original work contract changed; use a new authorized assignment')
    db.execute('INSERT OR IGNORE INTO workflow_work(message_id,contract,created) VALUES(?,?,?)',
               (mid, encoded(contract), time.time() if at is None else at))

def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_work'").fetchone())

def one(db, mid):
    if not exists(db):
        raise ValueError('No work contract for this message')
    row = db.execute('SELECT w.*,m.sender,m.recipient FROM workflow_work w JOIN workflow_messages m ON m.id=w.message_id WHERE w.message_id=?', (mid,)).fetchone()
    if not row:
        raise ValueError('No work contract for this original message')
    return dict(row)

def actor_check(flow, row, actor, sender=False):
    expected = row['sender'] if sender else row['recipient']
    if actor != expected:
        raise ValueError('Only the original exact actor may perform this transition')
    from workflow import allowed
    a, b = (row['sender'], row['recipient']) if sender else (row['recipient'], row['sender'])
    if row['sender'] == ROUTER_SENDER:
        contract = json.loads(row['contract']) if isinstance(row['contract'], str) else row['contract']
        if not router_peer_in_project(flow, flow.read(), row['recipient'], contract['projectId']):
            raise ValueError('Current directed route revoked; preserve work and evidence')
        return
    if not allowed(flow.read(), a, b):
        raise ValueError('Current directed route revoked; preserve work and evidence')

def accept(flow, actor, mid):
    with flow._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = one(db, mid); actor_check(flow, row, actor)
        if row['closed']:
            raise ValueError('Work is already closed')
        at = row['accepted'] or time.time()
        db.execute('UPDATE workflow_work SET accepted=? WHERE message_id=?', (at, mid))
    return {'messageId': mid, 'workState': 'ACCEPTED', 'acceptedAt': at, 'taskComplete': False}

def evidence(path, binding=None):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Result and parent paths must not be symlinks')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 1048576:
            raise ValueError('Use one nonempty regular result file up to 1 MiB')
        data = os.read(fd, 1048577)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or len(data) != before.st_size:
            raise ValueError('Result changed while reading')
        if binding is not None:
            parsed = json.loads(data)
            if not isinstance(parsed, dict) or parsed.get('workHandoff') != binding:
                raise ValueError('Result must bind this exact message, assignment and recipient')
        return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    finally:
        os.close(fd)

def finish(flow, actor, mid, summary, blocked=False, presentation=None):
    bounded_text(summary, 'result summary', 2000)
    from workflow_handoff_presentation import validate, lead, quoted
    if presentation is not None: presentation = validate(presentation)
    from workflow import now
    with flow._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = one(db, mid); actor_check(flow, row, actor)
        if not row['accepted']:
            raise ValueError('Accept the exact assignment before returning work')
        contract = json.loads(row['contract'])
        binding = {'messageId': mid, 'assignmentId': contract['assignmentId'], 'actor': actor}
        result = {'disposition': 'BLOCKED' if blocked else 'RESULT_REPORTED',
                  'summary': summary, 'evidence': evidence(contract['resultPath'], binding)}
        if presentation is not None: result['presentation'] = presentation
        if row['returned']:
            if row['result'] != encoded(result):
                raise ValueError('Return already recorded with different evidence; preserve the original')
            return {'messageId': mid, 'workState': result['disposition'], 'returnMessageId': row['return_message_id'], 'reused': True, 'taskComplete': False}
        if row['closed']:
            raise ValueError('Work is already closed')
        rid = str(uuid.uuid4())
        body = (lead(flow,actor,row['sender'],kind='return',result=result)+
                '\n\nTechnical details\nOriginal binding:\n'+quoted({'messageId':mid,'assignmentId':contract['assignmentId']})+
                '\nRecorded result:\n'+quoted(result)+'\nInspect the pinned result and take the next authorized step. '
                'Close this work handoff with workflowctl.py work-close --message-id ' + mid +
                ' --outcome accepted|revision|blocked --note REASON. This return is a claim, not verified task completion.')
        db.execute('INSERT INTO workflow_messages VALUES(?,?,?,?,?,?,NULL)',
                   (rid, actor, row['sender'], body, 'work-return:' + mid, now()))
        db.execute('INSERT INTO workflow_message_intents VALUES(?,?)', (rid, 'handoff'))
        db.execute('UPDATE workflow_work SET returned=?,result=?,return_message_id=? WHERE message_id=?',
                   (time.time(), encoded(result), rid, mid))
    flow.mirror_pending()
    return {'messageId': mid, 'workState': result['disposition'], 'returnMessageId': rid, 'taskComplete': False}

def close(flow, actor, mid, outcome, note):
    if outcome not in ('accepted', 'revision', 'blocked'):
        raise ValueError('Use accepted, revision or blocked')
    bounded_text(note, 'closure reason', 2000)
    with flow._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = one(db, mid); actor_check(flow, row, actor, sender=True)
        if not row['returned'] and outcome != 'blocked':
            raise ValueError('A turn end or read acknowledgment is not a work return')
        closure = {'outcome': outcome, 'note': note}
        previous = json.loads(row['closure']) if row['closed'] else None
        if previous and any(previous.get(k) != v for k, v in closure.items()):
            raise ValueError('Original closure is immutable')
        if outcome == 'accepted':
            if evidence(json.loads(row['contract'])['resultPath']) != json.loads(row['result'])['evidence']:
                raise ValueError('Returned evidence changed; cannot accept stale bytes')
            if json.loads(row['result'])['disposition'] == 'BLOCKED':
                raise ValueError('Blocked return cannot be accepted as completed work')
        elif not previous and not row['returned']:
            closure['evidenceCondition'] = 'no-return'
        elif not previous:
            try:
                observed = evidence(json.loads(row['contract'])['resultPath'])
                closure['evidenceCondition'] = 'unchanged' if observed == json.loads(row['result'])['evidence'] else 'changed'
                closure['observedEvidence'] = observed
            except (OSError, ValueError) as exc:
                closure['evidenceCondition'] = 'unavailable'
                closure['evidenceError'] = str(exc)[:600]
        value = row['closure'] if previous else encoded(closure)
        db.execute('UPDATE workflow_work SET closed=COALESCE(closed,?),closure=? WHERE message_id=?', (time.time(), value, mid))
    return {'messageId': mid, 'workState': outcome.upper(), 'taskComplete': False,
            'scope': 'Sender disposition of one handoff; project acceptance remains separate'}

def snapshot(flow, session=None, at=None):
    from workflow import workflow_reader
    if not flow.path.exists():
        return []
    with workflow_reader(flow.path) as db:
        db.row_factory = sqlite3.Row
        if not exists(db): return []
        query = 'SELECT w.*,m.sender,m.recipient FROM workflow_work w JOIN workflow_messages m ON m.id=w.message_id'
        args = ()
        if session:
            query += ' WHERE m.sender=? OR m.recipient=?'; args = (session, session)
        rows = [dict(r) for r in db.execute(query + " ORDER BY (w.closed IS NULL OR json_extract(w.closure,'$.outcome') != 'accepted') DESC,w.created DESC", args)]
    # Keep every unresolved/negative disposition visible; cap only accepted history.
    visible = []
    accepted_history = 0
    for row in rows:
        accepted_closure = bool(row['closed'] and json.loads(row['closure'])['outcome'] == 'accepted')
        if accepted_closure:
            accepted_history += 1
        if not accepted_closure or accepted_history <= 200:
            visible.append(row)
    rows = visible
    clock = time.time() if at is None else at
    for row in rows:
        row['contract'] = json.loads(row['contract'])
        row['result'] = json.loads(row['result']) if row['result'] else None
        row['closure'] = json.loads(row['closure']) if row['closure'] else None
        row['overdue'] = not row['closed'] and clock > row['created'] + row['contract']['dueSeconds']
        row['workState'] = (row['closure']['outcome'].upper() if row['closed'] else
                            row['result']['disposition'] if row['returned'] else
                            'ACCEPTED' if row['accepted'] else 'AWAITING_PICKUP')
        row['needsAttention'] = bool(row['overdue'] or (row['returned'] and not row['closed']) or (row['closure'] and row['closure']['outcome'] != 'accepted'))
        row['nextActor'] = row['sender'] if row['returned'] or row['overdue'] else row['recipient']
        row['taskComplete'] = False
    return rows

def envelope(flow, mid):
    from workflow import workflow_reader
    with workflow_reader(flow.path) as db:
        db.row_factory = sqlite3.Row
        if not exists(db): return ''
        if not db.execute('SELECT 1 FROM workflow_work WHERE message_id=?', (mid,)).fetchone(): return ''
        row = one(db, mid)
    contract = json.loads(row['contract'])
    return ('\nWORK HANDOFF ' + encoded(contract) +
            '\n1. Accept: workflowctl.py work-accept --message-id ' + mid +
            '\n2. Write your JSON result with workHandoff exactly ' + encoded({'messageId': mid, 'assignmentId': contract['assignmentId'], 'actor': row['recipient']}) +
            ' plus your substantive result (keep IDs, paths and hashes there).'
            '\n3. Return: workflowctl.py work-return --message-id ' + mid + ' --summary "one plain sentence" (add --blocked for a blocker; '
            'optional --presentation-file FILE, schema ke.handoff-presentation.v1: title, result, remaining, nextStep, availability).'
            '\nThe return goes once to the original sender. read-ack or ending your turn does not close this work. Existing authorizations and source custody are unchanged.')


ALERT_TABLE = """CREATE TABLE IF NOT EXISTS workflow_work_alerts(
 original_message_id TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL,
 body_sha256 TEXT NOT NULL, created REAL NOT NULL)"""
ALERT_SENDER = 'service:birds'

def router_policy(root):
    path = Path(root) / 'runtime' / 'flow-router-policy.json'
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get('enabled') is not True:
        return None
    project = data.get('project')
    if not isinstance(project, str) or not project.strip():
        return None
    return data

def router_peer_in_project(flow, state, agent_id, project_id):
    """True only for a placed agent in an active lane of the bound project."""
    if not project_id or agent_id in (ROUTER_SENDER, ALERT_SENDER):
        return False
    seat = next((p for p in state.get('placements') or [] if p['agentId'] == agent_id), None)
    if not seat:
        return False
    from workflow_layout import active
    lane = next((l for l in flow.workspace.read()['lanes'] if l['id'] == seat['laneId']), None)
    return bool(lane and lane.get('projectId') == project_id and active(lane))

def router_send_allowed(flow, state, recipient):
    policy = router_policy(flow.root)
    if not policy or not state.get('enabled'):
        return False
    return router_peer_in_project(flow, state, recipient, policy.get('project'))

def router_message_permission(flow, message, state=None):
    """Bound service:router sends only, and only inside the source item's project."""
    from workflow import workflow_reader
    state = state if state is not None else flow.read()
    path = Path(flow.root) / 'runtime' / 'flow-router.sqlite3'
    if not path.exists() or path.is_symlink():
        return {'allowed': False, 'resolved': False}
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='router_messages'").fetchone():
                return {'allowed': False, 'resolved': False}
            link = db.execute('SELECT * FROM router_messages WHERE new_message_id=?', (message['id'],)).fetchone()
    except sqlite3.Error:
        return {'allowed': False, 'resolved': False}
    if not link:
        return {'allowed': False, 'resolved': False}
    if message['recipient'] != link['recipient']:
        return {'allowed': False, 'resolved': False}
    if hashlib.sha256(message['body'].encode()).hexdigest() != link['body_sha256']:
        return {'allowed': False, 'resolved': False}
    project = None
    try:
        with workflow_reader(flow.path) as db:
            db.row_factory = sqlite3.Row
            if exists(db):
                row = db.execute('SELECT contract FROM workflow_work WHERE message_id=?', (link['source_message_id'],)).fetchone()
                if row:
                    project = json.loads(row['contract']).get('projectId')
    except (OSError, ValueError, sqlite3.Error, TypeError):
        project = None
    policy = router_policy(flow.root)
    if policy and project and policy.get('project') != project:
        return {'allowed': False, 'resolved': False}
    project = project or (policy or {}).get('project')
    permitted = bool(state.get('enabled') and project and router_peer_in_project(flow, state, message['recipient'], project))
    return {'allowed': permitted, 'resolved': False}

def alert_link(db, mid):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_work_alerts'").fetchone():
        return None
    row = db.execute('SELECT * FROM workflow_work_alerts WHERE message_id=?', (mid,)).fetchone()
    return dict(row) if row else None

def alert_permission(flow, message, state=None):
    """Only a service-created alert bound to an authorized original request."""
    from workflow import workflow_reader, allowed
    if message['sender'] == ROUTER_SENDER:
        return router_message_permission(flow, message, state)
    if message['sender'] != ALERT_SENDER:
        return None
    with workflow_reader(flow.path) as db:
        db.row_factory = sqlite3.Row
        link = alert_link(db, message['id'])
        if not link:
            return {'allowed': False, 'resolved': False}
        row = one(db, link['original_message_id'])
    if message['recipient'] != row['sender'] or hashlib.sha256(message['body'].encode()).hexdigest() != link['body_sha256']:
        return {'allowed': False, 'resolved': False}
    if row['closed']:
        return {'allowed': False, 'resolved': True}
    state = state if state is not None else flow.read()
    contract = json.loads(row['contract'])
    lanes = {l['id']:l for l in flow.workspace.read()['lanes']}
    seats = {s['agentId']:s['laneId'] for s in state['placements']}
    bound_project = all(lanes.get(seats.get(a),{}).get('projectId') == contract['projectId'] for a in (row['sender'],row['recipient']))
    permitted = bool(state.get('enabled') and bound_project and routed_pair(flow,state,row['sender'],row['recipient']) and
                     allowed(state,row['sender'],row['recipient']) and allowed(state,row['recipient'],row['sender']))
    return {'allowed':permitted,'resolved':False,'originalMessageId':row['message_id']}

def alert_overdue(flow, at=None):
    """One durable service alert per overdue obligation, never a work retry."""
    from workflow import now, allowed
    clock = time.time() if at is None else at
    state = flow.read()
    if not state.get('enabled'):
        return 0
    with flow._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not exists(db):
            return 0
        db.execute(ALERT_TABLE)
        db.execute('CREATE TABLE IF NOT EXISTS workflow_work_alert_cursor(singleton INTEGER PRIMARY KEY CHECK(singleton=1),created REAL NOT NULL,message_id TEXT NOT NULL)')
        cursor = db.execute('SELECT created,message_id FROM workflow_work_alert_cursor WHERE singleton=1').fetchone()
        after_time,after_id = tuple(cursor) if cursor else (0,'')
        rows = db.execute("SELECT w.*,m.sender,m.recipient FROM workflow_work w JOIN workflow_messages m ON m.id=w.message_id LEFT JOIN workflow_work_alerts a ON a.original_message_id=w.message_id WHERE w.closed IS NULL AND a.message_id IS NULL AND w.created + json_extract(w.contract,'$.dueSeconds') < ? AND (w.created>? OR (w.created=? AND w.message_id>?)) ORDER BY w.created,w.message_id LIMIT 20",(clock,after_time,after_time,after_id)).fetchall()
        # Advance over ineligible records too; otherwise a revoked prefix starves later work.
        scanned = (rows[-1]['created'],rows[-1]['message_id']) if rows else (0,'')
        db.execute('INSERT OR REPLACE INTO workflow_work_alert_cursor VALUES(1,?,?)',scanned)
        created = 0
        lanes = {l['id']:l for l in flow.workspace.read()['lanes']}
        seats = {s['agentId']:s['laneId'] for s in state['placements']}
        for raw in rows:
            row = dict(raw);contract = json.loads(row['contract'])
            bound_project = all(lanes.get(seats.get(a),{}).get('projectId') == contract['projectId'] for a in (row['sender'],row['recipient']))
            if not bound_project or not routed_pair(flow,state,row['sender'],row['recipient']) or not allowed(state,row['sender'],row['recipient']) or not allowed(state,row['recipient'],row['sender']):
                continue
            mid = str(uuid.uuid4())
            from workflow_handoff_presentation import lead, quoted
            body = (lead(flow,ALERT_SENDER,row['sender'],kind='overdue')+'\n\nTechnical details\n'
                    'SYSTEM-GENERATED overdue work alert from Birds; this is not a recipient response.\n'
                    'Original binding:\n'+quoted({'messageId':row['message_id'],'assignmentId':contract['assignmentId'],'recipient':row['recipient']})+'\nThe contracted deadline passed without sender disposition. '
                    'Inspect the original obligation, recipient activity and any returned evidence. Resolve the concrete dependency or record the blocker within existing authority. '
                    'Do not automatically repeat the assignment or an uncertain delivery. This alert grants no new execution, spend, source or installation authority. '
                    'One alert is recorded for this original request; no repeated model polling.')
            db.execute('INSERT INTO workflow_messages VALUES(?,?,?,?,?,?,NULL)',(mid,ALERT_SENDER,row['sender'],body,'work-deadline:'+row['message_id'],now()))
            db.execute('INSERT INTO workflow_message_intents VALUES(?,?)',(mid,'handoff'))
            db.execute('INSERT INTO workflow_work_alerts VALUES(?,?,?,?)',(row['message_id'],mid,hashlib.sha256(body.encode()).hexdigest(),clock))
            created += 1
    if created:
        flow.mirror_pending()
    return created
