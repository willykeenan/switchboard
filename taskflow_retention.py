"""Current-authority retention of one coherently approved managed completion.

Uses existing TaskFlow records and Library idempotency, never dispatch or replay.
Historical/unbound DONE tasks remain untouched. Cross-store publication is not
atomic: authority is rechecked before Library and before either task mutation.
"""
import json, sqlite3
from workspace import Conflict
from taskflow_delivery import files_current, pins


def binding(task):
    """Exclude retention bookkeeping only; every other task byte is authoritative."""
    return {k:v for k,v in task.items()
            if k not in ('version','updatedAt','libraryRetention','libraryRetentionError','heldByWorker')}


def coherent(task):
    """Pure logical completion/approval/result binding; files are checked separately."""
    if task.get('legacy') or task.get('state')!='DONE':return False
    c=task.get('completion') or {};a=task.get('approval') or {};p=pins(task)
    kind='verified-task-artifact' if (task.get('delivery') or {}).get('kind')=='artifact' else 'verified-delivery'
    if c.get('kind')!=kind or not a.get('id') or c.get('approvalId')!=a['id'] or c.get('pins')!=p or a.get('pins')!=p:return False
    if a.get('auditDisposition')=='WAIVED_BY_USER':
        from taskflow_waiver import coherent_completion
        return coherent_completion(task)
    review=task.get('review') or {};approved=a.get('review') or {}
    for r in (review,approved):
        if (r.get('verdict')!='accept' or r.get('taskVersion')!=task['taskVersion']
                or r.get('resultHash')!=p['resultHash'] or r.get('assignmentId')!=task.get('assignmentId')
                or not r.get('reviewer') or not r.get('evidence')):return False
    if c.get('reviewer')!=review['reviewer'] or approved['reviewer'] in (task.get('ownerId'),task.get('workerId')):return False
    if kind=='verified-task-artifact':return review==approved and c['reviewer']==approved['reviewer']
    result=task.get('deliveryResult') or {}
    return (c.get('verification')==review and c.get('deliveryResult')==result
            and bool(result.get('attemptId')) and bool(result.get('receipt'))
            and result.get('actor') not in (task.get('ownerId'),task.get('workerId'),approved['reviewer'],c['reviewer'])
            and c['reviewer'] not in (task.get('ownerId'),task.get('workerId'),approved['reviewer']))


