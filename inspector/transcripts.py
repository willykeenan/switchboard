"""Exact-session, read-only public conversation records with byte pagination."""
from pathlib import Path
import hashlib,json,os,re,sqlite3,stat,time,threading
from .presentation import presentation
from .attachments import AttachmentTokens, decode_image, MAX_RECORD
MAX_READ=2*1024*1024

def safe_text(value,limit=100000):
    if not isinstance(value,str):value=json.dumps(value,ensure_ascii=False,indent=2)
    value=re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*','Bearer [redacted]',value)
    value=re.sub(r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|authorization|password|secret)["\']?\s*[:=]\s*["\']?)([^\s"\',}]+)',r'\1[redacted]',value)
    value=re.sub(r'\b(?:sk-[A-Za-z0-9_-]{18,}|gh[pousr]_[A-Za-z0-9_]{20,})\b','[redacted]',value)
    return value[:limit]+('\n[Long entry shortened in this viewer.]' if len(value)>limit else '')

def text_parts(content):
    if isinstance(content,str):return content
    if not isinstance(content,list):return ''
    return '\n'.join(str(x.get('text','')) if x.get('type') in ('text','input_text','output_text') else '[Image attachment]' if x.get('type') in ('image','input_image') else '' for x in content if isinstance(x,dict)).strip()

def parse_event(event,provider):
    if not isinstance(event,dict) or provider not in ('codex','claude'):return []
    if event.get('channel') in ('analysis','summary') or event.get('role') in ('system','developer'):return []
    stamp=event.get('timestamp');out=[]
    def add(role,text,label='',content=None,tool_kind='',call_id='',failed=False):
        view = presentation(content,role) if content is not None else {}
        if text or view.get('_attachments'):
            if 'displayText' in view: view['displayText']=safe_text(view['displayText'])
            if view.get('sources'): view['sources']={k:safe_text(v) for k,v in view['sources'].items()}
            out.append({'role':role,'text':safe_text(text),'label':label,'timestamp':stamp,'toolKind':tool_kind,'callId':call_id,'failed':failed,**view})
    if provider=='claude':
        role=event.get('type');m=event.get('message') or {}
        if role not in ('user','assistant') or event.get('isMeta') or not isinstance(m,dict):return []
        if m.get('role',role)!=role or m.get('channel') in ('analysis','summary'):return []
        content=m.get('content',[])
        add(role,text_parts(content),content=content)
        for x in content if isinstance(content,list) else []:
            if not isinstance(x,dict):continue
            if x.get('type')=='tool_use':add('tool',x.get('input',{}),x.get('name','Tool call'),tool_kind='call',call_id=x.get('id',''))
            elif x.get('type')=='tool_result':add('tool',text_parts(x.get('content')), 'Tool result',tool_kind='result',call_id=x.get('tool_use_id',''),failed=bool(x.get('is_error')))
        return out
    if event.get('type')!='response_item':return []
    p=event.get('payload') or {}
    if not isinstance(p,dict) or p.get('channel') in ('analysis','summary') or p.get('type')=='reasoning' or p.get('role') in ('system','developer'):return []
    typ=p.get('type')
    metadata=p.get('internal_chat_message_metadata_passthrough') or {}
    kinds=metadata.get('content_item_kinds',[]) if isinstance(metadata,dict) else []
    if typ=='message' and p.get('role')=='user' and kinds and all(kind=='goal.internal_context' for kind in kinds):
        content=p.get('content',[]);text=text_parts(content)
        if isinstance(content,list) and all(isinstance(part,dict) and part.get('type') in ('text','input_text') for part in content) and re.fullmatch(r'<codex_internal_context source="goal">[\s\S]*</codex_internal_context>\s*',text):
            objective=re.search(r'<objective>\s*([\s\S]*?)\s*</objective>',text)
            public='Automatic goal continuation recorded.'+(('\n\nRecorded goal: '+objective[1]) if objective else '')
            add('activity',public,'Automatic continuation')
            out[-1]['sourceKind']='goal.internal_context';out[-1]['providerRecordId']=p.get('id');out[-1]['sourceNotice']='Generated provider control record. Internal operating instructions are excluded; the original record remains in the provider log.'
            return out
    if typ=='message' and p.get('role') in ('user','assistant'):add(p['role'],text_parts(p.get('content')),content=p.get('content'))
    elif typ in ('function_call','custom_tool_call'):add('tool',p.get('arguments',p.get('input','')),p.get('name','Tool call'),tool_kind='call',call_id=p.get('call_id',''))
    elif typ in ('function_call_output','custom_tool_call_output'):add('tool',p.get('output',''),'Tool result',tool_kind='result',call_id=p.get('call_id',''))
    return out

