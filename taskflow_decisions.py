"""Task-bound decision seals and nonrefundable original-task learning budgets.

This host integration currently admits only judgment in an explicitly granted,
already-running cooperative turn. It never starts a provider and never admits a
generated executable. Those capability/profile adapters remain unbound. All state
lives in the existing TaskFlow database and all mutations use its transaction.
"""
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import uuid
from urllib.parse import unquote, urlparse

import taskflow_policy as policy
import taskflow_learning as learning
from workspace import Conflict

SCHEMA = '''
CREATE TABLE IF NOT EXISTS taskflow_decision_config(task_id TEXT PRIMARY KEY,body TEXT NOT NULL,body_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_decision_budgets(task_id TEXT PRIMARY KEY,limits TEXT NOT NULL,started_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_decision_charges(attempt_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_decision_proposals(decision_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,body TEXT NOT NULL,body_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS taskflow_decision_heads(task_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL);
'''


def install_schema(db):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)


def require(ok, reason):
    if not ok:
        raise Conflict(reason)


def task_key(task):
    return {key: task[key] for key in ('projectId', 'taskId', 'taskVersion')}


def request_hash(task):
    return learning.digest({key: task.get(key) for key in
                            ('taskId', 'taskVersion', 'request', 'definitionOfDone',
                             'amendments', 'scopes', 'inputs', 'dependencies', 'ownerId', 'laneId')})


def _ref(value):
    learning.reference(value)
    return Path(unquote(urlparse(value['uri']).path))


def _loaded(row):
    if row is None:
        return None
    body = json.loads(row['body'])
    require(learning.digest(body) == row['body_hash'], 'Decision record integrity changed')
    return body


def config(db, task_id):
    return _loaded(db.execute('SELECT * FROM taskflow_decision_config WHERE task_id=?', (task_id,)).fetchone())


def remaining(db, task_id, own_attempt=None):
    row = db.execute('SELECT * FROM taskflow_decision_budgets WHERE task_id=?', (task_id,)).fetchone()
    require(row is not None, 'Original task learning budget is missing')
    limits = policy.budget(json.loads(row['limits']))
    used = {key: 0 for key in limits}
    for entry in db.execute('SELECT attempt_id,body FROM taskflow_decision_charges WHERE task_id=?', (task_id,)):
        charge = json.loads(entry['body'])
        policy.budget(charge['reserved'])
        # Validation of an already charged attempt includes its own allocation.
        # A new attempt never receives this credit, including after failure.
        if entry['attempt_id'] != own_attempt:
            for key, value in charge['reserved'].items():
                used[key] += value
    require(all(used[k] <= limits[k] for k in limits), 'Learning budget accounting is inconsistent')
    return {k: limits[k] - used[k] for k in limits}, row['started_at'] + limits['maxWallSeconds']


