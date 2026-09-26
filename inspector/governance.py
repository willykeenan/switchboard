"""Role-specific operating context. Observation never starts or redirects work."""
import shlex
from urllib.parse import urlencode

def library_access(workflow,session,base,actions):
 actions=list(actions)
 if 'context' in actions:actions.append('workspace')
 if 'note' in actions:actions.append('lifecycle')
 scope={k:base[k] for k in ('project','lane','team')}
 return {'url':'/library?'+urlencode(scope), 'command':'python3 '+shlex.quote(str(workflow.root/'librarianctl.py'))+' context --session '+shlex.quote(session), 'actions':actions, 'scope':scope}

def role_contract(workflow,session):
 state=workflow.snapshot();p=next((p for p in state['placements'] if p['agentId']==session),None)
 if not p:return None
 lane=next((l for l in state['lanes'] if l['id']==p['laneId']),None)
 if not lane:return None
 team=p.get('teamId') or p['role'];t=next((t for t in state['teams'] if t['id']==team and t['laneId']==lane['id']),None);role=p['role']
 context_team=t['parentId'] if team.startswith('lib-') and t else team
 base={'project':lane['projectId'],'lane':lane['id'],'team':context_team,'workflowRevision':state['revision'],'authority':'Keep current owners and running work. Use permitted passive messages; no automatic wake or interruption.'}
 if lane['id']=='project-'+lane['projectId']+'-librarian':
  base.update(lane='',team='')
  return {**base,'function':'Project Librarian','library':library_access(workflow,session,base,['context','search','note','review','save-context']),'duties':['Maintain project evidence across workstreams; retain sources, versions, decisions, contradictions and context history.','Reconcile team Context Librarians and Research Librarians without changing their assignments or source ownership.','Record project-wide evidence coverage and reuse decisions; scientific admission and qualification remain with their current owners.','Read runtime observations when relevant. Library records alone never establish that a model agent is running.']}
 if role=='researcher':
  if team.startswith('lib-context-'):function='Context Librarian';duties=['Maintain the current question, decisions, constraints, uncertainty and next handoff in the team context record.','Reconcile team work and evidence before changing context; preserve version history and cite contradictory records.','Read active job records when their outputs or source versions affect context. Never stop or restart another owner’s job.']
  elif team.startswith('lib-research-'):function='Research Librarian';duties=['Search the shared project library and active work before new research.','Preserve sources, content versions, provenance, negative findings and evidence links.','Record a team-scoped reuse, extend, replicate or new-research decision with pinned evidence versions.']
  else:function='Research specialist / team';duties=['Read your team context and search the shared project library before starting a new research question.','Record the library review and identify the remaining evidence gap. Do not repeat existing work without a reason.','The designated specialist leads methods, bounded specialist teams and synthesis. Return findings, uncertainty and reproducible evidence to the Workstream Owner and library.','Choose ordinary reasoning for dependent small investigations; bounded CPU/GPU jobs for executable work; agent swarms only for independent research assignments under the existing allowed runtime and one-writer rules.']
  return {**base,'function':function,'library':library_access(workflow,session,base,['context','search','note','review','save-context']),'requiredBeforeNewResearch':'Read current team context, search prior and active work, and record a review whose context and source versions are current.','duties':duties}
 if lane['id']=='project-'+lane['projectId']+'-chief':return {**base,'function':'Chief of Staff','duties':['Translate the operator’s intent into scoped work for the existing Workstream Owner.','Before routing a change, read the target lane’s current owner, decisions, dependencies, active work, live jobs and context fingerprint.','Attach that fingerprint and the exact proposed change to a permitted passive request. The receiving owner rechecks it at a safe boundary before adopting it.','If the target context changed, refresh and reconcile. Do not replace the owner, disturb a live turn, start competing work or treat an audit finding as approval.']}
 if role=='coordinator':
  base.update(team='')
  work_lanes=[l for l in state['lanes'] if l['projectId']==lane['projectId'] and l.get('lifecycle','active')=='active' and l.get('kind','strategy')!='support' and not l['id'].startswith('project-'+lane['projectId']+'-')]
  owners=[s['agentId'] for s in state['placements'] if s['laneId']==lane['id'] and (s.get('teamId') or s['role'])=='coordinator']
  lead=next((r['agentId'] for r in state.get('teamLeads',[]) if r['laneId']==lane['id'] and r['teamId']=='coordinator'),owners[0] if len(owners)==1 else None)
  shared=len(work_lanes)==1 and work_lanes[0]['id']==lane['id'] and lead==session
  base.update(additionalFunctions=['Chief of Staff'] if shared else [],projectLeadership={'mode':'shared-workstream-owner' if len(work_lanes)==1 else 'distinct-chief-required','activeWorkstreamCount':len(work_lanes),'sharedIdentity':session if shared else None,'basis':'Read-only interpretation of saved project topology; no assignment or provider session is created.'})
  return {**base,'function':'Workstream Owner','library':library_access(workflow,session,base,['context','search','note','save-context']),'duties':['Own design, scope, decisions, assignment contracts, integration direction and delivery.','Give Research evidence questions and Build & Integration concrete output contracts.','Recheck incoming change requests against current context at a safe boundary; stale requests require reconciliation.','Return outcomes and decision requests through the Chief of Staff to the operator; use Alignment and Independent Audit for checks.']}
 if role=='auditor':
  base.update(lane='',team='')
  return {**base,'function':'Independent Audit team','library':library_access(workflow,session,base,['context','search']),
          'auditCommand':'python3 '+shlex.quote(str(workflow.root/'taskflowctl.py'))+' audit-claim --file /absolute/audit-request.json',
          'duties':['Share the project audit queue; atomically claim one task before reviewing. Never review your own implementation or integration.',
                    'Read the exact request, acceptance criteria, prior results and versioned project Library context. Pin relevant source versions and record coverage.',
                    'Resolve missing research context through task-linked questions to permitted project researchers; answers and sources stay with the audit.',
                    'Return written findings plus mechanical evidence. Routine in-scope corrections return to the same Task Board without owner acknowledgment.',
                    'A reviewed candidate is not installed completion. Retain every unmet delivery and scientific gate; report queue pressure and missing reviewer skills.']}
 if role=='worker':return {**base,'function':'Task worker','library':library_access(workflow,session,base,['context','search']),
                           'duties':['Lead your assigned Task Board task through implementation, independent audit and the required delivery state.',
                                     'Use the shared Task Board queue and exact task identity for results and in-scope corrections. Owners coordinate dependencies and exceptions.',
                                     'Preserve one writer for each shared source or installation target; a seat does not grant new source or external-action authority.']}
 if role=='writer':return {**base,'function':'Build & Integration','library':library_access(workflow,session,base,['context','search']),'duties':['Build, integrate, repair and verify the Workstream Owner’s approved design.','Each assigned worker leads its task. Coordinate dependencies, APIs, versions and tests through the shared Task Board; serialize only actual shared-source and installation writes.','Use agent swarms only when bounded independent work and permitted runtime capacity justify it; otherwise build directly.','Keep exactly one writer per shared target; retain exact source custody and return evidence through the shared task and Audit team.']}
 return base
