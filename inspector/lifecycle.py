"""Append-only scope and receiving-owner records inside the existing Library store."""
import json,time,uuid
from urllib.parse import urlencode
from .library import connect,encoded,digest,now
from .context import resolve_scope,read_context
KINDS={'foundation':'Founding scope','revision':'Scope revision','disposition':'Evidence disposition','requirement':'Requirement version'}

def history(db,project,lane,team):
    rows=db.execute("""SELECT id,body,created FROM notes
      WHERE project=? AND lane=? AND kind IN ('Founding scope','Scope revision','Evidence disposition','Requirement version')
      AND id IN (SELECT id FROM record_scopes WHERE team=?) ORDER BY created,id""",(project,lane,team))
    out=[]
    for row in rows:
        try:value=json.loads(row['body'])
        except (ValueError,TypeError):continue
        if value.get('schemaVersion')=='ke.library.lifecycle.v1':
            out.append({**value,'id':row['id'],'createdAt':row['created']})
    return out

def write(library,item):
    project,lane,team=item.get('project'),item.get('lane',''),item.get('team','')
    state,_,_,_=resolve_scope(library,project,lane,team,writing=True)
    event=item.get('event')
    if event not in KINDS:raise ValueError('Choose foundation, revision, disposition or requirement')
    text=item.get('text')
    if not isinstance(text,str) or not text.strip() or len(text)>12000:raise ValueError('A scope or disposition explanation is required')
    pins=item.get('evidence')
    if not isinstance(pins,list) or not (0 if event=='requirement' else 1)<=len(pins)<=30:raise ValueError('Pin existing evidence records (at most 30)')
    key,sha=library.request_identity('lifecycle',item)
    if not key:raise ValueError('A stable requestId is required')
    with connect(library.dbpath) as db:
        db.execute('BEGIN IMMEDIATE')
        prior=library.prior_write(db,key,sha,'lifecycle',item)
        if prior:return prior
        state,_,_,_=resolve_scope(library,project,lane,team,writing=True)
        current=read_context(db,library,project,lane,team)
        if state['revision']!=current['workflowRevision']:raise ValueError('Assignments changed; reload before saving')
        records=history(db,project,lane,team)
        if type(item.get('version')) is not int or item['version']!=len(records):
            raise ValueError('Lifecycle changed; reload before saving')
        if item.get('contextFingerprint')!=current['fingerprint']:
            raise ValueError('Context changed; reload before saving')
        if event=='foundation' and any(r['event']=='foundation' for r in records):
            raise ValueError('Founding scope is preserved; record a revision instead')
        if event=='revision' and not any(r['event']=='foundation' for r in records):
            raise ValueError('Record the founding source before a scope revision')
        checked=[]
        for pin in pins:
            if not isinstance(pin,dict) or set(pin)!={'id','sha'}:raise ValueError('Evidence requires exact id and SHA-256')
            row=db.execute('SELECT * FROM documents WHERE id=?',(pin['id'],)).fetchone()
            if not row:raise ValueError('Evidence is missing')
            if not library.readable(row):raise ValueError('Learning evidence access or current provenance is unavailable')
            library.check_item_scope(db,row,project,lane,team)
            version=db.execute('SELECT 1 FROM versions WHERE id=? AND sha=?',(pin['id'],pin['sha'])).fetchone()
            if not version:raise ValueError('Evidence version is missing')
            # Historical founding decisions stay available; a receiving disposition
            # must be based on current source bytes and a current context.
            if (event=='disposition' or event=='requirement' and item.get('status')=='Reported complete') and (pin['sha']!=row['sha'] or library.source_state(row)):
                raise ValueError('Evidence changed; reopen before recording a disposition')
            checked.append(dict(pin))
        receiver=item.get('receivingOwner','')
        decision=item.get('decision','')
        if event=='disposition':
            seats=[p for p in state['placements'] if p['agentId']==receiver and
                   p['role'] in ('coordinator','writer') and
                   any(l['id']==p['laneId'] and l['projectId']==project for l in state['lanes']) and
                   (not lane or p['laneId']==lane)]
            if not seats:raise ValueError('Choose an exact receiving Owner or Build identity in this scope')
            if decision not in ('Accepted','Revise','Rejected','Deferred'):raise ValueError('Choose a receiving disposition')
        else:
            if receiver or decision:raise ValueError('Receiving dispositions must use a disposition record')
        if library.workflow.snapshot()['revision']!=state['revision']:raise ValueError('Assignments changed; reload before saving')
        actor=item.get('actor','local-operator')
        if not isinstance(actor,str) or len(actor)>300:raise ValueError('Invalid recording identity')
        record={'recordedBy':actor,'schemaVersion':'ke.library.lifecycle.v1','event':event,'version':len(records)+1,
                'text':text.strip(),'evidence':checked,'contextFingerprint':current['fingerprint'],
                'contextVersion':current['current']['version'],'workflowRevision':state['revision'],
                'receivingOwner':receiver,'decision':decision,
                'adoptionVerification':'recorded-receipt-not-independently-verified' if event=='disposition' else None}
        identifier=str(uuid.uuid4());stamp=now()
        if event=='requirement':
            title,acceptance=item.get('title',''),item.get('acceptance','')
            owner,status=item.get('owner',''),item.get('status','Captured')
            if not isinstance(title,str) or not title.strip() or len(title)>200:raise ValueError('Requirement title is required')
            if not isinstance(acceptance,str) or not acceptance.strip() or len(acceptance)>12000:raise ValueError('Acceptance criteria are required')
            if status not in ('Captured','Assigned','In progress','Awaiting review','Reported complete','Deferred','Rejected'):raise ValueError('Invalid requirement status')
            if owner and not any(p['agentId']==owner and any(l['id']==p['laneId'] and l['projectId']==project for l in state['lanes']) for p in state['placements']):raise ValueError('Requirement owner is not assigned in this project')
            if status=='Reported complete' and not checked:raise ValueError('Reported completion requires pinned evidence')
            requirement=item.get('requirementId') or identifier
            previous=[r for r in records if r['event']=='requirement' and r.get('requirementId')==requirement]
            if item.get('requirementId') and not previous:raise ValueError('Requirement belongs to a different scope or is missing')
            record.update(familyId=None,requirementId=requirement,requirementVersion=len(previous)+1,title=title.strip(),acceptance=acceptance.strip(),owner=owner,status=status,completionVerified=False)
        raw=encoded(record)
        title=KINDS[event]+': '+text.strip()[:150]
        db.execute('INSERT INTO notes VALUES(?,?,?,?,?,?,?)',(identifier,project,lane,title,KINDS[event],raw,stamp))
        db.execute('INSERT INTO note_scopes VALUES(?,?)',(identifier,team))
        db.execute('INSERT INTO record_scopes VALUES(?,?,?,?)',(identifier,project,lane,team))
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,project,lane,'',title,KINDS[event],digest(raw),raw,time.time(),len(raw),stamp,'Recorded','Pinned project-lifetime record'))
        db.execute('INSERT INTO versions VALUES(?,?,?,?)',(identifier,digest(raw),raw,stamp))
        db.execute('INSERT INTO search VALUES(?,?,?)',(identifier,title,raw))
        result={'ok':True,'id':identifier,'version':record['version']}
        library.record_write(db,key,sha,result)
        return result

