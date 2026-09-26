"""A local, versioned evidence catalog. Source documents are never modified."""
from pathlib import Path
from contextlib import contextmanager
import datetime,hashlib,json,os,re,sqlite3,subprocess,sys,threading,time,uuid
from .transcripts import safe_text
EXT={'.md','.txt','.json','.csv','.pdf'}
SKIP={'.git','node_modules','__pycache__','.venv','venv','.next','dist','build','target','sessions','archived_sessions','memory','memories'}
DENY={'agents.md','claude.md','skill.md','config.json','config.toml','package-lock.json','package.json','credentials.json','auth.json','secrets.json','token.json','.env'}
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def encoded(v):return json.dumps(v,ensure_ascii=False,sort_keys=True)
def digest(s):return hashlib.sha256(s.encode()).hexdigest()

@contextmanager
def connect(path):
    db=sqlite3.connect(path,timeout=15);db.row_factory=sqlite3.Row
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'documents','versions','search','notes','reviews'} <= tables:
        db.executescript('''PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,project TEXT,lane TEXT,path TEXT,title TEXT,kind TEXT,sha TEXT,body TEXT,modified REAL,size INTEGER,indexed TEXT,state TEXT,basis TEXT);
    CREATE TABLE IF NOT EXISTS versions(id TEXT,sha TEXT,body TEXT,captured TEXT,PRIMARY KEY(id,sha));
    CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED,title,body);
    CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,project TEXT,lane TEXT,title TEXT,kind TEXT,body TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,project TEXT,lane TEXT,query TEXT,decision TEXT,reason TEXT,evidence TEXT,created TEXT);
    ''')
    try:yield db;db.commit()
    except BaseException:db.rollback();raise
    finally:db.close()