def configure(store, db, task, spec, actor):
    learning.transaction(db)
    require(actor == 'operator-ui', 'Only the authenticated operator configures learning authority')
    require(not store.recovery_hold, 'Restored task copies cannot configure learning')
    require(task['state'] in ('CAPTURED', 'READY', 'QUEUED', 'BLOCKED'), 'Configure only an unassigned live task')
    require(not db.execute("SELECT 1 FROM taskflow_assignments WHERE task_id=? AND state IN ('OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING')", (task['taskId'],)).fetchone(),
            'Existing assignment retains the task')
    keys = {'enabled', 'budget', 'thresholds', 'observation', 'observationSources',
            'outcomeContractRef', 'evaluatorRef', 'judgment', 'outcomeDependencies', 'nodeDependencies'}
    require(isinstance(spec, dict) and set(spec) == keys and type(spec['enabled']) is bool,
            'Exact learning configuration required')
    limits = policy.budget(spec['budget'])
    require(limits['maxWallSeconds'] > 0, 'Finite positive original-task deadline required')
    policy.validate_thresholds(spec['thresholds'])
    require(all(isinstance(spec[k], list) and len(spec[k]) <= 100 for k in ('outcomeDependencies', 'nodeDependencies')),
            'Bounded learning dependencies required')
    learning.current(db, task['projectId'], learning.epoch(db, task['projectId']),
                     spec['outcomeDependencies'], spec['nodeDependencies'])
    for name in ('outcomeContractRef', 'evaluatorRef'):
        _ref(spec[name])
    observation = spec['observation']
    observation_keys = {'artifactRef', 'observedAt', 'validUntil', 'environmentRevision',
                        'featureSchemaVersion', 'missingFields', 'readers', 'purpose', 'retentionPolicyRef'}
    require(isinstance(observation, dict) and set(observation) == observation_keys,
            'Exact minimized observation and retention contract required')
    require(isinstance(observation['missingFields'], list) and len(observation['missingFields']) <= 100
            and all(isinstance(x, str) and 0 < len(x) <= 1000 for x in observation['missingFields']),
            'Bounded missing observation fields required')
    from taskflow import inside
    sources = spec['observationSources']
    require(isinstance(sources, list) and 1 <= len(sources) <= 30, 'Bounded observation source pins required')
    for ref in [observation['artifactRef'], *sources]:
        require(inside(_ref(ref), task['scopes']), 'Observation is outside the original task scope')
    _ref(observation['retentionPolicyRef'])
    require(observation['environmentRevision'] == learning.digest(sources), 'Observation environment binding differs')
    require(policy.timestamp(observation['observedAt']) <= store.clock() < policy.timestamp(observation['validUntil']),
            'A fresh observation is required when configuring the task')
    require(isinstance(observation['readers'], list) and 1 <= len(observation['readers']) <= 30
            and all(isinstance(x, str) and x for x in observation['readers']), 'Explicit observation readers required')
    require(all(isinstance(observation[k], str) and 0 < len(observation[k]) <= 1000
                for k in ('purpose', 'featureSchemaVersion')), 'Bounded observation purpose and feature schema required')
    grant = spec['judgment']
    if grant is not None:
        require(isinstance(grant, dict) and set(grant) ==
                {'workerId', 'threadId', 'turnId', 'mode', 'adapterRef', 'authorityRef', 'charge', 'question'},
                'Exact existing-turn judgment binding required')
        require(grant['mode'] == 'judgment-only', 'Tool-assisted judgment has no verified host adapter yet')
        require(all(isinstance(grant[k], str) and 0 < len(grant[k]) <= 2000
                    for k in ('workerId', 'threadId', 'turnId', 'question')), 'Bounded judgment identity and question required')
        policy.budget(grant['charge'])
        require(grant['charge']['maxJudgmentTurns'] == 1 and grant['charge']['maxModelCalls'] >= 1
                and grant['charge']['maxWallSeconds'] > 0, 'Reserve one judgment turn and a positive bounded allocation')
        require(policy.affordable(grant['charge'], limits), 'Judgment exceeds original-task budget')
        import taskflow_cooperative
        require(_ref(grant['adapterRef']) == Path(taskflow_cooperative.__file__).resolve(),
                'Only the existing observed-current-turn adapter is supported')
        _ref(grant['authorityRef'])
        require(grant['workerId'] in observation['readers'], 'Judgment worker lacks observation access')
    old_budget = db.execute('SELECT limits FROM taskflow_decision_budgets WHERE task_id=?', (task['taskId'],)).fetchone()
    require(old_budget is None or old_budget['limits'] == learning.encode(limits),
            'Original-task budgets are immutable; retries and revisions cannot reset them')
    body = {**copy.deepcopy(spec), 'task': task_key(task), 'requestHash': request_hash(task),
            'approvedBy': actor, 'approvedAt': store.clock()}
    from taskflow_learning_library import bind_configuration
    body['libraryPublication'] = bind_configuration(body,task,store.clock())
    db.execute('INSERT OR IGNORE INTO taskflow_decision_budgets VALUES(?,?,?)',
               (task['taskId'], learning.encode(limits), store.clock()))
    db.execute('INSERT OR REPLACE INTO taskflow_decision_config VALUES(?,?,?)',
               (task['taskId'], learning.encode(body), learning.digest(body)))
    db.execute('DELETE FROM taskflow_decision_heads WHERE task_id=?', (task['taskId'],))
    task['learningRequired'] = True
    return body


