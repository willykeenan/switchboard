"""Explicit same-ID legacy origin adoption. No capture, readiness, start or acceptance.

Caller authenticates with the existing scoped cycle credential. Source locators
are expectations only: the server resolves the registered original transcript,
opens its actual bytes, and revalidates actor/task/source before the transaction.
"""
import copy
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from workspace import Conflict
from agent_settings import Denied
from taskflow import digest, encoded
from inspector.transcripts import Transcripts, trusted_open, parse_event, text_parts, MAX_RECORD

OP='bind-legacy-origin'
FIELDS={'taskId','version','taskVersion','taskSha256','sourceRefsSha256','projectId','laneId','requestId','origin','source'}
ORIGIN={'providerThreadId','messageId','position','recordSha256','sha256'}


def validate(item):
    if not isinstance(item,dict) or set(item)!=FIELDS:raise ValueError('Exact legacy-origin fields required')
    if not isinstance(item['origin'],dict) or set(item['origin'])!=ORIGIN:raise ValueError('Exact original provider record locator required')
    if not isinstance(item['source'],dict) or set(item['source'])!={'path','sha256'}:raise ValueError('Exact existing request source required')
    for k in ('version','taskVersion'):
        if type(item[k]) is not int or item[k]<1:raise ValueError('Positive exact task versions required')
    if type(item['origin']['position']) is not int or item['origin']['position']<0:raise ValueError('Exact nonnegative record offset required')
    for value in [item['taskSha256'],item['sourceRefsSha256'],item['origin']['recordSha256'],item['origin']['sha256'],item['source']['sha256']]:
        if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('Exact SHA-256 required')
    for k in ('taskId','projectId','laneId','requestId'):
        if not isinstance(item[k],str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,199}',item[k]):raise ValueError('Invalid origin operation identity')


def owner(store,grants,token,item):
    actor=grants.resolve(store,token,OP)
    if actor['lane']!=item['laneId']:raise Denied('Origin binding is outside credential lane')
    state=store._lane(item['projectId'],item['laneId'])
    seats=[p for p in state['placements'] if p['laneId']==item['laneId'] and p['role']=='coordinator' and (p.get('teamId') or p['role'])=='coordinator']
    if len(seats)!=1 or seats[0]['agentId']!=actor['subject']:raise Denied('Exact current sole lane owner is required')
    if actor['endpoint']!=item['origin']['providerThreadId']:raise Denied('Original provider thread differs from authenticated owner')
    return actor


