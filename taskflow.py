"""Durable task intake, exact-once assignments and task-scoped information flow.

Uses the existing board database. Legacy contracts are read-only inputs; the task
ID stays stable. Owner notification delivery is never an execution predicate.
This module does not start provider sessions or infer permission from presence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from contextlib import ExitStack
from pathlib import Path

from workspace import Conflict

RULE_VERSION = 'taskflow-3'
from taskflow_delivery import STAGES, REVIEW_STATES
ACTIVE = ('OFFERED', 'ACCEPTED', 'STARTING', 'RUNNING', 'UNCERTAIN', 'CANCELLING')
TERMINAL = ('DONE', 'CANCELLED')
SCHEMA = """
CREATE TABLE IF NOT EXISTS taskflow_tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_keys(key TEXT PRIMARY KEY, digest TEXT NOT NULL, task_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS taskflow_event_task ON taskflow_events(task_id,seq);
CREATE TABLE IF NOT EXISTS taskflow_assignments(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS taskflow_one_task ON taskflow_assignments(task_id) WHERE state IN ('OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING');
CREATE UNIQUE INDEX IF NOT EXISTS taskflow_one_worker ON taskflow_assignments(worker) WHERE state IN ('OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING');
CREATE TABLE IF NOT EXISTS taskflow_workers(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_policies(lane TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_notifications(seq INTEGER PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL, read_at REAL);
CREATE TABLE IF NOT EXISTS taskflow_instance(id INTEGER PRIMARY KEY CHECK(id=1), root TEXT NOT NULL);
"""


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def legacy_progress(raw):
    """Preserve malformed retained notes without interpreting them as authority."""
    try:value=json.loads(raw or '{}')
    except (ValueError,TypeError):return {'legacyProgress':raw,'legacyProgressError':'Progress is not valid JSON; owner reconciliation required'}
    problem=not isinstance(value,dict)
    if not problem:
        problem=any(value.get(k) is not None and not isinstance(value[k],dict)
                    for k in ('workerAcceptance','sourceRequest'))
        problem=problem or any(k in value and
                    (not isinstance(value[k],list) or any(not isinstance(x,str) for x in value[k]))
                    for k in ('childTaskIds','requiredSubtaskIds','taskIds','individualBoardTaskIds','canonicalImplementationTaskIds'))
        problem=problem or any(value.get(k) is not None and not isinstance(value[k],str)
                    for k in ('recordKind','assignedWorker','parentTaskId','canonicalParentTaskId'))
        problem=problem or any(isinstance(value.get(k),dict) and value[k].get(field) is not None and not isinstance(value[k][field],str)
                    for k,field in (('workerAcceptance','worker'),('sourceRequest','text')))
        attachments=value.get('attachments',[])
        problem=problem or not isinstance(attachments,list) or any(not isinstance(ref,dict) for ref in attachments)
        if not problem:
            refs=[value.get('sourceRequest') or {},*attachments]
            problem=any(ref.get(k) is not None and not isinstance(ref[k],str)
                        for ref in refs for k in ('path','sha256','fileName'))
        if not problem and value.get('recordKind') in ('task-intake','linked-task-subelement','initiative-task') and value.get('delivery') is not None:
            # Reuse the existing consumer's contract instead of guessing which
            # malformed nested delivery fields it can safely interpret.
            from taskflow_delivery import contract
            try:contract(value['delivery'])
            except (ValueError,TypeError,AttributeError,OSError):problem=True
    if problem:return {'legacyProgress':value,'legacyProgressError':'Progress has an unsupported shape; owner reconciliation required'}
    return value


def text(value, label, limit=16000, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError('Invalid '+label)
    return value.strip()


def paths(values):
    if not isinstance(values, list) or len(values) > 30:
        raise ValueError('Invalid source scopes')
    result=[]
    for value in values:
        p=Path(text(value, 'absolute scope', 4096))
        if not p.is_absolute() or '..' in p.parts or p.is_symlink() or p.resolve()!=p:
            raise ValueError('Scope must be an absolute resolved path without symlinks')
        result.append(str(p))
    return sorted(set(result))


def inside(path, scopes):
    p=Path(path).resolve()
    return any(p==Path(s) or Path(s) in p.parents for s in scopes)


def overlap(a, b):
    return any(inside(x, b) for x in a) or any(inside(x, a) for x in b)


def pin_files(values):
    if not isinstance(values, list) or len(values)>30:
        raise ValueError('At most 30 explicit input files')
    result=[]
    for value in values:
        p=Path(text(value, 'input path', 4096))
        if not p.is_absolute() or p.resolve()!=p or not p.is_file() or p.stat().st_size>8_000_000:
            raise ValueError('Input must be an explicit regular file at most 8 MB: '+str(p))
        b=p.read_bytes()
        result.append({'path':str(p),'name':p.name,'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)})
    return result


class TaskFlow:
    def __init__(self, root, flow=None, clock=time.time, observer=None):
        self.root=Path(root).resolve(); self.path=self.root/'board.sqlite3'; self.flow=flow; self.clock=clock
        self.observer=observer
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute('INSERT OR IGNORE INTO taskflow_instance VALUES(1,?)',(str(self.root),))
            self.boundRoot=db.execute('SELECT root FROM taskflow_instance WHERE id=1').fetchone()[0]
            from audit_team import SCHEMA as AUDIT_SCHEMA
            db.executescript(AUDIT_SCHEMA)
            from taskflow_delivery import SCHEMA as DELIVERY_SCHEMA
            db.executescript(DELIVERY_SCHEMA)
            from taskflow_annotations import SCHEMA as ANNOTATION_SCHEMA
            db.executescript(ANNOTATION_SCHEMA)
            from taskflow_learning import install_schema as install_learning_schema
            install_learning_schema(db)
            from taskflow_decisions import install_schema as install_decision_schema
            install_decision_schema(db)
        from audit_team import AuditTeam
        self.audit=AuditTeam(self)
        from taskflow_cycle import Cycle
        from execution_admission import Admission
        self.cycle=Cycle(self)
        with self.connect() as db: Admission.install_schema(db)
        from taskflow_delivery import Delivery
        self.delivery=Delivery(self)
        from taskflow_annotations import Annotations
        self.annotations=Annotations(self)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT body FROM taskflow_tasks").fetchall():
                t=json.loads(row[0])
                if t["state"]=="DONE" and not t.get("completion"):
                    t.update(state="NEEDS_VERIFICATION",historicalState="DONE",waitReason="Historical completion retained; required destination and independent verification have not been recorded")
                    self._put(db,t);self._event(db,t,"completion-migration","task-board",{"previousState":"DONE"})

    @property
    def recovery_hold(self):return self.boundRoot!=str(self.root)

    def connect(self):
        from store import ClosingConnection
        db=sqlite3.connect(self.path, timeout=15, factory=ClosingConnection)
        try:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA synchronous=FULL')
            return db
        except BaseException:
            db.close()
            raise

    def _one(self, db, table, ident, field='id'):
        r=db.execute('SELECT body FROM '+table+' WHERE '+field+'=?',(ident,)).fetchone()
        return json.loads(r[0]) if r else None

    def _put(self, db, task):
        task['updatedAt']=self.clock(); task['version']+=1
        db.execute('INSERT OR REPLACE INTO taskflow_tasks VALUES(?,?)',(task['taskId'],encoded(task)))

    def _assignment(self, db, assignment):
        db.execute('INSERT INTO taskflow_assignments VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,body=excluded.body',
                   (assignment['id'],assignment['taskId'],assignment['workerId'],assignment['state'],encoded(assignment)))

    def _busy_couriers(self, db):
        """Named birds occupied only while an offer is in flight; they then return to the birdhouse."""
        busy={}
        for row in db.execute('SELECT body FROM taskflow_assignments ORDER BY rowid'):
            a=json.loads(row[0]); bird=a.get('courierId')
            if not bird or a['state'] in ('DECLINED','EXPIRED','FAILED','CANCELLED'): continue
            t=self._one(db,'taskflow_tasks',a['taskId'])
            if a.get('phase') and a['state']=='OFFERED' and t and t['state'] not in TERMINAL:
                busy[bird]=a;continue
            if not t or t.get('assignmentId')!=a['id'] or t['state'] in ('QUEUED','READY','CAPTURED','LINKED',*TERMINAL): continue
            # In flight to drop-off only. After accept/return the bird roosts; the parcel stays in inventory.
            if a['state']=='OFFERED': busy[bird]=a
        return busy

    def _event(self, db, task, kind, actor, detail=None, source=None, destination=None):
        body={'taskId':task['taskId'],'taskVersion':task['taskVersion'],'kind':kind,'actor':actor,
              'at':self.clock(),'detail':detail or {},'source':source,'destination':destination,
              'assignmentId':task.get('assignmentId'),'ruleVersion':RULE_VERSION}
        seq=db.execute('INSERT INTO taskflow_events(task_id,body) VALUES(?,?)',(task['taskId'],encoded(body))).lastrowid
        if task.get('ownerId'):
            db.execute('INSERT INTO taskflow_notifications VALUES(?,?,?,NULL)',(seq,task['ownerId'],task['taskId']))
        return seq

    def _lane(self, project, lane):
        if not self.flow:
            raise Conflict('Workflow context unavailable')
        state=self.flow.read()
        lanes=self.flow.workspace.read()['lanes']
        from workflow_layout import active
        found=next((x for x in (state.get('laneCatalog') or lanes) if x['id']==lane and x['projectId']==project),None)
        if not found or not active(found):
            raise Conflict('The selected lane is unavailable or closed')
        return state

    def policy(self, lane):
        with self.connect() as db:
            return self._one(db,'taskflow_policies',lane,'lane')

    def save_policy(self, item, actor='operator-ui'):
        if actor!='operator-ui':
            raise ValueError('Only the operator configures dispatch rules')
        project=text(item.get('projectId'),'project'); lane=text(item.get('laneId'),'lane')
        self._lane(project,lane)
        maximum=item.get('maxWorkers',25)
        if type(maximum) is not int or not 1<=maximum<=25:
            raise ValueError('Worker limit must be between 1 and 25')
        caps=item.get('capabilities',['general'])
        if not isinstance(caps,list) or not caps or len(caps)>20:
            raise ValueError('Choose supported capabilities')
        caps=[text(c,'capability',80) for c in caps]
        minutes=item.get('maxMinutes',30)
        if type(minutes) is not int or not 1<=minutes<=120:
            raise ValueError('Runtime limit must be 1–120 minutes')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE'); old=self._one(db,'taskflow_policies',lane,'lane')
            if item.get('version',0)!=(old or {}).get('version',0):raise Conflict('Dispatch rules changed; refresh')
            attendant=None
            if item.get('attendantId'):
                attendant_id=text(item['attendantId'],'Task Board attendant',200)
                seat=next((s for s in self.flow.read()['placements'] if s['agentId']==attendant_id and s['laneId']==lane),None)
                if not seat or seat['role']=='auditor' or not any(s['agent_id']==attendant_id for s in self.flow.catalog()['sessions']):
                    raise ValueError('Choose an existing non-auditor attendant in this lane')
                attendant={'agentId':attendant_id,'role':seat['role'],'teamId':seat.get('teamId') or seat['role']}
            # Settings forms may omit the finite grant. Preserve its exact
            # identity, expiry and ceilings; omission never renews or clears it.
            execution=item['execution'] if 'execution' in item else (old or {}).get('execution')
            if execution is not None:
                from execution_admission import validate
                execution=validate(execution)
                owner=next((s for s in self.flow.read()['placements'] if s['agentId']==execution['parentId'] and s['laneId']==lane and s['role']=='coordinator'),None)
                if not owner:raise Conflict('Finite policy must bind the saved original lane owner')
            p={'execution':execution,'projectId':project,'laneId':lane,'version':(old or {}).get('version',0)+1,
               'enabled':item.get('enabled') is True,'autoReady':item.get('autoReady') is True,
               'allowManagedStarts':item.get('allowManagedStarts') is True,'capabilities':caps,
               'scopes':paths(item.get('scopes',[])),'maxWorkers':maximum,'maxMinutes':minutes,
               'maxIdleBirds':5,'attendant':attendant,'approvedBy':actor,'at':self.clock(),'ruleVersion':RULE_VERSION}
            db.execute('INSERT OR REPLACE INTO taskflow_policies VALUES(?,?)',(lane,encoded(p)))
        return p

    def capture(self, item, actor='operator-ui', _task_id=None):
        request=text(item.get('request'),'request',64000)
        project=text(item.get('projectId'),'project'); lane=text(item.get('laneId'),'lane');self._lane(project,lane)
        key=text(item.get('key'),'submission key',200)
        fingerprint=digest({**{k:item.get(k) for k in ['request','projectId','laneId','inputs','ownerId','sourceRefs','delivery']},**({'pidTask':item['pidTask']} if 'pidTask' in item else {})})
        refs=item.get('sourceRefs',[])
        if not isinstance(refs,list) or len(refs)>50:raise ValueError('Invalid source references')
        if len(encoded(refs))>16000:raise ValueError('Source references are too large')
        if item.get('ownerId'):text(item['ownerId'],'owner identity',200)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM taskflow_keys WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Conflict('Submission key already belongs to different content')
                return self._one(db,'taskflow_tasks',old['task_id'])
            from taskflow_delivery import contract
            delivery=contract(item['delivery']) if item.get('delivery') is not None else None
            inputs=pin_files(item.get('inputs',[]))
            policy=self._one(db,'taskflow_policies',lane,'lane')
            ready=bool(policy and policy['enabled'] and policy['autoReady'] and not item.get('requireOwnerReadiness'))
            if _task_id and self._one(db,'taskflow_tasks',_task_id):raise Conflict('Original task ID already has another intake record')
            t={'taskId':_task_id or str(uuid.uuid4()),'version':0,'taskVersion':1,'request':request,'title':request.splitlines()[0][:160],
               'projectId':project,'laneId':lane,'capturedBy':actor,'ownerId':item.get('ownerId') or None,
               'state':'READY' if ready else 'CAPTURED','waitReason':'' if ready else 'Dispatch setup or a scoped readiness decision is required',
               'definitionOfDone':request if ready else '', 'capabilities':policy['capabilities'][:1] if ready else [],
               'scopes':policy['scopes'] if ready else [],'dependencies':[],'inputs':inputs,'sourceRefs':refs,
               'amendments':[],'result':None,'delivery':delivery,'assignmentId':None,'station':'board:'+lane,'createdAt':self.clock(),
               'policyVersion':policy['version'] if ready else None,'ruleVersion':RULE_VERSION}
            if 'pidTask' in item:
                raise Conflict('PID execution is not included in this release')
            self._put(db,t);self._event(db,t,'captured',actor,{'request':request,'inputs':inputs,'sourceRefs':refs},destination=t['station'])
            db.execute('INSERT INTO taskflow_keys VALUES(?,?,?)',(key,fingerprint,t['taskId']))
            return t

    def get(self, task_id):
        with self.connect() as db:
            t=self._one(db,'taskflow_tasks',task_id)
            if t:return self.presentation(t,db)
        return next((t for t in self.legacy() if t['taskId']==task_id),None)

    def presentation(self,t,db):
        if t['state']=='RUNNING':
            a=self._one(db,'taskflow_assignments',t.get('assignmentId'))
            fresh=bool(a and (a.get('providerTurnId') or a.get('executionTransport')=='local-pid' and a.get('executionEvidence',{}).get('runnerPid')) and a.get('executionEvidence') and 0<=self.clock()-a['heartbeatAt']<=30)
            t={**t,'displayState':'RUNNING' if fresh else 'STATUS_UNKNOWN','executionFresh':fresh}
        # In flight (OFFERED) the bird carries the parcel. Inventory starts at accept/drop-off.
        t={**t,'heldByWorker':t.get('state') in ('ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING')}
        if t.get('state')=='RESERVED' and not t.get('assignmentId'):
            t={**t,'station':'board:'+str(t.get('laneId')),'heldByWorker':False}
        return t

    def legacy(self):
        if not self.flow:return []
        state=self.flow.read(); workspace=self.flow.workspace.read();lanes=workspace['lanes']; lane_map={l['id']:l['projectId'] for l in lanes}
        lane_map.update({l['id']:l['projectId'] for l in state.get('laneCatalog') or []})
        seats={p['agentId']:p['laneId'] for p in state['placements']}
        links={x['taskId']:x['laneId'] for x in workspace.get('taskLinks',[])}
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():return []
            records=[dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY task_id')]
            live=set()
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='taskflow_assignments'").fetchone():
                live={r[0] for r in db.execute('SELECT task_id FROM taskflow_assignments WHERE state IN ('+','.join('?' for _ in ACTIVE)+')',ACTIVE)}
        result=[]
        for t in records:
            progress=legacy_progress(t.get('progress_json'));aliases=json.loads(t.get('aliases_json') or '[]')
            linked=[value for value in aliases if value in lane_map]
            lane=links.get(t['task_id']) or (linked[0] if len(linked)==1 else None) or seats.get(t['owner'])
            t={**{'version':0,'scope_json':'[]','criteria_json':'[]','dependencies_json':'[]','created_at':None,'updated_at':None},**t}
            # Idle desks are not a parking lot for retained contracts. Only a live
            # TaskFlow assignment may sit on a person. Everything else belongs on the board.
            worker=progress.get('assignedWorker') or ((progress.get('workerAcceptance') or {}).get('worker') if progress.get('implementationAccepted') else None)
            responsible=worker or t['owner']
            live_run=t['task_id'] in live
            station=responsible if live_run and responsible in seats else 'board:'+str(lane)
            mapped={'VERIFIED_COMPLETE':'DONE','ACKNOWLEDGED':'QUEUED','REPORTED_DONE':'REVIEW'}
            if t['status']=='WORKING': mapped_state='STATUS_UNKNOWN' if live_run else 'QUEUED'
            else: mapped_state=mapped.get(t['status'],t['status'])
            result.append({'taskId':t['task_id'],'legacy':True,'title':t['objective'].splitlines()[0][:160],
                'request':(progress.get('sourceRequest') or {}).get('text') or t['objective'],'state':mapped_state,
                'legacyStatus':t['status'],'waitReason':t['blocker'] or t['next_action'],'version':t['version'],'taskVersion':1,
                'ownerId':t['owner'],'capturedBy':t['owner'],'workerId':worker,
                'laneId':lane,'projectId':lane_map.get(lane),'createdAt':t['created_at'],'updatedAt':t['updated_at'],
                'scopes':json.loads(t['scope_json']),'dependencies':json.loads(t['dependencies_json']),
                'definitionOfDone':progress.get('acceptanceCriteria') or json.loads(t['criteria_json']),'inputs':[],
                'sourceRefs':([progress['sourceRequest']] if progress.get('sourceRequest') else [])+[{'taskId':i,'relationship':'canonical implementation'} for i in progress.get('canonicalImplementationTaskIds',[])],
                'station':station,'locationBasis':'Live TaskFlow assignment' if live_run else 'Task board · retained contract, not live execution',
                'heldByWorker':live_run,'progress':progress,'assignmentId':None,'result':None,'ruleVersion':'legacy-contract'})
        from taskflow_intake import disposition
        records={row['taskId']:row for row in result}
        for row in result:
            row['intakeDisposition']=disposition(row,records)
            if row['progress'].get('legacyProgressError'):
                row['intakeDisposition']={'kind':'invalid-retained-record','executable':False,'childTaskIds':[],
                                         'reason':row['progress']['legacyProgressError']}
            if not row['intakeDisposition']['executable']:
                row['waitReason']=row['intakeDisposition']['reason']+' '+(row['waitReason'] or '')
        return result

    def list(self, project=None, lane=None):
        with self.connect() as db:rows=[self.presentation(json.loads(r[0]),db) for r in db.execute('SELECT body FROM taskflow_tasks')]
        own={r['taskId'] for r in rows};rows += [r for r in self.legacy() if r['taskId'] not in own]
        return [r for r in rows if (not project or r['projectId']==project) and (not lane or r['laneId']==lane)]

    def detail(self, task_id):
        task=self.get(task_id)
        if not task:raise ValueError('Task not found')
        with self.connect() as db:
            events=[dict(json.loads(r['body']),seq=r['seq']) for r in db.execute('SELECT seq,body FROM taskflow_events WHERE task_id=? ORDER BY seq',(task_id,))]
            assignments=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_assignments WHERE task_id=? ORDER BY rowid',(task_id,))]
            if (task.get('legacy') or task.get('intake')) and db.execute("SELECT 1 FROM sqlite_master WHERE name='task_events'").fetchone():
                events += [{'seq':r['seq'],'kind':'legacy-contract-update','actor':r['actor'],'at':r['created_at'],'detail':json.loads(r['payload_json'])} for r in db.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY seq',(task_id,))]
        # Authentication material is never part of the browser or task payload.
        for a in assignments:
            a.pop('receiptSecret',None);a.pop('supervisorClaim',None)
        from taskflow_intake import linked_tasks
        from taskflow_intake import timestamp
        events.sort(key=lambda e:(timestamp(e.get('at'),0),e['seq']))
        return {'task':task,'events':events,'assignments':assignments,'linkedTasks':linked_tasks(self,task),'audit':self.audit.detail(task_id),'deliveries':self.delivery.detail(task_id),'annotations':self.annotations.detail(task_id),'ruleVersion':RULE_VERSION}

    def adopt_intake(self):
        from taskflow_intake import reconcile
        return reconcile(self)

    def ready(self, task_id, version, spec, actor='operator-ui'):
        self.adopt_intake()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self._one(db,'taskflow_tasks',task_id)
            return self._ready_in_db(db,t,task_id,version,spec,actor)

    def _is_attendant(self, policy, actor):
        """A saved readiness grant follows its exact seat, never just a name."""
        grant=policy.get('attendant')
        if not grant or grant['agentId']!=actor:return False
        state=self.flow.read()
        if not state.get('enabled'):return False
        return any(s['agentId']==actor and s['laneId']==policy['laneId'] and s['role']==grant['role']
                   and (s.get('teamId') or s['role'])==grant['teamId'] for s in state['placements'])

    def _ready_in_db(self,db,t,task_id,version,spec,actor):
        if not t:raise Conflict('This is an execution contract, not a task intake record; retain its existing worker')
        if t.get('intake',{}).get('canonicalTaskIds'):raise Conflict('Continue the linked canonical implementation; do not dispatch a duplicate task')
        if t.get('intake',{}).get('reservedWorkerId'):raise Conflict('An existing worker accepted this task; reconcile that reservation before new dispatch')
        if t['version']!=version:raise Conflict('Task changed; refresh')
        if t['state'] in (*ACTIVE,*STAGES) or t['state'] in TERMINAL:raise Conflict('An active or terminal task cannot be silently reassigned')
        if self.audit.current(db,task_id):raise Conflict('Finish or release the active audit before changing the next step')
        self._lane(t['projectId'],t['laneId']);p=self._one(db,'taskflow_policies',t['laneId'],'lane')
        if not p or not p['enabled']:raise Conflict('Enable scoped dispatch rules first')
        is_worker=actor in (t.get('workerId'),t.get('originalWorkerId')) and actor is not None
        is_attendant=self._is_attendant(p,actor)
        if actor not in ('operator-ui',t.get('ownerId')) and not is_worker and not is_attendant:raise ValueError('Only the responsible worker, owner, appointed attendant or operator may resolve scope')
        if is_worker and actor not in ('operator-ui',t.get('ownerId')):
            if any(not inside(path,t['scopes']) for path in paths(spec.get('scopes',t['scopes']))) or not set(spec.get('capabilities',t['capabilities']))<=set(t['capabilities']):raise Conflict('A worker next step cannot expand approved source scope or capabilities')
            t.setdefault('requiredOutcome',t['definitionOfDone'])
        scopes=paths(spec.get('scopes',t['scopes']))
        if scopes and any(not inside(s,p['scopes']) for s in scopes):raise Conflict('Task scopes exceed the approved lane scope')
        caps=spec.get('capabilities',t['capabilities'])
        if not isinstance(caps,list) or not caps or not set(caps)<=set(p['capabilities']):raise ValueError('Unsupported task capabilities')
        deps=spec.get('dependencies',t['dependencies'])
        if not isinstance(deps,list) or len(deps)>30 or task_id in deps:raise ValueError('Invalid task dependencies')
        for dep in deps:
            d=self._one(db,'taskflow_tasks',dep) or self.get(dep)
            if not d or d['projectId']!=t['projectId']:raise ValueError('Dependency must be a known task in this project')
            if self._depends(db,dep,task_id):raise ValueError('Dependency cycle')
        if 'correctionWorkerId' in spec:
            if not t.get('correctionWorkerId') or actor not in ('operator-ui',t.get('ownerId')):
                raise ValueError('Only the operator or task owner may reassign a queued correction')
            worker=self._one(db,'taskflow_workers',spec['correctionWorkerId'])
            if not worker or not worker['enabled'] or (worker['projectId'],worker['laneId'])!=(t['projectId'],t['laneId']):
                raise Conflict('Choose an enabled worker enrolled in this task lane')
            if any(not inside(s,worker['scopes']) for s in scopes) or not set(caps)<=set(worker['capabilities']):
                raise Conflict('Correction worker must own the required source scope and capabilities')
            t['correctionWorkerId']=worker['workerId']
        source=t['station']
        t.update(definitionOfDone=text(spec.get('definitionOfDone',t['definitionOfDone']),'required outcome'),
                 capabilities=caps,scopes=scopes,dependencies=deps,state='READY',station='board:'+t['laneId'],waitReason='',policyVersion=p['version'])
        if 'pidTask' in spec or t.get('pidTask') is not None:
            raise Conflict('PID execution is not included in this release')
        self._put(db,t);self._event(db,t,'ready',actor,{'scope':scopes,'definitionOfDone':t['definitionOfDone'],'correctionWorkerId':t.get('correctionWorkerId')},source=source,destination=t['station']);return t


    def _depends(self,db,start,target,seen=None):
        seen=set() if seen is None else seen
        if start==target:return True
        if start in seen:return False
        seen.add(start);t=self._one(db,'taskflow_tasks',start)
        return bool(t and any(self._depends(db,x,target,seen) for x in t['dependencies']))

    def amend(self,task_id,version,body,actor='operator-ui'):
        body=text(body,'amendment',32000)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self._one(db,'taskflow_tasks',task_id)
            if not t or t['version']!=version:raise Conflict('Task changed or is a legacy contract')
            if t['state'] in TERMINAL:raise Conflict('Capture a linked follow-up for a completed task')
            t['amendments'].append({'text':body,'actor':actor,'at':self.clock(),'version':t['taskVersion']+1});t['taskVersion']+=1
            if t['state'] in (*ACTIVE,*STAGES):
                t['waitReason']='New instructions await reconciliation with the active assignment'
            else:t.update(state='NEEDS_COORDINATION',waitReason='Review the revised task scope')
            self._put(db,t);self._event(db,t,'amended',actor,{'text':body});return t

    def enroll(self,item,actor='operator-ui'):
        if actor!='operator-ui':raise ValueError('Only the operator enrolls exact workers')
        aid=text(item.get('workerId'),'worker'); lane=text(item.get('laneId'),'lane');project=text(item.get('projectId'),'project')
        state=self._lane(project,lane);s=next((s for s in self.flow.catalog()['sessions'] if s['agent_id']==aid),None)
        if not s:raise ValueError('Choose an existing exact session')
        seat=next((p for p in state['placements'] if p['agentId']==aid),None)
        if not seat or seat['laneId']!=lane:raise Conflict('Worker must have a saved seat in the selected lane')
        purpose=item.get('purpose','work')
        if purpose not in ('work','audit','delivery'):raise ValueError('Choose exact enrollment purpose')
        if (purpose=='audit' and seat['role']!='auditor') or (purpose!='audit' and seat['role'] not in ('worker','writer','researcher')):raise ValueError('Saved role does not permit this enrollment purpose')
        mode=item.get('mode','cooperative')
        if mode not in ('cooperative','managed'):raise ValueError('Unsupported worker adapter')
        if mode=='managed' and (not s.get('managed') or not s.get('endpoint') or s['endpoint']==aid):
            raise Conflict('Automatic starts require an existing Switchboard-managed session; imported sessions use safe-boundary pickup')
        p=self.policy(lane)
        if not p:raise Conflict('Configure lane dispatch first')
        scopes=paths(item.get('scopes',[]));caps=item.get('capabilities',['general'])
        if not isinstance(caps,list) or not caps or not set(caps)<=set(p['capabilities']):raise ValueError('Capabilities exceed lane rules')
        if any(not inside(s,p['scopes']) for s in scopes):raise ValueError('Worker scope exceeds lane rules')
        w={'purpose':purpose,'workerId':aid,'projectId':project,'laneId':lane,'name':s['title'],'capabilities':caps,'scopes':scopes,
           'mode':mode,'enabled':True,'enrolledAt':self.clock(),'approvedBy':actor,'model':s.get('model'),
           'providerThreadId':s.get('endpoint'),'effort':s.get('reasoning') or 'ultra','role':seat['role'],'teamId':seat.get('teamId') or seat['role'],'idleAt':None,'ruleVersion':RULE_VERSION}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=self._one(db,'taskflow_workers',aid)
            if db.execute('SELECT 1 FROM taskflow_assignments WHERE worker=? AND state IN ('+','.join('?' for _ in ACTIVE)+')',(aid,*ACTIVE)).fetchone():raise Conflict('Worker has an active or uncertain assignment')
            count=sum(1 for r in db.execute('SELECT body FROM taskflow_workers') if (x:=json.loads(r[0]))['laneId']==lane and x['enabled'] and x['workerId']!=aid)
            if count>=p['maxWorkers']:raise Conflict('Room worker limit reached')
            db.execute('INSERT OR REPLACE INTO taskflow_workers VALUES(?,?)',(aid,encoded(w)))
        return w

    def idle(self,worker_id,actor):
        if actor!=worker_id:raise ValueError('Worker must announce its own safe-boundary availability')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');w=self._one(db,'taskflow_workers',worker_id)
            if not w or not w['enabled']:raise Conflict('Worker is not enrolled')
            if self.delivery.active(db) and any(a['actor']==worker_id for a in self.delivery.active(db)):raise Conflict('Worker still owns a delivery')
            if db.execute('SELECT 1 FROM taskflow_assignments WHERE worker=? AND state IN ('+','.join('?' for _ in ACTIVE)+')',(worker_id,*ACTIVE)).fetchone():raise Conflict('Worker still owns an assignment')
            w.update(idleAt=self.clock(),ruleVersion=RULE_VERSION);db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),worker_id));return w

    def _pickup_blocker(self,t,w,p,state,assignment=None):
        """Reject impossible work before settings or provider inventory I/O.

        An empty reason is not admission. _eligible and the reservation still
        check identity, capacity, task binding, source custody and current rules.
        Do not filter on model defaults here: _future_worker resolves them later.
        """
        if w.get('purpose','work')!='work':return 'Worker enrollment is reserved for another phase'
        if self.recovery_hold:return 'Restored board copy is disarmed; reconcile with the original instance before execution'
        if not p or not p['enabled']:return 'Dispatch is paused'
        if not state.get('enabled'):return 'Workflow dispatch is disabled'
        if not w['enabled'] or w['laneId']!=t['laneId'] or w['projectId']!=t['projectId']:return 'Worker is not enrolled for this lane'
        if not assignment and (w.get('idleAt') is None or self.clock()-w['idleAt']>60):return 'Worker availability is stale or unknown'
        if w['mode']=='managed':
            if not p['allowManagedStarts']:return 'Automatic managed starts are disabled'
            from execution_admission import validate
            try:limits=validate(p.get('execution'))
            except ValueError as exc:return str(exc)
            now=self.clock()
            if now>=limits['expiresAt']:return 'Execution policy expired; no renewal inferred'
            if now<limits['periodStart']:return 'Execution policy has not started'
        return ''

    def _eligible(self,db,t,w,p,state,assignment=None):
        if assignment and assignment.get('phase') in ('audit','verify','delivery'):
            return self.cycle.eligible(db,t,w,p,state,assignment)
        blocked=self._pickup_blocker(t,w,p,state,assignment)
        if blocked:return blocked
        if t.get('pidTask') is not None:
            return 'PID execution is not included in this release'
        if w['mode']=='managed':
            from execution_admission import parent_current
            if not parent_current(state,p):return 'Original setup owner binding changed; policy does not survive a role handoff'
            if t.get('ownerId')!=((p or {}).get('execution') or {}).get('parentId'):return 'Task owner is outside the finite parent policy'
            try:
                from execution_admission import Admission
                Admission(self.root,self.clock).check(db,p,w,(assignment or {}).get('maxMinutes',min(p['maxMinutes'],w.get('configuredMinutes') or p['maxMinutes']) if p else None), 'work',(assignment or {}).get('id'),t['taskId'])
            except (ValueError,Conflict) as exc:return str(exc)
            session=next((x for x in self.flow.catalog()['sessions'] if x['agent_id']==w['workerId']),None)
            if not session or not session.get('managed') or session.get('endpoint')!=w['providerThreadId']:return 'Original managed identity changed'
        if t.get('intakeDisposition',{}).get('executable') is False:
            return t['intakeDisposition']['reason']
        if t.get('correctionWorkerId') and t['correctionWorkerId']!=w['workerId']:return 'Correction is queued for its responsible worker'
        if t.get('intake',{}).get('canonicalTaskIds'):return 'Existing canonical implementation owns this request'
        if t.get('intake',{}).get('reservedWorkerId'):return 'An existing worker accepted this task; retain its reservation'
        if any(a['actor']==w['workerId'] for a in self.delivery.active(db)):return 'Worker retains an active delivery'
        if any(overlap(t['scopes'],[a['payload']['delivery']['destination']['path']]) for a in self.delivery.active(db)):return 'An active delivery retains overlapping destination source scope'
        if db.execute("SELECT 1 FROM taskflow_audit_claims WHERE reviewer=? AND state='CLAIMED'",(w['workerId'],)).fetchone():return 'Worker retains an active independent audit'
        if not assignment and db.execute('SELECT 1 FROM taskflow_assignments WHERE worker=? AND state IN ('+','.join('?' for _ in ACTIVE)+')',(w['workerId'],*ACTIVE)).fetchone():return 'Worker is busy with an active or uncertain assignment; correction stays queued' if t.get('correctionWorkerId') else 'Worker already owns another task'
        seat=next((x for x in state['placements'] if x['agentId']==w['workerId']),None)
        if not seat or seat['laneId']!=t['laneId']:return 'Worker placement changed'
        if seat['role']!=w['role'] or (seat.get('teamId') or seat['role'])!=w['teamId']:return 'Worker role or team changed; renew exact enrollment'
        if not set(t['capabilities'])<=set(w['capabilities']):return 'Worker lacks required capability'
        if not set(t['capabilities'])<=set(p['capabilities']):return 'Task capability is no longer permitted'
        if any(not inside(s,w['scopes']) for s in t['scopes']):return 'Worker does not own the required source scope'
        if any(not inside(s,p['scopes']) for s in t['scopes']):return 'Task source scope is no longer permitted'
        if t['policyVersion']!=p['version']:return 'Dispatch rules changed; review task readiness'
        # The board endpoint is explicitly enrolled per lane. Explicit pair blocks
        # between consecutive workers still override eligible same-lane handoffs.
        previous=t.get('previousWorkerId') or t.get('workerId')
        if previous and previous!=w['workerId']:
            from workflow import allowed
            if not allowed(state,previous,w['workerId']):return 'The worker handoff connection is blocked'
        for dep in t['dependencies']:
            d=self._one(db,'taskflow_tasks',dep) or self.get(dep)
            if not d or d['state']!='DONE':return 'Waiting for dependency '+dep
        for row in db.execute('SELECT body FROM taskflow_assignments WHERE state IN ('+','.join('?' for _ in ACTIVE)+')',ACTIVE):
            a=json.loads(row[0])
            if assignment and a['id']==assignment['id']:continue
            if a['workerId']==w['workerId']:return 'Worker already owns another task'
            if overlap(t['scopes'],a['payload']['scopes']):return 'Another worker owns an overlapping source scope'
        # Legacy task contracts are still authoritative source claims. They are
        # not migrated or silently treated as idle because an owner changed seats.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
            for row in db.execute("SELECT * FROM tasks WHERE status='WORKING'"):
                old=dict(row)
                if old['owner']==w['workerId']:return 'Worker has an existing active task contract'
                if overlap(t['scopes'],json.loads(old.get('scope_json') or '[]')):return 'An existing task owns an overlapping source scope'
        production=self.root/'runtime/production.sqlite3'
        if production.exists():
            from store import query_only_existing
            with query_only_existing(production) as other:
                for row in other.execute("SELECT agent_id,body FROM work_orders WHERE status IN ('STARTING','RUNNING','CANCELLING','UNCERTAIN')"):
                    order=json.loads(row[1])
                    if row[0]==w['workerId']:return 'Worker has an active managed work order'
                    if overlap(t['scopes'],[order['spec']['workspace']]):return 'A managed work order owns an overlapping source scope'
        from taskflow_delivery import payload_files
        for src in payload_files({**t,**self._history_payload(db,t)}):
            try:
                pth=Path(src['path'])
                if pth.resolve()!=pth or not pth.is_file() or hashlib.sha256(pth.read_bytes()).hexdigest()!=src['sha256']:return 'A pinned input changed'
            except OSError as exc:
                return 'A pinned input is unavailable: '+str(src['path'])+' ('+type(exc).__name__+', errno '+str(exc.errno)+'). Restore file access for the board service before dispatch.'
        if t.get('learningRequired'):
            from taskflow_decisions import check
            try:check(self,db,t,w,assignment)
            except (ValueError,Conflict,OSError) as exc:return 'Learning decision held: '+str(exc)
        return ''

    def _history_payload(self,db,t):
        return {'priorDeliveries':t.get('deliveryHistory',[]),
                'priorResults':[a['result'] for a in self._past(db,t['taskId']) if a.get('result')],
                'priorReviews':self._reviews(db,t['taskId']),
                'historicalIntegrity':t.get('historicalIntegrity',[])}

    def _future_worker(self,w):
        """Snapshot defaults only when PREPARING a new assignment, never in a
        running supervisor. Existing enrollment purpose/scope stays unchanged.
        """
        if w['mode']!='managed' or not (self.root/'runtime/production.sqlite3').is_file():return w
        from agent_settings import read_defaults
        saved=read_defaults(self.root,w['workerId']);values=saved['values']
        return {**w,'model':values.get('model') or w['model'],'effort':values.get('effort') or w['effort'],
                'settingsVersion':saved['version'],'configuredMinutes':values.get('minutes'),
                'personalContext':values.get('personalContext') or '', 'expectedArtifacts':values.get('artifacts') or []}

    def _reconcile_history(self,db,t):
        # Only an already-authorized delivery correction without live custody
        # may admit unavailable history as explicitly untrusted repair context.
        if not t.get('correctionWorkerId') or not t.get('deliveryHistory') or self.delivery.active(db,t['taskId']):return
        if any(a['state'] in ACTIVE for a in self._past(db,t['taskId'])) or self.audit.current(db,t['taskId']):return
        from taskflow_delivery import historical_files,file_integrity
        current={(f['path'],f['sha256']) for f in [*t['inputs'],*t.get('intake',{}).get('inputs',[])]}
        known={(r['file']['path'],r['file']['sha256']) for r in t.get('historicalIntegrity',[])}
        added=[]
        for source in historical_files(self._history_payload(db,t)):
            key=(source['path'],source['sha256'])
            if key in current or key in known:continue
            status=file_integrity(source)
            if status['trusted']:continue
            added.append({'taskId':t['taskId'],'file':source,**status,'at':self.clock(),
                          'reason':'Invalid historical evidence retained for same-task correction; never use these bytes as approved evidence.'})
            known.add(key)
        if added:
            t.setdefault('historicalIntegrity',[]).extend(added);t['taskVersion']+=1
            self._put(db,t);self._event(db,t,'historical-evidence-invalidated','task-board',{'files':added})

    def dispatch(self,worker_id=None,*,managed=False):
        # Cooperative CLI readiness may run under different macOS permissions.
        # Only the installed clock opts into managed reservations, so the input
        # reads in _eligible run in the service that will deliver the parcel.
        # This is routing, not a security boundary against arbitrary local code.
        if not self.flow:return []
        self.adopt_intake()
        state=self.flow.read();offered=[]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            workers=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_workers ORDER BY id')]
            if worker_id:workers=[w for w in workers if w['workerId']==worker_id]
            tasks=sorted([json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_tasks')],key=lambda t:(t['createdAt'],t['taskId']))
            for t in tasks:
                if t['state'] not in ('READY','QUEUED'):continue
                self._reconcile_history(db,t)
                try:self._lane(t['projectId'],t['laneId'])
                except Conflict:continue
                p=self._one(db,'taskflow_policies',t['laneId'],'lane');reason='No enrolled available worker matches this task'
                available_workers=workers
                if t.get('correctionWorkerId'):
                    available_workers=[w for w in workers if w['workerId']==t['correctionWorkerId']]
                    reason='Correction queued: responsible worker is unavailable or not enrolled'
                occupied={bird for bird in self._busy_couriers(db)}
                bird=next(('bird-'+str(i) for i in range(1,6) if 'bird-'+str(i) not in occupied),None)
                if not bird:
                    # No input read happened on this path, even in the service.
                    # Courier capacity cannot clear an unresolved access hold.
                    if t.get('waitReason','').startswith('A pinned input '):continue
                    if t['waitReason']!='All five couriers are delivering tasks':
                        t.update(state='QUEUED',waitReason='All five couriers are delivering tasks');self._put(db,t)
                    continue
                input_failure=False
                for w in available_workers:
                    if input_failure:continue
                    if w['mode']=='managed' and not managed:
                        reason='Waiting for the board service to verify inputs before reserving managed work'
                        continue
                    problem=self._pickup_blocker(t,w,p,state)
                    if problem:
                        reason=problem
                        continue
                    try:w=self._future_worker(w)
                    except (ValueError,OSError) as exc:reason='Future settings unavailable: '+str(exc);continue
                    problem=self._eligible(db,t,w,p,state)
                    if problem:
                        reason=problem
                        # Input failure belongs to the parcel. Another worker's
                        # generic mismatch must not overwrite its useful cause.
                        if problem.startswith('A pinned input '):input_failure=True
                        continue
                    aid=str(uuid.uuid4());payload={k:t[k] for k in ['taskId','taskVersion','request','amendments','definitionOfDone','scopes','dependencies','inputs','sourceRefs']}
                    payload['expectedArtifacts']=t.get('expectedArtifacts') or w.get('expectedArtifacts',[])
                    if t.get('pidTask') is not None:payload['pidTask']=copy.deepcopy(t['pidTask'])
                    payload['agentContext']=w.get('personalContext','')
                    payload['delivery']=t.get('delivery');payload.update(self._history_payload(db,t))
                    if t.get('intake'):payload['intake']=t['intake']
                    payload['requiredOutcome']=t.get('requiredOutcome') or t['definitionOfDone'];payload['returnTo']=t.get('originalWorkerId') or w['workerId'];payload['ruleVersion']=RULE_VERSION
                    if t.get('learningRequired'):
                        from taskflow_decisions import check
                        decision,_=check(self,db,t,w)
                        payload['learningDecision']={'request':decision['request'],'result':decision['result']}
                    a={'id':aid,'taskId':t['taskId'],'workerId':w['workerId'],'state':'OFFERED','taskVersion':t['taskVersion'],
                       'policyVersion':p['version'],'payload':payload,'payloadHash':digest(payload),'at':self.clock(),'heartbeatAt':self.clock(),
                       'courierId':bird,'sourceStation':t['station'],'mode':w['mode'],'receiptSecret':secrets.token_hex(24),
                       'workdir':str(self.root/'runtime/taskflow-work'/aid),'maxMinutes':min(p['maxMinutes'],w.get('configuredMinutes') or p['maxMinutes']),'result':None,'providerTurnId':None}
                    a.update(model=w['model'],effort=w['effort'],settingsVersion=w.get('settingsVersion'))
                    if w['mode']=='managed':
                        from execution_admission import Admission
                        Admission(self.root,self.clock).reserve(db,p,w,a['maxMinutes'],'work',aid,t['taskId'])
                    from taskflow_decisions import admit
                    admit(self,db,t,w,a)
                    self._assignment(db,a);src=t['station'];t.update(assignmentId=aid,state='OFFERED',waitReason='',workerId=w['workerId'],station='transit:'+aid,courierId=bird)
                    w['idleAt']=None;db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),w['workerId']))
                    t.setdefault('originalWorkerId',w['workerId']);self._put(db,t)
                    self._event(db,t,'offered','task-board',{'payloadHash':a['payloadHash'],'courierId':a['courierId']},source=src,destination=w['workerId']);offered.append(a);break
                else:
                    if not managed and t.get('waitReason','').startswith('A pinned input '):
                        # A caller with broader permissions cannot clear the
                        # service's hold; the next service tick rechecks it.
                        reason=t['waitReason']
                    if t['state']!='QUEUED' or t['waitReason']!=reason:
                        t.update(state='QUEUED',waitReason=reason);self._put(db,t)
                        if reason.startswith('A pinned input '):
                            self._event(db,t,'input-access-held','task-board',{'reason':reason,'reservationCreated':False})
            return offered

    def _past(self,db,task_id):
        return [a for r in db.execute('SELECT body FROM taskflow_assignments WHERE task_id=? ORDER BY rowid',(task_id,)) if (a:=json.loads(r[0])).get('phase','work')=='work']

    def _reviews(self,db,task_id):
        """Immutable verdicts travel with the same parcel on every later trip."""
        return [event['detail'] for row in db.execute('SELECT body FROM taskflow_events WHERE task_id=? ORDER BY seq',(task_id,))
                if (event:=json.loads(row[0])).get('kind') in ('review-accept','review-revise','review-fail')]

    def offers(self,worker_id):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body FROM taskflow_assignments WHERE worker=? AND state='OFFERED'",(worker_id,))]

    def active(self,worker_id=None):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_assignments WHERE state IN ('+','.join('?' for _ in ACTIVE)+')',ACTIVE) if not worker_id or json.loads(r[0])['workerId']==worker_id]

    def context(self,actor):
        with self.connect() as db:
            worker=self._one(db,'taskflow_workers',actor)
            notes=[dict(json.loads(r['body']),seq=r['seq']) for r in db.execute('SELECT e.body,e.seq FROM taskflow_notifications n JOIN taskflow_events e ON e.seq=n.seq WHERE n.owner=? AND n.read_at IS NULL ORDER BY e.seq LIMIT 100',(actor,))]
            policies=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_policies')]
        attendant={'lanes':[{k:p[k] for k in ('projectId','laneId','version','enabled','capabilities','scopes')} for p in policies if self._is_attendant(p,actor)],
                   'command':'taskflowctl.py ready --file READY.json',
                   'instructions':'Use the exact current taskId and version, required outcome, capability, source scopes and dependencies. Readiness stays inside the saved lane policy; dispatch chooses the eligible worker. A linked canonical task must not be duplicated.'}
        assignments=self.active(actor)
        for a in assignments:
            a.pop('receiptSecret',None);a.pop('supervisorClaim',None);a['currentTask']=self.get(a['taskId'])
            a['instructionsChanged']=a['currentTask']['taskVersion']!=a['taskVersion']
            a['dispatchPolicy']=self.policy(a['currentTask']['laneId'])
            a['evidenceNotes']=self.annotations.detail(a['taskId'])['notes']
        return {'worker':worker,'attendant':attendant,'assignments':assignments,'ownerUpdates':notes,'audit':self.audit.context(actor),'delivery':self.delivery.context(actor),'ruleVersion':RULE_VERSION,
                'instructions':'Owners coordinate scope, dependencies, integration and exceptions; routine enrolled delivery needs no owner acknowledgment. Imported workers only pick up at their own safe boundary. Read and hash the complete task payload before accepting. A received bird is not proof of execution. Use taskflowctl.py; do not approve policies or wake peers.'}

    def activities(self):
        result={}
        for a in self.active():
            if a['state']!='RUNNING' or not a.get('providerTurnId') or not a.get('executionEvidence'):continue
            age=self.clock()-a['heartbeatAt'];fresh=0<=age<=30
            iso=lambda ts:datetime.fromtimestamp(ts,timezone.utc).isoformat()
            result[a['workerId']]={'phase':'working' if fresh else 'unavailable','label':'Working on '+a['payload']['request'].splitlines()[0][:100] if fresh else 'Task execution status unavailable',
                'active':fresh,'fresh':fresh,'stale':not fresh,'turnStatus':'open' if fresh else 'unknown',
                'observedAt':iso(a['heartbeatAt']),'expiresAt':iso(a['heartbeatAt']+30),'turnStartedAt':iso(a['startedAt']),
                'source':'Task Board supervisor and exact observed provider turn','taskId':a['taskId'],'assignmentId':a['id'],
                'providerThreadId':a['providerThreadId'],'providerTurnId':a['providerTurnId'],
                'lastAction':a.get('progress') or 'Executing the received task payload','lastActionAt':iso(a['heartbeatAt']),
                'model':self.context(a['workerId'])['worker']['model'],'reasoningEffort':self.context(a['workerId'])['worker']['effort']}
        return result

    def reconcile(self):
        """Expire only unreceived deliveries; uncertain execution keeps its claims."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT body FROM taskflow_assignments WHERE state IN ('+','.join('?' for _ in ACTIVE)+')',ACTIVE).fetchall():
                a=json.loads(row[0]);t=self._one(db,'taskflow_tasks',a['taskId']);w=self._one(db,'taskflow_workers',a['workerId'])
                if a.get('phase') in ('audit','verify','delivery'):
                    self.cycle.reconcile(db,a,t,w);continue
                if a['state']=='OFFERED' and not a.get('supervisorClaim'):
                    p=self._one(db,'taskflow_policies',t['laneId'],'lane')
                    try:reason=self._eligible(db,t,w,p,self._lane(t['projectId'],t['laneId']),a)
                    except Conflict as exc:reason=str(exc)
                    if t['taskVersion']!=a['taskVersion']:reason='Task instructions changed before pickup'
                    if self.clock()-a['at']>60:reason=reason or 'Worker did not receive the offer before availability expired'
                    if reason:
                        from execution_admission import Admission
                        Admission(self.root,self.clock).mark(db,a['id'],'TERMINAL')
                        a['state']='EXPIRED';w['idleAt']=None;db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),w['workerId']))
                        t.update(state='NEEDS_COORDINATION' if t['taskVersion']!=a['taskVersion'] or t['policyVersion']!=(p or {}).get('version') else 'QUEUED',waitReason=reason,station='board:'+t['laneId'])
                        t.pop('courierId',None)
                        self._assignment(db,a);self._put(db,t);self._event(db,t,'delivery-held','task-board',{'reason':reason},source='transit:'+a['id'],destination=t['station'])
                elif a['state']!='UNCERTAIN' and self.clock()-a['heartbeatAt']>max(90,a['maxMinutes']*60+30):
                    a['uncertainFrom']=a['state']
                    a['state']='UNCERTAIN';t.update(state='UNCERTAIN',blockedAtStation=t['station'],waitReason='Worker heartbeat lost; reconcile the exact assignment before releasing ownership')
                    self._assignment(db,a);self._put(db,t);self._event(db,t,'execution-uncertain','task-board',{'reason':t['waitReason']})

    def _worker_execution_busy(self, db, worker_id):
        if db.execute('SELECT 1 FROM taskflow_assignments WHERE worker=? AND state IN ('+','.join('?' for _ in ACTIVE)+')',(worker_id,*ACTIVE)).fetchone():
            return True
        return any(a.get('actor')==worker_id for a in self.delivery.active(db))

    def recover(self,assignment_id,actor,reason):
        """Release only proven terminal or durably never-started ownership."""
        with self.connect() as db:a=self._one(db,'taskflow_assignments',assignment_id)
        if not a or actor not in (a['workerId'],'operator-ui'):raise Conflict('Only the exact worker or operator may request reconciliation')
        if a['mode']=='managed':
            import fcntl
            locks=self.root/'runtime/production-locks';locks.mkdir(parents=True,exist_ok=True)
            with (locks/(a['workerId'].split(':',1)[1]+'.lock')).open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise Conflict('Managed supervisor still holds this worker; retain ownership')
                return self._recover(assignment_id,actor,reason,managed_lock_held=True)
        return self._recover(assignment_id,actor,reason)

    def _recover(self,assignment_id,actor,reason,managed_lock_held=False):
        reason=text(reason,'recovery reason',4000)
        if self.recovery_hold:raise Conflict('Restored copies cannot recover live assignments')
        with self.connect() as db:a=self._one(db,'taskflow_assignments',assignment_id)
        if not a or actor not in (a['workerId'],'operator-ui'):raise Conflict('Only the exact worker or operator may request reconciliation')
        if a['state']!='UNCERTAIN':raise Conflict('Assignment is not uncertain')
        proof=None
        prestart=a.get('uncertainFrom') in ('OFFERED','ACCEPTED') or (a['mode']=='cooperative' and a.get('acceptedAt') is not None and not a.get('startedAt'))
        claim_released=not a.get('supervisorClaim') or (a['mode']=='managed' and managed_lock_held)
        if claim_released and not any(a.get(k) for k in ('providerTurnId','providerThreadId','executionEvidence','startedAt')) and prestart:
            proof={'state':'never-started','assignmentId':a['id'],'managedWorkerLockHeld':managed_lock_held,'source':'Durable pre-start assignment with no provider allocation or execution intent; any managed supervisor reservation is covered by the exact worker lock'}
        elif a.get('executionTransport')=='local-pid':
            raise Conflict('PID execution is not included in this release')
        elif a.get('providerThreadId') and a.get('providerTurnId'):
            if self.observer:proof=self.observer(a['providerThreadId'],a['providerTurnId'])
            elif a['mode']=='cooperative':
                from taskflow_cooperative import terminal
                proof=terminal(self,a['workerId'],a['providerThreadId'],a['providerTurnId'])
            else:
                proof=None
            if not proof or proof.get('state')!='terminal' or proof.get('threadId')!=a['providerThreadId'] or proof.get('turnId')!=a['providerTurnId']:proof=None
        elif a['mode']=='managed' and a.get('providerThreadId'):
            proof=None
        if not proof:raise Conflict('Exact terminal or never-started evidence is unavailable; retain ownership without replay')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');current=self._one(db,'taskflow_assignments',assignment_id)
            if current!=a:raise Conflict('Assignment changed during observation; refresh reconciliation')
            t=self._one(db,'taskflow_tasks',a['taskId']);w=self._one(db,'taskflow_workers',a['workerId'])
            if a.get('phase'):
                return self.cycle.recover(db,a,t,w,proof,reason)
            if proof.get('turnId'):a['providerTurnId']=proof['turnId']
            a.update(state='CANCELLED' if a.get('cancelRequested') else 'FAILED',recovery={'reason':reason,'evidence':proof,'at':self.clock()})
            w['idleAt']=None;db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),w['workerId']))
            t.update(state='CANCELLED' if a.get('cancelRequested') else 'QUEUED',assignmentId=None,station='board:'+t['laneId'],waitReason='Recovered exact assignment; awaiting eligible pickup')
            if a.get('executionTransport')=='local-pid' and not a.get('cancelRequested'):
                t.update(state='BLOCKED',waitReason='PID execution recovered; reconcile its evidence before explicitly preparing new work. No automatic retry.')
            t.pop('courierId',None)
            from execution_admission import Admission
            Admission(self.root,self.clock).mark(db,a['id'],'TERMINAL')
            self._assignment(db,a);self._put(db,t);self._event(db,t,'assignment-recovered',actor,a['recovery'],source=a['workerId'],destination=t['station'])
        return a

    def pause_worker(self,worker_id,actor='operator-ui'):
        if actor!='operator-ui':raise ValueError('Only the operator changes enrollment')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');w=self._one(db,'taskflow_workers',worker_id)
            if not w:raise ValueError('Worker not enrolled')
            w.update(enabled=False,idleAt=None);db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),worker_id))
        self.reconcile();return w

    def cancel(self,task_id,version,actor='operator-ui'):
        if actor!='operator-ui':raise ValueError('Only the operator requests task cancellation')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self._one(db,'taskflow_tasks',task_id)
            if not t or t['version']!=version:raise Conflict('Task changed; refresh before cancelling')
            if t['state'] in TERMINAL:return t
            phases=[json.loads(r[0]) for r in db.execute("SELECT body FROM taskflow_assignments WHERE task_id=? AND state IN ('OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING')",(task_id,)) if json.loads(r[0]).get('phase')]
            if phases:
                for phase in phases:
                    phase['cancelRequested']=True;self._assignment(db,phase)
                t['waitReason']='Managed phase stop requested; exact turn and role custody retained'
                self._put(db,t);self._event(db,t,'managed-phase-stop-requested',actor,{'assignments':[a['id'] for a in phases]});return t
            delivery=self.delivery.active(db,task_id)
            if delivery and any(x['state']!='OFFERED' for x in delivery):
                for x in delivery:x['cancelRequested']=True;self.delivery.put(db,x)
                t['waitReason']='Stop requested; delivery actor retains custody until exact source release or terminal recovery'
                self._put(db,t);self._event(db,t,'delivery-cancel-requested',actor,{'attemptIds':[x['id'] for x in delivery]});return t
            for x in delivery:x['state']='CANCELLED';self.delivery.put(db,x)
            a=self._one(db,'taskflow_assignments',t['assignmentId']) if t.get('assignmentId') else None
            if a and a['state'] in ACTIVE:
                if a['state']=='UNCERTAIN':raise Conflict('Reconcile the exact uncertain turn before releasing or cancelling ownership')
                if a['state']=='OFFERED' and not a.get('supervisorClaim'):
                    a['state']='CANCELLED';t.update(state='CANCELLED',station='board:'+t['laneId'],waitReason='Cancelled before pickup')
                else:
                    a['cancelRequested']=True
                    if a['state'] in ('RUNNING','STARTING'):a['state']='CANCELLING'
                    t.update(state='CANCELLING',waitReason='Waiting for the assigned worker to confirm stopping; ownership remains reserved')
                self._assignment(db,a)
                if a['state']=='CANCELLED':
                    from execution_admission import Admission
                    Admission(self.root,self.clock).mark(db,a['id'],'TERMINAL')
            else:t.update(state='CANCELLED',station='board:'+t['laneId'],waitReason='Cancelled by the operator')
            if t['state'] in TERMINAL: t.pop('courierId',None)
            self._put(db,t);self._event(db,t,'cancel-requested',actor);return t

    def transition(self,assignment_id,actor,operation,values=None):
        v=values or {}
        with self.connect() as db:phase_assignment=self._one(db,'taskflow_assignments',assignment_id)
        if phase_assignment and phase_assignment.get('phase') in ('audit','verify','delivery'):
            return self.cycle.transition(assignment_id,actor,operation,v)
        if operation in ('accept','start'):self.adopt_intake()
        observation=None
        if operation=='start':
            with self.connect() as db:binding=self._one(db,'taskflow_assignments',assignment_id)
            if binding and binding['workerId']==actor and binding['mode']=='cooperative' and not self.observer:
                from taskflow_cooperative import observe
                observation=observe(self,actor,v.get('threadId'),v.get('turnId'))
            elif self.observer:observation=self.observer(v.get('threadId'),v.get('turnId'))
            if not observation or observation.get('state')!='running':raise Conflict('The exact provider turn is not observed running')
        with ExitStack() as context_boundary, self.connect() as db:
            db.execute('BEGIN IMMEDIATE');a=self._one(db,'taskflow_assignments',assignment_id)
            if not a or actor!=a['workerId']:raise ValueError('Exact assigned worker identity required')
            t=self._one(db,'taskflow_tasks',a['taskId']);w=self._one(db,'taskflow_workers',actor)
            if operation in ('accept','start','return') and t.get('learningRequired'):
                from taskflow_decisions import validate_boundary
                validate_boundary(self,db,t,w,a,operation,v)
            if operation in ('accept','start'):
                p=self._one(db,'taskflow_policies',t['laneId'],'lane');state=self._lane(t['projectId'],t['laneId'])
                if not p or not p['enabled'] or p['version']!=a['policyVersion'] or t['taskVersion']!=a['taskVersion']:
                    raise Conflict('Task or dispatch rules changed before execution')
                if not w or not w['enabled'] or not any(x['agentId']==actor and x['laneId']==t['laneId'] for x in state['placements']):raise Conflict('Worker enrollment or placement changed')
                problem=self._eligible(db,t,w,p,state,a)
                if problem:raise Conflict(problem)
                if v.get('payloadHash')!=a['payloadHash']:raise Conflict('Receiver must acknowledge the exact payload hash')
            if operation=='accept':
                if a['state']=='ACCEPTED':return a
                if a['state']!='OFFERED':raise Conflict('Only an offered task can be accepted')
                a['state']='ACCEPTED';a['acceptedAt']=self.clock();t.update(state='ACCEPTED',station=actor);t.pop('blockedAtStation',None)
                self._event(db,t,'accepted',actor,{'payloadHash':a['payloadHash']},source='transit:'+a['id'],destination=actor)
            elif operation=='start':
                if a['state']=='RUNNING' and a.get('providerTurnId')==v.get('turnId'):return a
                if a['state'] not in ('ACCEPTED','STARTING','CANCELLING'):raise Conflict('Accept the exact task before starting')
                thread=text(v.get('threadId'),'provider thread');turn=text(v.get('turnId'),'provider turn')
                expected=w['providerThreadId']
                if thread!=expected:raise Conflict('Provider thread differs from the enrolled worker')
                next_state='CANCELLING' if a.get('cancelRequested') else 'RUNNING'
                a.update(state=next_state,providerThreadId=thread,providerTurnId=turn,startedAt=self.clock(),executionEvidence=observation)
                t.update(state=next_state,station=actor);self._event(db,t,'running',actor,{'threadId':thread,'turnId':turn,'evidence':a['executionEvidence']})
            elif operation=='progress':
                if a['state'] not in ('RUNNING','ACCEPTED','CANCELLING'):raise Conflict('Task is not active')
                a['progress']=text(v.get('message'),'progress',4000);t['progress']={'message':a['progress']};self._event(db,t,'worker-message',actor,{'text':a['progress']})
            elif operation=='return':
                if a['state'] not in ('RUNNING','ACCEPTED'):raise Conflict('No active assignment to return')
                boundary=None;terminal_evidence=None
                if a['state']=='RUNNING' and a['mode']=='cooperative' and (a.get('executionEvidence') or {}).get('adapter')=='cooperative-current-turn':
                    from taskflow_cooperative import observe
                    current=observe(self,actor,a.get('providerThreadId'),a.get('providerTurnId'))
                    if not current or v.get('sourceReleased') is not True:raise Conflict('The bound current worker must explicitly release the task source at its safe boundary')
                    boundary={'kind':'cooperative-source-release','workerId':actor,'threadId':a['providerThreadId'],'turnId':a['providerTurnId'],
                              'at':self.clock(),'observation':current,'sourceReleased':True,'providerTurnEnded':False}
                elif a['state']=='RUNNING' and a.get('executionTransport')=='local-pid':
                    raise Conflict('PID execution is not included in this release')
                elif a['state']=='RUNNING':
                    observed=self.observer(a.get('providerThreadId'),a.get('providerTurnId')) if self.observer else None
                    if not observed or observed.get('state')!='terminal':raise Conflict('Observe completion of the exact running turn before returning its ownership')
                    if a['mode']=='managed':
                        if observed.get('status')!='completed' or observed.get('threadId')!=a.get('providerThreadId') or observed.get('turnId')!=a.get('providerTurnId'):raise Conflict('Exact managed work turn must be successfully completed')
                        terminal_evidence=copy.deepcopy(observed)
                elif a['mode']=='managed':raise Conflict('Exact managed work turn must be observed before return')
                manifest=pin_files(v.get('artifacts',[]))
                if not manifest:raise ValueError('Return at least one nonempty result artifact')
                for f in manifest:
                    if f['bytes']==0 or not inside(f['path'],[a['workdir'],*a['payload']['scopes']]):raise ValueError('Result artifact is empty or outside assigned output scope')
                expected=a['payload'].get('expectedArtifacts') or []
                if expected and {str(Path(f['path']).relative_to(a['workdir'])) for f in manifest}!=set(expected):raise Conflict('Return every exact configured/declared output in the assigned workspace')
                from taskflow_decisions import validate_outputs
                validate_outputs(db,t,a,manifest)
                from taskflow_judgment import attach
                judgment=attach(db,t,a,v.get('judgmentReturn'),manifest)
                if a.get('executionTransport')=='local-pid':
                    reservation=db.execute('SELECT body FROM execution_reservations WHERE id=?',(a['id'],)).fetchone()
                    bound=json.loads(reservation[0]).get('taskBinding') if reservation else None
                    if bound is not None:
                        # Grant validation and RETURNED/REVIEW commit share this
                        # writer transaction. A supervisor's earlier read cannot
                        # authorize acceptance after a committed revocation.
                        p=self._one(db,'taskflow_policies',t['laneId'],'lane')
                        if (p or {}).get('execution',{}).get('taskBinding')!=bound:
                            raise Conflict('Exact PID grant revoked or changed before return')
                        if not w or a.get('cancelRequested') or p['version']!=a['policyVersion']:
                            raise Conflict('Bound PID assignment or dispatch rules changed before return')
                        problem=self._eligible(db,t,w,p,self._lane(t['projectId'],t['laneId']),a)
                        if problem:raise Conflict(problem)
                a['result']={'summary':text(v.get('summary'),'result summary'),'artifacts':manifest,'at':self.clock(),'payloadHash':a['payloadHash']}
                if judgment:a['result']['judgmentReturn']=judgment
                if v.get('sourceCommit') is not None:
                    import re
                    if not re.fullmatch(r'[0-9a-f]{40}',v['sourceCommit']):raise ValueError('Return the exact source commit')
                    a['result']['sourceCommit']=v['sourceCommit']
                if boundary:a['result']['sourceRelease']=boundary
                if terminal_evidence:a['result']['terminalEvidence']=terminal_evidence
                a['state']='RETURNED';t.update(state='REVIEW',result=a['result'],station='review:'+t['laneId'],waitReason='Returned result needs functional verification; audit waived' if t.get('auditWaiver') else 'Awaiting independent result review',originalWorkerId=t.get('originalWorkerId') or actor)
                self._event(db,t,'returned',actor,a['result'],source=actor,destination=t['station'])
            elif operation in ('decline','fail','uncertain','cancelled'):
                if a['state'] not in ACTIVE:raise Conflict('Assignment is already terminal')
                if operation=='cancelled' and not a.get('cancelRequested'):raise Conflict('No cancellation was requested; report a failure without cancelling the task')
                if operation=='decline' and a['state']!='OFFERED':raise Conflict('Accepted ownership requires an observed terminal result before release')
                if operation in ('fail','cancelled') and a['state'] in ('STARTING','RUNNING','UNCERTAIN','CANCELLING'):
                    if a.get('executionTransport')=='local-pid':
                        raise Conflict('PID execution is not included in this release')
                    else:
                        if not self.observer:raise Conflict('Observe the exact provider turn before releasing execution ownership')
                        observed=self.observer(a.get('providerThreadId'),a.get('providerTurnId'))
                        if not observed or observed.get('state')!='terminal':raise Conflict('Provider execution has no observed terminal result')
                reason=text(v.get('reason'),'reason',4000)
                if operation=='uncertain' and a['state']!='UNCERTAIN':a['uncertainFrom']=a['state']
                if operation in ('fail','uncertain'):t['blockedAtStation']=t['station']
                a['state']={'decline':'DECLINED','fail':'FAILED','uncertain':'UNCERTAIN','cancelled':'CANCELLED'}[operation]
                t.update(state={'decline':'QUEUED','fail':'BLOCKED','uncertain':'UNCERTAIN','cancelled':'CANCELLED'}[operation],waitReason=reason)
                if operation=='decline':t['station']='board:'+t['laneId'];t.pop('courierId',None);w['idleAt']=None;db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),actor))
                if operation=='fail':t['station']='board:'+t['laneId'];t.pop('courierId',None)
                if operation=='cancelled':t.pop('courierId',None)
                self._event(db,t,operation,actor,{'reason':reason})
            else:raise ValueError('Unknown worker action')
            from execution_admission import Admission
            if a['state'] in ('RETURNED','FAILED','CANCELLED','DECLINED'):Admission(self.root,self.clock).mark(db,a['id'],'TERMINAL')
            elif a['state']=='UNCERTAIN':Admission(self.root,self.clock).mark(db,a['id'],'UNCERTAIN')
            a['heartbeatAt']=self.clock();self._assignment(db,a);self._put(db,t);return a

    def review(self,task_id,version,actor,verdict,summary,evidence_paths,learning_outcome=None):
        evidence=pin_files(evidence_paths)
        if not evidence:raise ValueError('Review evidence is required')
        summary=text(summary,'review findings')
        with ExitStack() as context_boundary, self.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self._one(db,'taskflow_tasks',task_id)
            if not t or t['version']!=version or t['state'] not in REVIEW_STATES:raise Conflict('Task is not awaiting this review')
            past=self._past(db,task_id)
            if verdict=='accept' and past and past[-1]['taskVersion']!=t['taskVersion']:raise Conflict('Returned work predates current instructions; complete a reconciled worker step before acceptance')
            if any(a['workerId']==actor for a in past) or actor==t.get('ownerId'):raise ValueError('Reviewer must be independent of implementation and owner closure')
            if actor!='operator-ui':self.audit.validate_verdict(db,t,actor,verdict,context_boundary)
            elif self.audit.current(db,task_id):raise Conflict('Release the assigned audit before an operator overrides its review')
            for f in (t.get('result') or {}).get('artifacts',[]):
                if hashlib.sha256(Path(f['path']).read_bytes()).hexdigest()!=f['sha256']:raise Conflict('Returned artifacts changed')
            if verdict not in ('accept','revise','fail'):raise ValueError('Choose accept, revise or fail')
            claim=self.audit.current(db,task_id)
            verification=t['state']=='VERIFY'
            t['review']={'reviewer':actor,'verdict':verdict,'summary':summary,'evidence':evidence,'at':self.clock(),
                         'taskVersion':t['taskVersion'],'assignmentId':t.get('assignmentId'),
                         'resultHash':digest(t.get('result'))}
            if learning_outcome is not None:
                from taskflow_learning import validate_evaluation
                if verdict!='accept':raise Conflict('Structured learning requires an independently accepted result')
                validate_evaluation(learning_outcome)
                t['review']['learningOutcome']=copy.deepcopy(learning_outcome)
            source=t['station']
            if verification:self.delivery.finish_verification(db,t,actor,verdict,summary,evidence)
            elif verdict=='accept':self.delivery.approve(db,t,actor)
            else:
                t.update(state='READY',station='board:'+t['laneId'],waitReason='Review requested correction: '+summary,
                         previousWorkerId=t.get('workerId'),correctionWorkerId=t.get('workerId'),assignmentId=None)
                t['taskVersion']+=1
                t['amendments'].append({'text':'Independent audit correction: '+summary,'at':self.clock(),'actor':actor})
            if t['state']=='DONE' and t.get('learningRequired') and learning_outcome is None:
                raise Conflict('Final learning-task acceptance requires an explicit independent outcome evaluation')
            if t['state'] in (*TERMINAL,'READY','QUEUED'): t.pop('courierId',None)
            self.audit.finish(db,t,actor,verdict)
            self._put(db,t)
            acceptance_seq=self._event(db,t,'review-'+verdict,actor,t['review'],source=source,destination=t['station'])
            if learning_outcome is not None:
                from taskflow_learning import record_review
                record_review(db,task_id,acceptance_seq,store=self)
            return t

    def learning_decision(self,task_id,version,actor,operation,spec=None):
        from taskflow_decisions import configure,prepare,revoke
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self._one(db,'taskflow_tasks',task_id)
            if not t or t['version']!=version:raise Conflict('Current task record version required')
            if operation=='configure':result=configure(self,db,t,spec,actor)
            elif operation=='prepare':result=prepare(self,db,t,actor)
            elif operation=='revoke':result=revoke(self,db,t,actor)
            else:raise ValueError('Unknown learning decision operation')
            self._put(db,t);self._event(db,t,'learning-decision-'+operation,actor,{'recordHash':digest(result)})
            return {'task':t,'decision':result}

    def correct_learning(self,task_id,version,actor,correction):
        # This is an explicit operator annotation, never a generated-worker action.
        if actor!='operator-ui':raise Conflict('Only explicit authenticated human correction is supported')
        if self.recovery_hold:raise Conflict('Original TaskFlow instance required for correction')
        if not isinstance(correction,dict) or set(correction)!={'learningOutcome','supersedes'}:
            raise ValueError('Exact outcome correction and predecessor required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self._one(db,'taskflow_tasks',task_id)
            if not t or t['version']!=version:raise Conflict('Current task version required')
            seq=self._event(db,t,'learning-outcome-corrected',actor,correction)
            from taskflow_learning import record_correction
            result=record_correction(db,task_id,seq)
            self._put(db,t)
            return {'task':t,'outcome':result}

    def handoff(self,task_id,version,spec,actor):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self._one(db,'taskflow_tasks',task_id)
            if not t or t['version']!=version or t['state']!='REVIEW':raise Conflict('Return current results before handing off')
            if actor not in ('operator-ui',t.get('workerId'),t.get('ownerId')):raise ValueError('Current worker, owner or operator must define next work')
            if self.audit.current(db,task_id):raise Conflict('Finish or release the active independent audit before handing off')
            t['previousWorkerId']=t.get('workerId');t['assignmentId']=None;t.pop('courierId',None);t.setdefault('requiredOutcome',t['definitionOfDone'])
            result=self._ready_in_db(db,t,task_id,version,spec,actor)
            self._event(db,t,'handoff-prepared',actor,{'definitionOfDone':spec.get('definitionOfDone'),'requiredOutcome':t['requiredOutcome']})
            return result

    def snapshot(self,project=None,lane=None):
        tasks=self.list(project,lane)
        with self.connect() as db:
            workers=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_workers')]
            policies=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_policies')]
            events=[dict(json.loads(r['body']),seq=r['seq']) for r in db.execute('SELECT seq,body FROM taskflow_events ORDER BY seq DESC LIMIT 300')]
            assignments=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_assignments ORDER BY rowid')]
            flights=self._busy_couriers(db)
        ids={t['taskId'] for t in tasks}
        names=('Finch','Wren','Lark','Kite','Swift')
        birds=[]
        for i,name in enumerate(names,1):
            bird='bird-'+str(i);flight=flights.get(bird)
            if flight and flight['taskId'] not in ids:continue
            history=[]
            for assignment in assignments:
                if assignment['taskId'] not in ids or assignment['courierId']!=bird:continue
                payload=assignment['payload']
                history.append({'assignmentId':assignment['id'],'taskId':assignment['taskId'],
                                'taskVersion':assignment['taskVersion'],'workerId':assignment['workerId'],
                                'state':assignment['state'],'offeredAt':assignment['at'],
                                'acceptedAt':assignment.get('acceptedAt'),'payloadHash':assignment['payloadHash'],
                                'inputs':payload.get('inputs',[]),'result':assignment.get('result'),
                                'source':assignment.get('sourceStation'),'destination':assignment['workerId']})
            birds.append({'id':bird,'name':name,'state':'delivering' if flight else 'standby',
                          'taskId':flight['taskId'] if flight else None,'history':history,
                          'historyBoundary':'Flies pickup and drop-off, then returns to the birdhouse. Replay is not completion.'})
        from taskflow_completion import feed
        return {'completionFeed':feed(self,tasks,assignments),'tasks':tasks,'workers':[w for w in workers if (not project or w['projectId']==project) and (not lane or w['laneId']==lane)],
                'policies':[p for p in policies if (not project or p['projectId']==project) and (not lane or p['laneId']==lane)],
                'events':[e for e in reversed(events) if e['taskId'] in ids], 'maxIdleBirds':5,'birds':birds,
                'auditTeam':self.audit.snapshot(project),'ruleVersion':RULE_VERSION,'observedAt':self.clock(),'recoveryHold':self.recovery_hold}

    def mutate(self,payload,actor='operator-ui'):
        op=payload.get('operation');i=payload.get('item',{})
        if op=='retain-episode':
            from taskflow_retention import retain_episode
            return retain_episode(self,i,actor)
        if op.startswith('delivery-'):return self.delivery.mutate(op,i,actor)
        if op.startswith('audit-'):return self.audit.mutate(op,i,actor)
        if op in ('waiver-record','waiver-accept','waiver-finish'):
            from taskflow_waiver import mutate
            return mutate(self,op,i,actor)
        if op=='annotate':return self.annotations.add(i,actor)
        if op=='annotation-access':return self.annotations.access(i,actor)
        if op=='capture':return self.capture(i,actor)
        if op=='policy':return self.save_policy(i,actor)
        if op=='enroll':return self.enroll(i,actor)
        if op=='pause-worker':return self.pause_worker(i['workerId'],actor)
        if op=='cancel':return self.cancel(i['taskId'],i['version'],actor)
        if op=='recover':return self.recover(i['assignmentId'],actor,i.get('reason','Check exact assignment recovery evidence'))
        if op=='pid-recovery-prepare':
            raise Conflict('PID execution is not included in this release')
        if op=='ready':return self.ready(i['taskId'],i['version'],i,actor)
        if op=='amend':return self.amend(i['taskId'],i['version'],i['text'],actor)
        if op=='review':return self.review(i['taskId'],i['version'],actor,i['verdict'],i['summary'],i.get('evidence',[]),i.get('learningOutcome'))
        if op=='learning-correct':return self.correct_learning(i['taskId'],i['version'],actor,i['correction'])
        if op=='learning-retain':
            from taskflow_learning_library import retain
            return retain(self,i,actor)
        if op=='learning-context':
            from taskflow_learning_library import context
            return context(self,i,actor)
        if op in ('learning-configure','learning-prepare','learning-revoke'):
            return self.learning_decision(i['taskId'],i['version'],actor,op.split('-',1)[1],i.get('configuration'))
        if op=='handoff':return self.handoff(i['taskId'],i['version'],i,actor)
        raise ValueError('Unknown Task Board operation')
