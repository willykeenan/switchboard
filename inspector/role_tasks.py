"""Read-only task groups from exact contracts plus explicitly scoped TaskFlow rows."""
from contextlib import closing
from pathlib import Path
import hashlib,json,sqlite3,time
from .role_work import RoleWork, PROJECT, LANE, SCOPE_WHITESPACE


def obligation_action(row, names):
    """Keep an action's explanation and responsibility bound to one obligation."""
    closed = row['closedAt'] is not None
    returned = row['returnedAt'] is not None
    result = row.get('return') or {}
    blocked = not closed and returned and result.get('disposition') == 'BLOCKED'
    who = None if closed else row['sender'] if returned else row['recipient']
    return {
        'id': row['id'], 'state': row['state'],
        'lifecycle': 'closed' if closed else 'open',
        'needsAttention': not closed and (blocked or bool(row['overdue'])),
        'awaitingReturn': not closed and not returned,
        'blocked': blocked, 'overdue': not closed and bool(row['overdue']),
        'originId': row['sender'], 'origin': names.get(row['sender'], 'Unresolved project task'),
        'nextActorId': who, 'nextActor': names.get(who, 'Unresolved project task') if who else None,
        'summary': ((row.get('closure') or {}).get('note') or result.get('summary') or
                    ('Closed obligation; retained as history.' if closed else
                     'Awaiting a bound work return. Acceptance alone does not prove execution.'))[:600],
    }


def grouped(work, registry):
    names = {m['agentId']: m['label'] for m in registry['members']}
    groups = {}
    for row in work:
        alias = next((g for g in registry.get('taskGroups', []) if
                      row['recipient'] == g['recipient'] and row['assignmentId'] in g['assignmentIds']), None)
        key = (row['recipient'], alias['id'] if alias else row['assignmentId'])
        groups.setdefault(key, {'rows': [], 'alias': alias})['rows'].append(row)
    result = []
    for (recipient, key), g in groups.items():
        history = sorted(g['rows'], key=lambda x: (x['createdAt'], x['id']), reverse=True)
        live = [x for x in history if x['closedAt'] is None]
        actions = [obligation_action(x, names) for x in live]
        # Blockers first, then overdue work, then newest. Every header field is
        # taken from this same action; all other live actions stay explicit.
        actions.sort(key=lambda a: (a['blocked'], a['overdue']), reverse=True)
        primary = actions[0] if actions else obligation_action(history[0], names)
        current = next(x for x in history if x['id'] == primary['id'])
        state = ('BLOCKED / returned' if primary['blocked'] else primary['state']) if live else \
                'Obligations closed / ' + (current.get('closure') or {}).get('outcome', 'unknown').upper()
        title = g['alias']['title'] if g['alias'] else current['assignmentId'].replace('-', ' ').replace(':', ' · ')
        result.append({
            **primary, 'id': 'group-' + hashlib.sha256((recipient + '\n' + key).encode()).hexdigest(),
            'title': title, 'kind': 'Bound work group', 'state': state,
            'owner': names.get(recipient, 'Unresolved project task'),
            'selectedObligationId': primary['id'] if live else None,
            'needsAttention': any(a['needsAttention'] for a in actions),
            'awaitingReturn': any(a['awaitingReturn'] for a in actions),
            'updatedAt': max(x['closedAt'] or x['returnedAt'] or x['acceptedAt'] or x['createdAt'] for x in history),
            'technicalId': key, 'obligationCount': len(history), 'openObligations': len(live),
            'transport': 'Not measured here; read/pickup/return are separate from message delivery.',
            'progressMeaning': 'Grouped contract obligations, not measured execution or completion.',
            'actions': actions,
            'history': [{'id': x['id'], 'assignmentId': x['assignmentId'], 'state': x['state'],
                         'origin': names.get(x['sender'], x['sender']), 'recipient': names.get(x['recipient'], x['recipient']),
                         'createdAt': x['createdAt'], 'acceptedAt': x['acceptedAt'], 'returnedAt': x['returnedAt'],
                         'closedAt': x['closedAt'], 'summary': (x.get('return') or {}).get('summary'),
                         'closure': x.get('closure')} for x in history],
        })
    return result


