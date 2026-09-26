"""Observe an imported worker's own current turn; never start or wake a session.

The catalog binds the registered provider thread to its existing event-log reader.
A fresh standalone app-server may call that same active thread notLoaded, so it
must not be used to infer termination of a turn owned by the desktop app.
"""
from activity import epoch
import hashlib
import json
from pathlib import Path
import time


def historical_terminal(store,actor,thread_id,turn_id):
    """Read exact terminal metadata from the registered log after a later turn.

    The bounded scan never treats absence, a final message, or another turn's end
    as termination. Request/assistant contents are neither used nor returned.
    """
    name=getattr(store.flow,'_rollout_paths',{}).get(actor)
    if not name:return None
    path=Path(name)
    if not path.is_file() or path.is_symlink():return None
    deadline=time.monotonic()+2
    try:
        with path.open('rb') as stream:
            header=json.loads(stream.readline(65537))
            if header.get('type')!='session_meta' or header.get('payload',{}).get('id')!=thread_id:return None
            size=path.stat().st_size;offset=max(stream.tell(),size-16*1024*1024)
            if offset>stream.tell():stream.seek(offset);stream.readline(1000001)
            remaining=16*1024*1024
            while remaining>0 and time.monotonic()<deadline:
                line=stream.readline(min(remaining,1000001));remaining-=len(line)
                if not line:break
                if len(line)>1000000:return None
                try:event=json.loads(line)
                except (ValueError,UnicodeError):continue
                payload=event.get('payload') or {}
                if event.get('type')!='event_msg' or payload.get('turn_id')!=turn_id or payload.get('type') not in ('task_complete','task_failed','turn_aborted'):continue
                ended=epoch(event.get('timestamp'))
                if ended is None:return None
                return {'state':'terminal','threadId':thread_id,'turnId':turn_id,'source':'Registered Codex event log exact terminal event',
                        'endedAt':ended,'observedAt':store.clock(),'eventSha256':hashlib.sha256(line).hexdigest()}
    except (OSError,ValueError,UnicodeError):return None
    return None


def terminal(store,actor,thread_id,turn_id):
    if not store.flow or not actor.startswith('codex:') or not turn_id:return None
    session=next((s for s in store.flow.catalog()['sessions'] if s['agent_id']==actor),None)
    if not session or session.get('managed') or session.get('endpoint')!=thread_id:return None
    a=session.get('activity') or {};ended=epoch(a.get('turnEndedAt'))
    if a.get('source')!='Codex event log' or a.get('turnId')!=turn_id or a.get('turnStatus') not in ('finished','failed','stopped') or ended is None:return historical_terminal(store,actor,thread_id,turn_id)
    return {'state':'terminal','threadId':thread_id,'turnId':turn_id,'source':'Registered Codex event log','endedAt':ended,'observedAt':store.clock()}


def observe(store, actor, thread_id, turn_id):
    if not store.flow or not actor.startswith('codex:') or not thread_id or not turn_id:return None
    session=next((s for s in store.flow.catalog()['sessions'] if s['agent_id']==actor),None)
    if not session or session.get('managed') or session.get('endpoint')!=thread_id:return None
    a=session.get('activity') or {};at=epoch(a.get('observedAt'))
    if a.get('source')!='Codex event log' or a.get('turnId')!=turn_id or at is None:return None
    if not 0<=store.clock()-at<=30 or not a.get('fresh') or not a.get('active') or a.get('turnStatus')!='open':return None
    return {'state':'running','threadId':thread_id,'turnId':turn_id,'source':'Registered Codex event log',
            'observedAt':at,'adapter':'cooperative-current-turn','providerStartIssued':False,
            'boundary':'Explicit worker task binding inside its existing user-authorized turn; no provider resume or launch.'}