def phase_bindings(store,db,task,limits,sessions,*,work_only=False):
    """Bind retained result to exact original returned assignments, reservations and events."""
    from taskflow import digest
    assignments=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_assignments WHERE task_id=?',(task['taskId'],))]
    events=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_events WHERE task_id=?',(task['taskId'],))]
    events=[e for e in events if e.get('taskVersion')==task['taskVersion']]
    waived=task['approval'].get('auditDisposition')=='WAIVED_BY_USER'
    wanted={'work':task['workerId']} if waived or work_only else {'work':task['workerId'],'audit':task['approval']['review']['reviewer']}
    if not waived and not work_only and task['completion']['kind']=='verified-delivery':
        wanted.update(verify=task['completion']['reviewer'],delivery=task['deliveryResult']['actor'])
    used=set();proofs=[]
    for phase,actor in wanted.items():
        matches=[]
        for a in assignments:
            if (a.get('state')!='RETURNED' or a.get('mode')!='managed' or a.get('cancelRequested')
                    or a.get('taskVersion')!=task['taskVersion'] or a.get('phase','work')!=phase
                    or a.get('workerId')!=actor):continue
            thread,turn=a.get('providerThreadId'),a.get('providerTurnId')
            is_pid=a.get('executionTransport')=='local-pid'
            pid_terminal=None;session=sessions.get(actor,{})
            if is_pid:
                continue
            elif not isinstance(thread,str) or not thread or not isinstance(turn,str) or not turn:continue
            if session.get('provider')!='codex' or not session.get('managed') or (not is_pid and session.get('endpoint')!=thread):continue
            started=a.get('executionEvidence') or {}
            if is_pid:
                if started.get('runnerPid')!=pid_terminal['runnerPid'] or started.get('contractHash')!=pid_terminal['contractHash']:continue
            elif started.get('threadId')!=thread or started.get('turnId')!=turn:continue
            if a.get('payloadHash')!=digest(a.get('payload')):continue
            row=db.execute('SELECT body FROM execution_reservations WHERE id=?',(a['id'],)).fetchone()
            if not row:continue
            r=json.loads(row[0])
            if (r.get('state')!='TERMINAL' or r.get('taskId')!=task['taskId'] or r.get('policyId')!=limits['id']
                    or r.get('parentId')!=task['ownerId'] or r.get('laneId')!=task['laneId'] or r.get('phase')!=phase
                    or r.get('workerId')!=actor or actor not in limits['workerIds']
                    or r.get('model')!=a.get('model') or r.get('effort')!=a.get('effort')
                    or r['model'] not in limits['models'] or r['effort'] not in limits['efforts']):continue
            if phase=='work':
                if a['id']!=task['assignmentId'] or a.get('result')!=task['result'] or task['result'].get('payloadHash')!=a['payloadHash']:continue
                # Bind successful work observation into the result hash reviewed
                # by Audit. Historical unproven returns stay untouched.
                terminal=task['result'].get('terminalEvidence') or {}
                if is_pid:
                    if terminal!=pid_terminal:continue
                    running_detail={'executionTransport':'local-pid','pid':started['runnerPid'],'modelCalls':0}
                else:
                    if terminal.get('state')!='terminal' or terminal.get('status')!='completed' or terminal.get('threadId')!=thread or terminal.get('turnId')!=turn:continue
                    running_detail={'threadId':thread,'turnId':turn,'evidence':started}
                if not any(e.get('kind')=='running' and e.get('assignmentId')==a['id'] and e.get('actor')==actor
                           and e.get('detail')==running_detail for e in events):continue
                if not any(e.get('kind')=='returned' and e.get('assignmentId')==a['id'] and e.get('actor')==actor and e.get('detail')==task['result'] for e in events):continue
            else:
                proof_events=[e for e in events if e.get('kind') in ('managed-phase-returned','managed-phase-recovered')
                              and (e.get('detail') or {}).get('assignmentId')==a['id']]
                good=[]
                for e in proof_events:
                    detail=e['detail'];p=detail.get('terminal') or detail.get('evidence') or {}
                    if (p.get('state')=='terminal' and p.get('status')=='completed'
                            and p.get('threadId')==thread and p.get('turnId')==turn
                            and (e['kind']=='managed-phase-recovered' and detail.get('committedResultPreserved')
                                 or detail.get('phaseId')==a.get('phaseId') and detail.get('phase')==phase)):good.append(e)
                if not good:continue
                table='taskflow_delivery_attempts' if phase=='delivery' else 'taskflow_audit_claims'
                row=db.execute('SELECT body FROM '+table+' WHERE id=?',(a.get('phaseId'),)).fetchone()
                if not row:continue
                phase_record=json.loads(row[0])
                if phase_record.get('managedAssignmentId')!=a['id'] or phase_record.get('taskId')!=task['taskId']:continue
                phase_start=phase_record.get('executionEvidence') or {}
                if phase_start.get('threadId')!=thread or phase_start.get('turnId')!=turn:continue
                if phase=='delivery':
                    d=task['deliveryResult']
                    if (phase_record.get('state')!='RETURNED' or a['phaseId']!=d['attemptId'] or phase_record.get('actor')!=actor
                            or phase_record.get('threadId')!=thread or phase_record.get('turnId')!=turn
                            or phase_record.get('result')!=d.get('observation')):continue
                    if a['payload'].get('approval')!=task['approval']:continue
                else:
                    review=task['approval']['review'] if phase=='audit' else task['review']
                    if (phase_record.get('state')!='FINISHED' or phase_record.get('verdict')!='accept'
                            or phase_record.get('reviewer')!=actor or phase_record.get('resultHash')!=pins(task)['resultHash']
                            or phase_record.get('taskVersion')!=task['taskVersion']):continue
                    if not any(e.get('kind')=='review-accept' and e.get('actor')==actor and e.get('detail')==review for e in events):continue
                    if a.get('result') and a['result'].get('artifacts')!=review['evidence']:continue
            matches.append(a)
        if len(matches)!=1:return None
        a=matches[0];pair=(a['providerThreadId'],a['providerTurnId'])
        if pair in used:return None
        used.add(pair);proofs.append({'phase':phase,'assignmentId':a['id'],'phaseId':a.get('phaseId'),
                                    'workerId':actor,'threadId':pair[0],'turnId':pair[1],'payloadHash':a['payloadHash']})
        if a.get('executionTransport')=='local-pid':proofs[-1].update(executionTransport='local-pid',runnerPid=a['executionEvidence']['runnerPid'])
    if waived:
        from taskflow_waiver import valid, delivery_binding
        waiver=valid(task)
        kinds=['waiver-accept']+(['waiver-finish'] if task['completion']['kind']=='verified-delivery' else [])
        for kind in kinds:
            if not any(e.get('kind')==kind and e.get('actor')==task['ownerId'] and
                (e.get('detail') or {}).get('approvalId')==task['approval']['id'] and
                e['detail'].get('evidence')==waiver['evidence'] for e in events):return None
        if task['completion']['kind']=='verified-delivery':
            delivery=delivery_binding(store,db,task,sessions,events)
            if not delivery:return None
            proofs.append(delivery)
    return proofs


