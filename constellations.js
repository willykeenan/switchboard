'use strict';
window.SwitchboardHistoryInitialQuery=location.search;
const workflowRouteKey='switchboard.workflow-route.v1';
const workflowSections=new Set(['owner','research','build','execution','evidence','review']);
function restoreWorkflowRoute(){
 const query=new URLSearchParams(location.search);
 if(['project','lane','section','history'].some(k=>query.has(k)))return;
 try{const saved=JSON.parse(localStorage.getItem(workflowRouteKey));
  const valid=v=>typeof v==='string'&&/^[a-z0-9][a-z0-9_-]{0,79}$/.test(v);
  if(!saved||!valid(saved.project))return;
  const next=new URLSearchParams({project:saved.project});
  if(valid(saved.lane)){next.set('lane',saved.lane);if(workflowSections.has(saved.section))next.set('section',saved.section);if(saved.layer==='work'&&typeof saved.agent==='string'&&/^(codex|claude|grok):[a-zA-Z0-9_-]{1,160}$/.test(saved.agent)){next.set('layer','work');next.set('agent',saved.agent)}}
  history.replaceState(null,'','/constellations?'+next);
 }catch{}
}
restoreWorkflowRoute();
const $=id=>document.getElementById(id), el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};
let data=null,project=new URLSearchParams(location.search).get('project')||'demo',zoom=.8,selected=null,busy=false,dragging=false,shown=60,toastTimer,viewLane=new URLSearchParams(location.search).get('lane'),editingLane=null,editingTeam=null,memberTarget=null;
let framedProject=null,framedCameraKey=null,focusedDeskRoute=null;
const cameraKey=()=>{const q=new URLSearchParams(location.search),suffix=viewLane?['section','team','layer','agent'].map(k=>q.get(k)||'').filter(Boolean).join('.'):'';return 'switchboard.camera.v1.'+project+'.'+(viewLane||'overview')+(suffix?'.'+suffix:'');};
function savedCamera(key){try{const s=JSON.parse(localStorage.getItem(key));if(s&&[s.zoom,s.x,s.y].every(Number.isFinite)&&s.zoom>=.3&&s.zoom<=1.4&&Math.abs(s.x)<10000000&&Math.abs(s.y)<10000000)return s;}catch{}return null;}
function cameraPosition(){return window.SwitchboardCanvasPan?.camera?.()||{x:$('viewport').scrollLeft,y:$('viewport').scrollTop};}
function moveCamera(x,y){if(window.SwitchboardCanvasPan?.scrollTo)window.SwitchboardCanvasPan.scrollTo(x,y);else $('viewport').scrollTo(x,y);}
window.SwitchboardWorkflowCamera={capture(){saveCamera();return {key:framedCameraKey,...cameraPosition()};}};
function saveCamera(){if(!framedCameraKey||window.WorkflowLibraryView?.active)return;try{localStorage.setItem(framedCameraKey,JSON.stringify({zoom,...cameraPosition()}));}catch{}}

