"""Project/lane coordination overlay. Never launches agents or changes task ownership.

The small registry has its own transactional database. Existing task/agent rows
and append-only room messages remain the sources of truth for work and history.
"""
from __future__ import annotations
import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROLES = ('coordinator', 'researcher', 'writer', 'worker', 'auditor')
EMPTY = {'projects': [], 'lanes': [], 'members': [], 'taskLinks': [], 'dependencies': []}


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', value):
        raise ValueError('Use a short lowercase ID with letters, numbers or hyphens')
    return value


def bounded(value, label, size=2000, required=True):
    if not isinstance(value, str) or len(value) > size or (required and not value.strip()):
        raise ValueError('Invalid ' + label)
    return value.strip()


class Conflict(ValueError):
    pass


def workspace_reader(path):
    """Existing-file, SQL-read-only connection with the local writer fd mode.

    Mixing read-only and read-write file descriptors on Apple's bundled SQLite
    can break same-process lock reuse. query_only keeps the observation boundary
    while matching the mode used by the board and workspace writers.
    """
    from store import ClosingConnection
    db=sqlite3.connect(Path(path).as_uri()+'?mode=rw',uri=True,timeout=5,factory=ClosingConnection)
    try:
        db.execute('PRAGMA query_only=ON')
        return db
    except BaseException:
        db.close();raise


