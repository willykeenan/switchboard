'use strict';
window.LibraryWorkspace={create({scope,post,node,button,changeScope,openRecord,pins}){
 const root=document.getElementById('library-workspace');let generation=0,current=null;
 const el=(tag,text,cls='')=>node(tag,cls,text);
 const label=(text,control)=>{const n=el('label',text);n.append(control);return n;};
 const request=async url=>{const r=await fetch(url,{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.error||'Library unavailable');return d;};
 const identity=(id,workflow)=>workflow.sessions?.find(s=>s.agent_id===id);
 function conversation(id,workflow){const known=identity(id,workflow);if(!known)return el('span',id+' · identity unavailable','status');const a=button(known.title||id,()=>{const dialog=el('dialog',undefined,'librarian-conversation'),frame=el('iframe');frame.title=known.title||id;frame.src='/session?'+new URLSearchParams({agent:id,embedded:'1'});dialog.append(button('Close conversation',()=>{dialog.close();dialog.remove();}),frame);document.body.append(dialog);dialog.showModal();dialog.addEventListener('close',()=>dialog.remove(),{once:true});});a.title=id;return a;}
 function sections(title){const s=el('section',undefined,'workspace-section');s.append(el('h2',title));return s;}
 function fieldText(body){const box=el('div',undefined,'workspace-fields');for(const [key,text]of Object.entries(body||{}))if(text){const row=el('div');row.append(el('strong',({question:'Current question',decisions:'Decisions',uncertainties:'Open questions',nextStep:'Next step'})[key]||key),el('p',String(text)));box.append(row);}return box;}
 function eventForm(data,workflow,run){
  const form=el('form',undefined,'lifecycle-form'),event=el('select'),text=el('textarea'),receiver=el('select'),decision=el('select'),error=el('p','','notice-error');
  event.name='event';for(const [v,name]of [['foundation','Founding scope'],['revision','Scope revision'],['disposition','Receiving disposition']])event.append(new Option(name,v));event.value=data.foundingScope?'revision':'foundation';
  text.required=true;text.maxLength=12000;text.name='text';text.rows=4;receiver.name='receivingOwner';receiver.append(new Option('Select receiving owner / Build identity',''));
  const ids=new Set();for(const seat of data.receivers)if(!ids.has(seat.agentId)){ids.add(seat.agentId);receiver.append(new Option(identity(seat.agentId,workflow)?.title||seat.agentId,seat.agentId));}
  decision.name='decision';for(const value of ['Accepted','Revise','Rejected','Deferred'])decision.append(new Option(value,value));
  const receiveBox=el('div');receiveBox.append(label('Receiving identity',receiver),label('Recorded disposition',decision),el('p','A recorded acceptance is a claim. It does not independently verify adoption.','status'));
  const sync=()=>receiveBox.hidden=event.value!=='disposition';event.onchange=sync;sync();
  const evidence=pins();const pinned=el('div',undefined,'workspace-pins');for(const pin of evidence)pinned.append(el('code',pin.id+' · '+pin.sha.slice(0,12)));
  const submit=el('button','Save pinned record','primary');submit.type='submit';submit.disabled=!evidence.length;
  form.append(label('Record type',event),label('Scope, decision or disposition',text),receiveBox,el('p',evidence.length+' opened evidence version'+(evidence.length===1?'':'s')+' pinned. Open records in Evidence & records to add sources.','status'),pinned,submit,error);
  form.onsubmit=async e=>{e.preventDefault();if(run!==generation)return;submit.disabled=true;const payload={project:data.project,lane:data.lane,team:data.team,event:event.value,text:text.value,version:data.version,contextFingerprint:data.context.fingerprint,evidence,receivingOwner:event.value==='disposition'?receiver.value:'',decision:event.value==='disposition'?decision.value:''};const raw=JSON.stringify(payload);if(form.dataset.payload!==raw){form.dataset.payload=raw;form.dataset.key=crypto.randomUUID();}try{await post('lifecycle',{...payload,requestId:form.dataset.key});if(run===generation)await refresh();}catch(e){if(run===generation){error.textContent=e.message;submit.disabled=false;}}};
  return form;
 }
 function requirements(data,workflow,run){
  const section=sections('Requirements');section.classList.add('workspace-requirements');
  for(const source of data.requirementsSources||[]){const row=el('div',undefined,'requirement-source');row.dataset.record=source.id;row.title=source.id;row.append(source.available?button(source.title,()=>openRecord(source.id,source.sha)):el('p',source.title,'status'));section.append(row);}
  const records=data.history.filter(r=>r.event==='requirement'),latest=new Map();for(const r of records)latest.set(r.requirementId,r);
  for(const r of latest.values()){const card=el('article',undefined,'lifecycle-record');card.dataset.requirement=r.requirementId;card.append(el('h3',r.title),el('p',r.text),el('p',r.status+' · '+(r.owner?'Owner: '+(identity(r.owner,workflow)?.title||r.owner):'Owner unassigned')+' · version '+r.requirementVersion,'status'),el('strong','Acceptance criteria'),el('p',r.acceptance),el('p','Completion is unverified · evidence '+r.evidenceState+' · '+(r.contextCurrent?'current context':'earlier context'),'status'));
   for(const pin of r.evidence)card.append(button('Evidence · '+pin.sha.slice(0,12),()=>openRecord(pin.id,pin.sha)));
   const versions=el('details');versions.append(el('summary','Requirement history'));for(const v of records.filter(v=>v.requirementId===r.requirementId)){const entry=el('details');entry.append(el('summary','Version '+v.requirementVersion+' · '+v.status),el('p',v.text),el('p','Owner: '+(v.owner||'Unassigned')),el('p','Acceptance: '+v.acceptance));for(const pin of v.evidence)entry.append(button('Evidence · '+pin.sha.slice(0,12),()=>openRecord(pin.id,pin.sha)));versions.append(entry);}card.append(versions);section.append(card);
  }
  if(!records.length)section.append(el('p','No structured requirement versions in this scope. Existing requirements remain in the linked source records.','status'));
  if(['closed','merged'].includes(data.areas.find(a=>a.id===data.lane)?.lifecycle))return section;
  const form=el('form',undefined,'lifecycle-form requirement-form'),selector=el('select'),title=el('input'),text=el('textarea'),acceptance=el('textarea'),owner=el('select'),status=el('select'),error=el('p','','notice-error');
  selector.name='requirementId';selector.append(new Option('Capture a new ask',''),...[...latest.values()].map(r=>new Option('Revise: '+r.title,r.requirementId)));title.name='title';title.required=true;title.maxLength=200;text.name='text';text.required=true;text.maxLength=12000;acceptance.name='acceptance';acceptance.required=true;acceptance.maxLength=12000;owner.name='owner';owner.append(new Option('Unassigned',''));
  const projectLanes=new Set(data.areas.map(a=>a.id)),ids=new Set((workflow.placements||[]).filter(p=>projectLanes.has(p.laneId)).map(p=>p.agentId));for(const id of ids)owner.append(new Option(identity(id,workflow)?.title||id,id));
  status.name='status';for(const v of ['Captured','Assigned','In progress','Awaiting review','Reported complete','Deferred','Rejected'])status.append(new Option(v,v));
  selector.onchange=()=>{const r=latest.get(selector.value);title.value=r?.title||'';text.value=r?.text||'';acceptance.value=r?.acceptance||'';owner.value=r?.owner||'';status.value=r?.status||'Captured';};
  const submit=el('button','Save requirement version','primary');submit.type='submit';
  form.append(label('Requirement',selector),label('Title',title),label('Requested outcome',text),label('Acceptance criteria',acceptance),label('Responsible identity',owner),label('Status',status),el('p',pins().length+' opened source versions will be pinned. Saving records an ask or a status claim; it does not verify completion.','status'),submit,error);
  form.onsubmit=async e=>{e.preventDefault();if(run!==generation)return;submit.disabled=true;const payload={...Object.fromEntries(new FormData(form)),project:data.project,lane:data.lane,team:data.team,event:'requirement',evidence:pins(),version:data.version,contextFingerprint:data.context.fingerprint};const serialized=JSON.stringify(payload);if(form.dataset.payload!==serialized){form.dataset.payload=serialized;form.dataset.key=crypto.randomUUID();}try{await post('lifecycle',{...payload,requestId:form.dataset.key});if(run===generation)await refresh();}catch(e){if(run===generation){error.textContent=e.message;submit.disabled=false;}}};section.append(form);return section;
 }
 async function refresh(){
  const run=++generation,selected=scope();root.replaceChildren(el('p','Reading Library workspace…','status'));
  const results=await Promise.allSettled([request('/api/library/workspace?'+new URLSearchParams(selected)),request('/api/library/services?'+new URLSearchParams({project:selected.project})),request('/api/workflow')]);
  if(run!==generation)return;
  if(results[0].status==='rejected'){root.replaceChildren(el('p',results[0].reason.message,'notice-error'),button('Retry workspace',refresh));return;}
  const data=results[0].value,registry=results[1].status==='fulfilled'?results[1].value:null,workflow=results[2].status==='fulfilled'?results[2].value:{sessions:[]};current=data;
  if(data.project!==selected.project||data.lane!==selected.lane||data.team!==selected.team){root.replaceChildren(el('p','Workspace scope changed. Reload to continue.','notice-error'));return;}
  const serviceOK=registry?.project===selected.project&&registry.schemaReady===true;
  const homes=serviceOK?registry.homes||[]:[],services=serviceOK?registry.services||[]:[];
  root.replaceChildren();
  const controls=el('div',undefined,'workspace-controls'),areas=el('select'),teams=el('select');areas.setAttribute('aria-label','Workspace area');areas.append(new Option('Whole project',''),...data.areas.map(a=>new Option(a.name,a.id)));areas.value=data.lane;areas.onchange=()=>changeScope(data.project,areas.value,'');
  teams.setAttribute('aria-label','Workspace team');teams.append(new Option('Whole workstream',''));for(const [id,name]of [['coordinator','Workstream Owner'],['researcher','Research'],['writer','Build & Integration'],['worker','Execution'],['auditor','Audit']])teams.append(new Option(name,id));for(const t of data.areas.find(a=>a.id===data.lane)?.teams||[])teams.append(new Option(t.name,t.id));teams.value=data.team;teams.disabled=!data.lane;teams.onchange=()=>changeScope(data.project,data.lane,teams.value);
  controls.append(areas,teams,button('Refresh workspace',refresh));root.append(controls);
  const staffing=sections('Library services'),unique=new Map(homes.map(h=>[h.agent,h]));
  staffing.append(el('p',!serviceOK?'Staffing registry unavailable':unique.size?unique.size+' enrolled librarian'+(unique.size===1?'':'s')+' · shared queues':'No enrolled librarians','workspace-summary'));
  const homeList=el('div',undefined,'library-homes');for(const h of unique.values()){const card=el('article',undefined,'library-home');card.dataset.agent=h.agent;card.append(conversation(h.agent,workflow),el('code',h.agent),el('p',h.provider+' · queue '+h.queue,'status'));const assignments=services.filter(s=>s.agent===h.agent);card.append(el('p',assignments.length+' service references · one recorded queue','status'));homeList.append(card);}staffing.append(homeList);
  const roleSeats=(workflow.placements||[]).filter(p=>p.laneId==='project-'+data.project+'-librarian'&&!unique.has(p.agentId));if(roleSeats.length){staffing.append(el('h3','Assigned Library roles'));for(const p of roleSeats){const row=el('div',undefined,'service-reference');row.append(conversation(p.agentId,workflow),el('span',p.role+' · role assigned, service enrollment not recorded','status'));staffing.append(row);}}
  const selectedServices=services.filter(s=>!data.lane||!s.lane||s.lane===data.lane).filter(s=>!data.team||!s.team||s.team===data.team);
  if(!selectedServices.length)staffing.append(el('p',serviceOK?'No service is enrolled for this scope.':'Service availability could not be read.','status'));
  for(const s of selectedServices){const row=el('div',undefined,'service-reference');row.dataset.service=s.id;row.append(el('strong',s.kind==='context'?'Context':'Research'),conversation(s.agent,workflow),el('span',s.state+' · '+(s.scopeUsable?'scope admitted':s.scopeBlocker||'scope unavailable'),'status'),el('code',s.queue||homes.find(h=>h.agent===s.agent)?.queue||'Queue unavailable'));staffing.append(row);}
  staffing.append(el('p','Enrollment and service references do not establish a running agent. Conversations show recorded activity.','status'));root.append(staffing);
  const coverage=sections('Project coverage'),grid=el('div',undefined,'area-grid');
  for(const a of data.areas){const card=el('article',undefined,'area-card');card.dataset.area=a.id;const heading=button(a.name,()=>changeScope(data.project,a.id,''));heading.setAttribute('aria-current',String(a.id===data.lane));card.append(heading,el('p',a.objective||'Objective not recorded'),el('p',a.lifecycle+(a.mergedInto?' → '+a.mergedInto:''),'status'));
   const refs=services.filter(s=>s.lane===a.id||!s.lane),missing=[...a.missing];for(const kind of ['context','research'])if(!refs.some(s=>s.kind===kind&&s.state==='active'&&s.scopeUsable))missing.push((kind==='context'?'Context':'Research')+(serviceOK?' service not usable':' service unverified'));
   card.append(el('p',Object.values(a.counts).reduce((n,v)=>n+v,0)+' records · '+a.contextCount+' saved contexts','status'));const gaps=el('ul',undefined,'area-gaps');for(const gap of missing)gaps.append(el('li',gap));if(!missing.length)gaps.append(el('li','Required records present · adoption still requires verification'));card.append(gaps);
   for(const id of a.owners)card.append(conversation(id,workflow));grid.append(card);
  }coverage.append(grid);root.append(coverage);
  const context=sections('Scope and decisions');context.append(el('p',(data.team?'Team':data.lane?'Workstream':'Project')+' context · version '+data.context.current.version,'status'),fieldText(data.context.current.body));if(!data.context.current.version)context.append(el('p','No saved context in this scope.','status'));
  const versions=el('details');versions.append(el('summary','Context history · '+data.contextVersions.length+' versions'));for(const v of data.contextVersions){const entry=el('details');entry.append(el('summary','Version '+v.version+' · '+v.updated),fieldText(v.body));versions.append(entry);}context.append(versions);root.append(context);
  root.append(requirements(data,workflow,run));
  const history=sections('Founding scope and evidence history');if(!data.foundingScope)history.append(el('p','Founding source not recorded. Pin the original directions or source before recording it.','status'));
  for(const record of data.history.filter(r=>r.event!=='requirement')){const entry=el('article',undefined,'lifecycle-record');entry.dataset.event=record.event;entry.append(el('h3',({foundation:'Founding scope',revision:'Scope revision',disposition:'Receiving disposition'})[record.event]),el('p',record.text),el('p','Version '+record.version+' · '+record.createdAt+' · evidence '+record.evidenceState+' · '+(record.contextCurrent?'current context':'earlier context'),'status'));
   if(record.event==='disposition')entry.append(el('p',record.decision+' · recorded, adoption unverified','adoption-claim'),conversation(record.receivingOwner,workflow));
   for(const pin of record.evidence)entry.append(button('Read pinned source · '+pin.sha.slice(0,12),()=>openRecord(pin.id,pin.sha)));history.append(entry);
  }
  const area=data.areas.find(a=>a.id===data.lane);if(area&&['closed','merged'].includes(area.lifecycle))history.append(el('p','This workstream is '+area.lifecycle+'. Its history remains readable.','status'));else history.append(eventForm(data,workflow,run));root.append(history);
 }
 return {refresh,get current(){return current;},invalidate(){generation++;}};
}};
