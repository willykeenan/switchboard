"""Finite setup admission shared by existing TaskFlow and Production supervisors.

No scheduler, credentials, policy activation or provider calls. Reservations are
durable before any launch, count failures/retries, and never expire implicitly.
Unknown starts keep capacity. Limits do not reset on lane-policy revision.
"""
import copy
import fcntl
import json
from store import ClosingConnection
import math
import re
import sqlite3
import time
from pathlib import Path
from workspace import Conflict

SCHEMA = '''CREATE TABLE IF NOT EXISTS execution_reservations(
 id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS execution_limits(id TEXT PRIMARY KEY, body TEXT NOT NULL);'''

def validate(value):
    required = {'id','parentId','periodStart','expiresAt','concurrency','maxStarts','maxReservedMinutes','maxMinutes',
                'maxDepth','models','efforts','workerIds','productionOrderIds'}
    if not isinstance(value,dict) or set(value) not in (required,required|{'taskBinding'}):
        raise ValueError('An exact finite execution policy is required')
    for key,maximum in [('maxStarts',1000),
                        ('maxReservedMinutes',100000),('maxMinutes',120)]:
        if type(value[key]) is not int or not 1<=value[key]<=maximum:
            raise ValueError('Invalid finite execution bound: '+key)
    if value['concurrency']!='inherit-game-controls':raise ValueError('Concurrency must inherit the installed game controls; no additional owner cap')
    if value['maxDepth']!=1 or type(value['maxDepth']) is not int:
        raise ValueError('Only depth-one existing managed workers are supported')
    if not isinstance(value['expiresAt'],(int,float)) or not math.isfinite(value['expiresAt']):
        raise ValueError('A finite absolute policy expiry is required')
    if not isinstance(value['periodStart'],(int,float)) or not math.isfinite(value['periodStart']) or value['periodStart']>=value['expiresAt']:
        raise ValueError('The exact original approval period start is required')
    for key in ('id','parentId'):
        if not isinstance(value[key],str) or not value[key] or len(value[key])>200:
            raise ValueError('Invalid execution '+key)
    for key in ('models','efforts','workerIds','productionOrderIds'):
        if not isinstance(value[key],list) or len(value[key])>1000 or any(not isinstance(x,str) or not x for x in value[key]) or len(set(value[key]))!=len(value[key]):
            raise ValueError('Exact unique allowlist required: '+key)
    if not value['models'] or value['efforts']!=['ultra'] or not value['workerIds']:
        raise ValueError('Explicit models, Ultra and original managed identities required')
    if 'taskBinding' in value:
        b=value['taskBinding']
        fields={'schema','taskId','taskVersion','requestHash','contractHash','workerId','phase',
                'maxStarts','maxSeconds','maxWorkers','modelCalls'}
        schemas={'ke.execution.pid-task-binding.v1':fields,'ke.execution.pid-task-binding.v2':fields|{'recovery'}}
        if not isinstance(b,dict) or b.get('schema') not in schemas or set(b)!=schemas[b['schema']]:
            raise ValueError('Exact versioned PID task binding required')
        if 'recovery' in b:
            recovery=b['recovery']
            if not isinstance(recovery,dict) or set(recovery)!={'assignmentId','assignmentHash','reservationHash','authorization'}:
                raise ValueError('Exact predecessor and recovery authorization required')
            if not isinstance(recovery['assignmentId'],str) or not recovery['assignmentId'] or len(recovery['assignmentId'])>200:
                raise ValueError('Exact predecessor assignment identity required')
            auth=recovery['authorization']
            if not isinstance(auth,dict) or set(auth)!={'path','sha256'} or not isinstance(auth['path'],str) or not Path(auth['path']).is_absolute():
                raise ValueError('Exact recovery authorization file pin required')
            if any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in
                   (recovery['assignmentHash'],recovery['reservationHash'],auth['sha256'])):
                raise ValueError('Exact recovery evidence hashes required')
        if any(not isinstance(b[k],str) or not b[k] or len(b[k])>200 for k in ('taskId','workerId')):
            raise ValueError('Exact PID task and worker identities required')
        if type(b['taskVersion']) is not int or b['taskVersion']<1 or b['phase']!='work':
            raise ValueError('PID binding supports an exact task version and work phase only')
        if any(not isinstance(b[k],str) or not re.fullmatch('[0-9a-f]{64}',b[k]) for k in ('requestHash','contractHash')):
            raise ValueError('Exact PID request and contract hashes required')
        if type(b['maxStarts']) is not int or b['maxStarts']!=1 or type(b['modelCalls']) is not int or b['modelCalls']!=0:
            raise ValueError('PID task binding permits one start and zero model calls')
        if type(b['maxSeconds']) is not int or not 1<=b['maxSeconds']<=60 or type(b['maxWorkers']) is not int or not 1<=b['maxWorkers']<=6:
            raise ValueError('PID task binding exceeds the one-minute/six-worker ceiling')
        if value['workerIds']!=[b['workerId']] or value['productionOrderIds'] or value['maxMinutes']!=1:
            raise ValueError('PID task binding requires its single worker, one minute and no production orders')
    return dict(value)


