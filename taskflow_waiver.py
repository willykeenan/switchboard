"""Explicit founder audit waiver, separate from review and functional delivery.

No enrollment, allowance, provider launch, installation or manufactured PASS.
The existing owner binds a real user command to one task's current requirements.
Returned outputs and installed behavior still need exact functional evidence.
"""
import json
import re
import uuid
from pathlib import Path
from workspace import Conflict

TRANSCRIPT_ROOT = Path.home()/'.codex/sessions'
DISPOSITION = 'WAIVED_BY_USER'


def read_receipt(path, task_id):
    from taskflow import pin_files
    from taskflow_delivery import files_current
    receipt_pin = pin_files([path])[0]
    value = json.loads(Path(receipt_pin['path']).read_text())
    if (value.get('auditRequired') is not False or value.get('auditPassed') is not False
            or value.get('auditDisposition') != DISPOSITION):
        raise Conflict('An explicit user audit waiver is required; never substitute PASS')
    source = value.get('source', {})
    reference = source.get('reference', '')
    log_name, sep, message_id = reference.partition('#')
    log = Path(log_name).resolve()
    if (source.get('role') != 'user' or source.get('provider') != 'codex'
            or not sep or message_id != source.get('messageId')
            or not log.is_relative_to(TRANSCRIPT_ROOT.resolve())):
        raise Conflict('Bind the original user message in its local Codex transcript')
    command = source.get('text', '').strip()
    if not re.fullmatch(r'(?:/approved(?:\s+[^\n]+)?|\[\$approved\]\([^\n]+/approved/SKILL\.md\))', command):
        raise Conflict('A quoted mention or skill attachment is not an approval command')
    session_id = None
    found = False
    with log.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            item = row.get('payload', {})
            if row.get('type') == 'session_meta':
                session_id = item.get('id')
            if row.get('type') != 'response_item' or item.get('id') != message_id:
                continue
            body = ''.join(x.get('text', '') for x in item.get('content', []) if isinstance(x, dict))
            found = item.get('role') == 'user' and body == source.get('text')
            if source.get('timestamp') and row.get('timestamp') != source['timestamp']:
                found = False
            break
    if not found or session_id != source.get('sessionId'):
        raise Conflict('Original user approval identity or text does not match')
    scope = value.get('scope', {})
    artifacts = scope.get('artifacts', [])
    if not isinstance(artifacts, list):
        raise Conflict('Approval artifacts must be explicit pins')
    files_current(artifacts)
    bound = scope.get('taskId') == task_id
    # A founder-approved finite renewal can name an existing task in its pinned
    # proposal while its receipt remains owned by the controller conversation.
    for artifact in artifacts:
        if Path(artifact['path']).suffix != '.json':
            continue
        try:
            proposal = json.loads(Path(artifact['path']).read_text())
        except (ValueError, OSError):
            continue
        declared = proposal.get('executionRenewal', {}).get('scope', '')
        bound = bound or task_id in re.findall(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', declared)
    if not bound:
        raise Conflict('The approved scope does not name this exact original task')
    return value, [receipt_pin, *artifacts]


def requirements(task):
    from taskflow_delivery import pins
    p = pins(task)
    return {k:p[k] for k in ('taskId', 'taskVersion', 'requirementsHash', 'criteriaHash')}


def coherent_completion(task):
    """Waived completion requires functional proof, never an invented reviewer."""
    from taskflow_delivery import pins
    a = task.get('approval') or {}
    c = task.get('completion') or {}
    w = task.get('auditWaiver') or {}
    if (w.get('binding') != requirements(task) or w.get('auditDisposition') != DISPOSITION
            or w.get('auditPassed') is not False or not w.get('evidence')
            or a.get('waiver') != w or a.get('auditDisposition') != DISPOSITION
            or a.get('auditPassed') is not False or a.get('approvedBy') != task.get('ownerId')
            or c.get('verifiedBy') != task.get('ownerId') or c.get('auditDisposition') != DISPOSITION):
        return False
    wanted = {x['id'] for x in task['delivery']['criteria']}
    for block, full in [(a.get('functionalChecks') or {}, False), (c.get('functionalChecks') or {}, True)]:
        rows = block.get('checks', [])
        ids = {r.get('id') for r in rows}
        if (block.get('pins') != pins(task) or not block.get('evidence') or not rows
                or len(rows) != len(ids) or not ids <= wanted or full and ids != wanted
                or any(r.get('passed') is not True for r in rows)):
            return False
    if task['delivery']['kind'] == 'artifact':
        return c['functionalChecks'] == a['functionalChecks']
    d = task.get('deliveryResult') or {}
    return bool(d.get('attemptId') and d.get('receipt')) and c.get('deliveryResult') == d


def delivery_binding(store, db, task, sessions, events):
    """Require the original cooperative installer's observed return, not a run charge."""
    from taskflow import digest
    from taskflow_delivery import files_current
    result = task.get('deliveryResult') or {}
    attempt = store._one(db, 'taskflow_delivery_attempts', result.get('attemptId'))
    if not attempt or attempt.get('managedAssignmentId'):
        return None
    actor = result.get('actor')
    if (actor == task.get('ownerId') or actor != task['delivery']['actorId']
            or attempt.get('actor') != actor or attempt.get('taskId') != task['taskId']
            or attempt.get('state') != 'RETURNED' or attempt.get('result') != result.get('observation')
            or attempt.get('payloadHash') != digest(attempt.get('payload'))
            or attempt['payload'].get('approval') != task['approval']):
        return None
    session = sessions.get(actor, {})
    if session.get('provider') != 'codex' or session.get('endpoint') != attempt.get('threadId'):
        return None
    for proof in (attempt.get('executionEvidence') or {}, attempt.get('sourceRelease') or {}):
        if (proof.get('threadId') != attempt.get('threadId') or not proof.get('turnId')
                or proof.get('turnId') != attempt.get('turnId') or proof.get('state') not in ('running','terminal')):
            return None
    if store.delivery.eligible(db,task,actor,attempt):
        return None
    if not any(e.get('kind') == 'delivery-return' and e.get('actor') == actor
            and (e.get('detail') or {}).get('attemptId') == attempt['id'] for e in events):
        return None
    files_current([result['receipt'], *result['evidence']])
    observation = store.delivery.verify_receipt(task, json.loads(Path(result['receipt']['path']).read_text()))
    if task['delivery']['kind'] == 'local-app':
        store.delivery.verify_release(task, observation)
    return {'phase':'delivery', 'attemptId':attempt['id'], 'workerId':actor,
            'threadId':attempt['threadId'], 'turnId':attempt['turnId'],
            'payloadHash':attempt['payloadHash'], 'mode':'cooperative'}


def valid(task):
    from taskflow_delivery import files_current
    waiver = task.get('auditWaiver')
    if not waiver or waiver.get('binding') != requirements(task):
        raise Conflict('Audit waiver does not match current task requirements')
    files_current(waiver['evidence'])
    if waiver.get('auditDisposition') != DISPOSITION or waiver.get('auditPassed') is not False:
        raise Conflict('Invalid waiver disposition')
    return waiver


def checks(task, values, complete=False):
    from taskflow import pin_files, text
    from taskflow_delivery import pins
    if values.get('pins') != pins(task):
        raise Conflict('Functional checks must bind the exact current result and delivery')
    evidence = pin_files(values.get('evidence', []))
    if not evidence:
        raise Conflict('Functional evidence is required despite the audit waiver')
    expected = {c['id'] for c in task['delivery']['criteria']}
    rows = values.get('checks', [])
    ids = {r.get('id') for r in rows if isinstance(r, dict)} if isinstance(rows, list) else set()
    if (not isinstance(rows, list) or not rows or len(ids) != len(rows)
            or not ids <= expected or complete and ids != expected
            or any(not isinstance(r, dict) or r.get('passed') is not True for r in rows)):
        raise Conflict('Every exact acceptance criterion needs a passing functional result')
    return {'summary':text(values.get('summary'), 'functional results'),
            'checks':rows, 'evidence':evidence, 'pins':pins(task)}


def mutate(store, operation, values, actor):
    from taskflow import ACTIVE, STAGES
    from taskflow_delivery import files_current, pins, contract
    receipt = None
    if operation == 'waiver-record':
        receipt = read_receipt(values['receipt'], values['taskId'])
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        task = store._one(db, 'taskflow_tasks', values['taskId'])
        if not task or task['version'] != values['version']:
            raise Conflict('Task changed; refresh before recording an approval decision')
        if actor not in ('operator-ui', task.get('ownerId')):
            raise Conflict('Only this exact task owner or operator may reconcile user approval')
        if store.audit.current(db, task['taskId']):
            raise Conflict('An existing audit retains its claim; reconcile at its safe boundary')
        source = task['station']
        if operation == 'waiver-record':
            if task['state'] in (*ACTIVE, *STAGES, 'DONE', 'CANCELLED'):
                raise Conflict('Preserve active execution, delivery and terminal tasks')
            # Ready and its waiver must become visible together. Calling ready
            # first can offer a worker before the second command records waiver.
            if 'ready' in values:
                if task.get('auditWaiver'):
                    raise Conflict('Readiness already bound; do not replace an existing waiver')
                store._ready_in_db(db, task, task['taskId'], task['version'], values['ready'], actor)
            document, evidence = receipt
            if task.get('auditWaiver'):
                old = valid(task)
                if old['evidence'] == evidence:
                    return task
                raise Conflict('Retain the existing waiver; do not silently replace approval')
            task['auditWaiver'] = {'auditRequired':False, 'auditDisposition':DISPOSITION,
                'auditPassed':False, 'source':document['source'], 'approvedScope':document['scope'],
                'binding':requirements(task), 'evidence':evidence, 'recordedBy':actor, 'at':store.clock()}
            task.update(auditRequired=False, auditDisposition=DISPOSITION, auditPassed=False)
        elif operation in ('waiver-accept', 'waiver-finish'):
            waiver = valid(task)
            expected_state = 'REVIEW' if operation == 'waiver-accept' else 'VERIFY'
            if task['state'] != expected_state:
                raise Conflict('The original task has not returned the required real work')
            evidence = checks(task, values, operation == 'waiver-finish' or task['delivery']['kind'] == 'artifact')
            files_current([*task.get('inputs', []), *task['result']['artifacts']])
            if operation == 'waiver-accept':
                past = store._past(db, task['taskId'])
                if not past or past[-1]['state'] != 'RETURNED' or past[-1]['taskVersion'] != task['taskVersion']:
                    raise Conflict('A current real returned assignment is required')
                delivery = contract(task.get('delivery'))
                if delivery['kind'] == 'local-app' and not re.fullmatch(r'[0-9a-f]{40}', task['result'].get('sourceCommit', '')):
                    raise Conflict('Return the exact app source commit before approval')
                task['approval'] = {'id':str(uuid.uuid4()), 'pins':pins(task),
                    'auditDisposition':DISPOSITION, 'auditPassed':False, 'waiver':waiver,
                    'functionalChecks':evidence, 'approvedBy':actor, 'at':store.clock()}
                if delivery['kind'] == 'artifact':
                    task.update(state='DONE', station='completed:'+task['laneId'], waitReason='',
                        completion={'kind':'verified-task-artifact', 'approvalId':task['approval']['id'],
                            'pins':pins(task), 'auditDisposition':DISPOSITION, 'verifiedBy':actor,
                            'functionalChecks':evidence, 'at':store.clock()})
                else:
                    task.update(state='DELIVERY_QUEUED', station='board:'+task['laneId'],
                        waitReason='User-approved result awaits its designated installer; audit waived')
            else:
                result = task.get('deliveryResult')
                if not result:
                    raise Conflict('An actual returned installation receipt is required')
                files_current([result['receipt'], *result['evidence']])
                observed = store.delivery.verify_receipt(task, json.loads(Path(result['receipt']['path']).read_text()))
                if task['delivery']['kind'] == 'local-app':
                    if observed.get('releaseReceipt') != result['observation'].get('releaseReceipt'):
                        raise Conflict('Pinned installed release proof changed')
                    store.delivery.verify_release(task, observed)
                task.update(state='DONE', station='completed:'+task['laneId'], waitReason='',
                    completion={'kind':'verified-delivery', 'approvalId':task['approval']['id'],
                        'pins':pins(task), 'auditDisposition':DISPOSITION, 'verifiedBy':actor,
                        'functionalChecks':evidence, 'deliveryResult':result, 'at':store.clock()})
        else:
            raise ValueError('Unknown waiver operation')
        if task['state'] == 'DONE':
            task.pop('courierId', None)
        store._put(db, task)
        store._event(db, task, operation, actor,
            {'auditDisposition':DISPOSITION, 'approvalId':(task.get('approval') or {}).get('id'),
             'evidence':task['auditWaiver']['evidence']}, source=source, destination=task['station'])
        return task
