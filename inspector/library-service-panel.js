'use strict';
/* One project Library registry; no enrollment, assignment or provider writes. */
window.LibraryServicePanel=(()=>{
 const root=document.getElementById('library-services');if(!root)return {load(){}};
 const summary=root.querySelector('summary'),body=root.querySelector('.library-service-content');let sequence=0;
 const el=(tag,text,cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
 async function load(project,lane='',team=''){
  const run=++sequence;
  try{
   const r=await fetch('/api/library/services?'+new URLSearchParams({project,lane,team}),{cache:'no-store'}),d=await r.json();
   if(!r.ok)throw Error(d.error||'Library services unavailable');if(run!==sequence)return;
   const homes=new Map(d.homes.map(h=>[h.agent,h]));
   summary.textContent='Library services · '+(!d.schemaReady?'Setup pending':homes.size?homes.size+' registered · enrollment unavailable':'No librarians assigned');
   body.replaceChildren();body.append(el('p',!d.schemaReady?'The service registry has not been set up.':!homes.size?'No librarian has been enrolled for this project. Existing evidence and context remain available.':'Registered homes are retained. Runtime enrollment is not yet available; these records do not prove active staffing.','status'));
   for(const h of homes.values()){
    const row=el('section','','library-service-home');row.dataset.agentId=h.agent;
    const who=el('button',h.agent);who.type='button';who.onclick=()=>{if(parent!==window&&parent.WorkflowDesk){parent.WorkflowLibraryView?.hide();parent.WorkflowDesk.select(h.agent);}};
    row.append(who,el('p',h.lane+' / '+h.team,'item-meta'),el('p','Provider: '+h.provider+' · Queue: '+h.queue,'item-meta'));
    for(const s of d.services.filter(s=>s.agent===h.agent)){
     const ref=el('div',s.kind+' · '+s.lane+' / '+s.team+' · '+(s.scopeUsable?'Available':s.scopeBlocker||s.state),'library-service-reference');ref.dataset.serviceId=s.id;ref.title='Service '+s.id+' · version '+s.version;row.append(ref);
    }
    body.append(row);
   }
   body.append(el('p','Assignments revision '+(d.revision??'unavailable')+' · Workflow '+d.topologyRevision,'item-meta'));
  }catch(e){if(run!==sequence)return;summary.textContent='Library services · unavailable';body.replaceChildren(el('p',e.message,'notice-error'));}
 }
 return {load};
})();