def accepted_episode(store,db,task,actor):
    """Validate an owner-requested historical PID episode, not permission to run.

    Cooperative review has its own observed-turn evidence. It is never relabeled
    as a managed assignment, and an expired execution grant is never renewed.
    The existing automatic managed retention path remains unchanged.
    """
    from execution_admission import validate,check_task_binding
    from taskflow import ACTIVE,digest,inside
    if actor!=task.get('ownerId') or not coherent(task):
        raise Conflict('Only the exact owner may retain a coherently completed task')
    if task['completion']['kind']!='verified-task-artifact' or task['approval'].get('auditDisposition')=='WAIVED_BY_USER':
        raise Conflict('Accepted episode requires independent acceptance of a task artifact')
    state=store._lane(task['projectId'],task['laneId'])
    seat=next((p for p in state.get('placements',[]) if p.get('agentId')==actor),None)
    lane=next((l for l in (state.get('laneCatalog') or state.get('lanes') or []) if l.get('id')==(seat or {}).get('laneId')),{})
    role={'project':lane.get('projectId'),'lane':(seat or {}).get('laneId')}
    if not state.get('enabled') or (role['project'],role['lane'])!=(task['projectId'],task['laneId']):
        raise Conflict('Current project and owner Library authority are required')
    if not any(p['agentId']==actor and p['laneId']==task['laneId'] and p['teamId']=='coordinator' for p in state.get('teamLeads',[])):
        raise Conflict('The current workstream lead must retain its own episode')
    sessions={s['agent_id']:s for s in store.flow.catalog()['sessions']}
    owner=sessions.get(actor,{})
    if not actor.startswith('codex:') or owner.get('provider')!='codex' or owner.get('endpoint')!=actor.split(':',1)[1]:
        raise Conflict('Exact registered owner identity required')
    policy=store._one(db,'taskflow_policies',task['laneId'],'lane')
    if (not policy or policy.get('projectId')!=task['projectId'] or not task.get('scopes')
            or any(not inside(p,policy.get('scopes') or []) for p in task['scopes'])
            or any(c not in policy.get('capabilities',[]) for c in task.get('capabilities',[]))):
        raise Conflict('Task no longer belongs to the current project scope')
    if store.delivery.active(db,task['taskId']) or db.execute(
            'SELECT 1 FROM taskflow_assignments WHERE task_id=? AND state IN ('+','.join('?' for _ in ACTIVE)+')',
            (task['taskId'],*ACTIVE)).fetchone():
        raise Conflict('Active work prevents historical episode retention')
    a=store._one(db,'taskflow_assignments',task['assignmentId'])
    if not a or a.get('executionTransport')!='local-pid':raise Conflict('An authenticated returned PID assignment is required')
    reservation=store._one(db,'execution_reservations',a['id'])
    limits=store._one(db,'execution_limits',(reservation or {}).get('policyId'))
    if not limits:raise Conflict('Original sealed execution policy is required')
    limits=validate(limits)
    indexed=db.execute('SELECT policy_id,state FROM execution_reservations WHERE id=?',(a['id'],)).fetchone()
    if (limits['parentId']!=actor or limits['id']!=reservation.get('policyId') or reservation.get('id')!=a['id']
            or indexed is None or tuple(indexed)!=(limits['id'],reservation.get('state'))
            or type(reservation.get('minutes')) is not int or type(a.get('maxMinutes')) is not int
            or reservation['minutes']!=a['maxMinutes'] or not 1<=reservation['minutes']<=limits['maxMinutes']
            or reservation.get('taskBinding')!=limits.get('taskBinding')):
        raise Conflict('Historical owner, charged runtime and sealed task binding must match')
    check_task_binding(db,limits,{'workerId':task['workerId']},reservation['minutes'],'work',task['taskId'],a['id'])
    recovery=(limits.get('taskBinding') or {}).get('recovery')
    if recovery:
        predecessor=store._one(db,'taskflow_assignments',recovery['assignmentId'])
        charged=store._one(db,'execution_reservations',recovery['assignmentId'])
        if (not predecessor or not charged or predecessor.get('taskId')!=task['taskId'] or charged.get('taskId')!=task['taskId']
                or digest(predecessor)!=recovery['assignmentHash'] or digest(charged)!=recovery['reservationHash']):
            raise Conflict('The original failed assignment and its charge must be preserved')
        files_current([recovery['authorization']])
    for at in (reservation.get('at'),a.get('acceptedAt'),a.get('startedAt')):
        if not isinstance(at,(int,float)) or not limits['periodStart']<=at<limits['expiresAt']:
            raise Conflict('Original execution must have started inside its historical allowance')
    work=phase_bindings(store,db,task,limits,sessions,work_only=True)
    if not work:raise Conflict('Original PID assignment, terminal proof, reservation or events changed')
    payload=a['payload']
    fields=('taskId','taskVersion','request','amendments','definitionOfDone','scopes','dependencies','inputs','sourceRefs','pidTask','expectedArtifacts')
    if any(task.get(k)!=payload.get(k) for k in fields) or payload.get('requiredOutcome')!=(task.get('requiredOutcome') or task['definitionOfDone']):
        raise Conflict('Accepted task differs from its executed requirements')
    if payload.get('delivery')!=task.get('delivery'):
        r=task.get('artifactDeliveryReconciliation') or {}
        if (payload.get('delivery') is not None or r.get('assignmentId')!=a['id'] or r.get('actor')!=actor
                or r.get('payloadHash')!=a['payloadHash'] or r.get('requirementsHash')!=pins(task)['requirementsHash']
                or r.get('delivery')!=task['delivery'] or not any(
                    e['kind']=='artifact-delivery-reconciled' and e['actor']==actor and e['detail']==r
                    for e in (json.loads(row[0]) for row in db.execute('SELECT body FROM taskflow_events WHERE task_id=?',(task['taskId'],))))):
            raise Conflict('Changed delivery requires the exact original-owner reconciliation')
    review=task['review'];events=[json.loads(r[0]) for r in db.execute('SELECT body FROM taskflow_events WHERE task_id=?',(task['taskId'],))]
    events=[e for e in events if e.get('taskVersion')==task['taskVersion']]
    claims=[]
    for row in db.execute('SELECT id,body FROM taskflow_audit_claims WHERE task_id=?',(task['taskId'],)):
        c=json.loads(row[1]);e=c.get('executionEvidence') or {};reviewer=sessions.get(c.get('reviewer'),{})
        if (c.get('id')!=row[0] or c.get('taskId')!=task['taskId'] or c.get('state')!='FINISHED' or c.get('verdict')!='accept' or c.get('managedAssignmentId')
                or c.get('reviewer')!=review['reviewer'] or c.get('taskVersion')!=task['taskVersion']
                or c.get('resultHash')!=pins(task)['resultHash'] or c.get('requirementsHash')!=pins(task)['requirementsHash']
                or c.get('projectId')!=task['projectId'] or not c.get('contextAssessment')
                or e.get('adapter')!='cooperative-current-turn' or e.get('providerStartIssued') is not False
                or e.get('state')!='running' or not e.get('turnId') or reviewer.get('provider')!='codex'
                or reviewer.get('endpoint')!=e.get('threadId') or c.get('reviewer')!='codex:'+str(e.get('threadId'))):continue
        if not any(v.get('kind')=='audit-context' and v.get('actor')==c['reviewer'] and
                   v.get('detail')=={'claimId':c['id'],'records':[{'id':r['id'],'bodyHash':r['bodyHash']} for r in c['library']],
                                    'summary':c['contextAssessment'],'executionEvidence':e} for v in events):continue
        if not any(v.get('kind')=='review-accept' and v.get('actor')==c['reviewer'] and v.get('detail')==review for v in events):continue
        if not c.get('startedAt',0)<=review['at']<=c.get('finishedAt',0):continue
        claims.append(c)
    if len(claims)!=1:raise Conflict('One exact independent cooperative acceptance and context event are required')
    store.delivery.valid_approval(task)
    files_current([*task['inputs'],*task['result']['artifacts'],*review['evidence']])
    c=claims[0]
    return {'schema':'ke.taskflow.accepted-episode.v1','taskId':task['taskId'],'taskVersion':task['taskVersion'],
        'ownerId':actor,'projectId':task['projectId'],'laneId':task['laneId'],'request':task['request'],
        'pins':pins(task),'sourceRefs':task.get('sourceRefs',[]),'result':task['result'],'review':review,
        'approval':task['approval'],'completion':task['completion'],'workBindings':work,
        'historicalExecution':{'policyId':limits['id'],'policyHash':digest(limits),'reservationHash':digest(reservation)},
        'reviewBinding':{'kind':'cooperative-current-turn','claimId':c['id'],'claimHash':digest(c),'executionEvidence':c['executionEvidence']},
        'claimLimit':'Accepted historical PID artifact and independent cooperative review only; no current-source, learned-route, model-training, game-quality or execution-authority claim.'}


