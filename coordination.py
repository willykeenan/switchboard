"""Coordination v2: local state, scoped retrieval and recoverable delivery.

No model calls, shell execution, automatic ownership transfer or authority grants.
Legacy room history stays authoritative for message bytes and sequence numbers.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS coordination_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, objective TEXT NOT NULL,
 scope_json TEXT NOT NULL, dependencies_json TEXT NOT NULL, criteria_json TEXT NOT NULL,
 aliases_json TEXT NOT NULL, status TEXT NOT NULL, next_action TEXT NOT NULL,
 blocker TEXT NOT NULL, progress_json TEXT NOT NULL, version INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, version INTEGER NOT NULL,
 created_at TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_proofs(
 task_id TEXT NOT NULL, criterion TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 verified_at TEXT NOT NULL, PRIMARY KEY(task_id,criterion));
CREATE TABLE IF NOT EXISTS room_outbox(
 id TEXT PRIMARY KEY, body TEXT NOT NULL, recipients TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'QUEUED', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, receipt TEXT, last_error TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handoffs(
 incident_id TEXT PRIMARY KEY, owner TEXT NOT NULL, stage TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, due_at REAL NOT NULL, pid INTEGER,
 detail TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS route_observations(incident_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, next_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS incident_links(
 incident_id TEXT PRIMARY KEY REFERENCES incidents(incident_id), canonical_id TEXT NOT NULL REFERENCES incidents(incident_id),
 reason TEXT NOT NULL, actor TEXT NOT NULL, linked_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS incident_links_canonical ON incident_links(canonical_id);
CREATE TABLE IF NOT EXISTS monitor_observations(
 monitor_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL, record_hash TEXT NOT NULL,
 observations INTEGER NOT NULL, last_observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS room_messages(
 seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, author TEXT NOT NULL,
 recipients_json TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
 created_at TEXT NOT NULL, reply_to TEXT);
CREATE TABLE IF NOT EXISTS reader_cursors(endpoint TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS briefing_metrics(id INTEGER PRIMARY KEY AUTOINCREMENT,
 created_at TEXT NOT NULL, endpoint TEXT NOT NULL, bytes INTEGER NOT NULL, elapsed_ms REAL NOT NULL);
INSERT OR IGNORE INTO coordination_meta VALUES('schema_version','2');
"""


def core():
    import board_core
    return board_core


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def migrate_legacy(connection):
    if not connection.in_transaction:
        connection.execute('BEGIN IMMEDIATE')
    columns={r[1] for r in connection.execute('PRAGMA table_info(room_outbox)')}
    for name in ['event_type','incident_id']:
        if name not in columns:
            connection.execute('ALTER TABLE room_outbox ADD COLUMN '+name+' TEXT')
    incident_columns={r[1] for r in connection.execute('PRAGMA table_info(incidents)')}
    if 'requested_owner' not in incident_columns:
        connection.execute('ALTER TABLE incidents ADD COLUMN requested_owner TEXT')
    if 'needs_operator' not in incident_columns:
        # Databases created by earlier builds used an operator-specific column name.
        legacy=[c for c in incident_columns if c.endswith('_needed')]
        if legacy:connection.execute('ALTER TABLE incidents RENAME COLUMN '+legacy[0]+' TO needs_operator')
        else:connection.execute('ALTER TABLE incidents ADD COLUMN needs_operator INTEGER NOT NULL DEFAULT 0')
    if connection.execute("SELECT 1 FROM coordination_meta WHERE key='legacy_handoffs_imported'").fetchone():
        return
    connection.execute("""INSERT OR IGNORE INTO handoffs(incident_id,owner,stage,attempts,due_at,detail,updated_at)
        SELECT incident_id,assigned_agent_id,
        CASE WHEN status='ACKNOWLEDGED' THEN 'ACKNOWLEDGED' ELSE 'LEGACY_UNVERIFIED' END,
        wake_attempts,0,'Imported existing ownership; no delivery or new wake inferred',updated_at
        FROM incidents WHERE assigned_agent_id IS NOT NULL AND status!='RESOLVED'""")
    connection.execute("INSERT OR IGNORE INTO coordination_meta VALUES('legacy_handoffs_imported','true')")


def visible_incidents_sql():
    return 'incident_id NOT IN (SELECT incident_id FROM incident_links)'


