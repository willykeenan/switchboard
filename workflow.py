"""Operator-controlled durable session placements and directed communication graph.

This module enqueues exact messages. The separately supervised handoff daemon starts eligible idle recipients. No function here calls providers.
The board owns this small store; provider session stores are read-only inputs.
"""
from __future__ import annotations
import copy
import json
import os
import sqlite3
import time
import threading
from activity import ActivityReader, managed_activity, present, blank
import uuid
from pathlib import Path
from datetime import datetime, timezone

ROLES = ('coordinator', 'researcher', 'writer', 'worker', 'auditor')
DEFAULT = {'enabled': True, 'defaultCommunication': 'same-lane', 'placements': [], 'connections': [], 'positions': [], 'notes': [], 'laneCatalog': None, 'teams': [], 'teamPositions': [], 'teamLeads': []}

def now(): return datetime.now(timezone.utc).isoformat()


def workflow_reader(path, timeout=5):
    """SQL-read-only existing-file reader sharing safe same-process fd mode."""
    from store import DiagnosticConnection, annotate
    try: db=sqlite3.connect(Path(path).as_uri()+'?mode=rw',uri=True,timeout=timeout,factory=DiagnosticConnection)
    except sqlite3.Error as exc:
        annotate(exc,path,'workflow-reader:connect');raise
    db.productionDatabase=str(path)
    try:
        db.execute('PRAGMA query_only=ON')
        return db
    except BaseException:
        db.close();raise

def read_state(root):
    path = Path(root) / 'runtime' / 'workflow.sqlite3'
    if not path.exists(): return dict(revision=0, **copy.deepcopy(DEFAULT))
    with workflow_reader(path) as db:
        row = db.execute('SELECT revision,body FROM workflow WHERE id=1').fetchone()
    if not row: raise ValueError('Workflow state is incomplete; communication is held')
    return dict(revision=row[0], **{**copy.deepcopy(DEFAULT), **json.loads(row[1])})

def manual_routing(root):
    try: return read_state(root)['enabled']
    except (OSError, ValueError, sqlite3.Error): return True  # Fail closed, never fall back to a wake.

def set_routing_mode(root, manual, actor='operator-cli'):
    """Explicit operator switch between manual routing (default) and the legacy
    automatic owner assignment. Only the flag changes; placements and history stay."""
    if type(manual) is not bool: raise ValueError('Routing mode must be manual (True) or automatic (False)')
    path=Path(root)/'runtime'/'workflow.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.is_symlink(): raise ValueError('Workflow store cannot be a symlink')
    db=sqlite3.connect(path,timeout=10)
    try:
      with db:
        db.execute('CREATE TABLE IF NOT EXISTS workflow(id INTEGER PRIMARY KEY,revision INTEGER,body TEXT,updated_at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS workflow_history(revision INTEGER PRIMARY KEY,body TEXT,actor TEXT,updated_at TEXT)')
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT revision,body FROM workflow WHERE id=1').fetchone()
        current=row[0] if row else 0
        body={**copy.deepcopy(DEFAULT),**(json.loads(row[1]) if row else {})}
        if body['enabled']!=manual:
            body['enabled']=manual;text=json.dumps(body,ensure_ascii=False);stamp=now()
            db.execute('INSERT OR REPLACE INTO workflow VALUES(1,?,?,?)',(current+1,text,stamp))
            db.execute('INSERT INTO workflow_history VALUES(?,?,?,?)',(current+1,text,actor,stamp))
    finally:
        db.close()
    os.chmod(path,0o600)
    return {'manualRouting':manual_routing(root),'revision':read_state(root)['revision']}

def allowed(state, sender, recipient):
    if sender == recipient: return False
    from workflow_layout import active
    closed={l['id'] for l in state.get('laneCatalog') or [] if not active(l)}
    if any(p['agentId'] in (sender,recipient) and p['laneId'] in closed for p in state['placements']): return False
    edge = next((e for e in state['connections'] if e['from']==sender and e['to']==recipient), None)
    if edge is not None: return edge['allow']
    from company_workflow import role_route
    if role_route(state,sender,recipient):return True
    seats = {p['agentId']:p['laneId'] for p in state['placements']}
    return state['defaultCommunication']=='same-lane' and bool(seats.get(sender)) and seats.get(sender)==seats.get(recipient)

