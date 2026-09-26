'use strict';
window.TeamLibrary={create({project,lane,team,post,node,button,begin=()=>0,isActive=()=>true}){
 let value=null;
 const root=document.getElementById('preview');
 async function load(){
  const response=await fetch('/api/library/context?'+new URLSearchParams({project,lane,team}),{cache:'no-store'});
  const data=await response.json();if(!response.ok)throw Error(data.error);
  value=data;return data;
 }
 async function render(){
  const run=begin();
  try{
   const data=await load();if(!isActive(run))return;
   const label=team?'Team context':lane?'Workstream context':'Project context';
   const breadcrumb=[data.organization.project?.name||project,data.organization.lane?.name,data.organization.team?.name].filter(Boolean).join(' / ');
   root.replaceChildren();root.append(node('p','item-meta',breadcrumb),node('h2','',label));
   const form=node('form','team-context-form');
   for(const [key,label]of [['question','Current question'],['decisions','Decisions and constraints'],['uncertainties','Open questions and contradictions'],['nextStep','Next step / handoff']]){
    const field=node('label','',label),input=node('textarea');input.name=key;input.rows=key==='question'?2:3;
    input.value=data.current.body[key]||'';input.maxLength=12000;if(key==='question')input.required=true;
    field.append(input);form.append(field);
   }
   const save=button('Save context',()=>{});save.type='submit';save.className='primary';
   const status=node('span','status',data.current.version?'Version '+data.current.version+' · '+new Date(data.current.updated).toLocaleString():'No context recorded');
   const tools=node('div','preview-tools');tools.append(save,status);form.append(tools);
   let requestId=null,serialized=null;
   form.onsubmit=async event=>{
    event.preventDefault();if(!isActive(run))return;save.disabled=true;
    try{
     const payload={...Object.fromEntries(new FormData(form)),project,lane,team,version:data.current.version,contextFingerprint:data.fingerprint};
     const current=JSON.stringify(payload);if(current!==serialized){requestId=crypto.randomUUID();serialized=current;}
     await post('context',{...payload,requestId});if(isActive(run))await render();
    }catch(error){if(isActive(run))status.textContent=error.message;}finally{save.disabled=false;}
   };
   root.append(form);
   const linked=node('div','context-linked');linked.append(node('h3','','Assigned sessions'));
   for(const session of data.organization.members){
    const link=node('a','context-member',session.title);link.href='/session?agent='+encodeURIComponent(session.agentId);
    link.target='_blank';link.rel='noopener';linked.append(link);
   }
   if(!data.organization.members.length)linked.append(node('p','status','No sessions assigned'));
   linked.append(node('h3','','Registered work'));
   for(const task of data.organization.tasks){
    const row=node('div','context-task');
    row.append(node('span','tag',task.status),node('p','',task.objective),node('span','status',task.next_action||task.blocker||''));linked.append(row);
   }
   linked.append(node('h3','','Library reviews'));
   for(const review of data.reviews){
    const row=node('div','context-task');row.append(node('span','tag',review.contextCurrent?'Current context':'Context changed'),node('p','',review.decision+' · '+review.query),node('p','status',review.reason));linked.append(row);
   }
   if(!data.reviews.length)linked.append(node('p','status','No library review recorded in this scope'));
   root.append(linked);document.getElementById('library-content').classList.add('show-preview');
  }catch(error){if(isActive(run))root.replaceChildren(node('p','reader-error',error.message));}
 }
 return {render,load,get:()=>value};
}};