def formal(database, reader, lanes, actor, registry):
    """Do not instantiate TaskFlow: that constructor installs schemas and mutates."""
    names={m['agentId']:m['label'] for m in registry['members']};result=[]
    with closing(reader(Path(database),timeout=2)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN');ticks=[0]
        def budget():ticks[0]+=1;return ticks[0]>2000
        db.set_progress_handler(budget,1000)
        bad = db.execute('''SELECT 1 FROM taskflow_tasks WHERE CASE
                WHEN typeof(body) != 'text' THEN 1
                WHEN instr(body,char(0))>0 THEN 1
                WHEN length(body)>1048576 THEN 1
                WHEN NOT json_valid(body) THEN 1
                WHEN json_type(body) != 'object' THEN 1
                WHEN json_type(body,'$.projectId') IS NOT 'text'
                  OR json_type(body,'$.laneId') IS NOT 'text' THEN 1
                WHEN length(json_extract(body,'$.projectId')) NOT BETWEEN 1 AND 200
                  OR length(json_extract(body,'$.laneId')) NOT BETWEEN 1 AND 200 THEN 1
                WHEN length(trim(json_extract(body,'$.projectId'),?))=0
                  OR length(trim(json_extract(body,'$.laneId'),?))=0 THEN 1
                WHEN instr(replace(body -> '$.projectId',?,''),?)>0
                  OR instr(replace(body -> '$.laneId',?,''),?)>0 THEN 1
                WHEN (SELECT count(*) FROM json_each(body)
                      WHERE key IN ('projectId','laneId')) != 2 THEN 1
                ELSE 0 END LIMIT 1''', (SCOPE_WHITESPACE, SCOPE_WHITESPACE,
                                       '\\\\', '\\u0000', '\\\\', '\\u0000')).fetchone()
        if bad:raise ValueError('Formal queue contains damaged attribution metadata')
        where="json_extract(body,'$.projectId')=? AND json_extract(body,'$.laneId') IN ("+','.join('?' for _ in lanes)+')';args=[PROJECT,*lanes]
        if actor:where+=" AND (json_extract(body,'$.workerId')=? OR json_extract(body,'$.ownerId')=?)";args.extend([actor,actor])
        rows=db.execute('SELECT id,body FROM taskflow_tasks WHERE '+where+' ORDER BY id LIMIT 101',args).fetchall()
        if len(rows)>100:raise ValueError('Formal task queue exceeds the100-task read bound')
        for raw in rows:
            t=json.loads(raw['body'])
            if t.get('taskId')!=raw['id'] or not isinstance(t.get('state'),str) or not isinstance(t.get('title'),str):raise ValueError('Formal task identity unavailable')
            assignment=None
            if t.get('assignmentId'):
                a=db.execute('SELECT task_id,body FROM taskflow_assignments WHERE id=?',(t['assignmentId'],)).fetchone()
                if a:
                    if a[0]!=t['taskId'] or not isinstance(a[1],str) or len(a[1])>1048576 or '\x00' in a[1]:raise ValueError('Formal assignment unavailable')
                    assignment=json.loads(a[1])
                    if not isinstance(assignment,dict) or assignment.get('taskId')!=t['taskId'] or assignment.get('id')!=t['assignmentId'] or not isinstance(t.get('workerId'),str) or assignment.get('workerId')!=t['workerId']:raise ValueError('Formal assignment binding unavailable')
            state='Recorded '+t['state'];fresh=False
            if t['state']=='RUNNING':
                a=assignment or {};heartbeat=a.get('heartbeatAt');evidence=a.get('executionEvidence');fresh=a.get('state')=='RUNNING' and isinstance(heartbeat,(int,float)) and 0<=time.time()-heartbeat<=30 and isinstance(evidence,dict) and bool(evidence) and bool(a.get('providerTurnId') or a.get('executionTransport')=='local-pid' and evidence.get('runnerPid'))
                state='Recorded RUNNING · fresh heartbeat' if fresh else 'STATUS UNKNOWN · recorded RUNNING'
            owner=names.get(t.get('workerId'),names.get(t.get('ownerId'),'Unassigned / unknown'))
            closed=t['state'] in ('DONE','CANCELLED')
            result.append({'id':'formal-'+t['taskId'],'title':t['title'],'kind':'Task Board task','state':state,'owner':owner,'origin':names.get(t.get('capturedBy'),'Unresolved origin'),'nextActor':None if closed else owner,'lifecycle':'closed' if closed else 'open','needsAttention':not closed and (t['state'] in ('BLOCKED','UNCERTAIN') or t['state']=='RUNNING' and not fresh),'awaitingReturn':not closed and t['state'] in ('QUEUED','READY','OFFERED','ACCEPTED','STARTING','RUNNING'),'summary':t.get('waitReason') or t.get('definitionOfDone') or t.get('request',''),'updatedAt':t.get('updatedAt') or t.get('createdAt'),'technicalId':t['taskId'],'transport':'Transport status separate; see original assignment record.','progressMeaning':'Authoritative recorded TaskFlow state; running additionally requires its existing30s execution-evidence heartbeat rule. No process is probed.','detail':{'task':t,'assignment':assignment,'executionFresh':fresh}})
    return result


def page(database, reader, board_database, registry, member, lanes, query):
    actor=None if member['view'] in ('owner','dispatch','library') else member['agentId']
    raw=RoleWork(database,reader,lanes=lanes,actor=actor,page_size=400).get({'project':[PROJECT],'lane':[LANE],'kind':['work']})
    groups=grouped(raw['records'],registry);formal_error=None
    try:tasks=formal(board_database,reader,lanes,actor,registry)
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):tasks=[];formal_error='Formal queue unavailable; typed work remains shown. No empty-queue conclusion is valid.'
    records=groups+tasks;records.sort(key=lambda r:(r.get('updatedAt') or 0,r['id']),reverse=True)
    coverage={'typedObligations':len(raw['records']),'typedGroups':len(groups),'formalTasks':None if formal_error else len(tasks),'typedCoverageComplete':raw['nextOffset'] is None,'formalError':formal_error,'legacyBoundary':'Legacy records whose project is inferred only from owner placement are not admitted. Exact project/lane TaskFlow contracts and typed project work are shown.'}
    if 'id' in query:
        rows=[r for r in records if r['id']==query['id']]
        if len(rows)!=1:raise ValueError('Task group unavailable in this project/task scope')
        return rows,None,coverage
    offset=int(query.get('offset','0'));selected=records[offset:offset+20]
    # History is detail-only; list rows retain counts and exact original identities.
    selected=[{k:v for k,v in r.items() if k not in ('history','detail','actions')} for r in selected]
    return selected,offset+20 if offset+20<len(records) else None,coverage
