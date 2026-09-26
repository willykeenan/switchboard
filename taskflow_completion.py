"""Authoritative verified-completion feed; presentation/cursor persistence is client-owned.
No alert on historical imports, synthetic runtime, provider return or pending review.
This projection does not mutate, capture, approve, dispatch or start work.
"""
import hashlib,json

def feed(store,tasks,assignments):
 from taskflow import digest
 workspace=hashlib.sha256(str(store.root).encode()).hexdigest()
 items=[]
 with store.connect() as db:
  highwater=db.execute('SELECT coalesce(max(seq),0) FROM taskflow_events').fetchone()[0]
  for t in tasks:
   origins=[s for s in t.get('sourceRefs',[]) if s.get('kind')=='owner-conversation' and s.get('recordSha256') and s.get('providerThreadId')]
   origins += [s for s in t.get('sourceRefs',[]) if s.get('kind')=='scene-report' and s.get('id') and s.get('reporter','').startswith('human:') and any(f.get('path','').endswith('/'+s['id']+'/report.json') and f.get('sha256') for f in t.get('inputs',[]))]
   if not origins or not t.get('ownerId') or t['state']!='DONE' or not t.get('completion') or (t.get('libraryRetention') or {}).get('state')!='recorded':continue
   runs=[a for a in assignments if a['taskId']==t['taskId'] and a['state']=='RETURNED']
   def verified_run(a):
    if a.get('executionTransport')=='local-pid':
     return False
    return a.get('executionTransport')=='codex-app-server' and bool(a.get('providerTurnId'))
   if not runs or not all(verified_run(a) for a in runs):continue
   # Existing event schema stores kind inside the immutable JSON body.
   events=[(r['seq'],json.loads(r['body'])) for r in db.execute('SELECT seq,body FROM taskflow_events WHERE task_id=? ORDER BY seq',(t['taskId'],))]
   seq=next((seq for seq,e in reversed(events) if e['kind'] in ('reviewed-library-retained','verified-library-retained')),None)
   if seq is None:continue
   ident=digest({'workspace':workspace,'taskId':t['taskId'],'taskVersion':t['taskVersion'],'completion':t['completion']})
   items.append({'id':ident,'sequence':seq,'kind':'verified-task-completion','taskId':t['taskId'],'taskVersion':t['taskVersion'],
    'scope':{'workspaceId':workspace,'projectId':t['projectId'],'laneId':t['laneId'],'ownerId':t['ownerId']},
    'title':t['request'].splitlines()[0][:160],'message':'Task complete','result':{'taskId':t['taskId'],'approvalId':(t.get('approval') or {}).get('id'),'completion':t['completion'],'deliveredResult':t.get('deliveryResult'),'library':t['libraryRetention']},
    'navigation':{'action':'view-task-result','taskId':t['taskId']},'createsWork':False})
 return {'schemaVersion':'ke.task-completions.v1','workspaceId':workspace,'highWatermark':highwater,'items':items,
  'clientContract':{'initialLoad':'Save highWatermark quietly; retain history as unread without replaying old toasts','subsequentLoads':'Persist seen completion IDs and cursor per user/workspace/project/lane; batch missed items; never derive completion from animation','presentationOwner':'Systems world candidate','serverDeduplication':'Stable completion ID from persisted task identity and completion receipt'}}
