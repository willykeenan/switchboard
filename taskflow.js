'use strict';
// Presentation queue only. Its clock never acknowledges, starts or completes work.
window.TaskFlowMotion=(()=>{
 const kinds=new Set(['offered','returned','review-accept','review-revise','review-fail','ready','audit-claimed','audit-released','delivery-offered','delivery-accept','delivery-return','delivery-fail','intake-adopted','intake-updated']);
 const valid=e=>e&&Number.isSafeInteger(e.seq)&&typeof e.taskId==='string'&&kinds.has(e.kind)&&typeof e.source==='string'&&typeof e.destination==='string'&&e.source!==e.destination&&!e.source.startsWith('transit:')&&!e.destination.startsWith('transit:')&&Number.isFinite(e.at);
 function create(){
  const queues=new Map();let watermark=0,baseline=false,omitted=0;
  const clear=id=>id===undefined?queues.clear():queues.delete(id);
  function add(e,replay=false){
   if(!valid(e))return false;
   const q=queues.get(e.taskId)||[];
   if(q.length>=40||[...queues.values()].reduce((n,items)=>n+items.length,0)>=400){omitted++;return false;}
   q.push({...e,replay,elapsed:0,lastTick:null});queues.set(e.taskId,q);return true;
  }
  function ingest(events){
   const ordered=(events||[]).filter(e=>Number.isSafeInteger(e.seq)).sort((a,b)=>a.seq-b.seq);
   for(const e of ordered){if(e.seq<=watermark)continue;if(baseline){if(e.kind==='delivery-held')clear(e.taskId);else add(e);}watermark=e.seq;}
   baseline=true;
  }
  function step(id,now,paused=false){
   const q=queues.get(id),e=q?.[0];if(!e)return null;
   if(paused){e.lastTick=null;return e;}
   if(e.lastTick!==null)e.elapsed+=Math.max(0,Math.min(250,now-e.lastTick));e.lastTick=now;
   if(e.elapsed>=6200){q.shift();if(!q.length)queues.delete(id);else q[0].lastTick=now;return q[0]||null;}return e;
  }
  return {ingest,step,clear,current:id=>queues.get(id)?.[0],count:id=>id===undefined?[...queues.values()].reduce((n,q)=>n+q.length,0):queues.get(id)?.length||0,replay(e){if(!valid(e))return false;clear(e.taskId);return add(e,true);},reset(){clear();watermark=0;baseline=false;omitted=0;},get omitted(){return omitted;}};
 }
 return {create,valid};
})();
window.TaskFlowHealth={assess(task){
 const state=task.displayState||task.state;
 const live=task.heldByWorker===true||['OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING'].includes(task.state);
 const container=(task.intakeDisposition||task.intake||{}).kind==='container';
 // Parent folders wait on children. They are not failed courier routes.
 if(container&&['NEEDS_COORDINATION','QUEUED','CAPTURED','READY'].includes(state))return {blocked:false,reason:'',state,container:true};
 const blocked=(!task.legacy||live)&&['BLOCKED','UNCERTAIN','DELIVERY_UNCERTAIN','NEEDS_VERIFICATION','STATUS_UNKNOWN','FAILED'].includes(state);
 return {blocked,reason:blocked?(task.waitReason||task.blocker||({STATUS_UNKNOWN:'Current execution is unverified',UNCERTAIN:'The worker stopped reporting; the exact assignment needs checking',FAILED:'The task failed'}[state])||'Work is blocked'):'',state,container};
}};
// One task identity powers board rows, moving/stationed icons and the inspector.
window.TaskBoard = {create(ctx) {
 const make=(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls||'';if(text!==undefined)n.textContent=text;return n;};
 const button=(label,fn,cls='')=>{const n=make('button',cls,label);n.type='button';n.onclick=fn;return n;};
 // Reuse the approved whitepaper vector; poses and document placement identify each stage.
 const birdArtwork="<svg class=\"tf-carry-bird\" data-artwork=\"whitepaper-wireframe-v3\" viewBox=\"-.55 -.65 1.45 1.5\" aria-hidden=\"true\"><g class=\"tf-bird-pose\"><path class=\"tf-bird-outline\" d=\"M0 0L-0.47 -0.56 M-0.47 -0.56L0.04 -0.31 M0.04 -0.31L0.32 -0.57 M0.32 -0.57L0.52 -0.44 M0.52 -0.44L0.72 -0.42 M0.72 -0.42L0.51 -0.29 M0.51 -0.29L0.27 0.16 M0.27 0.16L-0.18 0.24 M-0.18 0.24L-0.36 0.52 M-0.36 0.52L-0.18 0.1 M-0.18 0.1L0 0 M0 0L0.04 -0.31 M0.04 -0.31L0.52 -0.44 M0.04 -0.31L0.27 0.16 M0 0L0.27 0.16 M0 0L-0.18 0.24 M0.27 0.16L-0.18 0.1 M0.52 -0.44L0.51 -0.29\" fill=\"none\" stroke=\"#fff\" stroke-width=\".042\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/></g><g class=\"tf-paper\"><path d=\"M-.31 .56h.15l.08 .08v.15h-.23z\" fill=\"#ff4d58\" stroke=\"#ff9b9f\" stroke-width=\".018\"/><path d=\"M-.16 .56v.08h.08m-.19 .045h.14m-.14 .055h.14\" fill=\"none\" stroke=\"#ffe6e8\" stroke-width=\".014\"/></g><g class=\"tf-stage-cues\" fill=\"none\" stroke=\"#a9d6e8\" stroke-width=\".055\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path class=\"tf-cue tf-cue-stationed\" d=\"M-.42 .80H.64M-.34 .80v-.08M.56 .80v-.08\"/><path class=\"tf-cue tf-cue-pickup\" d=\"M.65 .69V.25M.52 .38l.13-.13.13.13\"/><path class=\"tf-cue tf-cue-moving\" d=\"M-.48 -.14h.26M-.52 .03h.31M-.42 .20h.17\"/><path class=\"tf-cue tf-cue-handoff\" d=\"M.65 .22v.39m-.13-.13.13.13.13-.13M.02 .72v.08h.50v-.08\"/><g class=\"tf-cue tf-cue-reduced\"><path d=\"M.10 .80s-.27-.27-.27-.45a.27 .27 0 0 1 .54 0c0 .18-.27 .45-.27 .45z\"/><path d=\"M-.40-.51v.25M-.29-.51v.25\"/></g></g></svg>";
 const birdPortrait=()=>{const n=make('span','tf-courier-portrait');n.innerHTML=birdArtwork;return n;};
 const stateName=s=>({CAPTURED:'Captured',RESERVED:'Worker acceptance recorded',STATUS_UNKNOWN:'Execution unverified',LINKED:'Linked to existing work',READY:'Ready',QUEUED:'Queued',OFFERED:'Offered',ACCEPTED:'Accepted',RUNNING:'Working',STARTING:'Starting',REVIEW:'Needs review',VERIFY:'Needs delivery verification',DELIVERY_QUEUED:'Awaiting delivery',DELIVERY_OFFERED:'Delivery offered',DELIVERING:'Delivering',DELIVERY_UNCERTAIN:'Delivery needs checking',NEEDS_VERIFICATION:'Historical completion unverified',NEEDS_COORDINATION:'Needs coordination',BLOCKED:'Blocked',UNCERTAIN:'Status needs checking',DONE:'Completed',CANCELLED:'Cancelled'}[s]||s);
 const iconLayer=make('div','tf-icon-layer');iconLayer.setAttribute('aria-label','Tasks at their current work stations');document.body.append(iconLayer);
 const svgNS='http://www.w3.org/2000/svg',paths=document.createElementNS(svgNS,'svg');paths.classList.add('tf-individual-paths');paths.setAttribute('aria-label','Paths to individual agents');document.body.append(paths);let pathSignature='';
 const svg=(tag,attrs)=>{const n=document.createElementNS(svgNS,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,String(v));return n;};
 const pane=make('section','tf-inspector');pane.setAttribute('aria-label','Selected task');
 const connection=make('div','tf-connection');connection.setAttribute('role','status');document.body.append(connection);
 let snapshot={tasks:[],events:[],workers:[],policies:[]},selected=null,selectedVersion=null,disposed=false,polling=false,generation=0,raf=null,drawTimer=null,updatesAvailable=true;
 const icons=new Map(),motionQueue=window.TaskFlowMotion.create();let scopeKey='',detailController=null,following=null;
 const travel=make('section','tf-travel-feed');travel.setAttribute('aria-label','Recorded task handoffs');document.body.append(travel);
 const on=(n,event,fn)=>n.addEventListener(event,fn);
 const scopedTasks=()=>snapshot.tasks.filter(t=>t.projectId===ctx.project()&&(!ctx.lane()||t.laneId===ctx.lane()));
 const person=id=>ctx.data()?.sessions?.find(x=>x.agent_id===id)?.title||id||'Awaiting assignment';
 const stationName=id=>id?.startsWith('board:')?'Task Board':id?.startsWith('completed:')?'Completed Tasks':id?.startsWith('review:')?'Review queue':person(id);
 async function api(path,options={}){const r=await fetch(path,{cache:'no-store',...options});let d;try{d=await r.json()}catch{throw Error('The Task Board returned an unreadable response')};if(!r.ok)throw Error(d.error||'Task Board unavailable');return d;}
 async function mutate(operation,item){const result=await api('/api/taskflow',{method:'POST',headers:{'Content-Type':'application/json','X-KE-Board-Token':ctx.data().controlToken},body:JSON.stringify({operation,item})});await refresh();return result;}
 function note(message){connection.textContent=message;connection.hidden=!message;}
 function pretty(value){return typeof value==='string'?value:JSON.stringify(value,null,2);}
 function section(title,body){const s=make('section','tf-section');s.append(make('h3','',title));if(body!==undefined)s.append(make('p','tf-text',pretty(body)));pane.append(s);return s;}
 function details(title,body){const d=make('details','tf-record');d.append(make('summary','',title),make('pre','',pretty(body)));return d;}
 function input(form,label,value='',multiline=false){const wrap=make('label','tf-field'),n=make(multiline?'textarea':'input');wrap.append(make('span','',label),n);n.value=value;form.append(wrap);return n;}
 function check(form,label,value=false){const w=make('label','tf-check'),n=make('input');n.type='checkbox';n.checked=value;w.append(n,document.createTextNode(label));form.append(w);return n;}
 function dialog(title){const d=make('dialog','tf-dialog'),form=make('form'),error=make('p','tf-error');error.setAttribute('role','alert');const head=make('div','tf-dialog-head');head.append(make('h2','',title),button('Close',()=>d.close()));d.append(head,form,error);document.body.append(d);d.addEventListener('close',()=>d.remove());d.showModal();return {d,form,error};}
 function lines(s){return s.split('\n').map(x=>x.trim()).filter(Boolean);}
 function auditTeam(){
  const {d,form,error}=dialog('Audit team'),paint=()=>{const pool=snapshot.auditTeam||{members:[],waiting:0,claims:[],unroutableTaskIds:[]};form.replaceChildren();
   form.append(make('p','tf-text',pool.members.length+' reviewers · '+pool.claims.length+' claimed · '+pool.waiting+' waiting'),make('p','tf-text','One active review per auditor. Reviewers share the project queue and retain independent findings, Library source versions and research answers.'));
   const library=make('a','tf-artifact','Open project Library');library.href='/library?project='+encodeURIComponent(ctx.project());form.append(library);
   if(!pool.members.length)form.append(make('p','tf-error','No auditors are seated. Add existing qualified reviewers to the project Audit team.'));
   if(pool.capacitySignal==='backlog')form.append(make('p','tf-error','The waiting queue exceeds the reviewer pool. Additional qualified audit capacity is needed.'));
   if(pool.unroutableTaskIds?.length)form.append(make('p','tf-error',pool.unroutableTaskIds.length+' tasks have no permitted independent review route.'));
   for(const m of pool.members){const row=make('article','tf-worker');row.append(make('strong','',person(m.agentId)),make('span','',m.state==='unclaimed'?'No audit claimed':m.state==='stale'?'Review needs attention':m.state==='offered'?'Awaiting acceptance':m.state==='accepted'?'Accepted · awaiting audit work':m.state==='context_ready'?'Context assessed · execution unverified':'Review execution observed'));if(m.claim)row.append(button('Open assigned audit',()=>{d.close();open(m.claim.taskId)}));form.append(row);}
   const queue=make('section','tf-section');queue.append(make('h3','','Shared audit queue'));for(const t of snapshot.tasks.filter(t=>['REVIEW','VERIFY'].includes(t.state)))queue.append(button(t.title+' · '+(t.audit?person(t.audit.reviewer)+' · '+(t.audit.phase||'Accepted'):'Waiting for auditor'),()=>{d.close();open(t.taskId)}));form.append(queue);
  };paint();form.append(button('Refresh audit team',async()=>{try{await refresh();paint()}catch(e){error.textContent=e.message}}));
 }
 function courier(id){const b=(snapshot.birds||[]).find(x=>x.id===id);if(!b)return;
  const {form}=dialog(b.name+' · courier history');form.dataset.courierId=id;const portrait=birdPortrait();portrait.dataset.state=b.state;form.prepend(portrait);
  form.append(make('p','tf-text',b.state==='delivering'?'In flight to drop-off · then returns to the birdhouse':'In the birdhouse · no model running'),make('small','',b.id));
  if(b.taskId)form.append(button('Open carried task',()=>open(b.taskId)));
  for(const trip of [...(b.history||[])].reverse()){
   const row=make('section','tf-section');row.append(make('strong','',person(trip.workerId)+' · '+stateName(trip.state)),button('Open task '+trip.taskId,()=>open(trip.taskId)),details('Delivery receipt · '+trip.assignmentId,trip));form.append(row);
  }
  if(!b.history?.length)form.append(make('p','tf-text','No recorded deliveries in this project yet.'));
  form.append(make('p','tf-text',b.historyBoundary||'A courier delivery does not prove task execution.'));
 }
 function capture(laneId){
  const lane=ctx.data().lanes.find(x=>x.id===laneId);if(!lane)return;
  const {d,form,error}=dialog('Add task · '+lane.name),keyName='switchboard.task-draft.'+laneId;
  let draft={key:crypto.randomUUID(),request:'',inputs:''};try{draft={...draft,...JSON.parse(localStorage.getItem(keyName)||'{}')}}catch{}
  const request=input(form,'What needs to be done?',draft.request,true),files=input(form,'Input file paths (optional, one per line)',draft.inputs,true);
  const save=()=>{draft.request=request.value;draft.inputs=files.value;localStorage.setItem(keyName,JSON.stringify(draft))};on(request,'input',save);on(files,'input',save);
  const submit=button('Save task',()=>{});submit.type='submit';form.append(submit);
  form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;save();try{const owner=ctx.data().placements.find(p=>p.laneId===laneId&&p.role==='coordinator')?.agentId;
   const t=await mutate('capture',{request:request.value,inputs:lines(files.value),key:draft.key,projectId:lane.projectId,laneId,ownerId:owner});localStorage.removeItem(keyName);d.close();open(t.taskId);
  }catch(err){error.textContent=err.message+' · Your draft is retained.';}finally{submit.disabled=false}};request.focus();
 }
 function deliveryDialog(t){
  const {d,form,error}=dialog('Required delivery'),prior=t.delivery||{};
  form.append(make('p','tf-text','Choose what must be delivered before this task can finish. Existing workers keep their approved scope. An independent result approval does not complete an app installation.'));
  const kind=make('select');kind.setAttribute('aria-label','Delivery kind');for(const [value,label] of [['artifact','Verified artifact in this task'],['document','Document at a destination'],['research','Research at a destination'],['local-app','Installed local application']]){const option=make('option','',label);option.value=value;kind.append(option)}kind.value=prior.kind||'document';form.append(kind);
  const path=input(form,'Destination absolute path',prior.destination?.path||''),bundle=input(form,'Application bundle identifier',prior.destination?.bundleId||'');
  const worker=make('select');worker.setAttribute('aria-label','Existing delivery worker');const empty=make('option','','Choose an enrolled worker');empty.value='';worker.append(empty);for(const w of snapshot.workers.filter(w=>w.projectId===t.projectId&&w.enabled&&w.mode==='cooperative')){const o=make('option','',person(w.workerId));o.value=w.workerId;worker.append(o)}worker.value=prior.actorId||'';form.append(worker);
  const criteria=input(form,'Required acceptance criteria (one per line)',(prior.criteria||[]).map(x=>x.description).join('\n'),true);
  const display=()=>{path.closest('label').hidden=kind.value==='artifact';bundle.closest('label').hidden=kind.value!=='local-app';worker.hidden=kind.value==='artifact'};kind.onchange=display;display();
  const save=button('Save delivery requirement',()=>{});save.type='submit';form.append(save);
  form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{const destination=kind.value==='artifact'?{kind:'task-result'}:kind.value==='local-app'?{kind:'local-app',path:path.value,bundleId:bundle.value}:{kind:'directory',path:path.value};await mutate('delivery-configure',{taskId:t.taskId,version:t.version,delivery:{kind:kind.value,destination,actorId:worker.value,criteria:lines(criteria.value).map((description,i)=>({id:prior.criteria?.[i]?.id||'criterion-'+(i+1),description}))}});d.close();await loadDetail()}catch(err){error.textContent=err.message}finally{save.disabled=false}};
 }
 function setup(laneId){
  const lane=ctx.data().lanes.find(x=>x.id===laneId);if(!lane)return;
  const p=snapshot.policies.find(x=>x.laneId===laneId)||{version:0,capabilities:['general'],scopes:[],maxWorkers:25,maxMinutes:30};
  const {d,form,error}=dialog('Task dispatch · '+lane.name);
  form.append(make('p','tf-text','Save which scoped work may be matched automatically. The owner receives updates; routine delivery does not wait for an owner acknowledgment. Enroll individual existing workers below.'));
  const enabled=check(form,'Enable task matching',p.enabled),auto=check(form,'New requests use these scope and capability rules',p.autoReady);
  let attendantId=p.attendant?.agentId||'';
  const attendants=make('fieldset','tf-section');attendants.append(make('legend','','Task Board attendant'),make('p','tf-text','The attendant can send scoped ready work to dispatch. Dispatch chooses an eligible worker and carries the task by bird.'));
  for(const candidate of [{agentId:'',title:'No attendant'},...ctx.data().placements.filter(s=>s.laneId===laneId&&s.role!=='auditor').map(s=>({agentId:s.agentId,title:person(s.agentId)}))]){
   const label=make('label','tf-worker'),choice=make('input');choice.type='radio';choice.name='taskboard-attendant';choice.value=candidate.agentId;choice.checked=attendantId===candidate.agentId;choice.onchange=()=>{attendantId=choice.value};label.append(choice,make('span','',candidate.title));attendants.append(label);
  }
  form.append(attendants);
  const scopes=input(form,'Permitted source scopes (absolute paths, one per line)',p.scopes.join('\n'),true),caps=input(form,'Capabilities (one per line)',p.capabilities.join('\n'),true);
  const maximum=input(form,'Maximum enrolled workers (1–25)',p.maxWorkers),minutes=input(form,'Maximum minutes per managed task',p.maxMinutes);
  const managed=check(form,'Permit automatic starts for explicitly enrolled managed workers',p.allowManagedStarts);
  form.append(make('p','tf-text','Imported conversations use cooperative pickup at their own safe boundary. This setting does not wake imported agents, change their models or create new agents. Bird standby is limited to five software couriers.'));
  const save=button('Save dispatch rules',()=>{});save.type='submit';form.append(save);
  form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{await mutate('policy',{projectId:lane.projectId,laneId,version:p.version,enabled:enabled.checked,autoReady:auto.checked,attendantId,allowManagedStarts:managed.checked,scopes:lines(scopes.value),capabilities:lines(caps.value),maxWorkers:Number(maximum.value),maxMinutes:Number(minutes.value)});d.close();}catch(err){error.textContent=err.message}finally{save.disabled=false}};
  const roster=make('section','tf-section');roster.append(make('h3','','Existing worker enrollment'));d.append(roster);
  for(const seat of ctx.data().placements.filter(x=>x.laneId===laneId&&x.role!=='coordinator'&&x.role!=='auditor')){
   const w=snapshot.workers.find(x=>x.workerId===seat.agentId),s=ctx.data().sessions.find(x=>x.agent_id===seat.agentId);if(!s)continue;
   const row=make('div','tf-worker');row.append(make('strong','',s.title),make('span','',w?(w.mode==='managed'?'Managed dispatch enrolled':'Cooperative pickup enrolled'):'Not enrolled'));
   row.append(button(w?'Update enrollment':'Enroll for cooperative pickup',async()=>{try{await mutate('enroll',{workerId:s.agent_id,projectId:lane.projectId,laneId,scopes:lines(scopes.value),capabilities:lines(caps.value),mode:'cooperative'});d.close();setup(laneId)}catch(err){error.textContent=err.message}}));
   if(w?.enabled)row.append(button('Pause enrollment',async()=>{try{await mutate('pause-worker',{workerId:s.agent_id});d.close();setup(laneId)}catch(err){error.textContent=err.message}}));
   if(s.managed)row.append(button('Enroll managed automatic start',async()=>{try{await mutate('enroll',{workerId:s.agent_id,projectId:lane.projectId,laneId,scopes:lines(scopes.value),capabilities:lines(caps.value),mode:'managed'});d.close();setup(laneId)}catch(err){error.textContent=err.message}}));roster.append(row);
  }
 }
 function readyDialog(t,operation='ready'){const {d,form,error}=dialog(operation==='handoff'?'Define the next worker step':'Define ready work'),outcome=input(form,'Required result',pretty(t.definitionOfDone)||t.request,true),caps=input(form,'Capabilities (one per line)',(t.capabilities||['general']).join('\n'),true),scopes=input(form,'Source scopes (one per line)',t.scopes.join('\n'),true),deps=input(form,'Dependency task IDs (one per line)',t.dependencies.join('\n'),true);let correctionWorker=null;if(t.correctionWorkerId){correctionWorker=make('select');correctionWorker.setAttribute('aria-label','Correction worker');const candidates=(snapshot.workers||[]).filter(w=>w.laneId===t.laneId&&w.projectId===t.projectId&&w.enabled);if(!candidates.some(w=>w.workerId===t.correctionWorkerId))candidates.unshift({workerId:t.correctionWorkerId,name:person(t.correctionWorkerId)+' (unavailable)'});for(const w of candidates){const option=make('option','',w.name||person(w.workerId));option.value=w.workerId;option.selected=w.workerId===t.correctionWorkerId;correctionWorker.append(option)}const label=make('label','tf-field');label.append(make('span','','Correction worker'),correctionWorker);form.append(label)}const submit=button('Make ready',()=>{});submit.type='submit';form.append(submit);form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;try{await mutate(operation,{taskId:t.taskId,version:t.version,definitionOfDone:outcome.value,capabilities:lines(caps.value),scopes:lines(scopes.value),dependencies:lines(deps.value),...(correctionWorker&&correctionWorker.value!==t.correctionWorkerId?{correctionWorkerId:correctionWorker.value}:{})});d.close();await open(t.taskId)}catch(err){error.textContent=err.message}finally{submit.disabled=false}};}
 function noteDialog(t,supersedes=null){const {d,form,error}=dialog(supersedes?'Correct evidence note':'Add evidence note');form.append(make('p','tf-text','Add an observation or evidence link. To change what the worker must do, use Add instructions.'));const body=input(form,'Observation','',true),evidence=input(form,'Evidence file paths (one per line)','',true),key=crypto.randomUUID(),save=button('Save evidence note',()=>{});save.type='submit';form.append(save);form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{await mutate('annotate',{taskId:t.taskId,key,text:body.value,evidence:lines(evidence.value),supersedes});d.close();await loadDetail()}catch(err){error.textContent=err.message}finally{save.disabled=false}};}
 function noteContributors(t,grants){const {d,form,error}=dialog('Task evidence-note contributors');form.append(make('p','tf-text','Allow an existing person to record observations on this task. The task owner and work assignment stay unchanged.'));for(const s of ctx.data().sessions){const grant=grants.find(g=>g.contributor===s.agent_id),row=make('div','tf-event'),toggle=button(grant?.enabled?'Remove note access':'Allow notes',async()=>{try{await mutate('annotation-access',{taskId:t.taskId,contributor:s.agent_id,enabled:!grant?.enabled,version:grant?.version||0});d.close();await loadDetail()}catch(err){error.textContent=err.message}});row.append(make('strong','',s.title||s.agent_id),toggle);form.append(row)}}
 async function open(id){selected=id;selectedVersion=null;try{localStorage.setItem('switchboard.selected-task.'+ctx.project(),id)}catch{};ctx.showTask(pane,'Task',id);await loadDetail();}
 async function loadDetail(){
  if(!selected)return;const id=selected;detailController?.abort();const ctl=new AbortController();detailController=ctl;
  try{const d=await api('/api/taskflow/task?id='+encodeURIComponent(id),{signal:ctl.signal});if(selected!==id||disposed)return;
   const signature=JSON.stringify([d.task.version,d.events.length,d.linkedTasks,d.annotations]);if(selectedVersion===signature)return;selectedVersion=signature;
   const focused=pane.contains(document.activeElement)?{tag:document.activeElement.tagName,text:document.activeElement.textContent}:null;
   const reading=[...pane.querySelectorAll('[data-reading-key]')].find(n=>n.getBoundingClientRect().bottom>pane.parentElement.getBoundingClientRect().top+10),anchor=reading?{key:reading.dataset.readingKey,top:reading.getBoundingClientRect().top}:null;
   const openSections=new Set([...pane.querySelectorAll('details[open]')].map(n=>n.querySelector('summary')?.textContent)),scroll=pane.parentElement?.scrollTop||0;
   pane.replaceChildren();pane.dataset.taskId=id;const t=d.task,head=make('div','tf-task-heading');head.append(make('span','tf-state',stateName(t.displayState||t.state)),make('h2','',t.title),make('small','',t.taskId));pane.append(head);
   section('Task',t.request);section(t.workerId?'Assigned worker':t.state==='LINKED'?'Implementation':t.intakeDisposition?.executable===false?'Record owner':'Assignment',t.workerId?person(t.workerId):t.state==='LINKED'?'Linked to existing work':t.intakeDisposition?.executable===false?person(t.ownerId):({DONE:'Completed',CANCELLED:'Cancelled',REVIEW:'Awaiting independent review',CAPTURED:'Needs a scoped task contract'}[t.state]||'Awaiting an eligible worker'));if(t.audit)section('Independent audit',person(t.audit.reviewer));if(t.waitReason)section('Current status',t.waitReason);
   const health=window.TaskFlowHealth.assess(t);if(health.blocked){const alert=make('p','tf-stuck-explanation','Needs attention at '+stationName(t.blockedAtStation||t.station)+' · '+health.reason);alert.setAttribute('role','status');head.append(alert);}
   if(t.legacyStatus==='WORKING')section('Execution evidence','The saved contract says WORKING. No current task-specific run is bound to this record. This is historical status, not proof of live work.');
   if(t.progress)section('Progress',t.progress.message||t.progress);if(t.definitionOfDone)section('Required result',t.definitionOfDone);
   for(const a of t.amendments||[])section('Updated instructions · '+new Date(a.at*1000).toLocaleString(),a.text);
   const actions=make('div','tf-actions');pane.append(actions);actions.append(button('Follow task',()=>follow(t)),button('Show current position',()=>{motionQueue.clear(id);follow(t)}));const library=make('a','tf-artifact','Open project Library');library.href='/library?project='+encodeURIComponent(ctx.project());actions.append(library);
   if(!t.legacy&&!['DONE','CANCELLED'].includes(t.state)){
    actions.append(button('Add instructions',()=>{const {d,form,error}=dialog('Add task instructions'),body=input(form,'Update', '',true),save=button('Save update',()=>{});save.type='submit';form.append(save);form.onsubmit=async e=>{e.preventDefault();try{await mutate('amend',{taskId:id,version:t.version,text:body.value});d.close();await loadDetail()}catch(err){error.textContent=err.message}}}));
    if(['CAPTURED','READY','QUEUED','NEEDS_COORDINATION','BLOCKED','NEEDS_VERIFICATION'].includes(t.state))actions.append(button('Define ready work',()=>readyDialog(t)));
    if(!['UNCERTAIN','CANCELLING'].includes(t.state))actions.append(button('Cancel task',async()=>{try{await mutate('cancel',{taskId:id,version:t.version});await loadDetail()}catch(err){note(err.message)}}));
   }
   const notes=d.annotations?.notes||[],superseded=new Set(notes.map(n=>n.supersedes).filter(Boolean));
   if(notes.length){const observations=section('Evidence notes');observations.append(make('p','tf-text','Recorded observations. Instructions, ownership and completion remain governed by the task above.'));for(const n of notes){const row=make('article','tf-event');row.dataset.annotationId=n.id;row.append(make('strong','',superseded.has(n.id)?'Earlier observation · retained':'Observation'),make('small','',person(n.actor)+' · '+new Date(n.at*1000).toLocaleString()),make('p','tf-text',n.text));for(const f of n.evidence){const link=make('a','tf-artifact',f.name);link.href='/api/taskflow/artifact?id='+encodeURIComponent(id)+'&sha='+encodeURIComponent(f.sha256);link.target='_blank';link.rel='noopener';row.append(link)}row.append(button('Correct this note',()=>noteDialog(t,n.id)));observations.append(row)}}
   actions.append(button('Add evidence note',()=>noteDialog(t)),button('Manage note contributors',()=>noteContributors(t,d.annotations?.contributors||[])));
   if(t.legacy)section('Retained task record','This record preserves its original contract and ownership. Its recorded status is '+t.legacyStatus+'. New execution needs an explicit scoped continuation; opening this record does not start work.');
   if(!t.legacy){section('Required delivery',t.delivery||'Delivery kind, destination and criteria have not been declared. This task cannot be completed yet.');if(['CAPTURED','READY','QUEUED','NEEDS_COORDINATION','BLOCKED','NEEDS_VERIFICATION'].includes(t.state))actions.append(button('Define delivery requirement',()=>deliveryDialog(t)));if(t.approval)section('Exact result approval',t.approval);if(t.deliveryResult)section('Delivered result awaiting verification',t.deliveryResult);if(t.completion)section('Verified completion',t.completion);if(d.deliveries?.length)section('Delivery attempts',d.deliveries);if(t.deliveryHistory?.length)section('Retained delivery corrections',t.deliveryHistory);}
   if(t.intake){section('Retained intake context',t.intake.sourceProgress);if(d.linkedTasks?.length){const linked=section('Existing implementation');for(const x of d.linkedTasks)linked.append(button((x.title||x.taskId)+' · '+stateName(x.state),()=>open(x.taskId)),make('p','tf-text',x.waitReason||''));}}
   if(!t.legacy&&t.state==='UNCERTAIN')actions.append(button('Check recovery evidence',async()=>{try{await mutate('recover',{assignmentId:t.assignmentId,reason:'Operator requested exact assignment reconciliation'});await loadDetail()}catch(e){note(e.message)}}));
   if(!t.legacy&&t.state==='REVIEW')actions.append(button('Define next step',()=>readyDialog(t,'handoff')));
   if(!t.legacy&&['REVIEW','VERIFY'].includes(t.state))actions.append(button('Review result',()=>{const {d,form,error}=dialog('Review this task result'),summary=input(form,'Findings','',true),evidence=input(form,'Review evidence file paths (one per line)','',true),decision=make('select');for(const [value,label]of [['revise','Return to Task Board for correction'],['fail','Failed review: queue correction'],['accept',t.state==='VERIFY'?'Accept verified delivery':'Approve exact result']]){const option=make('option','',label);option.value=value;decision.append(option)}const label=make('label','tf-field');label.append(make('span','','Decision'),decision);form.append(label);const save=button('Save review',()=>{});save.type='submit';form.append(save);form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{await mutate('review',{taskId:id,version:t.version,verdict:decision.value,summary:summary.value,evidence:lines(evidence.value)});d.close();await loadDetail()}catch(err){error.textContent=err.message}finally{save.disabled=false}}}));
   const inputs=section('Inputs and results');for(const f of [...(t.inputs||[]),...(t.result?.artifacts||[])]){
    const a=make('a','tf-artifact',f.name+' · '+f.bytes+' bytes');a.href='/api/taskflow/artifact?id='+encodeURIComponent(id)+'&sha='+f.sha256;a.target='_blank';a.rel='noopener';inputs.append(a,details('Source and integrity · '+f.name,{path:f.path,sha256:f.sha256}));
   }if(!inputs.querySelector('a'))inputs.append(make('p','','No artifacts attached yet.'));
   if(t.result?.summary)section('Returned result',t.result.summary);
   if(t.sourceRefs?.length)section('Requirement and source links',t.sourceRefs);
   if(d.audit?.claims?.length){const audit=section('Audit context and findings');for(const c of d.audit.claims){audit.append(details(person(c.reviewer)+' · '+c.state,{contextAssessment:c.contextAssessment,library:(c.library||[]).map(r=>({id:r.id,title:r.title,bodyHash:r.bodyHash})),verdict:c.verdict}));}for(const q of d.audit.questions||[])audit.append(details('Research question · '+q.state,{question:q.question,recipient:person(q.recipient),answer:q.answer,evidence:q.evidence}));}
   const timeline=section('Task and information path');
   for(const e of d.events){const row=make('article','tf-event'),detail=e.detail||{};row.dataset.readingKey=e.kind+':'+e.seq;row.append(make('strong','',e.kind.replaceAll('-',' ')),make('small','',person(e.actor)+' · '+(typeof e.at==='number'?new Date(e.at*1000).toLocaleString():e.at||'')));
    if(e.source||e.destination)row.append(make('p','tf-route-label',stationName(e.source)+' → '+stationName(e.destination)));
    if(window.TaskFlowMotion.valid(e))row.append(button('Replay handoff',()=>{motionQueue.replay(e);follow(t)}));
    if(detail.text||detail.summary||detail.reason)row.append(make('p','tf-text',detail.text||detail.summary||detail.reason));
    row.append(details('Evidence · '+e.kind+' #'+e.seq,detail));timeline.append(row);
   }
   const assignments=section('Worker handoffs');for(const a of d.assignments)assignments.append(details(person(a.workerId)+' · '+stateName(a.state)+' · '+a.id,{assignmentId:a.id,payloadHash:a.payloadHash,payload:a.payload,acceptedAt:a.acceptedAt,threadId:a.providerThreadId,turnId:a.providerTurnId,result:a.result}));
   for(const n of pane.querySelectorAll('details'))if(openSections.has(n.querySelector('summary')?.textContent))n.open=true;
   if(pane.parentElement)pane.parentElement.scrollTop=scroll;if(anchor){const restored=[...pane.querySelectorAll('[data-reading-key]')].find(n=>n.dataset.readingKey===anchor.key);if(restored)pane.parentElement.scrollTop+=restored.getBoundingClientRect().top-anchor.top;}
   if(focused)[...pane.querySelectorAll('button,summary,a')].find(n=>n.tagName===focused.tag&&n.textContent===focused.text)?.focus({preventScroll:true});
   ctx.setTaskTitle?.(t.title,id);
  }catch(err){if(err.name!=='AbortError'){note(err.message);if(!pane.childNodes.length)pane.append(make('p','tf-error',err.message));}}
 }
 function stationNode(station,t){
  if(!station)return null;
  const visible=n=>{if(!n?.getClientRects().length||getComputedStyle(n).visibility==='hidden')return false;const r=n.getBoundingClientRect();if(!r.width||!r.height)return false;const x=(r.left+r.right)/2,y=(r.top+r.bottom)/2;for(let p=n.parentElement;p&&p.id!=='world';p=p.parentElement){const s=getComputedStyle(p),b=p.getBoundingClientRect();if(/auto|scroll|hidden|clip/.test(s.overflowX)&&(x<b.left||x>b.right)||/auto|scroll|hidden|clip/.test(s.overflowY)&&(y<b.top||y>b.bottom))return false;}return true;};
  if(station.startsWith('board:')||station.startsWith('completed:')){const lane=station.slice(station.indexOf(':')+1),list=[...document.querySelectorAll('[data-task-lane]')].find(n=>n.dataset.taskLane===lane),anchor=[...(list?.querySelectorAll('[data-task-anchor]')||[])].find(n=>n.dataset.taskAnchor===t.taskId),summary=station.startsWith('completed:')?list?.querySelector('.tf-completed-station'):null;return [anchor,summary,list?.closest('[data-room-id]')?.querySelector('[data-room-entrance]')].find(visible)||null;}
  if(station.startsWith('review:')){const lane=station.slice('review:'.length),room=[...document.querySelectorAll('[data-room-id]')].find(n=>n.dataset.roomId===lane+':review');return [[...(room?.querySelectorAll('[data-review-task]')||[])].find(n=>n.dataset.reviewTask===t.taskId),room?.querySelector('.tf-review-queue')].find(visible)||null;}
  return [...document.querySelectorAll('[data-desk-agent]')].find(n=>n.dataset.deskAgent===station&&visible(n))||null;
 }
 function follow(t){following=t.taskId;ctx.revealTask?.(t);requestAnimationFrame(schedule);}
 function stopFollowing(){following=null;schedule();}
 function point(node,id){if(!node)return null;if(node.matches('[data-desk-agent]'))node=node.querySelector('.wf-agent-avatar')||node;const r=node.getBoundingClientRect();return {node,id,port:'task',x:r.left+r.width/2,y:r.top+r.height/2};}
 // Visual door-to-desk L only. Never used to start or finish work.
 function doorDeskFallback(from,to){
  if(!from||!to||![from.x,from.y,to.x,to.y].every(Number.isFinite))return '';
  return 'M'+from.x+','+from.y+' L'+from.x+','+to.y+' L'+to.x+','+to.y;
 }
 function drawAgentPaths(view){
  const geometry=window.WorkflowRoads?.snapshot();if(!geometry||!view)return;
  const signature=geometry.key;if(signature===pathSignature)return;pathSignature=signature;paths.replaceChildren();
  const footprint={left:3,right:3,top:3,bottom:3};let connected=0,unavailable=0;
  for(const room of geometry.rooms){if(!room.door)continue;const from=point(room.door,room.id);
   for(const desk of room.desks){const id=desk.node.dataset.deskAgent;if(!id)continue;const to=point(desk.node,id),route=window.WorkflowRoads.courierRoute(from,to,footprint);
    if(route?.available){
     const g=svg('g',{'data-agent-path':id,'data-room-id':room.id});g.append(svg('path',{d:route.d,class:'tf-agent-path-bed'}),svg('path',{d:route.d,class:'tf-agent-path-line'}),svg('circle',{cx:to.x,cy:to.y,r:5,class:'tf-agent-path-end'}));const title=svg('title',{});title.textContent='Path to '+person(id);g.append(title);paths.append(g);connected++;
     continue;
    }
    unavailable++;
    const d=doorDeskFallback(from,to);if(!d)continue;
    const g=svg('g',{'data-agent-path':id,'data-room-id':room.id,'data-fallback':'true'});g.append(svg('path',{d,class:'tf-agent-path-bed'}),svg('path',{d,class:'tf-agent-path-line'}),svg('circle',{cx:to.x,cy:to.y,r:5,class:'tf-agent-path-end'}));const title=svg('title',{});title.textContent='Path to '+person(id);g.append(title);paths.append(g);
   }
  }
  paths.dataset.connectedAgents=connected;paths.dataset.unavailableAgents=unavailable;
 }
 function along(points,fraction){const spans=points.slice(1).map((p,i)=>Math.hypot(p.x-points[i].x,p.y-points[i].y)),total=spans.reduce((a,b)=>a+b,0);let remain=total*fraction;for(let i=0;i<spans.length;i++){if(remain<=spans[i]){const q=spans[i]?remain/spans[i]:0;return {x:points[i].x+(points[i+1].x-points[i].x)*q,y:points[i].y+(points[i+1].y-points[i].y)*q}}remain-=spans[i]}return points.at(-1);}
 const birdFlights=new Map(),flightSprites=new Map(),birdLastPos=new Map();let prevOutbound=new Set();const droppedBirds=new Set();
 const flightLayer=make('div','tf-flight-layer');flightLayer.setAttribute('aria-hidden','true');document.body.append(flightLayer);
 function perchPoint(id){const n=document.querySelector('.tf-roost-bird[data-courier-id="'+id+'"]');return n?point(n,'roost:'+id):null;}
 function flightSprite(id){let n=flightSprites.get(id);if(!n){n=make('div','tf-flight-bird');n.dataset.courierId=id;n.innerHTML=birdArtwork;flightLayer.append(n);flightSprites.set(id,n);}return n;}
 function releaseFlight(id){birdFlights.delete(id);birdLastPos.delete(id);const n=flightSprites.get(id);if(n){n.remove();flightSprites.delete(id);}}
 function resetFlights(){for(const id of [...flightSprites.keys()])releaseFlight(id);prevOutbound=new Set();droppedBirds.clear();}
 function homeRoute(from,to){if(!from||!to||![from.x,from.y,to.x,to.y].every(Number.isFinite))return null;return {available:true,points:[from,to],d:'M'+from.x+','+from.y+' L'+to.x+','+to.y};}
 function schedule(){if(raf||drawTimer||disposed)return;const render=now=>{if(raf)cancelAnimationFrame(raf);clearTimeout(drawTimer);raf=null;drawTimer=null;if(!disposed)draw(now);};raf=requestAnimationFrame(render);drawTimer=setTimeout(()=>render(performance.now()),100);}
 function draw(now){raf=null;let moving=false;const tasks=scopedTasks(),retained=new Set(tasks.map(t=>t.taskId)),stacks=new Map(),outbound=new Set(),view=document.getElementById('viewport')?.getBoundingClientRect(),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(view){const clip='inset('+Math.max(0,view.top)+'px '+Math.max(0,innerWidth-view.right)+'px '+Math.max(0,innerHeight-view.bottom)+'px '+Math.max(0,view.left)+'px)';iconLayer.style.clipPath=clip;paths.style.clipPath=clip;}drawAgentPaths(view);paths.querySelectorAll('.tf-active-task-path').forEach(n=>n.remove());
  for(const [id,icon]of icons)if(!retained.has(id)){icon.remove();icons.delete(id)}
  const journeys=[];
  for(const t of tasks){
   let icon=icons.get(t.taskId);if(!icon){icon=button('',e=>{e.stopPropagation();open(e.detail&&icon.dataset.pressedTask?icon.dataset.pressedTask:t.taskId);delete icon.dataset.pressedTask},'tf-task-icon');icon.dataset.taskId=t.taskId;const symbol=make('span','tf-task-symbol');symbol.innerHTML=birdArtwork;icon.append(symbol,make('span','tf-icon-label'),make('span','tf-icon-state'));icons.set(t.taskId,icon);iconLayer.append(icon);
    icon.addEventListener('pointerdown',e=>{e.stopPropagation();icon.dataset.pressedTask=t.taskId;icon.dataset.pressed='true';icon.setPointerCapture?.(e.pointerId)});icon.addEventListener('pointerup',()=>{delete icon.dataset.pressed;schedule()});icon.addEventListener('pointercancel',()=>{delete icon.dataset.pressedTask;delete icon.dataset.pressed;schedule()});icon.addEventListener('keydown',e=>e.stopPropagation());
   }
   const health=!updatesAvailable&&!['DONE','CANCELLED'].includes(t.state)?{blocked:true,reason:'Task updates unavailable; showing the last recorded location'}:window.TaskFlowHealth.assess(t);if(health.blocked&&!motionQueue.current(t.taskId)?.replay)motionQueue.clear(t.taskId);
   const motion=motionQueue.current(t.taskId),recordedStation=health.blocked?(t.blockedAtStation||t.station):t.station,station=recordedStation?.startsWith('transit:')?(motion?.source||t.workerId):recordedStation;
   const node=stationNode(station,t),room=node?.closest('[data-room-id]'),scale=Math.max(1,Math.min(1.4,room?room.getBoundingClientRect().width/room.offsetWidth:1));
   const carrier=(snapshot.birds||[]).find(b=>b.taskId===t.taskId&&b.state==='delivering');
   const courierId=carrier?.id||(motion&&motion.detail&&motion.detail.courierId)||t.courierId;
   let endpoint=point(node,station),position=endpoint,phase='stationed',routeError='';icon.style.setProperty('--tf-scale',scale);
   if(courierId&&(carrier||motion))icon.dataset.courier=courierId;else delete icon.dataset.courier;
   delete icon.dataset.route;delete icon.dataset.routePlan;delete icon.dataset.eventSeq;
   if(motion){
    icon.dataset.eventSeq=motion.seq;icon.dataset.replay=String(motion.replay);
    const from=point(stationNode(motion.source,t),motion.source),to=point(stationNode(motion.destination,t),motion.destination);
    if(reduced){phase='reduced';}
    else if(from&&to){
     // Fit the visible bird to real clearance; its independent hit target stays 44px.
     let route;for(const half of [...new Set([14*scale,12,10,8])].filter(n=>n<=14*scale)){
      route=window.WorkflowRoads?.courierRoute(from,to,{left:half,right:half,top:half,bottom:half});
      if(route?.available){icon.style.setProperty('--tf-scale',half/14);break;}
     }
     if(route?.available&&route.points.length){position=along(route.points,Math.max(0,Math.min(1,(motion.elapsed-600)/4800)));phase=motion.elapsed<600?'pickup':motion.elapsed<5400?'moving':'handoff';icon.dataset.route=JSON.stringify(route.points);icon.dataset.routePlan=JSON.stringify({points:route.points,footprint:route.footprint,sourceRoom:route.sourceRoom,targetRoom:route.targetRoom,sourceGate:route.sourceGate,targetGate:route.targetGate});paths.append(svg('path',{d:route.d,class:'tf-active-task-path','data-task-id':t.taskId,'data-source':motion.source,'data-destination':motion.destination}));}else{routeError=route?.reason||'Path unavailable';position=from;}
    }else{routeError='Source or destination is outside this view';position=from;}
    const outside=position&&view&&(position.x<view.left+16||position.x>view.right-16||position.y<view.top+16||position.y>view.bottom-16);
    const paused=!!routeError||document.hidden||icon.dataset.pressed==='true'||(!reduced&&outside&&following!==t.taskId);
    motionQueue.step(t.taskId,now,paused);if(!paused)moving=true;
    journeys.push({task:t,motion,reason:routeError||(outside&&following!==t.taskId?'Outside the visible canvas · use Follow task':reduced?'Reduced motion · showing current position':''),count:motionQueue.count(t.taskId)});
   }
   const count=stacks.get(station)||0;stacks.set(station,count+1);if(position&&!motion&&!station?.startsWith('board:')&&!station?.startsWith('completed:')&&!station?.startsWith('review:')&&!routeError)position={x:position.x+(count%4)*46,y:position.y-18*scale-Math.floor(count/4)*48};
   if(following===t.taskId&&position&&view&&!document.hidden){const dx=position.x-(view.left+view.right)/2,dy=position.y-(view.top+view.bottom)/2;if(Math.abs(dx)>view.width*.2||Math.abs(dy)>view.height*.2){const viewport=document.getElementById('viewport'),camera=window.SwitchboardCanvasPan?.camera();if(camera)window.SwitchboardCanvasPan.scrollTo(camera.x+dx,camera.y+dy);else viewport.scrollBy(dx,dy);schedule();}}
   const liveCarry=!!carrier||t.state==='OFFERED'||String(station||'').startsWith('transit:');
   const inFlight=!!motion||liveCarry;
   icon.hidden=!inFlight||!position||!view||position.x<view.left||position.x>view.right||position.y<view.top||position.y>view.bottom;
   if(carrier&&!motion&&!droppedBirds.has(courierId))phase='pickup';
   if(position){icon.style.left=position.x+'px';icon.style.top=position.y+'px'}
   if(courierId&&position&&liveCarry){outbound.add(courierId);droppedBirds.delete(courierId);birdLastPos.set(courierId,position);if(birdFlights.get(courierId)?.leg==='home')releaseFlight(courierId);}
   icon.dataset.phase=phase;icon.dataset.station=station||'';icon.dataset.state=t.state;icon.dataset.routeError=routeError;icon.classList.toggle('tf-completed',t.state==='DONE');
   icon.dataset.stuck=String(health.blocked);icon.dataset.pathBlocked=String(!!routeError);icon.classList.toggle('tf-stuck',health.blocked);icon.classList.toggle('tf-path-blocked',!!routeError);
   const current=stateName(t.displayState||t.state),live=motion?(motion.replay?'REPLAY · ':'Recorded handoff · ')+stationName(motion.source)+' → '+stationName(motion.destination)+' · Current: '+current:carrier?carrier.name+' carrying '+current:current;
   icon.querySelector('.tf-icon-label').textContent=t.title;icon.querySelector('.tf-icon-state').textContent=live;
   icon.title=t.title+' · '+live+(motion?' · #'+motion.seq+' at '+new Date(motion.at*1000).toLocaleTimeString():'')+(routeError?' · '+routeError:'');icon.setAttribute('aria-label','Open task: '+t.title+' · '+live);
   icon.dataset.hasTransfer=String(!!motion||phase==='carrying');icon.dataset.followed=String(following===t.taskId);
   if(health.blocked){icon.querySelector('.tf-icon-state').textContent='NEEDS ATTENTION · '+health.reason;icon.title+=' · '+health.reason;icon.setAttribute('aria-label','Blocked task: '+t.title+' · '+health.reason);}
   if(health.blocked)journeys.push({task:{...t,station},health,reason:health.reason,count:0});
   for(const row of document.querySelectorAll('.wf-bunker-task[data-task-id],.tf-review-task[data-task-id]'))if(row.dataset.taskId===t.taskId){row.classList.toggle('tf-stuck-row',health.blocked);row.dataset.stuck=String(health.blocked);}
   if(position&&view){const labelWidth=Math.min(240,Math.max(100,view.width-24)),shift=Math.max(view.left+labelWidth/2+8,Math.min(view.right-labelWidth/2-8,position.x))-position.x;icon.style.setProperty('--tf-label-shift',shift+'px');icon.style.setProperty('--tf-label-width',labelWidth+'px');}
   if(selected===t.taskId&&pane.dataset.taskId===t.taskId){let status=pane.querySelector('.tf-travel-status');if(!status){status=make('p','tf-travel-status');status.setAttribute('role','status');pane.querySelector('.tf-task-heading')?.append(status)}status.textContent=routeError?'Recorded transfer could not be drawn: '+routeError:phase==='moving'?'Showing the recorded courier transfer. Task status is '+stateName(t.displayState||t.state)+'.':'';status.hidden=!status.textContent;}
  }
  const justDropped=[...prevOutbound].filter(id=>!outbound.has(id));prevOutbound=outbound;
  for(const id of justDropped){droppedBirds.add(id);const last=birdLastPos.get(id);if(!last||birdFlights.has(id))continue;if(reduced){releaseFlight(id);continue;}birdFlights.set(id,{leg:'home',from:last,elapsed:0,lastTick:now});}
  const away=new Set(outbound);
  for(const [id,flight] of [...birdFlights]){
   if(flight.leg!=='home'||outbound.has(id)){if(outbound.has(id))releaseFlight(id);continue;}
   const dest=perchPoint(id);if(!dest){away.add(id);moving=true;continue;}
   if(document.hidden){flight.lastTick=null;away.add(id);moving=true;continue;}
   if(flight.lastTick!==null)flight.elapsed+=Math.max(0,Math.min(250,now-flight.lastTick));flight.lastTick=now;
   const route=homeRoute(flight.from,dest);if(!route){releaseFlight(id);continue;}
   if(flight.elapsed>=2800){releaseFlight(id);continue;}
   const sprite=flightSprite(id);sprite.dataset.phase='home';sprite.dataset.state='returning';
   const pos=along(route.points,Math.min(1,flight.elapsed/2800));sprite.style.left=pos.x+'px';sprite.style.top=pos.y+'px';away.add(id);
   moving=true;
  }
  for(const n of document.querySelectorAll('.tf-roost-bird')){
   const id=n.dataset.courierId,out=away.has(id);n.dataset.perch=out?'empty':'home';
   const bird=(snapshot.birds||[]).find(b=>b.id===id),label=n.querySelector('.tf-perch-label');
   if(label)label.textContent=(bird?.name||id)+' · '+(out?(outbound.has(id)?'out':'returning'):'home');
  }
  renderTravel(journeys,view);
  if(moving)schedule();
 }
 function renderTravel(journeys,view){
  travel.hidden=!journeys.length&&!following&&!motionQueue.omitted;if(view){travel.style.left=(view.left+12)+'px';travel.style.bottom=Math.max(12,innerHeight-view.bottom+12)+'px';travel.style.maxWidth=Math.max(180,Math.min(430,view.width-24))+'px';}
  const signature=JSON.stringify([journeys.map(j=>[j.task.taskId,j.motion?.seq,j.motion?.replay,j.reason,j.count,j.task.displayState||j.task.state]),following,motionQueue.omitted]);if(travel.dataset.signature===signature)return;travel.dataset.signature=signature;
  const blocked=journeys.filter(j=>j.health?.blocked);travel.classList.toggle('tf-has-blocked',!!blocked.length);travel.replaceChildren(make('strong','',(blocked.length?blocked.length+' need attention · ':'')+motionQueue.count()+' recorded handoff'+(motionQueue.count()===1?'':'s')));
  if(following)travel.append(button('Stop following',stopFollowing));
  for(const j of [...journeys].sort((a,b)=>Number(b.task.taskId===following)-Number(a.task.taskId===following)||Number(!!b.health)-Number(!!a.health)).slice(0,4)){const row=make('div','tf-travel-row');row.dataset.taskId=j.task.taskId;row.dataset.eventSeq=j.motion?.seq||'';row.dataset.stuck=String(!!j.health?.blocked);row.append(button(j.task.title,()=>open(j.task.taskId)),make('span','',j.motion?stationName(j.motion.source)+' → '+stationName(j.motion.destination):'Stopped at '+stationName(j.task.station)),make('small','',j.motion?(j.motion.replay?'REPLAY · ':'Recorded · ')+new Date(j.motion.at*1000).toLocaleTimeString()+' · Current: '+stateName(j.task.displayState||j.task.state):'NEEDS ATTENTION'),button('Follow task',()=>follow(j.task)));if(j.reason)row.append(make('small','tf-error',j.reason));if(j.count>1)row.append(make('small','',(j.count-1)+' next handoff'+(j.count===2?'':'s')+' queued'));travel.append(row);}
  if(journeys.length>4)travel.append(make('small','',(journeys.length-4)+' more tasks have recorded handoffs'));
  if(motionQueue.omitted)travel.append(make('small','tf-error',motionQueue.omitted+' additional handoffs remain in task history. Open a task to replay them.'));
 }
 async function refresh(){if(polling||disposed)return;polling=true;const g=++generation,scope=ctx.project();if(scope!==scopeKey){scopeKey=scope;motionQueue.reset();following=null;resetFlights();}
  try{const d=await api('/api/taskflow?project='+encodeURIComponent(scope));if(disposed||g!==generation||ctx.project()!==scope)return;
   updatesAvailable=true;motionQueue.ingest(d.events);snapshot=d;ctx.onSnapshot(d);note(d.recoveryHold?'Restored board copy · dispatch is disarmed until the original instance is reconciled':'');schedule();
   for(const board of document.querySelectorAll('.canvas-heading')){let roster=board.querySelector('.tf-bird-roster');if(!roster){roster=make('section','tf-bird-roster tf-shared-roost tf-birdhouse');roster.setAttribute('aria-label','Birdhouse');board.append(roster)}
    roster.classList.add('tf-birdhouse');
    const birds=d.birds||[],signature=JSON.stringify(birds.map(b=>[b.id,b.state,b.taskId,b.history?.length]));if(roster.dataset.signature!==signature){roster.dataset.signature=signature;
     const outCount=birds.filter(b=>b.state==='delivering').length;
     roster.replaceChildren(make('span','tf-birdhouse-label','Birdhouse · '+(birds.length-outCount)+' home · '+outCount+' out'));
     const flock=make('div','tf-flock');for(const b of birds){const n=button('',()=>courier(b.id),'tf-roost-bird');n.dataset.courierId=b.id;n.dataset.state=b.state;n.dataset.perch=b.state==='delivering'?'empty':'home';if(b.taskId)n.dataset.taskId=b.taskId;n.innerHTML=birdArtwork;n.append(make('span','tf-perch-label',b.name+' · '+(b.state==='delivering'?'out':'home')));n.setAttribute('aria-label','Open courier '+b.name+' history');flock.append(n)}roster.append(flock);roster.title='Birdhouse. Birds fly out for pickup and drop-off, then come home. Standby does not run a model.';}}
   if(selected){if(d.tasks.some(t=>t.taskId===selected))await loadDetail();else{selected=null;selectedVersion=null}}
   else if(!restored.has(ctx.project())){restored.add(ctx.project());let saved=null;try{saved=localStorage.getItem('switchboard.selected-task.'+ctx.project())}catch{};if(saved&&d.tasks.some(t=>t.taskId===saved))await open(saved)}
  }catch(err){updatesAvailable=false;note('Task updates unavailable · '+err.message);schedule()}finally{polling=false}
 }
 const scopeChanged=()=>refresh();
 function destroy(){disposed=true;clearInterval(timer);detailController?.abort();if(raf)cancelAnimationFrame(raf);clearTimeout(drawTimer);resetFlights();flightLayer.remove();iconLayer.remove();paths.remove();connection.remove();travel.remove();motionQueue.clear();window.removeEventListener('resize',schedule);document.removeEventListener('scroll',schedule,true);window.removeEventListener('switchboard-workflow-scope',scopeChanged);document.removeEventListener('visibilitychange',schedule);document.removeEventListener('wheel',stopFollowing,true);document.removeEventListener('pointerdown',manualPan,true);}
 const manualPan=e=>{if(['viewport','world','pan-surface','pan-origin'].includes(e.target.id))stopFollowing();};
 window.addEventListener('resize',schedule);document.addEventListener('scroll',schedule,true);window.addEventListener('switchboard-workflow-scope',scopeChanged);document.addEventListener('visibilitychange',schedule);document.addEventListener('wheel',stopFollowing,{capture:true,passive:true});document.addEventListener('pointerdown',manualPan,true);
 const restored=new Set();const timer=setInterval(refresh,2000);refresh();
 return {capture,setup,auditTeam,courier,open,refresh,redraw:schedule,destroy,get snapshot(){return snapshot;},stateName};
}};
