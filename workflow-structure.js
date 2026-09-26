'use strict';
window.WorkflowStructure=(()=>{
 const labels={coordinator:'Workstream Owner',researcher:'Research',writer:'Build & Integration',worker:'Execution',auditor:'Independent Audit'};
 const descriptions={coordinator:'Design and delivery',researcher:'Research and findings',writer:'Build and integrate',worker:'Jobs and results',auditor:'Independent review'};
 const sections=[{id:'owner',title:'Workstream Owner',role:'coordinator'},{id:'research',title:'Research',role:'researcher'},{id:'build',title:'Build & Integration',role:'writer'},{id:'execution',title:'Execution & jobs',role:'worker'},{id:'evidence',title:'Evidence & context'},{id:'review',title:'Review',role:'auditor'}];
 const el=(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls||'';if(text!==undefined)n.textContent=text;return n;};
 const button=(text,fn,cls='')=>{const n=el('button',cls,text);n.type='button';n.onclick=e=>{e.stopPropagation();fn();};return n;};
 const link=(text,url,cls='')=>{const n=el('a',cls,text);n.href=url;return n;};
 function create(ctx){
  const data=()=>ctx.data(),session=id=>data().sessions.find(s=>s.agent_id===id),seats=(l,role)=>data().placements.filter(p=>p.laneId===l.id&&(!role||p.role===role));
  const responsibility=t=>{
   if(t.heldByWorker&&t.workerId)return session(t.workerId)?.title||t.workerId;
   if(t.state==='LINKED'||t.intake?.canonicalTaskIds?.length)return 'Linked to existing work';
   if(t.intakeDisposition?.executable===false)return (t.intakeDisposition.kind==='container'?'Work group · ':'Record owner · ')+(session(t.ownerId)?.title||t.ownerId||'Unrecorded');
   return ({CAPTURED:'Needs a scoped task contract',REVIEW:'Awaiting independent review',DONE:'Completed',CANCELLED:'Cancelled',NEEDS_COORDINATION:'Needs coordination'}[t.state])||'Awaiting an eligible worker';
  };
  const href=(l,section='')=>'/constellations?project='+encodeURIComponent(l.projectId)+'&lane='+encodeURIComponent(l.id)+(section?'&section='+section:'');
  const library=(project,lane='')=>'/library?project='+encodeURIComponent(project)+(lane?'&lane='+encodeURIComponent(lane):'');
  const lead=(l,role)=>data().teamLeads.find(t=>t.laneId===l.id&&t.teamId===role);
  const retired=l=>l.lifecycle&&l.lifecycle!=='active';
  const projectTeam=(project,key)=>data().lanes.find(l=>l.id==='project-'+project+'-'+key);
  const numberLabel=l=>{if(l.kind==='support')return 'SHARED SUPPORT';const own=data().lanes.filter(x=>x.projectId===l.projectId&&!retired(x)&&x.kind!=='support'&&x.supportMode!=='alignment'&&!x.id.startsWith('project-'+x.projectId+'-')),index=own.findIndex(x=>x.id===l.id);return index>=0?'LANE '+String(index+1).padStart(2,'0'):'WORKSTREAM'};
  function roster(l,role,compact=true,team=role){
   const list=el('div','wf-agents');const members=seats(l,role).filter(p=>(p.teamId||p.role)===team);
   const visible=members.filter(p=>!p.teamId?.startsWith('lib-')),leadId=lead(l,team)?.agentId;for(const p of visible){const s=session(p.agentId);if(!s)continue;const card=ctx.card(s,true);if(data().teamLeads.find(t=>t.laneId===l.id&&t.teamId===team)?.agentId===p.agentId)card.classList.add('wf-lead-agent');list.append(card);}
   if(!members.filter(p=>!p.teamId?.startsWith('lib-')).length){const drop=el('div','wf-empty-seat');drop.append(el('span','','Drop agent'),button('+',()=>ctx.openMember(l.id,role,team),'wf-add-agent'));list.append(drop);}
   if(!retired(l))ctx.dropZone(list,l.id,role,team===role?'':team);for(const t of data().teams.filter(t=>t.laneId===l.id&&t.parentId===team&&!t.id.startsWith('lib-'))){const child=el('section','wf-nested-team');child.dataset.nestedTeam=t.id;const head=el('div','wf-nested-head');head.append(el('strong','',t.name),button('⋯',()=>ctx.openTeamEditor(l.id,role,t.id),'wf-icon'));child.append(head,roster(l,role,compact,t.id));if(role==='researcher')child.append(librarians(l,t.id,compact));list.append(child);}return list;
  }
  function section(l,s,compact){const box=el('section','wf-section wf-'+s.id);box.dataset.section=s.id;const head=el('div','wf-section-head');head.append(link(s.title,href(l,s.id)),button('⋯',()=>ctx.openTeamEditor(l.id,s.role,s.role),'wf-team-menu'));box.append(head,roster(l,s.role,compact));if(s.role==='researcher')box.append(librarians(l,'researcher',compact));if(['researcher','writer'].includes(s.role)){const teams=el('div','wf-team-branch-control');teams.append(button(s.role==='researcher'?'+ Specialist team':'+ Writer team',()=>ctx.openTeamEditor(l.id,s.role,null,s.role),'wf-text-button'),button('+ Agent',()=>ctx.openMember(l.id,s.role,s.role),'wf-text-button'));box.append(teams);}return box;}
  // Rooms are a projection of the saved lane/team structure. Their entrances are
  // stable receiving anchors for the separately owned road/courier component.
  function inventoryItems(agentId){
   return (data().tasks||[]).filter(t=>t.heldByWorker&&t.workerId===agentId);
  }
  function paintInventory(box,agentId){
   if(!box||!agentId)return;
   let inv=box.querySelector('.wf-agent-inventory');
   if(!inv){inv=el('div','wf-agent-inventory');inv.setAttribute('aria-label','Live inventory');box.append(inv);}
   const items=inventoryItems(agentId),sig=JSON.stringify(items.map(t=>[t.taskId,t.state,t.title,t.displayState]));
   if(inv.dataset.signature===sig)return;inv.dataset.signature=sig;inv.replaceChildren(el('span','wf-inventory-label','Inventory'));
   if(!items.length){inv.append(el('span','wf-inventory-empty','Empty · no live assignment'));return;}
   for(const t of items){
    const row=button('',()=>window.SwitchboardTasks?.open(t.taskId),'wf-inventory-item');
    row.dataset.taskId=t.taskId;row.append(el('span','wf-task-state',window.SwitchboardTasks?.stateName(t.displayState||t.state)||t.state),el('strong','',t.title));
    inv.append(row);
   }
  }
  function deskBox(card){
   if(card.closest('.wf-agent-box'))return card.closest('.wf-agent-box');
   const box=el('div','wf-agent-box');box.dataset.agentBox=card.dataset.agentId;card.classList.add('wf-bunker-desk');card.dataset.deskAgent=card.dataset.agentId;
   card.parentNode.insertBefore(box,card);box.append(card);paintInventory(box,card.dataset.agentId);return box;
  }
  function room(l,s,support=false){
   const r=section(l,s,true);r.classList.add('wf-bunker-room');r.dataset.roomId=l.id+':'+s.id;r.dataset.roomLane=l.id;r.dataset.roomTeam=s.role;r.setAttribute('role','region');
   const heading=r.querySelector('.wf-section-head>a');heading.id='room-'+encodeURIComponent(l.id+'-'+s.id);r.setAttribute('aria-labelledby',heading.id);
   if(support&&s.id==='owner')heading.textContent=l.id==='project-'+l.projectId+'-chief'?'Chief of Staff':l.supportMode==='alignment'?'Alignment':'Service owner';
   const captions={owner:'Leadership office',research:'Research lab',build:'Developer studio',execution:'Operations room',review:'Independent review room'};
   r.querySelector('.wf-section-head').after(el('span','wf-room-caption',captions[s.id]));
   for(const card of r.querySelectorAll('.session'))deskBox(card);
   for(const empty of r.querySelectorAll('.wf-empty-seat>span'))empty.textContent='Vacant desk';
   if(s.id==='review'){const queue=el('div','tf-review-queue');queue.dataset.reviewLane=l.id;r.append(queue);renderReviewQueue(l,queue);}
   const door=link('Enter '+heading.textContent,href(l,s.id),'wf-room-door');door.dataset.roomEntrance=r.dataset.roomId;door.setAttribute('aria-label','Open '+heading.textContent+' in '+l.name);r.append(door);return r;
  }
  function taskBoard(l){
   const r=el('section','wf-bunker-room wf-bunker-tasks');r.dataset.roomId=l.id+':tasks';r.dataset.roomLane=l.id;r.setAttribute('aria-label','Recorded tasks for '+l.name);
   const head=el('div','wf-section-head');head.append(el('strong','','Task board'),button('Work orders',()=>ctx.work(),'wf-icon-link'));r.append(head,el('span','wf-room-caption','Recorded work · select a task to inspect'));
   const boards=el('div');boards.dataset.taskLane=l.id;r.append(boards);renderTaskBoard(l,boards);
   const door=button('Open work orders',()=>ctx.work(),'wf-room-door');door.dataset.roomEntrance=r.dataset.roomId;r.append(door);return r;
  }
  function renderTaskBoard(l,list){
   const rows=(data().tasks||[]).filter(t=>t.laneId===l.id),signature=JSON.stringify(rows.map(t=>[t.taskId,t.version,t.state,t.title,t.workerId,t.waitReason,t.intakeDisposition,t.intake?.canonicalTaskIds]));if(list.dataset.signature===signature)return;
   const focus=list.contains(document.activeElement)?document.activeElement.dataset.taskId:null;list.dataset.signature=signature;list.replaceChildren();
   const actions=el('div','tf-board-actions');actions.append(button('+ Task',()=>window.SwitchboardTasks?.capture(l.id)),button('Dispatch',()=>window.SwitchboardTasks?.setup(l.id)),button('Audit team',()=>window.SwitchboardTasks?.auditTeam()));list.append(actions);
   const waitingStates=['CAPTURED','LINKED','NEEDS_COORDINATION','QUEUED','READY'],done=rows.filter(t=>['DONE','CANCELLED'].includes(t.state));
   const waiting=rows.filter(t=>waitingStates.includes(t.state)),active=rows.filter(t=>!['DONE','CANCELLED'].includes(t.state)&&!waitingStates.includes(t.state));
   const render=(t,parent,showWait)=>{const row=button('',()=>window.SwitchboardTasks?.open(t.taskId),'wf-bunker-task');row.dataset.taskId=t.taskId;row.title=t.request+' · '+(t.waitReason||t.state);const anchor=el('span','tf-task-anchor','');anchor.dataset.taskAnchor=t.taskId;row.append(anchor,el('span','wf-task-state',window.SwitchboardTasks?.stateName(t.displayState||t.state)||t.state),el('strong','',t.title),el('small','',responsibility(t)));if(showWait&&t.waitReason)row.append(el('small','tf-wait-reason',t.waitReason));parent.append(row)};
   list.append(el('span','tf-task-count',active.length+' in flight · '+waiting.length+' waiting · '+done.length+' completed'));
   const openList=el('div','wf-bunker-task-list');for(const t of active)render(t,openList,true);list.append(openList);
   const hold=el('section','tf-waiting-board');hold.append(el('strong','','Waiting on setup or existing work ('+waiting.length+')'));const holdList=el('div','wf-bunker-task-list');for(const t of waiting)render(t,holdList,true);if(!waiting.length)hold.append(el('p','wf-room-vacancy','No waiting tasks'));hold.append(holdList);list.append(hold);
   const history=el('section','tf-completed-board');history.dataset.roomId=l.id+':completed';history.append(el('strong','tf-completed-station','Completed tasks ('+done.length+')'));const doneList=el('div','wf-bunker-task-list');for(const t of done)render(t,doneList);history.append(doneList);list.append(history);
   if(!rows.length)list.append(el('p','wf-room-vacancy','No task records linked'));
   if(focus)[...list.querySelectorAll('[data-task-id]')].find(x=>x.dataset.taskId===focus)?.focus({preventScroll:true});
  }
  function libraryRoom(l){
   const r=el('section','wf-bunker-room wf-bunker-library');r.dataset.section='evidence';r.dataset.roomId=l.id+':evidence';r.dataset.roomLane=l.id;r.setAttribute('aria-label','Library for '+l.name);
   const head=el('div','wf-section-head');head.append(link('Library',library(l.projectId,l.id)));r.append(head,el('span','wf-room-caption','Shared sources, requirements and retained results'));
   const shelves=el('div','wf-room-shelves');for(const [label,panel]of [['Sources','records'],['Requirements','requirements'],['Workspace','workspace']])shelves.append(link(label,library(l.projectId,l.id)+'&panel='+panel,'wf-library-shelf'));r.append(shelves);
   const librarians=data().placements.filter(p=>p.laneId===l.id&&p.teamId?.startsWith('lib-'));r.append(el('span','wf-room-vacancy',librarians.length?'Assigned librarians are at their saved team desks':'No librarian assigned in this lane'));
   const door=link('Open Library',library(l.projectId,l.id),'wf-room-door');door.dataset.roomEntrance=r.dataset.roomId;r.append(door);return r;
  }
  function floor(l){
   const n=el('div','wf-bunker-floor');n.dataset.bunkerLane=l.id;
   if(retired(l))n.append(el('div','wf-retired','Retired · disarmed'));
   if(isProjectFunction(l)){
    n.classList.add('wf-support-floor');for(const s of sections.filter(x=>x.role&&seats(l,x.role).length))n.append(room(l,s,true));
    if(!seats(l).length)n.append(el('p','wf-room-vacancy','No agents assigned to this support function'));
    n.append(taskBoard(l),libraryRoom(l));
   }else{
    const west=el('div','wf-bunker-west'),studio=el('div','wf-bunker-studio'),services=el('div','wf-bunker-services');
    west.append(room(l,sections[0]),libraryRoom(l),room(l,sections[1]));
    studio.append(room(l,sections[2]),taskBoard(l));
    services.append(room(l,sections[3]),room(l,sections[5]));
    n.append(west,studio,services);
   }
   // Retain malformed or missing-session placements visibly instead of silently
   // dropping real membership from the world. This does not repair assignment.
   const shown=new Set([...n.querySelectorAll('[data-desk-agent]')].map(x=>x.dataset.deskAgent));
   const unshown=seats(l).filter(p=>!shown.has(p.agentId));if(unshown.length){const missing=el('section','wf-bunker-room wf-unresolved-seats');missing.setAttribute('aria-label','Unresolved placement records');missing.append(el('strong','','Placement needs inspection'));for(const p of unshown){const s=session(p.agentId);if(s){const c=ctx.card(s,true);missing.append(c);deskBox(c);}else missing.append(el('p','wf-room-vacancy',p.agentId+' · session unavailable'));}n.append(missing);}
   return n;
  }
  function overview(l){const n=el('div','wf-overview wf-bunker-overview');n.append(floor(l));return n;}
  function renderReviewQueue(l,list){const tasks=(data().tasks||[]).filter(t=>t.laneId===l.id&&t.state==='REVIEW'),key=JSON.stringify(tasks.map(t=>[t.taskId,t.version]));if(list.dataset.signature===key)return;list.dataset.signature=key;list.replaceChildren(el('span','wf-small-label','Result review queue'));for(const t of tasks){const row=button(t.title,()=>window.SwitchboardTasks?.open(t.taskId),'tf-review-task');row.dataset.taskId=t.taskId;const anchor=el('span','tf-task-anchor','');anchor.dataset.reviewTask=t.taskId;row.prepend(anchor);list.append(row);}if(!tasks.length)list.append(el('span','wf-muted','No returned results waiting'));}
  function updateRecords(){for(const list of document.querySelectorAll('[data-task-lane]')){const l=data().lanes.find(x=>x.id===list.dataset.taskLane);if(l)renderTaskBoard(l,list);}for(const list of document.querySelectorAll('[data-review-lane]')){const l=data().lanes.find(x=>x.id===list.dataset.reviewLane);if(l)renderReviewQueue(l,list);}for(const box of document.querySelectorAll('.wf-agent-box'))paintInventory(box,box.dataset.agentBox);}
  function evidence(l,parent){const target=el('div','wf-evidence-list');parent.append(target);target.append(el('span','wf-muted','Loading records…'));fetch('/api/library?project='+encodeURIComponent(l.projectId)+'&lane='+encodeURIComponent(l.id)).then(r=>r.json()).then(d=>{target.replaceChildren();for(const x of (d.items||[]).slice(0,6)){const a=link(x.title,library(l.projectId,l.id)+'&item='+x.id,'wf-evidence-item');a.append(el('span','',x.kind+' · '+x.state));target.append(a);}if(!d.items?.length)target.append(link('Open library',library(l.projectId,l.id)));}).catch(()=>target.replaceChildren(link('Open library',library(l.projectId,l.id))));}
  function work(l,parent){const tasks=(data().tasks||[]).filter(t=>t.laneId===l.id);for(const t of tasks){const row=button('',()=>window.SwitchboardTasks?.open(t.taskId),'wf-work-record');row.dataset.taskId=t.taskId;row.append(el('span','wf-small-label',t.state),el('strong','',t.title),el('small','',responsibility(t)));parent.append(row);}if(!tasks.length)parent.append(el('span','wf-muted','No work records linked'));parent.append(button('Open work orders',()=>ctx.work()));}
  function detail(l){const n=el('section','lane wf-detail');n.dataset.laneId=l.id;n.style.left='35px';n.style.top='25px';const selected=new URLSearchParams(location.search).get('section'),s=sections.find(x=>x.id===selected);const top=el('div','wf-detail-top');top.append(link('← '+(s?l.name:'All workstreams'),s?href(l):'/constellations?project='+l.projectId),button('⋯',()=>ctx.openLaneEditor(l.id)));n.append(top);const header=el('div','wf-detail-title');header.append(el('span','wf-small-label',s?l.name:numberLabel(l)),el('h2','',s?s.title:l.name));if(retired(l))header.append(el('span','wf-retired','Retired · disarmed'));n.append(header);
   if(s?.id==='evidence'){location.href=library(l.projectId,l.id);return n;}
   if(s){const tools=el('div','wf-detail-tools');if(s.id==='research'||s.id==='build'){tools.append(link('Research library',library(l.projectId,l.id)),button('+ Add agent',()=>ctx.openMember(l.id,s.role,s.role)),button('Set lead / teams',()=>ctx.openTeamEditor(l.id,s.role,s.role)));}if(s.id==='owner')tools.append(button('Set owner',()=>ctx.openTeamEditor(l.id,s.role,s.role)));if(s.id==='execution')tools.append(link('CPU · GPU · swarms','/agents?project='+l.projectId),button('Work orders',()=>ctx.work()));n.append(tools);if(s.role)n.append(ctx.teamBlock(l,s.role,s.role,true));const bottom=el('div','wf-detail-records');bottom.append(el('h3','','Latest work'));work(l,bottom);if(['research','review','owner'].includes(s.id)){bottom.append(el('h3','','Evidence'));evidence(l,bottom);}n.append(bottom);return n;}
   n.classList.add('wf-bunker-detail');n.append(floor(l));return n;
  }
  function librarianId(l,team,kind){return 'lib-'+kind+'-'+l.id+'-'+team;}
  async function assignLibrarian(l,team,kind,id){const tid=librarianId(l,team,kind),name=kind==='context'?'Context Librarian':'Research Librarian';if(!data().teams.some(t=>t.id===tid)){if(!await ctx.mutate('team',{id:tid,laneId:l.id,role:'researcher',parentId:team,name,objective:kind==='context'?'Maintain the team question, current context, decisions, handoffs and unresolved contradictions.':'Search prior research, preserve source versions and evidence, and record reuse or new-research decisions.'}))return;}if(await ctx.mutate('place',{agentId:id,laneId:l.id,role:'researcher',teamId:tid}))await ctx.mutate('team-lead',{laneId:l.id,teamId:tid,agentId:id});}
  function librarianPicker(l,team,kind){const d=el('dialog','wf-picker'),head=el('div','wf-picker-head');head.append(el('h2','',kind==='context'?'Context Librarian':'Research Librarian'),button('×',()=>d.close()));const input=el('input');input.type='search';input.placeholder='Assign an existing agent';const list=el('div','wf-picker-list');function render(){list.replaceChildren();for(const s of data().sessions.filter(s=>s.title.toLowerCase().includes(input.value.toLowerCase())).slice(0,40))list.append(button(s.title,async()=>{d.close();await assignLibrarian(l,team,kind,s.agent_id);},'wf-agent-choice'));}input.oninput=render;d.append(head,input,list);document.body.append(d);d.onclose=()=>d.remove();render();d.showModal();}
  function librarians(l,team='researcher',compact=false){const root=el('div','wf-librarians');root.dataset.researchTeam=team;for(const [kind,title,symbol]of [['context','Context Librarian','◈'],['research','Research Librarian','▤']]){const tid=librarianId(l,team,kind),slot=el('section','wf-librarian');slot.dataset.librarian=kind;const header=el('div','wf-librarian-head');const url=library(l.projectId,l.id)+'&team='+encodeURIComponent(team)+(kind==='context'?'&view=context':'');header.append(link(symbol+' '+title,url),button('+',()=>librarianPicker(l,team,kind),'wf-icon'));slot.append(header);const placed=data().placements.filter(p=>p.laneId===l.id&&p.teamId===tid);for(const p of placed){const s=session(p.agentId);if(s)slot.append(ctx.card(s,true));}if(!placed.length)slot.append(el('span','wf-unassigned','Agent unassigned'));slot.ondragover=e=>{if(e.dataTransfer.types.includes('application/x-ke-session')){e.preventDefault();e.stopPropagation();slot.classList.add('over');}};slot.ondragleave=()=>slot.classList.remove('over');slot.ondrop=async e=>{e.preventDefault();e.stopPropagation();slot.classList.remove('over');const id=e.dataTransfer.getData('application/x-ke-session');if(session(id))await assignLibrarian(l,team,kind,id);};root.append(slot);}return root;}
  async function assignProject(project,key,agent){const audit=['alignment','audit'].includes(key),existing=audit?data().lanes.find(l=>l.projectId===project&&l.supportMode==='alignment'):projectTeam(project,key),id=existing?.id||'project-'+project+'-'+(audit?'alignment':key),name=key==='chief'?'Chief of Staff':audit?'Alignment & Audit':'Project Librarian',role=key==='audit'?'auditor':'coordinator';if(!existing){if(!await ctx.mutate('lane-create',{id,projectId:project,name,objective:key==='chief'?'the operator’s project strategist and coordination lead.':audit?'Independent alignment and evidence review across project workstreams.':'Maintain project research, decisions and current context.',kind:'support',supportMode:audit?'alignment':'general'}))return;}if(await ctx.mutate('place',{agentId:agent,laneId:id,role,teamId:''}))await ctx.mutate('team-lead',{laneId:id,teamId:role,agentId:agent});}
  function chooseProjectAgent(project,key){const d=el('dialog','wf-picker');const header=el('div','wf-picker-head');header.append(el('h2','',key==='chief'?'Chief of Staff':key==='alignment'?'Alignment':key==='audit'?'Independent Audit':'Project Librarian'),button('×',()=>d.close()));const input=el('input');input.type='search';input.placeholder='Find an existing agent';input.setAttribute('aria-label','Find an existing agent');const list=el('div','wf-picker-list');const render=()=>{list.replaceChildren();data().sessions.filter(s=>s.title.toLowerCase().includes(input.value.toLowerCase())).slice(0,30).forEach(s=>list.append(button(s.title,async()=>{d.close();await assignProject(project,key,s.agent_id);},'wf-agent-choice')));};input.oninput=render;d.append(header,input,list);document.body.append(d);d.addEventListener('close',()=>d.remove());render();d.showModal();input.focus();}
  const reviewCache=new Map();
  function reviewHistory(project,box){if(!reviewCache.has(project))reviewCache.set(project,fetch('/api/reviews?project='+encodeURIComponent(project),{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json();}));reviewCache.get(project).then(d=>{if(!box.isConnected||!d.reviews?.length)return;const ref=button(d.reviews.length+' bounded reviews · '+(session(d.reviewer)?.title||d.reviewerTitle),()=>{const dialog=el('dialog','wf-review-history'),head=el('div','wf-picker-head');head.append(el('h2','','Independent review history'),button('Close',()=>dialog.close()));dialog.append(head,el('p','',d.notice),button(session(d.reviewer)?.title||d.reviewerTitle,()=>{dialog.close();ctx.openTranscript(d.reviewer);},'wf-text-button'));for(const r of d.reviews){const item=el('details'),summary=el('summary','',r.disposition+' · '+(r.versionOrCommit?.slice(0,12)||'Bounded candidate'));item.append(summary,el('p','',JSON.stringify(r.scope)),el('code','',r.reviewRecord.immutableRef),el('small','',r.reviewRecord.locator));dialog.append(item);}document.body.append(dialog);dialog.onclose=()=>dialog.remove();dialog.showModal();},'wf-review-reference');ref.title='Exact version-bound reviews; no whole-platform completion';box.append(ref);}).catch(()=>{});}
  function projectLayer(){
   const project=ctx.project();
   return button('Leadership & audit',()=>{
    const d=el('dialog','wf-project-people'),head=el('div','wf-picker-head');
    head.append(el('h2','','Leadership & audit'),button('Close',()=>d.close()));d.append(head);
    const route=el('p','wf-people-route','You → project leadership → task teams');d.append(route);
    const exactRef=(id)=>{const person=session(id),ref=button(person?.title||id,()=>{d.close();ctx.openTranscript(id);},'wf-person-ref');ref.dataset.agentRef=id;ref.title=id;return ref;};
    const group=(title,l,roles,emptyKey)=>{
     const box=el('section','wf-people-group');box.append(el('strong','wf-small-label',title));
     const members=l?seats(l).filter(p=>roles.includes(p.role)):[],seen=new Set();
     for(const p of members){if(seen.has(p.agentId))continue;seen.add(p.agentId);box.append(exactRef(p.agentId));}
     if(!members.length)box.append(emptyKey?button('Choose existing agent',()=>{d.close();chooseProjectAgent(project,emptyKey);},'wf-text-button'):el('span','wf-muted','No assigned agent'));
     if(l)box.append(link('Open '+l.name,href(l),'wf-people-lane'));d.append(box);return box;
    };
    const workLanes=data().lanes.filter(l=>l.projectId===project&&!retired(l)&&!isProjectFunction(l));
    if(workLanes.length===1){const box=group('Workstream Owner · shared project leadership',workLanes[0],['coordinator']);box.append(el('span','wf-muted','One workstream: the owner also carries project coordination.'));}
    else{group('Chief of Staff',projectTeam(project,'chief'),['coordinator'],'chief');for(const l of workLanes)group(l.name+' · Workstream Owner',l,['coordinator']);}
    const audit=data().lanes.find(l=>l.projectId===project&&l.supportMode==='alignment');
    group('Alignment',audit,['coordinator'],'alignment');const reviews=group('Independent Audit',audit,['auditor'],'audit');reviewHistory(project,reviews);reviews.append(button('Audit queue & capacity',()=>window.SwitchboardTasks?.auditTeam()));
    if(audit&&seats(audit).some(p=>!['coordinator','auditor'].includes(p.role)))group('Alignment team',audit,['researcher','writer','worker']);
    for(const l of data().lanes.filter(l=>l.projectId===project&&isProjectFunction(l)&&l!==audit&&l!==projectTeam(project,'chief')&&!retired(l))){if(seats(l).length)group(l.name,l,data().roles);}
    document.body.append(d);d.onclose=()=>d.remove();d.showModal();
   },'wf-leadership-control');
  }
  const isProjectFunction=l=>l.kind==='support'||l.supportMode==='alignment'||l.id.startsWith('project-'+l.projectId+'-');

  function layerNavigation(l,sectionId,agentId){
   const root=el('nav','wf-layer-navigation');root.setAttribute('aria-label','Workflow layers');root.append(el('span','wf-layer-label','Layers'));
   const active=agentId?'work':sectionId?'team':l?'lane':'project';
   const item=(label,key,fn,url)=>{const n=url?link(label,url,'wf-layer-link'):button(label,fn,'wf-layer-link');n.dataset.layer=key;n.title=label;n.setAttribute('aria-current',active===key?'page':'false');root.append(n);return n;};
   item('Project','project',null,'/constellations?project='+encodeURIComponent(ctx.project()));
   const workLanes=data().lanes.filter(x=>x.projectId===ctx.project()&&!retired(x)&&!isProjectFunction(x)),chosen=l||(workLanes.length===1?workLanes[0]:null);
   const pick=(title,options)=>{const d=el('dialog','wf-layer-picker'),head=el('div','wf-picker-head');head.append(el('h2','',title),button('Close',()=>d.close()));d.append(head);for(const [name,url]of options)d.append(link(name,url,'wf-layer-choice'));document.body.append(d);d.onclose=()=>d.remove();d.showModal();};
   item(l?l.name:'Lane','lane',()=>pick('Open a lane',workLanes.map(x=>[x.name,href(x)])),chosen?href(chosen):null);
   const teamButton=item(sectionId?sections.find(x=>x.id===sectionId)?.title||'Team':'Team','team',()=>pick('Open a department',sections.filter(x=>x.role).map(x=>[x.title,href(chosen,x.id)])));teamButton.disabled=!chosen;
   const person=agentId||ctx.selected?.(),seat=person&&data().placements.find(p=>p.agentId===person),same=seat&&data().lanes.find(x=>x.id===seat.laneId)?.projectId===ctx.project();
   const focus=item('Work','work',()=>ctx.openFocus(person));focus.disabled=!same;focus.title=same?'Focus on this agent and its recorded work':'Select an agent to focus on its work';return root;
  }
  function focusedWork(l,id){
   const person=session(id),root=el('section','lane wf-detail wf-focused');root.dataset.laneId=l.id;root.dataset.focusAgent=id;root.style.left='35px';root.style.top='25px';
   const top=el('div','wf-detail-top');top.append(link('← '+l.name,href(l)),el('span','wf-small-label','FOCUSED WORK'));root.append(top,el('h2','wf-focus-title',person.title));
   const actor=el('div','wf-focus-actor');actor.append(ctx.card(person,true));root.append(actor);
   const task=person.latestTask,record=el('article','wf-focus-record');record.append(el('h3','',task?'Latest recorded task':'No task record linked'));
   if(task){record.append(el('p','wf-recorded-status',(task.status||'State not reported').replaceAll('_',' ')),el('p','wf-focus-objective',task.objective||'Objective not recorded'));for(const [name,value]of [['Next step',task.next_action||task.nextAction],['Dependency',task.blocker]])if(value){record.append(el('h4','',name),el('p','',value));}record.append(el('small','wf-muted','Recorded task state; current work and completion require their own evidence.'));}
   const actions=el('div','wf-detail-tools');actions.append(button('Open conversation',()=>ctx.openTranscript(id)),button('Work & history',()=>ctx.work()),link('Lane Library',library(l.projectId,l.id)));record.append(actions);root.append(record);return root;
  }
  return {overview,detail,focusedWork,layerNavigation,projectLayer,librarians,isProjectFunction,updateRecords,projectButton:()=>link('Library',library(ctx.project()))};
 }
 function icon(kind){const paths={inbox:'M3 4h14v10H3z M3 10h4l2 3h2l2-3h4',send:'M3 3l15 7-15 7 3-7z M6 10h12',results:'M5 2h7l4 4v12H5z M12 2v5h4 M8 11h5 M8 14h5'};const n=document.createElementNS('http://www.w3.org/2000/svg','svg');n.setAttribute('viewBox','0 0 20 20');n.setAttribute('aria-hidden','true');const p=document.createElementNS(n.namespaceURI,'path');p.setAttribute('d',paths[kind]||paths.results);p.setAttribute('fill','none');p.setAttribute('stroke','currentColor');p.setAttribute('stroke-width','1.4');p.setAttribute('stroke-linejoin','round');n.append(p);return n;}
 return Object.freeze({labels,descriptions,sections,create,icon});
})();
