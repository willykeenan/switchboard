"""Passive activity metadata. Never exposes reasoning, prompts, commands or tool outputs; public commentary is redacted and bounded."""
from __future__ import annotations
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
import copy
import json
import re
import threading
import time

FRESH_SECONDS = 60
TAIL_BYTES = 1024 * 1024
TURN_METADATA_BYTES = 16 * TAIL_BYTES
TERMINAL = {'finished', 'stopped', 'failed'}
LABELS = {'unknown':'Activity unknown', 'unavailable':'Status unavailable', 'quiet':'No recent update',
          'queued':'Queued', 'starting':'Starting…', 'thinking':'Thinking…', 'working':'Working…',
          'tools':'Working…', 'command':'Running a command…', 'editing':'Editing files…',
          'searching':'Searching…', 'responding':'Responding…', 'waiting':'Waiting…',
          'input':'Needs your input', 'compacting':'Updating context…',
          'finished':'Idle · turn ended', 'stopped':'Turn stopped', 'failed':'Turn failed',
          'review':'Awaiting your review', 'dependencies':'Waiting for dependencies',
          'approval':'Awaiting your approval', 'cancelling':'Stopping…', 'uncertain':'Runtime needs checking',
          'accepted':'Deliverables accepted', 'rejected':'Revision requested', 'hired':'Ready for assignment'}

def epoch(value):
    try:
        if isinstance(value,(int,float)): return float(value)
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,OverflowError): return None

def iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value is not None else None

def blank():
    return {'phase':'unknown','at':None,'detail':'No usable activity event is available.',
            'turnStatus':'unknown','turnStartedAt':None,'turnEndedAt':None,
            'lastFinishedAt':None,'lastFinishedDurationSeconds':None,'lastAction':None,'lastActionAt':None,'model':None,'reasoning':None,'modelObservedAt':None,'planProgress':None,'publicAction':None,'publicActionAt':None,'pending':{}}

def identifier(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/ -]{0,99}',value) else None

def public_summary(content):
    if not isinstance(content,list):return None
    text=' '.join(p.get('text','') for p in content if isinstance(p,dict) and p.get('type') in ('text','output_text') and isinstance(p.get('text'),str))
    text=re.sub(r'(?i)\bBearer\s+[^\s]+','Bearer [redacted]',text)
    text=re.sub(r'(?i)(\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+',r'\1[redacted]',text)
    text=re.sub(r'\b(?:sk-[A-Za-z0-9_-]{18,}|gh[pousr]_[A-Za-z0-9_]{20,})\b','[redacted]',text)
    return re.sub(r'\s+',' ',text).strip()[:280] or None

def plan_progress(arguments,at):
    """Count reported checklist states only; no task text, inferred ETA or success claim."""
    if not isinstance(arguments,str) or len(arguments)>100000:return None
    try:payload=json.loads(arguments)
    except ValueError:return None
    plan=payload.get('plan') if isinstance(payload,dict) else None
    if not isinstance(plan,list) or not 1<=len(plan)<=200:return None
    if any(not isinstance(p,dict) or p.get('status') not in ('pending','in_progress','completed') for p in plan):return None
    return {'completed':sum(p['status']=='completed' for p in plan),'total':len(plan),
            'unit':'reported plan steps','observedAt':iso(at),'basis':'Agent-reported checklist; not assignment acceptance or measured compute.'}

def tool_kind(name):
    """Only allowlisted categories leave the observer; tool arguments never do."""
    name=str(name or '').lower()
    if 'request_user_input' in name: return 'input','A request for your input was observed.'
    if any(x in name for x in ('sleep','wait_threads','wait_agent')) or name in ('wait','functions.wait'):
        return 'waiting','Waiting for a timer, task or tool result.'
    if any(x in name for x in ('exec_command','write_stdin','bash','terminal')): return 'command','A command was requested; its result is pending.'
    if any(x in name for x in ('apply_patch','edit_file','write_file')): return 'editing','A file change was requested; its result is pending.'
    if any(x in name for x in ('web','search','browse')): return 'searching','A search or browser tool was requested.'
    return 'tools','A tool call is pending.'

def set_phase(s,phase,at,detail,**extra):
    s.update(phase=phase,at=at,detail=detail,**extra)