def provider_identity(session):
    """Use the catalog's exact mapping, including managed aliases. Never a title/PID guess."""
    provider=session.get('provider')
    if provider not in ('codex','claude'):return provider,None
    endpoint=session.get('endpoint')
    if session.get('managed'):
        # workflow.catalog maps managed.id -> providerThreadId in endpoint. Before
        # first launch it deliberately uses managed.id, which is not a provider log.
        if not endpoint or endpoint==session['agent_id']:return provider,None
    elif not endpoint:
        prefix,separator,endpoint=session['agent_id'].partition(':')
        if not separator or prefix!=provider:return provider,None
    if not isinstance(endpoint,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,179}',endpoint):return provider,None
    return provider,endpoint

def trusted_open(path,roots):
    """Walk the ORIGINAL lexical absolute path; never resolve away symlinks."""
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Transcript source is outside the registered provider logs')
    root=next((Path(r) for r in roots if Path(r).is_absolute() and '..' not in Path(r).parts and path.is_relative_to(Path(r))),None)
    if root is None or path==root:raise ValueError('Transcript source is outside the registered provider logs')
    # Walk from the filesystem anchor as well: a symlink in the trusted root's
    # own parents is not permission to canonicalize an unsafe supplied path.
    fd=os.open(path.anchor,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    opened=[];prefix=Path(path.anchor);leaf=None
    try:
        for component in path.parts[1:-1]:
            child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child;prefix=prefix/component
            st=os.fstat(fd);opened.append((prefix,st.st_dev,st.st_ino))
        leaf=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        st=os.fstat(leaf);opened.append((path,st.st_dev,st.st_ino))
        for name,device,inode in opened:
            visible=os.stat(name,follow_symlinks=False)
            if stat.S_ISLNK(visible.st_mode) or (visible.st_dev,visible.st_ino)!=(device,inode):
                raise ValueError('Transcript source changed during protected open')
        result=leaf;leaf=None;return result
    finally:
        if leaf is not None:os.close(leaf)
        os.close(fd)

class Transcripts:
    def __init__(self,workflow):self.workflow=workflow;self.attachments=AttachmentTokens();self.readers={};self.reader_lock=threading.RLock()
    def roots(self,provider):
        if provider=='codex':return [self.workflow.codex_home/'sessions',self.workflow.codex_home/'archived_sessions']
        if provider=='claude':return [self.workflow.codex_home.parent/'.claude/projects']
        return []
    def resolve(self,agent):
        sessions=self.workflow.catalog()['sessions'];s=next((x for x in sessions if x['agent_id']==agent),None)
        if not s:raise ValueError('Unknown agent session')
        provider,endpoint=provider_identity(s)
        path=None
        if endpoint and provider=='codex':
            dbpath=self.workflow.codex_home/'state_5.sqlite'
            if dbpath.exists():
                with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True,timeout=3) as db:
                    row=db.execute('SELECT rollout_path FROM threads WHERE id=?',(endpoint,)).fetchone()
                if row and row[0]:path=Path(row[0])
        elif endpoint and provider=='claude':path=self.workflow.claude_paths().get(endpoint)
        if path:
            path=Path(path)
            if not path.is_absolute() or path.is_symlink() or not any(path.resolve().is_relative_to(x.resolve()) for x in self.roots(provider)):
                raise ValueError('Transcript source is outside the registered provider logs')
        return s,path
    def page(self,agent,before=0,limit=100,reader='',initialized=False):
        # Each open reader validates only the byte ranges it has actually displayed.
        # This catches truncation and in-place rewrites without hashing entire archives.
        with self.reader_lock:return self._page(agent,before,limit,reader,initialized)

    def user_record(self,agent,entry):
        """Recheck one displayed user record for scoped owner intake. Read-only;
        log appends do not change this record's digest or stable source locator.
        """
        session,path=self.resolve(agent)
        if session.get('provider')!='codex' or not path or entry.get('role')!='user':raise ValueError('An observed original Codex user record is required')
        with os.fdopen(trusted_open(path,self.roots('codex')),'rb') as f:
            st=os.fstat(f.fileno());offset=entry['position'];f.seek(offset);line=f.readline(MAX_RECORD+1)
        if not line.endswith(b'\n') or len(line)>MAX_RECORD:raise ValueError('Complete bounded provider record required')
        event=json.loads(line);parsed=parse_event(event,'codex');n=entry.get('sequence',0)
        expected=hashlib.sha256((str(st.st_ino)+':'+str(offset)+':'+str(n)).encode()).hexdigest()[:24]
        if expected!=entry['id'] or n>=len(parsed) or parsed[n].get('role')!='user' or parsed[n]['text']!=entry['text']:raise ValueError('Provider user record changed since observation')
        payload=event.get('payload',{});body=text_parts(payload.get('content'))
        if payload.get('type')!='message' or payload.get('role')!='user' or not body or len(body)>64000:raise ValueError('Exact complete user request exceeds intake bounds or is not a user message')
        return {'text':body,'recordSha256':hashlib.sha256(line).hexdigest(),'providerRecordId':payload.get('id') or entry['id']}
    def _page(self,agent,before=0,limit=100,reader='',initialized=False):
        if not isinstance(agent,str) or len(agent)>180 or not isinstance(limit,int) or not 1<=limit<=100 or not isinstance(before,int) or before<0:raise ValueError('Invalid transcript request')
        session,path=self.resolve(agent)
        provider,endpoint=provider_identity(session)
        result={'agentId':agent,'sourceAgentId':provider+':'+endpoint if endpoint else None,'title':session['title'],'provider':session.get('provider'),'entries':[],'nextBefore':None,'readOnly':True,'available':False,'pollSeconds':3,'viewVersion':hashlib.sha256(self.attachments.secret).hexdigest()[:16],'notice':'Messages and tool records. Private reasoning and internal instructions are excluded.'}
        if not path or not path.exists():return {**result,'notice':'No readable transcript has been recorded for this session yet.'}
        try:fd=trusted_open(path,self.roots(provider))
        except FileNotFoundError:return {**result,'notice':'No readable transcript has been recorded for this session yet.'}
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):raise ValueError('Transcript must be a regular file')
            result['resetRequired']=self.validate_reader(f,st,agent,reader,initialized) if reader else False
            end=min(before or st.st_size,st.st_size);start=max(0,end-MAX_READ);f.seek(start);raw=f.read(end-start)
            while start and b'\n' not in raw[:-1] and end-start<MAX_RECORD:
                start=max(0,end-min(MAX_RECORD,(end-start)*2));f.seek(start);raw=f.read(end-start)
        if start:
            cut=raw.find(b'\n')
            if cut<0:return {**result,'available':True,'nextBefore':start,'notice':'A large record was skipped; load earlier entries.'}
            start+=cut+1;raw=raw[cut+1:]
        entries=[];offset=start
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b'\n'):break
            try:
                for n,entry in enumerate(parse_event(json.loads(line),session.get('provider'))):
                    entry['id']=hashlib.sha256((str(st.st_ino)+':'+str(offset)+':'+str(n)).encode()).hexdigest()[:24];entry['_offset']=offset;entry['position']=offset;entry['sequence']=n
                    specs=entry.pop('_attachments',[])
                    entry['attachments']=[self.attachments.describe(spec,{'agent':agent,'source':provider+':'+endpoint,'inode':st.st_ino,'offset':offset,'length':len(line),'record':hashlib.sha256(line).hexdigest(),'entry':n,'attachment':i}) for i,spec in enumerate(specs)]
                    entries.append(entry)
            except (ValueError,TypeError,KeyError):pass
            offset+=len(line)
        index=max(0,len(entries)-limit)
        while index and entries[index-1]['_offset']==entries[index]['_offset']:index-=1
        page=entries[index:];cursor=page[0]['_offset'] if index else start
        if reader:
            with os.fdopen(trusted_open(path,self.roots(provider)),'rb') as verify:
                result['resetRequired']=self.remember_range(verify,st,agent,reader,cursor,offset) or result['resetRequired']
        result['coverage']={'from':cursor,'through':offset}
        for entry in page:entry.pop('_offset',None)
        return {**result,'available':True,'entries':page,'nextBefore':cursor or None,'sourceVersion':f'{st.st_ino}:{st.st_size}:{st.st_mtime_ns}','partialTail':end==st.st_size and bool(raw) and not raw.endswith(b'\n')}

    def attachment(self,agent,token):
        payload=self.attachments.verify(token,agent)
        session,path=self.resolve(agent)
        provider,endpoint=provider_identity(session)
        if not path or payload['source']!=provider+':'+endpoint: raise ValueError('Attachment source changed')
        fd=trusted_open(path,self.roots(provider))
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_ino!=payload['inode'] or payload['offset']+payload['length']>st.st_size: raise ValueError('Attachment source changed')
            f.seek(payload['offset']);line=f.read(payload['length'])
        if hashlib.sha256(line).hexdigest()!=payload['record']: raise ValueError('Attachment message changed; refresh this session')
        try:
            entry=parse_event(json.loads(line),provider)[payload['entry']]
            spec=entry['_attachments'][payload['attachment']]
        except (ValueError,KeyError,IndexError,TypeError): raise ValueError('Attachment is not part of a public message') from None
        body,mime,width,height=decode_image(spec.get('_data'))
        return body,mime,spec['name']

    def validate_reader(self,f,st,agent,reader,initialized):
        if not isinstance(reader,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',reader):raise ValueError('Invalid transcript reader identity')
        now=time.monotonic()
        self.readers={key:value for key,value in self.readers.items() if now-value['at']<900}
        old=self.readers.get(reader)
        changed=bool(initialized and not old)
        if old:
            changed=old['agent']!=agent or old['inode']!=st.st_ino
            if not changed:
                for start,end,digest in old['ranges']:
                    if end>st.st_size:changed=True;break
                    f.seek(start)
                    if hashlib.sha256(f.read(end-start)).hexdigest()!=digest:changed=True;break
        if changed or not old:self.readers[reader]={'agent':agent,'inode':st.st_ino,'ranges':[],'at':now}
        self.readers[reader]['at']=now
        if len(self.readers)>32:
            victim=min((key for key in self.readers if key!=reader),key=lambda key:self.readers[key]['at']);del self.readers[victim]
        return changed
    def remember_range(self,f,st,agent,reader,start,end):
        cache=self.readers[reader]
        # Refuse a replacement between parsing and fingerprinting rather than bless mixed bytes.
        current=os.fstat(f.fileno())
        if current.st_ino!=st.st_ino or current.st_size<st.st_size or current.st_mtime_ns!=st.st_mtime_ns and current.st_size<=st.st_size:
            cache['ranges']=[];return True
        if end<=start:return False
        ranges=[]
        for a,z,digest in cache['ranges']:
            if start<=z and end>=a:start=min(a,start);end=max(z,end)
            else:ranges.append((a,z,digest))
        reset=False
        if sum(z-a for a,z,_ in ranges)+end-start>64*1024*1024 or len(ranges)>32:
            cache['ranges']=[];return True
        f.seek(start);digest=hashlib.sha256(f.read(end-start)).hexdigest();ranges.append((start,end,digest));cache['ranges']=ranges
        return reset