const names=WorkflowStructure.labels;
const workflowStructure=WorkflowStructure.create({data:()=>data,project:()=>project,teamBlock,card,selected:()=>selected,openFocus,openMember,openTeamEditor:openTeam,mutate,openTranscript:openSession,openLane,openLaneEditor,dropZone,work:()=>window.Switchboard.work()});
const trayHomesKey='switchboard.unassigned-projects.v1';let trayHomes={};try{const saved=JSON.parse(localStorage.getItem(trayHomesKey));if(saved&&typeof saved==='object'&&!Array.isArray(saved))trayHomes=saved}catch{}
function rememberTrayHome(id,home){if(home)trayHomes[id]=home;else delete trayHomes[id];try{localStorage.setItem(trayHomesKey,JSON.stringify(trayHomes))}catch{}}
const stateNames={TURN_STARTED:'Last event: turn started',TURN_FINISHED:'Last turn finished',TURN_STOPPED:'Last turn stopped',UNKNOWN:'Activity unknown'};
const seat=id=>data.placements.find(p=>p.agentId===id),session=id=>data.sessions.find(s=>s.agent_id===id),lane=id=>data.lanes.find(l=>l.id===id);
function say(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,2600)}
function error(text){$('error').textContent=text;$('error').hidden=!text}
const roleDetails=WorkflowStructure.descriptions;
const roleChoices=()=>data.roles.map(r=>({value:r,label:names[r],detail:roleDetails[r],tone:r}));
const laneChoice=l=>({value:l.id,label:l.name,detail:(data.projects.find(p=>p.id===l.projectId)?.name||'')+' · '+(l.objective||''),badge:data.placements.filter(p=>p.laneId===l.id).length+' members · '+(l.kind==='support'?'Shared support':'Project work')});
const sessionChoice=s=>({value:s.agent_id,label:s.title,detail:s.provider.toUpperCase()+' · '+(lane(seat(s.agent_id)?.laneId)?.name||s.projectName||'Unassigned'),badge:s.model||s.agent_id});
const teamChoice=t=>({value:t.id,label:t.name,detail:teamPath(t.parentId),badge:members(t.laneId,t.id,true).length+' members',tone:t.role});
const isActive=l=>l&&(!l.lifecycle||l.lifecycle==='active'), teamOf=p=>p.teamId||p.role;
function canMessage(from,to){
 if(from===to||[from,to].some(id=>seat(id)&&!isActive(lane(seat(id).laneId))))return false;
 const rule=data.connections.find(e=>e.from===from&&e.to===to);if(rule)return rule.allow;
 const a=seat(from),b=seat(to),la=lane(a?.laneId),lb=lane(b?.laneId);
 if(data.enabled&&data.defaultCommunication!=='explicit-only'&&la&&lb&&la.projectId===lb.projectId){
  for(const [auditor,home,peer] of [[a,la,b],[b,lb,a]])if(auditor.role==='auditor'&&home.supportMode==='alignment'&&home.companyWorkflowVersion==='company-workflow-1'&&['auditor','worker','writer','researcher','coordinator'].includes(peer.role))return true;
 }
 return data.defaultCommunication==='same-lane'&&!!a&&a.laneId===b?.laneId;
}
async function refresh(force=false){if(busy||dragging||window.SwitchboardCanvasPan?.active||(!force&&document.querySelector('dialog[open]')))return;try{const r=await fetch('/api/workflow',{cache:'no-store'});if(!r.ok)throw Error((await r.json()).error||'Cannot reach the workflow');const next=await r.json();if(dragging||window.SwitchboardCanvasPan?.active)return;const changed=!data||next.revision!==data.revision;data=next;window.AgentSettingsInspector?.sync(data);window.WorkflowSignalLayer?.update(data);if(!changed&&!force){window.WorkflowHistoryEntry?.update(project,viewLane);window.WorkflowDesk?.update(data,project,viewLane);workflowStructure.updateRecords();return;}const scroll=$('session-list').scrollTop;render();$('session-list').scrollTop=scroll;$('last-refresh').textContent='Updated '+new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});$('save-state').textContent='Saved · '+data.revision;error(data.errors.join(' · '));}catch(e){error(e.message+' · Keeping your last displayed layout.');$('save-state').textContent='Offline'}}
async function mutate(operation,item){if(busy)return false;const priorHome=operation==='place'?lane(seat(item.agentId)?.laneId)?.projectId:null;document.querySelectorAll('.dialog-error').forEach(n=>n.remove());busy=true;$('save-state').textContent='Saving…';try{const r=await fetch('/api/workflow',{method:'POST',headers:{'Content-Type':'application/json','X-KE-Board-Token':data.controlToken},body:JSON.stringify({operation,item,revision:data.revision})});const result=await r.json();if(!r.ok)throw Error(result.error||'Save failed');if(operation==='place')rememberTrayHome(item.agentId,item.laneId?null:priorHome||project);busy=false;await refresh(true);return true}catch(e){busy=false;await refresh();error(e.message);const dialog=document.querySelector('dialog[open]');if(dialog){const message=el('p','dialog-error',e.message);message.setAttribute('role','alert');dialog.prepend(message)}$('save-state').textContent='Not saved';return false}}
function card(s,inLane=false){const n=el('div','session');n.tabIndex=0;n.draggable=true;n.dataset.agentId=s.agent_id;n.setAttribute('role','button');n.setAttribute('aria-label','Agent settings for '+s.title);n.title=s.title+'\n'+s.agent_id;const avatar=button('',e=>{e.stopPropagation();openSession(s.agent_id)},'wf-agent-avatar');avatar.setAttribute('aria-label','Open conversation with '+s.title);let hash=0;for(const ch of s.agent_id)hash=(hash*31+ch.charCodeAt(0))>>>0;avatar.style.setProperty('--avatar-hue',hash%360);avatar.textContent=(hash%36).toString(36).toUpperCase()+((hash>>>6)%36).toString(36).toUpperCase();avatar.title='Local identity mark · '+s.agent_id;n.append(avatar);const ports=el('div','wf-agent-ports');for(const [kind,label]of [['inbox','Inbox'],['send','Send'],['results','Results']]){const port=button('',e=>{e.stopPropagation();if(kind==='inbox')window.WorkflowDesk.showInbox(s.agent_id)},'wf-agent-port');port.append(WorkflowStructure.icon(kind));port.dataset.port=kind;port.setAttribute('aria-label',label+' · '+s.title);if(kind!=='inbox'){port.disabled=true;port.title=kind==='send'?'Direct Send is awaiting the trusted session adapter':'Returned artifact view is awaiting exact assignment-result linkage';}else port.title='Received messages for '+s.title;ports.append(port);}n.append(ports);const leadership=data.teamLeads.find(t=>t.agentId===s.agent_id&&t.laneId===seat(s.agent_id)?.laneId),role=leadership&&lane(leadership.laneId)?.id==='project-'+lane(leadership.laneId)?.projectId+'-chief'?'Chief of Staff':leadership&&lane(leadership.laneId)?.id==='project-'+lane(leadership.laneId)?.projectId+'-librarian'?'Library Lead':leadership?((names[leadership.teamId]||data.teams.find(t=>t.id===leadership.teamId)?.name||'Team')+(leadership.teamId==='coordinator'?'':' lead')):null;const title=el('div','session-name',role||s.title);n.append(title);if(role)n.append(el('div','session-task-title',s.title));if(inLane){const focus=button('Focus work →',e=>{e.stopPropagation();openFocus(s.agent_id)},'wf-focus-control');n.append(focus);}const meta=el('div','session-meta');meta.append(el('span','provider '+s.provider,s.provider.toUpperCase()),window.SwitchboardActivity.badge(s));n.append(meta);const config=el('div','agent-config-badge',(s.model||'Model not recorded')+' · '+(s.reasoning||'reasoning unknown'));config.title=s.managed?'Configured for future assignments':'Saved provider metadata; not observed execution';n.append(config);if(!inLane&&seat(s.agent_id))n.append(el('div','placement-label',lane(seat(s.agent_id).laneId)?.name+' · '+names[seat(s.agent_id).role]));let suppressClickUntil=0;n.addEventListener('dragstart',e=>{dragging=true;suppressClickUntil=Infinity;e.dataTransfer.setData('application/x-ke-session',s.agent_id);e.dataTransfer.setData('text/plain',s.agent_id);e.dataTransfer.effectAllowed='move'});n.addEventListener('dragend',()=>{dragging=false;suppressClickUntil=Date.now()+300;document.querySelectorAll('.over').forEach(e=>e.classList.remove('over'))});n.addEventListener('click',()=>{if(Date.now()>suppressClickUntil)window.AgentSettingsInspector.open(s.agent_id)});n.addEventListener('keydown',e=>{if(e.target===n&&(e.key==='Enter'||e.key===' ')){e.preventDefault();window.AgentSettingsInspector.open(s.agent_id)}});return n}
const trayToggle=button('‹',()=>{const collapsed=!document.body.classList.contains('tray-collapsed');try{localStorage.setItem('switchboard.tray.v1.'+project,String(collapsed));}catch{}applyTray(collapsed);fitAutomaticLanes();},'wf-tray-toggle');trayToggle.setAttribute('aria-label','Collapse session tray');document.querySelector('.tray-title').append(trayToggle);dropZone(trayToggle,'','worker');
function applyTray(collapsed){document.body.classList.toggle('tray-collapsed',collapsed);trayToggle.textContent=collapsed?'›':'‹';trayToggle.setAttribute('aria-label',collapsed?'Show sessions or drop to unassign':'Collapse session tray');trayToggle.title=collapsed?'Show sessions · drop an agent here to unassign':'Collapse session tray';}
function renderTray(){if(!data)return;const query=$('search').value.toLowerCase(),all=$('all-projects').checked,p=data.projects.find(p=>p.id===project);const filtered=data.sessions.filter(s=>!seat(s.agent_id)?.laneId&&(all||trayHomes[s.agent_id]===project||s.projectName?.toLowerCase()===p?.name.toLowerCase()||lane(seat(s.agent_id)?.laneId)?.projectId===project||(!s.projectName&&s.latestTask?.objective?.toLowerCase().includes(p?.name.toLowerCase())))&&(!query||(s.title+' '+s.agent_id+' '+s.provider+' '+s.projectName).toLowerCase().includes(query)));$('session-count').textContent=filtered.length;let trayState=null;try{trayState=localStorage.getItem('switchboard.tray.v1.'+project);}catch{}applyTray(trayState===null?!filtered.length:trayState==='true');$('session-list').replaceChildren(...filtered.slice(0,shown).map(s=>card(s)));if(!filtered.length)$('session-list').append(el('p','empty','No unassigned sessions match. Try including other projects.'));$('more').hidden=filtered.length<=shown}
function dropZone(n,laneId,role,teamId=''){n.addEventListener('dragover',e=>{e.stopPropagation();if(e.dataTransfer.types.includes('application/x-ke-session')){e.preventDefault();n.classList.add('over');e.dataTransfer.dropEffect='move'}});n.addEventListener('dragleave',e=>{if(!n.contains(e.relatedTarget))n.classList.remove('over')});n.addEventListener('drop',async e=>{e.preventDefault();e.stopPropagation();n.classList.remove('over');dragging=false;const id=e.dataTransfer.getData('application/x-ke-session');if(!session(id))return;if(await mutate('place',{agentId:id,laneId,role,teamId}))say(laneId?'Placement saved. Live turn left undisturbed.':'Session unassigned.')})}
function coords(l,i){return data.positions.find(p=>p.laneId===l.id)||{x:35+(i%3)*405,y:30+Math.floor(i/3)*545}}
function button(label,action,cls=''){const b=el('button',cls,label);b.type='button';b.onclick=action;return b}
function teamLabel(id){return names[id]||data.teams.find(t=>t.id===id)?.name||id}
function descendants(id){const found=[];function walk(parent){data.teams.filter(t=>t.parentId===parent).forEach(t=>{found.push(t.id);walk(t.id)})}walk(id);return found}
function members(laneId,teamId,deep=false){const ids=new Set([teamId,...(deep?descendants(teamId):[])]);return data.placements.filter(p=>p.laneId===laneId&&ids.has(teamOf(p)))}
function openLane(id){saveCamera();viewLane=id;history.pushState(null,'','?project='+encodeURIComponent(project)+'&lane='+encodeURIComponent(id));render()}
function openFocus(id){const p=seat(id);if(!p||lane(p.laneId)?.projectId!==project)return;saveCamera();selected=id;viewLane=p.laneId;const section=WorkflowStructure.sections.find(s=>s.role===p.role)?.id||'owner';history.pushState(null,'','?'+new URLSearchParams({project,lane:viewLane,section,layer:'work',agent:id}));render();openSession(id)}
function renderLayerNavigation(){const q=new URLSearchParams(location.search);$('workflow-layers').replaceChildren(workflowStructure.layerNavigation(viewLane?lane(viewLane):null,viewLane?q.get('section'):null,viewLane&&q.get('layer')==='work'?q.get('agent'):null))}
window.addEventListener('popstate',()=>{if(!data)return;const q=new URLSearchParams(location.search);saveCamera();project=q.get('project')||project;viewLane=q.get('lane');if(q.get('agent'))selected=q.get('agent');render()});
function teamBlock(l,role,id=role,detail=false){
 const node=el('div','role-slot'+(id!==role?' subteam':''));node.dataset.role=role;node.dataset.teamId=id;node.style.setProperty('--role-color','var(--'+role+')');
 const all=members(l.id,id,true),direct=members(l.id,id),head=el('div','role-label');head.append(el('span','',teamLabel(id)),el('span','team-count',all.length+' member'+(all.length===1?'':'s')));node.append(head);
 const lead=data.teamLeads.find(x=>x.laneId===l.id&&x.teamId===id);if(lead)node.append(el('div','lead-label','Lead · '+(session(lead.agentId)?.title||lead.agentId)));
 if(detail){const tools=el('div','team-actions');tools.append(button('+ Hire',()=>window.Switchboard.hire(l.id,role,id)),button('+ Member',()=>openMember(l.id,role,id)),button('+ Subteam',()=>openTeam(l.id,role,null,id)),button('Edit',()=>openTeam(l.id,role,id)));node.append(tools);const t=data.teams.find(t=>t.id===id);if(t?.objective)node.append(el('p','hint',t.objective));}
 const shown=detail?direct:all;shown.forEach(p=>{const s=session(p.agentId);node.append(s?card(s,true):el('div','session','Unavailable session · '+p.agentId))});
 if(!all.length)node.append(button('＋ Hire an agent or drop a session here',()=>window.Switchboard.hire(l.id,role,id),'drop-label hire-slot'));else if(!detail)node.append(button('+ Hire',()=>window.Switchboard.hire(l.id,role,id),'hire-team'));
 if(false)node.append(button('View all '+all.length+' members →',()=>openLane(l.id),'view-members'));
 if(detail&&role==='researcher'&&!id.startsWith('lib-'))node.append(workflowStructure.librarians(l,id));if(detail)data.teams.filter(t=>t.laneId===l.id&&t.parentId===id&&!t.id.startsWith('lib-')).forEach(t=>node.append(teamBlock(l,role,t.id,true)));
 if(isActive(l))dropZone(node,l.id,role,id===role?'':id);return node;
}
function render(){
 const nextCameraKey=cameraKey();if(framedCameraKey!==nextCameraKey)saveCamera();const camera=framedCameraKey!==nextCameraKey?savedCamera(nextCameraKey):null;
 const prev=project;choices('project',data.projects.map(p=>({value:p.id,label:p.name,detail:p.objective,badge:data.lanes.filter(l=>l.projectId===p.id&&isActive(l)).length+' active lanes'})),prev);project=data.projects.some(p=>p.id===prev)?prev:data.projects[0]?.id;$('project').value=project;$('project-name').textContent=data.projects.find(p=>p.id===project)?.name||'Choose project';
 if(viewLane&&lane(viewLane)?.projectId!==project)viewLane=null;
 rememberWorkflowRoute();window.WorkflowDesk?.update(data,project,viewLane);syncFocusedDeskRoute();
 window.WorkflowHistoryEntry?.update(project,viewLane);$('project-title').textContent=viewLane?lane(viewLane).name:data.projects.find(p=>p.id===project)?.name||'Your project';$('back-project').hidden=!viewLane;$('undo').disabled=data.revision<2;
 $('project-layer').replaceChildren(workflowStructure.projectLayer());renderLayerNavigation();document.body.classList.toggle('viewing-lane',!!viewLane);$('lane-layer').replaceChildren();const lanes=data.lanes.filter(l=>l.projectId===project&&isActive(l)&&(!workflowStructure.isProjectFunction(l)||data.placements.some(p=>p.laneId===l.id))),singleWorkLane=lanes.filter(l=>!workflowStructure.isProjectFunction(l)).length===1;if(framedCameraKey!==nextCameraKey)zoom=camera?.zoom||(singleWorkLane?1:.8);$('canvas-empty').hidden=!!lanes.length||!!viewLane;
 if(!lanes.length&&!viewLane){const closed=data.lanes.some(l=>l.projectId===project&&!workflowStructure.isProjectFunction(l));$('canvas-empty').replaceChildren(el('p','',closed?'All workstreams are closed. Use Manage lanes to reopen one.':'This project has no workstreams yet. Use + Lane to create one.'));}
 if(viewLane)renderDetail(lane(viewLane));else lanes.forEach((l,i)=>{
  const p=coords(l,i),box=el('section','lane'+(singleWorkLane&&l.kind!=='support'?' wf-single-lane':''));box.dataset.laneId=l.id;box.dataset.kind=l.kind||'strategy';box.style.left=p.x+'px';box.style.top=p.y+'px';box.dataset.anchorX=p.x;box.dataset.anchorY=p.y;
  const h=el('div','lane-header');h.tabIndex=0;h.setAttribute('role','button');h.setAttribute('aria-label','Open '+l.name);h.title='Click to open; drag to move this lane';h.append(el('span','handle','⠿'),el('span','lane-index',l.kind==='support'?'SHARED SUPPORT':'LANE '+String(lanes.slice(0,i+1).filter(x=>x.kind!=='support').length).padStart(2,'0')),el('h2','',l.name),el('div','lane-objective',l.objective));box.append(h);
  const settings=button('•••',()=>openLaneEditor(l.id),'lane-settings');settings.setAttribute('aria-label','Settings for '+l.name);box.append(settings);
  if(l.supportMode==='alignment')box.append(el('div','advisory-label','Advisory · the operator confirms corrections'));
  box.append(workflowStructure.overview(l));$('lane-layer').append(box);moveLane(h,box,l.id,p);resizeLane(box,l.id);
 });
 renderTray();renderEdges();$('queue-count').textContent=data.requests.length;if($('connections-dialog').open)renderConnections();if($('inbox-dialog').open)renderInbox();applyZoom();if(framedCameraKey!==nextCameraKey){framedProject=project;framedCameraKey=nextCameraKey;if(camera)moveCamera(camera.x,camera.y);else frameProject();saveCamera();}
 window.dispatchEvent(new CustomEvent('switchboard-workflow-scope',{detail:{camera:{key:framedCameraKey,...cameraPosition()}}}));
}
function frameProject(){
 const view=$('viewport'),nodes=[...document.querySelectorAll('#lane-layer > .lane')];
 if(!nodes.length){moveCamera(0,0);return}
 const x=Math.min(...nodes.map(n=>parseFloat(n.style.left)||0))*zoom,y=Math.min(...nodes.map(n=>parseFloat(n.style.top)||0))*zoom;
 moveCamera(Math.max(0,x-28),Math.max(0,y-24));
}
function rememberWorkflowRoute(){
 if(!project)return;const section=new URLSearchParams(location.search).get('section');
 const prior=new URLSearchParams(location.search),query=new URLSearchParams({project});for(const k of ['history','room','message','q','author','kind'])if(prior.has(k))query.set(k,prior.get(k));if(prior.has('history')&&prior.has('lane'))query.set('lane',prior.get('lane'));if(viewLane){query.set('lane',viewLane);if(workflowSections.has(section))query.set('section',section);if(prior.get('layer')==='work'&&seat(prior.get('agent'))?.laneId===viewLane){query.set('layer','work');query.set('agent',prior.get('agent'))}}
 if(window.WorkflowLibraryView?.active||new URLSearchParams(location.search).get('view')==='library')query.set('view','library');
 // Library scope is separate from the workflow camera/layer and belongs to one project.
 if(prior.get('project')===project)for(const k of ['librarylane','libraryteam','librarypanel'])if(prior.has(k))query.set(k,prior.get(k));
 history.replaceState(null,'','/constellations?'+query);
 try{localStorage.setItem(workflowRouteKey,JSON.stringify({project,lane:viewLane||null,section:viewLane&&workflowSections.has(section)?section:null,layer:query.get('layer'),agent:query.get('agent')}))}catch{}
}
// A focused route selects its exact session once on entry. Ordinary refreshes
// preserve later deliberate panel exploration, tab choice and minimization.
function syncFocusedDeskRoute(){
 const q=new URLSearchParams(location.search),id=q.get('agent');
 const valid=viewLane&&q.get('layer')==='work'&&seat(id)?.laneId===viewLane&&session(id);
 const key=valid?[project,viewLane,q.get('section')||'',id].join('|'):null;
 if(key===focusedDeskRoute)return;focusedDeskRoute=key;
 if(valid){selected=id;window.WorkflowDesk?.select(id);}
}
function renderDetail(l){const q=new URLSearchParams(location.search),id=q.get('agent'),focus=q.get('layer')==='work'&&seat(id)?.laneId===l.id&&session(id);if(focus)selected=id;const box=focus?workflowStructure.focusedWork(l,id):workflowStructure.detail(l);$('lane-layer').append(box);resizeLane(box,l.id,true);}
function moveLane(handle,box,id,p,teamId=null){handle.addEventListener('pointerdown',e=>{if(e.button!==0||e.target.closest('button'))return;e.preventDefault();const x=e.clientX,y=e.clientY,startX=parseFloat(box.style.left),startY=parseFloat(box.style.top);let changed=false;dragging=true;handle.setPointerCapture(e.pointerId);const move=ev=>{if(Math.abs(ev.clientX-x)+Math.abs(ev.clientY-y)<4&&!changed)return;changed=true;box.style.left=Math.max(0,Math.min(12000,startX+(ev.clientX-x)/zoom))+'px';box.style.top=Math.max(0,Math.min(12000,startY+(ev.clientY-y)/zoom))+'px';renderEdges()};const end=async ev=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',end);handle.removeEventListener('pointercancel',end);dragging=false;if(changed)await mutate(teamId?'team-position':'position',{laneId:id,...(teamId?{teamId}:{}),x:parseFloat(box.style.left),y:parseFloat(box.style.top)});else if(!teamId&&ev.type!=='pointercancel')openLane(id)};handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end)});handle.addEventListener('keydown',async e=>{if(!teamId&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openLane(id);return}if(!e.altKey||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;e.preventDefault();await mutate(teamId?'team-position':'position',{laneId:id,...(teamId?{teamId}:{}),x:Math.max(0,parseFloat(box.style.left)+(e.key==='ArrowRight'?20:e.key==='ArrowLeft'?-20:0)),y:Math.max(0,parseFloat(box.style.top)+(e.key==='ArrowDown'?20:e.key==='ArrowUp'?-20:0))})})}
function resizeLane(box,id,detail=false){
 const q=new URLSearchParams(location.search),layerSize=detail?['section','layer','agent'].map(k=>q.get(k)||'').filter(Boolean).join('.'):'';const key='switchboard.lane-size.v1.'+project+'.'+id+(detail?'.detail':'')+(layerSize?'.'+layerSize:'');
 const apply=size=>{if(size){box.style.width=Math.max(detail?600:370,Math.min(2400,size.width))+'px';if(size.height)box.style.height=Math.max(300,Math.min(5000,size.height))+'px';else box.style.removeProperty('height');}box.classList.toggle('wf-sized',!!size?.height);};
 let savedSize=null;try{const saved=JSON.parse(localStorage.getItem(key));if(saved&&Number.isFinite(saved.width)&&(!saved.height||Number.isFinite(saved.height)))savedSize=saved;}catch{}
 if(savedSize)apply(savedSize);else if(box.classList.contains('wf-single-lane')){box.dataset.automaticSize='true';apply({width:automaticLaneWidth()})}
 const grip=button('⌟',()=>{},'wf-resize');grip.setAttribute('aria-label','Resize '+lane(id).name);grip.title='Drag to resize · double-click for automatic height · arrow keys to resize';box.append(grip);
 const save=()=>{delete box.dataset.automaticSize;try{localStorage.setItem(key,JSON.stringify({width:box.offsetWidth,height:box.classList.contains('wf-sized')?box.offsetHeight:null}));say('Lane size saved')}catch{say('Lane resized; size could not be saved')}renderEdges();};
 grip.addEventListener('pointerdown',e=>{if(e.button!==0)return;e.preventDefault();e.stopPropagation();dragging=true;const x=e.clientX,y=e.clientY,w=box.offsetWidth,h=box.offsetHeight;grip.setPointerCapture(e.pointerId);
 const move=ev=>{apply({width:w+(ev.clientX-x)/zoom,height:h+(ev.clientY-y)/zoom});renderEdges()};
 const end=()=>{grip.removeEventListener('pointermove',move);grip.removeEventListener('pointerup',end);grip.removeEventListener('pointercancel',end);dragging=false;save()};grip.addEventListener('pointermove',move);grip.addEventListener('pointerup',end);grip.addEventListener('pointercancel',end);});
 grip.addEventListener('dblclick',e=>{e.stopPropagation();apply({width:box.offsetWidth});save()});
 grip.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;e.preventDefault();e.stopPropagation();apply({width:box.offsetWidth+(e.key==='ArrowRight'?40:e.key==='ArrowLeft'?-40:0),height:box.offsetHeight+(e.key==='ArrowDown'?40:e.key==='ArrowUp'?-40:0)});save()});
}
function clearLaneOverlaps(){
 if(dragging||viewLane)return;const placed=[],boxes=[...document.querySelectorAll('#lane-layer>.lane[data-anchor-y]')].sort((a,b)=>Number(a.dataset.anchorY)-Number(b.dataset.anchorY)||Number(a.dataset.anchorX)-Number(b.dataset.anchorX));
 for(const box of boxes){const x=Number(box.dataset.anchorX),width=box.offsetWidth,height=box.offsetHeight;let y=Number(box.dataset.anchorY);for(let pass=0;pass<=placed.length;pass++){const blockers=placed.filter(prior=>x<prior.x+prior.width+24&&x+width+24>prior.x&&y<prior.y+prior.height+28&&y+height+28>prior.y);if(!blockers.length)break;y=Math.max(...blockers.map(prior=>prior.y+prior.height+28))}box.style.top=y+'px';placed.push({x,y,width,height})}
}
function renderEdges(){clearLaneOverlaps();const svg=$('edges');svg.replaceChildren();const ns='http://www.w3.org/2000/svg';const drawn=new Set();data.connections.filter(e=>!viewLane&&e.allow).forEach(e=>{const a=seat(e.from),b=seat(e.to);if(!a||!b||a.laneId===b.laneId)return;const key=[a.laneId,b.laneId].sort().join('|');if(drawn.has(key))return;drawn.add(key);const aa=document.querySelector('[data-lane-id="'+a.laneId+'"]'),bb=document.querySelector('[data-lane-id="'+b.laneId+'"]');if(!aa||!bb)return;const x1=parseFloat(aa.style.left)+aa.offsetWidth/2,y1=parseFloat(aa.style.top)+45,x2=parseFloat(bb.style.left)+bb.offsetWidth/2,y2=parseFloat(bb.style.top)+45;const path=document.createElementNS(ns,'path');path.setAttribute('class','edge');path.setAttribute('d',`M ${x1} ${y1} C ${x1} ${(y1+y2)/2}, ${x2} ${(y1+y2)/2}, ${x2} ${y2}`);svg.append(path)});const boxes=[...document.querySelectorAll('.lane')];$('world').style.width=Math.max(1320,...boxes.map(b=>parseFloat(b.style.left)+b.offsetWidth+40))+'px';$('world').style.height=Math.max(900,...boxes.map(b=>parseFloat(b.style.top)+b.offsetHeight+50))+'px';window.WorkflowDesk?.redraw();window.WorkflowSignalLayer?.redraw();window.SwitchboardTasks?.redraw()}
// Layout is measured at 100%; camera zoom must never change card geometry.
function automaticLaneWidth(){return Math.max(370,Math.min(1120,$('viewport').clientWidth-70));}
function fitAutomaticLanes(){if(!data||window.WorkflowLibraryView?.active)return;for(const box of document.querySelectorAll('.wf-single-lane[data-automatic-size]'))box.style.width=automaticLaneWidth()+'px';renderEdges()}
function fitCanvas(){
 const view=$('viewport'),nodes=[...document.querySelectorAll('#lane-layer > .lane')];
 if(!nodes.length)return;
 const padding=24;let left,top,right,bottom;
 // A zoom label or responsive panel may reflow the toolbar. Settle that geometry
 // within this Fit action before choosing the final camera position.
 for(let pass=0;pass<4;pass++){
  renderEdges();left=Math.min(...nodes.map(n=>parseFloat(n.style.left)||0));top=Math.min(...nodes.map(n=>parseFloat(n.style.top)||0));
  right=Math.max(...nodes.map(n=>(parseFloat(n.style.left)||0)+n.offsetWidth));bottom=Math.max(...nodes.map(n=>(parseFloat(n.style.top)||0)+n.offsetHeight));
  const fitted=Math.max(.3,Math.floor(100*Math.min(1,(view.clientWidth-padding*2)/Math.max(1,right-left),(view.clientHeight-padding*2)/Math.max(1,bottom-top)))/100);
  if(pass&&Math.abs(fitted-zoom)<.001)break;
  zoom=fitted;applyZoom();
 }
 const insetX=Math.max(0,Math.min(padding,(view.clientWidth-(right-left)*zoom)/2)),insetY=Math.max(0,Math.min(padding,(view.clientHeight-(bottom-top)*zoom)/2));
 moveCamera(left*zoom-insetX,top*zoom-insetY);saveCamera();
 if((right-left)*zoom>view.clientWidth||(bottom-top)*zoom>view.clientHeight)say('Minimum zoom reached. Pan to see the remaining content.');
}

const automaticLaneObserver=new ResizeObserver(()=>fitAutomaticLanes());automaticLaneObserver.observe($('viewport'));window.addEventListener('resize',fitAutomaticLanes);
function applyZoom(){zoom=Math.max(.3,Math.min(1.4,zoom));$('world').style.removeProperty('zoom');$('world').dataset.scale=String(zoom);$('world').style.transform='scale('+zoom+')';fitAutomaticLanes();zoom=Math.max(.3,Math.min(1.4,zoom));const percent=Math.round(zoom*100);$('zoom-label').textContent=percent+'%';$('zoom-slider').value=percent;$('zoom-slider').setAttribute('aria-valuetext',percent+' percent');if(framedCameraKey===cameraKey())saveCamera();window.WorkflowDesk?.redraw();window.WorkflowSignalLayer?.redraw();window.SwitchboardTasks?.redraw()}
let cameraTimer; $('viewport').addEventListener('scroll',()=>{clearTimeout(cameraTimer);cameraTimer=setTimeout(saveCamera,120)},{passive:true});window.addEventListener('pagehide',saveCamera);
$('viewport').addEventListener('wheel',event=>{if(!event.ctrlKey&&!event.metaKey)return;event.preventDefault();const r=$('viewport').getBoundingClientRect(),x=event.clientX-r.left,y=event.clientY-r.top,wx=(cameraPosition().x+x)/zoom,wy=(cameraPosition().y+y)/zoom;zoom=Math.max(.3,Math.min(1.4,zoom*Math.exp(-event.deltaY*.003)));applyZoom();moveCamera(wx*zoom-x,wy*zoom-y);saveCamera()},{passive:false});
function openSession(id){selected=id;if(window.WorkflowDesk){window.WorkflowDesk.select(id);renderLayerNavigation();fitAutomaticLanes()}}
function openConnections(sender=selected){
 const ordered=[...data.sessions].sort((a,b)=>Number(!!seat(b.agent_id))-Number(!!seat(a.agent_id)));
 choices('sender',ordered.map(sessionChoice),sender&&session(sender)?sender:ordered[0]?.agent_id,{searchable:true,list:true});
 choices('default-rule',[{value:'same-lane',label:'Within each lane',detail:'Lane members can communicate unless you block a connection'},{value:'explicit-only',label:'Only my connections',detail:'Every sender needs your explicit permission for each recipient'}],data.defaultCommunication);
 $('recipient-search').value='';renderConnections();$('connections-dialog').showModal();
}
function renderConnections(){
 if(!data)return;
 const from=$('sender').value,q=$('recipient-search').value.toLowerCase();$('default-rule').value=data.defaultCommunication;
 const focused=document.activeElement.closest?.('[data-recipient]'),focusId=focused?.dataset.recipient,focusValue=document.activeElement.dataset.value;
 const ordered=[...data.sessions].sort((a,b)=>Number(!!seat(b.agent_id))-Number(!!seat(a.agent_id)));
 const recipients=ordered.filter(s=>s.agent_id!==from&&(!q||(s.title+' '+s.agent_id).toLowerCase().includes(q)));$('connection-list').replaceChildren();
 recipients.slice(0,100).forEach(s=>{
  const row=el('div','connection-row'),who=el('div','connection-person');who.append(el('strong','',s.title),el('small','',s.provider+' · '+(lane(seat(s.agent_id)?.laneId)?.name||s.projectName||'Unassigned')));
  const yes=canMessage(from,s.agent_id),status=el('span','permission '+(yes?'allow':'block'),yes?'Allowed':'Blocked');
  const control=el('visual-choices');control.setAttribute('aria-label','Permission to '+s.title);control.dataset.recipient=s.agent_id;
  const edge=data.connections.find(e=>e.from===from&&e.to===s.agent_id);
  status.title=edge?'Saved directed '+(edge.allow?'allow':'block'):yes&&seat(from)?.laneId!==seat(s.agent_id)?.laneId?'Project Audit team default route':'Saved workflow default';
  control.setChoices([{value:'default',label:'Default'},{value:'allow',label:'Allow'},{value:'block',label:'Block'}],edge?(edge.allow?'allow':'block'):'default',{compact:true});
  control.onchange=async()=>{const value=control.value;control.disabled=true;if(await mutate('connection',{from,to:s.agent_id,allow:value==='default'?null:value==='allow'}))say('Connection saved')};
  row.append(who,status,control);$('connection-list').append(row);
  if(focusId===s.agent_id)[...control.querySelectorAll('.choice-card')].find(b=>b.dataset.value===focusValue)?.focus({preventScroll:true});
 });
 if(!recipients.length)$('connection-list').append(el('p','empty','No matching recipients.'));
 if(recipients.length>100)$('connection-list').append(el('p','hint','Showing 100 recipients. Search to find another session.'));
}
let inboxSignature=null;
// Insert into the parent's existing constellations.js lexical scope.
function inspectRoutedMessage(message) {
 const placement = data.placements.find(p => p.agentId === message.sender)
   || data.placements.find(p => p.agentId === message.recipient);
 const destination = placement && lane(placement.laneId);
 $('inbox-dialog').close();
 if (destination && !['closed','merged'].includes(destination.lifecycle)) {
  project = destination.projectId;
  viewLane = destination.id;
 } else viewLane = null;
 render();
 window.WorkflowDesk.inspectMessage(message);fitAutomaticLanes();
}

function renderInbox(){const signature=JSON.stringify(data.requests.map(r=>[r.id,r.status,r.body,r.sender,r.recipient,r.read_at,session(r.sender)?.title,session(r.recipient)?.title]));if(signature===inboxSignature)return;inboxSignature=signature;$('requests').replaceChildren();if(!data.requests.length){$('requests').append(el('p','empty','No waiting requests. Your sessions can keep working.'));return}data.requests.forEach(r=>{const box=el('div','request'+(r.status==='HELD'?' held':''));box.tabIndex=0;box.setAttribute('role','button');box.setAttribute('aria-label','Inspect message from '+r.sender+' to '+r.recipient);box.onclick=e=>{if(!e.target.closest('button,a,input'))inspectRoutedMessage(r)};box.onkeydown=e=>{if(e.target===box&&['Enter',' '].includes(e.key)){e.preventDefault();inspectRoutedMessage(r)}};box.append(el('span','',r.status==='HELD'?'HELD · CONNECTION NOT PERMITTED':'QUEUED · NO INTERRUPTION'),el('h3','',(session(r.sender)?.title||r.sender)+' → '+(session(r.recipient)?.title||r.recipient)),el('p','',r.body));if(r.status==='HELD'){const b=el('button','','Review connection');b.onclick=()=>{$('inbox-dialog').close();selected=r.sender;openConnections(r.sender);$('recipient-search').value=session(r.recipient)?.title||r.recipient;renderConnections()};box.append(b)}$('requests').append(box)})}
function renderEditTeams(selectedTeam=''){
 const l=$('edit-lane').value,r=$('edit-role').value;
 choices('edit-team',[{value:'',label:names[r],detail:'Main team',tone:r},...data.teams.filter(t=>t.laneId===l&&t.role===r).map(teamChoice)],selectedTeam);$('edit-team').disabled=!l;
}
function teamPath(id){const t=data.teams.find(t=>t.id===id);return t?teamPath(t.parentId)+' / '+t.name:names[id]||id}
function openMember(laneId,role,teamId){memberTarget={laneId,role,teamId:teamId===role?'':teamId};$('member-title').textContent='Add to '+teamLabel(teamId);$('member-search').value='';choices('member-select',data.sessions.map(sessionChoice),'',{list:true});$('add-member').disabled=true;renderMemberPicker();$('member-dialog').showModal()}
function renderMemberPicker(){$('member-select').filter($('member-search').value);$('add-member').disabled=!$('member-select').value}
$('member-select').onchange=()=>{$('add-member').disabled=!$('member-select').value};
function openLaneEditor(id=null){
 editingLane=id;const l=lane(id);$('lane-dialog-title').textContent=l?'Edit '+l.name:'Add a lane';$('lane-name').value=l?.name||'';$('lane-objective').value=l?.objective||'';choices('lane-kind',[{value:'strategy',label:'Project work',detail:'A lane delivering part of the project'},{value:'support',label:'Shared support',detail:'A team helping other lanes work together'}],l?.kind||'strategy');choices('lane-support',[{value:'general',label:'General support',detail:'Shared research, writing or operations'},{value:'alignment',label:'Alignment & audit',detail:'Audits the lanes; the operator confirms corrections',tone:'auditor'}],l?.supportMode||'general');syncLaneSupport();$('lane-lifecycle').hidden=!l;
 if(l){const n=data.placements.filter(p=>p.laneId===id).length;$('lane-impact').textContent=l.lifecycle==='merged'?'Merged into '+lane(l.mergedInto)?.name+'. Reopening creates an empty source lane; Undo restores the complete merge.':n+' session'+(n===1?'':'s')+' retained. Closing holds their workflow messages and preserves history. Their live work continues.';$('close-lane').hidden=!isActive(l);$('reopen-lane').hidden=isActive(l);$('merge-section').hidden=!isActive(l);const targets=data.lanes.filter(x=>x.projectId===l.projectId&&x.id!==id&&isActive(x));choices('merge-target',targets.map(laneChoice),'',{searchable:targets.length>4});$('merge-lane').disabled=true;}
 $('lane-dialog').showModal();
}
function openManageLanes(){
 $('lanes-list').replaceChildren(...data.lanes.filter(l=>l.projectId===project).map(l=>{const row=el('div','connection-row');row.append(el('strong','connection-person',l.name),el('span','permission',l.lifecycle||'active'),button('Edit',()=>{$('lanes-dialog').close();openLaneEditor(l.id)}));return row}));$('lanes-dialog').showModal();
}
function openTeam(laneId,role,id=null,parentId=role){
 editingTeam={laneId,role,id,parentId};const t=data.teams.find(t=>t.id===id),root=data.roles.includes(id);$('team-dialog-title').textContent=root?names[id]:(t?'Edit '+t.name:'Add a subteam');$('custom-team-fields').hidden=root;$('team-name').value=t?.name||'';$('team-objective').value=t?.objective||'';
 const excluded=new Set([id,...(id?descendants(id):[])]);choices('team-parent',[{value:role,label:names[role],detail:'Main team',tone:role},...data.teams.filter(x=>x.laneId===laneId&&x.role===role&&!excluded.has(x.id)).map(teamChoice)],t?.parentId||parentId);
 choices('team-lead',[{value:'',label:'No lead assigned',detail:'Members retain their individual responsibilities'},...(id?members(laneId,id):[]).map(p=>session(p.agentId)?sessionChoice(session(p.agentId)):{value:p.agentId,label:p.agentId})],data.teamLeads.find(x=>x.laneId===laneId&&x.teamId===id)?.agentId||'',{searchable:true});$('remove-team').hidden=!t;$('team-dialog').showModal();
}
$('back-project').onclick=()=>{viewLane=null;history.replaceState(null,'','?project='+encodeURIComponent(project));render()};
function syncLaneSupport(){$('lane-support').disabled=$('lane-kind').value!=='support';if($('lane-support').disabled)$('lane-support').value='general'}
$('lane-kind').onchange=syncLaneSupport;
$('merge-target').onchange=()=>{$('merge-lane').disabled=!$('merge-target').value};
$('add-lane').onclick=()=>openLaneEditor();$('manage-lanes').onclick=openManageLanes;
$('save-lane').onclick=async()=>{const item={name:$('lane-name').value,objective:$('lane-objective').value,kind:$('lane-kind').value,supportMode:$('lane-support').value};if(editingLane)item.laneId=editingLane;else item.projectId=project;if(await mutate(editingLane?'lane-edit':'lane-create',item)){$('lane-dialog').close();say('Lane saved')}};
$('close-lane').onclick=async()=>{if(await mutate('lane-close',{laneId:editingLane})){$('lane-dialog').close();if(viewLane===editingLane)viewLane=null;render();say('Lane closed. History and live work preserved.')}};
$('reopen-lane').onclick=async()=>{if(await mutate('lane-reopen',{laneId:editingLane})){$('lane-dialog').close();say('Lane reopened')}};
$('merge-lane').onclick=async()=>{const target=$('merge-target').value;if(await mutate('lane-merge',{laneId:editingLane,targetId:target})){$('lane-dialog').close();openLane(target);say('Lane merged. Source teams and history preserved.')}};
$('save-team').onclick=async()=>{const t=editingTeam,root=data.roles.includes(t.id);let ok;if(root)ok=await mutate('team-lead',{laneId:t.laneId,teamId:t.id,agentId:$('team-lead').value});else ok=await mutate('team',{id:t.id||undefined,laneId:t.laneId,role:t.role,parentId:$('team-parent').value,name:$('team-name').value,objective:$('team-objective').value,leadAgentId:$('team-lead').value});if(ok){$('team-dialog').close();say('Team saved')}};
$('remove-team').onclick=async()=>{if(await mutate('team-remove',{teamId:editingTeam.id})){$('team-dialog').close();say('Members and subteams moved to the parent')}};
$('member-search').oninput=renderMemberPicker;$('add-member').onclick=async()=>{if(await mutate('place',{...memberTarget,agentId:$('member-select').value})){$('member-dialog').close();say('Member assigned. Live work preserved.')}};
$('edit-lane').onchange=()=>renderEditTeams();$('edit-role').onchange=()=>renderEditTeams();
window.WorkflowSignalLayer=WorkflowSignals.create({project:()=>project,openMessage:m=>window.WorkflowDesk.inspectMessage(m)});
window.WorkflowDesk=WorkflowCommunications.create({data:()=>data,project:()=>project,lane:()=>viewLane,openTranscript:openSession,openConnections,onMessages:rows=>window.WorkflowSignalLayer.updateMessages(rows)});
window.SwitchboardTasks=TaskBoard.create({revealTask:t=>{const home=seat(t.station)?.laneId||t.laneId;if(home&&viewLane!==home)openLane(home);},data:()=>data,project:()=>project,lane:()=>viewLane,showTask:(...args)=>window.WorkflowDesk.showTask(...args),setTaskTitle:(...args)=>window.WorkflowDesk.setTaskTitle(...args),onSnapshot:s=>{if(data){data.tasks=s.tasks;workflowStructure.updateRecords();}}});
$('project-button').onclick=()=>{$('project-dialog').showModal()};
$('project').onchange=()=>{$('project-dialog').close();project=$('project').value;viewLane=null;history.replaceState(null,'','?project='+encodeURIComponent(project));shown=60;render()};$('search').oninput=()=>{shown=60;renderTray()};$('all-projects').onchange=renderTray;$('more').onclick=()=>{shown+=60;renderTray()};$('connections').onclick=()=>openConnections();$('sender').onchange=renderConnections;$('recipient-search').oninput=renderConnections;$('default-rule').onchange=()=>mutate('default',{value:$('default-rule').value});$('undo').onclick=async()=>{if(await mutate('undo',{}))say('Previous edit restored')};$('zoom-slider').addEventListener('input',()=>{zoom=Number($('zoom-slider').value)/100;applyZoom()});$('zoom-in').onclick=()=>{zoom=Math.min(1.4,zoom+.1);applyZoom()};$('zoom-out').onclick=()=>{zoom=Math.max(.3,zoom-.1);applyZoom()};$('fit').onclick=fitCanvas;$('place-session').onclick=async()=>{if(await mutate('place',{agentId:selected,laneId:$('edit-lane').value,role:$('edit-role').value,teamId:$('edit-team').value})){$('session-dialog').close();say('Placement saved. Live turn left undisturbed.')}};$('remove-session').onclick=async()=>{if(await mutate('place',{agentId:selected,laneId:'',role:'worker'})){$('session-dialog').close();say('Session unassigned')}};$('save-note').onclick=async()=>{if(await mutate('note',{agentId:selected,text:$('assignment-note').value}))say('Direction saved for the next safe boundary')};$('session-connections').onclick=()=>{$('session-dialog').close();openConnections(selected)};$('inbox-button').onclick=()=>{renderInbox();$('inbox-dialog').showModal()};document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());dropZone($('unassign'),'','worker');document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh(true)});setInterval(()=>{if(!document.hidden&&!document.querySelector('dialog[open]'))refresh()},5000);refresh(true);

