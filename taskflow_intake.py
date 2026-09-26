"""Adopt typed intake into the same-ID queue without taking existing writers.

Source contracts/history stay untouched. Canonical implementation links reserve
execution; related references are context, not evidence of delivery or completion.
"""
from datetime import datetime
import json
from taskflow_delivery import STAGES, REVIEW_STATES

from taskflow import ACTIVE, TERMINAL, RULE_VERSION, digest, encoded


# The attendant's progress record mixes instructions and operational observations.
# Only these request-bearing fields amend a frozen worker contract. Full progress
# remains in intake history, including timestamps, diagnostics and delivery state.
REQUEST_FIELDS = (
    'briefText', 'requestedObjective', 'intendedResult', 'acceptanceCriteria',
    'requiredOutcome', 'requirements', 'delivery',
    'directionAmendments', 'userAmendments', 'latestUserAmendment',
    'automaticContinuationAmendment', 'latestBirdVisibilityAmendment',
    'latestPlacementAmendment', 'latestTaskInteractionAmendment',
    'lifecycleGapAmendment', 'taskInteractionAmendmentPath', 'whitepaperRevisionRequest',
    'diagramWhitepaperRevision', 'laneDirective', 'completionRequires',
    'assessmentAloneIsNotCompletion', 'individualBoxResizingRequired',
    'groupResizeIsInsufficient', 'requestedInitialWorkerCapPerRoom',
    'requiredFirstExample', 'taskBoardPanelFirst', 'implementationScope',
    'originalParentRequest', 'parentTaskId', 'canonicalParentTaskId',
    'requiredSubtaskIds', 'childTaskIds', 'subelements', 'relatedCanonicalTaskIds',
    'relatedInitiativeIds', 'relatedIntakeIds', 'relatedSourceManifest',
    'existingWhitepaperContext', 'existingWhitepaperDraft', 'deliverable',
    'initiativeBoardId', 'initiativeId', 'dependencies', 'recordKind',
)

EXECUTABLE_KINDS = {'task-intake', 'linked-task-subelement', 'initiative-task'}


def disposition(row, records):
    progress=row['progress'];kind=progress.get('recordKind')
    children=list(dict.fromkeys([
        *progress.get('childTaskIds',[]), *progress.get('requiredSubtaskIds',[]),
        *progress.get('taskIds',[]), *progress.get('individualBoardTaskIds',[]),
        *(r['taskId'] for r in records.values() if r['ownerId']==row['ownerId']
          and r['progress'].get('parentTaskId')==row['taskId']),
    ]))
    if kind=='initiative' or children or progress.get('countAsTask') is False:
        return {'kind':'container','executable':False,'childTaskIds':children,
                'reason':'Container only; deliver and review its child tasks separately.'}
    if kind not in EXECUTABLE_KINDS:
        return {'kind':'retained-record','executable':False,'childTaskIds':[],
                'reason':'Retained record; no supported executable intake kind is recorded.'}
    if not row.get('projectId') or not row.get('laneId'):
        return {'kind':'unscoped','executable':False,'childTaskIds':[],
                'reason':'Task is retained; an exact project and lane are required for dispatch.'}
    return {'kind':'executable','executable':True,'childTaskIds':[],'reason':''}


def work_contract(intake):
    return {
        **{key: intake.get(key) for key in (
            'sourceTaskId', 'sourceOwnerId', 'request', 'title', 'definitionOfDone',
            'scopes', 'executionDependencies', 'intakeParentIds', 'canonicalTaskIds',
            'reservedWorkerId', 'inputs',
        )},
        'requestFields': {key: intake.get('sourceProgress', {}).get(key)
                          for key in REQUEST_FIELDS},
    }


def timestamp(value, fallback):
    try:return float(value)
    except (TypeError, ValueError):
        try:return datetime.fromisoformat(value[:-1]+'+00:00' if isinstance(value,str) and value.endswith('Z') else value).timestamp()
        except (TypeError, ValueError):return fallback


