"""Owner-published, current-head Library proposals. No executable eligibility.

The task transaction holds publication identity; Library owns the note. A note
written before a crash is historical/pending until its canonical link commits.
Retries use the same Library request key. No task/grant is replayed or renewed.
"""
import json
from pathlib import Path
import sqlite3
from urllib.parse import unquote,urlparse

import taskflow_learning as L
import taskflow_judgment as J
from taskflow_policy import timestamp
from workspace import Conflict

SCHEMA='''CREATE TABLE IF NOT EXISTS taskflow_library_lessons(
 lesson_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT NOT NULL,
 lane_id TEXT NOT NULL, note_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL, body_hash TEXT NOT NULL)'''


def install_schema(db):
    db.execute(SCHEMA)
    db.execute('''CREATE TABLE IF NOT EXISTS taskflow_library_publication_errors(
        lesson_id TEXT PRIMARY KEY, reason TEXT NOT NULL, retry_after REAL NOT NULL)''')


def require(ok,why):
    if not ok:raise Conflict(why)


def access_policy(config,task,now):
    ref=config['observation']['retentionPolicyRef'];L.reference(ref)
    spec=json.loads(Path(unquote(urlparse(ref['uri']).path)).read_text())
    keys={'schemaVersion','projectId','laneId','ownerId','readers','purpose','validUntil',
          'historyRetention','allowLibraryPublication','content','publicationMode'}
    require(isinstance(spec,dict) and set(spec)==keys
            and spec['schemaVersion']=='ke.taskflow.learning-library-policy.v1', 'Explicit Library access and retention policy required')
    require(spec['projectId']==task['projectId'] and spec['laneId']==task['laneId'] and spec['ownerId']==task['ownerId'],
            'Library policy belongs to another task owner or project')
    require(spec['allowLibraryPublication'] is True and spec['content']=='redacted-proposal-summary-and-provenance'
            and spec['historyRetention']=='manual-removal-only', 'Unsupported or disabled Library retention policy')
    require(spec['publicationMode'] in ('owner-requested','automatic-under-current-owner'), 'Explicit Library publication mode required')
    readers=spec['readers']
    require(isinstance(readers,list) and 1<=len(readers)<=30 and all(isinstance(x,str) and x for x in readers)
            and len(set(readers))==len(readers) and task['ownerId'] in readers
            and set(readers)<=set(config['observation']['readers']), 'Explicit observation-compatible Library readers required')
    require(isinstance(spec['purpose'],str) and 0<len(spec['purpose'])<=1000 and timestamp(spec['validUntil'])>now,
            'Current purpose-bound Library policy required')
    return spec


def bind_configuration(config,task,now):
    """Only a policy already pinned by operator configuration may enable the clock."""
    ref=config['observation']['retentionPolicyRef'];L.reference(ref)
    try:value=json.loads(Path(unquote(urlparse(ref['uri']).path)).read_text())
    except ValueError:return None  # Other existing retention contracts are unchanged.
    if not isinstance(value,dict) or value.get('schemaVersion')!='ke.taskflow.learning-library-policy.v1':return None
    spec=access_policy(config,task,now)
    return {'mode':spec['publicationMode'],'policyHash':L.digest(spec),'policyRef':ref}


def owner_current(store,task,actor):
    state=store._lane(task['projectId'],task['laneId'])
    require(not store.recovery_hold and state.get('enabled') is True, 'Original enabled workflow required')
    require(actor==task['ownerId'] and any(p['agentId']==actor and p['laneId']==task['laneId']
                and p.get('role')=='coordinator' for p in state['placements'])
            and any(p['agentId']==actor and p['laneId']==task['laneId'] and p['teamId']=='coordinator'
                    for p in state.get('teamLeads',[])), 'Current original workstream owner required')
    sessions=[s for s in store.flow.catalog()['sessions'] if s['agent_id']==actor]
    require(len(sessions)==1 and sessions[0].get('endpoint')==actor.removeprefix('codex:')
            and actor.startswith('codex:'), 'Exact registered owner session required')
    return state


