"""Project-wide independent audit pool. No inference, hiring or wake path.

All claims share the task database transaction. Library reads are project scoped;
questions use the existing permission-checked inbox and remain attached to tasks.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from workspace import Conflict
from taskflow_delivery import REVIEW_STATES, observed, pins

SCHEMA = '''
CREATE TABLE IF NOT EXISTS taskflow_audit_claims(id TEXT PRIMARY KEY, task_id TEXT, reviewer TEXT, state TEXT, body TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS taskflow_audit_one_task ON taskflow_audit_claims(task_id) WHERE state='CLAIMED';
CREATE UNIQUE INDEX IF NOT EXISTS taskflow_audit_one_reviewer ON taskflow_audit_claims(reviewer) WHERE state='CLAIMED';
CREATE TABLE IF NOT EXISTS taskflow_audit_questions(id TEXT PRIMARY KEY, task_id TEXT, recipient TEXT, state TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS taskflow_audit_availability(id TEXT PRIMARY KEY, body TEXT NOT NULL);
'''


class AuditTeam:
    def __init__(self, store): self.s=store

    def topology(self):
        if not self.s.flow:return {'placements':[],'connections':[],'enabled':False},{}
        state=self.s.flow.read()
        lanes=state.get('laneCatalog') or self.s.flow.workspace.read()['lanes']
        return state,{x['id']:x for x in lanes}

    def eligible(self,db,task,actor):
        if task.get('auditWaiver'):return False
        from workflow import allowed
        from workflow_layout import active
        session=next((x for x in self.s.flow.catalog()['sessions'] if x['agent_id']==actor),None) if self.s.flow else None
        if not session or session.get('provider','codex')!='codex':return False
        state,lanes=self.topology()
        if not state.get('enabled'):return False
        seat=next((x for x in state['placements'] if x['agentId']==actor),None)
        lane=lanes.get((seat or {}).get('laneId'))
        home=lanes.get(task['laneId'])
        if not seat or seat['role']!='auditor' or not lane or not home or not active(lane) or not active(home):return False
        if lane['projectId']!=task['projectId'] or home['projectId']!=task['projectId']:return False
        if actor in (task.get('ownerId'),task.get('workerId')) or any(a['workerId']==actor for a in self.s._past(db,task['taskId'])):return False
        if any(a['workerId']==actor and not (a.get('phase') in ('audit','verify') and a['taskId']==task['taskId']) for a in self.s.active()):return False
        if any(a['actor']==actor for a in self.s.delivery.active(db)):return False
        if task['state']=='VERIFY' and actor in ((task.get('deliveryResult') or {}).get('actor'),(task.get('approval') or {}).get('review',{}).get('reviewer')):return False
        return bool(task.get('workerId')) and allowed(state,task['workerId'],actor) and allowed(state,actor,task['workerId'])

    def available(self,actor,enabled=True):
        """An existing reviewer opts into passive offers at its safe boundary."""
        from taskflow import encoded
        if self.s.recovery_hold:raise Conflict('Restored copies cannot receive audits')
        state,lanes=self.topology()
        seat=next((p for p in state['placements'] if p['agentId']==actor and p['role']=='auditor'),None)
        if not seat or not any(s['agent_id']==actor for s in self.s.flow.catalog()['sessions']):raise Conflict('A registered project Codex auditor is required')
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=self.s._one(db,'taskflow_audit_availability',actor) or {}
            value={**old,'reviewer':actor,'projectId':lanes[seat['laneId']]['projectId'],'availableAt':self.s.clock() if enabled else None}
            db.execute('INSERT OR REPLACE INTO taskflow_audit_availability VALUES(?,?)',(actor,encoded(value)))
        self.dispatch()
        return self.context(actor)

    def dispatch(self):
        """Persist role-queue offers, never provider starts or peer-route bypasses.

        Unaccepted offers expire. Accepted reviews retain custody until release;
        missing/stale reviewers surface an exception, never a second live owner.
        """
        from taskflow import digest,encoded
        if self.s.recovery_hold:return []
        offered=[]
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            availability={r['id']:json.loads(r['body']) for r in db.execute('SELECT * FROM taskflow_audit_availability')}
            for c in self.claims(db):
                if c.get('managedAssignmentId') or c['state']!='CLAIMED' or c.get('phase')!='OFFERED':continue
                t=self.s._one(db,'taskflow_tasks',c['taskId']);v=availability.get(c['reviewer'],{})
                fresh=v.get('availableAt') is not None and 0<=self.s.clock()-v['availableAt']<=300
                if t['state'] in REVIEW_STATES and fresh and self.s.clock()-c['offeredAt']<60 and c['taskVersion']==t['taskVersion'] and c['resultHash']==digest(t['result']) and self.eligible(db,t,c['reviewer']):continue
                c.update(state='EXPIRED',finishedAt=self.s.clock(),reason='Unaccepted offer expired or eligibility changed');self.put(db,c)
                v['availableAt']=None
                if c['reviewer'] in availability:db.execute('UPDATE taskflow_audit_availability SET body=? WHERE id=?',(encoded(v),c['reviewer']))
                t.update(audit=None,station='review:'+t['laneId'],waitReason='Waiting for auditor')
                self.s._put(db,t);self.s._event(db,t,'audit-offer-expired','task-board',{'claimId':c['id']})
            tasks=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_tasks')]
            tasks.sort(key=lambda t:((t.get('result') or {}).get('at',t['createdAt']),t['taskId']))
            for t in tasks:
                if t['state'] not in REVIEW_STATES:continue
                current=self.current(db,t['taskId'])
                if current:
                    if current.get('managedAssignmentId'):continue
                    if current.get('phase')!='OFFERED' and (not self.eligible(db,t,current['reviewer']) or self.s.clock()-current['heartbeatAt']>1800):
                        reason='Assigned review needs attention; custody retained until exact reviewer release'
                        if t.get('waitReason')!=reason:
                            t['waitReason']=reason;self.s._put(db,t);self.s._event(db,t,'audit-needs-attention','task-board',{'claimId':current['id'],'reason':reason})
                    continue
                candidates=[]
                for actor,v in availability.items():
                    if v.get('availableAt') is None or not 0<=self.s.clock()-v['availableAt']<=300 or v['projectId']!=t['projectId']:continue
                    if db.execute("SELECT 1 FROM taskflow_audit_claims WHERE reviewer=? AND state='CLAIMED'",(actor,)).fetchone():continue
                    if self.eligible(db,t,actor):candidates.append((v.get('lastOfferedAt',0),actor))
                if not candidates:
                    reason='Waiting for auditor; no available permitted independent Codex reviewer'
                    if t.get('waitReason')!=reason:
                        t.update(waitReason=reason,station='review:'+t['laneId']);self.s._put(db,t);self.s._event(db,t,'audit-waiting','task-board',{'reason':reason})
                    continue
                _,actor=min(candidates)
                c={'id':str(uuid.uuid4()),'taskId':t['taskId'],'projectId':t['projectId'],'taskVersion':t['taskVersion'],'reviewer':actor,'state':'CLAIMED','phase':'OFFERED',
                   'offeredAt':self.s.clock(),'heartbeatAt':self.s.clock(),'resultHash':digest(t['result']),'library':[],'contextAssessment':None}
                self.put(db,c);availability[actor]['lastOfferedAt']=self.s.clock()
                db.execute('UPDATE taskflow_audit_availability SET body=? WHERE id=?',(encoded(availability[actor]),actor))
                t.update(audit={'claimId':c['id'],'reviewer':actor,'phase':'OFFERED'},waitReason='Awaiting reviewer acceptance')
                self.s._put(db,t);self.s._event(db,t,'audit-offered','task-board',{'claimId':c['id'],'reviewer':actor})
                offered.append(c)
        return offered

    def claims(self,db):
        return [json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_audit_claims ORDER BY rowid')]

    def questions(self,db,task_id):
        return [json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_audit_questions WHERE task_id=? ORDER BY rowid',(task_id,))]

    def put(self,db,c):
        from taskflow import encoded
        db.execute('INSERT INTO taskflow_audit_claims VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,body=excluded.body',(c['id'],c['taskId'],c['reviewer'],c['state'],encoded(c)))

    def current(self,db,task_id):
        r=db.execute("SELECT body FROM taskflow_audit_claims WHERE task_id=? AND state='CLAIMED'",(task_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def detail(self,task_id):
        with self.s.connect() as db:
            return {'claims':[c for c in self.claims(db) if c['taskId']==task_id], 'questions':self.questions(db,task_id)}

    def context(self,actor):
        from workflow import allowed
        with self.s.connect() as db:
            claimed=[c for c in self.claims(db) if c['state']=='CLAIMED' and c['reviewer']==actor]
            queue=[t for t in self.s.list() if not t.get('legacy') and t['state'] in REVIEW_STATES and not self.current(db,t['taskId']) and self.eligible(db,t,actor)]
            state,_=self.topology()
            questions=[json.loads(r[0]) for r in db.execute("SELECT body FROM taskflow_audit_questions WHERE recipient=? AND state='OPEN'",(actor,))]
            questions=[q for q in questions if allowed(state,q['reviewer'],actor) and allowed(state,actor,q['reviewer'])]
            for c in claimed:
                c['evidenceNotes']=self.s.annotations.list(db,c['taskId'])
                c['task']=self.s.get(c['taskId']);c['questions']=self.questions(db,c['taskId'])
                _,lanes=self.topology()
                c['researchContacts']=[{**p,'canAsk':allowed(state,actor,p['agentId']) and allowed(state,p['agentId'],actor)} for p in state['placements'] if p['role']=='researcher' and lanes.get(p['laneId'],{}).get('projectId')==c['projectId']]
                c['projectLibrary']='/library?project='+c['projectId']
        return {'claims':[c for c in claimed if c.get('phase')!='OFFERED'],'offers':[c for c in claimed if c.get('phase')=='OFFERED'],'queue':sorted(queue,key=lambda t:((t.get('result') or {}).get('at',t['updatedAt']),t['taskId'])),'questions':questions,
                'instructions':'Use audit-idle at your safe boundary to receive shared project offers; inspect audit.offers and accept with audit-claim. Offers are not execution. Claim one audit before reviewing. Read the exact task, its Library context and source versions. Ask task-linked questions of permitted researchers. Return findings plus evidence. A candidate review is not installed verification; do not accept an unmet outcome. No owner acknowledgment is needed for routine audit or in-scope corrections.'}

    def snapshot(self,project=None):
        from workflow_layout import active
        state,lanes=self.topology()
        with self.s.connect() as db:
            claims=[c for c in self.claims(db) if c['state']=='CLAIMED' and (not project or c['projectId']==project)]
            tasks=[t for t in self.s.list() if not t.get('legacy') and t['state'] in REVIEW_STATES and (not project or t['projectId']==project)]
            members=[]
            # One coherent inventory per snapshot, including managed auditors.
            seats=[seat for seat in state['placements'] if seat['role']=='auditor'
                   and (lane:=lanes.get(seat['laneId'])) and active(lane)
                   and (not project or lane['projectId']==project)]
            sessions={x['agent_id']:x for x in self.s.flow.catalog()['sessions']} if seats else {}
            for seat in seats:
                lane=lanes.get(seat['laneId'])
                member=sessions.get(seat['agentId'])
                if not member or member.get('provider','codex')!='codex' or seat['role']!='auditor' or not lane or not active(lane) or (project and lane['projectId']!=project):continue
                claim=next((c for c in claims if c['reviewer']==seat['agentId']),None)
                members.append({'agentId':seat['agentId'],'projectId':lane['projectId'],'laneId':lane['id'],'claim':claim,
                                'state':'stale' if claim and self.s.clock()-claim['heartbeatAt']>1800 else claim.get('phase','ACCEPTED').lower() if claim else 'unclaimed'})
            waiting=[t for t in tasks if not any(c['taskId']==t['taskId'] for c in claims)]
            unroutable=[t['taskId'] for t in waiting if not any(self.eligible(db,t,m['agentId']) for m in members)]
        return {'members':members,'claims':claims,'waiting':len(waiting),'waitingTaskIds':[t['taskId'] for t in waiting],
                'unroutableTaskIds':unroutable,'oldestWaitSeconds':max([max(0,self.s.clock()-(t.get('result') or {}).get('at',t['createdAt'])) for t in waiting] or [0]),
                'capacitySignal':'unroutable' if unroutable else 'backlog' if len(waiting)>len(members) else 'clear',
                'maxConcurrentPerReviewer':1,'autoHire':False,'standbyInference':False}

    def claim(self,task_id,actor):
        if self.s.recovery_hold:raise Conflict('Restored copies cannot claim live work')
        from taskflow import digest
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not task_id:
                tasks=sorted((t for t in self.s.list() if not t.get('legacy')),key=lambda t:((t.get('result') or {}).get('at',t['updatedAt']),t['taskId']))
                task_id=next((t['taskId'] for t in tasks if t['state'] in REVIEW_STATES and not self.current(db,t['taskId']) and self.eligible(db,t,actor)),None)
            t=self.s._one(db,'taskflow_tasks',task_id)
            if not t or t['state'] not in REVIEW_STATES or not self.eligible(db,t,actor):raise Conflict('No permitted independent audit is ready')
            old=self.current(db,task_id)
            if old:
                if old['reviewer']==actor:
                    if old.get('phase')!='OFFERED':return old
                    if self.s.clock()-old['offeredAt']>=60 or old['taskVersion']!=t['taskVersion'] or old['resultHash']!=digest(t['result']):raise Conflict('Audit offer expired; refresh the shared queue')
                    old.update(phase='ACCEPTED',acceptedAt=self.s.clock(),heartbeatAt=self.s.clock());self.put(db,old)
                    t.update(audit={'claimId':old['id'],'reviewer':actor,'phase':'ACCEPTED'},station=actor,waitReason='Reviewer accepted; audit work not yet observed')
                    self.s._put(db,t);self.s._event(db,t,'audit-claimed',actor,{'claimId':old['id']},source='review:'+t['laneId'],destination=actor)
                    return old
                raise Conflict('Another auditor owns this review')
            if db.execute("SELECT 1 FROM taskflow_audit_claims WHERE reviewer=? AND state='CLAIMED'",(actor,)).fetchone():raise Conflict('Finish or release your current audit first')
            c={'id':str(uuid.uuid4()),'taskId':task_id,'projectId':t['projectId'],'taskVersion':t['taskVersion'],'reviewer':actor,'state':'CLAIMED',
               'phase':'ACCEPTED','acceptedAt':self.s.clock(),'heartbeatAt':self.s.clock(),'resultHash':digest(t['result']),'library':[],'contextAssessment':None}
            self.put(db,c);source=t['station'];t.update(audit={'claimId':c['id'],'reviewer':actor},station=actor)
            self.s._put(db,t);self.s._event(db,t,'audit-claimed',actor,{'claimId':c['id']},source=source,destination=actor)
            return c

    def require(self,db,task_id,actor):
        t=self.s._one(db,'taskflow_tasks',task_id);c=self.current(db,task_id)
        if not t or t['state'] not in REVIEW_STATES or not c or c['reviewer']!=actor or not self.eligible(db,t,actor):raise Conflict('Claim this independent audit first; current permissions must still allow it')
        if c.get('phase')=='OFFERED':raise Conflict('Accept the audit offer before working or issuing a verdict')
        if c['taskVersion']!=t['taskVersion']:raise Conflict('Task instructions changed; release and re-claim the audit')
        return t,c

    def library(self,task_id,actor,query='',offset=0,ids=None,*,task_db=None):
        if task_db is None:
            with self.s.connect() as owned:
                owned.execute('BEGIN');return self.library(task_id,actor,query,offset,ids,task_db=owned)
        t,_=self.require(task_db,task_id,actor)
        if not isinstance(query,str) or len(query)>300 or type(offset) is not int or offset<0:raise ValueError('Invalid Library query')
        path=self.s.root/'runtime/library/library.sqlite3'
        if not path.is_file() or path.is_symlink():return {'status':'unavailable','items':[],'total':0,'nextOffset':None}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            where='project=?';args=[t['projectId']]
            if ids is not None:
                if not isinstance(ids,list) or not 1<=len(ids)<=30:raise ValueError('Choose 1–30 Library records')
                where+=' AND id IN ('+','.join('?' for _ in ids)+')';args+=ids
            elif query:
                where+=' AND (title LIKE ? OR body LIKE ?)';args += ['%'+query+'%']*2
            total=db.execute('SELECT count(*) FROM documents WHERE '+where,args).fetchone()[0]
            rows=[dict(r) for r in db.execute('SELECT * FROM documents WHERE '+where+' ORDER BY modified DESC,id LIMIT 30 OFFSET ?',[*args,offset])]
        from inspector.library import Library
        scoped=Library(self.s.root,self.s.flow).for_actor(actor,self.s,task_db)
        permitted=[]
        for r in rows:
            if not scoped.readable(r):continue
            r['sourceState']=Library.source_state(r) or 'indexed';r['bodyHash']=hashlib.sha256(r['body'].encode()).hexdigest()
            if len(r['body'])>64000:r['body']=r['body'][:64000];r['truncated']=True
            permitted.append(r)
        return {'status':'available','projectId':t['projectId'],'items':permitted,'total':len(permitted),'totalKind':'authorized-page','nextOffset':offset+30 if offset+30<total else None,
                'notice':'Indexed project records; source state and timestamps are explicit. Record the required coverage before a verdict.'}

    def prepare(self,task_id,actor,ids,assessment):
        from taskflow import text
        assessment=text(assessment,'Library and research context assessment')
        if not isinstance(ids,list) or len(ids)>30 or any(not isinstance(x,str) for x in ids):raise ValueError('Choose at most 30 exact Library record IDs')
        library=self.library(task_id,actor,ids=ids) if ids else {'status':'no-records-selected','items':[]}
        if len(library['items'])!=len(set(ids)):raise Conflict('A selected Library record is missing or outside this project')
        if any(r['sourceState'] not in ('indexed',) or r.get('truncated') for r in library['items']):raise Conflict('Resolve changed, missing or truncated Library sources before pinning context')
        try:execution=observed(self.s,actor)
        except Conflict:
            execution=None
            with self.s.connect() as db:
                claim=self.current(db,task_id)
                a=self.s._one(db,'taskflow_assignments',claim['managedAssignmentId']) if claim and claim.get('managedAssignmentId') else None
            if a and a['workerId']==actor and a['state']=='RUNNING':
                proof=self.s.observer(a['providerThreadId'],a['providerTurnId']) if self.s.observer else None
                if proof and proof.get('state')=='terminal' and proof.get('status')=='completed' and proof.get('threadId')==a['providerThreadId'] and proof.get('turnId')==a['providerTurnId']:execution=a.get('executionEvidence')
        from contextlib import ExitStack
        with ExitStack() as boundary,self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');t,c=self.require(db,task_id,actor)
            if ids:
                from taskflow_learning_library import reserve_library
                reserve_library(self.s,boundary)
                checked=self.library(task_id,actor,ids=ids,task_db=db)
                if ({r['id']:r['bodyHash'] for r in checked['items']}!=
                        {r['id']:r['bodyHash'] for r in library['items']}):
                    raise Conflict('Library access, outcome or content changed before context acceptance')
            c.update(requirementsHash=pins(t)['requirementsHash'],executionEvidence=execution,library=library['items'],contextAssessment=assessment,phase='REVIEWING' if execution else 'CONTEXT_READY',startedAt=self.s.clock() if execution else None,heartbeatAt=self.s.clock());self.put(db,c)
            t.update(audit={'claimId':c['id'],'reviewer':actor,'phase':c['phase']},waitReason='Review execution observed' if execution else 'Context assessed; exact audit execution not yet observed');self.s._put(db,t)
            self.s._event(db,t,'audit-context',actor,{'claimId':c['id'],'records':[{'id':r['id'],'bodyHash':r['bodyHash']} for r in c['library']],'summary':assessment,'executionEvidence':execution})
        return c

    def release(self,task_id,actor,reason):
        from taskflow import text
        reason=text(reason,'release reason')
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self.s._one(db,'taskflow_tasks',task_id);c=self.current(db,task_id)
            if not c or actor not in (c['reviewer'],'operator-ui'):raise Conflict('Only the claiming auditor or operator may release this review')
            if c.get('managedAssignmentId'):
                a=self.s._one(db,'taskflow_assignments',c['managedAssignmentId'])
                if a and a['state'] in ('OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING'):raise Conflict('Managed audit retains its exact assignment; cancel/recover that turn before releasing the role claim')
            c.update(state='RELEASED',reason=reason,finishedAt=self.s.clock());self.put(db,c)
            t.update(audit=None)
            if t['state'] in REVIEW_STATES:t['station']='review:'+t['laneId']
            self.s._put(db,t);self.s._event(db,t,'audit-released',actor,{'reason':reason},source=c['reviewer'],destination=t['station'])
        return c

    def ask(self,task_id,actor,recipient,question,key):
        from taskflow import text,encoded,digest
        from workflow import allowed
        question=text(question,'research question');key=text(key,'question key',100)
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');t,c=self.require(db,task_id,actor);state,lanes=self.topology()
            seat=next((x for x in state['placements'] if x['agentId']==recipient),None)
            if not seat or lanes[seat['laneId']]['projectId']!=t['projectId'] or seat['role'] not in ('researcher','worker','writer','coordinator'):raise ValueError('Choose a project researcher or responsible task specialist')
            if not allowed(state,actor,recipient) or not allowed(state,recipient,actor):raise Conflict('A permitted question and return path are required')
            ident=digest([c['id'],key]);row=db.execute('SELECT body FROM taskflow_audit_questions WHERE id=?',(ident,)).fetchone()
            if row:
                q=json.loads(row[0])
                if (q['question'],q['recipient'])!=(question,recipient):raise Conflict('Question key reused with different content')
            else:
                q={'id':ident,'claimId':c['id'],'taskId':task_id,'reviewer':actor,'recipient':recipient,'question':question,'state':'OPEN','createdAt':self.s.clock()}
                db.execute('INSERT INTO taskflow_audit_questions VALUES(?,?,?,?,?)',(ident,task_id,recipient,'OPEN',encoded(q)))
                self.s._event(db,t,'audit-question',actor,q,source=actor,destination=recipient)
        delivery=self.s.flow.send(actor,recipient,'AUDIT_QUESTION '+task_id+' '+ident+'\n'+question,'audit-question-'+ident)
        return {**q,'delivery':delivery}

    def answer(self,ident,actor,answer,files):
        from taskflow import text,pin_files,encoded
        from workflow import allowed
        answer=text(answer,'research answer');evidence=pin_files(files)
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');r=db.execute('SELECT body FROM taskflow_audit_questions WHERE id=?',(ident,)).fetchone()
            if not r:raise ValueError('Unknown audit question')
            q=json.loads(r[0]);state,_=self.topology()
            if q['recipient']!=actor or not allowed(state,actor,q['reviewer']) or not allowed(state,q['reviewer'],actor):raise Conflict('Only the permitted exact recipient can answer')
            if q['state']=='ANSWERED':
                if q['answer']!=answer or q['evidence']!=evidence:raise Conflict('Answer already recorded with different content')
                return q
            if q['state']!='OPEN':raise Conflict('Question already resolved; retain its recorded resolution')
            q.update(state='ANSWERED',answer=answer,evidence=evidence,answeredAt=self.s.clock())
            db.execute('UPDATE taskflow_audit_questions SET state=?,body=? WHERE id=?',('ANSWERED',encoded(q),ident))
            t=self.s._one(db,'taskflow_tasks',q['taskId']);self.s._event(db,t,'audit-answer',actor,q,source=actor,destination=q['reviewer'])
        return q

    def resolve(self,ident,actor,reason,files):
        from taskflow import text,pin_files,encoded
        reason=text(reason,'question resolution');evidence=pin_files(files)
        if not evidence:raise ValueError('Evidence is required to resolve a question without a researcher reply')
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');r=db.execute('SELECT body FROM taskflow_audit_questions WHERE id=?',(ident,)).fetchone()
            if not r:raise ValueError('Unknown audit question')
            q=json.loads(r[0]);t,_=self.require(db,q['taskId'],actor)
            if q['state']!='OPEN':raise Conflict('Question already answered or resolved')
            q.update(state='RESOLVED',resolution=reason,resolvedBy=actor,evidence=evidence,resolvedAt=self.s.clock())
            db.execute('UPDATE taskflow_audit_questions SET state=?,body=? WHERE id=?',('RESOLVED',encoded(q),ident))
            self.s._event(db,t,'audit-question-resolved',actor,q)
        return q

    def validate_verdict(self,db,t,actor,verdict,boundary=None):
        from taskflow import digest
        _,c=self.require(db,t['taskId'],actor)
        if c.get('managedAssignmentId'):
            a=self.s._one(db,'taskflow_assignments',c['managedAssignmentId'])
            proof=self.s.observer(a.get('providerThreadId'),a.get('providerTurnId')) if a and self.s.observer else None
            if not a or a.get('cancelRequested') or a['state']!='RUNNING' or not proof or proof.get('state')!='terminal' or proof.get('status')!='completed' or proof.get('threadId')!=a.get('providerThreadId') or proof.get('turnId')!=a.get('providerTurnId'):raise Conflict('Managed audit verdict requires the exact observed terminal turn')
        if not c.get('executionEvidence'):raise Conflict('Record context assessment within your exact observed current audit turn before a verdict')
        if not c['contextAssessment']:raise Conflict('Record Library and research context coverage before issuing a verdict')
        if c['resultHash']!=digest(t['result']):raise Conflict('Returned result changed during audit')
        if c.get('requirementsHash') is not None or t.get('artifactDeliveryReconciliation'):
            if c.get('requirementsHash')!=pins(t)['requirementsHash']:
                raise Conflict('Task requirements changed; refresh audit context before a verdict')
        for q in self.questions(db,t['taskId']):
            if q['state'] not in ('ANSWERED','RESOLVED') and verdict=='accept':raise Conflict('Resolve the recorded research questions before accepting the result')
            for f in q.get('evidence',[]):
                if hashlib.sha256(Path(f['path']).read_bytes()).hexdigest()!=f['sha256']:raise Conflict('Research answer evidence changed')
        if c['library']:
            if boundary is None:raise Conflict('Library evidence requires the result transaction boundary')
            from taskflow_learning_library import reserve_library
            reserve_library(self.s,boundary)
        for r in c['library']:
            from inspector.library import Library
            if Library.source_state(r):raise Conflict('Pinned Library source changed during audit')
            current=self.library(t['taskId'],actor,ids=[r['id']],task_db=db)['items']
            if len(current)!=1 or current[0]['bodyHash']!=r['bodyHash']:raise Conflict('Library context version changed during audit')

    def finish(self,db,t,actor,verdict):
        c=self.current(db,t['taskId'])
        if c:
            c.update(state='FINISHED',verdict=verdict,finishedAt=self.s.clock());self.put(db,c)
        t['audit']=None

    def mutate(self,op,i,actor):
        if op=='audit-idle':return self.available(actor)
        if op=='audit-pause':return self.available(actor,False)
        if op=='audit-claim':return self.claim(i.get('taskId'),actor)
        if op=='audit-library':return self.library(i['taskId'],actor,i.get('query',''),i.get('offset',0))
        if op=='audit-context':return self.prepare(i['taskId'],actor,i.get('libraryItemIds',[]),i['assessment'])
        if op=='audit-release':return self.release(i['taskId'],actor,i['reason'])
        if op=='audit-question':return self.ask(i['taskId'],actor,i['recipient'],i['question'],i['key'])
        if op=='audit-answer':return self.answer(i['questionId'],actor,i['answer'],i.get('evidence',[]))
        if op=='audit-resolve':return self.resolve(i['questionId'],actor,i['reason'],i.get('evidence',[]))
        if op=='audit-heartbeat':
            with self.s.connect() as db:
                db.execute('BEGIN IMMEDIATE');_,c=self.require(db,i['taskId'],actor);c['heartbeatAt']=self.s.clock();self.put(db,c)
            return c
        raise ValueError('Unknown audit operation')
