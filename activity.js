'use strict';
window.SwitchboardActivity=(()=>{
 const records=new Map(),sessions=new Map();let offline=false,pending=false,detailId=null;
 const text=(tag,cls,value)=>{const n=document.createElement(tag);n.className=cls;n.textContent=value;return n};
 const terminal=new Set(['finished','stopped','failed','accepted','review','rejected','hired','approval','dependencies','uncertain']);
 function duration(seconds){if(seconds==null)return 'Unknown';seconds=Math.max(0,Math.floor(seconds));if(seconds<60)return seconds+'s';if(seconds<3600)return Math.floor(seconds/60)+'m '+seconds%60+'s';if(seconds<86400)return Math.floor(seconds/3600)+'h '+Math.floor(seconds%3600/60)+'m';return Math.floor(seconds/86400)+'d '+Math.floor(seconds%86400/3600)+'h'}
 function state(activity){
  const a={...(activity||{phase:'unknown',label:'Activity unknown',detail:'No provider activity evidence is available.',active:false})};
  a.detail=historicalText(a.detail);a.lastAction=historicalText(a.lastAction);
  if(a.phase==='finished'){a.label='Idle · turn ended';a.active=false;}
  const at=Date.parse(a.observedAt);a.age=Number.isFinite(at)?Math.max(0,(Date.now()-at)/1000):null;
  if(offline){a.phase='unavailable';a.label='Status unavailable';a.active=false;a.detail='The activity feed could not be refreshed. '+(activity?'Last recorded: '+activity.label+'. ':'')+(a.detail||'')}
  else if(a.expiresAt&&Date.now()>Date.parse(a.expiresAt)&&!terminal.has(a.phase)){a.phase='quiet';a.label='No recent update';a.active=false;a.stale=true;}
  return a;
 }
 function describe(a){return [a.label,a.detail,a.observedAt?'Last signal: '+new Date(a.observedAt).toLocaleString():'No timestamp available',a.source,a.historyPartial?'Earlier history is outside the observation window.':'',a.basis].filter(Boolean).join('\n')}
 function fillBadge(n,activity){
  const a=state(activity);n.dataset.phase=a.phase;n.classList.toggle('is-active',!!a.active);n.title=describe(a);
  if(!n.firstChild){const dot=text('span','activity-dot','');dot.setAttribute('aria-hidden','true');n.append(dot,text('span','activity-label',''),text('span','activity-age',''));}
  const label=n.querySelector('.activity-label'),age=n.querySelector('.activity-age');if(label.textContent!==a.label)label.textContent=a.label;
  age.textContent=a.age==null?'':duration(a.age)+' ago';age.title='Time since the last recorded activity signal';
  n.setAttribute('aria-label',a.label+(a.age==null?'':', last signal '+duration(a.age)+' ago'));
 }
 function historicalText(value){return String(value||'').replace(/; (?:the turn is continuing|continuing the turn)\.?/gi,'.').replace(/Last turn finished/g,'Turn ended')}
 function modelView(s,a){
  const observed=a.model&&a.modelObservedAt;
  const raw=observed?a.model:s.model, effort=observed?a.reasoning:s.reasoning;
  const names={'gpt-6-astra':'GPT-6 Astra','gpt-5.6-sol':'GPT-5.6 Sol','gpt-5.6-terra':'GPT-5.6 Terra','gpt-5.6-luna':'GPT-5.6 Luna','gpt-daybreak-blue-latest':'Daybreak','claude-opus-4-6':'Claude Opus 4.6','claude-sonnet-4-6':'Claude Sonnet 4.6'};
  const safe=v=>typeof v==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,99}$/.test(v)?v:null;
  const model=safe(raw),reason=safe(effort);
  const started=Date.parse(a.turnStartedAt),at=Date.parse(a.modelObservedAt);
  const current=observed&&a.turnStatus==='open'&&Number.isFinite(started)&&at>=started;
  const basis=observed?(current?'Observed in this turn':'Last observed model'):'Configured model; execution not confirmed';
  return {label:(model?(names[model]||model):'Model unknown')+' '+(reason?reason[0].toUpperCase()+reason.slice(1):'Reasoning unknown'),basis,at:a.modelObservedAt||null};
 }
 function assignmentView(s,a){
  const task=s.currentAssignment;
  const exact=task&&task.owner===s.agent_id&&task.taskId&&s.currentTaskId===task.taskId&&task.turnId&&task.turnId===a.turnId;
  const states={RETURNED:'Output returned · awaiting review',REVIEWED:'Reviewed · awaiting installation',CANDIDATE:'Candidate ready',REVIEW:'Awaiting review',AUDITED:'Reviewed · awaiting installation',INSTALLED:'Installed · awaiting verification',REVISE:'Corrections required',BLOCKED:'Blocked',WORKING:'Work in progress',QUEUED:'Work queued'};
  if(!exact)return {taskId:null,label:'Assignment not linked',detail:s.latestTask?'Latest registered work: '+s.latestTask.task_id+' · '+s.latestTask.status+' · '+(s.latestTask.updated_at||'date unknown')+'. This record is not linked to the observed turn.':'No exact assignment is linked to this turn.',progress:null,recipient:null};
  const accepted=task.status==='ACCEPTED'&&typeof task.acceptanceEvidence==='string'&&task.acceptanceEvidence;
  const label=accepted?'Assignment accepted':states[task.status]||'Assignment state unknown';
  const p=task.progress,at=Date.parse(p?.observedAt),age=(Date.now()-at)/1000;
  const measured=p&&p.kind==='measured'&&p.taskId===task.taskId&&p.source&&Number.isFinite(p.completed)&&Number.isFinite(p.total)&&p.total>0&&p.completed>=0&&p.completed<=p.total&&age>=-5&&age<=60;
  const progress=measured?{completed:p.completed,total:p.total,unit:typeof p.unit==='string'?p.unit:'units'}:null;
  return {taskId:task.taskId,label,detail:task.taskId+' · '+(task.updatedAt||'date unknown')+(accepted?' · '+task.acceptanceEvidence:''),progress,recipient:typeof task.recipientAgentId==='string'?task.recipientAgentId:null};
 }
 function fillCard(host,s){
  const raw=records.get(s.agent_id)||{},a=state(raw),model=modelView(s,raw),work=assignmentView(s,raw);
  const m=host.querySelector('.agent-model-label');m.textContent=model.label;m.parentElement.title=model.basis+(model.at?' · '+new Date(model.at).toLocaleString():'')+'. The white bolt identifies model information; it does not assert Fast or priority service.';
  m.parentElement.dataset.basis=model.basis;
  const action=host.querySelector('.agent-current-action'),history=!a.active&&!!(a.lastAction||a.detail);
  const message=(history?'Last action: ':'Now: ')+(history?(a.lastAction||a.detail):(a.detail||'No current action observed.'));
  action.textContent=message;action.title=message;action.dataset.historical=String(history);
  const update=host.querySelector('.agent-public-update');update.hidden=!raw.publicAction;update.textContent=raw.publicAction?('Last public update: '+raw.publicAction):'';update.title=raw.publicActionAt?new Date(raw.publicActionAt).toLocaleString():'';
  const dependency=host.querySelector('.agent-dependency');
  const request=s.pendingRequest,linked=request&&request.owner===s.agent_id&&request.turnId===raw.turnId&&request.id;
  dependency.textContent=linked?('Request '+request.id+' · '+(request.state||'State unknown')+' · '+(request.recipientAgentId||'Recipient not reported')):(a.phase==='input'||a.phase==='approval'?'Input requested · recipient not reported':'');
  dependency.hidden=!dependency.textContent;
  const assignment=host.querySelector('.agent-work-summary');assignment.textContent=work.label;if(work.taskId)assignment.dataset.taskId=work.taskId;else delete assignment.dataset.taskId;assignment.title=work.detail+(work.recipient?' · Recipient: '+work.recipient:' · Recipient not reported');
  const progress=host.querySelector('.agent-progress');progress.replaceChildren();
  if(work.progress&&!offline){const p=work.progress;progress.append(text('span','',p.completed+' / '+p.total+' '+p.unit));const bar=document.createElement('progress');bar.value=p.completed;bar.max=p.total;bar.setAttribute('aria-label','Measured assignment progress');progress.append(bar)}
  else if(raw.planProgress){const p=raw.planProgress;progress.append(text('span','',(a.active?'Reported plan: ':'Last reported plan: ')+p.completed+' / '+p.total+' steps'));progress.title=p.basis+' · '+new Date(p.observedAt).toLocaleString()}
  else{progress.textContent='Progress not reported';progress.title='No fresh, exact assignment measurement or reported checklist is available.'}
 }
 function badge(s){
  sessions.set(s.agent_id,s);if(s.activity)records.set(s.agent_id,s.activity);
  const host=text('span','agent-observation','');host.dataset.observationId=s.agent_id;
  const n=text('span','activity-badge','');n.dataset.activityId=s.agent_id;fillBadge(n,records.get(s.agent_id));host.append(n);
  const model=text('span','agent-model','');const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('agent-model-bolt');svg.setAttribute('viewBox','0 0 16 16');svg.setAttribute('aria-hidden','true');const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d','M9.1 1.5 3.2 8.4a.6.6 0 0 0 .46.99h3.21L6.35 14.1a.45.45 0 0 0 .79.34l5.87-6.87a.6.6 0 0 0-.46-.99H9.33l.56-4.72a.45.45 0 0 0-.79-.36Z');svg.append(path);model.append(svg,text('span','agent-model-label',''));host.append(model,text('span','agent-current-action',''),text('span','agent-public-update',''),text('span','agent-work-summary',''),text('span','agent-dependency',''),text('span','agent-progress',''));fillCard(host,s);return host;
 }
 function details(s){
  detailId=s.agent_id;if(!records.has(s.agent_id))records.set(s.agent_id,s.activity);
  const host=document.getElementById('session-status');host.replaceChildren();host.className='activity-details';host.dataset.activitySession=s.agent_id;
  const current=badge(s);current.classList.add('activity-current');host.append(current,text('p','activity-description',''));
  const list=document.createElement('dl');for(const [key,label] of [['source','Observed through'],['signal','Last activity signal'],['turn','Turn timing'],['finished','Last finished turn'],['action','Last tool event'],['model','Model evidence'],['lineage','Parent session']]){const value=text('dd','','');value.dataset.activityField=key;list.append(text('dt','',label),value)}host.append(list);
  host.dataset.model=[s.model,s.reasoning].filter(Boolean).join(' / ')||'Not reported';updateDetails();
 }
 function updateDetails(){
  const host=document.getElementById('session-status');if(!host||!detailId)return;
  const a=state(records.get(detailId)),raw=records.get(detailId)||{},start=Date.parse(raw.turnStartedAt),end=Date.parse(raw.turnEndedAt);
  const session=sessions.get(detailId)||{};
  const values={lineage:session.lineage?.agentId===detailId&&session.lineage?.source&&session.lineage?.parentAgentId?session.lineage.parentAgentId+' · '+session.lineage.source:'Not reported; placement is not provider parentage.',source:a.source||'Unavailable',action:historicalText(raw.lastAction)||'No tool completion observed in this turn.',signal:a.observedAt?new Date(a.observedAt).toLocaleString()+' · '+duration(a.age)+' ago':'Not observed',
   turn:Number.isFinite(start)?(Number.isFinite(end)?'Last turn lasted '+duration((end-start)/1000):'Turn began '+new Date(start).toLocaleTimeString()+' · '+duration((Date.now()-start)/1000)+' elapsed'):(raw.turnStatus==='open'?'Turn start was outside the sampled history.':raw.turnStatus==='queued'?'Request recorded; turn start not confirmed.':'No complete turn timing available.'),
   finished:raw.lastFinishedAt?new Date(raw.lastFinishedAt).toLocaleString()+(raw.lastFinishedDurationSeconds!=null?' · '+duration(raw.lastFinishedDurationSeconds):''):'Not observed in the available history.',model:modelView(sessions.get(detailId)||{},raw).label+' · '+modelView(sessions.get(detailId)||{},raw).basis};
  const description=host.querySelector('.activity-description');const desc=(a.phase==='quiet'?'Last recorded: '+(raw.label||raw.recordedPhase||'activity')+'. ':'')+(a.detail||'');if(description.textContent!==desc)description.textContent=desc;
  host.querySelectorAll('[data-activity-field]').forEach(n=>{const value=values[n.dataset.activityField];if(n.textContent!==value)n.textContent=value});
 }
 function render(){document.querySelectorAll('[data-activity-id]').forEach(n=>fillBadge(n,records.get(n.dataset.activityId)));document.querySelectorAll('[data-observation-id]').forEach(n=>{const s=sessions.get(n.dataset.observationId);if(s)fillCard(n,s)});updateDetails()}
 async function poll(){
  if(pending||document.hidden)return;pending=true;const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),8000);
  try{const params=new URLSearchParams();[...new Set([...document.querySelectorAll('[data-activity-id]')].map(n=>n.dataset.activityId))].slice(0,200).forEach(id=>params.append('agent',id));const r=await fetch('/api/workflow/activity?'+params,{cache:'no-store',signal:abort.signal});if(!r.ok)throw Error('Activity unavailable');const result=await r.json();if(result.schemaVersion!=='ke.workflow-activity.v1'||!result.activities)throw Error('Invalid activity feed');
   for(const id of params.getAll('agent'))records.set(id,result.activities[id]||{phase:'unavailable',label:'Status unavailable',active:false,detail:'This exact session was absent from the activity response.'});offline=false;
  }catch(e){offline=true}finally{clearTimeout(timer);pending=false;render()}
 }
 document.addEventListener('DOMContentLoaded',()=>{
  const help=text('button','activity-help','Activity key');help.type='button';help.onclick=()=>{
   let dialog=document.getElementById('activity-key');if(!dialog){dialog=document.createElement('dialog');dialog.id='activity-key';const top=text('div','dialog-top','');const close=text('button','','×');close.type='button';close.setAttribute('aria-label','Close activity key');close.onclick=()=>dialog.close();top.append(text('h2','','What activity means'),close);dialog.append(top);
    const definitions=[['Thinking…','Recent reasoning activity. The content of the reasoning stays private.'],['Working… / Running a command…','Tool execution or a continuing turn. More specific command, file and search events are shown when available.'],['Responding…','The agent recently produced or streamed a message.'],['Waiting… / Needs your input','A recorded wait or input request. This is separate from reasoning or tool execution.'],['Idle · turn ended','A turn completion event was recorded. The project or its background jobs may still be unfinished.'],['No recent update','The last active signal is over 60 seconds old. Current activity is unconfirmed.'],['Status unavailable','The provider log or activity feed cannot be read.'],['Awaiting your review','Managed work has returned and is waiting for your acceptance.']];
    for(const [label,meaning] of definitions){const row=text('div','activity-definition','');row.append(text('strong','',label),text('p','',meaning));dialog.append(row)}dialog.append(text('p','hint','Updated every few seconds from local logs and managed runtime events. Hover over a status or open a session for timestamps, source and turn timing.'));document.body.append(dialog)}dialog.showModal();
  };document.querySelector('.tray-title')?.append(help);poll();
 });
 setInterval(poll,3000);setInterval(()=>{if(!document.hidden)render()},1000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
 return {badge,details,poll,modelView,assignmentView};
})();