def context(row, records):
    progress=row['progress'];deps=[];parents=[]
    for ident in row['dependencies']:
        parent=records.get(ident)
        # An attendant inventory is containment, not a requirement to finish
        # that perpetual attendant role before delivering its captured requests.
        if parent and parent['ownerId']==row['ownerId'] and (
            row['taskId'] in parent['progress'].get('individualBoardTaskIds',[]) or
            progress.get('parentTaskId')==ident and disposition(parent,records)['kind']=='container'
        ):parents.append(ident)
        else:deps.append(ident)
    canonical=list(dict.fromkeys(progress.get('canonicalImplementationTaskIds',[])))
    parent=records.get(progress.get('parentTaskId'))
    if parent and parent['ownerId']==row['ownerId']:
        canonical=list(dict.fromkeys([*canonical,*parent['progress'].get('canonicalImplementationTaskIds',[])]))
    if progress.get('canonicalParentTaskId'):
        canonical=list(dict.fromkeys([*canonical,progress['canonicalParentTaskId']]))
    inputs=[]
    for ref in [progress.get('sourceRequest') or {},*progress.get('attachments',[])]:
        if isinstance(ref,dict) and ref.get('path') and ref.get('sha256'):
            inputs.append({'path':ref['path'],'name':ref.get('fileName') or ref['path'].split('/')[-1],
                           'sha256':ref['sha256'],'bytes':ref.get('byteLength')})
    return {'sourceTaskId':row['taskId'],'sourceVersion':row['version'],'sourceStatus':row['legacyStatus'],
            'sourceOwnerId':row['ownerId'],'sourceProgress':progress,'sourceDependencies':row['dependencies'],
            'intakeParentIds':parents,'executionDependencies':deps,'canonicalTaskIds':canonical,
            'reservedWorkerId':(progress.get('workerAcceptance') or {}).get('worker') if progress.get('implementationAccepted') is True else None,
            'inputs':inputs,'sourceRefs':row['sourceRefs'],'request':row['request'],'title':row['title'],
            'definitionOfDone':row['definitionOfDone'],'scopes':row['scopes']}