def begin(s,at,turn_id=None,explicit=True):
    if turn_id and turn_id==s.get('turnId') and s['turnStatus']=='open': return
    last, duration=s.get('lastFinishedAt'),s.get('lastFinishedDurationSeconds')
    model,reasoning,observed=s.get('model'),s.get('reasoning'),s.get('modelObservedAt')
    s.clear();s.update(blank());s.update(lastFinishedAt=last,lastFinishedDurationSeconds=duration,
        turnStatus='open',turnId=turn_id,turnStartedAt=iso(at) if explicit else None,model=model,reasoning=reasoning,modelObservedAt=observed)
    set_phase(s,'starting',at,'A new turn started.' if explicit else 'Recent activity was observed; the turn start is outside the sampled history.')

def finish(s,phase,at):
    start=epoch(s.get('turnStartedAt'));duration=max(0,at-start) if start is not None else None
    s.update(turnStatus=phase,turnEndedAt=iso(at),pending={})
    if phase=='finished':s.update(lastFinishedAt=iso(at),lastFinishedDurationSeconds=duration)
    set_phase(s,phase,at,{'finished':'The agent turn ended. This does not imply that its project or background jobs are complete.',
                         'stopped':'The turn was interrupted or cancelled.', 'failed':'The provider recorded a failed turn.'}[phase])