$('adopt-company-workflow').onclick=async()=>{if(await mutate('company-workflow',{projectId:project})){$('project-dialog').close();say('Company workflow ready for staffing. Existing tasks and source owners are preserved.')}};
$('new-company-project').onclick=()=>{
 const d=el('dialog','tf-dialog'),form=el('form'),heading=el('h2','','New project or business'),name=el('input'),objective=el('textarea'),save=el('button','','Create with company workflow'),close=el('button','','Cancel'),status=el('p','dialog-error');
 name.required=true;name.maxLength=100;name.setAttribute('aria-label','Project name');name.placeholder='Project name';objective.required=true;objective.maxLength=2000;objective.setAttribute('aria-label','Project purpose');objective.placeholder='What this project will deliver';save.type='submit';close.type='button';close.onclick=()=>d.close();form.append(name,objective,save,close,status);d.append(heading,form);document.body.append(d);d.onclose=()=>d.remove();d.showModal();const id='business-'+crypto.randomUUID().slice(0,12);
 form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{const current=await fetch('/api/workspace',{cache:'no-store'}).then(r=>r.json());const response=await fetch('/api/workspace',{method:'POST',headers:{'Content-Type':'application/json','X-KE-Board-Token':data.controlToken},body:JSON.stringify({operation:'project',revision:current.revision,item:{id,name:name.value,objective:objective.value}})});const result=await response.json();if(!response.ok)throw Error(result.error||'Project setup failed');project=id;viewLane=null;history.replaceState(null,'','?project='+encodeURIComponent(project));await refresh(true);d.close();$('project-dialog').close();say('Project created with Task Board, workers, Research, Library and Audit structure. Staff seats to enable work.')}catch(err){status.textContent=err.message+' Your entries are retained; retry completes the same project.'}finally{save.disabled=false}};
};
