"""Exact existing-task start. Desktop owner first; verified idle resume if unloaded.

No steer, new task, model override, or replay after an uncertain start.
"""
from __future__ import annotations
from workflow_handoff_presentation import marker_matches
import contextlib
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import os
import time
UUID=re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')

class StartNotAccepted(RuntimeError):
    """Only an explicit router rejection, never a timeout or unknown reply."""

def qualified_resume_command():
    """Host-pinned desktop resume binaries are not part of this release."""
    raise ValueError('Pinned desktop resume runtimes are not included; requalification is required')

class DesktopTransport:
    def __init__(self,flow,client_factory=None,rpc_factory=None):
        self.flow=flow
        self.client_factory=client_factory
        self.rpc_factory=rpc_factory
        self.sessions={}
    def rpc(self):
        if self.rpc_factory:return self.rpc_factory()
        raise ValueError('No provider transport is configured')
    def resume_rpc(self):
        if self.rpc_factory:return self.rpc_factory()
        raise ValueError('No provider transport is configured')
    @contextlib.contextmanager
    def prepare(self,target,force_resume=False):
        if target.get('provider')!='codex':
            raise ValueError('This provider has no verified idle-only desktop start adapter; recipient remains visible for repair.')
        endpoint=str(target.get('endpoint',''))
        if not UUID.fullmatch(endpoint):raise ValueError('Exact provider task identity is invalid')
        cwd=Path(target.get('cwd') or '')
        if not cwd.is_absolute() or not cwd.is_dir():raise ValueError('Exact recipient workspace is unavailable')
        if force_resume:
            with self.prepare_unloaded(target) as prepared:yield prepared
            return
        if self.client_factory is None:
            raise ValueError('Installed desktop transport is unavailable')
        else:factory=self.client_factory
        client=factory(self.flow.codex_home/'ipc/ipc.sock')
        try:
            try:
                discovery=client.request('thread-owner-discovery',{'hostId':'local','conversationId':endpoint},version=1,timeout_ms=5000)
            except Exception as error:
                # Only a failed read-only discovery permits the unloaded-task path.
                # Invalid socket ownership/protocol and arbitrary errors stay held.
                if getattr(error,'code',None) not in ('desktop_ipc_timeout','desktop_ipc_read_failed'):
                    raise
                discovery={'resultType':'error','error':'owner-discovery-timeout'}
            owner=str(discovery.get('handledByClientId') or '').lower()
            if discovery.get('resultType')=='success' and UUID.fullmatch(owner):
                yield {'client':client,'owner':owner,'endpoint':endpoint,'cwd':str(cwd)}
            elif discovery.get('resultType')=='error' and discovery.get('error') in ('no-client-found','owner-discovery-timeout'):
                client.close()
                with self.prepare_unloaded(target) as prepared:yield prepared
            else:
                raise ValueError('The exact recipient has no available desktop owner; discovery response is unverified.')
        finally:client.close()
    @contextlib.contextmanager
    def prepare_unloaded(self,target):
        endpoint=target['endpoint']
        if endpoint in self.sessions:raise ValueError('An existing handoff retains this recipient; reconcile it first')
        if not target.get('model') or not target.get('reasoning'):
            raise ValueError('Saved model and reasoning are required before reopening an unloaded task')
        rpc=self.resume_rpc();prepared=None
        try:
            before=rpc.call('thread/read',{'threadId':endpoint,'includeTurns':False},15).get('thread',{})
            if before.get('id')!=endpoint or before.get('cwd')!=target['cwd']:
                raise ValueError('Exact stored recipient/workspace does not match')
            if before.get('status',{}).get('type')!='notLoaded':
                raise ValueError('Recipient is loaded or active; wait for desktop ownership, never take over')
            # Only the qualified runtime may acquire the provider's writer lock.
            # Unknown, timed-out or rejected acquisition never reaches turn/start.
            result=rpc.call('thread/resume',{'threadId':endpoint,'excludeTurns':True},20)
            thread=result.get('thread',{})
            if thread.get('id')!=endpoint or thread.get('status',{}).get('type')!='idle' or result.get('cwd')!=target['cwd']:
                raise ValueError('Resumed recipient is not the exact idle task')
            if (result.get('model'),result.get('reasoningEffort'),result.get('modelProvider'))!=(target['model'],target['reasoning'],before.get('modelProvider')):
                raise ValueError('Resumed model, reasoning or provider differs from saved recipient settings')
            if result.get('approvalPolicy')!='never' or result.get('sandbox',{}).get('type')!='dangerFullAccess':
                raise ValueError('Resumed permissions do not match the approved local workflow')
            prepared={'rpc':rpc,'endpoint':endpoint,'cwd':target['cwd'],'sent':False,
                      'settings':{k:result.get(k) for k in ('model','modelProvider','reasoningEffort','approvalPolicy','sandbox','serviceTier')}}
            yield prepared
        finally:
            # A start may have reached the provider even when its reply was lost.
            # Retain that connection for reconciliation; never kill it as a retry.
            if not prepared or not prepared['sent']:
                rpc.close()
                if prepared and prepared.get('registration'):
                    try:self.publish_process(prepared['registration'],'cancelled')
                    except OSError:pass
    def before_send(self,p,request_id):
        """Required bookkeeping before consuming a possible provider write."""
        if 'rpc' in p:
            session={**p,'requestId':request_id,'startedAt':time.time()}
            self.publish_process(session,'prepared')
            p['registration']=session
    def start_receipt(self,p,request_id):
        if 'rpc' not in p:return {}
        return {'transport':'codex-app-server-exact-idle-resume-v1',
                'clientUserMessageId':request_id,'conversationId':p['endpoint'],'settings':p['settings']}
    def publish_process(self,session,status):
        process=getattr(session['rpc'],'process',None)
        if not process:return
        from workflow_handoffs import atomic_json
        from store import cpu_registry
        import shutil
        path=cpu_registry()/('handoff-'+session['requestId']+'.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        atomic_json(path,{'schemaVersion':'ke.cpu-job.v2','jobId':'handoff-'+session['requestId'],
            'owner':'codex:'+session['endpoint'],'parentThreadId':session['endpoint'],
            'title':'Existing task handoff: '+session['endpoint'],'pid':process.pid,'parentPid':os.getpid(),
            'pgid':process.pid,'command':[shutil.which('codex') or 'codex','app-server'],
            'status':status,'startedAt':session['startedAt'],'updatedAt':time.time(),'etaSeconds':None,
            'bindingConstraint':'One authorized existing model turn; this process is transport, not a PID audit',
            'workers':[{'pid':process.pid,'id':session['requestId'],'status':status,'assignedWork':'Run and observe the exact accepted handoff'}]})
    def start_unloaded(self,p,message,request_id):
        if 'registration' not in p:self.before_send(p,request_id)
        p['sent']=True
        self.sessions[p['endpoint']]=p['registration']
        result=p['rpc'].call('turn/start',{'threadId':p['endpoint'],
            'input':[{'type':'text','text':message,'text_elements':[]}],
            'clientUserMessageId':request_id},20)
        turnid=str(result.get('turn',{}).get('id',''))
        if not UUID.fullmatch(turnid):raise ValueError('Resumed provider returned no exact turn ID')
        self.sessions[p['endpoint']]['turnId']=turnid
        process=getattr(p['rpc'],'process',None)
        return {'turnId':turnid,'clientUserMessageId':request_id,'conversationId':p['endpoint'],
                'transport':'codex-app-server-exact-idle-resume-v1','settings':p['settings'],
                'runtimePid':getattr(process,'pid',None)}
    def start(self,p,message,request_id):
        if "rpc" in p:return self.start_unloaded(p,message,request_id)
        # Version 2 is the installed app's existing start protocol. Starting cannot steer
        # another active turn. Provider rejection or lost acknowledgement is never retried.
        response=p['client'].request('thread-follower-start-turn',{
            'conversationId':p['endpoint'],
            'turnStart':{'request':{'threadId':p['endpoint'],
                'input':[{'type':'text','text':message,'text_elements':[]}],
                'clientUserMessageId':request_id,'cwd':p['cwd']},
                'context':{'inheritThreadSettings':True,'useAppServerPermissionDefault':True}}},
            version=2,target_client_id=p['owner'],timeout_ms=12000)
        if response.get('resultType')=='error' and response.get('error')=='no-client-found':
            raise StartNotAccepted('Desktop router explicitly rejected the stale owner before accepting a turn')
        if response.get('resultType')!='success':
            raise ValueError('Desktop did not confirm start: '+str(response)[:600])
        if response.get('handledByClientId')!=p['owner'] or response.get('method') not in (None,'thread-follower-start-turn'):
            raise ValueError('Desktop receipt did not match the exact discovered task owner')
        result=response.get('result') or {}
        # Desktop IPC uses result.result for a targeted response in current builds.
        if isinstance(result,dict) and isinstance(result.get('result'),dict):result=result['result']
        turn=result.get('turn') if isinstance(result.get('turn'),dict) else result
        turnid=str(turn.get('id') or turn.get('turnId') or result.get('turnId') or '').lower()
        if not UUID.fullmatch(turnid):raise ValueError('Desktop returned no exact turn ID')
        return {'turnId':turnid,'clientUserMessageId':request_id,'conversationId':p['endpoint'],
                'ownerClientId':p['owner'],'transport':'codex-desktop-start-only-v2','settings':'inherited'}
    def observation_binding(self,target,request_id,receipt):
        for key,expected in [('conversationId',target['endpoint']),('clientUserMessageId',request_id)]:
            if key in receipt and receipt[key]!=expected:
                raise ValueError('Observation identity mismatch: '+key+'; no release or resend')
        return ((receipt.get('conversationId') or target['endpoint'])==target['endpoint'] and
                (receipt.get('clientUserMessageId') or request_id)==request_id)
    def observe_paged(self,target,request_id,receipt):
        bound=self.observation_binding(target,request_id,receipt)
        if target.get('provider')!='codex' or not UUID.fullmatch(target.get('endpoint','')):
            raise ValueError('Exact Codex recipient required for history observation')
        session=self.sessions.get(target['endpoint']);owned=session is not None
        if owned and (session.get('endpoint')!=target['endpoint'] or session.get('requestId')!=request_id):
            raise ValueError('Owned observation session does not match original request')
        tid=receipt.get('turnId') or (session or {}).get('turnId')
        if tid and not UUID.fullmatch(str(tid)):raise ValueError('Invalid delivered turn identity')
        if owned and receipt.get('turnId') and session.get('turnId') and receipt['turnId']!=session['turnId']:
            raise ValueError('Owned observation turn conflicts with original receipt')
        if tid and not (bound or owned):
            raise ValueError('Exact delivered turn lacks request/recipient binding; observation held')
        search=receipt.get('historySearch') or {}
        if not isinstance(search,dict):raise ValueError('Invalid saved history search')
        if search.get('requestId')!=request_id:search={}
        if search.get('threadId',target['endpoint'])!=target['endpoint'] or search.get('turnId',tid)!=tid:
            raise ValueError('Saved history search identity mismatch')
        cursor=search.get('cursor')
        if cursor is not None and (not isinstance(cursor,str) or not cursor or len(cursor)>4096):
            raise ValueError('Invalid saved history cursor')
        pages=search.get('pages',0) if cursor else 0
        visited=search.get('visited',[]) if cursor else []
        if not isinstance(pages,int) or not 0<=pages<256 or not isinstance(visited,list) or len(visited)>256 or any(not isinstance(v,str) for v in visited):
            raise ValueError('History search bound exceeded or invalid; observation held')
        candidate=search.get('candidate') if cursor and not tid else None
        if candidate is not None and (not isinstance(candidate,dict) or set(candidate)!={'id','status'} or not UUID.fullmatch(str(candidate['id']))):
            raise ValueError('Invalid saved marker candidate')
        rpc=session['rpc'] if owned else self.rpc()
        try:
            if owned:
                import queue
                for _ in range(10000):
                    try:rpc.events.get_nowait()
                    except (AttributeError,queue.Empty):break
            params={'threadId':target['endpoint'],'limit':10,'itemsView':'notLoaded' if tid else 'full','sortDirection':'desc'}
            if cursor:params['cursor']=cursor
            result=rpc.call('thread/turns/list',params,15)
            if not isinstance(result,dict) or not isinstance(result.get('data'),list) or len(result['data'])>10:
                raise ValueError('Invalid bounded history page; observation held')
            next_cursor=result.get('nextCursor')
            if next_cursor is not None and (not isinstance(next_cursor,str) or not next_cursor or len(next_cursor)>4096 or next_cursor==cursor or next_cursor in visited):
                raise ValueError('History pagination did not advance; no resend permitted')
            matches=[]
            for turn in result['data']:
                if not isinstance(turn,dict):raise ValueError('Invalid history turn')
                if tid and turn.get('id')!=tid:continue
                if not tid:
                    found=False
                    for item in turn.get('items',[]):
                        if not isinstance(item,dict) or item.get('type')!='userMessage':continue
                        body='\n'.join(x.get('text','') for x in item.get('content',[]) if isinstance(x,dict))
                        if marker_matches(body,request_id):found=True
                    if not found:continue
                if not UUID.fullmatch(str(turn.get('id',''))):raise ValueError('Invalid exact history turn identity')
                if any(turn.get(k,target['endpoint'])!=target['endpoint'] for k in ('threadId','conversationId')):
                    raise ValueError('History turn recipient mismatch')
                matches.append({'id':turn['id'],'status':turn.get('status')})
            if len(matches)>1:raise ValueError('Ambiguous handoff history; no release permitted')
            if matches:
                if candidate is not None and candidate!=matches[0]:raise ValueError('Ambiguous handoff marker across history pages')
                candidate=matches[0]
            # A trusted exact turn can resolve on its page. Marker-only recovery
            # completes the bounded scan before treating its candidate as unique.
            found=candidate if (tid or not next_cursor) else None
            observed={'inProgress':'RUNNING','completed':'RETURNED','failed':'FAILED','interrupted':'FAILED'}.get((found or {}).get('status'))
            if observed:
                warning=None
                if owned and observed in ('RETURNED','FAILED'):
                    try:self.publish_process(session,'completed' if observed=='RETURNED' else 'failed')
                    except OSError as error:warning=str(error)
                    finally:rpc.close();self.sessions.pop(target['endpoint'],None)
                if owned and observed=='RUNNING':
                    try:self.publish_process(session,'running')
                    except OSError as error:warning=str(error)
                return {'status':observed,'detail':'Exact provider turn observed through paginated history; result approval remains separate.',
                        'receipt':{'turnId':found['id'],'observedTurnId':found['id'],'conversationId':target['endpoint'],
                                   'clientUserMessageId':request_id,'basis':'supported thread/turns/list',
                                   'historySearch':None,'registryWarning':warning}}
            return {'detail':'Exact handoff lifecycle unresolved; '+('continuing from saved cursor.' if next_cursor else 'history exhausted without a unique recognized lifecycle; observation only, never resend.'),
                    'receipt':{'historySearch':{'requestId':request_id,'threadId':target['endpoint'],'turnId':tid,
                        'cursor':next_cursor,'pages':pages+1,'visited':visited+([cursor] if cursor else []),
                        'candidate':candidate if not tid else None,'exhausted':not bool(next_cursor)}}}
        finally:
            if not owned:rpc.close()
    def observe(self,target,request_id,receipt):
        bound=self.observation_binding(target,request_id,receipt)
        if target.get('provider')=='codex' and (target.get('endpoint') in self.sessions or receipt.get('transport')=='codex-app-server-exact-idle-resume-v1'):
            return self.observe_paged(target,request_id,receipt)
        # Read only the exact registered provider log. Never infer success from a PID.
        if target.get('provider')!='codex':return {}
        with sqlite3.connect((self.flow.codex_home/'state_5.sqlite').as_uri()+'?mode=ro',uri=True) as db:
            row=db.execute('SELECT rollout_path FROM threads WHERE id=?',(target['endpoint'],)).fetchone()
        if not row:return {}
        path=Path(row[0]);a=self.flow.activity_reader.observe(str(path))
        tid=receipt.get('turnId')
        # Lost send acknowledgements are reconciled by the unique request marker in a
        # real user message, never by text in an assistant quote or a tool result.
        marker='HANDOFF '+request_id
        with path.open('rb') as f:
            f.seek(0,2);size=f.tell();f.seek(max(0,size-16*1024*1024))
            if size>16*1024*1024:f.readline()
            lines=f.read().decode('utf-8',errors='replace').splitlines()
        turn=None;seen=False;found_turn=tid;terminal=None;marker_turns=set()
        for line in lines:
            try:e=json.loads(line)
            except ValueError:continue
            p=e.get('payload') or {};kind=e.get('type')
            if not isinstance(p,dict):continue
            if kind=='event_msg' and p.get('type')=='task_started':turn=p.get('turn_id')
            if kind=='turn_context':
                turn=p.get('turn_id') or turn
                if seen and not found_turn:found_turn=turn
            body=None
            if kind=='event_msg' and p.get('type')=='user_message':body=p.get('message')
            elif kind=='response_item' and p.get('type')=='message' and p.get('role')=='user':
                body='\n'.join(x.get('text','') for x in p.get('content',[]) if isinstance(x,dict))
            if isinstance(body,str) and marker_matches(body,request_id):
                seen=True;found_turn=found_turn or turn
                if turn:marker_turns.add(turn)
            if kind=='event_msg' and p.get('type') in ('task_complete','turn_aborted','task_failed') and p.get('turn_id')==found_turn and found_turn:
                terminal='RETURNED' if p['type']=='task_complete' else 'FAILED'
        if len(marker_turns)>1 or (seen and tid and marker_turns and marker_turns!={tid}):
            raise ValueError('Ambiguous or conflicting local handoff marker; observation held')
        # A bound latest-turn receipt is still only one observation. Reconcile
        # all available request-marker conflicts before any local status return.
        if tid and bound and a.get('turnId')==tid:
            status={'open':'RUNNING','finished':'RETURNED','failed':'FAILED','stopped':'FAILED'}.get(a.get('turnStatus'))
            if status:return {'status':status,'detail':'Exact delivered provider turn '+a['turnStatus']+'. Task acceptance and installation remain separate.','receipt':{'observedTurnId':tid,'observedAt':a.get('observedAt')}}
        if seen and found_turn and marker_turns=={found_turn}:
            proof={**receipt,'turnId':found_turn,'clientUserMessageId':request_id,'conversationId':target['endpoint'],
                   'basis':'exact user message in provider transcript'}
            if isinstance(proof.get('historySearch'),dict) and proof['historySearch'].get('turnId') is None:
                proof['historySearch']=None  # New exact binding uses a fresh notLoaded history view.
            status=terminal or ('RUNNING' if a.get('turnStatus')=='open' and a.get('turnId')==found_turn else None)
            if status:return {'status':status,'detail':'Exact handoff lifecycle observed in registered transcript.','receipt':proof}
            if UUID.fullmatch(target['endpoint']):
                observation=self.observe_paged(target,request_id,proof)
                return {**observation,'receipt':{**proof,**observation.get('receipt',{})}}
            return {'status':'ACCEPTED','detail':'Exact user marker observed; lifecycle unresolved.','receipt':proof}
        return self.observe_paged(target,request_id,receipt) if UUID.fullmatch(target['endpoint']) else {}