def retain_episode(store,item,actor,library=None):
    """Explicit owner note operation. No dispatch, grant, model call or replay.

    Task mutations serialize on its DB. Library has an idempotent key and a
    pre-commit guard. If a process dies after Library commit, an identical retry
    attaches the same note; the note itself grants no execution authority.
    """
    from inspector.library import Library,digest as text_digest
    from inspector.transcripts import safe_text
    from taskflow import digest
    if set(item)!={'taskId','version'}:raise ValueError('Retain an exact current taskId and version only; content is derived')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');t=store._one(db,'taskflow_tasks',item['taskId'])
        if not t or type(item['version']) is not int or t['version']!=item['version']:raise Conflict('Current task version required')
        record=accepted_episode(store,db,t,actor);record_hash=digest(record)
        body=json.dumps(record,sort_keys=True);body_hash=text_digest(safe_text(body))
        lib=library or Library(store.root,store.flow)
        def guard():
            current=store._one(db,'taskflow_tasks',t['taskId'])
            if not current or binding(current)!=binding(t) or digest(accepted_episode(store,db,current,actor))!=record_hash:
                raise Conflict('Accepted episode or current owner authority changed before publication')
        def verify(identifier):
            note=lib.item(identifier,project=t['projectId'],lane=t['laneId'],team='')
            if note.get('team')!='' or note.get('sha')!=body_hash or note.get('body')!=safe_text(body):
                raise Conflict('Recorded episode no longer matches its retained Library note')
        if t.get('libraryRetention'):
            prior=t['libraryRetention']
            if prior.get('kind')!='accepted-episode' or prior.get('recordHash')!=record_hash:
                raise Conflict('A different retention already owns this task')
            verify(prior['id']);guard();return t
        result=lib.write('note',{'project':t['projectId'],'lane':t['laneId'],'team':'','kind':'Finding',
            'title':('Accepted PID episode: '+t['request'].splitlines()[0])[:200],'body':body,
            'requestId':'taskflow-completed:'+t['taskId']+':'+str(t['taskVersion'])},guard=guard)
        guard();verify(result['id'])
        t['libraryRetention']={'id':result['id'],'taskVersion':t['taskVersion'],'state':'recorded',
            'kind':'accepted-episode','actor':actor,'recordHash':record_hash,'noteSha256':body_hash}
        t.pop('libraryRetentionError',None)
        store._put(db,t);store._event(db,t,'accepted-episode-retained',actor,t['libraryRetention'])
        return t


