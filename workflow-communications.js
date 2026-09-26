'use strict';
window.WorkflowCommunications = {create(ctx) {
 const make = (tag, cls = '', text) => {const n = document.createElement(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n;};
 const button = (text, action, cls = '') => {const n = make('button', cls, text); n.type = 'button'; n.onclick = action; return n;};
 const root = make('aside', 'wf-desk'); root.setAttribute('aria-label', 'Agent session and communications');
 const head = make('div', 'wf-desk-head'), title = make('strong', '', 'Lane communications');
 const minimize=button('−',()=>setCollapsed(true),'wf-desk-minimize');minimize.setAttribute('aria-label','Minimize agent panel');head.append(title,minimize);
 const tabs = make('div', 'wf-desk-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Lane desk');
 const sessionTab = button('Session', () => switchTab('session')), messagesTab = button('Messages', () => switchTab('messages')),historyTab=button('Work & history',()=>showHistory()),taskTab=button('Task',()=>switchTab('task'));
 for (const n of [sessionTab, messagesTab,historyTab,taskTab]) n.setAttribute('role', 'tab');
 const permissions = button('Connections', () => ctx.openConnections(agent)); tabs.append(sessionTab, messagesTab,historyTab,taskTab, permissions);
 const filter = make('input', 'wf-desk-search'); filter.type = 'search'; filter.placeholder = 'Search loaded messages'; filter.setAttribute('aria-label', 'Search loaded lane messages');
 const content = make('div', 'wf-desk-content'), messagePane = make('div'), sessionPane = make('div'); sessionPane.style.height = '100%';
 const notice = make('p', 'reader-error'), route = make('section', 'wf-route'), list = make('div', 'wf-message-list'); notice.setAttribute('role', 'status'); notice.hidden = true; route.hidden = true; route.style.cssText = 'position:sticky;top:0;z-index:2';
 const taskPane=make('div','tf-task-pane');let taskTitle='Task',taskId=null;const historyPane=make('div','wf-history-pane');let historyUI=null,historyScope=null;messagePane.append(notice,route,list);content.append(messagePane,sessionPane,historyPane,taskPane); const assignment=make('details','wf-desk-assignment'),assignmentTitle=make('summary'),assignmentBody=make('div');assignment.append(assignmentTitle,assignmentBody);root.append(head,assignment,tabs,filter,content);
 const workspace=ctx.mount||document.querySelector('.workflow-workspace'),main=!ctx.mount&&workspace.closest('.main');let dock=workspace;if(main){dock=main.querySelector(':scope > .wf-main-layout');if(!dock){dock=make('div','wf-main-layout');const canvas=make('div','wf-main-canvas');for(const child of [...main.children])if(child.tagName!=='FOOTER')canvas.append(child);dock.append(canvas);main.prepend(dock);}}const restorePanel=button('‹',()=>setCollapsed(false),'wf-desk-restore');restorePanel.setAttribute('aria-label','Restore agent panel');restorePanel.hidden=true;dock.append(root,restorePanel);const resize=make('div','wf-desk-resize');resize.tabIndex=0;resize.setAttribute('role','separator');resize.setAttribute('aria-label','Resize agent panel');resize.setAttribute('aria-orientation','vertical');root.prepend(resize);
 const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'); svg.classList.add('wf-message-arrows'); svg.setAttribute('aria-hidden', 'true'); document.body.append(svg);
 let state = null, project = null, lane = null, agent = null, tab = 'messages', chosen = null, cursor = null, revision = null;
 let collapsed=false,panelWidth=440,viewStorageKey=null,restoring=false,restoreMessage=null,restoreMessagePages=0;
 function viewKey(p,l){return 'switchboard.desk.v2.'+p+'.'+(l||'overview');}
 function saveView(){if(restoring||restoreMessage||!viewStorageKey)return;try{localStorage.setItem(viewStorageKey,JSON.stringify({agent,tab,collapsed,width:panelWidth,messageId:chosen?.id||null,recipientFilter,historyScope,search:filter.value,scroll:content.scrollTop,expanded:[...expanded].slice(-100)}));}catch{}}
 function readView(key){try{return JSON.parse(localStorage.getItem(key))||{};}catch{return {};}}
 function applyWidth(value){panelWidth=Math.max(280,Math.min(800,Number(value)||440));root.style.width=panelWidth+'px';resize.setAttribute('aria-valuenow',Math.round(panelWidth));redraw();}
 function setCollapsed(value){collapsed=value;root.hidden=value;restorePanel.hidden=!value;restorePanel.title='Restore '+name(agent);saveView();window.dispatchEvent(new Event('resize'));}
 resize.addEventListener('pointerdown',e=>{if(e.button!==0)return;e.preventDefault();e.stopPropagation();const x=e.clientX,width=root.getBoundingClientRect().width;resize.setPointerCapture(e.pointerId);const move=ev=>{applyWidth(width+x-ev.clientX);};const end=()=>{resize.removeEventListener('pointermove',move);resize.removeEventListener('pointerup',end);resize.removeEventListener('pointercancel',end);saveView();};resize.addEventListener('pointermove',move);resize.addEventListener('pointerup',end);resize.addEventListener('pointercancel',end);});
 resize.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();applyWidth(panelWidth+(e.key==='ArrowLeft'?20:-20));saveView();});
 let overviewAgent = null, reportedLane = null, messageAgent = null,recipientFilter=null;
 let attachmentDialog = null;
 function previewAttachment(event){
  const frame=sessionPane.querySelector('iframe');const d=event.data;
  if(disposed||event.origin!==location.origin||event.source!==frame?.contentWindow||d?.type!=='switchboard:attachment-preview'||d.agent!==agent||typeof d.attachment?.url!=='string')return;
  const url=new URL(d.attachment.url,location.origin);if(url.origin!==location.origin||url.pathname!=='/api/transcript/attachment'||url.searchParams.get('agent')!==agent||!url.searchParams.get('token'))return;
  attachmentDialog?.close();const dialog=make('dialog','wf-attachment-dialog'),header=make('div','wf-attachment-head'),img=make('img');img.src=url.href;img.alt=String(d.attachment.name||'Message attachment');header.append(make('strong','',img.alt),button('Close',()=>dialog.close()));dialog.append(header,img);document.body.append(dialog);attachmentDialog=dialog;dialog.onclose=()=>{dialog.remove();if(attachmentDialog===dialog)attachmentDialog=null;};dialog.showModal();
 }
 window.addEventListener('message',previewAttachment);
 let generation = 0, disposed = false, olderLoaded = false, frameAgent = undefined, routeKey = '', drawFrame = null;
 const records=new Map(),nodes=new Map(),expanded=new Set(),requests=new Map(),scroll={messages:0,session:0,history:0,task:0};
 const person = id => state?.sessions?.find(s => s.agent_id === id), name = id => person(id)?.title || id || 'Lane communications';
 const more = button('Earlier messages', () => loadMessages(true));
 function clearDrawing() {svg.replaceChildren();delete svg.dataset.sender;delete svg.dataset.recipient;delete svg.dataset.messageId;document.querySelectorAll('.message-sender,.message-recipient').forEach(n => n.classList.remove('message-sender','message-recipient'));}
 function draw() {
  drawFrame=null;clearDrawing();if(disposed||!chosen||root.hidden||!root.isConnected||tab!=='messages')return;
  const geometry=window.WorkflowRouteGeometry;if(!geometry)return;
  const from=geometry.endpoint(chosen.sender,'send',{label:name(chosen.sender)}),to=geometry.endpoint(chosen.recipient,'inbox',{label:name(chosen.recipient)});if(!from||!to)return;
  let planned=geometry.route?geometry.route(from,to):{available:true,d:geometry.curve(from,to)};const rawFootprint=document.querySelector('.wf-couriers')?.dataset.footprint;if(planned.roads&&rawFootprint){const bodyPlan=window.WorkflowRoads.courierRoute(from,to,JSON.parse(rawFootprint));if(bodyPlan&&bodyPlan.available)planned=bodyPlan;}let notice=route.querySelector('.wf-route-unavailable');
  if(!planned.available){if(!notice){notice=make('p','wf-route-unavailable');route.append(notice);}notice.textContent='Route unavailable · '+planned.reason;return;}notice?.remove();
  const path=document.createElementNS(ns,'path');path.setAttribute('d',planned.d);path.setAttribute('class','wf-message-route');path.setAttribute('fill','none');path.setAttribute('stroke','#edd49e');path.setAttribute('stroke-width','2');svg.append(path);
  for(const p of [from,to]){const dot=document.createElementNS(ns,'circle');for(const [k,v]of Object.entries({cx:p.x,cy:p.y,r:6,fill:'#162333',stroke:'#edd49e'}))dot.setAttribute(k,v);svg.append(dot);if(p.offscreen){const label=document.createElementNS(ns,'text');label.setAttribute('x',Math.max(15,Math.min(innerWidth-220,p.x)));label.setAttribute('y',p.y-12);label.setAttribute('fill','#edd49e');label.setAttribute('font-size','12');label.textContent='Offscreen · '+p.label;svg.append(label);}}
  svg.dataset.sender=chosen.sender;svg.dataset.recipient=chosen.recipient;svg.dataset.messageId=chosen.id;
 }
 function redraw() {if (!disposed && drawFrame === null) drawFrame=requestAnimationFrame(draw);}
 function renderRoute() {
  const key=chosen?JSON.stringify([chosen.id,chosen.sender,chosen.recipient,chosen.status,name(chosen.sender),name(chosen.recipient)]):'';
  if (key===routeKey) return;routeKey=key;route.replaceChildren();route.hidden=!chosen;if (!chosen) return;
  route.dataset.messageId=chosen.id;route.append(make('span','wf-small-label','MESSAGE ROUTE'));
  for (const [id,role] of [[chosen.sender,'from'],[chosen.recipient,'to']]) {
   if (role==='to') route.append(make('span','wf-route-direction','↓ '+chosen.status));
   const n=button(name(id),()=>select(id),'wf-route-person '+role);n.dataset.routeAgent=id;n.dataset.routeRole=role;n.title=id;route.append(n);
  }
  route.append(button('Replay recorded exchange',()=>{if(!window.WorkflowSignalLayer?.replayMessage(chosen)){notice.textContent='No recorded permitted delivery available for replay.';notice.hidden=false;}},'wf-text-button'));
  route.append(button('Clear selection',()=>{chosen=null;renderMessages();redraw();},'wf-text-button'));
 }
 function anchor() {const top=content.getBoundingClientRect().top;const row=[...list.querySelectorAll('.wf-message')].find(n=>!n.hidden&&n.getBoundingClientRect().bottom>top);return {top:content.scrollTop,row,offset:row?.getBoundingClientRect().top};}
 function restore(saved) {content.scrollTop=saved.top;if (saved.row?.isConnected&&!saved.row.hidden) content.scrollTop+=saved.row.getBoundingClientRect().top-saved.offset;}
 function renderMessages() {
  const saved=anchor();renderRoute();const ordered=[...records.values()].sort((a,z)=>a.created_at===z.created_at?(a.id<z.id?1:a.id>z.id?-1:0):(a.created_at<z.created_at?1:-1));
  const query=filter.value.toLowerCase();let position=list.firstElementChild,shown=0;
  for (const m of ordered) {
   let entry=nodes.get(m.id);
   if (!entry) {
    const row=make('article','wf-message'),pick=button('',()=>highlight(records.get(m.id)),'wf-message-pick'),meta=make('div','wf-message-meta'),status=make('span'),time=make('time'),sender=make('strong'),recipient=make('span','wf-message-to'),details=make('details'),summary=make('summary','','Message content'),body=make('p');
    pick.style.cssText='display:block;width:100%;text-align:left;white-space:normal;background:transparent;border:0;padding:0;color:inherit';
    body.style.display='block';body.style.overflow='visible';meta.append(status,time);pick.append(meta,sender,recipient);details.append(summary,body);row.append(pick,details);row.dataset.messageId=m.id;
    details.addEventListener('toggle',()=>{if(details.open)expanded.add(m.id);else expanded.delete(m.id);redraw();});
    row.addEventListener('click',event=>{if(!event.target.closest('button,a,input'))highlight(records.get(m.id));});
    entry={row,pick,status,time,sender,recipient,details,body,key:''};nodes.set(m.id,entry);
   }
   const key=JSON.stringify([m,name(m.sender),name(m.recipient)]);
   if(entry.key!==key){entry.status.textContent=m.status;entry.time.textContent=new Date(m.created_at).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});entry.sender.textContent=name(m.sender);entry.recipient.textContent='→ '+name(m.recipient);entry.body.textContent=m.body;entry.pick.setAttribute('aria-label',`Inspect message from ${name(m.sender)} to ${name(m.recipient)}`);entry.key=key;}
   entry.row.classList.toggle('selected',chosen?.id===m.id);entry.pick.setAttribute('aria-pressed',String(chosen?.id===m.id));entry.details.open=expanded.has(m.id);
   entry.row.hidden=!!(recipientFilter&&m.recipient!==recipientFilter)||!(m.id===chosen?.id||(m.body+' '+name(m.sender)+' '+name(m.recipient)).toLowerCase().includes(query));if(!entry.row.hidden)shown++;
   if(entry.row!==position)list.insertBefore(entry.row,position);position=entry.row.nextElementSibling;
  }
  for(const [id,entry] of nodes)if(!records.has(id)){entry.row.remove();nodes.delete(id);expanded.delete(id);}
  let empty=list.querySelector('.wf-desk-empty');if(!shown){if(!empty){empty=make('p','wf-desk-empty');list.append(empty);}empty.textContent=records.size?'No matching messages.':'No routed messages for this lane.';}else empty?.remove();
  if(cursor){list.append(more);more.disabled=requests.has('older');}else more.remove();restore(saved);redraw();
 }
 async function loadMessages(older=false) {
  if(disposed||!state||(!lane&&!messageAgent)||older&&!cursor)return;
  const kind=older?'older':'head';if(requests.has(kind))return requests.get(kind);
  const scope=generation,requestLane=lane,requestAgent=messageAgent,before=older?cursor:null;
  const run=(async()=>{try{
   const url='/api/messages?'+(requestLane?'lane='+encodeURIComponent(requestLane):'agent='+encodeURIComponent(requestAgent))+(before?'&before='+encodeURIComponent(before):'');
   const response=await fetch(url,{cache:'no-store'}),data=await response.json();if(!response.ok)throw Error(data.error||'Messages unavailable');
   if(disposed||scope!==generation)return;
   if(!Array.isArray(data.items))throw Error('Invalid message response');
   // Carry the reader's permission revision through the existing one-argument
   // callback; reuse its reconciled receipts in the panel as well as the courier.
   const rows=data.items.map(m=>({...m,workflowRevision:data.workflowRevision})),reconciled=ctx.onMessages?.(rows);
   for(const incoming of Array.isArray(reconciled)?reconciled:rows){const m=window.WorkflowReceiptState?.merge(records.get(incoming?.id),incoming)||incoming;if(!m?.id||!m.sender||!m.recipient)continue;records.set(m.id,m);if(chosen?.id===m.id)chosen=m;}
   if(older){olderLoaded=true;cursor=data.nextBefore;}else if(!olderLoaded)cursor=data.nextBefore;
   notice.hidden=true;
   if(restoreMessage&&records.has(restoreMessage.id)){chosen=records.get(restoreMessage.id);expanded.add(chosen.id);renderMessages();content.scrollTop=restoreMessage.scroll;restoreMessage=null;redraw();}
   else renderMessages();
  }catch(error){if(!disposed&&scope===generation){notice.textContent=error.message+' · Keeping the displayed messages.';notice.hidden=false;}}
  finally{if(scope===generation){requests.delete(kind);more.disabled=false;if(restoreMessage){if(cursor&&restoreMessagePages++<20)setTimeout(()=>loadMessages(true),0);else{notice.textContent='The saved message is outside the available loaded history.';notice.hidden=false;restoreMessage=null;}}}}})();
  requests.set(kind,run);more.disabled=requests.has('older');return run;
 }
 function switchTab(value) {
  assignment.hidden=value==='history'||value==='task';content.classList.toggle('wf-reading-session',value==='session'||value==='history');
  if(tab!==value)scroll[tab]=content.scrollTop;const changed=tab!==value;tab=value;
  sessionTab.setAttribute('aria-selected',String(value==='session'));messagesTab.setAttribute('aria-selected',String(value==='messages'));historyTab.setAttribute('aria-selected',String(value==='history'));filter.hidden=value!=='messages';messagePane.hidden=value!=='messages';sessionPane.hidden=value!=='session';historyPane.hidden=value!=='history';taskPane.hidden=value!=='task';taskTab.setAttribute('aria-selected',String(value==='task'));if(value==='task')title.textContent=taskTitle;
  if(value==='session'){
   if(frameAgent!==agent){frameAgent=agent;sessionPane.replaceChildren();if(agent){const frame=make('iframe','wf-session-frame');frame.title='Session transcript: '+name(agent);frame.src='/session?agent='+encodeURIComponent(agent)+'&embedded=1';sessionPane.append(frame);}else sessionPane.append(make('p','wf-desk-empty','Choose an agent in this lane.'));}
   clearDrawing();
  }else if(value==='messages'){renderMessages();redraw();}else clearDrawing();
  if(changed)content.scrollTop=scroll[value];saveView();
 }
 function showHistory(scope=null,mode=null){const p=scope?.project??project,l=scope?.lane??reportedLane??'';historyScope={project:p,lane:l};if(!historyUI)historyUI=SwitchboardHistory.create({mount:historyPane,project:p,lane:l,onAgent:select,onScope:(hp,hl)=>{historyScope={project:hp,lane:hl};saveView();}});else historyUI.updateScope(p,l);collapsed=false;root.hidden=false;restorePanel.hidden=true;title.textContent='Work & history';switchTab('history');if(mode)historyUI.open(mode);saveView();return historyUI.ready;}
 function renderAssignment(){const s=person(agent),t=s?.latestTask,a=s?.activity;assignmentTitle.textContent=t?.objective?'Assignment · '+t.objective:'Assignment not recorded';assignmentBody.replaceChildren();if(t){assignmentBody.append(make('p','',t.objective),make('span','wf-small-label',t.status||'State unavailable'));if(t.next_action)assignmentBody.append(make('p','','Next: '+t.next_action));if(t.blocker)assignmentBody.append(make('p','','Blocker: '+t.blocker));assignmentBody.append(make('small','',t.task_id||''));}if(a){const elapsed=Number.isFinite(a.durationSeconds)?Math.floor(a.durationSeconds/60)+'m '+a.durationSeconds%60+'s':null;assignmentBody.append(make('p','',a.lastAction||a.detail||'Activity detail unavailable'));assignmentBody.append(make('small','',[(a.turnStatus==='open'?'Current turn':'Recorded turn')+(elapsed?' · '+elapsed:' · elapsed unavailable'),a.source,a.observedAt?'Observed '+new Date(a.observedAt).toLocaleTimeString():'Time unavailable'].filter(Boolean).join(' · ')));}assignment.title=t?.objective||'No linked durable assignment was returned by the source';}
 function showInbox(id){select(id);recipientFilter=id;chosen=null;switchTab('messages');loadMessages();}
 function select(id) {recipientFilter=null;if(id!==agent)attachmentDialog?.close();state=ctx.data?.()||state;if(!person(id))return false;const view=ctx.lane?ctx.lane():reportedLane;if(!view){overviewAgent=id;update(state,ctx.project?.()||project,null);}agent=id;title.textContent=name(id);renderAssignment();collapsed=false;restorePanel.hidden=true;root.hidden=false;switchTab('session');saveView();return true;}
 function highlight(message) {
  if(!message||typeof message.id!=='string'||typeof message.sender!=='string'||typeof message.recipient!=='string')return false;
  state=ctx.data?.()||state;restoreMessage=null;chosen={...message};records.set(message.id,chosen);expanded.add(message.id);collapsed=false;restorePanel.hidden=true;root.hidden=false;switchTab('messages');renderMessages();redraw();saveView();return true;
 }
 function inspectMessage(message){state=ctx.data?.()||state;const p=ctx.project?.()||project;overviewAgent=[message.sender,message.recipient].find(id=>{const seat=state.placements.find(x=>x.agentId===id);return state.lanes.find(l=>l.id===seat?.laneId)?.projectId===p;})||agent;update(state,p,ctx.lane?ctx.lane():reportedLane);return highlight(message);}
 function update(next,p,l) {
  const storageKey=viewKey(p,l),restore=storageKey!==viewStorageKey;if(restore){saveView();viewStorageKey=storageKey;restoring=true;}const saved=restore?readView(storageKey):null;
  state=next;if(restore&&saved.agent){const placed=next.placements.find(x=>x.agentId===saved.agent),parent=next.lanes.find(x=>x.id===placed?.laneId);if(!person(saved.agent)||(placed&&(l?placed.laneId!==l:parent?.projectId!==p)))saved.agent=null;}if(project!==p||reportedLane!==l)overviewAgent=null;project=p;reportedLane=l;if(restore&&saved.agent&&!l)overviewAgent=saved.agent;
  const seat=next.placements.find(x=>x.agentId===overviewAgent),seatLane=next.lanes?.find(x=>x.id===seat?.laneId);
  if(seatLane&&seatLane.projectId!==p)overviewAgent=null;
  const effectiveLane=l||(overviewAgent?seat?.laneId:null)||null,effectiveAgent=!effectiveLane?overviewAgent:null;
  const changed=lane!==effectiveLane||messageAgent!==effectiveAgent;l=effectiveLane;messageAgent=effectiveAgent;
  if(changed){generation++;requests.clear();lane=l;records.clear();nodes.clear();list.replaceChildren();chosen=null;restoreMessage=null;expanded.clear();cursor=null;olderLoaded=false;routeKey='';route.replaceChildren();route.hidden=true;filter.value='';notice.hidden=true;scroll.messages=0;scroll.session=0;
   const placements=next.placements.filter(x=>x.laneId===l);agent=overviewAgent||(placements.find(x=>x.role==='coordinator')||placements[0])?.agentId||null;title.textContent=name(agent);root.hidden=!l&&!overviewAgent;switchTab('messages');content.scrollTop=0;if(l||messageAgent)loadMessages();else clearDrawing();
  }else{
   title.textContent=tab==='task'?taskTitle:name(agent);if(chosen)renderRoute();if((l||messageAgent)&&revision!==next.revision)loadMessages();redraw();
  }
  if(!l&&!chosen&&!overviewAgent&&!['history','task'].includes(tab))root.hidden=true;if(tab==='history')title.textContent='Work & history';revision=next.revision;
  if(restore){applyWidth(saved.width);if(saved.tab==='history'){showHistory(saved.historyScope||{project:p,lane:reportedLane||''});collapsed=!!saved.collapsed;}else if(saved.agent&&person(saved.agent)){agent=saved.agent;overviewAgent=!reportedLane?agent:null;title.textContent=name(agent);root.hidden=false;recipientFilter=saved.recipientFilter===agent?agent:null;filter.value=String(saved.search||'');for(const id of saved.expanded||[])expanded.add(id);if(saved.messageId){restoreMessage={id:saved.messageId,scroll:Number(saved.scroll)||0};restoreMessagePages=0;}switchTab(['session','messages'].includes(saved.tab)?saved.tab:'session');content.scrollTop=Number(saved.scroll)||0;collapsed=!!saved.collapsed;}else collapsed=false;restoring=false;}
  renderAssignment();if(collapsed){root.hidden=true;restorePanel.hidden=false;restorePanel.title='Restore '+name(agent);}else restorePanel.hidden=true;
 }
 const viewport=document.getElementById('viewport');filter.oninput=()=>{renderMessages();saveView();};window.addEventListener('resize',redraw);viewport?.addEventListener('scroll',redraw);content.addEventListener('scroll',redraw);content.addEventListener('scroll',saveView,{passive:true});window.addEventListener('pagehide',saveView);
 const observer=window.ResizeObserver?new ResizeObserver(redraw):null;observer?.observe(root);if(viewport)observer?.observe(viewport);
 const stopGeometry=window.WorkflowRouteGeometry?.watch(redraw);
 const timer=setInterval(()=>{if((lane||messageAgent)&&!document.hidden)loadMessages();},5000);
 function destroy(){stopGeometry?.();historyUI?.destroy();attachmentDialog?.close();window.removeEventListener('message',previewAttachment);disposed=true;generation++;clearInterval(timer);if(drawFrame!==null)cancelAnimationFrame(drawFrame);observer?.disconnect();window.removeEventListener('resize',redraw);viewport?.removeEventListener('scroll',redraw);content.removeEventListener('scroll',redraw);clearDrawing();window.removeEventListener('pagehide',saveView);content.removeEventListener('scroll',saveView);root.remove();restorePanel.remove();svg.remove();}
 function showTask(node,label,id){taskId=id;taskTitle=label;taskPane.replaceChildren(node);collapsed=false;root.hidden=false;restorePanel.hidden=true;switchTab('task');}
 function setTaskTitle(label,id){if(taskId===id){taskTitle=label;if(tab==='task')title.textContent=label;}}
 switchTab('messages');root.hidden=true;return {update,select,showInbox,showHistory,highlight,inspectMessage,showTask,setTaskTitle,redraw,refresh:loadMessages,destroy};
}};
