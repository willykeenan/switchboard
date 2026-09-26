"""Pure layout operations; a single workflow revision owns lanes and team trees.

These are organization records, never provider tasks, process trees or file leases.
The legacy workspace catalog remains historical input until the first layout edit.
"""
import copy
import uuid
from workspace import bounded, identifier

ROLES = ('coordinator', 'researcher', 'writer', 'worker', 'auditor')
TEAM_NAMES = dict(coordinator='Coordination', researcher='Research', writer='Writing', worker='Execution', auditor='Audit')
ALIGNMENT_CHARTER = {
    'purpose': 'Audit how lanes fit together as work progresses.',
    'checks': ['Shared input and output contracts', 'Dependencies and duplicate work',
               'Evidence, ownership and acceptance criteria', 'Changes that could break another lane'],
    'deliverable': 'A finding with affected lanes, evidence, impact and a proposed correction for the operator.',
    'routineReviewApprovalRequired': False,
    'decisionRule': 'Independent reviewers may claim approved tasks and return in-scope corrections through the Task Board without another owner or operator acknowledgment. New scope or external actions require their existing authority. '
                    'A finding, peer request or lane placement is not approval. '
                    'Keep existing work and scientific ownership intact; do not wake or redirect agents.',
    'mode': 'advisory', 'humanConfirmationRequired': True,
}

def active(lane): return lane.get('lifecycle', 'active') == 'active'
def lanes_for(state, legacy): return copy.deepcopy(state.get('laneCatalog') if state.get('laneCatalog') is not None else legacy)
def team_of(placement): return placement.get('teamId') or placement['role']

def validate_layout(data, projects, legacy):
    lanes = lanes_for(data, legacy)
    if not isinstance(lanes, list) or len(lanes) > 1000: raise ValueError('Too many lanes')
    lane_map = {}
    for lane in lanes:
        identifier(lane['id']); bounded(lane['name'], 'lane name', 100); bounded(lane['objective'], 'lane objective')
        if lane['id'] in lane_map or lane['projectId'] not in projects: raise ValueError('Unknown project or duplicate lane')
        if lane.get('lifecycle', 'active') not in ('active', 'closed', 'merged'): raise ValueError('Invalid lane lifecycle')
        if lane.get('kind', 'strategy') not in ('strategy', 'support'): raise ValueError('Invalid lane kind')
        if lane.get('supportMode', 'general') not in ('general', 'alignment'): raise ValueError('Invalid support mode')
        lane_map[lane['id']] = lane
    for lane in lanes:
        if lane.get('mergedInto') and (lane['mergedInto'] not in lane_map or lane['mergedInto'] == lane['id']): raise ValueError('Invalid merge destination')
    teams = {}
    for team in data['teams']:
        if set(team) != {'id','laneId','role','parentId','name','objective'}: raise ValueError('Invalid team fields')
        identifier(team['id']); bounded(team['name'], 'team name', 100); bounded(team['objective'], 'team objective', required=False)
        if team['id'] in ROLES or team['id'] in teams or team['laneId'] not in lane_map or team['role'] not in ROLES: raise ValueError('Invalid team identity')
        teams[team['id']] = team
    for team in teams.values():
        cursor=team; seen={team['id']}
        while cursor['parentId'] != cursor['role']:
            parent=teams.get(cursor['parentId'])
            if not parent or parent['laneId'] != team['laneId'] or parent['role'] != team['role'] or parent['id'] in seen: raise ValueError('A team needs a parent in its own role, without a cycle')
            seen.add(parent['id']); cursor=parent
            if len(seen)>8: raise ValueError('Use at most eight nested team levels')
    for p in data['placements']:
        if p.get('teamId') and (p['teamId'] not in teams or (teams[p['teamId']]['laneId'],teams[p['teamId']]['role']) != (p['laneId'],p['role'])): raise ValueError('Session team must belong to its lane and role')
    def valid_team(row):
        return row['laneId'] in lane_map and (row['teamId'] in ROLES or row['teamId'] in teams and teams[row['teamId']]['laneId']==row['laneId'])
    for key in ('teamPositions','teamLeads'):
        seen=set()
        for row in data[key]:
            expected={'laneId','teamId','x','y'} if key=='teamPositions' else {'laneId','teamId','agentId'}
            if set(row)!=expected or not valid_team(row) or (row['laneId'],row['teamId']) in seen: raise ValueError('Invalid team configuration')
            seen.add((row['laneId'],row['teamId']))
            if key=='teamPositions' and any(type(row[k]) not in (int,float) or not 0<=row[k]<=12000 for k in ('x','y')): raise ValueError('Invalid team position')
            if key=='teamLeads' and not any(p['agentId']==row['agentId'] and p['laneId']==row['laneId'] and team_of(p)==row['teamId'] for p in data['placements']): raise ValueError('The team lead must be a directly assigned member')
    if any(not isinstance(data[k],list) or len(data[k])>5000 for k in ('teams','teamPositions','teamLeads')): raise ValueError('Layout is too large')
    return lane_map

