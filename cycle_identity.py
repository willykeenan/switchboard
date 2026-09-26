"""Purpose-bound local credentials for routine TaskFlow operations, never starts.

The shared UI token never becomes an owner, reviewer or operator principal here.
Local same-UID processes can read each other's files: this is scoped application
authorization, not cryptographic isolation from the macOS account owner.
"""
import hashlib
import ipaddress
import json
import math
import secrets
import time
from urllib.parse import urlparse
from settings_identity import GrantStore
from agent_settings import Denied
from workspace import Conflict
from workflow_layout import active,team_of

def bound(store,subject,lane):
    flow=store.flow
    if hasattr(flow,'_catalog_lock'):
        with flow._catalog_lock:flow._cached=None
    state=flow.snapshot();sessions=state['sessions']
    matches=[s for s in sessions if s['agent_id']==subject or s.get('provider','')+':'+str(s.get('endpoint',''))==subject]
    if len(matches)!=1:raise Denied('Registered cycle identity is missing or ambiguous')
    member=matches[0];actor=member['agent_id']
    seat=next((p for p in state['placements'] if p['agentId']==actor),None)
    lane_record=next((l for l in state['lanes'] if l['id']==lane and active(l)),None)
    if not state.get('enabled') or not seat or not lane_record or seat['laneId']!=lane or member.get('provider','codex')!='codex' or not member.get('endpoint'):raise Denied('Current registered provider/lane binding is unavailable')
    ops={'context'}
    owner=seat['role']=='coordinator' and any(p['agentId']==actor and p['laneId']==lane and p['teamId']=='coordinator' for p in state.get('teamLeads',[]))
    policy=store.policy(lane)
    if owner:ops|={'bind-legacy-origin','capture','ready','delivery-configure','review-setup-return','accept-setup-return','learning-prepare','learning-retain','learning-context'}
    if policy and store._is_attendant(policy,actor):ops.add('ready')
    with store.connect() as db:w=store._one(db,'taskflow_workers',actor)
    if w and w['enabled'] and w['laneId']==lane and w['role']==seat['role'] and w['teamId']==team_of(seat):
        if w.get('purpose','work')=='audit':ops|={'audit-library','audit-context','audit-question','audit-resolve','review'}
        elif w.get('purpose','work')=='work':ops|={'ready','progress','return','fail','learning-context'}
    if len(ops)==1 and not owner:raise Denied('No saved routine cycle role is enabled')
    return {'subject':actor,'endpoint':member['endpoint'],'provider':'codex','lane':lane,
            'role':seat['role'],'team':team_of(seat),'operations':sorted(ops)}

class CycleGrants(GrantStore):
    def issue_cycle(self,store,subject,lane,ttl=3600):
        if type(ttl) is not int or not 1<=ttl<=28800:raise ValueError('Credential lifetime must be 1–28800 seconds')
        identity=bound(store,subject,lane);token=secrets.token_urlsafe(32);key=hashlib.sha256(token.encode()).hexdigest()
        self._edit(lambda d:d.__setitem__(key,{**identity,'expires':time.time()+ttl,'purpose':'routine-taskflow-v1'}))
        return token,key

    def resolve(self,store,token,operation):
        if not isinstance(token,str) or not 20<=len(token)<=256:raise Denied('A scoped cycle credential is required')
        g=self.read().get(hashlib.sha256(token.encode()).hexdigest())
        if not g or g.get('purpose')!='routine-taskflow-v1':raise Denied('Cycle credential is unknown or revoked')
        expiry=g.get('expires')
        if not isinstance(expiry,(int,float)) or not math.isfinite(expiry) or expiry<=time.time():raise Denied('Cycle credential expired')
        identity=bound(store,g['subject'],g['lane'])
        # Adding a new routine operation must not revoke all existing credentials
        # or silently grant the new operation to their holders. A removed operation
        # still invalidates the credential, preserving the existing revocation rule.
        granted=g.get('operations')
        if (any(g.get(k)!=v for k,v in identity.items() if k!='operations')
                or not isinstance(granted,list) or any(not isinstance(op,str) for op in granted)
                or not set(granted)<=set(identity['operations'])):
            raise Denied('Cycle credential provider, lane, role or grant changed')
        if operation not in granted:raise Denied('Routine role does not permit this operation; no start or acceptance authority inferred')
        return {**identity,'operations':granted}

class CycleRoutes:
    def __init__(self,store,grants,intake=None):
        from taskflow_intake_owner import OwnerIntake
        self.s=store;self.grants=grants;self.intake=intake or OwnerIntake(store)

    def execute(self,token,op,item):
        if not isinstance(item,dict):raise ValueError('Cycle item must be an object')
        actor=self.grants.resolve(self.s,token,op);subject=actor['subject']
        if 'actor' in item or 'role' in item or 'principal' in item:raise Denied('Actor identity is server-bound, not a request field')
        if item.get('taskId'):
            task=self.s.get(item['taskId'])
            if not task or task['laneId']!=actor['lane']:raise Denied('Task is outside the credential lane')
        if op=='bind-legacy-origin':
            from taskflow_origin import bind
            return bind(self.s,self.grants,token,item)
        if op=='context':return self.s.context(subject)
        if op=='capture':
            if item.get('laneId')!=actor['lane']:raise Denied('Intake lane differs from credential')
            return self.intake.capture(subject,item)
        if op in ('review-setup-return','accept-setup-return'):
            raise Denied('Managed production runner is not included in this release')
        if op=='ready':return self.s.ready(item['taskId'],item['version'],item,subject)
        if op in ('learning-retain','learning-context'):return self.s.mutate({'operation':op,'item':item},subject)
        if op=='learning-prepare':return self.s.learning_decision(item['taskId'],item['version'],subject,'prepare')
        if op=='delivery-configure':return self.s.delivery.configure(item['taskId'],item['version'],item['delivery'],subject)
        if op=='review':return self.s.review(item['taskId'],item['version'],subject,item['verdict'],item['summary'],item['evidence'],item.get('learningOutcome'))
        if op.startswith('audit-'):return self.s.audit.mutate(op,item,subject)
        return self.s.transition(item['assignmentId'],subject,op,item)

    def handle(self,h,method):
        if urlparse(h.path).path!='/api/taskflow-cycle':return False
        try:
            host=h.headers.get('Host','');origin='http://'+host
            if method!='POST' or host not in (f'127.0.0.1:{h.server.server_port}',f'localhost:{h.server.server_port}') or not ipaddress.ip_address(h.client_address[0]).is_loopback or h.headers.get('Origin')!=origin or h.headers.get('Sec-Fetch-Site')=='cross-site':raise Denied('Same-origin local cycle endpoint required')
            auth=h.headers.get('Authorization','')
            if not auth.startswith('Bearer '):raise Denied('Scoped cycle credential required; shared board token is not actor identity')
            size=int(h.headers.get('Content-Length','0'))
            if not 0<size<=100000 or h.headers.get('Transfer-Encoding') or h.headers.get('Content-Type')!='application/json':raise ValueError('Bounded JSON required')
            body=json.loads(h.rfile.read(size))
            if not isinstance(body,dict) or set(body)!={'operation','item'}:raise ValueError('Exact operation and item fields required')
            h.send_json(self.execute(auth[7:],body['operation'],body['item']))
        except Denied as e:h.send_json({'error':str(e),'code':'CYCLE_IDENTITY_DENIED'},403)
        except Conflict as e:h.send_json({'error':str(e),'code':'CYCLE_CONFLICT'},409)
        except (ValueError,KeyError,TypeError) as e:h.send_json({'error':str(e),'code':'CYCLE_INVALID'},400)
        return True
