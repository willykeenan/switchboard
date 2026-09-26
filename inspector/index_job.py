"""Six bounded file readers; one index writer; no source writes."""
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import argparse,fcntl,hashlib,json,os,stat,subprocess,time,sys
from .library import EXT,SKIP,DENY,connect,digest,encoded,now
from .transcripts import safe_text

def _read_document(item):
    p=Path(item['path']);row={**item,'id':digest(item['project']+'\0'+item['lane']+'\0'+str(p)),'title':p.stem.replace('_',' ').replace('-',' '),'kind':'Research' if p.suffix=='.md' else 'Evidence','body':'','sha':'','size':0,'modified':0,'state':'Source missing'}
    if any(t in p.name.lower() for t in ('decision','verdict')):row['kind']='Decision'
    try:
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):return row
            row.update(size=st.st_size,modified=st.st_mtime)
            prior=item.get('previous')
            if prior and prior['state'] in ('Indexed','Indexed excerpt') and prior['size']==st.st_size and prior['modified']==st.st_mtime and prior['sha']:
                return {'id':row['id'],'unchanged':True,'state':prior['state']}
            if st.st_size>2*1024*1024:return {**row,'state':'Metadata only · large file'}
            raw=f.read(2*1024*1024+1)
        row['sha']=hashlib.sha256(raw).hexdigest()
        if p.suffix.lower()=='.pdf':
            tool=Path('/opt/homebrew/bin/pdftotext')
            if tool.exists():raw=subprocess.run([str(tool),'-f','1','-l','100',str(p),'-'],capture_output=True,timeout=12).stdout
            else:return {**row,'state':'Metadata only · PDF reader unavailable'}
        text=raw.decode('utf-8',errors='replace');row['body']=safe_text(text,200000);row['state']='Indexed' if len(text)<=200000 else 'Indexed excerpt'
        if p.suffix=='.md':
            heading=next((line.lstrip('# ').strip() for line in text.splitlines()[:30] if line.startswith('# ')),None)
            if heading:row['title']=heading[:200]
        return row
    except (OSError,ValueError,subprocess.TimeoutExpired):return {**row,'state':'Source unreadable'}

def read_document(item):
    return {**_read_document(item),'workerPid':os.getpid()}

def job_identity(directory):
    # One identity per physical index store, independent of the checkout invoking it.
    return 'switchboard-library-index-'+digest(str(Path(directory).resolve()))[:20]

