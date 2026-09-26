"""Append-only evidence observations; never part of the task work contract."""
import json
import uuid
from workspace import Conflict

SCHEMA='''
CREATE TABLE IF NOT EXISTS taskflow_annotations(seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,actor TEXT NOT NULL,key TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(task_id,actor,key));
CREATE TABLE IF NOT EXISTS taskflow_annotation_access(task_id TEXT NOT NULL,actor TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(task_id,actor));
'''

class Annotations:
    def __init__(self,store):self.s=store
    def registered(self,actor):return bool(self.s.flow and any(x['agent_id']==actor for x in self.s.flow.catalog()['sessions']))
    def task(self,db,ident):return self.s._one(db,'taskflow_tasks',ident) or next((t for t in self.s.legacy() if t['taskId']==ident),None)
    def list(self,db,ident):return [dict(json.loads(r['body']),sequence=r['seq']) for r in db.execute('SELECT seq,body FROM taskflow_annotations WHERE task_id=? ORDER BY seq',(ident,))]
    def detail(self,ident):
        with self.s.connect() as db:return {'notes':self.list(db,ident),'contributors':[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_annotation_access WHERE task_id=?',(ident,))]}
    def access(self,values,actor):
        from taskflow import encoded,text
        if self.s.recovery_hold:raise Conflict('Restored copies cannot change annotation access')
        if set(values)-{'taskId','contributor','enabled','version'}:raise ValueError('Annotation access changes only the exact task contributor')
        contributor=text(values.get('contributor'),'exact contributor',200)
        if type(values.get('enabled')) is not bool:raise ValueError('Explicit enabled state required')
        if not self.registered(contributor):raise Conflict('Choose an existing registered contributor')
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self.task(db,values.get('taskId'))
            if not t or actor!='operator-ui' and (actor!=t.get('ownerId') or not self.registered(actor)):raise Conflict('Only this task owner or operator may grant evidence-note access')
            prior=db.execute('SELECT body FROM taskflow_annotation_access WHERE task_id=? AND actor=?',(t['taskId'],contributor)).fetchone()
            version=json.loads(prior[0])['version'] if prior else 0
            if values.get('version',0)!=version:raise Conflict('Annotation access changed; refresh before saving')
            value={'version':version+1,'taskId':t['taskId'],'contributor':contributor,'enabled':values['enabled'],'grantedBy':actor,'at':self.s.clock(),'scope':'evidence observations only'}
            db.execute('INSERT OR REPLACE INTO taskflow_annotation_access VALUES(?,?,?)',(t['taskId'],contributor,encoded(value)))
            self.s._event(db,t,'annotation-access',actor,value);return value
    def add(self,values,actor):
        from taskflow import encoded,text,pin_files,digest
        if self.s.recovery_hold:raise Conflict('Restored copies cannot annotate live tasks')
        if set(values)-{'taskId','key','text','evidence','supersedes'}:raise ValueError('Evidence notes cannot change instructions, sources, criteria, state or ownership; use the task amendment action')
        ident=text(values.get('taskId'),'task ID',200);key=text(values.get('key'),'stable annotation key',200);body=text(values.get('text'),'observation',12000)
        evidence=pin_files(values.get('evidence',[]));supersedes=values.get('supersedes')
        if supersedes is not None:supersedes=text(supersedes,'prior note ID',200)
        fingerprint=digest({'text':body,'evidence':evidence,'supersedes':supersedes})
        with self.s.connect() as db:
            db.execute('BEGIN IMMEDIATE');t=self.task(db,ident)
            if not t:raise Conflict('Annotate an existing exact task; do not create a replacement')
            grant=db.execute('SELECT body FROM taskflow_annotation_access WHERE task_id=? AND actor=?',(ident,actor)).fetchone()
            intrinsic=actor in (t.get('ownerId'),t.get('workerId'),(t.get('audit') or {}).get('reviewer'))
            if actor!='operator-ui' and (not self.registered(actor) or not intrinsic and not (grant and json.loads(grant[0])['enabled'])):raise Conflict('Exact task evidence-note access is required; historical ownership is unchanged')
            old=db.execute('SELECT body FROM taskflow_annotations WHERE task_id=? AND actor=? AND key=?',(ident,actor,key)).fetchone()
            if old:
                note=json.loads(old[0])
                if note['fingerprint']!=fingerprint:raise Conflict('Annotation key already belongs to different evidence')
                return note
            if supersedes and not any(n['id']==supersedes for n in self.list(db,ident)):raise Conflict('The corrected observation must belong to this exact task')
            note={'id':str(uuid.uuid4()),'taskId':ident,'actor':actor,'text':body,'evidence':evidence,'supersedes':supersedes,'fingerprint':fingerprint,'at':self.s.clock(),'observedTaskVersion':t['taskVersion'],'observedRecordVersion':t['version'],'observedSourceVersion':t.get('intake',{}).get('sourceVersion',t['version'] if t.get('legacy') else None),'meaning':'Context only; does not amend instructions or verify completion'}
            db.execute('INSERT INTO taskflow_annotations(task_id,actor,key,body) VALUES(?,?,?,?)',(ident,actor,key,encoded(note)))
            # No _put: source rows, semantic/record versions and assignments stay byte-identical.
            self.s._event(db,t,'evidence-note',actor,note);return note