def source_proof(store,item,transcripts):
    origin=item['origin'];actor=store.flow.catalog()['sessions']
    matches=[s for s in actor if s.get('provider')=='codex' and s.get('endpoint')==origin['providerThreadId']]
    if len(matches)!=1:raise Conflict('Original transcript identity is missing or ambiguous')
    session,path=transcripts.resolve(matches[0]['agent_id'])
    if session.get('provider')!='codex' or session.get('endpoint')!=origin['providerThreadId'] or not path:raise Conflict('Registered original transcript is unavailable')
    with os.fdopen(trusted_open(path,transcripts.roots('codex')),'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise Conflict('Original transcript is not a regular file')
        f.seek(origin['position']);raw=f.readline(MAX_RECORD+1)
    if not raw.endswith(b'\n') or len(raw)>MAX_RECORD or hashlib.sha256(raw).hexdigest()!=origin['recordSha256']:raise Conflict('Original provider record bytes changed')
    event=json.loads(raw);payload=event.get('payload',{});parsed=parse_event(event,'codex')
    if event.get('type')!='response_item' or payload.get('type')!='message' or payload.get('role')!='user' or payload.get('id')!=origin['messageId'] or not parsed or any(x.get('sourceKind') for x in parsed) or not any(x['role']=='user' for x in parsed):raise Conflict('Exact actual user record required; generated control records are not user origin')
    body=text_parts(payload.get('content'))
    if hashlib.sha256(body.encode()).hexdigest()!=origin['sha256']:raise Conflict('Complete original user message changed')
    source=Path(item['source']['path'])
    if not source.is_absolute() or source.resolve()!=source or any(p.is_symlink() for p in [source,*source.parents]):raise Conflict('Original request source path is unsafe')
    with os.fdopen(trusted_open(source,[Path(source.anchor)]),'rb') as f:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size>8_000_000:raise Conflict('Bounded regular original request source required')
        request=f.read(8_000_001)
    if hashlib.sha256(request).hexdigest()!=item['source']['sha256']:raise Conflict('Original request source bytes changed')
    contained=request.decode('utf-8').strip()
    if not contained or contained not in body:raise Conflict('Existing request source is not exact contained text in original user record')
    return {'sourceRequestSha256':item['source']['sha256'],'containedTextSha256':hashlib.sha256(contained.encode()).hexdigest(),
            'recordSha256':origin['recordSha256'],'messageSha256':origin['sha256'],'providerThreadId':origin['providerThreadId'],
            'messageId':origin['messageId'],'position':origin['position'],'requestIsExactContainedText':True,'recordTimestamp':event.get('timestamp')}


def bind(store,grants,token,item,transcripts=None):
    validate(item);actor=owner(store,grants,token,item);transcripts=transcripts or Transcripts(store.flow)
    proof=source_proof(store,item,transcripts);request_hash=digest(item)
    source_key='owner-message:codex:'+item['origin']['providerThreadId']+':'+item['origin']['messageId']
    key_digest=digest({'operation':OP,'taskId':item['taskId'],'origin':item['origin']})
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_actor=owner(store,grants,token,item)
        if current_actor!=actor:raise Denied('Authenticated owner changed during origin observation')
        task=store._one(db,'taskflow_tasks',item['taskId'])
        if not task or task['projectId']!=item['projectId'] or task['laneId']!=item['laneId'] or task.get('ownerId')!=actor['subject']:raise Denied('Existing task is not owned by this authenticated lane owner')
        key_row=db.execute('SELECT digest,task_id FROM taskflow_keys WHERE key=?',(source_key,)).fetchone()
        prior=task.get('originAdoption')
        if prior:
            if not key_row or key_row['task_id']!=task['taskId'] or key_row['digest']!=key_digest:raise Conflict('Original-message deduplication binding changed')
            if prior.get('requestId')!=item['requestId'] or prior.get('requestSha256')!=request_hash or prior.get('actor')!=actor['subject']:raise Conflict('Task already has a different origin adoption')
            origins=[r for r in task.get('sourceRefs',[]) if r.get('adoptionId')==prior['id']]
            if origins!=[dict(kind='owner-conversation',**item['origin'],adoptionId=prior['id'],provenance='legacy-origin-adoption')]:raise Conflict('Recorded origin adoption source changed')
            if not any(json.loads(r[0]).get('detail',{}).get('adoptionId')==prior['id'] for r in db.execute('SELECT body FROM taskflow_events WHERE task_id=?',(task['taskId'],))):raise Conflict('Origin adoption event is missing')
            return {'disposition':'already-bound','taskId':task['taskId'],'version':task['version'],'taskVersion':task['taskVersion'],'adoption':prior,'executionCreated':False,'historicalUserActionCreated':False}
        if key_row:raise Conflict('Original message already belongs to another capture or adoption')
        if task['version']!=item['version'] or task['taskVersion']!=item['taskVersion'] or digest(task)!=item['taskSha256'] or digest(task.get('sourceRefs',[]))!=item['sourceRefsSha256']:raise Conflict('Existing task version or complete source preimage changed')
        if task['state'] not in ('CAPTURED','QUEUED','READY') or task.get('assignmentId') or any(task.get(k) for k in ('result','review','approval','completion','libraryRetention')):raise Conflict('Origin adoption requires a quiescent unexecuted task')
        if db.execute("SELECT 1 FROM taskflow_assignments WHERE task_id=?",(task['taskId'],)).fetchone():raise Conflict('Existing assignment history requires separate reconciliation; never mutate its source pins')
        refs=task.get('sourceRefs',[])
        if any(r.get('kind')=='owner-conversation' for r in refs):raise Conflict('Existing owner-conversation origin cannot be replaced')
        original=[r for r in refs if r.get('kind')=='user-request' and r.get('path')==item['source']['path'] and r.get('sha256')==item['source']['sha256'] and r.get('providerThreadId')==actor['endpoint']]
        if len(original)!=1:raise Conflict('Exact original task request source must already be recorded')
        if len(refs)>=50:raise Conflict('Origin source-reference limit reached')
        if source_proof(store,item,transcripts)!=proof:raise Conflict('Original source changed before origin commit')
        if owner(store,grants,token,item)!=actor:raise Denied('Owner grant revoked before origin commit')
        ident=digest({'taskId':task['taskId'],'operation':item['requestId'],'request':request_hash})
        adoption={'schemaVersion':'ke.legacy-origin-adoption.v1','id':ident,'requestId':item['requestId'],'requestSha256':request_hash,
                  'actor':actor['subject'],'providerThreadId':actor['endpoint'],'at':store.clock(),'beforeTaskSha256':item['taskSha256'],
                  'beforeRecordVersion':task['version'],'taskVersion':task['taskVersion'],'proof':proof,
                  'provenance':'Existing legacy task adopted from reverified original user record; not a historical in-game capture',
                  'historicalUserActionCreated':False,'executionCreated':False}
        task['sourceRefs']=[*refs,dict(kind='owner-conversation',**item['origin'],adoptionId=ident,provenance='legacy-origin-adoption')]
        task['originAdoption']=adoption
        if len(encoded(task['sourceRefs']))>16000:raise Conflict('Origin source-reference payload limit reached')
        store._put(db,task)
        db.execute('INSERT INTO taskflow_keys VALUES(?,?,?)',(source_key,key_digest,task['taskId']))
        store._event(db,task,'legacy-origin-adopted',actor['subject'],{'adoptionId':ident,'requestId':item['requestId'],'beforeTaskSha256':item['taskSha256'],'recordSha256':proof['recordSha256'],'historicalUserActionCreated':False,'executionCreated':False})
        return {'disposition':'bound-existing','taskId':task['taskId'],'version':task['version'],'taskVersion':task['taskVersion'],'adoption':adoption,'executionCreated':False,'historicalUserActionCreated':False}