def incident_annotations(connection, incident):
    link=connection.execute('SELECT canonical_id FROM incident_links WHERE incident_id=?',(incident['incident_id'],)).fetchone()
    incident['linked_to']=link[0] if link else None
    incident['linked_reports']=connection.execute('SELECT count(*) FROM incident_links WHERE canonical_id=?',(incident['incident_id'],)).fetchone()[0]
    return incident


def consolidate_incidents(source, apply=False, actor='coordinator'):
    """Link exact unassigned OPEN monitor repeats; never change incident history/status."""
    if not source.startswith('monitor:'):
        raise ValueError('an exact monitor source is required')
    b=core();b.init_db()
    with b.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute("SELECT * FROM incidents WHERE source=? AND status='OPEN' AND assigned_agent_id IS NULL AND wake_attempts=0 AND "+visible_incidents_sql()+" ORDER BY created_at,incident_id",(source,)).fetchall()
        groups={}
        for r in rows:
            # Different details, authority, severity or named owners remain distinct.
            key=tuple(r[k] for k in ('team','title','details','source','safe_action','required_capability','severity','needs_operator','requested_owner'))
            groups.setdefault(key,[]).append(r['incident_id'])
        links=[{'incident_id':i,'canonical_id':ids[0]} for ids in groups.values() for i in ids[1:]]
        if apply:
            c.executemany('INSERT INTO incident_links VALUES(?,?,?,?,?)',[(r['incident_id'],r['canonical_id'],'Exact repeated unassigned monitor condition; underlying failure unresolved',actor,b.utc_now()) for r in links])
            if links:b.event(c,'INCIDENT_REPORTS_LINKED',actor=actor,payload={'source':source,'linked':len(links),'canonical_ids':sorted({r['canonical_id'] for r in links}),'resolved':0})
        return {'source':source,'applied':apply,'examined':len(rows),'distinct_conditions':len(groups),'linked':len(links),'links':links,'resolved':0,'history_deleted':0}


def reconcile_placeholder(incident_id, expected_owner, owner, reason, evidence, evidence_sha256, actor):
    """Correct a proven alias misroute; no retry or launch is performed."""
    b=core();b.init_db()
    path=Path(evidence)
    if not path.is_absolute() or not reason.strip() or hashlib.sha256(path.read_bytes()).hexdigest()!=evidence_sha256:
        raise ValueError('exact evidence hash and reason required')
    with b.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        i=c.execute('SELECT * FROM incidents WHERE incident_id=?',(incident_id,)).fetchone()
        old=c.execute('SELECT * FROM agents WHERE agent_id=?',(expected_owner,)).fetchone()
        new=c.execute('SELECT * FROM agents WHERE agent_id=?',(owner,)).fetchone()
        handoff=c.execute('SELECT * FROM handoffs WHERE incident_id=?',(incident_id,)).fetchone()
        if not i or i['assigned_agent_id']!=expected_owner or not old or old['provider']!='room':
            raise ValueError('expected alias ownership does not match')
        if i['status']!='ASSIGNED' or i['wake_status']!='ROOM_PING_ONLY' or (handoff and handoff['pid'] is not None):
            raise ValueError('only an unacknowledged room-only alias misroute is eligible')
        if not new or new['provider'] not in ('codex','claude') or new['status'] in ('DISABLED','OFFLINE'):
            raise ValueError('registered available exact owner required')
        hints=b._owner_hints(i)
        if not hints or any(not b._owner_matches(new,h) for h in hints):
            raise ValueError('requested owner does not match the recorded exact-owner constraint')
        now=b.utc_now()
        c.execute("UPDATE incidents SET assigned_agent_id=?,requested_owner=?,updated_at=? WHERE incident_id=?",(owner,owner,now,incident_id))
        c.execute("UPDATE handoffs SET owner=?,stage='QUEUED',due_at=?,detail=?,updated_at=? WHERE incident_id=?",(owner,time.time()+180,'Alias misroute corrected; room delivery only, no model launch',now,incident_id))
        b.event(c,'INCIDENT_OWNER_RECONCILED',actor=actor,incident_id=incident_id,team=i['team'],payload={'previous_owner':expected_owner,'owner':owner,'reason':reason,'evidence':str(path),'sha256':evidence_sha256,'previous_handoff':dict(handoff) if handoff else None,'new_launches':0})
        b.event(c,'INCIDENT_ASSIGNED',actor=actor,incident_id=incident_id,team=i['team'],payload={'agent_id':owner,'reason':'Corrected alias misroute; retained existing task owner; no launch'})
    return b.get_incident(incident_id)