def current(store,db,lesson_id):
    row=db.execute('SELECT body,body_hash,project_id FROM taskflow_judgment_lessons WHERE lesson_id=?',(lesson_id,)).fetchone()
    require(row is not None,'Outcome-bound proposal is missing')
    lesson=json.loads(row['body']);require(L.digest(lesson)==row['body_hash'],'Outcome-bound proposal changed')
    task=store._one(db,'taskflow_tasks',lesson['task']['taskId'])
    require(task and task.get('learningRequired') and task['state']=='DONE' and lesson['task']=={k:task[k] for k in ('projectId','taskId','taskVersion')},
            'Original completed learning task required')
    outcome,sha=L.head(db,lesson['outcomeRef']['outcomeId'])
    require(L.outcome_ref(outcome)==lesson['outcomeRef'] and outcome['status'] in ('ACCEPTED','CORRECTED')
            and outcome['task']==lesson['task'] and outcome['attemptId']==lesson['attemptId'], 'Learning outcome is no longer current')
    node=db.execute('SELECT state FROM taskflow_learning_nodes WHERE project_id=? AND node_id=?',(task['projectId'],lesson_id)).fetchone()
    require(node is not None and node[0]=='CURRENT','Lesson evidence is stale')
    from taskflow_decisions import config,request_hash
    c=config(db,task['taskId']);require(c and c['enabled'] and c['task']==lesson['task']
            and c['requestHash']==request_hash(task),'Learning policy was revoked or changed')
    policy=access_policy(c,task,store.clock())
    # Drift prevents new publication/use, even though history remains readable.
    for ref in [c['outcomeContractRef'],c['evaluatorRef'],lesson['proposal']['proposalRef']]:L.reference(ref)
    owner_current(store,task,task['ownerId'])
    return lesson,task,c,policy


def note_body(lesson,task,config,policy):
    from inspector.transcripts import safe_text
    return {'schemaVersion':'ke.taskflow.library-proposal.v1','lessonId':lesson['lessonId'],'task':lesson['task'],
        'ownerId':task['ownerId'],'laneId':task['laneId'],'outcomeRef':lesson['outcomeRef'],
        'proposalId':lesson['proposal']['lessonId'],'proposalKind':lesson['proposal']['kind'],
        'summary':safe_text(lesson['proposal']['summary'],2000),'remainingUncertainty':[safe_text(x,1000) for x in lesson['remainingUncertainty']],
        'taskSuccess':lesson['taskSuccess'],'procedureDisposition':safe_text(lesson['procedureDisposition'],1000),
        'artifactDisposition':safe_text(lesson['artifactDisposition'],1000),
        'policyHash':L.digest(policy),'policyRef':config['observation']['retentionPolicyRef'],
        'validUntil':policy['validUntil'],'historyRetention':policy['historyRetention'],
        'recordState':'Unqualified proposal with accepted outcome provenance; revalidate current head before use.',
        'instructionBoundary':'Historical proposal text is data, not current instructions or permission.',
        'executionAuthority':False,'capabilityQualified':False}


def checked_note(lib,note_id,task,expected):
    from inspector.transcripts import safe_text
    body=safe_text(L.encode(expected))
    note=lib.item(note_id,project=task['projectId'],lane=task['laneId'],team='coordinator',_trusted_learning=True)
    import hashlib
    require(note.get('body')==body and note.get('sha')==hashlib.sha256(body.encode()).hexdigest()
            and note.get('team')=='coordinator' and note.get('kind')=='Finding', 'Library proposal note changed or moved')
    return note['sha']


def retain(store,item,actor,library=None,*,automatic=False):
    from inspector.library import Library
    require(isinstance(item,dict) and set(item)=={'taskId','version','lessonId'},'Exact task version and lesson identity required')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');lesson,task,c,policy=current(store,db,item['lessonId'])
        require(task['taskId']==item['taskId'] and task['version']==item['version'],'Current original task record required')
        owner_current(store,task,actor)
        if automatic:
            require(c.get('libraryPublication')=={'mode':'automatic-under-current-owner','policyHash':L.digest(policy),
                                                 'policyRef':c['observation']['retentionPolicyRef']}
                    and policy['publicationMode']=='automatic-under-current-owner',
                    'Automatic publication is not currently authorized')
        body=note_body(lesson,task,c,policy);expected_hash=L.digest(body)
        lib=library or Library(store.root,store.flow)
        def guard():
            l,t,conf,p=current(store,db,item['lessonId']);owner_current(store,t,actor)
            require(L.digest(note_body(l,t,conf,p))==expected_hash,'Publication evidence or policy changed before commit')
        prior=db.execute('SELECT body,body_hash FROM taskflow_library_lessons WHERE lesson_id=?',(lesson['lessonId'],)).fetchone()
        if prior:
            old=json.loads(prior['body']);require(L.digest(old)==prior['body_hash'],'Publication link changed')
            require(old['recordHash']==expected_hash and old['ownerId']==actor,'Different publication owns this lesson')
            require(checked_note(lib,old['noteId'],task,body)==old['noteSha256'],'Publication note version changed')
            guard();return old
        result=lib.write('note',{'project':task['projectId'],'lane':task['laneId'],'team':'coordinator','kind':'Finding',
            'title':'Learning proposal: '+lesson['proposal']['kind'],'body':L.encode(body),
            'requestId':'taskflow-lesson:'+L.digest(lesson['lessonId'])},guard=guard)
        guard();sha=checked_note(lib,result['id'],task,body)
        link={'lessonId':lesson['lessonId'],'taskId':task['taskId'],'projectId':task['projectId'],'laneId':task['laneId'],
              'ownerId':actor,'noteId':result['id'],'noteSha256':sha,'recordHash':expected_hash,
              'usableUntil':timestamp(policy['validUntil']),'publicationMode':policy['publicationMode'],
              'requestOrigin':'existing-clock' if automatic else 'owner-request',
              'approvedBy':c['approvedBy'],'executionAuthority':False}
        db.execute('INSERT INTO taskflow_library_lessons VALUES(?,?,?,?,?,?,?)',
                   (lesson['lessonId'],task['taskId'],task['projectId'],task['laneId'],result['id'],L.encode(link),L.digest(link)))
        store._event(db,task,'learning-proposal-retained',actor,link)
        return link


