'use strict';
/* Shared view: existing Library stays in the workflow shell with its real source API. */
(()=>{
 const workspace=document.querySelector('.workflow-workspace');if(!workspace)return;
 const pane=document.createElement('section');pane.className='wf-library-view';pane.hidden=true;pane.setAttribute('aria-label','Project Library');workspace.prepend(pane);
 let active=false,scope='',camera=null;
 const selected=()=>new URLSearchParams(location.search),viewport=document.getElementById('viewport');
 function show(params){
  if(!params){params=selected();for(const key of ['lane','team','panel'])if(params.has('library'+key))params.set(key,params.get('library'+key));}

  if(!active)camera=window.SwitchboardWorkflowCamera?.capture?.()||window.SwitchboardCanvasPan?.camera?.()||{x:viewport.scrollLeft,y:viewport.scrollTop};active=true;
  const q=new URLSearchParams({project:params.get('project')||selected().get('project')||'demo',embedded:'1',lane:params.get('lane')||'',team:params.get('team')??(params.get('lane')?'researcher':''),panel:params.get('panel')||'workspace'});
  for(const k of ['lane','team','item','view','panel'])if(params.has(k)&&!(k==='view'&&params.get(k)==='library'))q.set(k,params.get(k));
  const key=q.toString();if(key!==scope){scope=key;const frame=document.createElement('iframe');frame.title='Project Library records and context';frame.src='/library?'+q;pane.replaceChildren(frame);}
  pane.hidden=false;document.body.classList.add('library-view');const u=new URL(location.href);u.searchParams.set('view','library');for(const key of ['lane','team','panel'])u.searchParams.set('library'+key,q.get(key)||'');history.replaceState(null,'',u);syncTabs();
 }
 function hide(){if(!active)return;active=false;pane.hidden=true;document.body.classList.remove('library-view');const u=new URL(location.href);u.searchParams.delete('view');history.replaceState(null,'',u);if(camera){if(window.SwitchboardCanvasPan?.scrollTo)window.SwitchboardCanvasPan.scrollTo(camera.x,camera.y);else viewport.scrollTo(camera.x,camera.y);}syncTabs();}
 function syncTabs(){for(const a of document.querySelectorAll('#capabilities-nav a')){const hasLibrary=!!document.querySelector('#capabilities-nav a[data-path="/library"]'),on=active&&hasLibrary?a.dataset.path==='/library':a.dataset.path==='/constellations';if(on)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');}}
 document.addEventListener('click',e=>{const a=e.target.closest('a[href]');if(e.defaultPrevented||!a||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;const u=new URL(a.href,location.href);if(u.origin!==location.origin)return;if(u.pathname==='/library'){e.preventDefault();show(u.searchParams);}else if(active&&u.pathname==='/constellations'&&a.closest('#capabilities-nav')){e.preventDefault();hide();}});
 window.addEventListener('switchboard-workflow-scope',e=>{if(!active)return;show();const restored=e.detail?.camera;if(restored&&(!camera?.key||camera.key!==restored.key))camera=restored;});
 window.addEventListener('message',e=>{if(e.origin!==location.origin||e.source!==pane.querySelector('iframe')?.contentWindow||e.data?.type!=='switchboard-library-scope')return;const d=e.data;if(!['project','lane','team','panel'].every(k=>typeof d[k]==='string')||d.project!==selected().get('project'))return;const u=new URL(location.href);for(const key of ['lane','team','panel'])u.searchParams.set('library'+key,d[key]);history.replaceState(null,'',u);const q=new URLSearchParams(scope);for(const key of ['lane','team','panel'])q.set(key,d[key]);scope=q.toString();});
 window.addEventListener('pagehide',()=>{try{localStorage.setItem('switchboard.library-active.v1.'+selected().get('project'),String(active));}catch{}});
 window.WorkflowLibraryView={show,hide,get active(){return active;}};
 if(selected().get('view')==='library')show();
 requestAnimationFrame(syncTabs);
})();

/* Library-only references in existing Research and Build department surfaces. */
(()=>{
 const canvas=document.querySelector('.workflow-workspace');if(!canvas)return;
 const cache=new Map();let scheduled=false;
 function addLinks(){scheduled=false;const targets=[...canvas.querySelectorAll('.wf-section[data-section="research"],.wf-section[data-section="build"]')];if(['research','build'].includes(new URLSearchParams(location.search).get('section')))targets.push(...canvas.querySelectorAll('.wf-detail-tools'));
  for(const target of targets){if(target.querySelector('.wf-library-references'))continue;const lane=target.closest('[data-lane-id]')?.dataset.laneId;if(!lane)continue;const project=new URLSearchParams(location.search).get('project');if(!project)continue;const bar=document.createElement('div');bar.className='wf-library-references';bar.style.cssText='display:flex;flex-wrap:wrap;gap:8px;padding:9px;font-size:11px';const url=new URL('/library',location.origin);url.search=new URLSearchParams({project,lane,team:'',panel:'workspace'});
   const library=document.createElement('a');library.textContent='Library services';library.href=url;bar.append(library);const requirements=document.createElement('a');requirements.textContent='Requirements';url.searchParams.set('panel','requirements');requirements.href=url;bar.append(requirements);const state=document.createElement('span');state.textContent='Reading service references…';bar.append(state);target.append(bar);
   if(!cache.has(project))cache.set(project,fetch('/api/library/services?'+new URLSearchParams({project}),{cache:'no-store'}).then(async r=>{if(!r.ok)throw Error();return r.json();}).catch(()=>null));
   cache.get(project).then(d=>{if(!bar.isConnected)return;if(!d||d.project!==project||!d.schemaReady){state.textContent='Staffing unavailable';return;}const refs=d.services.filter(s=>!s.lane||s.lane===lane);state.textContent=['context','research'].map(kind=>kind[0].toUpperCase()+kind.slice(1)+': '+(refs.some(s=>s.kind===kind&&s.state==='active'&&s.scopeUsable)?'scope admitted':'not usable')).join(' · ');state.title=refs.map(s=>s.id+' · '+s.agent+' · '+(s.scopeBlocker||s.state)).join('\n')||'No registered service references';});
  }
 }
 const schedule=()=>{if(!scheduled){scheduled=true;requestAnimationFrame(addLinks);}};new MutationObserver(schedule).observe(canvas,{childList:true,subtree:true});window.addEventListener('switchboard-workflow-scope',()=>{cache.clear();schedule();});schedule();
})();