def prune_leads(data):
    data['teamLeads']=[r for r in data['teamLeads'] if any(p['agentId']==r['agentId'] and p['laneId']==r['laneId'] and team_of(p)==r['teamId'] for p in data['placements'])]

def mutate_layout(data, op, item, legacy):
    data['laneCatalog']=lanes_for(data,legacy)
    lanes={l['id']:l for l in data['laneCatalog']}
    if op=='lane-create':
        lid=item.get('id') or 'lane-'+uuid.uuid4().hex[:12]
        if lid in lanes: raise ValueError('Lane already exists')
        data['laneCatalog'].append(dict(id=lid,projectId=item['projectId'],name=item['name'],objective=item['objective'],kind=item.get('kind','strategy'),supportMode=item.get('supportMode','general'),lifecycle='active',aliases=[]))
    elif op in ('lane-edit','lane-close','lane-reopen','lane-merge'):
        lane=lanes[item['laneId']]
        if op=='lane-edit':
            for key in ('name','objective','kind','supportMode'):
                if key in item: lane[key]=item[key]
        elif op=='lane-reopen':
            lane['lifecycle']='active'; lane.pop('mergedInto',None)
        elif op=='lane-close':
            if not active(lane): raise ValueError('Lane is already inactive')
            lane['lifecycle']='closed'
        else:
            target=lanes[item['targetId']]
            if not active(lane) or not active(target) or lane['id']==target['id'] or lane['projectId']!=target['projectId']: raise ValueError('Merge two active lanes in the same project')
            # Preserve the source team tree and its leads as named subteams.
            for role in ROLES:
                members=[p for p in data['placements'] if p['laneId']==lane['id'] and p['role']==role]
                children=[t for t in data['teams'] if t['laneId']==lane['id'] and t['role']==role]
                if not members and not children: continue
                tid='team-'+uuid.uuid4().hex[:12]
                data['teams'].append(dict(id=tid,laneId=target['id'],role=role,parentId=role,name=(TEAM_NAMES[role]+' · '+lane['name'])[:100],objective=lane['objective']))
                for p in members:
                    p['laneId']=target['id'];p['teamId']=p.get('teamId') or tid
                for t in children:
                    t['laneId']=target['id']
                    if t['parentId']==role:t['parentId']=tid
                for r in data['teamLeads']:
                    if r['laneId']==lane['id'] and (r['teamId']==role or any(t['id']==r['teamId'] for t in children)):
                        r['laneId']=target['id']
                        if r['teamId']==role:r['teamId']=tid
            data['teamPositions']=[r for r in data['teamPositions'] if r['laneId']!=lane['id']]
            lane.update(lifecycle='merged',mergedInto=target['id'])
    elif op=='team':
        if not active(lanes[item['laneId']]): raise ValueError('Reopen this lane before editing teams')
        tid=item.get('id') or 'team-'+uuid.uuid4().hex[:12]
        prior=next((t for t in data['teams'] if t['id']==tid),None)
        team={k:item[k] for k in ('laneId','role','parentId','name','objective')};team['id']=tid
        if prior and (prior['laneId'],prior['role'])!=(team['laneId'],team['role']): raise ValueError('Keep an existing team in its lane and role')
        data['teams']=[t for t in data['teams'] if t['id']!=tid]+[team]
        if 'leadAgentId' in item:
            data['teamLeads']=[r for r in data['teamLeads'] if (r['laneId'],r['teamId'])!=(team['laneId'],tid)]
            if item['leadAgentId']:data['teamLeads'].append(dict(laneId=team['laneId'],teamId=tid,agentId=item['leadAgentId']))
    elif op=='team-remove':
        team=next(t for t in data['teams'] if t['id']==item['teamId'])
        for t in data['teams']:
            if t['parentId']==team['id']: t['parentId']=team['parentId']
        for p in data['placements']:
            if p.get('teamId')==team['id']:
                p.pop('teamId')
                if team['parentId'] not in ROLES:p['teamId']=team['parentId']
        data['teams']=[t for t in data['teams'] if t['id']!=team['id']]
        for key in ('teamPositions','teamLeads'):data[key]=[r for r in data[key] if r['teamId']!=team['id']]
    elif op in ('team-position','team-lead'):
        key='teamPositions' if op=='team-position' else 'teamLeads'
        data[key]=[r for r in data[key] if (r['laneId'],r['teamId'])!=(item['laneId'],item['teamId'])]
        if op=='team-position' or item.get('agentId'):data[key].append(copy.deepcopy(item))
    else: raise ValueError('Unknown layout action')