def consume(s,event,provider,clock):
    if not isinstance(event,dict): return
    at=epoch(event.get('timestamp'))
    if at is None or at>clock+5 or (s.get('at') is not None and at<s['at']):return
    kind=event.get('type');p=event.get('payload') or {}
    if not isinstance(p,dict):return
    if kind=='turn_context':
        s.update(model=identifier(p.get('model')),reasoning=identifier(p.get('effort') or p.get('reasoning_effort')),modelObservedAt=iso(at))
        return
    if provider.lower()=='claude':
        m=event.get('message') or {}
        if not isinstance(m,dict):return
        if kind=='assistant' and identifier(m.get('model')):
            s.update(model=identifier(m['model']),reasoning=None,modelObservedAt=iso(at))
        content=m.get('content') or [];content=content if isinstance(content,list) else []
        parts=[c for c in content if isinstance(c,dict)]
        if kind=='user':
            results=[c for c in parts if c.get('type')=='tool_result']
            if results:
                if s['turnStatus'] in TERMINAL:return
                for c in results:s['pending'].pop(c.get('tool_use_id'),None)
                set_phase(s,'tools' if s['pending'] else 'working',at,'Tool result received.',turnStatus='open')
            elif event.get('isMeta') is not True:
                begin(s,at,explicit=False);set_phase(s,'queued',at,'A user request was recorded; execution is not yet confirmed.',turnStatus='queued')
        elif kind=='assistant':
            if s['turnStatus'] in TERMINAL or s['turnStatus']=='unknown':begin(s,at,explicit=False)
            s['turnStatus']='open'
            if m.get('stop_reason')=='end_turn':finish(s,'finished',at)
            else:
                for c in parts:
                    if c.get('type')=='thinking':set_phase(s,'thinking',at,'Recent reasoning activity was recorded. Contents are not collected.')
                    elif c.get('type')=='tool_use':
                        phase,detail=tool_kind(c.get('name'));s['pending'][c.get('id') or 'tool']=(phase,detail);set_phase(s,phase,at,detail)
                    elif c.get('type')=='text':set_phase(s,'responding',at,'The agent recently produced a message.')
        elif kind=='system' and event.get('subtype')=='turn_duration':finish(s,'finished',at)
        elif kind=='progress' and s['turnStatus'] not in TERMINAL:
            set_phase(s,'tools',at,'Tool progress was recorded.',turnStatus='open')
        return
    if kind=='event_msg':
        event_type=p.get('type')
        if event_type=='task_started':begin(s,at,p.get('turn_id'));return
        if event_type in ('task_complete','task_failed','turn_aborted'):
            tid=p.get('turn_id')
            if tid and s.get('turnId') and tid!=s['turnId']:return
            finish(s,{'task_complete':'finished','task_failed':'failed','turn_aborted':'stopped'}[event_type],at);return
        if event_type=='user_message':
            if s['turnStatus']!='open':begin(s,at,explicit=False);set_phase(s,'queued',at,'A user request was recorded; execution is not yet confirmed.',turnStatus='queued')
            return
        if event_type not in ('item_started','item_completed','exec_command_begin','exec_command_end','web_search_begin','web_search_end','patch_apply_begin','patch_apply_end'):return
        if p.get('turn_id') and s.get('turnId') and p['turn_id']!=s['turnId']:return
        item=p.get('item') or {};item=item if isinstance(item,dict) else {}
        it=re.sub(r'[^a-z]','',str(item.get('type','')).lower())
        if it=='reasoning':phase,detail='thinking','Recent reasoning activity was recorded. Contents are not collected.'
        elif it=='agentmessage':
            if s['turnStatus'] in TERMINAL:return
            phase,detail='responding','The agent recently produced a message.'
        elif it=='contextcompaction':phase,detail='compacting','The runtime is updating the task context.'
        elif it in ('commandexecution','filechange','websearch','mcptoolcall','dynamictoolcall'):
            phase,detail={'commandexecution':('command','A command is running.'),'filechange':('editing','A file change is in progress.'),'websearch':('searching','A web search is in progress.')}.get(it,('tools','A tool is running.'))
            if event_type=='item_completed':
                phase='working';detail={'commandexecution':'Command finished.','filechange':'File change finished.','websearch':'Search finished.'}.get(it,'Tool finished.')
        else:
            match={'exec_command_begin':('command','A command is running.'),'exec_command_end':('working','Command finished.'),'web_search_begin':('searching','A web search is in progress.'),'web_search_end':('working','Search finished.'),'patch_apply_begin':('editing','A file change is in progress.'),'patch_apply_end':('working','File change finished.')}.get(event_type)
            if not match:return
            phase,detail=match
        if s['turnStatus'] in TERMINAL:return
        if s['turnStatus']=='unknown':begin(s,at,p.get('turn_id'),explicit=False)
        if event_type=='item_completed' and it in ('commandexecution','filechange','websearch','mcptoolcall','dynamictoolcall'):
            if it=='commandexecution' and isinstance(item.get('exit_code'),int) and item['exit_code']!=0:detail='Command failed (exit '+str(item['exit_code'])+').'
            elif item.get('status')=='failed':detail='Tool failed.'
            s.update(lastAction=detail,lastActionAt=iso(at))
        set_phase(s,phase,at,detail,turnStatus='open');return
    if kind!='response_item':return
    typ=p.get('type')
    if p.get('role') in ('system','developer'):return
    if typ=='message' and p.get('role')=='assistant' and p.get('channel')=='commentary' and s['turnStatus'] not in TERMINAL:
        summary=public_summary(p.get('content'))
        if summary:s.update(publicAction=summary,publicActionAt=iso(at))
    if typ in ('function_call','custom_tool_call'):
        if s['turnStatus'] in TERMINAL or s['turnStatus']=='unknown':begin(s,at,explicit=False)
        if str(p.get('name','')).split('.')[-1]=='update_plan':
            progress=plan_progress(p.get('arguments'),at)
            if progress:s['planProgress']=progress
        phase,detail=tool_kind(p.get('name'));s['pending'][p.get('call_id') or 'tool']=(phase,detail)
        set_phase(s,phase,at,detail,turnStatus='open')
    elif typ in ('function_call_output','custom_tool_call_output'):
        if s['turnStatus'] in TERMINAL:return
        s['pending'].pop(p.get('call_id'),None)
        if s['pending']:phase,detail=next(reversed(s['pending'].values()))
        else:phase,detail='working','Tool result received.'
        set_phase(s,phase,at,detail,turnStatus='open')
    elif typ=='reasoning' or p.get('channel')=='analysis':
        if s['turnStatus'] in TERMINAL or s['turnStatus']=='unknown':begin(s,at,explicit=False)
        set_phase(s,'thinking',at,'Recent reasoning activity was recorded. Contents are not collected.',turnStatus='open')
    elif typ=='message' and p.get('role')=='assistant' and s['turnStatus'] not in TERMINAL:
        set_phase(s,'responding',at,'The agent recently produced a message; its turn has not yet been recorded as finished.',turnStatus='open')