_activity_reader = ActivityReader()
def turn_observation(path):
    activity=_activity_reader.observe(path)
    state={'finished':'TURN_FINISHED','stopped':'TURN_STOPPED','failed':'TURN_FAILED'}.get(activity['turnStatus'],'TURN_STARTED' if activity['turnStatus']=='open' else 'UNKNOWN')
    return {'state':state,'observedAt':activity['observedAt'],'basis':activity['basis'],'activity':activity}

class Workflow:
    def __init__(self, root, workspace, codex_home=None):
        self.root=Path(root); self.path=self.root/'runtime'/'workflow.sqlite3'; self.workspace=workspace
        self.codex_home=Path(codex_home) if codex_home else Path(os.environ.get('SWITCHBOARD_WORKFLOW_CODEX_HOME',str(Path.home()/'.codex')))
        self._cached=None; self._cached_at=0; self._catalog_lock=threading.RLock(); self.activity_reader=ActivityReader(); self._claude_paths={}; self._claude_at=0; self._rollout_paths={}

    def read(self): return read_state(self.root)

    def catalog(self):
        with self._catalog_lock:return self._catalog()

    def identity_catalog(self):
        """Fresh identities without opening every provider transcript for a map poll."""
        with self._catalog_lock:return self._catalog(observe=False)

    def display_identity(self, agent_id, sessions=None):
        """Presentation only: resolve the exact sender, never infer identity from a body."""
        if agent_id == 'service:birds':return 'Birds & Transport service'
        if agent_id == 'service:router':return 'Flow router service'
        sessions = self.identity_catalog()['sessions'] if sessions is None else sessions
        match = next((s for s in sessions if s['agent_id'] == agent_id), {})
        value = match.get('title') or match.get('display_name')
        if not isinstance(value,str) or not value.strip() or value.strip() in {agent_id,match.get('endpoint'),agent_id.partition(':')[2]}:
            return 'Unresolved agent'
        return ' '.join(value.split())[:240]

    def claude_paths(self):
        if time.monotonic()-self._claude_at<60:return self._claude_paths
        paths={};root=self.codex_home.parent/'.claude/projects'
        try:
            for project in root.iterdir():
                if not project.is_dir() or project.is_symlink():continue
                for path in project.glob('*.jsonl'):
                    if not path.is_symlink():paths[path.stem]=path
                    if len(paths)>=12000:break
                if len(paths)>=12000:break
        except OSError:pass
        self._claude_paths=paths;self._claude_at=time.monotonic();return paths

    def activity_snapshot(self,targets=None):
        catalog=self.catalog()
        if targets:
            wanted=set(targets[:200]);catalog['sessions']=[s for s in catalog['sessions'] if s['agent_id'] in wanted]
            for s in catalog['sessions']:
                if not s.get('managed') and s['agent_id'] in self._rollout_paths:s['activity']=self.activity_reader.observe(self._rollout_paths[s['agent_id']])
        if hasattr(self,'taskflow'):
            task_activities=self.taskflow.activities()
            for s in catalog['sessions']:
                if s['agent_id'] in task_activities:s['activity']=task_activities[s['agent_id']]
        return {'schemaVersion':'ke.workflow-activity.v1','sampledAt':now(),'pollSeconds':3,
                'activities':{s['agent_id']:s.get('activity') for s in catalog['sessions']},'errors':catalog['errors']}

    def _catalog(self,observe=True):
        if observe and self._cached is not None and time.monotonic()-self._cached_at<5: return copy.deepcopy(self._cached)
        agents,tasks=self.workspace.inventory(); sessions={a['agent_id']:dict(a,title=a['display_name'],projectName='',turn={'state':'UNKNOWN'}) for a in agents}
        errors=[]
        try:
            state=json.loads((self.codex_home/'.codex-global-state.json').read_text())
            assignments=state.get('thread-project-assignments',{}); projects=state.get('local-projects',{})
        except (OSError,ValueError): assignments={}; projects={}
        path=self.codex_home/'state_5.sqlite'
        if path.exists():
            try:
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3) as db:
                    db.row_factory=sqlite3.Row
                    rows=db.execute("SELECT id,name,title,cwd,model,reasoning_effort,updated_at,rollout_path,source FROM threads WHERE archived=0 ORDER BY updated_at DESC").fetchall()
                for i,r in enumerate(rows):
                    r=dict(r)
                    try:
                        src=json.loads(r['source'])
                        if isinstance(src,dict) and 'subagent' in src: continue
                    except (ValueError,TypeError): pass
                    aid='codex:'+r['id'];self._rollout_paths[aid]=r['rollout_path']; project=projects.get(assignments.get(r['id'],{}).get('projectId'),{})
                    a=sessions.get(aid,{})
                    title=r['name'] or r['title'] or r['id']
                    a.update(agent_id=aid,endpoint=r['id'],provider='codex',title=title,display_name=title,cwd=r['cwd'],model=r['model'],reasoning=r['reasoning_effort'],updatedAt=r['updated_at'],projectName=project.get('name',''),turn=turn_observation(r['rollout_path']) if observe and (i<80 or aid in sessions) else {'state':'UNKNOWN','basis':'Not sampled'})
                    a['activity']=a['turn'].get('activity',present(blank(),source='Not sampled'))
                    sessions[aid]=a
            except (OSError,sqlite3.Error) as exc: errors.append('Codex session inventory unavailable: '+str(exc))
        elif os.environ.get('SWITCHBOARD_WORKFLOW_FIXTURE')!='1': errors.append('Codex session store unavailable')
        from store import read_managed_agents as read_agents
        for managed in read_agents(self.root):
            endpoint=managed.get('providerThreadId')
            if endpoint:sessions.pop('codex:'+endpoint,None)
            sessions[managed['id']]={**sessions.get(managed['id'],{}),'agent_id':managed['id'],'endpoint':endpoint or managed['id'],
                'provider':'codex','title':managed['name'],'display_name':managed['name'],'model':managed['model'],
                'reasoning':managed['effort'],'projectName':next((p['name'] for p in self.workspace.read()['projects'] if p['id']==managed['projectId']),''),
                'managed':True,'workStatus':managed['workStatus'],'activity':managed_activity(managed),'turn':{'state':'UNKNOWN','basis':'See Work for supervised runtime status'},'updatedAt':managed['createdAt']}
        claude=self.claude_paths() if observe and any(s.get('provider')=='claude' for s in sessions.values()) else {}
        for s in sessions.values():
            if observe and s.get('provider')=='claude':s['activity']=self.activity_reader.observe(claude.get(s.get('endpoint') or s['agent_id'].split(':',1)[-1]),'claude')
            s.setdefault('activity',present(blank(),source='No provider activity source'))
            owned=sorted([t for t in tasks if t['owner']==s['agent_id']],key=lambda t:t['updated_at'],reverse=True) if observe else []
            s['latestTask']=owned[0] if owned else None
            s.setdefault('updatedAt',0)
        result={'sessions':sorted(sessions.values(),key=lambda s:(s.get('projectName','').lower()!='demo',-s.get('updatedAt',0))), 'errors':errors}
        if observe:self._cached=copy.deepcopy(result);self._cached_at=time.monotonic()
        return result

    def _connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink(): raise ValueError('Workflow store cannot be a symlink')
        from store import DiagnosticConnection, annotate
        try: db=sqlite3.connect(self.path,timeout=10,factory=DiagnosticConnection)
        except sqlite3.Error as exc:
            annotate(exc,self.path,'workflow-connect');raise
        db.productionDatabase=str(self.path);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS workflow(id INTEGER PRIMARY KEY,revision INTEGER,body TEXT,updated_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_history(revision INTEGER PRIMARY KEY,body TEXT,actor TEXT,updated_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_messages(id TEXT PRIMARY KEY,sender TEXT,recipient TEXT,body TEXT,message_key TEXT UNIQUE,created_at TEXT,read_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_message_intents(message_id TEXT PRIMARY KEY,intent TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_mirrors(message_id TEXT PRIMARY KEY,room_seq INTEGER,mirrored_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_links(id TEXT PRIMARY KEY,actor TEXT NOT NULL,other TEXT NOT NULL,edges TEXT NOT NULL,reason TEXT NOT NULL,revision INTEGER,created_at TEXT NOT NULL,removed_at TEXT,room_seq INTEGER)')
            self.path.chmod(0o600);return db
        except BaseException as exc:
            annotate(exc,self.path,'workflow-schema');db.close();raise

    def save(self, data, revision, actor):
        if set(data)!=set(DEFAULT): raise ValueError('Invalid workflow fields')
        if type(data['enabled']) is not bool or data['defaultCommunication'] not in ('same-lane','explicit-only'): raise ValueError('Invalid communication setting')
        catalog=self.catalog(); ids={s['agent_id'] for s in catalog['sessions']}
        from workflow_layout import validate_layout
        workspace=self.workspace.read()
        laneids=validate_layout(data,{p['id'] for p in workspace['projects']},workspace['lanes'])
        seen=set()
        for p in data['placements']:
            if set(p) not in ({'agentId','laneId','role'},{'agentId','laneId','role','teamId'}) or p['agentId'] not in ids or p['laneId'] not in laneids or p['role'] not in ROLES or p['agentId'] in seen: raise ValueError('Each existing session can have one primary placement')
            seen.add(p['agentId'])
        seen=set()
        for e in data['connections']:
            if set(e)!= {'from','to','allow'} or e['from'] not in ids or e['to'] not in ids or e['from']==e['to'] or type(e['allow']) is not bool or (e['from'],e['to']) in seen: raise ValueError('Invalid directed connection')
            seen.add((e['from'],e['to']))
        seen=set()
        for p in data['positions']:
            if set(p)!= {'laneId','x','y'} or p['laneId'] not in laneids or p['laneId'] in seen or any(type(p[k]) not in (int,float) or not 0<=p[k]<=12000 for k in ('x','y')): raise ValueError('Invalid canvas position')
            seen.add(p['laneId'])
        seen=set()
        for n in data['notes']:
            if set(n)!= {'agentId','text'} or n['agentId'] not in ids or n['agentId'] in seen or not isinstance(n['text'],str) or len(n['text'])>4000: raise ValueError('Invalid assignment note')
            seen.add(n['agentId'])
        body=json.dumps(data,ensure_ascii=False)
        if len(body)>2000000: raise ValueError('Workflow too large')
        phase='workflow-connect';committed=False
        try:
            with self._connect() as db:
                phase='workflow-begin';db.execute('BEGIN IMMEDIATE');r=db.execute('SELECT revision FROM workflow WHERE id=1').fetchone();current=r[0] if r else 0
                if type(revision) is not int or revision!=current:
                    from workspace import Conflict
                    raise Conflict('The workflow changed in another window. Refresh and try again; your newer layout is safe.')
                phase='workflow-write';stamp=now();db.execute('INSERT OR REPLACE INTO workflow VALUES(1,?,?,?)',(current+1,body,stamp));db.execute('INSERT INTO workflow_history VALUES(?,?,?,?)',(current+1,body,actor,stamp))
                phase='workflow-commit'
            committed=True;phase='workflow-readback'
            return self.read()
        except BaseException as exc:
            exc.workflowPhase=phase;exc.workflowCommitObserved=committed;exc.workflowExpectedRevision=revision
            raise

    def mutate(self,payload):
        state=self.read();data={k:state[k] for k in DEFAULT};op=payload['operation'];item=payload.get('item',{})
        from workflow_layout import mutate_layout, prune_leads, active
        if op=='company-workflow':
            project=item['projectId']
            if not any(p['id']==project for p in self.workspace.read()['projects']):raise ValueError('Unknown project')
            from company_workflow import project_lanes
            data['laneCatalog']=project_lanes(project,data['laneCatalog'] if data['laneCatalog'] is not None else self.workspace.read_source()['lanes'])
            data['enabled']=True
        elif op.startswith(('lane-','team-')) or op=='team':
            mutate_layout(data,op,item,self.workspace.read()['lanes'])
        elif op=='place':
            aid=item['agentId'];data['placements']=[p for p in data['placements'] if p['agentId']!=aid]
            if item.get('laneId'):
                lane=next((l for l in self.workspace.read()['lanes'] if l['id']==item['laneId']),None)
                if not lane or not active(lane): raise ValueError('Choose an active lane')
                p={k:item[k] for k in ('agentId','laneId','role')}
                if item.get('teamId'):p['teamId']=item['teamId']
                data['placements'].append(p)
            prune_leads(data)
        elif op=='connection':
            data['connections']=[e for e in data['connections'] if (e['from'],e['to'])!=(item['from'],item['to'])]
            if item.get('allow') is not None: data['connections'].append({k:item[k] for k in ('from','to','allow')})
        elif op=='position':
            data['positions']=[p for p in data['positions'] if p['laneId']!=item['laneId']]+[{k:item[k] for k in ('laneId','x','y')}]
        elif op=='note':
            data['notes']=[n for n in data['notes'] if n['agentId']!=item['agentId']]+[{k:item[k] for k in ('agentId','text')}]
        elif op=='default': data['defaultCommunication']=item['value']
        elif op=='undo':
            with workflow_reader(self.path) as db:
                r=db.execute('SELECT body FROM workflow_history WHERE revision=?',(state['revision']-1,)).fetchone()
            if not r: raise ValueError('No earlier layout to restore')
            data={**copy.deepcopy(DEFAULT),**json.loads(r[0])}
        else: raise ValueError('Unknown workflow action')
        return self.save(data,payload['revision'],'operator-ui')

    def messages(self,recipient=None):
        if not self.path.exists(): return []
        state=self.read()
        with workflow_reader(self.path) as db:
            db.row_factory=sqlite3.Row
            query='SELECT * FROM workflow_messages WHERE read_at IS NULL';args=[]
            if recipient: query+=' AND recipient=?';args.append(recipient)
            rows=[dict(r) for r in db.execute(query+' ORDER BY created_at DESC LIMIT 100',args)]
        from workflow_work import alert_permission
        for r in rows:
            alert=alert_permission(self,r,state)
            permitted=alert['allowed'] if alert is not None else allowed(state,r['sender'],r['recipient'])
            r['status']='CANCELLED' if alert and alert['resolved'] else 'QUEUED' if permitted else 'HELD'
        return [r for r in rows if not recipient or r['status']=='QUEUED']

    def send(self,sender,recipient,body,key,intent="handoff",work=None):
        state=self.read();ids={s['agent_id'] for s in self.catalog()['sessions']}
        if intent not in ("handoff","notification"): raise ValueError("Use handoff or notification intent")
        if not state['enabled']: raise ValueError('Manual workflow is not initialized')
        from workflow_work import ROUTER_SENDER, router_send_allowed
        if sender==ROUTER_SENDER:
            if recipient not in ids or sender==recipient: raise ValueError('Use two exact existing session IDs')
            if not router_send_allowed(self,state,recipient):
                raise ValueError('service:router cannot message agents outside the item project')
        elif sender not in ids or recipient not in ids or sender==recipient: raise ValueError('Use two exact existing session IDs')
        if work is not None:
            if intent != 'handoff': raise ValueError('Actionable work cannot be a passive notification')
            from workflow_work import prepare
            work=prepare(self,sender,recipient,work)
        if not isinstance(body,str) or not body.strip() or len(body)>16000 or not isinstance(key,str) or not key or len(key)>200: raise ValueError('A bounded message and stable idempotency key are required')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE');r=db.execute('SELECT * FROM workflow_messages WHERE message_key=?',(key,)).fetchone()
            if r:
                if (r['sender'],r['recipient'],r['body'])!=(sender,recipient,body):raise ValueError('Message key reused with different content')
                mid=r['id']
                old_intent=db.execute('SELECT intent FROM workflow_message_intents WHERE message_id=?',(mid,)).fetchone()
                if old_intent and old_intent['intent']!=intent:raise ValueError('Message key reused with different intent')
                from workflow_work import exists
                existing_work=exists(db) and db.execute('SELECT 1 FROM workflow_work WHERE message_id=?',(mid,)).fetchone()
                if work is None and existing_work:
                    raise ValueError('Original work contract cannot be omitted on repeat')
                if work is not None and not existing_work:
                    raise ValueError('Historical message cannot acquire a new work contract')
            else:
                from workflow_work import routed_pair
                if intent == 'handoff' and work is None and routed_pair(self,state,sender,recipient):
                    raise ValueError('Routed project action requires --work-file with assignment, deadline and result path; --notification is information only')
                mid=str(uuid.uuid4());db.execute('INSERT INTO workflow_messages VALUES(?,?,?,?,?,?,NULL)',(mid,sender,recipient,body,key,now()))
            db.execute("INSERT OR IGNORE INTO workflow_message_intents VALUES(?,?)",(mid,intent))
            if work is not None:
                from workflow_work import record
                record(db,mid,work)
        mirror=self.mirror_pending()
        queued=allowed(state,sender,recipient) or (sender==ROUTER_SENDER and router_send_allowed(self,state,recipient))
        return {'id':mid,'status':'QUEUED' if queued else 'HELD','launched':False,'interrupted':False,'roomMirrorError':mirror.get('error'),'intent':intent,'workTracked':work is not None,'note':'Handoff delivery is tracked at http://127.0.0.1:47836/; notifications do not wake recipients.'}

    def link(self,actor,other,reason,both=True):
        """An agent opens its own connections when its work needs another agent.

        Only edges touching the actor are added, as explicit Allows. The operator's explicit Blocks and closed
        lanes always win and are never changed here. Every link is logged and announced to the operator.
        """
        ids={s['agent_id'] for s in self.catalog()['sessions']}
        if actor not in ids or other not in ids or actor==other: raise ValueError('Use two exact existing session IDs')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000: raise ValueError('Say why in one short reason (1-1000 characters)')
        from workspace import Conflict
        from workflow_layout import active
        pairs=[(actor,other)]+([(other,actor)] if both else [])
        for attempt in range(3):
            state=self.read()
            if not state['enabled']: raise ValueError('Manual workflow is not initialized')
            closed={l['id'] for l in state.get('laneCatalog') or [] if not active(l)}
            if any(p['agentId'] in (actor,other) and p['laneId'] in closed for p in state['placements']): raise ValueError('A closed lane holds this pair; only the operator can reopen it')
            edges={(e['from'],e['to']):e['allow'] for e in state['connections']}
            blocked=[a+' -> '+b for a,b in pairs if edges.get((a,b)) is False]
            if blocked: raise ValueError('The operator blocked '+', '.join(blocked)+'; only they can change that in Constellations')
            added=[(a,b) for a,b in pairs if (a,b) not in edges]
            if not added:
                return {'linked':True,'added':[],'revision':state['revision'],'mayMessage':allowed(state,actor,other),'mayReceive':allowed(state,other,actor),'note':'Already linked; nothing changed.'}
            data={k:copy.deepcopy(state[k]) for k in DEFAULT}
            data['connections']+=[{'from':a,'to':b,'allow':True} for a,b in added]
            try:
                saved=self.save(data,state['revision'],'agent-link:'+actor);break
            except Conflict:
                if attempt==2: raise
        lid=str(uuid.uuid4());edges_json=json.dumps([{'from':a,'to':b} for a,b in added])
        with self._connect() as db:db.execute('INSERT INTO workflow_links VALUES(?,?,?,?,?,?,?,NULL,NULL)',(lid,actor,other,edges_json,reason.strip(),saved['revision'],now()))
        notice=self._announce_link(lid,actor,other,added,reason.strip())
        return {'linked':True,'id':lid,'added':[{'from':a,'to':b} for a,b in added],'revision':saved['revision'],
                'mayMessage':allowed(saved,actor,other),'mayReceive':allowed(saved,other,actor),'roomNotice':notice,
                'note':'Held messages between this pair now deliver on the next dispatcher pass. The operator sees this link in Constellations and can Block it.'}

    def unlink(self,actor,other):
        """Remove only the edges agents opened between this pair with link(); the operator's own edges stay."""
        ids={s['agent_id'] for s in self.catalog()['sessions']}
        if actor not in ids or other not in ids or actor==other: raise ValueError('Use two exact existing session IDs')
        with self._connect() as db:
            rows=[dict(r) for r in db.execute('SELECT id,edges FROM workflow_links WHERE removed_at IS NULL AND ((actor=? AND other=?) OR (actor=? AND other=?))',(actor,other,other,actor))]
        opened={(e['from'],e['to']) for r in rows for e in json.loads(r['edges'])}
        from workspace import Conflict
        for attempt in range(3):
            state=self.read();data={k:copy.deepcopy(state[k]) for k in DEFAULT}
            keep=[e for e in data['connections'] if not ((e['from'],e['to']) in opened and e['allow'] is True)]
            removed=len(data['connections'])-len(keep)
            if not removed:break
            data['connections']=keep
            try:
                state=self.save(data,state['revision'],'agent-unlink:'+actor);break
            except Conflict:
                if attempt==2: raise
        with self._connect() as db:
            for r in rows:db.execute('UPDATE workflow_links SET removed_at=? WHERE id=?',(now(),r['id']))
        return {'unlinked':removed,'revision':state['revision'],'mayMessage':allowed(state,actor,other),'mayReceive':allowed(state,other,actor)}

    def links(self,agent=None):
        if not self.path.exists():return []
        with workflow_reader(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_links'").fetchone():return []
            db.row_factory=sqlite3.Row
            rows=[dict(r) for r in db.execute('SELECT * FROM workflow_links ORDER BY created_at DESC LIMIT 200')]
        return [dict(r,edges=json.loads(r['edges'])) for r in rows if agent is None or agent in (r['actor'],r['other'])]

    def _announce_link(self,lid,actor,other,added,reason):
        """Tell the operator in the global room; a lost post never undoes the link."""
        try:
            from room_writer import post as write_room
            direction='both ways' if len(added)==2 else ('one way: '+added[0][0]+' -> '+added[0][1])
            body=('Link opened by an agent ('+direction+')\nFrom: '+self.display_identity(actor)+'\nWith: '+self.display_identity(other)+
                  '\nWhy: '+reason+'\n\nExact agents: '+actor+' and '+other+'\nThe operator can Block or remove it in Constellations: http://127.0.0.1:47834/constellations')
            result=write_room(Path(self.workspace.rooms.root)/'global',actor,body,['operator'],'message',None,'workflow-link:'+lid)
            with self._connect() as db:db.execute('UPDATE workflow_links SET room_seq=? WHERE id=?',(result['seq'],lid))
            return {'posted':True,'seq':result['seq']}
        except (OSError,ValueError,KeyError,sqlite3.Error,SystemExit) as exc:
            return {'posted':False,'error':str(exc)}

    def mirror_pending(self,limit=5):
        """Human-only room outbox. Stable keys recover a lost post receipt."""
        if not self.read()['enabled']:return {'mirrored':0}
        if time.monotonic()<getattr(self,'_mirror_retry_at',0):return {'mirrored':0}
        with self._connect() as db:
            pending=[dict(r) for r in db.execute('SELECT m.* FROM workflow_messages m LEFT JOIN workflow_mirrors r ON m.id=r.message_id WHERE r.message_id IS NULL ORDER BY m.created_at LIMIT ?',(max(1,min(limit,20)),))]
        if not pending:return {'mirrored':0}
        try:
            from room_writer import post as write_room
            room_dir=Path(self.workspace.rooms.root)/'global'
            for m in pending:
                body='Workflow message (delivery state: http://127.0.0.1:47836/)\nFrom: '+self.display_identity(m['sender'])+'\nTo: '+self.display_identity(m['recipient'])+'\n\n'+m['body']+'\n\nExact sender: '+m['sender']+'\nExact recipient: '+m['recipient']+'\nThe operator can inspect its current permission in Constellations: http://127.0.0.1:47834/constellations'
                result=write_room(room_dir,m['sender'],body,['operator'],'message',None,'workflow-inbox:'+m['id'])
                with self._connect() as db:db.execute('INSERT OR IGNORE INTO workflow_mirrors VALUES(?,?,?)',(m['id'],result['seq'],now()))
            return {'mirrored':len(pending)}
        except (OSError,ValueError,sqlite3.Error,SystemExit) as exc:
            self._mirror_retry_at=time.monotonic()+60
            return {'mirrored':0,'error':str(exc),'note':'Request stays saved; human-room mirror will retry without waking an agent.'}

    def context(self, session):
        state=self.read();ids={s['agent_id'] for s in self.catalog()['sessions']}
        endpoint=session.removeprefix('codex:')
        aid=session if session in ids else next((s['agent_id'] for s in self.catalog()['sessions'] if s.get('endpoint')==endpoint or s['agent_id'].split(':',1)[-1]==session),None)
        if not aid:return {'enabled':state['enabled'],'assignment':None,'inbox':[],'note':'Unknown exact session. Do not infer an assignment or contact another agent.'}
        from workflow_layout import lane_context, team_of
        placement=next((p for p in state['placements'] if p['agentId']==aid),None)
        own_lane=next((l for l in self.workspace.read()['lanes'] if placement and l['id']==placement['laneId']),None)
        organization=lane_context(state,own_lane)
        if organization:
            organization['members']=[p for p in state['placements'] if p['laneId']==own_lane['id']]
            organization['yourTeam']=team_of(placement)
        from inspector.governance import role_contract
        task_context={}
        with sqlite3.connect(self.root/'board.sqlite3') as db:
            has_tasks=db.execute("SELECT 1 FROM sqlite_master WHERE name='taskflow_tasks'").fetchone()
        if has_tasks:
            from taskflow import TaskFlow
            task_context=TaskFlow(self.root,self).context(aid)
        return {'operatingRole':role_contract(self,aid),'lane':organization,'enabled':state['enabled'],'revision':state['revision'],'session':aid,'assignment':next((p for p in state['placements'] if p['agentId']==aid),None),'assignmentNote':next((n['text'] for n in state['notes'] if n['agentId']==aid),''),'mayMessage':[i for i in sorted(ids) if allowed(state,aid,i)],'selfLink':{'command':'workflowctl.py link --to <exact agent id> --reason "<why this work needs them>"','rule':"Open your own two-way link when work needs another agent. The operator's explicit Blocks and closed lanes still win; every link is announced to them and they can Block it in Constellations."},'inbox':self.messages(aid),'taskBoard':task_context,'liveTurnRule':'Keep the current live task. Handoffs can wake eligible idle recipients; busy recipients finish first. Notifications never wake.','handoffs':self.handoff_status(aid)}

    def handoff_status(self,aid=None):
        from workflow_handoffs import Handoffs
        result=Handoffs(self).snapshot()
        if aid:result['handoffs']=[h for h in result['handoffs'] if aid in (h['sender'],h['recipient'])]
        from workflow_work import snapshot
        result['work']=snapshot(self,aid)
        result['workNeedsAttention']=sum(w['needsAttention'] for w in result['work'])
        return result

    def snapshot(self):
        state=self.read();w=self.workspace.read();catalog=self.catalog()
        if hasattr(self,'taskflow'):
            task_activities=self.taskflow.activities()
            for s in catalog['sessions']:
                if s['agent_id'] in task_activities:s['activity']=task_activities[s['agent_id']]
        from workflow_layout import ALIGNMENT_CHARTER
        from company_workflow import describe
        company=[describe(p['id'],w['lanes'],state['placements']) for p in w['projects']]
        return {**state,**catalog,'companyWorkflows':company,'tasks':self.taskflow.list() if hasattr(self,'taskflow') else [],'alignmentCharter':ALIGNMENT_CHARTER,'projects':w['projects'],'lanes':w['lanes'],'roles':list(ROLES),'requests':self.messages(),'handoffs':self.handoff_status(),'generatedAt':now(),'enforcement':{'boardWake':'disabled' if state['enabled'] else 'legacy','delivery':'exact-agent handoffs via supervised idle-start dispatcher; notifications remain passive','providerTools':'Instruction rule; direct provider tools and already-loaded turns are outside the local router'}}