class Library:
    def __init__(self,root,workflow,directory=None):
        self.root=Path(root);self.workflow=workflow;self.directory=Path(directory) if directory else self.root/'runtime/library';self.directory.mkdir(parents=True,exist_ok=True,mode=0o700);self.dbpath=self.directory/'library.sqlite3';self.lock=threading.RLock();self.last_start=0
        with connect(self.dbpath) as db:
            from .context import schema
            schema(db)
    def for_actor(self,actor,store=None,task_db=None):
        import copy
        scoped=copy.copy(self);scoped.reader=actor
        scoped.task_store=store if store is not None else getattr(self,'task_store',None)
        scoped.task_db=task_db if task_db is not None else getattr(self,'task_db',None)
        return scoped
    def readable(self,row,seen=None):
        """Unscoped UI access never implies permission to restricted learning."""
        from taskflow_learning_library import is_proposal,authorize_document
        from workspace import Conflict
        seen=set() if seen is None else set(seen)
        if row['id'] in seen or len(seen)>=30:return False
        seen.add(row['id'])
        try:
            if is_proposal(self.root,row):
                actor=getattr(self,'reader',None)
                if not actor:return False
                store=getattr(self,'task_store',None)
                if store is None:
                    from taskflow import TaskFlow
                    store=TaskFlow(self.root,self.workflow)
                active=getattr(self,'task_db',None)
                if active is not None:authorize_document(store,active,row,actor)
                else:
                    with store.connect() as db:
                        db.execute('BEGIN');authorize_document(store,db,row,actor)
            # A stored review/lifecycle reference must not launder restricted
            # evidence into a different ordinary Library record.
            try:body=json.loads(row['body'])
            except (ValueError,TypeError):body=None
            if isinstance(body,dict) and (row['kind']=='Library review' or body.get('schemaVersion')=='ke.library.lifecycle.v1'):
                return self.readable_ids(body.get('evidence',[]),seen)
            return True
        except (Conflict,ValueError,OSError,KeyError,TypeError,sqlite3.Error):return False
    def readable_ids(self,pins,seen=None):
        with connect(self.dbpath) as db:
            for p in pins:
                ident=p.get('id') if isinstance(p,dict) else p
                row=db.execute('SELECT * FROM documents WHERE id=?',(ident,)).fetchone()
                if row is None or not self.readable(row,seen):return False
        return True
    def sources(self):
        state=self.workflow.snapshot();lane_map={l['id']:l for l in state['lanes']};seats={p['agentId']:p['laneId'] for p in state['placements']};sources={};tasks=[]
        def add(path,lane,basis):
            if not path or lane not in lane_map:return
            p=Path(path)
            if not p.is_absolute() or p==Path.home() or len(p.parts)<5 or p.is_symlink():return
            if any(x in SKIP for x in p.parts) or p.name.lower() in DENY:return
            project=lane_map[lane]['projectId'];key=(project,lane,str(p));sources[key]={'path':str(p),'project':project,'lane':lane,'basis':basis}
        dbpath=self.root/'board.sqlite3'
        if dbpath.exists():
            with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True,timeout=5) as db:
                db.row_factory=sqlite3.Row
                for r in db.execute('SELECT task_id,owner,objective,status,next_action,blocker,scope_json,criteria_json,updated_at FROM tasks'):
                    lane=seats.get(r['owner'])
                    if not lane:continue
                    tasks.append({'id':'task:'+r['task_id'],'project':lane_map[lane]['projectId'],'lane':lane,'title':r['objective'],'kind':'Work record','body':encoded({k:r[k] for k in ('task_id','owner','objective','status','next_action','blocker','updated_at')}),'basis':'Registered work record'})
                    for path in json.loads(r['scope_json']):add(path,lane,'Registered task '+r['task_id'])
                    for c in json.loads(r['criteria_json']):add(c.get('path'),lane,'Evidence linked by '+r['task_id'])
        registry=self.root/'runtime/current-context/registry.json'
        if registry.exists():
            for c in json.loads(registry.read_text()).get('contexts',{}).values():
                lane=seats.get(c['owner']);add(c['manifestPath'],lane,'Registered current context '+c['contextId'])
                try:
                    manifest=json.loads(Path(c['manifestPath']).read_text())
                    for component in manifest.get('components',[]):
                        for evidence in component.get('evidence',[]):add(evidence.get('path'),lane,'Context evidence '+c['contextId'])
                except (OSError,ValueError):pass
        return {'sources':list(sources.values()),'tasks':tasks,'projects':state['projects'],'lanes':state['lanes'],'workflowRevision':state['revision']}
    def status(self):
        try:s=json.loads((self.directory/'index-status.json').read_text())
        except (OSError,ValueError):return {'status':'not-indexed'}
        if s.get('status')=='running':
            import fcntl
            try:
                lock_path=self.directory/'index.lock'
                if lock_path.is_symlink():raise OSError('Index lock is not a regular lock path')
                with lock_path.open('rb') as lock:
                    try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        return {**s,'livenessBasis':'The index writer lock is held; PID fields are recorded observations.'}
                    else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
            except FileNotFoundError:pass
            except OSError:
                return {**s,'status':'unavailable','livenessBasis':'Cannot inspect the index writer lock.'}
            s.update(status='interrupted',finishedAt=now(),historicalPid=s.get('pid'),
                     historicalPgid=s.get('pgid'),historicalWorkers=s.get('workers',[]),
                     pid=None,pgid=None,workers=[],livenessBasis='No writer holds index.lock; the recorded run is interrupted.')
        return s
    def refresh(self,force=False):
        with self.lock:
            state=self.status()
            if state.get('status') in ('running','unavailable') or (not force and time.time()-self.last_start<60):return state
            spec=self.sources();(self.directory/'index-spec.json').write_text(encoded(spec));self.last_start=time.time()
            log=(self.directory/'index.log').open('ab')
            try:subprocess.Popen([sys.executable,'-B','-m','inspector.index_job','--root',str(self.root),'--directory',str(self.directory)],cwd=Path(__file__).parent.parent,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
            finally:log.close()
            return {'status':'starting'}
    def query(self,project,lane='',q='',kind='',offset=0,team=''):
        from .context import resolve_scope
        resolve_scope(self,project,lane,team)
        if not isinstance(q,str) or len(q)>300 or type(offset) is not int or offset<0 or offset>100000:
            raise ValueError('Invalid library search')
        # Opening or searching is observational; indexing requires explicit refresh.
        where=['d.project=?'];args=[project]
        if lane:where.append('d.lane=?');args.append(lane)
        if team:
            where.append("(s.id IS NULL OR s.team='' OR s.team=?)");args.append(team)
        scope_clause=' AND '.join(where);scope_args=list(args)
        if kind:where.append('d.kind=?');args.append(kind)
        if q.strip():
            tokens=re.findall(r'\w+',q,flags=re.UNICODE)[:16]
            if tokens:where.append('d.id IN (SELECT id FROM search WHERE search MATCH ?)');args.append(' AND '.join('"'+x+'"' for x in tokens))
        clause=' AND '.join(where)
        tables='documents d LEFT JOIN record_scopes s ON s.id=d.id'
        with connect(self.dbpath) as db:
            total=db.execute('SELECT count(*) FROM '+tables+' WHERE '+clause,args).fetchone()[0]
            rows=[dict(r) for r in db.execute('SELECT d.*,s.team AS team,substr(d.body,1,260) AS excerpt FROM '+tables+' WHERE '+clause+' ORDER BY d.modified DESC,d.id LIMIT 60 OFFSET ?',[*args,offset])]
            counts=[dict(r) for r in db.execute('SELECT d.lane,d.kind,count(*) AS count FROM '+tables+' WHERE '+scope_clause+' GROUP BY d.lane,d.kind',scope_args)]
            review_count=db.execute("SELECT count(*) FROM "+tables+" WHERE "+scope_clause+" AND d.kind='Library review'",scope_args).fetchone()[0]
        from taskflow_learning_library import display_state
        visible=[r for r in rows if self.readable(r)]
        hidden=len(visible)!=len(rows);rows=visible
        next_offset=offset+60 if offset+60<total else None
        if hidden or getattr(self,'reader',None):
            total=len(rows);counts=[];review_count=sum(r['kind']=='Library review' for r in rows)
        for r in rows:
            state=display_state(self.root,r['id'],r['body'],time.time())
            if state:r['state']=state
            r.pop('body',None)
        spec=self.sources()
        return {'items':rows,'total':total,'totalKind':'authorized-page' if hidden or getattr(self,'reader',None) else 'matching-records','nextOffset':next_offset,'counts':counts,'reviewCount':review_count,'status':self.status(),'sources':[s for s in spec['sources'] if s['project']==project and (not lane or s['lane']==lane)],'projects':spec['projects'],'lanes':[l for l in spec['lanes'] if l['projectId']==project]}

    @staticmethod
    def source_state(row):
        if not row['path']:return None
        try:
            path=Path(row['path'])
            if path.is_symlink():return 'Source changed'
            before=path.stat()
            if before.st_mtime!=row['modified'] or before.st_size!=row['size']:return 'Source changed'
            if not row['sha'] or before.st_size>2*1024*1024:return 'Source version unavailable'
            sha=hashlib.sha256()
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as source:
                import stat
                opened=os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_ino!=before.st_ino:return 'Source changed'
                raw=source.read(2*1024*1024+1)
                if len(raw)>2*1024*1024:return 'Source changed'
                sha.update(raw)
            after=path.stat()
            if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns) or sha.hexdigest()!=row['sha']:
                return 'Source changed'
        except OSError:return 'Source missing'
        return None

    @staticmethod
    def check_item_scope(db,row,project,lane,team):
        if row['project']!=project or (lane and row['lane'] not in ('',lane)):
            raise ValueError('Library item belongs to a different project or workstream')
        scope=db.execute('SELECT team FROM record_scopes WHERE id=?',(row['id'],)).fetchone()
        if team and scope and scope['team'] not in ('',team):
            raise ValueError('Library item belongs to a different team')

    def item(self,identifier,version='',project=None,lane='',team='',*,_trusted_learning=False):
        from .context import resolve_scope
        if project is not None:resolve_scope(self,project,lane,team)
        with connect(self.dbpath) as db:
            row=db.execute('SELECT * FROM documents WHERE id=?',(identifier,)).fetchone()
            if not row:raise ValueError('Library item not found')
            if project is not None:self.check_item_scope(db,row,project,lane,team)
            out=dict(row)
            scope=db.execute('SELECT team FROM record_scopes WHERE id=?',(identifier,)).fetchone()
            out['team']=scope['team'] if scope else ''
            out['versions']=[dict(r) for r in db.execute('SELECT sha,captured FROM versions WHERE id=? ORDER BY captured DESC',(identifier,))]
            if version:
                v=db.execute('SELECT body,captured FROM versions WHERE id=? AND sha=?',(identifier,version)).fetchone()
                if not v:raise ValueError('Version not found')
                out.update(body=v['body'],sha=version,state='Historical version')
            else:
                state=self.source_state(out)
                if state:out['state']=state
        from taskflow_learning_library import display_state
        learning_state=display_state(self.root,identifier,out['body'],time.time())
        if learning_state:out['state']=learning_state
        if not _trusted_learning and not self.readable(out):
            out.update(body='Restricted learning evidence. A current permitted reader is required.',
                       title='Restricted learning evidence',redacted=True)
        out['citation']=f"{out['title']}\n{out['path'] or out['id']}\nSHA-256: {out['sha']}\nIndexed: {out['indexed']}"
        return out

    @staticmethod
    def request_identity(operation,item):
        key=item.get('requestId','')
        if not isinstance(key,str) or len(key)>128:raise ValueError('Invalid request key')
        payload={k:v for k,v in item.items() if k!='requestId'}
        return key,digest(encoded({'operation':operation,'payload':payload}))

    @staticmethod
    def prior_write(db,key,payload_sha,operation,item):
        if not key:return None
        prior=db.execute('SELECT result,payload_sha FROM write_keys WHERE key=?',(key,)).fetchone()
        if not prior:return None
        if prior['payload_sha']!=payload_sha:
            # Recognize pre-migration retries only when both the original payload
            # and the recorded operation match. Old keys never gain new authority.
            legacy=digest(json.dumps(item,ensure_ascii=False))
            result=json.loads(prior['result'])
            row=db.execute('SELECT kind FROM documents WHERE id=?',(result.get('id'),)).fetchone()
            recorded='review' if row and row['kind']=='Library review' else 'note' if row else None
            if prior['payload_sha']!=legacy or operation!=recorded:
                raise ValueError('Request key already belongs to a different change')
        return json.loads(prior['result'])

    @staticmethod
    def record_write(db,key,payload_sha,result):
        if key:db.execute('INSERT INTO write_keys VALUES(?,?,?)',(key,encoded(result),payload_sha))

    def write(self,operation,item,*,guard=None):
        from .context import resolve_scope,read_context
        project=item.get('project');lane=item.get('lane','');team=item.get('team','researcher' if lane else '')
        resolve_scope(self,project,lane,team,writing=True)
        if operation not in ('note','review'):raise ValueError('Unknown library operation')
        request_key,payload_sha=self.request_identity(operation,item)
        identifier=str(uuid.uuid4());created=now()
        with connect(self.dbpath) as db:
            # Serialize the retry lookup and mutation in one transaction, including
            # context-version and evidence checks; parallel retries insert once.
            db.execute('BEGIN IMMEDIATE')
            prior=self.prior_write(db,request_key,payload_sha,operation,item)
            if prior:
                if guard:guard()
                return prior
            if operation=='note':
                title=str(item.get('title','')).strip();body=str(item.get('body','')).strip();kind=item.get('kind','Research note')
                if not title or len(title)>200 or not body or len(body)>100000 or kind not in ('Research note','Decision','Finding'):raise ValueError('Title and note text required')
                body=safe_text(body)
                db.execute('INSERT INTO notes VALUES(?,?,?,?,?,?,?)',(identifier,project,lane,title,kind,body,created))
                db.execute('INSERT INTO note_scopes VALUES(?,?)',(identifier,team))
            else:
                current=read_context(db,self,project,lane,team)
                if item.get('contextFingerprint')!=current['fingerprint']:
                    raise ValueError('Library context changed. Read current context before recording the review.')
                decision=item.get('decision');reason=str(item.get('reason','')).strip();query=str(item.get('query',''))
                if decision not in ('Reuse','Extend','Replicate','New research') or not reason or len(reason)>4000 or len(query)>300:raise ValueError('Review decision and reason required')
                items=item.get('items',[])
                if not isinstance(items,list) or len(items)>100:raise ValueError('Review at most 100 opened sources')
                evidence=[];seen=set()
                for entry in items:
                    key=entry.get('id') if isinstance(entry,dict) else entry
                    pinned=entry.get('sha') if isinstance(entry,dict) else None
                    if not isinstance(key,str) or (isinstance(entry,dict) and not isinstance(pinned,str)):
                        raise ValueError('Opened source requires an identifier and SHA-256 version')
                    row=db.execute('SELECT * FROM documents WHERE id=?',(key,)).fetchone()
                    if not row:raise ValueError('An opened source is no longer in the library')
                    if not self.readable(row):raise ValueError('Learning evidence access or current provenance is unavailable')
                    self.check_item_scope(db,row,project,lane,team)
                    if pinned is not None and pinned!=row['sha']:
                        raise ValueError('An opened source version changed. Reopen and review its latest version.')
                    if self.source_state(row):
                        raise ValueError('An opened source changed or is missing. Refresh and review its latest version.')
                    if key not in seen:evidence.append({'id':row['id'],'sha':row['sha']});seen.add(key)
                db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',(identifier,project,lane,query,decision,safe_text(reason),encoded(evidence),created))
                db.execute('INSERT INTO review_scopes VALUES(?,?,?)',(identifier,team,current['fingerprint']))
                title=decision+': '+(query or 'Library review');kind='Library review'
                body=encoded({'query':query,'decision':decision,'reason':safe_text(reason),'evidence':evidence,'team':team})
            sha=digest(body)
            db.execute('INSERT INTO versions VALUES(?,?,?,?)',(identifier,sha,body,created))
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,project,lane,'',title,kind,sha,body,time.time(),len(body),created,'Recorded','Saved in the project library'))
            db.execute('INSERT INTO record_scopes VALUES(?,?,?,?)',(identifier,project,lane,team))
            db.execute('INSERT INTO search VALUES(?,?,?)',(identifier,title,body))
            result={'ok':True,'id':identifier}
            self.record_write(db,request_key,payload_sha,result)
            if guard:guard()
            return result