def presence(agent):
    b = core()
    age = max(0, time.time() - b._parse_time(agent['last_seen_at']))
    return {**agent, 'reported_status': agent['status'],
            'status': 'UNKNOWN' if agent['status'] == 'ACTIVE' and age > b.ACTIVE_HEARTBEAT_SECONDS else agent['status'],
            'heartbeat_age_seconds': round(age), 'presence_is_ownership': False}


def enqueue_event(connection, event_id, event_type, incident_id, team, payload):
    if event_type not in ('INCIDENT_OPENED', 'INCIDENT_ASSIGNED', 'WAKE_ATTEMPT_FINISHED', 'INCIDENT_ACKNOWLEDGED',
                          'INCIDENT_RESOLVED', 'HANDOFF_ESCALATED'):
        return
    incident = connection.execute('SELECT * FROM incidents WHERE incident_id=?', (incident_id,)).fetchone()
    if not incident:
        return
    owner = incident['assigned_agent_id']
    recipients = 'codex,claude,operator'
    if owner:
        a = connection.execute('SELECT provider,endpoint FROM agents WHERE agent_id=?', (owner,)).fetchone()
        if a:
            recipients = ','.join([a['provider'], a['endpoint'], owner])
    body = f"[{team}] {event_type} {incident_id}: {incident['title']}. {incident['details']} Next: {incident['safe_action']}. Owner: {owner or 'unassigned'}. {encode(payload)}"
    connection.execute('INSERT OR IGNORE INTO room_outbox(id,body,recipients,kind,created_at,event_type,incident_id) VALUES(?,?,?,?,?,?,?)',
                       (event_id, body, recipients, 'system', core().utc_now(),event_type,incident_id))


def flush_outbox(limit=20):
    import subprocess
    b = core()
    b.init_db()
    if os.environ.get('SWITCHBOARD_DISABLE_ROOM') == '1':
        return []
    results = []
    for _ in range(limit):
        with b.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM room_outbox WHERE status IN ('QUEUED','SENDING') AND next_at<=? ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if not row:
                break
            c.execute("UPDATE room_outbox SET status='SENDING',attempts=attempts+1,next_at=? WHERE id=?", (time.time()+60, row['id']))
        try:
            external_writer = str(b.ROOM_WRITER or '')
            if external_writer not in ('', '.'):
                # Optional external room writer (SWITCHBOARD_ROOM_WRITER).
                run = subprocess.run([sys.executable, external_writer, 'post', '--author', 'agent-board',
                                      '--to', row['recipients'], '--kind', row['kind'], '--idempotency-key', row['id']],
                                     input=row['body'], text=True, capture_output=True, timeout=20)
                if run.returncode:
                    raise RuntimeError(run.stderr[-500:])
                receipt = json.loads(run.stdout)
            else:
                from room_writer import post as write_room
                from store import rooms_root
                receipt = write_room(rooms_root(b.ROOT) / 'global', 'agent-board', row['body'],
                                     [r for r in row['recipients'].split(',') if r], row['kind'], None, row['id'])
            if not receipt.get('id') or not receipt.get('seq'):
                raise ValueError('missing room acknowledgement')
            with b.connect() as c:
                c.execute("UPDATE room_outbox SET status='DELIVERED',receipt=?,last_error=NULL WHERE id=?", (encode(receipt), row['id']))
                if row['event_type']=='INCIDENT_ASSIGNED':
                    c.execute("UPDATE handoffs SET stage='DELIVERED',updated_at=? WHERE incident_id=? AND stage='QUEUED'", (b.utc_now(),row['incident_id']))
            results.append({'id': row['id'], 'status': 'DELIVERED'})
        except Exception as exc:
            attempts = row['attempts'] + 1
            status = 'ESCALATED' if attempts >= 5 else 'QUEUED'
            with b.connect() as c:
                c.execute('UPDATE room_outbox SET status=?,next_at=?,last_error=? WHERE id=?',
                          (status, time.time()+min(300, 5*2**attempts), str(exc)[:500], row['id']))
            results.append({'id': row['id'], 'status': status})
    return results


