(function(root){
'use strict';
const PROJECT='demo', LANE='workstream-demo-work', STALE_MS=120000;
const object=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
function scope(search){
  const p=new URLSearchParams(search);
  if(p.getAll('project').length!==1||p.getAll('lane').length!==1||p.getAll('display').length!==1||p.get('project')!==PROJECT||p.get('lane')!==LANE||!['library','dispatch'].includes(p.get('display')))throw Error('Unknown display scope. Open this view from the registered service catalog.');
  return {project:PROJECT,lane:LANE,view:p.get('display'),source:'current'};
}
function url(s,offset=0){
  if(s.source==='current')return '/api/service-displays/current-work?'+new URLSearchParams({project:s.project,lane:s.lane,kind:s.view==='library'?'receipts':'work',offset:String(offset)});
  return s.view==='library'?'/api/library?'+new URLSearchParams({project:s.project,lane:s.lane,team:'',offset:String(offset)}):'/api/taskflow?'+new URLSearchParams({project:s.project,lane:s.lane});
}
function id(r,s){return s.source==='current'||s.view==='library'?r.id:r.taskId;}
function textField(o,key){if(o[key]!==undefined&&o[key]!==null&&typeof o[key]!=='string')throw Error('Malformed text field: '+key);}
function validRecord(r,s){
  if(!object(r)||typeof id(r,s)!=='string'||!id(r,s))throw Error('Unreadable record identity.');
  for(const key of ['title','request','objective','state','status','excerpt','waitReason','blocker','nextAction','requiredResult','path','sha','kind','basis'])textField(r,key);
  const good=s.source==='current'||s.view==='library'?r.project===s.project&&r.lane===s.lane:r.projectId===s.project&&r.laneId===s.lane;
  if(!good)throw Error('The service returned a record outside this project scope.');
  if(s.source==='current'&&(!object(r.contract)||r.contract.projectId!==s.project||r.contract.laneId!==s.lane||r.contract.assignmentId!==r.assignmentId||typeof r.state!=='string'||typeof r.assignmentId!=='string'||!object(r.receipt)))throw Error('Malformed current-work record.');
  if(s.source==='current'){for(const [key,field] of [['return','summary'],['closure','note']]){if(r[key]!=null){if(!object(r[key]))throw Error('Malformed lifecycle record.');textField(r[key],field);}}textField(r.receipt,'verification');textField(r.receipt,'reason');}
}
function rows(d,s){
  const list=s.source==='current'?d.records:s.view==='library'?d.items:d.tasks;
  if(!Array.isArray(list)||list.length>1000)throw Error('Unreadable or oversized record list.');
  list.forEach(r=>validRecord(r,s));return list;
}
function freshness(last,now,failed){if(!last)return failed?'error':'loading';return failed||now-last>STALE_MS?'stale':'current';}
function nextOffset(d,requested){
  if(d.nextOffset===null)return null;
  if(!Number.isInteger(d.nextOffset)||d.nextOffset<=requested||d.nextOffset>100000)throw Error('Invalid or nonadvancing page cursor.');
  return d.nextOffset;
}
function prepare(d,s,old,requested,more,now){
  if(!object(d))throw Error('Unreadable service response.');
  if(s.source==='current'&&(d.schema!=='ke.current-work.v1'||d.project!==s.project||d.lane!==s.lane||d.kind!==(s.view==='library'?'receipts':'work')||d.requestedOffset!==requested||!Number.isFinite(d.observedAt)))throw Error('Current-work response scope or page does not match.');
  const incoming=rows(d,s), seen=new Set(),records=more?[...old.records,...incoming]:[...incoming];
  for(const r of records){const key=id(r,s);if(seen.has(key))throw Error('Duplicate record identity in overlapping pages. Refresh to read a new snapshot.');seen.add(key);}
  const paged=s.source==='current'||s.view==='library';
  const offset=paged?nextOffset(d,requested):null;
  let sources=[],status=null,sourceStale=false;
  if(s.source==='historical'&&s.view==='library'){
    if(!Array.isArray(d.sources)||!object(d.status))throw Error('Malformed Library sources or index status.');
    for(const r of d.sources){if(!object(r)||r.project!==s.project||(r.lane&&r.lane!==s.lane))throw Error('Invalid Library source scope.');if(r.path!==undefined&&typeof r.path!=='string')throw Error('Invalid Library source path.');}
    for(const r of d.sources)textField(r,'basis');textField(d.status,'status');textField(d.status,'finishedAt');
    sources=[...d.sources];status={...d.status};
    sourceStale=!status.finishedAt||!Number.isFinite(Date.parse(status.finishedAt))||now-Date.parse(status.finishedAt)>86400000||status.status!=='completed';
  }
  // The caller replaces one snapshot only after every field is validated.
  return {records,offset,sources,status,sourceStale,last:now,observedAt:d.observedAt||null};
}
function validateDetail(d,r,s){
  const item=s.source==='current'?d.record:s.view==='library'?d:d.task;
  validRecord(item,s);
  if(id(item,s)!==id(r,s))throw Error('Returned detail identity does not match the selected record.');
  if(s.source==='current'&&(d.project!==s.project||d.lane!==s.lane||d.schema!=='ke.pid-current-work.v1'))throw Error('Wrong detail response scope.');
  return item;
}
const api={scope,url,rows,freshness,nextOffset,prepare,validateDetail,STALE_MS};
if(typeof module!=='undefined'&&module.exports)module.exports=api;
if(!root.document)return;
const $=id=>document.getElementById(id), node=(tag,text,cls)=>{const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e;};
const empty=()=>({records:[],offset:null,sources:[],status:null,sourceStale:false,last:0});
let s,snapshot=empty(),failed=false,pending=false,version=0,detailVersion=0;
function state(kind,text){$('status').dataset.state=kind;$('status').textContent=text;}
function coverage(){
  $('coverage').textContent=s.source==='current'?
    'Current Birds '+(s.view==='library'?'returned receipts':'work obligations')+': '+(snapshot.last?'a bounded page from the project work contracts.':'not yet available; absence of loaded data does not mean no work.')+' Historical Task Board records and the Library index are separate. Search covers loaded pages only. Receipt hashes are checked when you open a record.':
    'Historical '+(s.view==='library'?'Library index':'Task Board records')+' only. Current Birds obligations and their bound receipts are excluded from this source; choose Current work & bound receipts to view them. Search covers loaded pages only. A recent fetch does not prove coverage or a fresh index.';
}
function stamp(){
  const last=snapshot.last;
  if(last)$('freshness').textContent='Last successful read: '+new Date(last).toLocaleString()+'. '+((snapshot.sourceStale||freshness(last,Date.now(),failed)==='stale')?'Saved view may be out of date.':'');
  if(last&&!failed&&!pending&&!snapshot.sourceStale&&freshness(last,Date.now(),false)==='stale')state('stale','This view is stale. Refresh to read current records.');
}
async function read(path){
  const c=new AbortController(),t=setTimeout(()=>c.abort(),20000);
  try{const r=await fetch(path,{method:'GET',cache:'no-store',credentials:'same-origin',signal:c.signal});if(!r.ok)throw Error('Local service returned HTTP '+r.status+'.');const d=await r.json();if(!object(d)||d.error)throw Error(d?.error||'Unreadable service response.');return d;}
  catch(e){if(e.name==='AbortError')throw Error('The local service did not respond within 20 seconds.');throw e;}finally{clearTimeout(t);}
}
function display(){
  const q=$('search').value.toLowerCase();$('records').replaceChildren();let shown=0;
  for(const r of snapshot.records){
    if(q&&!JSON.stringify(r).toLowerCase().includes(q))continue;shown++;
    const a=node('article','','record'),top=node('div','','record-top');
    top.append(node('h2',r.title||r.request||r.objective||id(r,s)),node('span',r.state||r.status||'State not recorded','state'));a.append(top);
    if(s.source==='current'){
      a.append(node('p',r.closure?.note||r.return?.summary||'This obligation has no returned result yet.'));
      if(r.overdue)a.append(node('p','Deadline passed; lifecycle remains as recorded.'));
      a.append(node('div','Original message: '+r.id,'meta'));
      if(r.receipt?.recorded)a.append(node('div','Recorded SHA-256 (not checked yet): '+r.receipt.recorded.sha256,'path'));
    }else{
      a.append(node('p',s.view==='library'?(r.excerpt||'No text excerpt available.'):(r.waitReason||r.blocker||r.nextAction||r.requiredResult||r.request||'Recorded work item.')));
      a.append(node('div',(r.kind||'Task')+' · '+id(r,s),'meta'));
      if(r.path)a.append(node('div',r.path,'path'));if(r.sha)a.append(node('div','Recorded SHA-256: '+r.sha,'path'));
    }
    const b=node('button',s.source==='current'?'Read obligation & receipt':s.view==='library'?'Read source & receipt':'Read recorded work');b.type='button';b.onclick=()=>detail(r);a.append(b);$('records').append(a);
  }
  if(!shown)$('records').append(node('div',snapshot.records.length?'No records match this loaded-page filter.':s.source==='current'?'No matching work contracts in this returned page. This is not proof that the whole project is idle.':'No historical records loaded in this scope. Current work is a separate source.','empty'));
}
async function detail(r){
  const run=++detailVersion;$('detail').hidden=false;$('detail').replaceChildren(node('p','Reading the recorded source…'));
  try{
    const path=s.source==='current'?'/api/service-displays/current-work?'+new URLSearchParams({id:r.id,project:s.project,lane:s.lane,kind:s.view==='library'?'receipts':'work'}):s.view==='library'?'/api/library/item?'+new URLSearchParams({id:r.id,project:s.project,lane:s.lane,team:''}):'/api/taskflow/task?'+new URLSearchParams({id:r.taskId});
    const d=await read(path);if(run!==detailVersion)return;
    const item=validateDetail(d,r,s);
    const elements=[node('h2',item.title||item.request||'Recorded work'),node('p','Recorded lifecycle: '+(item.state||'Not recorded'),'meta')];
    if(s.source==='current'){
      const receipt=item.receipt;
      if(!object(receipt))throw Error('Unreadable receipt verification.');
      elements.push(node('p','Receipt: '+receipt.verification+(receipt.reason?' — '+receipt.reason:'')));
      if(receipt.recorded)elements.push(node('div','Recorded SHA-256: '+receipt.recorded.sha256,'path'));
      if(receipt.observedSha256)elements.push(node('div','Observed SHA-256: '+receipt.observedSha256,'path'));
      elements.push(node('p','Original message: '+item.id,'path'));
      for(const key of ['acceptedAt','returnedAt','closedAt'])elements.push(node('p',({acceptedAt:'Accepted',returnedAt:'Returned',closedAt:'Closed'})[key]+': '+(item[key]?new Date(item[key]*1000).toLocaleString():'Not recorded'),'meta'));
      elements.push(node('p','Recorded receipt claims: '+JSON.stringify(receipt.recordedClaims||{})));
      const raw=node('details','');raw.append(node('summary','Full bound work and receipt JSON'),node('pre',JSON.stringify(item,null,2)));elements.push(raw);
    }else elements.push(node('div',item.path||item.basis||item.taskId||'','path'),node('div',item.sha?'SHA-256: '+item.sha:'','path'),node('pre',s.view==='library'?(typeof item.body==='string'?item.body:JSON.stringify(item.body,null,2)):JSON.stringify(d,null,2)));
    $('detail').replaceChildren(...elements);
  }catch(e){if(run===detailVersion)$('detail').replaceChildren(node('p','Record unavailable: '+e.message));}
}
function sources(){
  $('sources').hidden=!(s.source==='historical'&&s.view==='library');
  const d=snapshot;$('source-content').replaceChildren();
  if(!d.status)return;
  $('source-content').append(node('p','Index state: '+(d.status.status||'Not reported')+'. Index freshness is separate from fetch freshness.','meta'),node('p','Last completed index: '+(d.status.finishedAt||'Not recorded'),'meta'));
  for(const r of d.sources)$('source-content').append(node('p',r.path||r.basis||'Source without a path','path'));
}
async function refresh(more=false){
  if(pending||more&&snapshot.offset===null)return;
  pending=true;const run=++version;++detailVersion;$('detail').hidden=true;$('refresh').disabled=true;$('more').disabled=true;$('record-source').disabled=true;state('loading','Reading project records…');
  const requested=more?snapshot.offset:0;
  try{
    const d=await read(url(s,requested)),candidate=prepare(d,s,snapshot,requested,more,Date.now());
    if(run!==version)return;
    snapshot=candidate;failed=false;
    state(snapshot.sourceStale?'stale':'current',(snapshot.sourceStale?'Library index is stale or incomplete. ':'')+snapshot.records.length+' records in loaded pages'+(snapshot.offset!==null?' · more available':''));
    sources();display();coverage();$('more').hidden=snapshot.offset===null;
  }catch(e){
    failed=true;state(snapshot.last?'stale':navigator.onLine===false?'offline':'error',(snapshot.last?'Showing the last successful read. ':'Records unavailable. ')+e.message+' Use Refresh records to try again.');
    if(!snapshot.last)$('records').replaceChildren(node('div','No live records were loaded. No sample records have been substituted.','empty'));
  }finally{pending=false;$('refresh').disabled=false;$('more').disabled=false;$('record-source').disabled=false;stamp();}
}
try{
  s=scope(location.search);const title=s.view==='library'?'Library & Receipts':'Task Board / Dispatch';document.title=title+' · Switchboard';$('title').textContent=title;
  for(const v of ['library','dispatch']){const a=$(v+'-link');a.href='/inspector/service-display.html?'+new URLSearchParams({project:s.project,lane:s.lane,display:v});if(v===s.view)a.setAttribute('aria-current','page');}
  $('refresh').onclick=()=>refresh();$('more').onclick=()=>refresh(true);$('search').oninput=display;
  $('record-source').onchange=()=>{if(pending)return;s.source=$('record-source').value;snapshot=empty();failed=false;$('freshness').textContent='No successful read yet.';$('search').value='';$('records').replaceChildren();$('sources').hidden=true;$('more').hidden=true;coverage();refresh();};
  coverage();setInterval(stamp,1000);refresh();
}catch(e){state('error',e.message);$('refresh').disabled=true;$('search').disabled=true;}
})(typeof window!=='undefined'?window:globalThis);