def admitted(store,task,db=None,details=False):
    """Read-only current role/scope/identity, with caller transaction when provided."""
    from execution_admission import validate,parent_current
    from taskflow import inside
    try:
        # Pure rejection precedes connection setup. Retain the same check when
        # the caller provides a transaction; current authority is read below.
        if not coherent(task):return False
        if db is None:
            with store.connect() as owned:return admitted(store,task,owned,details)
        current=store._one(db,'taskflow_tasks',task['taskId'])
        if not current or binding(current)!=binding(task):return False
        state=store._lane(task['projectId'],task['laneId'])
        policy=store._one(db,'taskflow_policies',task['laneId'],'lane')
        if not state.get('enabled') or not policy or not policy.get('enabled') or not policy.get('allowManagedStarts') or policy.get('projectId')!=task['projectId']:return False
        limits=validate(policy.get('execution'))
        if not limits['periodStart']<=store.clock()<limits['expiresAt']:return False
        if task.get('ownerId')!=limits['parentId'] or not parent_current(state,policy):return False
        sessions={s['agent_id']:s for s in store.flow.catalog()['sessions']}
        owner=sessions.get(task['ownerId'],{})
        if owner.get('provider')!='codex' or not owner.get('endpoint'):return False
        if task['ownerId'].startswith('codex:'):
            if owner['endpoint']!=task['ownerId'].split(':',1)[1]:return False
        else:
            origins=[r for r in task.get('sourceRefs',[]) if r.get('kind')=='owner-conversation' and r.get('recordSha256') and r.get('providerThreadId')==owner['endpoint']]
            if not owner.get('managed') or not origins:return False
        scopes=task.get('scopes') or []
        destination=(task.get('delivery') or {}).get('destination') or {}
        # Waived cooperative delivery checks its own enrolled installer lane in delivery_binding.
        if destination.get('path') and not task.get('auditWaiver'):scopes=[*scopes,destination['path']]
        if not scopes or any(not inside(p,policy.get('scopes') or []) for p in scopes):return False
        if any(c not in policy.get('capabilities',[]) for c in task.get('capabilities',[])):return False
        sealed=db.execute('SELECT body FROM execution_limits WHERE id=?',(limits['id'],)).fetchone()
        if not sealed or json.loads(sealed[0])!=limits:return False
        proofs=phase_bindings(store,db,task,limits,sessions)
        return proofs if details else bool(proofs)
    except (KeyError,TypeError,ValueError,OSError,sqlite3.Error,Conflict):return False


