"""Structured, non-executable judgment returns under the original TaskFlow attempt.

Worker text is an untrusted proposal. Acceptance of the task supplies outcome
provenance, not qualification, promotion, a new permission, or measured usage.
"""
import copy
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

import taskflow_learning as learning
import taskflow_policy as decisions
from workspace import Conflict

KINDS = {'composition','procedure','detector','predictor','counterexample','no-reliable-lesson'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS taskflow_judgment_returns(
 attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, decision_id TEXT UNIQUE NOT NULL,
 body TEXT NOT NULL, body_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_judgment_lessons(
 lesson_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 outcome_id TEXT NOT NULL, outcome_revision INTEGER NOT NULL, outcome_hash TEXT NOT NULL,
 body TEXT NOT NULL, body_hash TEXT NOT NULL);
'''


def install_schema(db):
    for statement in SCHEMA.split(';'):
        if statement.strip():db.execute(statement)


def require(ok, reason):
    if not ok:raise Conflict(reason)


def bounded(value, name, limit=4000):
    require(isinstance(value,str) and 0<len(value)<=limit, 'Bounded '+name+' required')


def proposal_id(value):
    """Provisional identity. Accepted lesson also binds the later outcome revision."""
    proposal={k:v for k,v in value['proposal'].items() if k!='lessonId'}
    return 'proposal:'+learning.digest({'decisionId':value['decisionId'],'attemptId':value['attemptId'],
        'task':value['task'],'proposal':proposal})


def artifact(ref, manifest):
    learning.reference(ref)
    path=str(Path(unquote(urlparse(ref['uri']).path)))
    require(any(f['path']==path and f['sha256']==ref['sha256'] for f in manifest),
            'Judgment evidence must be an exact returned artifact')


def attach(db, task, assignment, value, manifest):
    """Invoked inside ordinary return, before result and assignment commit."""
    if not task.get('learningRequired'):
        require(value is None, 'Judgment needs original task-bound learning authority')
        return None
    learning.transaction(db)
    keys={'schemaVersion','fixture','executionAuthority','decisionId','attemptId','task',
          'authorizedTurnRef','attemptSummary','resultEvidence','outcomeRef',
          'remainingUncertainty','proposal','usedBudget'}
    require(isinstance(value,dict) and set(value)==keys, 'Structured judgment return required')
    require(value['schemaVersion']=='ke.agentbrain.judgment-return.v2' and value['fixture'] is False
            and value['executionAuthority'] is False, 'Non-fixture, non-authorizing judgment required')
    require(value['task']=={k:task[k] for k in ('projectId','taskId','taskVersion')}
            and value['attemptId']==assignment['id']
            and value['decisionId']==assignment.get('learningDecisionId'), 'Exact judgment task/decision/attempt required')
    require(assignment['state']=='RUNNING' and assignment.get('mode')=='cooperative',
            'Judgment must come from the observed running assignment')
    row=db.execute('SELECT * FROM taskflow_learning_decisions WHERE decision_id=?',(value['decisionId'],)).fetchone()
    require(row is not None, 'Pre-action decision seal required')
    seal=json.loads(row['body']);require(learning.digest(seal)==row['body_hash'], 'Decision seal changed')
    grant=seal['grant']
    require(seal['attemptId']==assignment['id'] and grant['workerId']==assignment['workerId']
            and assignment['providerThreadId']==grant['threadId'] and assignment['providerTurnId']==grant['turnId']
            and value['authorizedTurnRef']==grant['authorityRef'], 'Returned authority or observed turn differs')
    learning.reference(value['authorizedTurnRef'])
    require(value['outcomeRef'] is None, 'Worker cannot supply independent outcome acceptance')
    bounded(value['attemptSummary'],'attempt summary')
    uncertain=value['remainingUncertainty']
    require(isinstance(uncertain,list) and len(uncertain)<=30, 'Bounded uncertainty list required')
    for item in uncertain:bounded(item,'uncertainty',1000)
    evidence=value['resultEvidence']
    require(isinstance(evidence,list) and 1<=len(evidence)<=30
            and len({learning.digest(r) for r in evidence})==len(evidence), 'Unique bounded result evidence required')
    for ref in evidence:artifact(ref,manifest)
    proposal=value['proposal']
    require(isinstance(proposal,dict) and set(proposal)=={'lessonId','kind','summary','proposalRef'}
            and proposal['kind'] in KINDS, 'Explicit learning proposal or no-reliable-lesson required')
    bounded(proposal['summary'],'proposal summary')
    artifact(proposal['proposalRef'],manifest)
    require(proposal['lessonId']==proposal_id(value), 'Content-addressed proposal identity differs')
    used=decisions.budget(value['usedBudget']);reserved=grant['charge']
    require(decisions.affordable(used,reserved), 'Declared usage exceeds the original reserved allocation')
    require(used['maxJudgmentTurns']==1 and used['maxModelCalls']>=1,
            'Judgment cannot self-report zero model calls or turns')
    measured_bytes=sum(f['bytes'] for f in manifest)+len(learning.encode(value).encode())
    require(measured_bytes<=reserved['maxOutputBytes'],
            'Judgment artifacts and structured return exceed reserved output bytes')
    # Evidence/turn identity is checked by the host. Usage fields remain declared
    # by the worker and are never substituted for physical/provider metering.
    body={'schemaVersion':'ke.taskflow.judgment-record.v1','return':copy.deepcopy(value),
          'turn':{'workerId':assignment['workerId'],'threadId':assignment['providerThreadId'],
                  'turnId':assignment['providerTurnId'],'source':assignment.get('executionEvidence')},
          'usageBasis':'worker-declared-unverified','measuredOutputBytes':measured_bytes,
          'executionAuthority':False}
    sha=learning.digest(body)
    db.execute('INSERT INTO taskflow_judgment_returns VALUES(?,?,?,?,?)',
               (assignment['id'],task['taskId'],value['decisionId'],learning.encode(body),sha))
    return {'attemptId':assignment['id'],'decisionId':value['decisionId'],
            'proposalId':proposal['lessonId'],'recordHash':sha,'executionAuthority':False}


def project(db, outcome):
    """Derive one immutable proposal/outcome link; never qualify a capability."""
    learning.transaction(db)
    row=db.execute('SELECT * FROM taskflow_judgment_returns WHERE attempt_id=?',(outcome['attemptId'],)).fetchone()
    if row is None:return None
    record=json.loads(row['body']);require(learning.digest(record)==row['body_hash'], 'Judgment record changed')
    value=record['return'];task_id=outcome['task']['taskId']
    require(row['task_id']==task_id and row['decision_id']==outcome['decisionId']
            and value['task']==outcome['task'], 'Outcome and judgment provenance differ')
    assignment=json.loads(db.execute('SELECT body FROM taskflow_assignments WHERE id=?',(outcome['attemptId'],)).fetchone()[0])
    require(assignment.get('result',{}).get('judgmentReturn',{}).get('recordHash')==row['body_hash'],
            'Accepted result did not bind this judgment')
    if outcome['status'] not in ('ACCEPTED','CORRECTED'):return None
    learning.reference(value['proposal']['proposalRef'])
    ref=learning.outcome_ref(outcome)
    key='lesson:'+learning.digest({'judgmentHash':row['body_hash'],'outcome':ref})
    body={'schemaVersion':'ke.taskflow.outcome-bound-proposal.v1','lessonId':key,
          'task':outcome['task'],'decisionId':outcome['decisionId'],'attemptId':outcome['attemptId'],
          'proposal':value['proposal'],'judgmentHash':row['body_hash'],'outcomeRef':ref,
          'taskSuccess':outcome['taskSuccess'],'procedureDisposition':outcome['procedureDisposition'],
          'artifactDisposition':outcome['artifactDisposition'],'remainingUncertainty':value['remainingUncertainty'],
          'state':'NO_RELIABLE_LESSON' if value['proposal']['kind']=='no-reliable-lesson' else 'PROPOSED',
          'claimLimit':'Provenance from independent task acceptance only; proposal is unqualified and untrusted.',
          'executionAuthority':False}
    encoded=learning.encode(body);sha=learning.digest(body)
    prior=db.execute('SELECT body,body_hash FROM taskflow_judgment_lessons WHERE lesson_id=?',(key,)).fetchone()
    require(prior is None or tuple(prior)==(encoded,sha), 'Immutable outcome-bound lesson changed')
    if prior is None:
        learning.add_dependency_node(db,outcome['task']['projectId'],key,outcome_refs=[ref],
                                     expected_epoch=learning.epoch(db,outcome['task']['projectId']))
    db.execute('INSERT OR IGNORE INTO taskflow_judgment_lessons VALUES(?,?,?,?,?,?,?,?)',
               (key,outcome['task']['projectId'],outcome['attemptId'],outcome['outcomeId'],outcome['revision'],ref['sha256'],encoded,sha))
    return key


def effective(db, project_id):
    """Current, project-scoped proposals only; absence of consumer progress is safe."""
    rows=db.execute('''SELECT l.body,l.body_hash FROM taskflow_judgment_lessons l
        JOIN taskflow_learning_heads h ON h.outcome_id=l.outcome_id
         AND h.revision=l.outcome_revision AND h.body_hash=l.outcome_hash
        JOIN taskflow_learning_nodes n ON n.project_id=l.project_id AND n.node_id=l.lesson_id
        WHERE l.project_id=? AND n.state='CURRENT' ORDER BY l.lesson_id''',(project_id,)).fetchall()
    result=[]
    for row in rows:
        body=json.loads(row['body']);require(learning.digest(body)==row['body_hash'], 'Lesson projection changed')
        learning.reference(body['proposal']['proposalRef'])
        result.append(body)
    return result