def present(s,clock=None,source='Local event log',partial=False):
    clock=time.time() if clock is None else clock
    phase=s['phase'];age=max(0,clock-s['at']) if s.get('at') is not None else None
    stale=age is not None and age>FRESH_SECONDS and phase not in TERMINAL
    shown='quiet' if stale else phase
    start=epoch(s.get('turnStartedAt'));end=epoch(s.get('turnEndedAt'))
    return {'phase':shown,'recordedPhase':phase,'label':LABELS.get(shown,LABELS['unknown']),
            'detail':s['detail'],'observedAt':iso(s.get('at')),'sampledAt':iso(clock),'ageSeconds':round(age) if age is not None else None,
            'fresh':age is not None and age<=FRESH_SECONDS,'stale':stale,
            'active':not stale and s['turnStatus']=='open','expiresAt':iso(s['at']+FRESH_SECONDS) if s.get('at') is not None and phase not in TERMINAL else None,
            'source':source,'turnId':s.get('turnId'),'turnStatus':s['turnStatus'],'turnStartedAt':s.get('turnStartedAt'),
            'turnEndedAt':s.get('turnEndedAt'),'durationSeconds':round(max(0,(end or clock)-start)) if start is not None else None,
            'lastFinishedAt':s.get('lastFinishedAt'),'lastFinishedDurationSeconds':s.get('lastFinishedDurationSeconds'),
            'lastAction':s.get('lastAction'),'lastActionAt':s.get('lastActionAt'),'actionHistorical':s['turnStatus'] in TERMINAL or stale,
            'model':s.get('model'),'reasoning':s.get('reasoning'),'modelObservedAt':s.get('modelObservedAt'),
            'planProgress':s.get('planProgress'),'publicAction':s.get('publicAction'),'publicActionAt':s.get('publicActionAt'),'historyPartial':partial,'basis':'Timestamped activity evidence; an open task or process alone is not proof of model activity.'}

def preceding_turn(path, end, clock):
    """Recover only the latest lifecycle marker before a truncated activity tail.

    Read backwards in bounded chunks. A terminal or unidentified start is a
    boundary, never permission to borrow an older turn. No prompt/tool content
    escapes this scan, and recent activity is still required by the caller.
    """
    consumed=0;carry=b'';deadline=time.monotonic()+2
    with path.open('rb') as stream:
        position=end
        while position>0 and consumed<TURN_METADATA_BYTES and time.monotonic()<deadline:
            count=min(position,65536,TURN_METADATA_BYTES-consumed)
            position-=count;stream.seek(position);raw=stream.read(count);consumed+=len(raw)
            lines=(raw+(carry or b'')).split(b'\n')
            skipping=carry is None
            if skipping:lines.pop()
            carry=lines.pop(0) if position and lines else (None if skipping and b'\n' not in raw else b'')
            for line in reversed(lines):
                if len(line)>TAIL_BYTES or b'"event_msg"' not in line:continue
                try:event=json.loads(line)
                except (ValueError,UnicodeError):continue
                if not isinstance(event,dict) or event.get('type')!='event_msg':continue
                payload=event.get('payload');at=epoch(event.get('timestamp'))
                if not isinstance(payload,dict) or at is None or at>clock+5:continue
                if payload.get('type') not in ('task_started','task_complete','task_failed','turn_aborted'):continue
                state=blank();consume(state,event,'codex',clock)
                return state,consumed
            # An oversized content record is irrelevant to lifecycle metadata.
            if carry is not None and len(carry)>TAIL_BYTES:carry=None
    return None,consumed

class ActivityReader:
    def __init__(self,max_entries=1024):self.entries=OrderedDict();self.lock=threading.RLock();self.max_entries=max_entries;self.bytes_read=0
    def observe(self,path,provider='codex',clock=None):
        clock=time.time() if clock is None else clock
        try:
            p=Path(path)
            with self.lock:
                stat=p.stat();key=(str(p),provider);entry=self.entries.get(key)
                identity=(stat.st_dev,stat.st_ino)
                if entry is None or entry['identity']!=identity or stat.st_size<entry['offset'] or (stat.st_size==entry['offset'] and stat.st_mtime_ns!=entry['mtime']):
                    entry={'identity':identity,'offset':max(0,stat.st_size-TAIL_BYTES),'state':blank(),'pending':b'','partial':stat.st_size>TAIL_BYTES,'drop':stat.st_size>TAIL_BYTES}
                if stat.st_size-entry['offset']>TAIL_BYTES:
                    entry.update(offset=max(0,stat.st_size-TAIL_BYTES),state=blank(),pending=b'',partial=True,drop=True)
                if stat.st_size>entry['offset']:
                    tail_start=entry['offset']
                    with p.open('rb') as f:f.seek(tail_start);raw=f.read(TAIL_BYTES)
                    self.bytes_read+=len(raw);entry['offset']+=len(raw)
                    lines=(entry['pending']+raw).split(b'\n');entry['pending']=lines.pop()
                    metadata_end=tail_start
                    if entry.get('drop') and lines:
                        metadata_end+=len(lines[0])+1;lines=lines[1:];entry['drop']=False
                    if len(entry['pending'])>=TAIL_BYTES:entry['pending']=b'';entry['drop']=True;entry['partial']=True
                    for line in lines:
                        try:consume(entry['state'],json.loads(line),provider,clock)
                        except (ValueError,UnicodeError,TypeError,AttributeError):continue
                    state=entry['state']
                    if provider.lower()=='codex' and entry['partial'] and not entry.get('metadataChecked') and state['turnStatus']=='open' and not state.get('turnId') and not state.get('turnStartedAt'):
                        entry['metadataChecked']=True
                        seed,read=preceding_turn(p,metadata_end,clock);self.bytes_read+=read
                        if seed is not None:
                            for line in lines:
                                try:consume(seed,json.loads(line),provider,clock)
                                except (ValueError,UnicodeError,TypeError,AttributeError):continue
                            # Do not bind metadata across a file replacement or truncation.
                            after=p.stat()
                            if (after.st_dev,after.st_ino)==identity and after.st_size>=entry['offset']:
                                entry['state']=seed
                entry['mtime']=stat.st_mtime_ns;self.entries[key]=entry;self.entries.move_to_end(key)
                while len(self.entries)>self.max_entries:self.entries.popitem(last=False)
                return present(entry['state'],clock,provider.title()+' event log',entry['partial'])
        except (OSError,TypeError,ValueError):
            result=present(blank(),clock,provider.title()+' event log');result.update(phase='unavailable',label=LABELS['unavailable'],detail='The session log could not be read.');return result