def sync_room(path=None):
    b = core()
    b.init_db()
    from store import rooms_root
    path = Path(path or rooms_root(b.ROOT) / 'global/messages.jsonl')
    if not path.exists():
        return 0
    # SQLite serializes the cursor and imported rows; incomplete tail is retried.
    with b.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT value FROM coordination_meta WHERE key='room_offset'").fetchone()
        offset = int(row[0]) if row else 0
        if path.stat().st_size < offset:
            raise ValueError('room history shrank; manual reconciliation required')
        n = 0
        with path.open('rb') as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line or not line.endswith(b'\n'):
                    break
                r = json.loads(line)
                c.execute('INSERT OR IGNORE INTO room_messages VALUES(?,?,?,?,?,?,?,?)',
                          (r['seq'], r['id'], r['author'], encode(r.get('recipients', [])), r['kind'], r['body'], r['createdAt'], r.get('replyTo')))
                offset = f.tell()
                n += 1
        c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('room_offset',?)", (str(offset),))
    return n


def decode_task(row):
    if not row:
        raise KeyError('task not registered')
    d = dict(row)
    for k in ['scope', 'dependencies', 'criteria', 'aliases', 'progress']:
        d[k] = json.loads(d.pop(k+'_json'))
    return d


def task_get(task_id):
    b = core(); b.init_db()
    with b.connect() as c:
        return decode_task(c.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())


def task_register(data):
    b = core(); b.init_db()
    for key in ['task_id', 'owner', 'objective', 'next_action']:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(key+' required')
    criteria = data.get('criteria', [])
    if not criteria or len({r['id'] for r in criteria}) != len(criteria):
        raise ValueError('unique acceptance criteria required')
    for r in criteria:
        if r.get('kind') not in ('artifact', 'json', 'human') or not r.get('description'):
            raise ValueError('criterion kind and description required')
        if r['kind'] != 'human' and (not Path(r.get('path','')).is_absolute() or not r.get('path')):
            raise ValueError('criterion requires an exact absolute artifact path')
        if r['kind'] == 'json' and (not r.get('pointer') or 'equals' not in r):
            raise ValueError('json criterion requires pointer and expected value')
    scopes = [str(Path(p).expanduser().resolve()) for p in data.get('scope', [])]
    dependencies = data.get('dependencies', [])
    with b.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT * FROM tasks WHERE task_id=?', (data['task_id'],)).fetchone()
        if existing:
            task = decode_task(existing)
            if task['owner'] == data['owner'] and task['objective'] == data['objective'] and task['scope'] == scopes and task['criteria'] == criteria and task['dependencies'] == dependencies:
                return task
            raise ValueError('task already registered; objective, owner and contract cannot be overwritten')
        if not c.execute("SELECT 1 FROM agents WHERE agent_id=? AND provider IN ('codex','claude')", (data['owner'],)).fetchone():
            raise ValueError('exact owner must be registered first')
        for dep in dependencies:
            if dep == data['task_id'] or not c.execute('SELECT 1 FROM tasks WHERE task_id=?', (dep,)).fetchone():
                raise ValueError('dependency must be an existing distinct task')
        for other in c.execute("SELECT task_id,scope_json FROM tasks WHERE status NOT IN ('VERIFIED_COMPLETE','CANCELLED')"):
            for p in scopes:
                for q in json.loads(other['scope_json']):
                    if Path(p) == Path(q) or Path(p) in Path(q).parents or Path(q) in Path(p).parents:
                        raise ValueError('scope owned by '+other['task_id'])
        now = b.utc_now()
        c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (data['task_id'], data['owner'], data['objective'], encode(scopes), encode(dependencies), encode(criteria), encode(data.get('aliases', [])),
                   'QUEUED', data['next_action'], '', '{}', 1, now, now))
        c.execute('INSERT INTO task_events(task_id,version,created_at,actor,payload_json) VALUES(?,?,?,?,?)',
                  (data['task_id'],1,now,data['owner'],encode({'event':'REGISTERED','contract':data})))
    return task_get(data['task_id'])