def reconcile(store):
    if store.recovery_hold:return []
    records={r['taskId']:r for r in store.legacy()};changed=[]
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        retained={r['id']:json.loads(r['body']) for r in db.execute('SELECT id,body FROM taskflow_tasks')}
        for row in records.values():
            decision=disposition(row,records)
            if not decision['executable']:
                # A formerly executable intake may acquire child work. Keep
                # current ownership, but invalidate unaccepted offers and never
                # schedule the parent alongside its new children.
                existing=store._one(db,'taskflow_tasks',row['taskId'])
                if existing and existing.get('intake') and existing.get('intakeDisposition')!=decision:
                    existing.update(intakeDisposition=decision,waitReason=decision['reason'])
                    # A structural split does not amend the already returned
                    # result. Keep its exact review version and claim usable;
                    # disposition still prevents another parent dispatch.
                    if existing['state'] not in (*STAGES,'REVIEW'):existing['taskVersion']+=1
                    if existing['state'] not in (*ACTIVE,*TERMINAL,*STAGES,'REVIEW'):existing['state']='NEEDS_COORDINATION'
                    store._put(db,existing);store._event(db,existing,'intake-disposition','task-board',decision)
                    changed.append(row['taskId'])
                continue
            incoming=context(row,records);t=store._one(db,'taskflow_tasks',row['taskId'])
            if t and incoming['reservedWorkerId'] and any(a.get('acceptedAt') is not None or a.get('providerTurnId') for a in store._past(db,t['taskId'])):
                # An imported acceptance report cannot replace already bound
                # TaskFlow custody or invalidate a returned result's review.
                # Keep it as attributed source context; real request fields
                # still participate in the semantic contract comparison below.
                incoming['acceptanceObservation']={'reportedWorkerId':incoming['reservedWorkerId'],'authoritativeWorkerId':t.get('workerId'),'sourceVersion':row['version'],'disposition':'late matching observation' if incoming['reservedWorkerId']==t.get('workerId') else 'conflicting source observation; existing TaskFlow custody retained'}
                incoming['reservedWorkerId']=(t.get('intake') or {}).get('reservedWorkerId')
            parent=retained.get(row['progress'].get('parentTaskId',''))
            if parent and parent['state'] in (*ACTIVE,*STAGES,'REVIEW'):
                incoming['canonicalTaskIds']=list(dict.fromkeys([*incoming['canonicalTaskIds'],parent['taskId']]))
            prior_contract={**(t.get('intake') or {}),'title':(t.get('intake') or {}).get('title',t['title'])} if t else None
            if t and (not t.get('intake') or t['intake']['sourceVersion']>=incoming['sourceVersion'] and digest(work_contract(prior_contract))==digest(work_contract(incoming))):
                p=store._one(db,'taskflow_policies',row['laneId'],'lane')
                if t.get('intake') and t['state']=='CAPTURED' and not t['amendments'] and not t['intake']['canonicalTaskIds'] and not t['intake'].get('reservedWorkerId') and p and p['enabled'] and p['autoReady']:
                    t.update(state='READY',waitReason='',scopes=t['scopes'] or p['scopes'],capabilities=p['capabilities'][:1],policyVersion=p['version'])
                    store._put(db,t);store._event(db,t,'ready','task-board',{'policyVersion':p['version']});changed.append(t['taskId'])
                continue
            if not t and (row['state'] in (*ACTIVE,*TERMINAL,*STAGES,'REVIEW') or row['legacyStatus']=='WORKING'):continue
            prior=t.get('intake') if t else None
            prior_station=t['station'] if t else 'board:'+str(row['laneId'])
            if not t:
                p=store._one(db,'taskflow_policies',row['laneId'],'lane')
                ready=bool(p and p['enabled'] and p['autoReady'] and not incoming['canonicalTaskIds'] and not incoming['reservedWorkerId'])
                t={'taskId':row['taskId'],'version':0,'taskVersion':1,'title':row['title'],
                   'request':row['request'],'ownerId':row['ownerId'],'capturedBy':row['capturedBy'],
                   'projectId':row['projectId'],'laneId':row['laneId'],'createdAt':timestamp(row['createdAt'],store.clock()),
                   'state':'LINKED' if incoming['canonicalTaskIds'] else 'READY' if ready else 'CAPTURED',
                   'waitReason':'Existing implementation retained; inspect linked tasks' if incoming['canonicalTaskIds'] else '' if ready else 'Configure scoped dispatch or define ready work',
                   'scopes':row['scopes'] or (p['scopes'] if ready else []),'capabilities':p['capabilities'][:1] if ready else ['general'],
                   'dependencies':incoming['executionDependencies'],'inputs':incoming['inputs'],'sourceRefs':row['sourceRefs'],
                   'definitionOfDone':row['definitionOfDone'] if isinstance(row['definitionOfDone'],str) else encoded(row['definitionOfDone']),
                   'amendments':[],'result':None,'assignmentId':None,'station':'board:'+row['laneId'],
                   'policyVersion':p['version'] if ready else None,'ruleVersion':RULE_VERSION}
            elif prior:
                # Older intake rows predate title preservation. Their existing
                # task title is the migration baseline, not a new instruction.
                prior={**prior, 'title':prior.get('title',t['title'])}
                if digest(work_contract(prior))!=digest(work_contract(incoming)):
                    t['taskVersion']+=1
                    t['amendments'].append({'text':'Source intake updated; reconcile retained context before continuing.',
                                            'actor':row['ownerId'],'at':store.clock(),'intakeVersion':row['version']})
                    if t['state'] not in (*ACTIVE,*TERMINAL,*STAGES,'REVIEW'):
                        t.update(state='LINKED' if incoming['canonicalTaskIds'] else 'NEEDS_COORDINATION',
                                 waitReason='Existing implementation retained' if incoming['canonicalTaskIds'] else 'Reconcile the updated intake context and scope')
                    else:t['waitReason']='Updated source intake awaits reconciliation with the existing assignment'
                    # Original request and pins stay immutable; current source
                    # context is supplied alongside them to every receiver.
            if incoming['sourceProgress'].get('delivery') is not None and (not prior or prior.get('sourceProgress',{}).get('delivery')!=incoming['sourceProgress']['delivery']):
                from taskflow_delivery import contract
                t['delivery']=contract(incoming['sourceProgress']['delivery'])
            t['intake']=incoming
            t['intakeDisposition']=decision
            if incoming['reservedWorkerId'] and t['state'] not in (*ACTIVE,*TERMINAL,*STAGES,'REVIEW'):
                t.update(state='RESERVED',workerId=incoming['reservedWorkerId'],station=incoming['reservedWorkerId'],
                         waitReason='Worker acceptance is recorded; dispatcher binding and current execution remain unverified')
            store._put(db,t);store._event(db,t,'intake-updated' if prior else 'intake-adopted','task-board',{'source':incoming},source=prior_station,destination=t['station'])
            changed.append(t['taskId'])
    return changed


def linked_tasks(store,task):
    result=[]
    for ident in task.get('intake',{}).get('canonicalTaskIds',[]):
        linked=store.get(ident)
        result.append({'taskId':ident,'title':(linked or {}).get('title'),
                       'state':(linked or {}).get('state','MISSING'),'ownerId':(linked or {}).get('ownerId'),
                       'waitReason':(linked or {}).get('waitReason')})
    return result