def check_task_binding(db, limits, worker, minutes, phase, task_id, ident):
    """Resolve the granted task from this transaction; never trust a caller's plan."""
    if limits.get('taskBinding') is None:
        return None
    raise Conflict('PID execution is not included in this release')

def parent_current(state,policy):
    parent=((policy or {}).get('execution') or {}).get('parentId')
    return any(p['agentId']==parent and p['laneId']==policy['laneId'] and p['role']=='coordinator' and (p.get('teamId') or p['role'])=='coordinator' for p in state.get('placements',[])) and any(x['agentId']==parent and x['laneId']==policy['laneId'] and x['teamId']=='coordinator' for x in state.get('teamLeads',[]))

class Admission:
    def __init__(self,root,clock=time.time): self.root=Path(root);self.clock=clock

    @staticmethod
    def install_schema(db):
        for statement in SCHEMA.split(';'):
            if statement.strip():db.execute(statement)

    def check_terminal_delivery(self,db,policy,worker,assignment):
        """Validate an already charged delivery for receipt recovery, never a start.

        Unlike check(), this cannot reserve capacity and does not renew an expired
        start window. Current phase authority and terminal observation are checked
        by the caller; the exact original sealed allowance and charge stay bound.
        """
        a=assignment or {};limits=validate((policy or {}).get('execution'))
        if (a.get('mode')!='managed' or a.get('phase')!='delivery'
                or a.get('state') not in ('RUNNING','UNCERTAIN') or a.get('cancelRequested')):
            raise Conflict('Exact retained managed delivery required for terminal receipt recovery')
        row=db.execute('SELECT policy_id,state,body FROM execution_reservations WHERE id=?',(a['id'],)).fetchone()
        sealed=db.execute('SELECT body FROM execution_limits WHERE id=?',(limits['id'],)).fetchone()
        if not row or not sealed or sealed[0]!=json.dumps(limits,sort_keys=True):
            raise Conflict('Original sealed delivery allowance and charge required')
        r=json.loads(row[2])
        if (row[0]!=limits['id'] or row[1]!=r.get('state') or r.get('state') not in ('ATTEMPTED','UNCERTAIN')
                or r.get('id')!=a['id'] or r.get('taskId')!=a['taskId'] or r.get('phase')!='delivery'
                or r.get('policyId')!=limits['id'] or r.get('parentId')!=limits['parentId']
                or r.get('laneId')!=policy['laneId'] or r.get('workerId')!=worker['workerId']
                or r.get('model')!=worker['model'] or r.get('effort')!=worker['effort']
                or a.get('workerId')!=worker['workerId'] or a.get('model')!=worker['model'] or a.get('effort')!=worker['effort']
                or worker['workerId'] not in limits['workerIds'] or worker['model'] not in limits['models']
                or worker['effort'] not in limits['efforts'] or type(r.get('minutes')) is not int
                or type(a.get('maxMinutes')) is not int or r['minutes']!=a['maxMinutes'] or not 1<=r['minutes']<=limits['maxMinutes']
                or r.get('taskBinding')!=limits.get('taskBinding')):
            raise Conflict('Original delivery reservation identity or charge changed')
        check_task_binding(db,limits,worker,r['minutes'],'delivery',a['taskId'],a['id'])
        for at in (r.get('at'),a.get('acceptedAt'),a.get('startedAt')):
            if type(at) not in (int,float) or not limits['periodStart']<=at<limits['expiresAt']:
                raise Conflict('Delivery must have started inside its original historical allowance')
        return limits

    def check(self,db,policy,worker,minutes,phase,ident=None,task_id=None):
        limits=validate((policy or {}).get('execution'))
        if self.clock()>=limits['expiresAt']:raise Conflict('Execution policy expired; no renewal inferred')
        if not policy.get('enabled') or not policy.get('allowManagedStarts'):raise Conflict('Managed execution is disabled')
        if worker['workerId'] not in limits['workerIds']:raise Conflict('Original managed identity is outside execution policy')
        if worker['model'] not in limits['models'] or worker['effort'] not in limits['efforts']:raise Conflict('Selected model/reasoning is outside the finite policy; no fallback')
        if type(minutes) is not int or not 1<=minutes<=limits['maxMinutes']:raise Conflict('Runtime exceeds the finite per-run limit')
        if phase not in ('work','audit','verify','delivery','production'):raise Conflict('Unsupported execution phase')
        binding=check_task_binding(db,limits,worker,minutes,phase,task_id,ident)
        self.install_schema(db)
        old=db.execute('SELECT body FROM execution_limits WHERE id=?',(limits['id'],)).fetchone()
        sealed=json.dumps(limits,sort_keys=True)
        if old and old[0]!=sealed:raise Conflict('Execution limits are immutable; do not reset or silently expand this policy')
        rows=[json.loads(r[0]) for r in db.execute('SELECT body FROM execution_reservations')]
        existing=next((r for r in rows if r['id']==ident),None)
        if existing:
            if existing.get('taskBinding')!=binding:
                raise Conflict('An existing PID reservation cannot lose or change its task binding')
            if existing['workerId']!=worker['workerId'] or existing['policyId']!=limits['id'] or existing['model']!=worker['model'] or existing['effort']!=worker['effort'] or existing['minutes']!=minutes or existing['phase']!=phase:
                raise Conflict('Reservation identity or immutable execution payload changed')
            if existing['state'] not in ('RESERVED','ATTEMPTED'):raise Conflict('This admission was already consumed; no duplicate or uncertain start')
            if binding is not None and (existing.get('taskId')!=task_id or existing.get('taskBinding')!=binding):
                raise Conflict('Reservation lacks this exact immutable PID task binding')
            if binding is not None and binding.get('recovery'):
                check_pid_recovery(db,task_id,binding,rows,successor_id=ident)
            return limits
        if binding is not None:
            check_pid_recovery(db,task_id,binding,rows)
        live=[r for r in rows if r['state'] in ('RESERVED','ATTEMPTED','UNCERTAIN')]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='taskflow_assignments'").fetchone():
            for raw in db.execute("SELECT body FROM taskflow_assignments WHERE state IN ('OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING')"):
                a=json.loads(raw[0])
                if a['mode']!='managed' or a['id']==ident or any(r['id']==a['id'] for r in live):continue
                task=json.loads(db.execute('SELECT body FROM taskflow_tasks WHERE id=?',(a['taskId'],)).fetchone()[0])
                live.append({'id':a['id'],'workerId':a['workerId'],'laneId':task['laneId'],'parentId':task.get('ownerId')})
        # Count legacy/imported managed runs outside this ledger too. Do not adopt
        # them or charge old completed QA against this finite setup period.
        p=self.root/'runtime/production.sqlite3'
        if p.is_file():
            from store import query_only_existing
            with query_only_existing(p) as other:
                for raw in other.execute("SELECT body FROM work_orders WHERE status IN ('STARTING','RUNNING','CANCELLING','UNCERTAIN')"):
                    o=json.loads(raw[0])
                    if o['id']==ident or any(r['id']==o['id'] for r in live):continue
                    live.append({'id':o['id'],'workerId':o['agentId'],'laneId':o['spec']['laneId'],
                                 'parentId':limits['parentId'] if o['spec']['laneId']==policy['laneId'] else None})
        if any(r['workerId']==worker['workerId'] for r in live):raise Conflict('Worker retains an active or uncertain admission')
        # Per-worker availability, source ownership and saved game controls are
        # enforced by the existing dispatcher. No second numerical cap here.
        # Renewal changes the policy id, never the original accounting period.
        # Preserve earlier TaskFlow reservations as well as imported Production
        # runs; otherwise a new policy id would silently replenish the allowance.
        used=[r for r in rows if r['policyId']==limits['id'] or
              (r.get('parentId')==limits['parentId'] and r.get('laneId')==policy['laneId'] and
               (r.get('at') or 0)>=limits['periodStart'])]
        # Installing this mechanism is not a budget reset. Charge actual earlier
        # setup starts/uncertain launch allocations from the original approval
        # period, including replacement order IDs and completed testers.
        if p.is_file():
            from store import query_only_existing
            with query_only_existing(p) as other:
                for raw in other.execute('SELECT body FROM work_orders'):
                    o=json.loads(raw[0])
                    if o['id']==ident or any(r['id']==o['id'] for r in used):continue
                    in_period = o['id'] in limits['productionOrderIds'] or (o.get('approvedAt') or 0)>=limits['periodStart']
                    if o['spec']['laneId']==policy['laneId'] and in_period and (o.get('providerTurnId') or o.get('launchKey')):
                        used.append({'id':o['id'],'minutes':o['spec']['minutes']})
        if len(used)>=limits['maxStarts']:raise Conflict('Finite start budget exhausted; retries count')
        if sum(r['minutes'] for r in used)+minutes>limits['maxReservedMinutes']:raise Conflict('Finite reserved-runtime budget exhausted')
        return limits

    def reserve(self,db,policy,worker,minutes,phase,ident,task_id):
        limits=self.check(db,policy,worker,minutes,phase,ident,task_id)
        if db.execute('SELECT 1 FROM execution_reservations WHERE id=?',(ident,)).fetchone():return
        db.execute('INSERT OR IGNORE INTO execution_limits VALUES(?,?)',(limits['id'],json.dumps(limits,sort_keys=True)))
        row={'id':ident,'taskId':task_id,'policyId':limits['id'],'parentId':limits['parentId'],
             'laneId':policy['laneId'],'workerId':worker['workerId'],'model':worker['model'],
             'effort':worker['effort'],'minutes':minutes,'phase':phase,'state':'RESERVED','at':self.clock()}
        if 'taskBinding' in limits:row['taskBinding']=limits['taskBinding']
        db.execute('INSERT INTO execution_reservations VALUES(?,?,?,?)',(ident,limits['id'],'RESERVED',json.dumps(row)))

    def mark(self,db,ident,state):
        row=db.execute('SELECT body FROM execution_reservations WHERE id=?',(ident,)).fetchone()
        if not row:return
        r=json.loads(row[0])
        if state=='ATTEMPTED' and r['state']!='RESERVED':raise Conflict('Admission has already attempted a provider start')
        if state not in ('ATTEMPTED','UNCERTAIN','TERMINAL'):raise ValueError('Invalid admission state')
        r.update(state=state,updatedAt=self.clock())
        db.execute('UPDATE execution_reservations SET state=?,body=? WHERE id=?',(state,json.dumps(r),ident))


def _prestart_predecessor(db, task_id, assignment_id, successor_id=None):
    raise Conflict('PID execution is not included in this release')


def prepare_pid_recovery(store, item, actor):
    raise Conflict('PID execution is not included in this release')


def check_pid_recovery(db, task_id, binding, rows, successor_id=None):
    raise Conflict('PID execution is not included in this release')