def task_update(task_id, owner, version, changes):
    b = core(); b.init_db()
    if set(changes) - {'status', 'next_action', 'blocker', 'progress'}:
        raise ValueError('immutable or unsupported field')
    allowed = {'QUEUED','ACKNOWLEDGED','WORKING','BLOCKED','REPORTED_DONE','CANCELLED'}
    with b.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = decode_task(c.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
        if old['owner'] != owner or old['version'] != version:
            raise ValueError('owner or version conflict')
        if old['status'] in ('VERIFIED_COMPLETE','CANCELLED'):
            raise ValueError('terminal task is immutable')
        new = {**old, **changes}
        if new['status'] not in allowed:
            raise ValueError('verified completion requires task-verify')
        if new['status'] in ('WORKING','REPORTED_DONE'):
            for dep in old['dependencies']:
                if c.execute('SELECT status FROM tasks WHERE task_id=?',(dep,)).fetchone()[0] != 'VERIFIED_COMPLETE':
                    raise ValueError('dependency not verified: '+dep)
        if new['status'] == 'BLOCKED' and not new['blocker'].strip():
            raise ValueError('blocker required')
        now = b.utc_now()
        c.execute('UPDATE tasks SET status=?,next_action=?,blocker=?,progress_json=?,version=version+1,updated_at=? WHERE task_id=?',
                  (new['status'],new['next_action'],new['blocker'],encode(new['progress']),now,task_id))
        c.execute('INSERT INTO task_events(task_id,version,created_at,actor,payload_json) VALUES(?,?,?,?,?)', (task_id,version+1,now,owner,encode(changes)))
    return task_get(task_id)


def verify_task(task_id, owner, version):
    b = core(); task = task_get(task_id)
    if task['owner'] != owner or task['version'] != version or task['status'] != 'REPORTED_DONE':
        raise ValueError('exact owner/version and REPORTED_DONE required')
    proofs = []
    for criterion in task['criteria']:
        if criterion['kind'] == 'human':
            raise ValueError('human acceptance remains pending: '+criterion['id'])
        p = Path(criterion['path']); raw = p.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if criterion.get('sha256') and criterion['sha256'] != digest:
            raise ValueError('artifact hash mismatch: '+criterion['id'])
        if criterion['kind'] == 'json':
            value = json.loads(raw)
            for part in criterion['pointer'].strip('/').split('/'):
                key = part.replace('~1','/').replace('~0','~')
                value = value[int(key)] if isinstance(value,list) else value[key]
            if type(value) != type(criterion['equals']) or value != criterion['equals']:
                raise ValueError('criterion failed: '+criterion['id'])
        proofs.append((task_id,criterion['id'],str(p),digest,b.utc_now()))
    with b.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        changed = c.execute("UPDATE tasks SET status='VERIFIED_COMPLETE',version=version+1,updated_at=? WHERE task_id=? AND owner=? AND version=? AND status='REPORTED_DONE'", (b.utc_now(),task_id,owner,version)).rowcount
        if changed != 1:
            raise ValueError('task changed while verifying')
        c.executemany('INSERT OR REPLACE INTO task_proofs VALUES(?,?,?,?,?)',proofs)
        c.execute('INSERT INTO task_events(task_id,version,created_at,actor,payload_json) VALUES(?,?,?,?,?)', (task_id,version+1,b.utc_now(),'deterministic-verifier',encode({'proofs':proofs})))
    return task_get(task_id)


def brief(endpoint, team=None, limit=8, after=None, aliases=()):
    b = core(); start = time.monotonic(); b.init_db()
    sync_error=None
    try:sync_room()
    except (OSError, ValueError, sqlite3.Error) as exc:sync_error=str(exc)
    limit = max(1,min(25,limit))
    with b.connect() as c:
        agents = [presence(b.row_to_agent(r)) for r in c.execute('SELECT * FROM agents WHERE endpoint=?',(endpoint,))]
        ids = [a['agent_id'] for a in agents]
        names = set([endpoint, *ids, *aliases, *[a['provider'] for a in agents], 'all'])
        tasks = [decode_task(r) for r in c.execute('SELECT * FROM tasks WHERE owner IN (SELECT agent_id FROM agents WHERE endpoint=?) ORDER BY updated_at DESC LIMIT 9',(endpoint,))]
        for task in tasks: names.update(task['aliases'])
        owned = [dict(r) for r in c.execute("SELECT * FROM incidents WHERE status!='RESOLVED' AND incident_id NOT IN (SELECT incident_id FROM incident_links) AND assigned_agent_id IN (SELECT agent_id FROM agents WHERE endpoint=?) ORDER BY created_at DESC LIMIT 9",(endpoint,))]
        unassigned = [dict(r) for r in c.execute("SELECT * FROM incidents WHERE status='OPEN' AND incident_id NOT IN (SELECT incident_id FROM incident_links) AND assigned_agent_id IS NULL AND team=? ORDER BY created_at DESC LIMIT 5",(team or '',))]
        incidents = [incident_annotations(c,i) for i in owned[:8]+unassigned[:4]]
        cursor = c.execute('SELECT seq FROM reader_cursors WHERE endpoint=?',(endpoint,)).fetchone()
        start_seq = after if after is not None else (cursor[0] if cursor else 0)
        bootstrap = after is None and cursor is None
        messages = []
        order = 'DESC' if bootstrap else 'ASC'
        for row in c.execute(f'SELECT * FROM room_messages WHERE seq>? ORDER BY seq {order}',(start_seq,)):
            if not names.intersection(json.loads(row['recipients_json'])):
                continue
            r = dict(row); r['recipients'] = json.loads(r.pop('recipients_json'))
            r['body_truncated'] = len(r['body']) > 1400
            r['body'] = r['body'][:1400]
            messages.append(r)
            if len(messages) > limit: break
        more = len(messages)>limit
        messages = sorted(messages[:limit], key=lambda x:x['seq'])
        seq = max([start_seq]+[r['seq'] for r in messages])
        c.execute('INSERT OR REPLACE INTO coordination_meta VALUES(?,?)',('offered:'+endpoint,str(seq)))
        deliveries = [dict(r) for r in c.execute('SELECT * FROM handoffs WHERE owner IN (SELECT agent_id FROM agents WHERE endpoint=?) ORDER BY updated_at DESC LIMIT 25',(endpoint,))]
        for incident in incidents:
            for field in ['title','details','safe_action','source']:
                original=incident.get(field,'')
                if len(original)>1000:
                    incident[field]=original[:1000]
                    incident[field+'_truncated']=True
        for task in tasks:
            task['objective']=task['objective'][:1000]
            task['next_action']=task['next_action'][:1000]
            task['criteria']=[{'id':r['id'],'kind':r['kind']} for r in task['criteria']]
            task['detail_command']='boardctl.py task-get --task-id '+task['task_id']
            task['progress_basis']='owner-reported; completion is checked separately against registered artifacts'
        result = {'contract':'ke.coordination.brief.v2','endpoint':endpoint,'generated_at':b.utc_now(),
                  'sync_error':sync_error,'room_freshness':'STALE_INDEX' if sync_error else 'CURRENT',
                  'agents':agents,'tasks':tasks[:8],'tasks_more':len(tasks)>8,'incidents':incidents, 'incidents_more':len(owned)>8 or len(unassigned)>4,
                  'messages':messages,'messages_more':more,'bootstrap_recent_only':bootstrap,
                  'cursor':{'saved':start_seq,'offered':seq,'requires_explicit_ack':True},'deliveries':deliveries,
                  'events':[{'seq':r['seq'],'kind':r['kind'],'author':r['author']} for r in messages],
                  'monitors':[],'teams':[{'name':n} for n in sorted({a['team'] for a in agents})],
                  'history':'First run: brief --after 0 for all unread history. Open /rooms in the board UI for full bodies; boardctl.py status for full inventory',
                  'overview':str(b.ROOT/'CURRENT.md')}
        c.execute('INSERT INTO briefing_metrics(created_at,endpoint,bytes,elapsed_ms) VALUES(?,?,?,?)', (b.utc_now(),endpoint,len(encode(result).encode()),round((time.monotonic()-start)*1000,2)))
    from workspace import Workspace
    from room_reader import RoomStore
    try:
        from store import rooms_root
        result['workspace'] = Workspace(b.ROOT, RoomStore(rooms_root(b.ROOT))).context(endpoint)
    except (OSError, sqlite3.Error, ValueError) as exc:
        result['workspace'] = {'error':str(exc),'note':'Project context unavailable; existing task ownership still applies.'}
    from workflow import Workflow, manual_routing
    if manual_routing(b.ROOT):
        result['messages']=[];result['events']=[];result['messages_more']=False
        result['workspace'].pop('recentLaneMessages',None)
        result['routingNote']='The operator controls the constellation. Unscoped legacy room posts are observation only; permitted requests are in workflow.inbox. Never interrupt or resume another live task.'
        from store import rooms_root
        try:result['workflow']=Workflow(b.ROOT,Workspace(b.ROOT,RoomStore(rooms_root(b.ROOT)))).context(endpoint)
        except Exception as exc:result['workflow']={'enabled':True,'error':str(exc),'inbox':[],'mayMessage':[],'note':'Routing held until saved workflow can be read.'}
    import current_context
    result['currentContext'] = current_context.project_for_endpoint(b.ROOT, endpoint)
    result['tasks'] = current_context.annotate_tasks(result['tasks'], result['currentContext'])
    return result


def cursor_ack(endpoint, through):
    b=core(); b.init_db()
    with b.connect() as c:
        offered=c.execute('SELECT value FROM coordination_meta WHERE key=?',('offered:'+endpoint,)).fetchone()
        maximum=int(offered[0]) if offered else 0
        if through < 0 or through > maximum: raise ValueError('cursor outside indexed history')
        c.execute('INSERT INTO reader_cursors VALUES(?,?) ON CONFLICT(endpoint) DO UPDATE SET seq=max(seq,excluded.seq)', (endpoint,through))
    return {'endpoint':endpoint,'acknowledged_through':through}


def handoff_tick():
    """Reconcile delivery without blindly replaying an uncertain process launch."""
    b=core(); b.init_db(); results=[]
    with b.connect() as c:
        rows=c.execute("SELECT h.*,i.status AS incident_status FROM handoffs h JOIN incidents i USING(incident_id) WHERE h.stage NOT IN ('ACKNOWLEDGED','RESOLVED','ESCALATED','LEGACY_UNVERIFIED') AND h.due_at<=?",(time.time(),)).fetchall()
    for row in rows:
        stage = 'ACKNOWLEDGED' if row['incident_status']=='ACKNOWLEDGED' else ('RESOLVED' if row['incident_status']=='RESOLVED' else 'ESCALATED')
        # A process may have started before a crash. Missing ACK is never permission
        # for a second model turn. The persisted incident surfaces the uncertainty.
        with b.connect() as c:
            c.execute('UPDATE handoffs SET stage=?,detail=?,updated_at=? WHERE incident_id=?', (stage,'acknowledgement deadline exceeded; reconcile exact owner before another wake' if stage=='ESCALATED' else 'owner acknowledged',b.utc_now(),row['incident_id']))
            if stage=='ESCALATED':
                b.event(c,'HANDOFF_ESCALATED',actor='coordinator',incident_id=row['incident_id'],payload={'owner':row['owner'],'reason':'missing acknowledgement; no automatic second launch'})
        results.append({'incident_id':row['incident_id'],'stage':stage})
    return results


def health():
    b=core(); b.init_db()
    with b.connect() as c:
        meta=dict(c.execute('SELECT key,value FROM coordination_meta'))
        tick=float(meta.get('last_tick','0')); age=round(time.time()-tick,1)
        return {'contract':'ke.coordination.health.v2','ok':age<30,'schema_version':meta['schema_version'],
                'last_tick_age_seconds':age,'last_error':meta.get('last_error',''),
                'outbox':dict(c.execute('SELECT status,count(*) FROM room_outbox GROUP BY status')),
                'handoffs':dict(c.execute('SELECT stage,count(*) FROM handoffs GROUP BY stage')),
                'tasks':dict(c.execute('SELECT status,count(*) FROM tasks GROUP BY status')),
                'ownership_policy':'never transfer on stale presence','history_preserved':True}


def dashboard():
    b=core();b.init_db()
    with b.connect() as c:
        agents=[presence(b.row_to_agent(r)) for r in c.execute('SELECT * FROM agents ORDER BY last_seen_at DESC LIMIT 40')]
        incidents=[incident_annotations(c,dict(r)) for r in c.execute("SELECT * FROM incidents WHERE status!='RESOLVED' AND incident_id NOT IN (SELECT incident_id FROM incident_links) ORDER BY (assigned_agent_id IS NOT NULL) DESC,updated_at DESC LIMIT 40")]
        counts=dict(c.execute("SELECT status,count(*) FROM incidents WHERE incident_id NOT IN (SELECT incident_id FROM incident_links) GROUP BY status"))
        for i in incidents:
            for key in ['title','details','safe_action']:i[key]=i[key][:1200]
        tasks=[decode_task(r) for r in c.execute('SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 20')]
        events=[dict(r) for r in c.execute('SELECT sequence,event_type,created_at,team,actor FROM events ORDER BY sequence DESC LIMIT 25')]
        return {'generated_at':b.utc_now(),'agents':agents,'agent_total':c.execute('SELECT count(*) FROM agents').fetchone()[0],
                'incidents':incidents,'events':events,'teams':[dict(r) for r in c.execute('SELECT * FROM teams')],
                'history':{'total_reports':c.execute('SELECT count(*) FROM incidents').fetchone()[0],'linked_reports':c.execute('SELECT count(*) FROM incident_links').fetchone()[0]},
                'counts':{'open':sum(v for k,v in counts.items() if k!='RESOLVED'),'working':counts.get('ACKNOWLEDGED',0),'needs_operator':counts.get('BLOCKED_HUMAN',0)},
                'tasks':[{'task_id':t['task_id'],'owner':t['owner'],'objective':t['objective'][:600],'status':t['status'],'next_action':t['next_action'][:600]} for t in tasks],
                'coordination':health(),'view_note':'Showing up to 40 distinct incidents and agents. Exact repeat reports are linked; all original records remain in full history.'}


def generate_overview():
    b=core(); b.init_db()
    with b.connect() as c:
        tasks=[decode_task(r) for r in c.execute("SELECT * FROM tasks WHERE status NOT IN ('VERIFIED_COMPLETE','CANCELLED') ORDER BY updated_at DESC LIMIT 30")]
        counts=dict(c.execute("SELECT status,count(*) FROM incidents WHERE status!='RESOLVED' AND incident_id NOT IN (SELECT incident_id FROM incident_links) GROUP BY status"))
    lines=['# Current coordination','Generated from structured board records. Do not edit this view.',
           'Use boardctl.py brief --endpoint YOUR_SESSION for scoped messages, ownership and next actions.',
           '', 'Open incident counts: '+encode(counts), '', '## Registered tasks']
    for t in tasks:
        lines.extend(['',f"- **{t['task_id']}** — {t['status']} · owner `{t['owner']}`",f"  {t['objective'][:500]}",f"  Next: {t['next_action'][:500]}"])
    if not tasks:lines.append('No v2 task contracts registered yet. Existing owners remain in the legacy board; no transfer is implied.')
    b.ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=b.ROOT/'CURRENT.md'; content='\n'.join(lines)+'\n'
    old=target.read_text() if target.exists() else ''
    old_body=old.split('\n',1)[1] if old.startswith('<!-- Generated at ') else old
    if old_body!=content:
        temp=target.with_name('.CURRENT.'+uuid.uuid4().hex+'.tmp');temp.write_text('<!-- Generated at '+b.utc_now()+' by agent-operations-board -->\n'+content);os.chmod(temp,0o600);os.replace(temp,target)
    return str(target)


def tick():
    b=core()
    try:
        sync_room(); flush_outbox(); handoff_tick(); generate_overview()
        with b.connect() as c:
            last=c.execute("SELECT value FROM coordination_meta WHERE key='last_current_backup'").fetchone()
            attempt=c.execute("SELECT value FROM coordination_meta WHERE key='last_current_backup_attempt'").fetchone()
        if (not last or time.time()-float(last[0])>86400) and (not attempt or time.time()-float(attempt[0])>=3600):
            import maintenance
            maintenance.board=b
            with b.connect() as c:
                c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('last_current_backup_attempt',?)",(str(time.time()),))
            try:
                maintenance.backup_current()
            except Exception as exc:
                with b.connect() as c:
                    c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('last_current_backup_error',?)",(str(exc)[:500],))
                raise
            else:
                with b.connect() as c:
                    c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('last_current_backup_error','')")
        with b.connect() as c:
            c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('last_tick',?)",(str(time.time()),))
            c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('last_error','')")
    except Exception as exc:
        with b.connect() as c:
            c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('last_error',?)",(str(exc)[:500],))
        raise