def main(root,directory=None):
    command=[sys.executable,*(['-B'] if sys.dont_write_bytecode else []),'-m','inspector.index_job','--root',str(root),'--directory',str(directory if directory is not None else Path(root)/'runtime/library')]
    root=Path(root).resolve()
    directory=(Path(directory) if directory else root/'runtime/library').resolve()
    directory.mkdir(parents=True,exist_ok=True)
    # Lock before reading the spec or publishing any registry/status record. A second
    # caller must never replace the active owner's status, even from another checkout.
    with (directory/'index.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'already-running','jobId':job_identity(directory)}
        return run_locked(root,directory,command)

def run_locked(root,directory,command):
    started=time.monotonic();jobid=job_identity(directory)
    registry=os.environ.get('SWITCHBOARD_CPU_REGISTRY') or os.environ.get('SWITCHBOARD_CPU_REGISTRY')
    jobpath=(Path(registry).expanduser() if registry else Path(root)/'runtime/cpu-workers/jobs')/(jobid+'.json')
    status={'schemaVersion':'ke.cpu-job.v2','jobId':jobid,'title':'Index project research library','owner':'switchboard:library',
            'pid':os.getpid(),'parentPid':os.getpid(),'pgid':os.getpgid(0),'command':command,
            'cwd':os.getcwd(),'root':str(root),'directory':str(directory),
            'status':'running','startedAt':now(),'etaSeconds':None,'workers':[],
            'progress':{'completed':0,'processed':0,'total':0,'unit':'documents','deduplicated':True},
            'bindingConstraint':'Bounded local file reads, at most six process workers and one SQLite writer',
            'sources':0,'skipped':0,'cappedSources':[]}
    status['registeredAt']=status['startedAt'];reader_pids=set()
    def report():
        status['updatedAt']=now();status['progress']['updatedAt']=status['updatedAt'];raw=encoded(status)
        for p in [directory/'index-status.json',jobpath]:
            p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.'+str(os.getpid())+'.tmp');tmp.write_text(raw);os.replace(tmp,p)
    def finish(state,**extra):
        elapsed=max(.001,time.monotonic()-started)
        status.update(status=state,finishedAt=now(),wallSeconds=round(elapsed,2),etaSeconds=0 if state=='completed' else None,
                      executedPid=os.getpid(),executedReaderPids=sorted(reader_pids),pid=None,parentPid=None,pgid=None,workers=[],**extra)
        report()
    try:
        report()
        spec=json.loads((directory/'index-spec.json').read_text())
        status['sources']=len(spec['sources'])
        files={}
        for source in spec['sources']:
            p=Path(source['path']);count=0
            candidates=[p] if not p.is_dir() else None
            if candidates is None:
                candidates=[]
                for parent,dirs,names in os.walk(p,followlinks=False):
                    dirs[:]=[d for d in sorted(dirs) if d not in SKIP and not d.startswith('.') and not Path(parent,d).is_symlink()]
                    if len(Path(parent).relative_to(p).parts)>7:dirs[:]=[];continue
                    for name in sorted(names):
                        f=Path(parent,name)
                        if f.suffix.lower() in EXT and name.lower() not in DENY and not name.startswith('.') and not f.is_symlink():candidates.append(f)
                        if len(candidates)>=3000:break
                    if len(candidates)>=3000:status['cappedSources'].append(str(p));break
            for f in candidates:
                if f.suffix.lower() not in EXT or f.name.lower() in DENY:continue
                key=(source['project'],source['lane'],str(f));files[key]={**source,'path':str(f)}
                if len(files)>=15000:break
            if len(files)>=15000:status['cappedSources'].append('Global 15000-document limit');break
        status['progress']['total']=len(files);report();rows=[];unchanged=[]
        with connect(directory/'library.sqlite3') as db:prior={r['id']:dict(r) for r in db.execute('SELECT id,sha,modified,size,state FROM documents')}
        for item in files.values():item['previous']=prior.get(digest(item['project']+'\0'+item['lane']+'\0'+item['path']))
        if files:
            with ProcessPoolExecutor(max_workers=min(6,len(files))) as pool:
                for i,row in enumerate(pool.map(read_document,files.values(),chunksize=8)):
                    pid=row.pop('workerPid');new_reader=pid not in reader_pids;reader_pids.add(pid)
                    status['workers']=[{'pid':p,'label':'Library file reader'} for p in sorted(reader_pids)]
                    (unchanged if row.get('unchanged') else rows).append(row)
                    status['progress'].update(completed=i+1,processed=i+1)
                    status['etaSeconds']=round((time.monotonic()-started)/(i+1)*(len(files)-i-1),1)
                    if new_reader or i%80==0:report()
            status['workers']=[];report()
        with connect(directory/'library.sqlite3') as db:
            for r in spec['tasks']:rows.append({**r,'path':'','sha':digest(r['body']),'state':'Recorded','modified':__import__('datetime').datetime.fromisoformat(json.loads(r['body']).get('updated_at').replace('Z','+00:00')).timestamp(),'size':len(r['body'])})
            for n in db.execute('SELECT * FROM notes'):
                r=dict(n);rows.append({**r,'path':'','sha':digest(r['body']),'state':'Library note','basis':'Saved in this library','modified':__import__('datetime').datetime.fromisoformat(r['created'].replace('Z','+00:00')).timestamp(),'size':len(r['body'])})
            for n in db.execute('SELECT * FROM reviews'):
                r=dict(n);body=encoded(r);rows.append({**r,'path':'','title':r['decision']+': '+r['query'],'kind':'Library review','body':body,'sha':digest(body),'state':'Recorded','basis':'Library review with pinned evidence versions','modified':__import__('datetime').datetime.fromisoformat(r['created'].replace('Z','+00:00')).timestamp(),'size':len(body)})
            db.execute("UPDATE documents SET state='Outside current source coverage' WHERE path!=''")
            for r in unchanged:db.execute('UPDATE documents SET state=? WHERE id=?',(r['state'],r['id']))
            changed_count=0
            for r in rows:
                if r['id'] in prior and prior[r['id']]['sha']==r['sha']:
                    db.execute('UPDATE documents SET state=? WHERE id=?',(r['state'],r['id']));continue
                changed_count+=1
                raw=safe_text(r['body'],200000);indexed=now()
                db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?)',(r['id'],r['sha'],raw,indexed))
                db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(r['id'],r['project'],r['lane'],r['path'],r['title'],r['kind'],r['sha'],raw,r['modified'],r['size'],indexed,r['state'],r['basis']))
                db.execute('DELETE FROM search WHERE id=?',(r['id'],));db.execute('INSERT INTO search VALUES(?,?,?)',(r['id'],r['title'],raw))
        finish('completed',documents=len(rows)+len(unchanged),changedDocuments=changed_count,unchangedDocuments=len(unchanged),throughput=round((len(rows)+len(unchanged))/max(.001,time.monotonic()-started),1))
        return status
    except BaseException as e:
        finish('failed',error=type(e).__name__+': '+str(e));raise
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--directory');args=parser.parse_args();main(args.root,args.directory)
