"""Read-only, bounded project viewers. No discovery, indexing or process probes."""
from pathlib import Path
from contextlib import closing
import datetime
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from .role_work import RoleWork, PROJECT, LANE, RECEIPT_ROOT

SECTIONS = ('tasks', 'work', 'documents', 'skills', 'receipts', 'pids')
LIMIT = 20
MAX_FILE = 1048576
PRIVATE_KEYS = re.compile(r'password|secret|credential|authorization|access.?token|refresh.?token|api.?key', re.I)

def safe_read(path, maximum=MAX_FILE):
    """Open all components without following links; refuse races and special files."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Unregistered path')
    folder = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder); folder = nxt
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        with os.fdopen(fd, 'rb') as f:
            before = os.fstat(f.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
                raise ValueError('Unavailable or oversized source')
            raw = f.read(maximum+1); after = os.fstat(f.fileno())
            if len(raw) != before.st_size or (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
                raise ValueError('Source changed while reading')
            return raw, hashlib.sha256(raw).hexdigest(), after.st_mtime
    finally:
        os.close(folder)

def clean(value, depth=0):
    if depth > 20: return '[nested content omitted]'
    if isinstance(value, dict):
        return {str(k): '[redacted]' if PRIVATE_KEYS.search(str(k)) else clean(v,depth+1) for k,v in value.items()}
    if isinstance(value, list): return [clean(x,depth+1) for x in value[:1000]]
    if isinstance(value, str):
        value = re.sub(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}', '[redacted]', value)
        value = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]{12,}', r'\1[redacted]', value)
        value = re.sub(r'(?im)((?:password|api[_-]?key|access[_-]?token|secret)\s*[=:]\s*)[^\s,;]+', r'\1[redacted]', value)
        return value[:100000]
    return value

def stamp(value):
    if isinstance(value,(int,float)):return value
    try:return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,AttributeError):return None

def title(value):
    # Presentation only; exact assignment and original body stay in details.
    value = re.sub(r'@[0-9a-f]{7,40}', '', str(value))
    value = re.sub(r'(?<!\w)[0-9a-f]{8}-[0-9a-f-]{27,}(?!\w)', '', value)
    value = re.sub(r'[-_:]+',' ',value).strip()
    return (value[:1].upper()+value[1:])[:150] or 'Project work'

class RolePanels:
    def __init__(self, database, reader, registry_path=None):
        self.database=Path(database); self.reader=reader
        self.registry_path=Path(registry_path) if registry_path else Path(__file__).with_name('role-registry.json')

    def registry(self):
        raw,_,_=safe_read(self.registry_path)
        r=json.loads(raw)
        members=r.get('members') or []
        if r.get('schema')!='ke.role-panel.registry.v1' or r.get('project')!=PROJECT or not members:
            raise ValueError('Viewer registration unavailable')
        if len({m['agentId'] for m in members})!=len(members) or len(r.get('resources',[]))>100:
            raise ValueError('Viewer registration damaged')
        r.setdefault('coverage','Registered role viewers for this project.')
        r.setdefault('resources',[])
        return r

    def membership(self,r,task):
        member=next((m for m in r['members'] if m['threadId']==task),None)
        if member is None:raise ValueError('Task is not registered for this project viewer')
        # Current saved enrollment, not folder association or URL assertion.
        with closing(self.reader(self.database,timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            row=db.execute('SELECT revision,body FROM workflow WHERE id=1').fetchone()
        if not row or not isinstance(row[1],str) or len(row[1])>1048576:raise ValueError('Enrollment unavailable')
        state=json.loads(row[1]);lanes={x['id'] for x in state.get('laneCatalog',[]) if x.get('projectId')==PROJECT and x.get('lifecycle','active')=='active'}
        seats=[x for x in state.get('placements',[]) if x.get('agentId')==member['agentId']]
        if state.get('enabled') is not True or len(seats)!=1 or seats[0].get('role')!=member['role'] or seats[0].get('laneId') not in lanes or seats[0].get('laneId')!=member['laneId']:
            raise ValueError('Task enrollment changed; viewer access unavailable')
        return member,tuple(sorted(set(r['lanes']) & lanes)),row[0]

    def publish(self,out,r,task):
        # Recheck admission at publication as well as before reading. This local
        # viewer is not an authenticated agent boundary or an OS access control.
        self.membership(r,task)
        return clean(out)

    def get(self,query):
        allowed={'project','task','section','offset','id'}
        if set(query)-allowed or any(not isinstance(v,list) or len(v)!=1 or not isinstance(v[0],str) for v in query.values()):
            raise ValueError('One value per allowed viewer field required')
        q={k:v[0] for k,v in query.items()}
        if q.get('project')!=PROJECT:raise ValueError('Exact registered project required')
        r=self.registry();member,lanes,revision=self.membership(r,q.get('task'))
        section=q.get('section',member['defaultSection'])
        if section not in SECTIONS:raise ValueError('Unknown viewer section')
        offset=q.get('offset','0')
        if not re.fullmatch(r'0|[1-9][0-9]{0,4}',offset) or ('id' in q and 'offset' in q):raise ValueError('Invalid bounded page or detail')
        out={'schema':'ke.role-panel.page.v1','project':PROJECT,'task':member['threadId'],'member':member,'section':section,'sampledAt':time.time(),'workflowRevision':revision,'coverage':r['coverage'],'requestedOffset':int(offset),'nextOffset':None,'records':[],'limitations':['Read-only local human view. No process starts, probes, signals, indexing or model calls.','Recorded acceptance is not a measurement of running work. Receipt claims are not independent installation proof.'],'members':[{'label':m['label'],'threadId':m['threadId'],'view':m['view']} for m in r['members']]}
        if section=='tasks':
            from .role_tasks import page
            rows,nxt,counts=page(self.database,self.reader,self.database.parent.parent/'board.sqlite3',r,member,lanes,q)
            out['records']=rows;out['nextOffset']=nxt;out['taskCoverage']=counts
            out['coverage']='Task groups from '+str(counts['typedObligations'])+' typed obligations, '+str(counts['typedGroups'])+' groups; formal Task Board tasks: '+str(counts['formalTasks'])+'. '+('All currently scoped typed obligations fit the400-record bound. ' if counts['typedCoverageComplete'] else 'Truncated at400 typed obligations; groups/history may be incomplete. ')+(counts['formalError'] or '')+' '+counts['legacyBoundary']+' Search and filters cover loaded pages.'
            if 'id' in q:out['record']=rows[0]
            return self.publish(out,r,member['threadId'])
        if section in ('work','receipts'):

            # Owner/Dispatch/Library see the bounded project queue. Specialists see
            # obligations involving their exact identity; all three project lanes included.
            actor=None if member['view'] in ('owner','dispatch','library') else member['agentId']
            work=RoleWork(self.database,self.reader,lanes=lanes,actor=actor)
            wq={'project':[PROJECT],'lane':[LANE],'kind':['receipts' if section=='receipts' else 'work']}
            if 'id' in q:wq['id']=[q['id']]
            else:wq['offset']=[offset]
            data=work.get(wq)
            out['records']=[self.work_record(x,r) for x in data['records']]
            out['nextOffset']=data['nextOffset'];out['coverage']='Current typed project work across the three registered project lanes;10 rows per page. '+('Only work involving this exact task. ' if actor else 'Project-wide queue. ')+'Search/filter counts cover loaded pages only.'
            if 'record' in data:
                row=self.work_record(data['record'],r);row['detail']=clean(data['record']);out['record']=row
                if data['record']['receipt']['verification']=='VERIFIED_RECORDED_HASH':
                    body=data['record']['receipt'].get('body',{})
                    row['facts']=self.facts(body)
                row['links']=r.get('recordLinks',{}).get(row['id'],[])
            return self.publish(out,r,member['threadId'])
        resources=[x for x in r['resources'] if x.get('project')==PROJECT and x.get('visibility')=='project' and x.get('section')==section]
        if 'id' in q:
            resources=[x for x in resources if x['id']==q['id']]
            if len(resources)!=1:raise ValueError('Resource unavailable in permitted project catalog')
        selected=resources[int(offset):int(offset)+LIMIT]
        if int(offset)+LIMIT<len(resources):out['nextOffset']=int(offset)+LIMIT
        out['records']=[self.resource(x,r,detail='id' in q) for x in selected]
        if 'id' in q:out['record']=out['records'][0]
        out['limitations']+=['Catalog registration is explicit and bounded. Missing documents or jobs are a coverage gap, not absence of project work. Procedure qualification is recorded; current executable eligibility is not inferred.']
        return self.publish(out,r,member['threadId'])

    def work_record(self,row,r):
        names={m['agentId']:m['label'] for m in r['members']}
        from .role_tasks import obligation_action
        action=obligation_action(row,names)
        return {**action,'id':row['id'],'title':title(row['assignmentId']),'state':row['state'],'kind':'Receipt' if row['returnedAt'] else 'Work obligation','owner':names.get(row['recipient'],'Unresolved project task'),'updatedAt':row['closedAt'] or row['returnedAt'] or row['acceptedAt'] or row['createdAt'],'dueAt':row['createdAt']+row['contract']['dueSeconds'],'technicalId':row['assignmentId'],'recordedHash':((row['receipt'].get('recorded') or {}).get('sha256')),'receiptVerification':row['receipt']['verification']}

    @staticmethod
    def facts(body):
        if not isinstance(body,dict):return {}
        keys=('verdict','status','source','findings','remainingGaps','nextAction','nextOwner','installed','installationCleared','sourcePackageAccepted','checks','tests','dependencies','questions','sources','producedArtifacts','readback','rollback','limits')
        return clean({k:body[k] for k in keys if k in body})

    def resource(self,x,r,detail=False):
        row={'id':x['id'],'title':x['label'],'kind':x['section'].title(),'state':'Unavailable','summary':x['basis'],'updatedAt':None,'owner':'Service','technicalId':x['id'],'links':x.get('links',[])}
        try:
            raw,sha,mtime=safe_read(x['path']);text=raw.decode('utf-8');row.update(state='Current' if sha==x['registeredSha256'] else 'Changed since registration',updatedAt=mtime,source={'path':x['path'],'registeredSha256':x['registeredSha256'],'observedSha256':sha,'modifiedAt':mtime},summary=text[:240] if x['section']=='documents' else x['basis'])
            if x['section']=='skills':
                recipe=json.loads(text);state_raw,_,_=safe_read(x['statePath']);state=json.loads(state_raw)
                row['state']='Recorded '+str(state.get('status','UNQUALIFIED'))
                if sha!=x['registeredSha256']:row['state']='STALE · recipe changed'
                row['summary']=x['basis']+' · '+recipe.get('id','')+' · '+recipe.get('skillId','')
                row['qualification']={'recordedStatus':state.get('status'),'qualifiedAt':state.get('qualification',{}).get('qualifiedAt'),'recipeHash':recipe.get('recipeHash'),'currentSourceRequalified':False,'meaning':'Recorded qualification only. Source/interpreter drift and execution authority must be checked by the existing runner before use. Viewing never runs it.'}
                if detail:
                    guide,_,_=safe_read(x['guidePath']);row['detail']={'procedure':recipe,'qualification':state,'guide':guide.decode('utf-8')}
                    # Only pins in the unchanged registered recipe, no discovery.
                    checked=[];total=0
                    pins=recipe.get('sourcePins',[])
                    if sha==x['registeredSha256'] and 0<len(pins)<=32:
                        for pin in pins:
                            try:
                                data,actual,_=safe_read(pin['path']);total+=len(data)
                                if total>8*MAX_FILE:raise ValueError('Pin byte bound')
                                checked.append({'path':pin['path'],'matches':actual==pin['sha256']})
                            except (OSError,ValueError,KeyError):checked.append({'path':pin.get('path'),'matches':False})
                        row['qualification']['sourcePinStatus']='MATCHED' if all(p['matches'] for p in checked) else 'STALE'
                        if not all(p['matches'] for p in checked):row['state']='STALE · source pins changed'
                    else:row['qualification']['sourcePinStatus']='UNAVAILABLE'
                    row['detail']['sourcePinChecks']=checked
            elif x['section']=='pids':
                if sha!=x['registeredSha256']:
                    row.update(state='Changed since registration',summary='Registered job changed. Its current fields and linked report are unavailable until the source owner updates the registration.')
                    return row
                job=json.loads(text);owner=job.get('owner') or ('codex:'+job['parentThreadId'] if job.get('parentThreadId') else None)
                if owner!=x['owner'] or owner not in {m['agentId'] for m in r['members']}:raise ValueError('Job owner is not registered in this project')
                row['owner']=next(m['label'] for m in r['members'] if m['agentId']==owner)
                recorded=str(job.get('state',job.get('status','unknown'))).lower();observed=stamp(job.get('updatedAt'));stale=observed is None or time.time()-observed>120
                row.update(state=('Stale · recorded '+recorded if stale and recorded in ('running','queued','pending') else 'Recorded '+recorded),updatedAt=observed,summary=job.get('displayCommand') or str(job.get('command','Command not recorded')),pid=job.get('pid') or job.get('executedPid'),pgid=job.get('pgid'),progress=job.get('progress'),etaSeconds=job.get('etaSeconds'),sampleAgeSeconds=round(time.time()-observed) if observed else None)
                if detail:
                    row['detail']=job
                    receipt=job.get('receiptPath')
                    if receipt:
                        # Linked reports must reside in this explicitly scoped evidence tree.
                        root=Path(receipt).resolve().parent
                        Path(receipt).resolve().relative_to(root)
                        data,digest,_=safe_read(receipt);report=json.loads(data)
                        if report.get('taskId')!=owner.partition(':')[2]:raise ValueError('Run receipt task binding does not match')
                        expected=x.get('runReceiptSha256')
                        if expected and digest!=expected:raise ValueError('Registered run receipt hash changed')
                        row['runReceipt']={'path':receipt,'observedSha256':digest,'body':report,'verification':'VERIFIED_REGISTERED_HASH' if expected else 'CURRENT_FILE_READ; no expected hash registered'}
            elif detail:row['detail']={'text':text}
        except (OSError,ValueError,TypeError,KeyError,UnicodeError):
            return {'id':x['id'],'title':x['label'],'kind':x['section'].title(),'state':'Unavailable','summary':'Registered source cannot be safely read or its project binding changed. Ask its source owner to restore or explicitly update registration; viewing cannot repair or index it.','updatedAt':None,'technicalId':x['id']}
        return row
