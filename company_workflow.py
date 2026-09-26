"""Customer-owned company workflow defaults, independent of KE identities/data."""
import copy
import hashlib

VERSION='company-workflow-1'

CONTRACT={
    'version':VERSION,
    'queue':'Task Board',
    'flow':['capture','scoped ready work','courier','qualified available worker','independent audit','verified completed task'],
    'taskLeadership':'The assigned worker leads the task through delivery; the owner coordinates requirements, dependencies and integration.',
    'audit':{'scope':'project','pool':True,'maxConcurrentPerReviewer':1,'context':'versioned project Library and task-specific research questions',
             'queue':'Shared project review queue on original task IDs','offerSeconds':60,'provider':'codex',
             'assignment':'Oldest eligible task; one unaccepted offer per available independent reviewer; explicit blocks remain binding',
             'staffing':'Use enrolled existing reviewers; expose backlog and missing skills. Staffing does not imply inference or launch approval.'},
    'couriers':{'maximumIdle':5,'standbyInference':False},
    'authority':'Purposeful project role routes preserve explicit blocks, source custody, scientific authority and external-action gates.',
}


def lane_id(project,kind):
    prefix='workstream-' if kind=='work' else 'project-'
    value=prefix+project+'-'+kind
    return value if len(value)<=80 else prefix+hashlib.sha256(project.encode()).hexdigest()[:24]+'-'+kind


def project_lanes(project,existing):
    """Add missing structure only; never move people or rewrite research lanes."""
    lanes=copy.deepcopy(existing);owned=[l for l in lanes if l['projectId']==project]
    definitions=[('work','Workstream','Deliver scoped tasks through workers and independent audit.','strategy','general'),
                 ('librarian','Project Library','Retain project requirements, decisions, source versions, evidence and research context.','support','general'),
                 ('alignment','Audit team','Share independent reviews across available auditors. Use project Library context and task-linked research questions.','support','alignment')]
    for kind,name,objective,lane_kind,support in definitions:
        match=next((l for l in owned if (kind=='work' and l.get('kind','strategy')!='support') or (kind=='alignment' and l.get('supportMode')=='alignment') or l['id']==lane_id(project,kind)),None)
        if match:
            # Adoption is additive; owner-authored lane names and objectives stay.
            match['companyWorkflowVersion']=VERSION
            continue
        new={'id':lane_id(project,kind),'projectId':project,'name':name,'objective':objective,'kind':lane_kind,'supportMode':support,'lifecycle':'active','aliases':[],'companyWorkflowVersion':VERSION}
        lanes.append(new);owned.append(new)
    return lanes


def role_route(state,sender,recipient):
    """The adopted project Audit team can exchange task/context with its teams.

    Called only after exact directed allow/block and closed-lane checks.
    Neither this function nor adoption starts a provider session.
    """
    if not state.get('enabled') or state.get('defaultCommunication')=='explicit-only':return False
    lanes={l['id']:l for l in state.get('laneCatalog') or []}
    seats={p['agentId']:p for p in state['placements']}
    a,b=seats.get(sender),seats.get(recipient)
    if not a or not b:return False
    la,lb=lanes.get(a['laneId']),lanes.get(b['laneId'])
    if not la or not lb or la['projectId']!=lb['projectId']:return False
    for auditor,home,peer in [(a,la,b),(b,lb,a)]:
        if auditor['role']=='auditor' and home.get('supportMode')=='alignment' and home.get('companyWorkflowVersion')==VERSION and peer['role'] in ('auditor','worker','writer','researcher','coordinator'):return True
    return False


def describe(project,lanes,placements):
    owned=[l for l in lanes if l['projectId']==project]
    audits=[l for l in owned if l.get('supportMode')=='alignment']
    ids={l['id'] for l in audits}
    members=[p['agentId'] for p in placements if p['laneId'] in ids and p['role']=='auditor']
    return {**copy.deepcopy(CONTRACT),'projectId':project,'adopted':any(l.get('companyWorkflowVersion')==VERSION for l in audits),
            'auditLaneIds':sorted(ids),'reviewerIds':members,'staffingState':'staffed' if len(members)>=2 else 'single-reviewer' if members else 'unstaffed'}
