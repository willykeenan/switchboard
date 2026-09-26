"""Explicit owner classification of an observed real user message; no sends/starts."""
import hashlib
from workspace import Conflict

class OwnerIntake:
    def __init__(self,store,transcripts=None):
        from inspector.transcripts import Transcripts
        self.s=store;self.transcripts=transcripts or Transcripts(store.flow)

    def capture(self,actor,item):
        state=self.s._lane(item['projectId'],item['laneId'])
        seat=next((p for p in state['placements'] if p['agentId']==actor),None)
        if not seat or seat['laneId']!=item['laneId'] or seat['role']!='coordinator':raise Conflict('The saved original lane owner must classify this request')
        if not any(x['agentId']==actor and x['laneId']==item['laneId'] and x['teamId']=='coordinator' for x in state.get('teamLeads',[])):raise Conflict('The sole appointed owner must record intake')
        if sum(p['laneId']==item['laneId'] and p['role']=='coordinator' and (p.get('teamId') or p['role'])=='coordinator' for p in state['placements'])!=1:raise Conflict('Owner identity is ambiguous; preserve existing ownership')
        kind=item.get('kind')
        if kind not in ('question','work'):raise ValueError('Owner must explicitly classify question or work')
        page=self.transcripts.page(actor,before=item.get('before',0),limit=100)
        entry=next((e for e in page['entries'] if e['id']==item.get('messageId')),None)
        if not page.get('available') or not entry or entry['role']!='user' or entry.get('sourceKind'):raise Conflict('Exact original user message must be observed in the registered owner transcript')
        if kind=='question':return {'disposition':'chat','messageId':entry['id'],'taskId':None,'executionCreated':False}
        record=self.transcripts.user_record(actor,entry) if hasattr(self.transcripts,'user_record') else {'text':entry['text'],'recordSha256':hashlib.sha256(entry['text'].encode()).hexdigest(),'providerRecordId':entry['id']}
        body=record['text']
        if not body or '[Long entry shortened in this viewer.]' in body:raise Conflict('Complete user request required; do not capture a truncated viewer excerpt')
        origin={'kind':'owner-conversation','providerThreadId':page['sourceAgentId'].split(':',1)[1],
                'messageId':record['providerRecordId'],'position':entry['position'],'recordSha256':record['recordSha256'],
                'sha256':hashlib.sha256(body.encode()).hexdigest()}
        # capturedBy is the scoped server principal. The stable source key means
        # a lost HTTP response cannot create a second task or launch anything.
        result=self.s.capture({'request':body,'projectId':item['projectId'],'laneId':item['laneId'],
            'key':'owner-message:'+page['sourceAgentId']+':'+record['providerRecordId'],'ownerId':actor,
            'sourceRefs':[origin],'inputs':item.get('inputs',[]),'delivery':item['delivery'],
            'requireOwnerReadiness':True},actor)
        return {'disposition':'captured','taskId':result['taskId'],'task':result,'executionCreated':False}