def rpc_activity(method,params,at=None):
    """Extract live phase metadata from supported app-server notifications."""
    at=time.time() if at is None else at;item=params.get('item') or {};typ=item.get('type') if isinstance(item,dict) else None
    if method.startswith('item/reasoning/'):phase,detail='thinking','The managed runtime is streaming reasoning activity.'
    elif method=='item/agentMessage/delta':phase,detail='responding','The managed runtime is streaming a response.'
    elif method in ('item/commandExecution/outputDelta','item/mcpToolCall/progress'):phase,detail='tools','The managed runtime reported tool progress.'
    elif method=='item/started':
        if typ not in ('reasoning','agentMessage','commandExecution','fileChange','webSearch','contextCompaction','mcpToolCall','dynamicToolCall'):return None
        phase,detail={'reasoning':('thinking','The managed runtime started reasoning.'),'agentMessage':('responding','The managed runtime started a response.'),'commandExecution':('command','The managed runtime started a command.'),'fileChange':('editing','The managed runtime started a file change.'),'webSearch':('searching','The managed runtime started a search.'),'contextCompaction':('compacting','The runtime is updating context.')}.get(typ,('tools','The managed runtime started a tool.'))
    elif method=='item/completed':phase,detail='working','The last runtime item finished.'
    else:return None
    s=blank();set_phase(s,phase,at,detail,turnStatus='open');return present(s,at,'Managed runtime events')

def managed_activity(managed,clock=None):
    clock=time.time() if clock is None else clock;status=managed.get('workStatus','HIRED');activity=copy.deepcopy(managed.get('workActivity'))
    heartbeat=epoch(managed.get('heartbeatAt'));fresh=heartbeat is not None and 0<=clock-heartbeat<=15
    mapped={'DRAFT':'approval','APPROVED':'queued','BLOCKED':'dependencies','STARTING':'starting','CANCELLING':'cancelling','UNCERTAIN':'uncertain','REVIEW':'review','ACCEPTED':'accepted','REJECTED':'rejected','FAILED':'failed','TIMED_OUT':'stopped','CANCELLED':'stopped','HIRED':'hired'}
    if status=='RUNNING' and fresh and activity:
        if (epoch(activity.get('expiresAt')) or 0)>=clock:
            activity.update(sampledAt=iso(clock),ageSeconds=round(max(0,clock-(epoch(activity.get('observedAt')) or clock))));return activity
    phase='working' if status=='RUNNING' and fresh else 'uncertain' if status in ('RUNNING','STARTING','CANCELLING') and not fresh else mapped.get(status,'unknown')
    s=blank();set_phase(s,phase,heartbeat if status in ('RUNNING','STARTING','CANCELLING') else epoch(managed.get('workUpdatedAt')),
        'Managed run heartbeat is current; its detailed phase is unavailable.' if phase=='working' else 'The managed work order reports '+status.lower()+'.',turnStatus='open' if phase in ('working','starting','cancelling') else status.lower())
    result=present(s,clock,'Managed work supervisor');result.update(phase=phase,label=LABELS[phase],stale=False,active=phase in ('working','starting','cancelling'))
    if phase not in ('working','starting','cancelling'):result['expiresAt']=None
    return result