def revoke(store, db, task, actor):
    learning.transaction(db)
    require(actor == 'operator-ui' and not store.recovery_hold, 'Explicit original-instance operator revocation required')
    c = config(db, task['taskId'])
    require(c is not None, 'No configured learning authority')
    c.update(enabled=False, revokedAt=store.clock())
    db.execute('UPDATE taskflow_decision_config SET body=?,body_hash=? WHERE task_id=?',
               (learning.encode(c), learning.digest(c), task['taskId']))
    return c


def assessment(store, db, task, c, own_attempt=None):
    left, deadline = remaining(db, task['taskId'], own_attempt)
    # New admissions must fit a full allocation in the remaining task window.
    # An exactly sealed existing attempt already paid that allocation. Its task
    # deadline and admitted-at expiry are checked independently; charging the
    # full wall allocation again here would prematurely strand valid work.
    if own_attempt is None:
        left['maxWallSeconds'] = min(left['maxWallSeconds'], max(0, math.floor(deadline - store.clock())))
    lane = store._one(db, 'taskflow_policies', task['laneId'], 'lane')
    valid = (not store.recovery_hold and c['task'] == task_key(task)
             and c['requestHash'] == request_hash(task) and c['enabled']
             and task['state'] not in ('CANCELLED', 'CANCELLING', 'DONE'))
    authority = bool(lane and lane.get('enabled') and task.get('policyVersion') == lane['version'])
    try:
        state = store._lane(task['projectId'], task['laneId'])
        authority = authority and state.get('enabled') is True
    except Conflict:
        authority = False
    current_observation = True
    try:
        for ref in [c['observation']['artifactRef'], c['observation']['retentionPolicyRef'], *c['observationSources']]:
            _ref(ref)
    except (ValueError, OSError):
        current_observation = False
    grant = c['judgment']
    judgment = None
    if grant is not None:
        w = store._one(db, 'taskflow_workers', grant['workerId'])
        authorized = bool(w and w['enabled'] and w['mode'] == 'cooperative'
                          and w['providerThreadId'] == grant['threadId']
                          and w['projectId'] == task['projectId'] and w['laneId'] == task['laneId']
                          and w.get('purpose', 'work') == 'work')
        try:
            _ref(grant['adapterRef']); _ref(grant['authorityRef'])
        except (ValueError, OSError):
            authorized = False
        from taskflow_cooperative import observe
        observed = observe(store, grant['workerId'], grant['threadId'], grant['turnId']) if authorized else None
        authorized = authorized and bool(observed and observed.get('state') == 'running'
                                         and observed.get('threadId') == grant['threadId']
                                         and observed.get('turnId') == grant['turnId'])
        judgment = {**grant, 'inputAccessVerified': current_observation and grant['workerId'] in c['observation']['readers'],
                    'authorizationVerified': authorized, 'adapterVerified': authorized,
                    'toolBindingRefs': []}
    elif left['maxJudgmentTurns'] and left['maxModelCalls']:
        # No grant is fabricated. This is a visible unresolved request, not a
        # worker assignment, budget reservation, provider call or wakeup.
        charge = {k: 0 for k in policy.BUDGET_KEYS}
        charge.update(maxJudgmentTurns=1, maxModelCalls=1, maxWallSeconds=1)
        judgment = {'mode': 'judgment-only', 'charge': charge, 'question': 'Bind an authorized current judgment turn.',
                    'inputAccessVerified': current_observation, 'authorizationVerified': False, 'adapterVerified': False}
    return {'task': task_key(task), 'taskCurrent': valid, 'taskAuthorityVerified': authority,
            'cancelled': task['state'] in ('CANCELLED', 'CANCELLING'), 'remainingBudget': left,
            'taskDeadline': deadline, 'learningEpoch': learning.epoch(db, task['projectId']),
            'observationCurrent': current_observation, 'observationHash': learning.digest(c['observation']),
            'candidates': [], 'judgment': judgment}


