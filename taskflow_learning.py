"""Canonical outcome revisions and replayable learning projections.

Internal TaskFlow transaction component, not an authorization or execution API.
The decision adapter must seal an admitted decision before work; this module has
no operation that creates those seals or starts work. Review/correction events
must be emitted by the existing authenticated TaskFlow path in the SAME write
transaction. Decision admission, runtime consumption, and installation remain
separate work; normal review and explicit operator correction are wired here.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlparse, unquote
from workspace import Conflict

SCHEMA = '''
CREATE TABLE IF NOT EXISTS taskflow_learning_decisions(
 decision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_id TEXT UNIQUE NOT NULL,
 project_id TEXT NOT NULL, body TEXT NOT NULL, body_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_learning_epochs(project_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_learning_outcomes(
 outcome_id TEXT NOT NULL, revision INTEGER NOT NULL, event_id TEXT UNIQUE NOT NULL,
 project_id TEXT NOT NULL, attempt_id TEXT NOT NULL, contract_hash TEXT NOT NULL,
 body TEXT NOT NULL, body_hash TEXT NOT NULL, source_event_hash TEXT NOT NULL,
 PRIMARY KEY(outcome_id,revision));
CREATE TABLE IF NOT EXISTS taskflow_learning_heads(
 outcome_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, project_id TEXT NOT NULL,
 attempt_id TEXT NOT NULL, contract_hash TEXT NOT NULL, body_hash TEXT NOT NULL,
 UNIQUE(attempt_id,contract_hash));
CREATE TABLE IF NOT EXISTS taskflow_learning_outbox(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
 project_id TEXT NOT NULL, epoch INTEGER NOT NULL, body TEXT NOT NULL, body_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_learning_nodes(
 project_id TEXT NOT NULL, node_id TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(project_id,node_id));
CREATE TABLE IF NOT EXISTS taskflow_learning_dependencies(
 project_id TEXT NOT NULL, dependent_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
 PRIMARY KEY(project_id,dependent_id,dependency_id));
CREATE INDEX IF NOT EXISTS taskflow_learning_reverse ON taskflow_learning_dependencies(project_id,dependency_id);
CREATE TABLE IF NOT EXISTS taskflow_learning_consumed(
 consumer_id TEXT NOT NULL, event_id TEXT NOT NULL, head_hash TEXT NOT NULL,
 PRIMARY KEY(consumer_id,event_id));
CREATE TABLE IF NOT EXISTS taskflow_learning_effective(
 consumer_id TEXT NOT NULL, attempt_id TEXT NOT NULL, contract_hash TEXT NOT NULL,
 outcome_id TEXT NOT NULL, revision INTEGER NOT NULL, body_hash TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(consumer_id,attempt_id,contract_hash));
CREATE TABLE IF NOT EXISTS taskflow_learning_consumer_errors(
 consumer_id TEXT NOT NULL, event_id TEXT NOT NULL, reason TEXT NOT NULL,
 retry_after REAL NOT NULL, PRIMARY KEY(consumer_id,event_id));
'''


class LearningConflict(ValueError):
    pass


def require(ok, message):
    if not ok:raise LearningConflict(message)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def install_schema(db):
    # executescript would implicitly commit its caller's transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip():db.execute(statement)
    from taskflow_judgment import install_schema as install_judgments
    install_judgments(db)
    from taskflow_learning_library import install_schema as install_library_lessons
    install_library_lessons(db)


def transaction(db):
    require(db.in_transaction, 'Existing TaskFlow write transaction required')


def identity(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', value), 'Invalid identity')
    return value


def reference(value):
    require(isinstance(value, dict) and set(value)=={'uri','sha256'}, 'Exact artifact reference required')
    require(isinstance(value['sha256'], str) and re.fullmatch('[0-9a-f]{64}',value['sha256']), 'Invalid artifact digest')
    uri=urlparse(value['uri']);require(uri.scheme=='file' and not uri.netloc and not uri.query and not uri.fragment, 'Local artifact required')
    path=Path(unquote(uri.path))
    require(path.is_absolute() and path.resolve()==path and not path.is_symlink() and path.is_file()
            and path.stat().st_size<=8_000_000, 'Bounded regular artifact required')
    require(hashlib.sha256(path.read_bytes()).hexdigest()==value['sha256'], 'Referenced artifact changed')
    return value


def epoch(db, project):
    row=db.execute('SELECT epoch FROM taskflow_learning_epochs WHERE project_id=?',(project,)).fetchone()
    return row[0] if row else 0


def head(db, outcome):
    row=db.execute('''SELECT o.* FROM taskflow_learning_heads h JOIN taskflow_learning_outcomes o
      ON o.outcome_id=h.outcome_id AND o.revision=h.revision AND o.body_hash=h.body_hash
      WHERE h.outcome_id=?''',(outcome,)).fetchone()
    require(row is not None, 'Missing or inconsistent canonical outcome head')
    value=json.loads(row['body']);require(digest(value)==row['body_hash'], 'Outcome body changed')
    return value, row['body_hash']


def outcome_ref(value):
    return {'outcomeId':value['outcomeId'],'revision':value['revision'],'sha256':digest(value)}


def evidence_key(ref):
    return 'outcome:'+ref['outcomeId']+':'+str(ref['revision'])+':'+ref['sha256']


def current(db, project, expected_epoch, outcomes=(), nodes=()):
    """Call inside the actual admission AND result-acceptance write transaction."""
    transaction(db)
    require(type(expected_epoch) is int and expected_epoch==epoch(db,project), 'Learning epoch changed; reseal decision')
    for ref in outcomes:
        value, sha=head(db,ref['outcomeId'])
        require(value['task']['projectId']==project and outcome_ref(value)==ref
                and value['status'] in ('ACCEPTED','CORRECTED'), 'Outcome dependency is no longer effective')
    for key in nodes:
        row=db.execute('SELECT state FROM taskflow_learning_nodes WHERE project_id=? AND node_id=?',(project,key)).fetchone()
        require(row is not None and row[0]=='CURRENT', 'Learning dependency is stale or missing')
    return True


def add_dependency_node(db, project, key, *, outcome_refs=(), node_refs=(), expected_epoch):
    """Record immutable evidence links only; CURRENT does not qualify executable code."""
    identity(key);transaction(db);current(db,project,expected_epoch,outcome_refs,node_refs)
    require(outcome_refs or node_refs, 'Evidence dependency required')
    require(key not in node_refs, 'Self dependency is invalid')
    body={'outcomes':list(outcome_refs),'nodes':list(node_refs),'epoch':expected_epoch}
    existing=db.execute('SELECT body FROM taskflow_learning_nodes WHERE project_id=? AND node_id=?',(project,key)).fetchone()
    if existing:
        require(existing[0]==encode(body), 'Immutable learning version already exists')
        return
    db.execute('INSERT INTO taskflow_learning_nodes VALUES(?,?,?,?)',(project,key,'CURRENT',encode(body)))
    for dep in [*(evidence_key(r) for r in outcome_refs),*node_refs]:
        db.execute('INSERT OR IGNORE INTO taskflow_learning_dependencies VALUES(?,?,?)',(project,key,dep))


def invalidate(db, project, old_ref):
    # UNION terminates even if corrupted history contains a dependency cycle.
    rows=db.execute('''WITH RECURSIVE stale(id) AS (
      SELECT dependent_id FROM taskflow_learning_dependencies WHERE project_id=? AND dependency_id=?
      UNION SELECT d.dependent_id FROM taskflow_learning_dependencies d JOIN stale s
      ON d.dependency_id=s.id WHERE d.project_id=?) SELECT id FROM stale''',
      (project,evidence_key(old_ref),project)).fetchall()
    for row in rows:
        db.execute("UPDATE taskflow_learning_nodes SET state='STALE' WHERE project_id=? AND node_id=?",(project,row[0]))
    return sorted(r[0] for r in rows)


def event(db, task_id, seq):
    transaction(db)
    row=db.execute('SELECT task_id,body FROM taskflow_events WHERE seq=?',(seq,)).fetchone()
    require(row is not None and row['task_id']==task_id, 'Canonical TaskFlow event required')
    e=json.loads(row['body']);require(e.get('taskId')==task_id, 'Event task identity differs')
    trow=db.execute('SELECT body FROM taskflow_tasks WHERE id=?',(task_id,)).fetchone()
    require(trow is not None, 'Canonical task required')
    task=json.loads(trow[0]);require(task.get('taskId')==task_id, 'Task index/body differs')
    return e,task


def validate_evaluation(value):
    keys={'decisionId','attemptId','status','taskSuccess','procedureDisposition','artifactDisposition','reason','evidence'}
    require(isinstance(value,dict) and set(value)==keys, 'Exact reviewed evaluation required')
    for key in ('decisionId','attemptId'):identity(value[key])
    require(value['status'] in ('ACCEPTED','CORRECTED','REVOKED','PENDING','CENSORED'), 'Unknown outcome status')
    require(type(value['taskSuccess']) is bool if value['status'] in ('ACCEPTED','CORRECTED') else value['taskSuccess'] is None,
            'Only accepted or corrected outcomes have a binary label')
    for key in ('procedureDisposition','artifactDisposition','reason'):
        require(isinstance(value[key],str) and 0<len(value[key])<=2000, 'Bounded evaluation text required')
    require(isinstance(value['evidence'],list) and 1<=len(value['evidence'])<=30, 'Bounded outcome evidence required')
    for ref in value['evidence']:reference(ref)


def record_review(db, task_id, seq, *, store=None):
    """Called after the normal review event is inserted, before its transaction commits."""
    e,task=event(db,task_id,seq)
    value=(e.get('detail') or {}).get('learningOutcome')
    if value is None:return None  # Existing tasks remain untouched; no retrospective labels.
    validate_evaluation(value)
    require(e['kind']=='review-accept' and e['detail']==task.get('review') and e['detail'].get('verdict')=='accept', 'Current normal acceptance required')
    require(e['taskVersion']==task['taskVersion'] and e['actor']==e['detail'].get('reviewer')
            and e['actor'] not in (task.get('ownerId'),task.get('workerId')), 'Independent exact review required')
    require(task.get('state')=='DONE' and task.get('completion'), 'Actual task completion required; intermediate approval is not outcome success')
    require(value['status'] in ('ACCEPTED','PENDING','CENSORED'), 'Initial outcome cannot be a correction or revocation')
    return append(db,task,e,seq,value,previous=None,store=store)


def record_correction(db, task_id, seq):
    """Consumes only an explicit human correction recorded by authenticated TaskFlow."""
    e,task=event(db,task_id,seq);detail=e.get('detail') or {};value=detail.get('learningOutcome')
    require(e['kind']=='learning-outcome-corrected' and e['actor']=='operator-ui', 'Explicit authenticated human correction required')
    validate_evaluation(value);require(value['status'] in ('CORRECTED','REVOKED','PENDING','CENSORED'), 'Correction status required')
    previous=detail.get('supersedes');require(isinstance(previous,dict), 'Exact predecessor required')
    return append(db,task,e,seq,value,previous)


def append(db, task, source_event, seq, evaluation, previous, *, store=None):
    transaction(db);project=task['projectId'];attempt=evaluation['attemptId']
    event_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'ke-learning-event:'+task['taskId']+':'+str(seq)))
    found=db.execute('SELECT body,source_event_hash FROM taskflow_learning_outcomes WHERE event_id=?',(event_id,)).fetchone()
    if found:
        require(found['source_event_hash']==digest(source_event), 'Duplicate event content changed')
        return json.loads(found['body'])
    row=db.execute('SELECT * FROM taskflow_learning_decisions WHERE decision_id=?',(evaluation['decisionId'],)).fetchone()
    require(row is not None, 'Previously admitted decision required; no post-hoc learning seal')
    seal=json.loads(row['body']);require(digest(seal)==row['body_hash'], 'Decision seal changed')
    require(row['task_id']==task['taskId']==seal.get('taskId') and row['attempt_id']==attempt==seal.get('attemptId')
            and row['project_id']==project==seal.get('projectId') and seal.get('decisionId')==evaluation['decisionId']
            and seal.get('state')=='ADMITTED', 'Decision/attempt/project binding differs')
    arow=db.execute('SELECT body FROM taskflow_assignments WHERE id=?',(attempt,)).fetchone()
    require(arow is not None, 'Original assignment required')
    assignment=json.loads(arow[0]);require(assignment.get('id')==attempt and assignment.get('taskId')==task['taskId']
        and assignment.get('taskVersion')==seal['taskVersion'], 'Original assignment differs')
    # Withdrawing an existing label must work even when its original artifacts
    # are precisely what has disappeared or lost integrity. Initial acceptance
    # and replacement labels still require current files. The withdrawal branch
    # below binds its historical references to the immutable canonical head.
    if previous is None or evaluation['status']!='REVOKED':
        for field in ('outcomeContractRef','evaluatorRef'):reference(seal[field])
    contract=digest(seal['outcomeContractRef']);outcome_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'ke-outcome:'+attempt+':'+contract))
    if previous is None:
        if task.get('learningRequired'):
            from taskflow_decisions import validate_learning_acceptance
            validate_learning_acceptance(db,task,seal,store)
        require(assignment.get('state')=='RETURNED' and task.get('assignmentId')==attempt
                and assignment.get('result')==task.get('result') and seal['taskVersion']==task['taskVersion'], 'Returned original attempt/result required')
        require(source_event['detail'].get('resultHash')==digest(task.get('result')), 'Review result binding changed')
        current(db,project,seal['learningEpoch'],seal.get('outcomeDependencies',()),seal.get('nodeDependencies',()))
        require(db.execute('SELECT 1 FROM taskflow_learning_heads WHERE outcome_id=?',(outcome_id,)).fetchone() is None, 'Outcome already has a head; correction required')
        revision=1;invalidated=[]
    else:
        old,old_hash=head(db,outcome_id)
        require(outcome_ref(old)==previous and old['task']['projectId']==project
                and old['attemptId']==attempt and old['decisionId']==evaluation['decisionId'], 'Correction predecessor differs')
        require(all(old[field]==seal[field] for field in ('outcomeContractRef','evaluatorRef')),
                'Historical correction references differ from the canonical predecessor')
        revision=old['revision']+1;invalidated=invalidate(db,project,previous)
    next_epoch=epoch(db,project)+1
    db.execute('INSERT INTO taskflow_learning_epochs VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET epoch=excluded.epoch',(project,next_epoch))
    accepted=evaluation['status'] in ('ACCEPTED','CORRECTED')
    body={'schemaVersion':'ke.agentbrain.accepted-outcome.v2','fixture':False,'executionAuthority':False,
          'outcomeId':outcome_id,'revision':revision,'supersedes':previous,'decisionId':evaluation['decisionId'],
          'attemptId':attempt,'task':{'projectId':project,'taskId':task['taskId'],'taskVersion':seal['taskVersion']},
          **{k:evaluation[k] for k in ('status','taskSuccess','procedureDisposition','artifactDisposition','reason','evidence')},
          'outcomeContractRef':seal['outcomeContractRef'],'evaluatorRef':seal['evaluatorRef'],
          'acceptedAt':datetime.fromtimestamp(source_event['at'],timezone.utc).isoformat() if accepted else None,
          'acceptanceEventId':event_id,'transactionRef':{'uri':'taskflow://event/'+str(seq),'sha256':digest(source_event)},'learningEpoch':next_epoch}
    sha=digest(body)
    db.execute('INSERT INTO taskflow_learning_outcomes VALUES(?,?,?,?,?,?,?,?,?)',
               (outcome_id,revision,event_id,project,attempt,contract,encode(body),sha,digest(source_event)))
    db.execute('INSERT INTO taskflow_learning_heads VALUES(?,?,?,?,?,?) ON CONFLICT(outcome_id) DO UPDATE SET revision=excluded.revision,body_hash=excluded.body_hash',
               (outcome_id,revision,project,attempt,contract,sha))
    notice={'eventId':event_id,'outcomeId':outcome_id,'previous':previous,'head':outcome_ref(body),
            'projectId':project,'learningEpoch':next_epoch,'invalidatedNodes':invalidated,'cause':source_event['kind']}
    db.execute('INSERT INTO taskflow_learning_outbox(event_id,project_id,epoch,body,body_hash) VALUES(?,?,?,?,?)',
               (event_id,project,next_epoch,encode(notice),digest(notice)))
    return body


def consume(db, consumer, event_id):
    """Projection and dedup commit together. A crash before ack safely replays."""
    transaction(db);identity(consumer)
    row=db.execute('SELECT * FROM taskflow_learning_outbox WHERE event_id=?',(event_id,)).fetchone()
    require(row is not None, 'Unknown canonical acceptance event')
    notice=json.loads(row['body']);require(digest(notice)==row['body_hash'] and notice['eventId']==event_id, 'Outbox event changed')
    if db.execute('SELECT 1 FROM taskflow_learning_consumed WHERE consumer_id=? AND event_id=?',(consumer,event_id)).fetchone():return {'duplicate':True}
    # Arrival order is irrelevant: project only the current canonical head.
    value,sha=head(db,notice['outcomeId']);require(value['task']['projectId']==row['project_id'], 'Outbox project differs')
    contract=digest(value['outcomeContractRef'])
    db.execute('INSERT INTO taskflow_learning_effective VALUES(?,?,?,?,?,?,?) ON CONFLICT(consumer_id,attempt_id,contract_hash) DO UPDATE SET outcome_id=excluded.outcome_id,revision=excluded.revision,body_hash=excluded.body_hash,body=excluded.body',
               (consumer,value['attemptId'],contract,value['outcomeId'],value['revision'],sha,encode(value)))
    from taskflow_judgment import project
    project(db,value)
    db.execute('INSERT INTO taskflow_learning_consumed VALUES(?,?,?)',(consumer,event_id,sha))
    return {'duplicate':False,'head':outcome_ref(value),'trainingLabel':value['taskSuccess']}


def effective(db, consumer):
    """A stale consumer projection never becomes effective training data."""
    rows=db.execute('''SELECT p.body,p.body_hash FROM taskflow_learning_effective p JOIN taskflow_learning_heads h
      ON h.outcome_id=p.outcome_id AND h.revision=p.revision AND h.body_hash=p.body_hash
      WHERE p.consumer_id=? ORDER BY p.attempt_id,p.contract_hash''',(consumer,)).fetchall()
    result=[]
    for row in rows:
        body=json.loads(row['body']);require(digest(body)==row['body_hash'], 'Projection changed')
        if body['status'] in ('ACCEPTED','CORRECTED'):result.append(body)
    return result


def drain(store, limit=32):
    """Bounded existing-clock consumer; no provider, dispatcher or Library writes.

    This advances the task-owned staging projection only. Library publication and
    capability qualification remain separate owner-controlled transitions.
    """
    require(type(limit) is int and 1<=limit<=100, 'Bounded consumer batch required')
    if store.recovery_hold:return []
    consumer='taskflow-judgment-proposals-v1'
    with store.connect() as db:
        rows=db.execute('''SELECT o.event_id,o.body FROM taskflow_learning_outbox o
          LEFT JOIN taskflow_learning_consumed c ON c.consumer_id=? AND c.event_id=o.event_id
          LEFT JOIN taskflow_learning_consumer_errors e ON e.consumer_id=? AND e.event_id=o.event_id
          WHERE c.event_id IS NULL AND (e.event_id IS NULL OR e.retry_after<=?)
          ORDER BY o.seq LIMIT ?''',(consumer,consumer,store.clock(),limit)).fetchall()
    results=[]
    for row in rows:
        event_id=row['event_id']
        try:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                result=consume(db,consumer,event_id)
                db.execute('DELETE FROM taskflow_learning_consumer_errors WHERE consumer_id=? AND event_id=?',(consumer,event_id))
            results.append({'eventId':event_id,'state':'consumed',**result})
        except (ValueError,Conflict,OSError,KeyError,TypeError) as exc:
            reason=(type(exc).__name__+': '+str(exc))[:1000]
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                prior=db.execute('SELECT reason FROM taskflow_learning_consumer_errors WHERE consumer_id=? AND event_id=?',(consumer,event_id)).fetchone()
                db.execute('INSERT INTO taskflow_learning_consumer_errors VALUES(?,?,?,?) ON CONFLICT(consumer_id,event_id) DO UPDATE SET reason=excluded.reason,retry_after=excluded.retry_after',
                           (consumer,event_id,reason,store.clock()+60))
                # Publish a new substantive failure to the ordinary task stream,
                # without changing the original accepted result or task version.
                if prior is None or prior[0]!=reason:
                    found=db.execute('''SELECT a.task_id FROM taskflow_learning_outcomes o
                        JOIN taskflow_assignments a ON a.id=o.attempt_id WHERE o.event_id=?''',(event_id,)).fetchone()
                    if found:
                        notice_failure(store,db,found[0],'learning-consumer-held',{'eventId':event_id,'reason':reason})
            results.append({'eventId':event_id,'state':'held','reason':reason})
    return results


def notice_failure(store,db,task_id,kind,detail):
    """An optional task diagnostic cannot roll back a durable consumer hold.

    The caller obtains task identity from relational columns, never the rejected
    content. The savepoint isolates unreadable task metadata or notification I/O.
    """
    db.execute('SAVEPOINT learning_failure_notice')
    try:
        task=store._one(db,'taskflow_tasks',task_id)
        if task:store._event(db,task,kind,'task-board',detail)
    except (Conflict,ValueError,OSError,KeyError,TypeError,sqlite3.Error):
        db.execute('ROLLBACK TO learning_failure_notice')
        db.execute('RELEASE learning_failure_notice')
        return False
    db.execute('RELEASE learning_failure_notice')
    return bool(task)