def retain(store,library=None):
    from inspector.library import Library
    results=[]
    for t in store.list():
        if t.get('libraryRetention') or not admitted(store,t):continue
        try:
            store.delivery.valid_approval(t)
            waived=t['approval'].get('auditDisposition')=='WAIVED_BY_USER'
            final_evidence=t['completion']['functionalChecks']['evidence'] if waived else t['review']['evidence']
            files_current([*t['result']['artifacts'],*final_evidence])
            if t['completion']['kind']=='verified-delivery':
                d=t['deliveryResult'];files_current([d['receipt'],*d['evidence'],*d['observation']['outputs']])
                receipt=json.loads(__import__('pathlib').Path(d['receipt']['path']).read_text())
                if any(receipt.get(k)!=v for k,v in [('taskId',t['taskId']),('approvalId',t['approval']['id']),('pins',pins(t)),('destination',t['delivery']['destination'])]):raise Conflict('Delivery receipt binding changed')
            proofs=admitted(store,t,details=True)
            if not proofs:continue
            record={'phaseBindings':proofs,'ownerId':t['ownerId'],'taskId':t['taskId'],'request':t['request'],'taskVersion':t['taskVersion'],
                    'sourceRefs':t.get('sourceRefs',[]),'result':t['result'],'review':t.get('review'),
                    'auditDisposition':t.get('auditDisposition'),'approval':t['approval'],
                    'completion':t['completion'],'deliveryHistory':t.get('deliveryHistory',[]),
                    'claimLimit':('User-approved, functionally verified result; audit waived. No independent-review, lesson acceptance or model-training claim.' if waived else 'Reviewed task result only; no game-quality, lesson acceptance or model-training claim.')}
            if not admitted(store,t):continue
            lib=library or Library(store.root,store.flow)
            result=lib.write('note',{'project':t['projectId'],'lane':t['laneId'],'team':'coordinator' if waived else 'auditor','kind':'Finding',
                'title':(('Verified task (audit waived): ' if waived else 'Reviewed task: ')+t['request'].splitlines()[0])[:200],'body':json.dumps(record,sort_keys=True),
                'requestId':'taskflow-completed:'+t['taskId']+':'+str(t['taskVersion'])})
            expected={'id':result['id'],'taskVersion':t['taskVersion'],'state':'recorded'}
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE');current=store._one(db,'taskflow_tasks',t['taskId'])
                if not current or binding(current)!=binding(t) or not admitted(store,current,db):continue
                if current.get('libraryRetention'):
                    if current['libraryRetention']!=expected:raise Conflict('Different retention already recorded')
                    continue
                current['libraryRetention']=expected;current.pop('libraryRetentionError',None)
                store._put(db,current);store._event(db,current,'verified-library-retained' if waived else 'reviewed-library-retained','task-board',expected)
            results.append(result)
        except Exception as exc:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE');current=store._one(db,'taskflow_tasks',t['taskId'])
                if (not current or binding(current)!=binding(t) or current.get('libraryRetention')
                        or not admitted(store,current,db)):continue
                if current.get('libraryRetentionError')!=str(exc):
                    current['libraryRetentionError']=str(exc);store._put(db,current)
                    store._event(db,current,'library-retention-blocked','task-board',{'reason':str(exc)})
    return results