def prepare(store, db, task, actor):
    learning.transaction(db)
    require(actor in ('operator-ui', task.get('ownerId')), 'Only the original owner or operator prepares a decision')
    require(not db.execute("SELECT 1 FROM taskflow_assignments WHERE task_id=? AND state IN ('OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING')", (task['taskId'],)).fetchone(),
            'Do not replace a decision while its assignment retains custody')
    c = config(db, task['taskId'])
    require(c is not None and task.get('learningRequired'), 'Explicit task-bound learning configuration required')
    facts = assessment(store, db, task, c)
    old = db.execute('SELECT decision_id FROM taskflow_decision_heads WHERE task_id=?', (task['taskId'],)).fetchone()
    request = {'schemaVersion': 'ke.agentbrain.decision-request.v2', 'fixture': False, 'executionAuthority': False,
               'decisionId': str(uuid.uuid4()), 'supersedesDecisionId': old[0] if old else None,
               'task': task_key(task), 'goal': task.get('requiredOutcome') or task['definitionOfDone'],
               'observation': c['observation'], 'outcomeContractRef': c['outcomeContractRef'],
               'policyRef': {'uri': 'taskflow://learning-policy/' + task['taskId'], 'sha256': learning.digest(c)},
               'budget': c['budget'], 'learningEpoch': facts['learningEpoch'],
               'sealedAt': datetime.fromtimestamp(store.clock(), timezone.utc).isoformat(),
               'taskState': {'status': 'current' if facts['taskCurrent'] else 'invalid',
                             'evidenceRef': {'uri': 'taskflow://task/' + task['taskId'], 'sha256': learning.digest(task)}},
               'executionProfileRef': None, 'implementationBinding': None}
    result = policy.select(request, facts, c['thresholds'], now=store.clock())
    body = {'request': request, 'result': result, 'configHash': learning.digest(c),
            'lanePolicyHash': learning.digest(store._one(db, 'taskflow_policies', task['laneId'], 'lane'))}
    db.execute('INSERT INTO taskflow_decision_proposals VALUES(?,?,?,?)',
               (request['decisionId'], task['taskId'], learning.encode(body), learning.digest(body)))
    db.execute('INSERT OR REPLACE INTO taskflow_decision_heads VALUES(?,?)', (task['taskId'], request['decisionId']))
    return copy.deepcopy(body)


def check(store, db, task, worker, assignment=None):
    if not task.get('learningRequired'):
        return None
    learning.transaction(db)
    c = config(db, task['taskId'])
    require(c is not None, 'Required learning configuration is missing')
    row = db.execute('SELECT p.* FROM taskflow_decision_heads h JOIN taskflow_decision_proposals p ON p.decision_id=h.decision_id WHERE h.task_id=?', (task['taskId'],)).fetchone()
    selected = _loaded(row)
    require(selected is not None, 'Prepare the exact task decision before dispatch')
    require(selected['configHash'] == learning.digest(c), 'Learning configuration changed; prepare a new decision')
    lane = store._one(db, 'taskflow_policies', task['laneId'], 'lane')
    require(selected['lanePolicyHash'] == learning.digest(lane), 'Dispatch policy changed after the decision')
    grant = c['judgment']
    require(grant and worker['workerId'] == grant['workerId'] and worker['mode'] == 'cooperative',
            'Only the exact granted cooperative judgment worker may receive this decision')
    require(task.get('pidTask') is None, 'Judgment cannot execute a mechanical contract through another label')
    request = selected['request']
    learning.current(db, task['projectId'], request['learningEpoch'], c['outcomeDependencies'], c['nodeDependencies'])
    if assignment:
        seal = _loaded(db.execute('SELECT * FROM taskflow_learning_decisions WHERE decision_id=?', (request['decisionId'],)).fetchone())
        charge = db.execute('SELECT body FROM taskflow_decision_charges WHERE attempt_id=?', (assignment['id'],)).fetchone()
        require(seal and seal['state'] == 'ADMITTED' and seal['attemptId'] == assignment['id'] and seal['taskId'] == task['taskId']
                and seal['taskVersion'] == task['taskVersion'] and seal['workerId'] == worker['workerId']
                and seal['decisionHash'] == learning.digest(selected) and seal['grant'] == grant and charge is not None,
                'Exact pre-action seal and original charge are required')
        require(json.loads(charge[0]) == {'decisionId': request['decisionId'], 'reserved': grant['charge']},
                'Decision charge changed')
        require(store.clock() < seal['admittedAt'] + grant['charge']['maxWallSeconds'],
                'This judgment allocation has expired; its original charge remains consumed')
    facts = assessment(store, db, task, c, assignment['id'] if assignment else None)
    result = policy.revalidate(request, selected['result'], facts, c['thresholds'], now=store.clock())
    require(result['disposition'] == 'NEEDS_JUDGMENT' and result['judgmentMode'] == 'judgment-only',
            'Executable and tool-assisted adapters remain unbound')
    for field in ('outcomeContractRef', 'evaluatorRef'):
        _ref(c[field])
    return selected, c


