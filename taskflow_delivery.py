"""Typed result approval, scoped cooperative delivery and independent verification.

Uses the original task database and event stream. This scheduler only offers work
in an admitted existing worker's passive context. It never copies an output,
installs software, launches a provider, or treats a receipt as founder acceptance.
"""
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import uuid
from pathlib import Path
from contextlib import ExitStack
from workspace import Conflict

STAGES = ('DELIVERY_QUEUED','DELIVERY_OFFERED','DELIVERING','DELIVERY_UNCERTAIN','VERIFY')
REVIEW_STATES = ('REVIEW','VERIFY')
LIVE = ('OFFERED','ACCEPTED','RUNNING','UNCERTAIN')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS taskflow_delivery_attempts(id TEXT PRIMARY KEY, task_id TEXT, actor TEXT, state TEXT, body TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS taskflow_delivery_one_task ON taskflow_delivery_attempts(task_id) WHERE state IN ('OFFERED','ACCEPTED','RUNNING','UNCERTAIN');
CREATE UNIQUE INDEX IF NOT EXISTS taskflow_delivery_one_actor ON taskflow_delivery_attempts(actor) WHERE state IN ('OFFERED','ACCEPTED','RUNNING','UNCERTAIN');
CREATE TABLE IF NOT EXISTS taskflow_delivery_availability(id TEXT PRIMARY KEY, body TEXT);
'''


def contract(value):
    from taskflow import text,paths
    if not isinstance(value,dict):raise Conflict('Declare the required delivery kind, destination and acceptance criteria before completing this task')
    kind=value.get('kind');destination=value.get('destination')
    if kind not in ('artifact','document','research','local-app'):raise ValueError('Choose task artifact, document, research or local app delivery')
    if not isinstance(destination,dict):raise ValueError('An exact destination is required')
    if kind=='artifact':
        if destination!={'kind':'task-result'}:raise ValueError('A task artifact is explicitly delivered to this task result')
    else:
        path=paths([destination.get('path')])[0]
        if kind=='local-app':
            if destination.get('kind')!='local-app' or not path.endswith('.app'):raise ValueError('Choose an exact local app bundle destination')
            destination={'kind':'local-app','path':path,'bundleId':text(destination.get('bundleId'),'bundle identifier',200)}
        else:
            if destination.get('kind')!='directory':raise ValueError('Document and research delivery requires a local destination directory')
            destination={'kind':'directory','path':path}
    criteria=value.get('criteria')
    if not isinstance(criteria,list) or not 1<=len(criteria)<=50:raise ValueError('Record 1–50 required acceptance criteria')
    criteria=[{'id':text(x.get('id'),'criterion ID',100),'description':text(x.get('description'),'criterion',2000)} for x in criteria]
    if len({x['id'] for x in criteria})!=len(criteria):raise ValueError('Acceptance criterion IDs must be unique')
    actor=text(value.get('actorId'),'existing delivery actor',200) if kind!='artifact' else None
    return {'kind':kind,'destination':destination,'criteria':criteria,'actorId':actor,'version':1}


def pins(task):
    from taskflow import digest
    return {'taskId':task['taskId'],'taskVersion':task['taskVersion'],'resultHash':digest(task.get('result')),
            'criteriaHash':digest((task.get('delivery') or {}).get('criteria')),
            'requirementsHash':digest({k:task.get(k) for k in ('request','amendments','definitionOfDone','requiredOutcome','inputs','sourceRefs','delivery')}),
            'sourceCommit':(task.get('result') or {}).get('sourceCommit')}


def files_current(files):
    for f in files:
        p=Path(f['path'])
        if not p.is_file() or p.resolve()!=p or hashlib.sha256(p.read_bytes()).hexdigest()!=f['sha256']:raise Conflict('Pinned delivery input or evidence changed: '+str(p))


def history_files(history):
    result=[]
    for row in history:
        result.extend(row.get('evidence',[]));result.extend((row.get('approval') or {}).get('review',{}).get('evidence',[]))
        approval=row.get('approval') or {}
        result.extend(approval.get('waiver',{}).get('evidence',[]))
        result.extend(approval.get('functionalChecks',{}).get('evidence',[]))
        delivery=row.get('deliveryResult') or {}
        if delivery.get('receipt'):result.append(delivery['receipt'])
        if delivery.get('observation',{}).get('releaseReceipt'):result.append(delivery['observation']['releaseReceipt'])
        result.extend(delivery.get('evidence',[]))
    return result


def historical_files(payload):
    return [*history_files(payload.get('priorDeliveries',[])),
            *[f for r in payload.get('priorResults',[]) for f in r['artifacts']],
            *[f for r in payload.get('priorReviews',[]) for f in r['evidence']]]


def file_integrity(source):
    """Observe an old pin without replacing its original digest or attribution."""
    p=Path(source['path'])
    try:
        if p.resolve()!=p:return {'status':'noncanonical','trusted':False}
        if not p.is_file():return {'status':'missing','trusted':False}
        actual=hashlib.sha256(p.read_bytes()).hexdigest()
        return {'status':'verified' if actual==source['sha256'] else 'changed',
                'trusted':actual==source['sha256'],'observedSha256':actual}
    except OSError:return {'status':'unavailable','trusted':False}


def payload_files(payload):
    """Current inputs stay mandatory; recorded invalid history is context only."""
    current=[*payload.get('inputs',[]),*payload.get('intake',{}).get('inputs',[])]
    excluded={(r['file']['path'],r['file']['sha256']) for r in payload.get('historicalIntegrity',[])
              if r.get('taskId')==payload.get('taskId') and r.get('trusted') is False}
    return current+[f for f in historical_files(payload) if (f['path'],f['sha256']) not in excluded]


def observed(store,actor,thread_id=None,turn_id=None,terminal=False):
    session=next((x for x in store.flow.catalog()['sessions'] if x['agent_id']==actor),None)
    if not session:raise Conflict('Exact actor is no longer registered')
    thread_id=thread_id or session.get('endpoint');turn_id=turn_id or (session.get('activity') or {}).get('turnId')
    if thread_id!=session.get('endpoint') or not turn_id:raise Conflict('Bind the exact registered provider turn')
    if store.observer:proof=store.observer(thread_id,turn_id)
    else:
        from taskflow_cooperative import observe,terminal as ended
        proof=(ended if terminal else observe)(store,actor,thread_id,turn_id)
    if not proof or proof.get('state')!=('terminal' if terminal else 'running') or proof.get('threadId')!=thread_id or proof.get('turnId')!=turn_id:raise Conflict('The exact provider turn is not observed '+('terminal' if terminal else 'running'))
    return proof



RELEASE_VALIDATOR_ENV='SWITCHBOARD_RELEASE_VALIDATOR'


def release_validator():
    """Operator-configured release receipt validator, or None when not configured."""
    configured=os.environ.get(RELEASE_VALIDATOR_ENV,'').strip()
    if not configured:return None
    path=Path(configured).expanduser()
    return path if path.is_absolute() and path.is_file() else None


class Delivery:
    def __init__(self,store):self.s=store
    def attempts(self,db,task_id=None):
        return [json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_delivery_attempts'+(' WHERE task_id=?' if task_id else ''),(task_id,) if task_id else ())]
    def active(self,db,task_id=None):return [a for a in self.attempts(db,task_id) if a['state'] in LIVE]
    def put(self,db,a):
        from taskflow import encoded
        db.execute('INSERT INTO taskflow_delivery_attempts VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,body=excluded.body',(a['id'],a['taskId'],a['actor'],a['state'],encoded(a)))
    def context(self,actor):
        with self.s.connect() as db:return {'attempts':[{**a,'evidenceNotes':self.s.annotations.list(db,a['taskId'])} for a in self.active(db) if a['actor']==actor],
            'instructions':'Inspect the exact approval and payload hash, accept, then bind your own current observed turn. Deliver only to the declared destination within existing source/install authority. Return a pinned receipt; independent verification completes the task. No provider wake or release authority is granted.'}
    def detail(self,task_id):
        with self.s.connect() as db:return self.attempts(db,task_id)
    def configure(self,task_id,version,value,actor):
        from taskflow import ACTIVE
        value=contract(value)
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self.s._one(db,'taskflow_tasks',task_id)
            if not t or t['version']!=version or actor not in ('operator-ui',t.get('ownerId')):raise Conflict('Only the task owner or operator may define its current delivery requirement')
            if t['state'] in (*ACTIVE,*STAGES,'REVIEW','DONE','CANCELLED') or self.s.audit.current(db,task_id):raise Conflict('Preserve active work and review; resolve its claim before changing the delivery contract')
            if value.get('actorId') and value['actorId']==t.get('ownerId'):raise Conflict('The owner coordinates delivery; choose an existing delivery worker')
            t.update(delivery=value,taskVersion=t['taskVersion']+1,approval=None,waitReason='Delivery requirement updated; refresh readiness')
            self.s._put(db,t);self.s._event(db,t,'delivery-defined',actor,value);return t
    def reconcile_artifact(self,task_id,version,assignment_id,reason,actor):
        """Legacy PID artifact reconciliation is not part of this release."""
        raise Conflict('PID execution is not included in this release')

    def valid_approval(self,t):
        a=t.get('approval')
        if not a or a.get('pins')!=pins(t):raise Conflict('Result approval no longer matches the exact task, requirements, source and criteria')
        if a.get('auditDisposition')=='WAIVED_BY_USER':
            from taskflow_waiver import valid
            waiver=valid(t)
            if a.get('waiver')!=waiver or a.get('auditPassed') is not False:raise Conflict('Approval waiver binding changed')
            evidence=[*waiver['evidence'],*a['functionalChecks']['evidence']]
        else:
            from taskflow import digest
            review=a.get('review') or {}
            reviewer=review.get('reviewer')
            if (review.get('verdict')!='accept' or not reviewer or
                reviewer in (t.get('ownerId'),t.get('workerId'),(t.get('delivery') or {}).get('actorId')) or
                review.get('taskVersion')!=t.get('taskVersion') or
                review.get('resultHash')!=digest(t.get('result')) or not review.get('evidence')):
                raise Conflict('Installation requires a current independent accepting audit for these exact result bytes')
            evidence=review['evidence']
        files_current([*t.get('inputs',[]),*(t.get('result') or {}).get('artifacts',[]),*evidence])
        return a
    def approve(self,db,t,actor):
        from taskflow import digest
        d=contract(t.get('delivery'))
        if actor=='operator-ui':raise Conflict('A typed result requires an independent assigned auditor')
        if d['kind']=='local-app' and not re.fullmatch(r'[0-9a-f]{40}',(t.get('result') or {}).get('sourceCommit','')):raise Conflict('The returned app candidate requires its exact source commit')
        t['approval']={'id':str(uuid.uuid4()),'pins':pins(t),'review':t['review'],'at':self.s.clock()}
        if d['kind']=='artifact':
            t.update(state='DONE',station='completed:'+t['laneId'],waitReason='',completion={'kind':'verified-task-artifact','approvalId':t['approval']['id'],'pins':pins(t),'reviewer':actor,'at':self.s.clock()})
        else:t.update(state='DELIVERY_QUEUED',station='board:'+t['laneId'],waitReason='Approved result awaits its designated delivery worker')
    def eligible(self,db,t,a,attempt=None):
        from taskflow import ACTIVE,inside,overlap
        from workflow import allowed
        d=t.get('delivery') or {};w=self.s._one(db,'taskflow_workers',a)
        if attempt and attempt.get('managedAssignmentId'):
            assignment=self.s._one(db,'taskflow_assignments',attempt['managedAssignmentId'])
            return self.s.cycle.eligible(db,t,w,self.s._one(db,'taskflow_policies',t['laneId'],'lane'),self.s.flow.read(),assignment)
        if d.get('actorId')!=a or a in (t.get('ownerId'),(t.get('approval') or {}).get('review',{}).get('reviewer')):return 'The exact independent delivery actor is required'
        if not w or not w['enabled'] or w['projectId']!=t['projectId'] or w['mode']!='cooperative':return 'Delivery requires an enrolled existing cooperative worker in this project'
        try:state=self.s._lane(t['projectId'],t['laneId']);self.s._lane(w['projectId'],w['laneId'])
        except Conflict:return 'Delivery lane is closed or missing'
        p=self.s.policy(w['laneId'])
        if not state.get('enabled') or not p or not p['enabled']:return 'Delivery dispatch is paused'
        seat=next((x for x in state['placements'] if x['agentId']==a),None)
        if not seat or seat['laneId']!=w['laneId'] or seat['role']!=w['role'] or (seat.get('teamId') or seat['role'])!=w['teamId']:return 'Delivery enrollment no longer matches the saved seat'
        if not inside(d['destination']['path'],w['scopes']) or not inside(d['destination']['path'],p['scopes']):return 'Destination is outside the enrolled delivery scope'
        if not allowed(state,t['workerId'],a) or not allowed(state,a,t['workerId']):return 'Delivery and correction connections must both be permitted'
        if db.execute("SELECT 1 FROM taskflow_audit_claims WHERE reviewer=? AND state='CLAIMED'",(a,)).fetchone():return 'Delivery actor owns an audit'
        if db.execute('SELECT 1 FROM taskflow_assignments WHERE worker=? AND state IN ('+','.join('?' for _ in ACTIVE)+')',(a,*ACTIVE)).fetchone():return 'Delivery actor still owns execution work'
        if any(x['actor']==a and (not attempt or x['id']!=attempt['id']) for x in self.active(db)):return 'Delivery actor already owns another delivery'
        if any(overlap([x['payload']['delivery']['destination']['path']],[d['destination']['path']]) and (not attempt or x['id']!=attempt['id']) for x in self.active(db)):return 'Another delivery retains this exact destination'
        for row in db.execute('SELECT body FROM taskflow_assignments WHERE state IN ('+','.join('?' for _ in ACTIVE)+')',ACTIVE):
            other=json.loads(row[0])
            if overlap(other['payload']['scopes'],[d['destination']['path']]):return 'An active source assignment retains the delivery destination'
        if attempt and (attempt['policyVersion']!=p['version'] or attempt['enrollmentHash']!=self.enrollment_hash(w)):return 'Delivery policy or enrollment changed'
        return None
    @staticmethod
    def enrollment_hash(w):
        from taskflow import digest
        return digest({k:v for k,v in w.items() if k!='idleAt'})
    def available(self,actor,enabled=True):
        from taskflow import encoded
        if self.s.recovery_hold:raise Conflict('Restored copies stay disarmed')
        if enabled:self.s.idle(actor,actor)
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if enabled and self.active(db) and any(a['actor']==actor for a in self.active(db)):raise Conflict('Finish the existing delivery before declaring availability')
            db.execute('INSERT OR REPLACE INTO taskflow_delivery_availability VALUES(?,?)',(actor,encoded({'at':self.s.clock() if enabled else None})))
        self.dispatch();return self.context(actor)
    def dispatch(self):
        from taskflow import digest,encoded
        if self.s.recovery_hold:return []
        offers=[]
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');availability={r['id']:json.loads(r['body']) for r in db.execute('SELECT * FROM taskflow_delivery_availability')}
            for a in self.active(db):
                if a.get('managedAssignmentId'):continue
                t=self.s._one(db,'taskflow_tasks',a['taskId']);v=availability.get(a['actor'],{})
                try:self.valid_approval(t);problem=self.eligible(db,t,a['actor'],a)
                except (Conflict,OSError) as e:problem=str(e)
                if a['state']=='OFFERED' and (problem or v.get('at') is None or self.s.clock()-v['at']>300 or self.s.clock()-a['offeredAt']>=60):
                    a.update(state='EXPIRED',reason=problem or 'Unaccepted delivery offer expired');self.put(db,a)
                    availability[a['actor']]={'at':None};db.execute('UPDATE taskflow_delivery_availability SET body=? WHERE id=?',(encoded({'at':None}),a['actor']))
                    if t['state']!='CANCELLED':t.update(state='DELIVERY_QUEUED',station='board:'+t['laneId'],waitReason=a['reason'])
                    self.s._put(db,t);self.s._event(db,t,'delivery-offer-expired','task-board',{'attemptId':a['id'],'reason':a['reason']})
                elif a['state']!='OFFERED' and (problem or self.s.clock()-a['heartbeatAt']>180):
                    if a['state']!='UNCERTAIN':
                        a.update(uncertainFrom=a['state'],state='UNCERTAIN',reason=problem or 'Delivery heartbeat lost');self.put(db,a)
                        t.update(state='DELIVERY_UNCERTAIN',waitReason=a['reason']+'; exact custody retained',blockedAtStation=a['actor']);self.s._put(db,t);self.s._event(db,t,'delivery-uncertain','task-board',{'attemptId':a['id'],'reason':a['reason']})
            tasks=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_tasks')]
            for t in sorted(tasks,key=lambda t:((t.get('approval') or {}).get('at',t['createdAt']),t['taskId'])):
                if t['state']!='DELIVERY_QUEUED' or self.active(db,t['taskId']):continue
                actor=(t.get('delivery') or {}).get('actorId');v=availability.get(actor,{})
                try:self.valid_approval(t)
                except (Conflict,OSError) as e:
                    # No live delivery owns this task. Retain the invalidated
                    # approval in history and return the same ID to its author;
                    # a stale approval must not become a permanent queue trap.
                    reason='Approval invalidated before delivery: '+str(e)
                    self.correction(db,t,'task-board',reason,[])
                    self.s._put(db,t);self.s._event(db,t,'delivery-approval-invalidated','task-board',{'reason':reason},destination=t['station'])
                    continue
                problem=self.eligible(db,t,actor)
                if not problem and (v.get('at') is None or not 0<=self.s.clock()-v['at']<=300):problem='Waiting for the designated delivery worker to become available'
                if problem:
                    if t.get('waitReason')!=problem:t['waitReason']=problem;self.s._put(db,t);self.s._event(db,t,'delivery-waiting','task-board',{'reason':problem})
                    continue
                w=self.s._one(db,'taskflow_workers',actor);payload={'approval':t['approval'],'delivery':t['delivery'],'result':t['result'],'inputs':t['inputs'],'request':t['request'],'amendments':t['amendments']}
                a={'id':str(uuid.uuid4()),'taskId':t['taskId'],'actor':actor,'state':'OFFERED','payload':payload,'payloadHash':digest(payload),'offeredAt':self.s.clock(),'heartbeatAt':self.s.clock(),'policyVersion':self.s.policy(w['laneId'])['version'],'enrollmentHash':self.enrollment_hash(w)}
                self.put(db,a);t.update(state='DELIVERY_OFFERED',waitReason='Awaiting delivery acceptance');self.s._put(db,t);self.s._event(db,t,'delivery-offered','task-board',{'attemptId':a['id'],'payloadHash':a['payloadHash']},source=t['station'],destination=actor);offers.append(a)
        return offers
    def verify_receipt(self,t,receipt):
        from taskflow import pin_files,inside
        approval=self.valid_approval(t);d=t['delivery']
        for k,v in {'taskId':t['taskId'],'approvalId':approval['id'],'pins':approval['pins'],'destination':d['destination']}.items():
            if receipt.get(k)!=v:raise Conflict('Delivery receipt does not match '+k)
        outputs=pin_files(receipt.get('outputs',[]))
        if d['kind']=='local-app':
            app=Path(d['destination']['path']);info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
            if info.get('CFBundleIdentifier')!=d['destination']['bundleId'] or (info.get('SourceCommit') or info.get('KESourceCommit') or info.get('KESwitchboardSourceCommit'))!=approval['pins']['sourceCommit'] or str(info.get('CFBundleVersion'))!=str(receipt.get('buildNumber')):raise Conflict('Installed bundle, build or source differs from the approved candidate')
            subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)],check=True,capture_output=True,timeout=15)
        else:
            if not outputs or any(not inside(f['path'],[d['destination']['path']]) for f in outputs):raise Conflict('Every document output must be at the declared destination')
            if sorted(f['sha256'] for f in outputs)!=sorted(f['sha256'] for f in t['result']['artifacts']):raise Conflict('Delivered document contents differ from the approved results')
        release=pin_files([receipt['releaseReceipt']])[0] if receipt.get('releaseReceipt') else None
        return {'outputs':outputs,'checkedAt':self.s.clock(),'sourceCommit':approval['pins']['sourceCommit'],'buildNumber':receipt.get('buildNumber'),'destination':d['destination'],'releaseReceipt':release}
    def verify_release(self,t,observation):
        """Installed completion also needs the host's full canonical release proof.

        The validator is operator configuration (SWITCHBOARD_RELEASE_VALIDATOR),
        never executable task input. A host without one cannot claim an installed outcome.
        """
        ref=observation.get('releaseReceipt')
        if not ref:raise Conflict('Local app completion requires a pinned full installed release receipt')
        files_current([ref]);r=json.loads(Path(ref['path']).read_text());d=t['delivery']['destination'];a=t['approval']
        if r.get('taskflowApproval')!={'taskId':t['taskId'],'approvalId':a['id'],'pins':a['pins']}:raise Conflict('Installed release receipt must bind this exact task approval')
        target=r.get('canonicalTarget',{})
        if target.get('appPath')!=d['path'] or target.get('bundleIdentifier')!=d['bundleId'] or r.get('candidate',{}).get('commitSha')!=a['pins']['sourceCommit']:raise Conflict('Installed release receipt target or source differs from approval')
        actual=r.get('requestContract',{}).get('acceptanceCriteria',[])
        if any(c not in actual for c in t['delivery']['criteria']):raise Conflict('Installed release proof does not cover every exact task criterion')
        validator=release_validator()
        if validator is None:raise Conflict('Installed outcome verification is not configured. Set '+RELEASE_VALIDATOR_ENV+' to a script that checks a release receipt and prints {"ok": true}.')
        result=subprocess.run([sys.executable,str(validator),ref['path']],capture_output=True,text=True,timeout=30)
        try:valid=json.loads(result.stdout).get('ok') is True
        except (ValueError,AttributeError):valid=False
        if result.returncode or not valid:raise Conflict('Full installed release verification failed; preserve the task for correction')
    def correction(self,db,t,actor,reason,evidence):
        t.setdefault('deliveryHistory',[]).append({'approval':t.get('approval'),'deliveryResult':t.get('deliveryResult'),'review':t.get('review'),'findings':reason,'evidence':evidence,'at':self.s.clock()})
        t.update(state='READY',station='board:'+t['laneId'],waitReason='Delivery correction: '+reason,previousWorkerId=t.get('workerId'),correctionWorkerId=t.get('workerId'),assignmentId=None,approval=None,deliveryResult=None,taskVersion=t['taskVersion']+1)
        t['amendments'].append({'text':'Independent delivery correction: '+reason,'actor':actor,'at':self.s.clock()})
    def finish_verification(self,db,t,actor,verdict,summary,evidence):
        if verdict=='accept':
            r=t.get('deliveryResult');files_current([r['receipt'],*r['evidence']]);observation=self.verify_receipt(t,json.loads(Path(r['receipt']['path']).read_text()))
            if t['delivery']['kind']=='local-app':
                if observation.get('releaseReceipt')!=r['observation'].get('releaseReceipt'):raise Conflict('Pinned installed release proof changed')
                self.verify_release(t,observation)
            if actor in (r['actor'],t['approval'].get('review',{}).get('reviewer')):raise Conflict('Final verification must be independent of installation and candidate approval')
            t.update(state='DONE',station='completed:'+t['laneId'],waitReason='',completion={'kind':'verified-delivery','approvalId':t['approval']['id'],'pins':pins(t),'reviewer':actor,'verification':t['review'],'deliveryResult':r,'at':self.s.clock()})
        else:self.correction(db,t,actor,summary,evidence)
    def validate_context_return(self,db,task,attempt,proof,boundary,*,recovery=False):
        if not attempt.get('managedAssignmentId'):return
        assignment=self.s._one(db,'taskflow_assignments',attempt['managedAssignmentId'])
        if (not assignment or assignment.get('mode')!='managed' or assignment.get('phase')!='delivery'
                or assignment.get('phaseId')!=attempt['id'] or assignment.get('taskId')!=task['taskId']
                or assignment.get('workerId')!=attempt['actor'] or attempt.get('cancelRequested')
                or assignment.get('providerThreadId')!=proof.get('threadId')
                or assignment.get('providerTurnId')!=proof.get('turnId')):
            raise Conflict('Receipt must retain its exact managed delivery assignment and terminal turn')

    def transition(self,ident,actor,op,v):
        from taskflow import digest,pin_files,text
        if self.s.recovery_hold:raise Conflict('Restored copies stay disarmed')
        if op in ('accept','start','return'):self.s.adopt_intake()
        with self.s.connect() as db:a=self.s._one(db,'taskflow_delivery_attempts',ident)
        if not a or a['actor']!=actor:raise Conflict('Exact assigned delivery actor required')
        proof=observed(self.s,actor,v.get('threadId'),v.get('turnId')) if op=='start' else None
        if op in ('return','fail') and a['state'] in ('RUNNING','UNCERTAIN') and a.get('turnId'):
            if v.get('sourceReleased') is not True:raise Conflict('Explicit source release from the bound turn is required')
            proof=observed(self.s,actor,a['threadId'],a['turnId'],terminal=bool(a.get('managedAssignmentId')))
        if op=='recover' and a['state']=='UNCERTAIN' and a.get('turnId'):proof=observed(self.s,actor,a['threadId'],a['turnId'],terminal=True)
        if a.get('managedAssignmentId') and (op=='return' or op=='recover' and v.get('receipt')) and proof.get('status')!='completed':raise Conflict('Managed delivery return requires a successfully completed exact turn')
        with ExitStack() as context_boundary, self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');current=self.s._one(db,'taskflow_delivery_attempts',ident)
            if current!=a:raise Conflict('Delivery changed during observation; refresh')
            t=self.s._one(db,'taskflow_tasks',a['taskId'])
            if op in ('accept','start','return'):
                self.valid_approval(t);problem=self.eligible(db,t,actor,a)
                if problem:raise Conflict(problem)
            if op in ('accept','start'):
                if a.get('cancelRequested'):raise Conflict('Delivery stop was requested; reconcile and release this assignment')
                if v.get('payloadHash')!=a['payloadHash'] or a['payload']['approval']!=t['approval']:raise Conflict('Acknowledge the exact still-approved delivery payload')
                files_current(t['result']['artifacts'])
            source=t['station']
            if op=='accept':
                if a['state']=='ACCEPTED':return a
                if a['state']!='OFFERED' or self.s.clock()-a['offeredAt']>=60:raise Conflict('Delivery offer is no longer current')
                a.update(state='ACCEPTED',acceptedAt=self.s.clock());t.update(state='DELIVERING',station=actor,waitReason='Delivery accepted; execution not yet observed')
            elif op=='start':
                if a['state']=='RUNNING' and a.get('turnId')==proof['turnId']:return a
                if a['state']!='ACCEPTED':raise Conflict('Accept the delivery before starting')
                a.update(state='RUNNING',threadId=proof['threadId'],turnId=proof['turnId'],executionEvidence=proof);t.update(waitReason='Delivery execution observed',station=actor)
            elif op=='heartbeat':
                if a['state'] not in ('ACCEPTED','RUNNING'):raise Conflict('No active delivery to heartbeat')
            elif op=='return':
                if a['state']!='RUNNING':raise Conflict('Bind observed execution before returning delivery')
                receipt=pin_files([v['receipt']])[0];evidence=pin_files(v.get('evidence',[]))
                if not evidence:raise Conflict('Delivery evidence is required')
                result=self.verify_receipt(t,json.loads(Path(receipt['path']).read_text()))
                self.validate_context_return(db,t,a,proof,context_boundary)
                a.update(state='RETURNED',result=result,sourceRelease=proof)
                t.update(deliveryResult={'actor':actor,'attemptId':ident,'receipt':receipt,'evidence':evidence,'observation':result},state='VERIFY',station='review:'+t['laneId'],waitReason='Delivered result needs functional verification; audit waived' if t.get('auditWaiver') else 'Delivered result awaits independent verification')
            elif op in ('fail','recover'):
                if op=='recover' and (a['state']!='UNCERTAIN' or not proof and a.get('uncertainFrom')!='ACCEPTED'):raise Conflict('Exact terminal or durable never-started evidence is required')
                if op=='fail' and a['state'] not in ('ACCEPTED','RUNNING','UNCERTAIN'):raise Conflict('No retained delivery to fail')
                reason=text(v.get('reason'),'delivery failure');evidence=pin_files(v.get('evidence',[]))
                if not evidence:raise Conflict('Delivery failure or recovery requires evidence')
                if op=='recover' and v.get('receipt'):
                    receipt=pin_files([v['receipt']])[0];result=self.verify_receipt(t,json.loads(Path(receipt['path']).read_text()))
                    self.validate_context_return(db,t,a,proof,context_boundary,recovery=True)
                    a.update(state='RETURNED',result=result,recoveryEvidence=proof)
                    t.update(deliveryResult={'actor':actor,'attemptId':ident,'receipt':receipt,'evidence':evidence,'observation':result},state='VERIFY',station='review:'+t['laneId'],waitReason='Exact terminal delivery recovered; functional verification required' if t.get('auditWaiver') else 'Exact terminal delivery recovered; independent verification required')
                else:
                    a.update(state='FAILED',reason=reason,evidence=evidence,recoveryEvidence=proof)
                    self.correction(db,t,actor,reason,evidence)
            else:raise ValueError('Unsupported delivery transition')
            if a.get('cancelRequested') and a['state'] in ('RETURNED','FAILED'):
                t.update(state='CANCELLED',station='board:'+t['laneId'],waitReason='Delivery stopped with exact source-release or terminal evidence; retained artifacts are not accepted completion')
            a['heartbeatAt']=self.s.clock();self.put(db,a);self.s._put(db,t);self.s._event(db,t,'delivery-'+op,actor,{'attemptId':ident,'state':a['state'],'proof':proof,'reason':v.get('reason'),'receipt':t.get('deliveryResult')},source=source,destination=t['station']);return a
    def mutate(self,op,v,actor):
        if op=='delivery-reconcile-artifact':return self.reconcile_artifact(v['taskId'],v['version'],v['assignmentId'],v['reason'],actor)
        if op=='delivery-configure':return self.configure(v['taskId'],v['version'],v['delivery'],actor)
        if op=='delivery-idle':return self.available(actor)
        if op=='delivery-pause':return self.available(actor,False)
        return self.transition(v['attemptId'],actor,op.removeprefix('delivery-'),v)
