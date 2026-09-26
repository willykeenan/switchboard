"""Bridge typed audit/delivery phases to the EXISTING TaskFlow assignments/runtime.

No alternate queue or provider transport. Role claims keep their original tables;
an assignment explicitly links the claim to one bounded managed provider turn.
"""
import json
import secrets
import uuid
from pathlib import Path
from workspace import Conflict
from execution_admission import Admission

PHASES=('audit','verify','delivery')

class Cycle:
    def __init__(self,store):self.s=store

    def eligible(self,db,t,w,p,state,a=None,phase=None,*,terminal_recovery=False):
        from taskflow import ACTIVE,inside,overlap
        from taskflow_delivery import files_current
        from workflow import allowed
        phase=phase or (a or {}).get('phase')
        if t.get('auditWaiver'):
            if phase in ('audit','verify'):return 'Formal audit waived by user; functional verification remains with owner'
        if self.s.recovery_hold:return 'Restored board is disarmed'
        from execution_admission import parent_current
        if not p or not p.get('enabled') or not p.get('allowManagedStarts') or not state.get('enabled'):return 'Managed phase dispatch is disabled'
        if not parent_current(state,p):return 'Original setup owner binding changed'
        if not w or not w['enabled'] or w['mode']!='managed' or w['projectId']!=t['projectId']:return 'An enrolled existing managed phase worker is required'
        if w['laneId']!=t['laneId']:return 'Managed phase must use its exact enrolled task lane'
        expected=('REVIEW',) if phase=='audit' else ('VERIFY',) if phase=='verify' else ('DELIVERY_QUEUED','DELIVERY_OFFERED','DELIVERING','DELIVERY_UNCERTAIN')
        if t['state'] not in expected:return 'Task no longer awaits this exact managed phase'
        purpose='audit' if phase in ('audit','verify') else 'delivery'
        if w.get('purpose','work')!=purpose:return 'Enrollment purpose does not match this phase'
        if purpose not in w['capabilities'] or purpose not in p['capabilities']:return 'Phase capability is not permitted'
        if not a and (w.get('idleAt') is None or not 0<=self.s.clock()-w['idleAt']<=60):return 'Phase worker availability is stale or unknown'
        seat=next((x for x in state['placements'] if x['agentId']==w['workerId']),None)
        if not seat or (seat['laneId'],seat['role'],seat.get('teamId') or seat['role'])!=(w['laneId'],w['role'],w['teamId']):return 'Phase worker saved lane/role/team changed'
        own=self.s._one(db,'taskflow_policies',w['laneId'],'lane')
        if not own or not own['enabled']:return 'Phase worker home lane is not enabled'
        if a and (a['policyVersion']!=p['version'] or a['enrollmentHash']!=self.s.delivery.enrollment_hash(w)):return 'Phase policy or enrollment changed'
        if a and a['taskVersion']!=t['taskVersion']:return 'Task changed before phase execution'
        session=next((x for x in self.s.flow.catalog()['sessions'] if x['agent_id']==w['workerId']),None)
        if not session or not session.get('managed') or session.get('endpoint')!=w['providerThreadId'] or session.get('provider','codex')!='codex':return 'Original managed provider identity changed'
        if t.get('ownerId')!=(p.get('execution') or {}).get('parentId'):return 'Task owner is outside the admitted parent'
        try:
            admission=Admission(self.s.root,self.s.clock)
            if terminal_recovery:admission.check_terminal_delivery(db,p,w,a)
            else:admission.check(db,p,w,(a or {}).get('maxMinutes',min(p['maxMinutes'],w.get('configuredMinutes') or p['maxMinutes'])),phase,(a or {}).get('id'))
            if phase=='delivery':
                self.s.delivery.valid_approval(t)
                if t.get('delivery',{}).get('actorId')!=w['workerId']:return 'Only the exact approved delivery worker is admitted'
                if w['workerId'] in (t['ownerId'],t['approval'].get('review',{}).get('reviewer')):return 'Delivery worker is not independent'
                if seat['role'] not in ('worker','writer'):return 'Saved role does not permit delivery'
                scopes=[t['delivery']['destination']['path']]
                if any(not inside(s,w['scopes']) or not inside(s,own['scopes']) for s in scopes):return 'Delivery destination exceeds enrolled source custody'
            else:
                if seat['role']!='auditor':return 'Saved role is not independent Audit House'
                if w['workerId'] in (t['ownerId'],t.get('workerId')) or any(x['workerId']==w['workerId'] for x in self.s._past(db,t['taskId'])):return 'Reviewer participated in implementation or owner closure'
                if phase=='verify' and w['workerId'] in ((t.get('deliveryResult') or {}).get('actor'),(t.get('approval') or {}).get('review',{}).get('reviewer')):return 'Installed verification must be independent of candidate approval and delivery'
                files_current(t['result']['artifacts']);scopes=[]
            production=self.s.root/'runtime/production.sqlite3'
            if production.is_file():
                import sqlite3
                from store import query_only_existing
                with query_only_existing(production) as other:
                    for raw in other.execute("SELECT body FROM work_orders WHERE status IN ('STARTING','RUNNING','CANCELLING','UNCERTAIN')"):
                        order=json.loads(raw[0])
                        if order['agentId']==w['workerId'] or overlap(scopes,[order['spec']['workspace']]):return 'Active managed production order retains actor or destination custody'
            if not allowed(state,t['workerId'],w['workerId']) or not allowed(state,w['workerId'],t['workerId']):return 'Phase and correction routes are not both permitted'
            for raw in db.execute('SELECT body FROM taskflow_assignments WHERE state IN ('+','.join('?' for _ in ACTIVE)+')',ACTIVE):
                other=json.loads(raw[0])
                if a and other['id']==a['id']:continue
                if other['workerId']==w['workerId'] or other['taskId']==t['taskId']:return 'Exact task or phase worker retains active/uncertain execution'
                if overlap(scopes,other['payload']['scopes']):return 'Active source assignment retains delivery destination'
            if any(x['actor']==w['workerId'] or overlap(scopes,[x['payload']['delivery']['destination']['path']]) for x in self.s.delivery.active(db) if not a or x['id']!=a['phaseId']):return 'Another delivery retains actor or destination custody'
            if db.execute("SELECT 1 FROM taskflow_audit_claims WHERE reviewer=? AND state='CLAIMED'"+(' AND id!=?' if a else ''),(w['workerId'],a['phaseId']) if a else (w['workerId'],)).fetchone():return 'Auditor retains another claim'
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
                for row in db.execute("SELECT * FROM tasks WHERE status='WORKING'"):
                    old=dict(row)
                    if old['owner']==w['workerId'] or overlap(scopes,json.loads(old.get('scope_json') or '[]')):return 'Existing task retains actor or source custody'
        except (ValueError,OSError,Conflict) as e:return str(e)
        return ''

    def dispatch(self):
        from taskflow import digest,encoded,ACTIVE
        if not self.s.flow or self.s.recovery_hold:return []
        offered=[];state=self.s.flow.read()
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            workers=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_workers ORDER BY id')]
            tasks=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_tasks ORDER BY rowid')]
            for t in tasks:
                phase={'REVIEW':'audit','VERIFY':'verify','DELIVERY_QUEUED':'delivery'}.get(t['state'])
                if t.get('auditWaiver') and phase in ('audit','verify'):continue
                if not phase or self.s.audit.current(db,t['taskId']) or self.s.delivery.active(db,t['taskId']):continue
                if any(a['state'] in ACTIVE for a in self.s._past(db,t['taskId'])):continue
                p=self.s._one(db,'taskflow_policies',t['laneId'],'lane');reason='No admitted managed phase worker'
                for w in workers:
                    try:w=self.s._future_worker(w)
                    except (ValueError,OSError) as exc:reason='Future settings unavailable: '+str(exc);continue
                    reason=self.eligible(db,t,w,p,state,phase=phase)
                    if reason:continue
                    busy=self.s._busy_couriers(db)
                    bird=next(('bird-'+str(i) for i in range(1,6) if 'bird-'+str(i) not in busy),None)
                    if not bird:reason='All existing couriers are delivering tasks';continue
                    aid=str(uuid.uuid4());pid=str(uuid.uuid4());now=self.s.clock()
                    payload={'taskId':t['taskId'],'taskVersion':t['taskVersion'],'phase':phase,
                             'request':phase.upper()+': '+t['request'],'definitionOfDone':t['definitionOfDone'],
                             'agentContext':w.get('personalContext',''),'amendments':t['amendments'],'inputs':[*t['inputs'],*t['result']['artifacts']],
                             'sourceRefs':t.get('sourceRefs',[]),'dependencies':[],
                             'scopes':[t['delivery']['destination']['path']] if phase=='delivery' else [],
                             'delivery':t['delivery'],'approval':t.get('approval'),'result':t['result'],
                             'requiredOutcome':t.get('requiredOutcome') or t['definitionOfDone'],
                             **self.s._history_payload(db,t)}
                    payload['phaseContract']=({'resultFields':['summary','artifacts','receipt'],
                        'instruction':'Deliver only the exact approved result within existing destination authority; return receipt path and independent evidence. Never promote a candidate without release authority.'} if phase=='delivery' else {
                        'resultFields':['summary','artifacts','verdict','libraryItemIds','contextAssessment'],'optionalResultFields':['learningOutcome'],
                        'instruction':'Independently inspect exact result and task criteria, read project Library; record findings and accept/revise/fail. A fixture is not game quality. Never impersonate the operator. Questions remain task-linked chat.'})
                    a={'id':aid,'taskId':t['taskId'],'workerId':w['workerId'],'phase':phase,'phaseId':pid,
                       'state':'OFFERED','taskVersion':t['taskVersion'],'policyVersion':p['version'],
                       'enrollmentHash':self.s.delivery.enrollment_hash(w),'payload':payload,'payloadHash':digest(payload),
                       'at':now,'heartbeatAt':now,'mode':'managed','receiptSecret':secrets.token_hex(24),
                       'workdir':str(self.s.root/'runtime/taskflow-work'/aid),'maxMinutes':min(p['maxMinutes'],w.get('configuredMinutes') or p['maxMinutes']),
                       'model':w['model'],'effort':w['effort'],'settingsVersion':w.get('settingsVersion'),'providerThreadId':None,'providerTurnId':None,'result':None,
                       'sourceStation':t['station'],'courierId':bird}
                    Admission(self.s.root,self.s.clock).reserve(db,p,w,a['maxMinutes'],phase,aid,t['taskId'])
                    if phase=='delivery':
                        self.s.delivery.put(db,{'id':pid,'taskId':t['taskId'],'actor':w['workerId'],'state':'OFFERED',
                            'managedAssignmentId':aid,'payload':payload,'payloadHash':a['payloadHash'],'offeredAt':now,
                            'heartbeatAt':now,'policyVersion':p['version'],'enrollmentHash':a['enrollmentHash']})
                        t.update(state='DELIVERY_OFFERED')
                    else:
                        self.s.audit.put(db,{'id':pid,'taskId':t['taskId'],'projectId':t['projectId'],
                            'taskVersion':t['taskVersion'],'reviewer':w['workerId'],'state':'CLAIMED','phase':'OFFERED',
                            'managedAssignmentId':aid,'offeredAt':now,'heartbeatAt':now,'resultHash':digest(t['result']),
                            'library':[],'contextAssessment':None})
                        t['audit']={'claimId':pid,'reviewer':w['workerId'],'phase':'OFFERED','assignmentId':aid}
                    self.s._assignment(db,a);w['idleAt']=None
                    db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),w['workerId']))
                    t.pop('managedPhaseWait',None)
                    t.update(waitReason='Managed '+phase+' offered; execution not yet observed')
                    self.s._put(db,t);self.s._event(db,t,'managed-phase-offered','task-board',{'assignmentId':aid,'phaseId':pid,'phase':phase,'workerId':w['workerId']});offered.append(a);break
                else:
                    # Cooperative phase queues own the user-facing wait reason.
                    # Keep managed admission diagnostics without rewriting it each tick.
                    diagnostic={'phase':phase,'reason':reason}
                    if t.get('managedPhaseWait')!=diagnostic:
                        t['managedPhaseWait']=diagnostic;self.s._put(db,t)
        return offered

    def transition(self,ident,actor,op,v):
        from taskflow import digest,pin_files,text,inside,ACTIVE
        s=self.s
        with s.connect() as db:a=s._one(db,'taskflow_assignments',ident)
        if not a or actor!=a['workerId']:raise Conflict('Exact assigned phase identity required')
        if op=='return':return self.finish(a,v)
        with s.connect() as db:
            db.execute('BEGIN IMMEDIATE');a=s._one(db,'taskflow_assignments',ident);t=s._one(db,'taskflow_tasks',a['taskId']);w=s._one(db,'taskflow_workers',actor)
            if op in ('accept','start'):
                problem=self.eligible(db,t,w,s._one(db,'taskflow_policies',t['laneId'],'lane'),s._lane(t['projectId'],t['laneId']),a)
                if problem:raise Conflict(problem)
                if v.get('payloadHash')!=a['payloadHash'] or a.get('cancelRequested'):raise Conflict('Current uncancelled phase payload acknowledgment required')
            if op=='accept':
                if a['state']!='OFFERED':raise Conflict('Phase is not offered')
                a.update(state='ACCEPTED',acceptedAt=s.clock())
            elif op=='start':
                proof=s.observer(v.get('threadId'),v.get('turnId')) if s.observer else None
                if a['state']!='STARTING' or not proof or proof.get('state')!='running' or proof.get('threadId')!=w['providerThreadId'] or proof.get('turnId')!=v.get('turnId'):raise Conflict('Exact admitted phase turn is not observed running')
                a.update(state='RUNNING',providerThreadId=w['providerThreadId'],providerTurnId=proof['turnId'],executionEvidence=proof,startedAt=s.clock())
            elif op=='progress':
                if a['state'] not in ACTIVE:raise Conflict('No active phase')
                a['progress']=text(v.get('message'),'phase progress',4000)
            elif op in ('fail','cancelled','uncertain'):
                if a['state'] not in ACTIVE:raise Conflict('Phase already ended')
                proof=s.observer(a.get('providerThreadId'),a.get('providerTurnId')) if s.observer else None
                if op!='uncertain' and a['state'] in ('STARTING','RUNNING','UNCERTAIN','CANCELLING') and (not proof or proof.get('state')!='terminal'):raise Conflict('Retain phase until exact terminal evidence')
                a.update(uncertainFrom=a['state'],state='UNCERTAIN' if op=='uncertain' else 'CANCELLED' if op=='cancelled' or a.get('cancelRequested') else 'FAILED',reason=text(v.get('reason'),'phase failure',4000))
                Admission(s.root,s.clock).mark(db,ident,'UNCERTAIN' if op=='uncertain' else 'TERMINAL')
            else:raise ValueError('Unsupported managed phase operation')
            a['heartbeatAt']=s.clock();s._assignment(db,a)
            self.mirror(db,t,a)
            if a['state']=='CANCELLED':t.update(state='CANCELLED',waitReason='Managed phase stopped with exact terminal evidence')
            s._put(db,t);s._event(db,t,'managed-phase-'+op,actor,{'assignmentId':ident,'phase':a['phase'],'state':a['state'],'reason':a.get('reason'),'proof':a.get('executionEvidence')})
        return a

    def mirror(self,db,t,a):
        s=self.s;ended=a['state'] in ('FAILED','EXPIRED','CANCELLED')
        if a['phase']=='delivery':
            d=s._one(db,'taskflow_delivery_attempts',a['phaseId'])
            d.update(state='FAILED' if ended else a['state'],heartbeatAt=a['heartbeatAt'],threadId=a.get('providerThreadId'),turnId=a.get('providerTurnId'),executionEvidence=a.get('executionEvidence'))
            s.delivery.put(db,d)
            t.update(state='DELIVERY_QUEUED' if ended else 'DELIVERY_UNCERTAIN' if a['state']=='UNCERTAIN' else 'DELIVERING' if a['state']!='OFFERED' else 'DELIVERY_OFFERED')
        else:
            c=s._one(db,'taskflow_audit_claims',a['phaseId'])
            c.update(state='RELEASED' if ended else 'CLAIMED',phase=a['state'],heartbeatAt=a['heartbeatAt'],executionEvidence=a.get('executionEvidence'))
            s.audit.put(db,c);t['audit']=None if ended else {'claimId':c['id'],'reviewer':c['reviewer'],'phase':a['state'],'assignmentId':a['id']}
        t.update(station=a['workerId'] if not ended else 'review:'+t['laneId'] if a['phase']!='delivery' else 'board:'+t['laneId'],waitReason=a.get('reason') or 'Managed '+a['phase']+' '+a['state'].lower())

    def finish(self,a,v):
        from taskflow import pin_files,inside
        s=self.s;actor=a['workerId'];proof=s.observer(a.get('providerThreadId'),a.get('providerTurnId')) if s.observer else None
        if a.get('cancelRequested'):raise Conflict('Phase cancellation requested; retain results without accepting them')
        if a['state']!='RUNNING' or not proof or proof.get('state')!='terminal' or proof.get('status')!='completed' or proof.get('threadId')!=a.get('providerThreadId') or proof.get('turnId')!=a.get('providerTurnId'):raise Conflict('Exact managed phase must be observed terminal before return')
        with s.connect() as db:
            current=s._one(db,'taskflow_assignments',a['id']);t=s._one(db,'taskflow_tasks',a['taskId']);w=s._one(db,'taskflow_workers',actor)
            if current!=a:raise Conflict('Phase changed during terminal observation')
            reason=self.eligible(db,t,w,s._one(db,'taskflow_policies',t['laneId'],'lane'),s.flow.read(),a)
            if reason:raise Conflict(reason)
        evidence=pin_files(v.get('artifacts',[]))
        if not evidence or any(not f['bytes'] or not inside(f['path'],[a['workdir']]) for f in evidence):raise Conflict('Phase evidence must be nonempty and inside the assigned workspace')
        # Existing services own substantive acceptance and destination checks.
        # If either fails, runtime marks this same assignment failed; no fake PASS.
        if a['phase'] in ('audit','verify'):
            s.audit.prepare(a['taskId'],actor,v.get('libraryItemIds',[]),v.get('contextAssessment',''))
            t=s.get(a['taskId']);s.review(t['taskId'],t['version'],actor,v.get('verdict'),v['summary'],[f['path'] for f in evidence],v.get('learningOutcome'))
        else:
            s.delivery.transition(a['phaseId'],actor,'return',{'receipt':v['receipt'],'evidence':[f['path'] for f in evidence],'sourceReleased':True})
        with s.connect() as db:
            db.execute('BEGIN IMMEDIATE');current=s._one(db,'taskflow_assignments',a['id'])
            current.update(state='RETURNED',result={'summary':v['summary'],'artifacts':evidence,'at':s.clock()},heartbeatAt=s.clock())
            s._assignment(db,current);Admission(s.root,s.clock).mark(db,a['id'],'TERMINAL')
            t=s._one(db,'taskflow_tasks',a['taskId']);s._event(db,t,'managed-phase-returned',actor,{'assignmentId':a['id'],'phaseId':a['phaseId'],'phase':a['phase'],'evidence':evidence,'terminal':proof})
        return current

    def reconcile(self,db,a,t,w):
        from taskflow import encoded
        p=self.s._one(db,'taskflow_policies',t['laneId'],'lane')
        reason=''
        if a['state']=='OFFERED' and not a.get('supervisorClaim'):
            reason=self.eligible(db,t,w,p,self.s.flow.read(),a)
            if self.s.clock()-a['at']>60:reason=reason or 'Unaccepted managed phase expired'
            if a.get('cancelRequested'):reason='Managed phase cancelled before supervisor claim'
            if reason:a.update(state='CANCELLED' if a.get('cancelRequested') else 'EXPIRED',reason=reason);Admission(self.s.root,self.s.clock).mark(db,a['id'],'TERMINAL')
        elif a['state']!='UNCERTAIN' and self.s.clock()-a['heartbeatAt']>max(90,a['maxMinutes']*60+30):
            a.update(uncertainFrom=a['state'],state='UNCERTAIN',reason='Managed phase heartbeat lost; exact custody retained')
            Admission(self.s.root,self.s.clock).mark(db,a['id'],'UNCERTAIN');reason=a['reason']
        if reason:
            self.s._assignment(db,a);self.mirror(db,t,a);self.s._put(db,t)
            if a['state']=='CANCELLED':t['state']='CANCELLED';self.s._put(db,t)
            w['idleAt']=None;db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),w['workerId']))
            self.s._event(db,t,'managed-phase-reconciled','task-board',{'assignmentId':a['id'],'reason':reason,'state':a['state']})

    def heartbeat(self,db,a):
        table='taskflow_delivery_attempts' if a['phase']=='delivery' else 'taskflow_audit_claims'
        phase=self.s._one(db,table,a['phaseId'])
        if phase:
            phase['heartbeatAt']=a['heartbeatAt']
            (self.s.delivery.put if a['phase']=='delivery' else self.s.audit.put)(db,phase)

    def recover(self,db,a,t,w,proof,reason):
        """Existing TaskFlow recovery already observed terminal/never-started and
        holds the original worker supervisor lock. Never infer a missing start.
        A crash after a committed verdict/receipt retains that completed result.
        """
        from taskflow import encoded
        s=self.s
        phase=s._one(db,'taskflow_delivery_attempts' if a['phase']=='delivery' else 'taskflow_audit_claims',a['phaseId'])
        committed=phase['state']==('RETURNED' if a['phase']=='delivery' else 'FINISHED')
        if committed and proof.get('status')!='completed':raise Conflict('Committed phase recovery requires a successfully completed exact turn')
        a.update(state='RETURNED' if committed else 'CANCELLED' if a.get('cancelRequested') else 'FAILED',
                 recovery={'reason':reason,'evidence':proof,'at':s.clock()},heartbeatAt=s.clock())
        if proof.get('turnId'):a['providerTurnId']=proof['turnId']
        w['idleAt']=None;db.execute('UPDATE taskflow_workers SET body=? WHERE id=?',(encoded(w),w['workerId']))
        if not committed:
            self.mirror(db,t,a)
            if a['state']=='CANCELLED':t.update(state='CANCELLED',waitReason='Managed phase stopped and reconciled')
        s._assignment(db,a);Admission(s.root,s.clock).mark(db,a['id'],'TERMINAL');s._put(db,t)
        s._event(db,t,'managed-phase-recovered',a['workerId'],{'assignmentId':a['id'],'committedResultPreserved':committed,**a['recovery']})
        return a