def admit(store, db, task, worker, assignment):
    checked = check(store, db, task, worker)
    if checked is None:
        return
    selected, c = checked
    request = selected['request']
    seal = {'decisionId': request['decisionId'], 'taskId': task['taskId'], 'taskVersion': task['taskVersion'],
            'projectId': task['projectId'], 'attemptId': assignment['id'], 'state': 'ADMITTED',
            'workerId': worker['workerId'], 'learningEpoch': request['learningEpoch'],
            'outcomeContractRef': c['outcomeContractRef'], 'evaluatorRef': c['evaluatorRef'],
            'outcomeDependencies': c['outcomeDependencies'], 'nodeDependencies': c['nodeDependencies'],
            'decisionHash': learning.digest(selected), 'grant': c['judgment'], 'admittedAt': store.clock()}
    charge = {'decisionId': request['decisionId'], 'reserved': c['judgment']['charge']}
    db.execute('INSERT INTO taskflow_decision_charges VALUES(?,?,?)', (assignment['id'], task['taskId'], learning.encode(charge)))
    db.execute('INSERT INTO taskflow_learning_decisions VALUES(?,?,?,?,?,?)',
               (seal['decisionId'], task['taskId'], assignment['id'], task['projectId'], learning.encode(seal), learning.digest(seal)))
    assignment['learningDecisionId'] = request['decisionId']


def validate_boundary(store, db, task, worker, assignment, operation, values):
    checked = check(store, db, task, worker, assignment)
    if checked is None:
        return
    _, c = checked
    grant = c['judgment']
    if operation == 'start':
        require(values.get('threadId') == grant['threadId'] and values.get('turnId') == grant['turnId'],
                'Start must bind the exact already-authorized judgment turn')
    elif operation == 'return':
        require(assignment.get('providerThreadId') == grant['threadId']
                and assignment.get('providerTurnId') == grant['turnId'], 'Returned judgment turn differs')


def validate_outputs(db, task, assignment, files):
    if not task.get('learningRequired'):
        return
    row = db.execute('SELECT body FROM taskflow_decision_charges WHERE attempt_id=?', (assignment['id'],)).fetchone()
    require(row is not None, 'Original judgment output allocation required')
    reserved = json.loads(row[0])['reserved']
    require(sum(f['bytes'] for f in files) <= reserved['maxOutputBytes'], 'Judgment result exceeds its reserved output budget')


def validate_learning_acceptance(db, task, seal, store):
    """Current permission and evidence, without requiring an old turn to stay live.

    Its historical start/return already passed the task boundary. The independent
    reviewer must still not accept new learning after a committed revocation.
    """
    if not task.get('learningRequired'):
        return
    learning.transaction(db)
    require(store is not None and not store.recovery_hold,
            'Original authoritative host required for learning acceptance')
    require(store._lane(task['projectId'], task['laneId']).get('enabled') is True,
            'Workflow permission was revoked before learning acceptance')
    c = config(db, task['taskId'])
    require(c is not None and c['enabled'] and c['task'] == task_key(task)
            and c['requestHash'] == request_hash(task), 'Learning authority was revoked or changed before acceptance')
    selected = _loaded(db.execute('SELECT * FROM taskflow_decision_proposals WHERE decision_id=?',
                                 (seal['decisionId'],)).fetchone())
    require(selected and selected['configHash'] == learning.digest(c)
            and seal['decisionHash'] == learning.digest(selected), 'Pre-action decision changed before acceptance')
    lane = json.loads(db.execute('SELECT body FROM taskflow_policies WHERE lane=?', (task['laneId'],)).fetchone()[0])
    require(lane.get('enabled') is True and selected['lanePolicyHash'] == learning.digest(lane),
            'Original lane permission changed before learning acceptance')
    for ref in [c['observation']['artifactRef'], c['observation']['retentionPolicyRef'],
                *c['observationSources'], c['judgment']['authorityRef'], c['judgment']['adapterRef']]:
        _ref(ref)