def snapshot(library,project,lane='',team=''):
    state,p,l,t=resolve_scope(library,project,lane,team)
    with connect(library.dbpath) as db:
        current=read_context(db,library,project,lane,team)
        records=history(db,project,lane,team)
        history_count=len(records)
        records=[r for r in records if library.readable_ids(r['evidence'])]
        for record in records:
            statuses=[]
            for pin in record['evidence']:
                row=db.execute('SELECT * FROM documents WHERE id=?',(pin['id'],)).fetchone()
                version=db.execute('SELECT 1 FROM versions WHERE id=? AND sha=?',(pin['id'],pin['sha'])).fetchone()
                statuses.append('missing' if not row or not version else 'changed' if row['sha']!=pin['sha'] or library.source_state(row) else 'current')
            record['evidenceState']='missing' if 'missing' in statuses else 'changed' if 'changed' in statuses else 'current'
            record['contextCurrent']=record['contextFingerprint']==current['fingerprint']
        versions=[dict(r) for r in db.execute('SELECT version,body,updated FROM context_versions WHERE project=? AND lane=? AND team=? ORDER BY version',(project,lane,team))]
        for version in versions:version['body']=json.loads(version['body'])
        areas=[]
        for area in state['lanes']:
            if area['projectId']!=project:continue
            lid=area['id']
            counts={r['kind']:r['n'] for r in db.execute('SELECT kind,count(*) n FROM documents WHERE project=? AND lane=? GROUP BY kind',(project,lid))}
            contexts=db.execute('SELECT count(*) FROM team_context WHERE project=? AND lane=?',(project,lid)).fetchone()[0]
            owner_ids=[x['agentId'] for x in state['placements'] if x['laneId']==lid and (x.get('teamId') or x['role'])=='coordinator']
            missing=[]
            if not owner_ids:missing.append('Owner not recorded')
            if not contexts:missing.append('Context not recorded')
            if not counts.get('Founding scope'):missing.append('Founding source not recorded')
            if not counts.get('Library review'):missing.append('Library review not recorded')
            if not counts.get('Evidence disposition'):missing.append('Receiving disposition not recorded')
            areas.append({'id':lid,'name':area['name'],'objective':area.get('objective',''),
                          'lifecycle':area.get('lifecycle') or 'active','mergedInto':area.get('mergedInto'),
                          'kind':area.get('kind','strategy'),'owners':owner_ids,'counts':counts,'contextCount':contexts,
                          'missing':missing,'teams':[x for x in state.get('teams',[]) if x['laneId']==lid],
                          'url':'/library?'+urlencode({'project':project,'lane':lid,'team':'','view':'workspace'})})
        receivers=[{'agentId':x['agentId'],'role':x['role'],'lane':x['laneId']}
                   for x in state['placements'] if x['role'] in ('coordinator','writer') and
                   any(a['id']==x['laneId'] for a in areas) and (not lane or x['laneId']==lane)]
        roots=[]
    return {'schemaVersion':'ke.library.workspace.v1','project':project,'lane':lane,'team':team,'requirementsSources':roots,
            'workflowRevision':state['revision'],'context':current,'contextVersions':versions,'history':records,
            'version':history_count,'areas':areas,'receivers':receivers,
            'foundingScope':next((r for r in records if r['event']=='foundation'),None),
            'adoptionVerified':False,'authority':'Records preserve evidence; receiving claims require independent verification.'}