def target_current(store,db,task_id,actor,*,record_version=None,task_version=None):
    """Current reader custody, shared by explicit reads and signed-context guards."""
    target=store._one(db,'taskflow_tasks',task_id)
    require(target and target['state'] not in ('CANCELLED','CANCELLING','DONE')
            and (record_version is None or target['version']==record_version)
            and (task_version is None or target['taskVersion']==task_version),
            'Current live receiving task required')
    state=owner_current(store,target,target['ownerId'])
    lane=store._one(db,'taskflow_policies',target['laneId'],'lane')
    require(lane and lane['enabled'] and lane['projectId']==target['projectId']
            and target.get('policyVersion')==lane['version'], 'Current receiving task policy required')
    if actor!=target['ownerId']:
        assignment=store._one(db,'taskflow_assignments',target.get('assignmentId'))
        worker=store._one(db,'taskflow_workers',actor)
        require(assignment and assignment['workerId']==actor and assignment['taskVersion']==target['taskVersion']
                and assignment['state'] in ('OFFERED','ACCEPTED','STARTING','RUNNING')
                and worker and worker['enabled'] and worker['projectId']==target['projectId']
                and worker['laneId']==target['laneId']
                and any(p['agentId']==actor and p['laneId']==target['laneId'] for p in state['placements']),
                'Receiving actor lacks current task custody')
    return target


def validated_record(store,db,lesson_id,target,actor,library=None):
    """Validate one exact record; never substitute a newly selected proposal."""
    from inspector.library import Library
    row=db.execute('SELECT * FROM taskflow_library_lessons WHERE lesson_id=?',(lesson_id,)).fetchone()
    require(row is not None,'Published proposal link is missing')
    link=json.loads(row['body']);require(L.digest(link)==row['body_hash'],'Publication link changed')
    lesson,origin,c,policy=current(store,db,lesson_id)
    require(origin['taskId']!=target['taskId'] and origin['ownerId']==target['ownerId']
            and origin['projectId']==target['projectId']==row['project_id']
            and origin['laneId']==target['laneId']==row['lane_id'], 'Distinct receiving task under same owner and scope required')
    require(actor in policy['readers'],'Reader not permitted by origin observation policy')
    require(set(target.get('capabilities',[])) & set(origin.get('capabilities',[])), 'Different task capability family')
    body=note_body(lesson,origin,c,policy)
    require(L.digest(body)==link['recordHash'] and checked_note(library or Library(store.root,store.flow),link['noteId'],origin,body)==link['noteSha256'],
            'Publication note or policy changed')
    return {'record':link,'context':body,'executionAuthority':False}


def is_proposal(root,row):
    """Recognize pending notes by type and committed notes by durable identity."""
    try:
        value=json.loads(row['body'])
        if isinstance(value,dict) and value.get('schemaVersion')=='ke.taskflow.library-proposal.v1':return True
    except (ValueError,TypeError):pass
    path=Path(root)/'board.sqlite3'
    if not path.is_file():return False
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='taskflow_library_lessons'").fetchone():return False
        return db.execute('SELECT 1 FROM taskflow_library_lessons WHERE note_id=?',(row['id'],)).fetchone() is not None


