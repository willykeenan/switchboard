"use strict";
let project=new URLSearchParams(location.search).get('project')||'', lane=new URLSearchParams(location.search).get('lane')||'';
let workspaceData=null, workspaceStamp='', workspaceChecked=0;
const stateLabel=s=>({UNLINKED:'No linked work',BLOCKED:'Blocked',WORKING:'Reported working',QUEUED:'Queued',ACKNOWLEDGED:'Acknowledged',REPORTED_DONE:'Awaiting verification',VERIFIED_COMPLETE:'Artifacts verified',MIXED:'Mixed states'}[s]||s);
function agentLabel(a){
  if(!a)return 'Unlinked agent';
  if(a.display_name&&a.display_name!==a.agent_id&&!a.display_name.includes(a.endpoint))return a.display_name;
  const owned=new Set(workspaceData.tasks.filter(t=>t.owner===a.agent_id).map(t=>t.task_id));
  const link=workspaceData.taskLinks.find(t=>owned.has(t.taskId));
  const home=workspaceData.lanes.find(l=>l.id===link?.laneId);
  return home?home.name+' · '+(a.provider==='codex'?'Codex':a.provider==='claude'?'Claude':a.provider):a.display_name||a.agent_id;
}
function workspaceHref(p,l=''){const q=new URLSearchParams({project:p});if(l)q.set('lane',l);return '/rooms?'+q;}
function button(text,fn){const b=node('button',text,'text-button');b.type='button';b.addEventListener('click',fn);return b;}
function workspaceLink(text,p,l='',cls=''){const a=node('a',text,cls);a.href=workspaceHref(p,l);a.addEventListener('click',e=>{if(e.metaKey||e.ctrlKey||e.shiftKey)return;e.preventDefault();selectWorkspace(p,l);});return a;}
function selectWorkspace(p,l=''){
  project=p;lane=l;room='global';focused=0;latestSeq=0;before=null;
  for(const id of ['search','author','kind'])$(id).value='';
  history.pushState(null,'',workspaceHref(p,l));renderRooms();renderWorkspace();load();window.scrollTo({top:0});
}
async function checkWorkspace(force=false){
  if(!force&&workspaceData&&Date.now()-workspaceChecked<10000)return;
  const d=await api('/api/workspace');workspaceChecked=Date.now();
  const stamp=JSON.stringify([d.revision,d.lanes,d.agents.map(a=>[a.agent_id,a.presence])]);workspaceData=d;
  if(force||stamp!==workspaceStamp){workspaceStamp=stamp;renderWorkspace();}
}
function renderWorkspace(){
  const box=$('workspace');box.hidden=!project;box.replaceChildren();
  if(!workspaceData)return;
  const d=workspaceData, nav=$('project-list');nav.replaceChildren();
  for(const p of d.projects){
    const item=node('div',undefined,'project-nav');
    const a=workspaceLink(p.name,p.id,'','room project-name'+(project===p.id&&!lane?' selected':''));if(project===p.id&&!lane)a.setAttribute('aria-current','page');item.append(a);
    if(project===p.id)for(const l of d.lanes.filter(l=>l.projectId===p.id)){
      const link=workspaceLink(l.name,p.id,l.id,'lane-nav'+(l.id===lane?' selected':''));if(l.id===lane)link.setAttribute('aria-current','page');item.append(link);
    }
    nav.append(item);
  }
  if(!d.projects.length)nav.append(node('small','Create your first project.','muted'));
  if(!project)return;
  const p=d.projects.find(p=>p.id===project), l=d.lanes.find(l=>l.id===lane&&l.projectId===project);
  if(!p||(lane&&!l)){box.append(node('p','This project or lane is unavailable. Choose one from the sidebar.'));return;}
  $('view-kind').textContent=l?p.name.toUpperCase()+' / LANE':'PROJECT / SHARED OUTCOME';
  $('search').placeholder=l?'Search this lane’s messages…':'Search this project’s messages…';
  $('room-title').textContent=l?l.name:p.name;$('room-description').textContent=l?l.objective:p.objective;
  const toolbar=node('div',undefined,'workspace-toolbar');
  toolbar.append(node('span',l?'LANE CONSTELLATION':'PROJECT LANES','eyebrow'));
  toolbar.append(button(l?'Edit lane':'Edit project',()=>openEditor(l?'lane':'project',l||p)));
  if(!l)toolbar.append(button('+ Add lane',()=>openEditor('lane')));
  box.append(toolbar);
  const lanes=d.lanes.filter(x=>x.projectId===project);
  if(!l){
    const cards=node('div',undefined,'lane-grid');
    for(const item of lanes){
      const c=node('article',undefined,'lane-card');const top=node('div',undefined,'card-top');
      top.append(node('span',stateLabel(item.status),'status-pill '+item.status),node('span',String(item.tasks.length)+' tasks','muted'));c.append(top);
      c.append(workspaceLink(item.name,p.id,item.id,'lane-title'),node('p',item.objective,'lane-objective'));
      const members=d.members.filter(m=>m.laneId===item.id), count=new Set(members.map(m=>m.agentId)).size;
      const roles=node('div',undefined,'role-dots');
      for(const role of d.roles){const filled=members.some(m=>m.role===role);const dot=node('span',role,'role-dot'+(filled?' filled':''));dot.title=filled?'Assigned '+role:'Unfilled '+role;roles.append(dot);}
      c.append(roles,node('small',count+' linked '+(count===1?'agent':'agents')+' · '+members.length+' role assignments','muted'));cards.append(c);
    }
    box.append(cards);if(!lanes.length)box.append(node('p','Add a lane to give one part of this project a clear responsibility.','empty'));
    box.append(node('p','Each lane owns a contribution to the project. Open a lane to see its people, work and conversations. Task states come from the owners’ records.','workspace-note'));
    renderDependencies(box,lanes,null);
  }else{
    const roster=node('div',undefined,'constellation-grid');
    for(const role of d.roles){
      const cell=node('section',undefined,'role-card');cell.append(node('h3',role));
      const assignments=d.members.filter(m=>m.laneId===l.id&&m.role===role);
      if(!assignments.length)cell.append(node('p','Unfilled','vacant'));
      for(const m of assignments){
        const a=d.agents.find(a=>a.agent_id===m.agentId);const member=node('div',undefined,'member');
        member.append(node('b',agentLabel(a)),node('small',a?.provider||'Unknown provider','muted'));
        const info=node('details');info.append(node('summary','Identity & check-in'),node('code',m.agentId),node('small','Last check-in: '+date(a?.last_seen_at)+' · '+(a?.presence||'UNKNOWN'),'muted'));member.append(info);
        member.append(button('Unlink role',()=>openEditor('unlink-member',m)));cell.append(member);
      }
      cell.append(button('+ Link '+role,()=>openEditor('member',{role})));roster.append(cell);
    }
    box.append(roster,node('p','A role assignment links an existing agent. Check-in recency is separate from running compute. The independent auditor cannot also be this lane’s researcher, writer or worker.','workspace-note'));
    const workHeading=node('div',undefined,'workspace-toolbar');workHeading.append(node('h2','Work in this lane'),button('+ Link task',()=>openEditor('task')));box.append(workHeading);
    if(!l.tasks.length)box.append(node('p','No task records linked yet.','muted'));
    for(const t of l.tasks){
      const card=node('article',undefined,'task-card'), head=node('div',undefined,'card-top');head.append(node('b',t.objective),node('span',stateLabel(t.status),'status-pill '+t.status));card.append(head);
      const a=d.agents.find(a=>a.agent_id===t.owner);card.append(node('p','Owner: '+agentLabel(a),'muted'));
      if(t.blocker)card.append(node('p','Blocker: '+t.blocker,'blocker'));
      if(t.next_action)card.append(node('p','Next: '+t.next_action));
      const details=node('details');details.append(node('summary','Task identity'),node('code',t.task_id),node('small','Updated '+date(t.updated_at),'muted'),button('Unlink task from lane',()=>openEditor('task',{taskId:t.task_id,laneId:''})));card.append(details);box.append(card);
    }
    renderDependencies(box,lanes,l);
  }
}
function renderDependencies(box,lanes,current){
  const heading=node('div',undefined,'workspace-toolbar');heading.append(node('h2','Between lanes'),button('+ Add dependency',()=>openEditor('dependency')));box.append(heading);
  const edges=workspaceData.dependencies.filter(e=>lanes.some(l=>l.id===e.laneId)&&(!current||e.laneId===current.id||e.needsLaneId===current.id));
  for(const e of edges){
    const a=lanes.find(l=>l.id===e.laneId),b=lanes.find(l=>l.id===e.needsLaneId),row=node('div',undefined,'dependency');
    row.append(workspaceLink(a.name,project,a.id),node('span',' needs '),workspaceLink(b.name,project,b.id),node('p',e.reason,'muted'),button('Edit dependency',()=>openEditor('dependency',e)));box.append(row);
  }
  if(!edges.length)box.append(node('p','No lane dependencies recorded. Shared discussions can still include several lanes.','muted'));
}
function openEditor(operation,item={}){
  if(!workspaceData)return;
  const d=workspaceData, form=$('workspace-form');form.replaceChildren();$('editor-error').textContent='';
  $('editor-title').textContent={project:item.id?'Edit project':'Create project',lane:item.id?'Edit lane':'Add lane',member:'Link an existing agent', 'unlink-member':'Unlink role',task:item.taskId?'Unlink task':'Link an existing task',dependency:'Lane dependency'}[operation];
  const fields={};
  function field(key,label,value='',options=null,required=true){
    const wrapper=node('label',label),input=node(options?'select':key==='objective'||key==='reason'?'textarea':'input');input.name=key;input.required=required;
    if(options)for(const [v,t]of options)input.append(new Option(t,v));
    input.value=value;input.maxLength=key==='id'?80:key==='name'?100:2000;wrapper.append(input);form.append(wrapper);fields[key]=input;return input;
  }
  if(operation==='project'||operation==='lane'){
    field('name','Name',item.name||'');field('id','Stable ID',item.id||'');fields.id.pattern='[a-z0-9][a-z0-9_-]{0,79}';fields.id.readOnly=!!item.id;
    field('objective','Outcome this '+operation+' owns',item.objective||'');
    if(!item.id)fields.name.addEventListener('input',()=>{if(!fields.id.dataset.edited)fields.id.value=fields.name.value.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'').slice(0,80);});
    fields.id.addEventListener('input',()=>fields.id.dataset.edited='1');
    if(operation==='lane')field('aliases','Specific room aliases, comma separated',item.aliases?.join(', ')||'',null,false);
  }else if(operation==='member'){
    field('role','Role',item.role||'researcher',d.roles.map(x=>[x,x]));
    const agents=[...d.agents].sort((a,b)=>agentLabel(a).localeCompare(agentLabel(b)));
    const chooser=field('agentId','Registered agent','',[['','Choose an existing agent'],...agents.map(a=>[a.agent_id,agentLabel(a)+' · '+a.agent_id])]);
    const search=field('agentSearch','Find an agent by name or task ID','',null,false);search.type='search';form.insertBefore(search.parentElement,chooser.parentElement);
    search.addEventListener('input',()=>{const q=search.value.toLowerCase();chooser.replaceChildren(new Option('Choose an existing agent',''),...agents.filter(a=>(agentLabel(a)+' '+a.agent_id).toLowerCase().includes(q)).map(a=>new Option(agentLabel(a)+' · '+a.agent_id,a.agent_id)));});
  }else if(operation==='task'&&!item.taskId){
    const members=new Set(d.members.filter(m=>m.laneId===lane).map(m=>m.agentId));
    field('taskId','Task owned by a linked agent','',[['','Choose a task'],...d.tasks.filter(t=>members.has(t.owner)).map(t=>[t.task_id,t.objective+' · '+t.task_id])]);
    form.append(node('p','Link the actual owner to this lane first. A task has one primary lane.','muted'));
  }else if(operation==='dependency'){
    const options=d.lanes.filter(l=>l.projectId===project).map(l=>[l.id,l.name]);
    field('laneId','Lane waiting on work',item.laneId||lane||options[0]?.[0]||'',options);
    field('needsLaneId','Needs a contribution from',item.needsLaneId||options.find(x=>x[0]!==fields.laneId.value)?.[0]||'',options);
    field('reason','What contribution is needed? Leave empty to remove an existing dependency.',item.reason||'',null,false);
  }else form.append(node('p','This removes the organizational link. The existing agent and task remain available.'));
  const save=node('button',operation.startsWith('unlink')||item.taskId?'Unlink':'Save','save-button');save.type='submit';form.append(save);
  const revision=d.revision;
  form.onsubmit=async e=>{
    e.preventDefault();save.disabled=true;
    const values=Object.fromEntries(Object.entries(fields).filter(([k])=>k!=='agentSearch').map(([k,v])=>[k,v.value.trim()]));
    let payload={...item,...values};
    if(operation==='lane')payload={...payload,projectId:project,aliases:(values.aliases||'').split(',').map(x=>x.trim()).filter(Boolean)};
    if(operation==='member')payload.laneId=lane;
    if(operation==='task')payload.laneId=item.taskId?'':lane;
    try{
      const r=await fetch('/api/workspace',{method:'POST',headers:{'Content-Type':'application/json','X-KE-Board-Token':d.controlToken},body:JSON.stringify({operation,revision,item:payload})});const result=await r.json();if(!r.ok)throw Error(result.error||'Could not save');
      $('workspace-editor').close();await checkWorkspace(true);
      if(operation==='project')selectWorkspace(payload.id);else if(operation==='lane')selectWorkspace(project,payload.id);else load();
    }catch(err){$('editor-error').textContent=err.message;if(err.message.includes('Workspace changed'))await checkWorkspace(true);}finally{save.disabled=false;}
  };
  $('workspace-editor').showModal();
}