class Workspace:
    def __init__(self, root, rooms):
        self.root = Path(root)
        self.path = self.root / 'runtime' / 'workspaces.sqlite3'
        self.rooms = rooms

    def read_source(self):
        if not self.path.exists():
            return dict(revision=0, updatedAt=None, **copy.deepcopy(EMPTY))
        with workspace_reader(self.path) as db:
            row = db.execute('SELECT revision, body, updated_at FROM workspace WHERE id=1').fetchone()
        return dict(revision=row[0], updatedAt=row[2], **json.loads(row[1])) if row else dict(revision=0, updatedAt=None, **copy.deepcopy(EMPTY))

    def read(self):
        d=self.read_source()
        from workflow import read_state
        from workflow_layout import lanes_for
        control=read_state(self.root)
        if control['enabled']:
            d['lanes']=lanes_for(control,d['lanes'])
            d['workflowRevision']=control['revision']
        return d

    def inventory(self):
        path = self.root / 'board.sqlite3'
        if not path.exists():
            return [], []
        with workspace_reader(path) as db:
            db.row_factory = sqlite3.Row
            agents = [dict(r) for r in db.execute("SELECT agent_id,endpoint,provider,display_name,status,last_seen_at FROM agents WHERE provider!='room'")]
            tasks = [dict(r) for r in db.execute('SELECT task_id,owner,objective,status,next_action,blocker,updated_at,aliases_json,dependencies_json FROM tasks')]
        for a in agents:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(a['last_seen_at'].replace('Z', '+00:00'))).total_seconds()
            except (ValueError, TypeError):
                age = None
            a['presence'] = a['status'] if age is not None and age < 180 else 'UNKNOWN'
            a['ageSeconds'] = round(age) if age is not None else None
        for t in tasks:
            t['aliases'] = json.loads(t.pop('aliases_json'))
            t['dependencies'] = json.loads(t.pop('dependencies_json'))
        return agents, tasks

    def validate(self, d):
        if set(d) != set(EMPTY) or any(not isinstance(d[k], list) or len(d[k]) > 1000 for k in EMPTY):
            raise ValueError('Invalid workspace document')
        projects, lanes = {}, {}
        for p in d['projects']:
            identifier(p['id']); bounded(p['name'], 'project name', 100); bounded(p['objective'], 'project objective')
            if p['id'] in projects:
                raise ValueError('Duplicate project ID')
            projects[p['id']] = p
        for l in d['lanes']:
            identifier(l['id']); bounded(l['name'], 'lane name', 100); bounded(l['objective'], 'lane objective')
            if l['projectId'] not in projects or l['id'] in lanes:
                raise ValueError('Unknown project or duplicate lane ID')
            aliases = l.get('aliases', [])
            if not isinstance(aliases, list) or len(aliases) > 30 or any(not isinstance(a, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:_-]{1,100}', a) or a in ('codex', 'claude', 'all', 'operator', 'agent-board') for a in aliases):
                raise ValueError('Use specific lane aliases; generic agent names mix unrelated work')
            lanes[l['id']] = l
        agents, tasks = self.inventory()
        agent_ids = {a['agent_id'] for a in agents}
        task_map = {t['task_id']: t for t in tasks}
        seen = set()
        for m in d['members']:
            key = (m['laneId'], m['agentId'], m['role'])
            if m['laneId'] not in lanes or m['agentId'] not in agent_ids or m['role'] not in ROLES or key in seen:
                raise ValueError('Role must link one existing registered agent to an existing lane')
            seen.add(key)
            if m['role'] == 'auditor' and any(x['laneId'] == m['laneId'] and x['agentId'] == m['agentId'] and x['role'] in ('researcher', 'writer', 'worker') for x in d['members']):
                raise ValueError('A researcher/writer/worker cannot be the independent auditor of the same lane')
        seen_tasks = set()
        for link in d['taskLinks']:
            t = task_map.get(link['taskId'])
            if not t or link['laneId'] not in lanes or t['task_id'] in seen_tasks:
                raise ValueError('Each existing task belongs to exactly one primary lane')
            seen_tasks.add(t['task_id'])
            if not any(m['laneId'] == link['laneId'] and m['agentId'] == t['owner'] for m in d['members']):
                raise ValueError('Link the actual task owner to this lane first')
        edges = {k: [] for k in lanes}
        seen_edges = set()
        for e in d['dependencies']:
            a, b = e['laneId'], e['needsLaneId']
            bounded(e['reason'], 'dependency reason', 500)
            if a not in lanes or b not in lanes or a == b or lanes[a]['projectId'] != lanes[b]['projectId'] or (a,b) in seen_edges:
                raise ValueError('Dependency must connect two distinct lanes in the same project')
            seen_edges.add((a,b)); edges[a].append(b)
        visited = set()
        def visit(k, stack):
            if k in stack:
                raise ValueError('Dependency cycle; shared discussion is not a blocking dependency')
            if k in visited:
                return
            for child in edges[k]:
                visit(child, stack | {k})
            visited.add(k)
        for k in edges:
            visit(k, set())

    def save(self, document, expected_revision, actor):
        bounded(actor, 'actor', 200)
        self.validate(document)
        body = json.dumps(document, ensure_ascii=False, sort_keys=True)
        if len(body.encode()) > 1000000:
            raise ValueError('Workspace too large')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError('Workspace database must not be a symlink')
        from store import ClosingConnection
        with sqlite3.connect(self.path, timeout=10, factory=ClosingConnection) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS workspace(id INTEGER PRIMARY KEY, revision INTEGER, body TEXT, updated_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_history(revision INTEGER PRIMARY KEY, body TEXT, actor TEXT, updated_at TEXT)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM workspace WHERE id=1').fetchone()
            revision = row[0] if row else 0
            if type(expected_revision) is not int or revision != expected_revision:
                raise Conflict('Workspace changed. Close and reopen this editor before saving; no changes were applied.')
            stamp = now()
            db.execute('INSERT OR REPLACE INTO workspace VALUES(1,?,?,?)', (revision+1, body, stamp))
            db.execute('INSERT INTO workspace_history VALUES(?,?,?,?)', (revision+1, body, actor, stamp))
        self.path.chmod(0o600)
        return self.read()

    def mutate(self, payload, actor='operator'):
        from workflow import manual_routing
        if payload['operation']=='lane' and manual_routing(self.root):
            raise Conflict('Edit lanes in Switchboard; lanes and teams share its undo history.')
        old = self.read_source()
        d = {k: old[k] for k in EMPTY}
        op = payload['operation']
        item = payload['item']
        if op in ('project', 'lane'):
            key = 'projects' if op == 'project' else 'lanes'
            allowed = ('id','name','objective') if op == 'project' else ('id','projectId','name','objective','aliases')
            item = {k:item[k] for k in allowed}
            existing = next((x for x in d[key] if x['id'] == item['id']), None)
            if existing:
                if op == 'lane' and existing['projectId'] != item['projectId']:
                    raise ValueError('A lane cannot silently move projects')
                existing.update(item)
            else:
                d[key].append(item)
        elif op == 'member':
            item = {k:item[k] for k in ('laneId','agentId','role')}
            d['members'] = [m for m in d['members'] if m != item]
            d['members'].append(item)
        elif op == 'unlink-member':
            d['members'] = [m for m in d['members'] if not all(m.get(k) == item.get(k) for k in ('laneId','agentId','role'))]
        elif op == 'task':
            d['taskLinks'] = [t for t in d['taskLinks'] if t['taskId'] != item['taskId']]
            if item.get('laneId'):
                d['taskLinks'].append({k:item[k] for k in ('laneId','taskId')})
        elif op == 'dependency':
            d['dependencies'] = [e for e in d['dependencies'] if not (e['laneId'] == item['laneId'] and e['needsLaneId'] == item['needsLaneId'])]
            if item.get('reason'):
                d['dependencies'].append({k:item[k] for k in ('laneId','needsLaneId','reason')})
        else:
            raise ValueError('Unknown operation')
        result=self.save(d, payload['revision'], actor)
        if op=='project' and not existing:
            # Only a newly created project adopts defaults automatically.
            # Existing projects use the visible company-workflow adoption action.
            from workflow import Workflow
            flow=Workflow(self.root,self)
            flow.mutate({'operation':'company-workflow','revision':flow.read()['revision'],'item':{'projectId':item['id']}})
        return result

    def snapshot(self):
        d = self.read()
        agents, tasks = self.inventory()
        from workflow import read_state
        control = read_state(self.root)
        if control['enabled']:
            # The manual canvas is the current assignment source. Legacy history
            # stays intact; a drag cannot rewrite the provider's task or files.
            d['members'] = copy.deepcopy(control['placements'])
            primary = {p['agentId']:p['laneId'] for p in control['placements']}
            d['taskLinks'] = [{'taskId':t['task_id'],'laneId':primary[t['owner']]} for t in tasks if t['owner'] in primary]
        task_map = {t['task_id']: t for t in tasks}
        for lane in d['lanes']:
            lane['tasks'] = [task_map[x['taskId']] for x in d['taskLinks'] if x['laneId'] == lane['id'] and x['taskId'] in task_map]
            current = {}
            for task in sorted(lane['tasks'],key=lambda t:t['updated_at']): current[task['owner']]=task
            lane['currentTasks']=list(current.values())
            lane['historicalTaskCount']=len(lane['tasks'])-len(current)
            states = [t['status'] for t in lane['currentTasks']]
            lane['status'] = 'UNLINKED' if not states else next((s for s in ('BLOCKED','WORKING','ACKNOWLEDGED','QUEUED','REPORTED_DONE') if s in states), 'VERIFIED_COMPLETE' if all(s == 'VERIFIED_COMPLETE' for s in states) else 'MIXED')
        d.update(agents=agents, tasks=tasks, roles=list(ROLES), generatedAt=now(),
                 semantics='Roles organize existing agents; they do not launch workers or transfer ownership. Status comes from linked task records, not live process proof.')
        return d

    def context(self, endpoint):
        d = self.snapshot()
        ids = {a['agent_id'] for a in d['agents'] if a['endpoint'] == endpoint or a['agent_id'] == endpoint}
        memberships = [m for m in d['members'] if m['agentId'] in ids]
        lanes = [l for l in d['lanes'] if any(m['laneId'] == l['id'] for m in memberships)]
        recent = self.feed(agent=next(iter(ids)),limit=5)['messages'] if memberships else []
        return {'revision':d['revision'], 'memberships':memberships,
                'lanes':[{'id':l['id'],'projectId':l['projectId'],'name':l['name'],'objective':l['objective'][:500],'status':l['status'],'taskIds':[t['task_id'] for t in l['tasks']]} for l in lanes],
                'projects':[p for p in d['projects'] if p['id'] in {l['projectId'] for l in lanes}],
                'dependencies':[e for e in d['dependencies'] if e['laneId'] in {l['id'] for l in lanes}],
                'peerLanes':[{'id':l['id'],'name':l['name'],'status':l['status']} for l in d['lanes'] if l['projectId'] in {own['projectId'] for own in lanes}],
                'postCommand':'workspace_ctl.py post --project PROJECT --lane LANE --author YOUR_ALIAS --to EXACT_RECIPIENT --message TEXT',
                'recentLaneMessages':[{'seq':m['seq'],'author':m['author'],'body':m['body'][:700],'bodyTruncated':len(m['body'])>700,'laneMatches':m['laneMatches']} for m in recent],
                'messagesNote':'Context only. Legacy matches are inferred. Reading does not acknowledge messages or grant authority; use exact recipient and original room message for handoffs.',
                'note':d['semantics']}

    def matches(self, m, lane, d):
        scope = m.get('workspace')
        if isinstance(scope, dict):
            return 'Explicit lane' if scope.get('projectId') == lane['projectId'] and lane['id'] in scope.get('laneIds', []) else None
        # Shared auditor membership alone must not flood every lane with all reviews.
        aliases = set(lane.get('aliases', []))
        participants = {m['author'], *(m.get('recipients') or [])}
        if aliases & participants:
            return 'Participant match'
        task_ids = [x['taskId'] for x in d['taskLinks'] if x['laneId'] == lane['id']]
        if any(t in m['body'] for t in task_ids):
            return 'Task reference'
        return None

    def feed(self, project='', lane='', agent='', room='global', q='', author='', kind='', before=0, limit=30):
        d = self.read()
        lanes = [l for l in d['lanes'] if (not project or l['projectId'] == project) and (not lane or l['id'] == lane)]
        if lane and not lanes or project and not any(p['id'] == project for p in d['projects']):
            raise ValueError('Unknown project or lane')
        if agent:
            lanes = [l for l in lanes if any(m['laneId'] == l['id'] and m['agentId'] == agent for m in d['members'])]
        if not 1 <= limit <= 100 or before < 0 or len(q) > 500:
            raise ValueError('Invalid page or search')
        s = self.rooms.snapshot(room)
        found = []
        for m in s['messages']:
            matched = [(l, self.matches(m, l, d)) for l in lanes]
            matched = [(l, basis) for l,basis in matched if basis]
            if not matched:
                continue
            found.append(dict(m, laneMatches=[{'id':l['id'], 'name':l['name'], 'basis':basis} for l,basis in matched]))
        total = len(found)
        authors = sorted({m['author'] for m in found})
        found = [m for m in found if (not author or m['author']==author) and (not kind or m.get('kind')==kind) and (not q or q.casefold() in (m['body']+' '+m['author']).casefold())]
        older = [m for m in found if not before or m['seq'] < before]
        page = list(reversed(older[-limit:]))
        return {'messages':page,'matched':len(found),'total':total,'authors':authors,'latestSeq':found[-1]['seq'] if found else 0,
                'nextBefore':page[-1]['seq'] if len(older)>limit else None,
                'invalidLines':s['invalidLines'],'partialTail':s['partialTail'],
                'note':'Legacy messages are inferred from specific participants or task references; explicit lane tags take precedence. Original messages remain in their room.'}

    def post(self, project, lanes, author, body, kind='message', recipients=None, key=None):
        d = self.read()
        selected = [l for l in d['lanes'] if l['id'] in lanes and l['projectId'] == project]
        if not lanes or len(set(lanes)) != len(selected):
            raise ValueError('Select existing lanes in this project')
        from room_writer import post as write_room
        room_dir = Path(self.rooms.root) / 'global'
        return write_room(room_dir, author, body, recipients or ['all'], kind, None, key,
                          workspace={'projectId':project,'laneIds':sorted(set(lanes))})