def authorize_document(store,db,row,actor):
    """Agent inspection and audit evidence use the same current origin policy."""
    if not is_proposal(store.root,row):return False
    require(isinstance(actor,str) and actor,'Scoped learning reader identity required')
    pin=db.execute('SELECT lesson_id,body,body_hash FROM taskflow_library_lessons WHERE note_id=?',(row['id'],)).fetchone()
    require(pin is not None,'Learning note is pending retention reconciliation')
    link=json.loads(pin['body']);require(L.digest(link)==pin['body_hash'],'Publication link changed')
    lesson,origin,c,policy=current(store,db,pin['lesson_id'])
    require(actor in policy['readers'],'Reader not permitted by origin observation policy')
    state=store.flow.read();lanes={l['id']:l for l in state['lanes']}
    require(any(p['agentId']==actor and lanes.get(p['laneId'],{}).get('projectId')==origin['projectId']
                for p in state['placements']), 'Reader no longer belongs to the origin project')
    body=note_body(lesson,origin,c,policy)
    from inspector.library import Library
    sha=checked_note(Library(store.root,store.flow),row['id'],origin,body)
    import hashlib
    require(row['project']==origin['projectId'] and row['lane']==origin['laneId']
            and hashlib.sha256(row['body'].encode()).hexdigest()==sha==link['noteSha256']
            and L.digest(body)==link['recordHash'],'Learning document differs from its current retained record')
    return True


def reserve_library(store,boundary):
    """Keep TaskFlow -> Library lock order through the caller's final commit."""
    from store import ClosingConnection
    from store import ordinary
    path=ordinary(store.root/'runtime/library/library.sqlite3')
    prior=getattr(boundary,'_learning_library_reservation',None)
    if prior:
        require(prior[0]==path,'Context boundary belongs to another Library')
        return prior[1]
    held=boundary.enter_context(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=5,factory=ClosingConnection))
    held.execute('BEGIN IMMEDIATE');boundary._learning_library_reservation=(path,held)
    return held


def context(store,item,actor,library=None,*,db=None):
    """Read with current receiving task and actor, never inherit the origin grant."""
    from inspector.library import Library
    require(isinstance(item,dict) and set(item)=={'taskId','version'},'Current receiving task/version required')
    if db is None:
        with store.connect() as owned:
            owned.execute('BEGIN');return context(store,item,actor,library,db=owned)
    target=target_current(store,db,item['taskId'],actor,record_version=item['version'])
    rows=db.execute('''SELECT p.* FROM taskflow_library_lessons p
      JOIN taskflow_judgment_lessons l ON l.lesson_id=p.lesson_id
      JOIN taskflow_learning_heads h ON h.outcome_id=l.outcome_id
       AND h.revision=l.outcome_revision AND h.body_hash=l.outcome_hash
      JOIN taskflow_learning_nodes n ON n.project_id=p.project_id AND n.node_id=p.lesson_id AND n.state='CURRENT'
      WHERE p.project_id=? AND p.lane_id=? AND
       (CASE WHEN json_valid(p.body) THEN json_extract(p.body,'$.usableUntil')>? ELSE 1 END)
      ORDER BY p.rowid DESC LIMIT 12''',(target['projectId'],target['laneId'],store.clock())).fetchall()
    result=[];omitted=[];lib=library or Library(store.root,store.flow)
    for row in rows:
        try:
            entry=validated_record(store,db,row['lesson_id'],target,actor,lib)
            if len(result)>=3:raise Conflict('Context proposal count bound')
            result.append(entry)
        except (Conflict,ValueError,OSError,KeyError,TypeError) as exc:
            omitted.append({'lessonId':row['lesson_id'],'reason':str(exc)[:300]})
    return {'schemaVersion':'ke.taskflow.learning-library-context.v1','taskId':target['taskId'],
            'records':result,'omitted':omitted,'scanLimit':12,'maxRecords':3,
            'executionAuthority':False,'modelCalls':0}


