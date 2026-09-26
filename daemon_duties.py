"""Bounded board duties: one in-flight call per duty, independent fault domains.

A stalled duty keeps its one worker and is reported overdue. It is never retried
concurrently or force-stopped, which could leave database/provider state uncertain.
"""
import json
import sys
import traceback
from pathlib import Path
import threading
import time


class DutyRunner:
    def __init__(self, duties, interval=5, overdue_after=30, clock=time.time):
        self.duties=dict(duties);self.interval=interval
        self.overdue_after=overdue_after;self.clock=clock
        self.lock=threading.Lock();self.stopped=False
        self.records={name:{'running':False,'startedAt':None,'finishedAt':None,
                            'lastSuccessAt':None,'lastError':'','lastFailure':None,'lastOverdue':None,'threadId':None,'failures':0,'runs':0}
                      for name in self.duties}

    def tick(self):
        with self.lock:
            if self.stopped:return
            now=self.clock()
            for name,fn in self.duties.items():
                record=self.records[name]
                if record['running'] or (record['finishedAt'] is not None and now-record['finishedAt']<self.interval):continue
                record.update(running=True,startedAt=now,runs=record['runs']+1)
                threading.Thread(target=self._run,args=(name,fn),name='board-duty-'+name,daemon=True).start()

    def _run(self,name,fn):
        error='';failure=None
        with self.lock:self.records[name]['threadId']=threading.get_ident()
        try:fn()
        except BaseException as exc:
            error=type(exc).__name__+':'+str(exc)
            try:
                from daemon_diagnostics import describe
                failure=describe(exc,name)
                print(json.dumps(failure,sort_keys=True),flush=True)
            except Exception:
                # Diagnostics must not mask the original failure or strand a duty.
                failure={'exceptionType':type(exc).__name__,'diagnosticsUnavailable':True}
                print('board duty '+name+' failed; diagnostics unavailable',flush=True)
        finally:
            with self.lock:
                record=self.records[name];record.update(running=False,finishedAt=self.clock(),lastError=error)
                if error:
                    record['failures']+=1;record['lastFailure']=failure
                else:record.update(lastSuccessAt=record['finishedAt'],failures=0)

    def stop(self):
        # Do not abandon ownership or spawn replacements for in-flight work.
        with self.lock:self.stopped=True

    def snapshot(self):
        with self.lock:
            now=self.clock();result={}
            for name,record in self.records.items():
                age=None if record['lastSuccessAt'] is None else max(0,now-record['lastSuccessAt'])
                overdue=record['running'] and now-record['startedAt']>self.overdue_after
                if overdue and (record.get('lastOverdue') or {}).get('startedAt')!=record['startedAt']:
                    frame=sys._current_frames().get(record.get('threadId'))
                    record['lastOverdue']={'startedAt':record['startedAt'],'observedAt':now,
                        'elapsedSeconds':now-record['startedAt'],'threadId':record.get('threadId'),
                        'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
                                  for f in traceback.extract_stack(frame)[-16:]] if frame else [],
                        'claim':'One content-free stack observation; no timeout increase or replacement worker'}
                state='overdue' if overdue else 'failed' if record['lastError'] else 'running' if record['running'] else 'waiting' if record['lastSuccessAt'] is None else 'healthy'
                result[name]={**record,'state':state,'lastSuccessAgeSeconds':age,'overdue':overdue}
            return {'ok':all(r['lastSuccessAt'] is not None and not r['lastError'] and not r['overdue'] for r in result.values()),
                    'stopped':self.stopped,'duties':result,'maximumInFlight':len(self.duties)}