def lane_context(state, lane):
    if not lane: return None
    result={k:lane.get(k) for k in ('id','name','objective','kind','supportMode')}
    result['lifecycle']=lane.get('lifecycle','active')
    if lane.get('supportMode')=='alignment':result['charter']=copy.deepcopy(ALIGNMENT_CHARTER)
    result['teams']=[t for t in state['teams'] if t['laneId']==lane['id']]
    result['teamLeads']=[t for t in state['teamLeads'] if t['laneId']==lane['id']]
    result['teamSemantics']='Existing-session organization only. Team leads do not gain authority to interrupt, spawn or redirect peers; provider parentage and source owners remain unchanged.'
    return result

# Current-state recovery includes both the canonical layout and its original
# workspace catalog. Each SQLite file is copied through SQLite's snapshot API.
STORE_NAMES=('workflow.sqlite3','workspaces.sqlite3','production.sqlite3','library/library.sqlite3')
def snapshot_stores(root):
    import base64, hashlib, sqlite3, tempfile
    from pathlib import Path
    files=[]
    for name in STORE_NAMES:
        path=Path(root)/'runtime'/name
        if not path.exists(): continue
        if path.is_symlink(): raise ValueError('Layout store must not be a symlink')
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/name;target.parent.mkdir(parents=True,exist_ok=True)
            from store import query_only_existing
            source = query_only_existing(path) if name == 'production.sqlite3' else sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
            with source as src, sqlite3.connect(target) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('Layout snapshot failed')
            raw=target.read_bytes()
        files.append({'name':name,'sha256':hashlib.sha256(raw).hexdigest(),'body':base64.b64encode(raw).decode()})
    path=Path(root)/'board.sqlite3'
    if path.exists():
        if path.is_symlink():raise ValueError('TaskFlow store must not be a symlink')
        from taskflow_recovery import snapshot
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'taskflow.sqlite3';coverage=snapshot(path,target)
            if coverage:
                raw=target.read_bytes()  # The helper has enforced its byte budget.
                files.append({'name':'board.sqlite3','sha256':hashlib.sha256(raw).hexdigest(),
                              'body':base64.b64encode(raw).decode(),'coverage':coverage})
    return {'schemaVersion':'ke.workflow.recovery.v1','files':files}

def restore_stores(root, snapshot):
    """Restore only into a new disposable directory, never over an existing store."""
    import base64, hashlib, sqlite3
    from pathlib import Path
    if snapshot.get('schemaVersion')!='ke.workflow.recovery.v1':raise ValueError('Invalid layout recovery version')
    root=Path(root);root.mkdir(parents=True,exist_ok=False);runtime=root/'runtime';runtime.mkdir()
    seen=set();total=0
    for record in snapshot['files']:
        name=record['name']
        if name not in (*STORE_NAMES,'board.sqlite3') or name in seen:raise ValueError('Unexpected layout recovery file')
        seen.add(name);raw=base64.b64decode(record['body'],validate=True)
        if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('Layout backup digest mismatch')
        target=root/name if name=='board.sqlite3' else runtime/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw);target.chmod(0o600);total+=len(raw)
        with sqlite3.connect(target) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Invalid layout backup database')
    return {'ok':True,'files':len(seen),'bytes':total,'live_database_touched':False}