def tick(store,limit=8):
    """Fixed local publication procedure, only under explicit pre-action policy."""
    require(type(limit) is int and 1<=limit<=32,'Bounded Library publication batch required')
    if store.recovery_hold:return []
    with store.connect() as db:
        rows=db.execute('''SELECT l.lesson_id,l.body FROM taskflow_judgment_lessons l
          JOIN taskflow_learning_heads h ON h.outcome_id=l.outcome_id
           AND h.revision=l.outcome_revision AND h.body_hash=l.outcome_hash
          JOIN taskflow_judgment_returns j ON j.attempt_id=l.attempt_id
          JOIN taskflow_decision_config c ON c.task_id=j.task_id
          LEFT JOIN taskflow_library_lessons p ON p.lesson_id=l.lesson_id
          LEFT JOIN taskflow_library_publication_errors e ON e.lesson_id=l.lesson_id
          WHERE p.lesson_id IS NULL AND
           (CASE WHEN json_valid(c.body) THEN json_extract(c.body,'$.libraryPublication.mode')='automatic-under-current-owner' ELSE 1 END)
           AND (e.lesson_id IS NULL OR e.retry_after<=?) ORDER BY l.rowid LIMIT ?''',(store.clock(),limit)).fetchall()
    results=[]
    for row in rows:
        try:
            with store.connect() as db:
                lesson,task,c,policy=current(store,db,row['lesson_id'])
                binding=c.get('libraryPublication')
                require(binding=={'mode':'automatic-under-current-owner','policyHash':L.digest(policy),
                                  'policyRef':c['observation']['retentionPolicyRef']}
                        and policy['publicationMode']==binding['mode'],'Automatic publication policy changed')
            result=retain(store,{'taskId':task['taskId'],'version':task['version'],'lessonId':row['lesson_id']},task['ownerId'],automatic=True)
            with store.connect() as db:db.execute('DELETE FROM taskflow_library_publication_errors WHERE lesson_id=?',(row['lesson_id'],))
            results.append({'lessonId':row['lesson_id'],'state':'retained','noteId':result['noteId']})
        except (Conflict,ValueError,OSError,KeyError,TypeError) as exc:
            reason=(type(exc).__name__+': '+str(exc))[:1000]
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                prior=db.execute('SELECT reason FROM taskflow_library_publication_errors WHERE lesson_id=?',(row['lesson_id'],)).fetchone()
                db.execute('INSERT INTO taskflow_library_publication_errors VALUES(?,?,?) ON CONFLICT(lesson_id) DO UPDATE SET reason=excluded.reason,retry_after=excluded.retry_after',
                           (row['lesson_id'],reason,store.clock()+60))
                if prior is None or prior[0]!=reason:
                    source=db.execute('''SELECT j.task_id FROM taskflow_judgment_lessons l
                      JOIN taskflow_judgment_returns j ON j.attempt_id=l.attempt_id WHERE l.lesson_id=?''',(row['lesson_id'],)).fetchone()
                    if source:L.notice_failure(store,db,source[0],'learning-publication-held',{'lessonId':row['lesson_id'],'reason':reason})
            results.append({'lessonId':row['lesson_id'],'state':'held','reason':reason})
    return results


def display_state(root,note_id,body,now):
    """Read-only annotation for existing Library UI. It never grants reader access."""
    try:value=json.loads(body)
    except (ValueError,TypeError):return None
    if not isinstance(value,dict) or value.get('schemaVersion')!='ke.taskflow.library-proposal.v1':return None
    try:
        path=Path(root)/'board.sqlite3'
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT body,body_hash FROM taskflow_library_lessons WHERE note_id=?',(note_id,)).fetchone()
            if row is None:return 'Pending retention reconciliation; not effective learning'
            link=json.loads(row['body']);require(L.digest(link)==row['body_hash'],'Publication link changed')
            import hashlib
            require(link['recordHash']==L.digest(value) and link['noteSha256']==hashlib.sha256(body.encode()).hexdigest(),
                    'Library proposal content changed')
            h=db.execute('SELECT revision,body_hash FROM taskflow_learning_heads WHERE outcome_id=?',(value['outcomeRef']['outcomeId'],)).fetchone()
            n=db.execute('SELECT state FROM taskflow_learning_nodes WHERE node_id=? AND project_id=?',(value['lessonId'],value['task']['projectId'])).fetchone()
            if not h or (h['revision'],h['body_hash'])!=(value['outcomeRef']['revision'],value['outcomeRef']['sha256']) or not n or n[0]!='CURRENT':
                return 'Historical proposal; outcome changed or revoked'
            c=db.execute('SELECT body FROM taskflow_decision_config WHERE task_id=?',(value['task']['taskId'],)).fetchone()
            if not c or not json.loads(c[0])['enabled']:return 'Unavailable proposal; learning authority revoked'
            L.reference(value['policyRef'])
            if now>=timestamp(value['validUntil']):return 'Historical proposal; context access expired'
            return 'Recorded proposal; unqualified and requires task access check'
    except (Conflict,ValueError,KeyError,TypeError,OSError,sqlite3.Error):return 'Unavailable proposal; provenance requires reconciliation'
