'use strict';
const $=id=>document.getElementById(id),agent=new URLSearchParams(location.search).get('agent'),R=window.SwitchboardMessageRenderer;
const entries=new Map(),nodes=new Map(),groups=new Map(),callNames=new Map(),MAX_ENTRIES=800;
let cursor=null,busy=false,following=true,version='',viewVersion='',initial=true,title='Agent',pending=0,pages=0;
const stream=$('transcript'),reader=crypto.randomUUID?.()||Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
const readingKey='switchboard.reader.v2.'+agent;
let savedReading=null,restoringHistory=false,restoreAttempts=0,readingTimer;try{savedReading=JSON.parse(localStorage.getItem(readingKey));}catch{}
if(savedReading&&savedReading.agent===agent){$('search').value=String(savedReading.query||'').slice(0,200);following=savedReading.following!==false;restoringHistory=!following&&!!savedReading.entryId;}
function rememberReading(){if(initial||busy||restoringHistory||stream.clientHeight===0)return;const a=anchor(),id=a.row?.dataset.entryId||a.row?.dataset.groupId;const open=[];for(const n of document.querySelectorAll('details[open]')){if(n.dataset.entryId)open.push('t:'+n.dataset.entryId);else if(n.dataset.groupId)open.push('g:'+n.dataset.groupId);else if(n.classList.contains('message-source'))open.push('s:'+n.closest('[data-entry-id]')?.dataset.entryId);}try{localStorage.setItem(readingKey,JSON.stringify({agent,entryId:id,offset:a.offset-stream.getBoundingClientRect().top,following,query:$('search').value,open:open.slice(0,100),source:version.split(':')[0]}));}catch{}}
function restoreReading(){if(!restoringHistory||stream.clientHeight===0)return;const n=nodes.get(savedReading.entryId)?.node||groups.get(savedReading.entryId)?.node;const row=n?.closest('.tool-group')||n;if(row&&!row.hidden){stream.scrollTop+=row.getBoundingClientRect().top-stream.getBoundingClientRect().top-(Number(savedReading.offset)||0);restoringHistory=false;following=false;return;}if(cursor&&entries.size<MAX_ENTRIES&&restoreAttempts++<20){setTimeout(()=>load(true),0);}else{restoringHistory=false;$('notice').textContent='The earlier reading position is no longer available. Current public records are shown.';}}
window.addEventListener('pagehide',rememberReading);
new ResizeObserver(()=>{if(!busy)restoreReading();}).observe(stream);

function anchor(){const y=stream.getBoundingClientRect().top,row=[...$('entries').children].find(n=>!n.hidden&&n.getBoundingClientRect().bottom>y);return {top:stream.scrollTop,row,offset:row?.getBoundingClientRect().top};}
function restore(a){stream.scrollTop=a.top;if(a.row?.isConnected&&!a.row.hidden)stream.scrollTop+=a.row.getBoundingClientRect().top-a.offset;}
function visibleRows(){return [...$('entries').children].filter(n=>!n.hidden);}
function render(force=false){
 const saved=anchor(),q=$('search').value.toLowerCase(),ordered=[...entries.values()].sort((a,b)=>a.position-b.position||(a.sequence||0)-(b.sequence||0));
 for(const e of ordered)if(e.toolKind==='call'&&e.callId)callNames.set(e.callId,e.label);
 let position=$('entries').firstElementChild,group=null,shown=0;const usedGroups=new Set();
 for(const e of ordered){
  let item=nodes.get(e.id),key=JSON.stringify([e.text,e.displayText,e.sources,(e.attachments||[]).map(({url,...a})=>a),title,callNames.get(e.callId)]);
  if(!item){item={node:e.role==='tool'?R.tool(e,callNames):R.message(e,{agent,title}),key};nodes.set(e.id,item);if(savedReading?.open?.includes('t:'+e.id)&&item.node.tagName==='DETAILS')item.node.open=true;if(savedReading?.open?.includes('s:'+e.id)){const source=item.node.querySelector('.message-source');if(source)source.open=true;}}
  else{if(item.key!==key){item.node=R.replaceContent(item.node,e,{agent,title},callNames);item.key=key;}else if(e.role!=='tool')R.refreshAttachments(item.node,e,agent);}
  item.node.hidden=q&&!((e.displayText??e.text)+' '+e.label).toLowerCase().includes(q);if(!item.node.hidden)shown++;
  if(e.role==='tool'){
   if(!group){group=groups.get(e.id);if(!group){const n=R.node('details','tool-group'),summary=R.node('summary'),body=R.node('div','tool-group-records');n.dataset.groupId=e.id;n.append(summary,body);group={node:n,summary,body,count:0,visible:0};groups.set(e.id,group);if(savedReading?.open?.includes('g:'+e.id))group.node.open=true;}group.count=0;group.visible=0;group.position=group.body.firstElementChild;usedGroups.add(e.id);if(group.node!==position)$('entries').insertBefore(group.node,position);position=group.node.nextElementSibling;}
   if(item.node!==group.position)group.body.insertBefore(item.node,group.position);group.position=item.node.nextElementSibling;group.count++;if(!item.node.hidden)group.visible++;group.node.hidden=!group.visible;const label='Tool activity · '+group.count+' records';if(group.summary.textContent!==label)group.summary.textContent=label;if(q&&group.visible)group.node.open=true;
  }else{group=null;if(item.node!==position)$('entries').insertBefore(item.node,position);position=item.node.nextElementSibling;}
 }
 for(const [id,g]of groups)if(!usedGroups.has(id)){g.node.remove();groups.delete(id);}
 for(const [id,n]of nodes)if(!entries.has(id)){n.node.remove();nodes.delete(id);}
 $('empty').hidden=shown>0;$('empty').textContent=entries.size?'No matching loaded messages.':'No public conversation messages recorded yet.';restore(saved);
}
function olderControl(){$('older').hidden=!cursor;$('older').disabled=busy||entries.size>=MAX_ENTRIES;$('older').textContent=entries.size>=MAX_ENTRIES?'History limit reached · use Latest to reset':'Load earlier messages';}
async function load(older=false,reset=false,force=false){
 if(busy||older&&!cursor)return;busy=true;olderControl();const saved=anchor(),wasFollowing=following;try{
  const response=await fetch('/api/transcript?agent='+encodeURIComponent(agent)+'&reader='+reader+(version?'&initialized=1':'')+(older?'&before='+encodeURIComponent(cursor):''),{cache:'no-store'}),d=await response.json();if(!response.ok)throw Error(d.error||'Unable to read this session');if(d.agentId!==agent)throw Error('Selected session does not match this transcript');
  title=d.title;document.title=title+' · Session';$('title').textContent=title;$('provider').textContent=d.provider.toUpperCase();$('notice').textContent=d.notice;$('state').textContent=d.available?'Reading session':'No transcript';$('state').classList.remove('error');
  if(!force&&!d.resetRequired&&!older&&!reset&&version===d.sourceVersion&&viewVersion===d.viewVersion)return;
  const priorParts=version.split(':'),nextParts=(d.sourceVersion||'').split(':');const rewritten=version&&nextParts[0]===priorParts[0]&&(Number(nextParts[1])<Number(priorParts[1])||Number(nextParts[1])===Number(priorParts[1])&&nextParts[2]!==priorParts[2]);
  if(restoringHistory&&savedReading.source&&savedReading.source!==nextParts[0]){restoringHistory=false;savedReading=null;$('notice').textContent='The source changed. Current public records are shown; the previous reading position was not restored.';}
  if(reset||d.resetRequired||rewritten||version&&nextParts[0]!==priorParts[0]){entries.clear();nodes.clear();groups.clear();$('entries').replaceChildren();cursor=null;pages=0;}
  if(d.coverage){const present=new Set(d.entries.map(e=>e.id));for(const [id,e]of entries)if(e.position>=d.coverage.from&&e.position<d.coverage.through&&!present.has(id))entries.delete(id);}
  let added=0;
  for(const e of d.entries){if(!entries.has(e.id))added++;entries.set(e.id,e);}
  if(older){cursor=d.nextBefore;pages++;}else if(!pages||reset)cursor=d.nextBefore;
  // Bound loaded history without removing what the user is currently reading.
  if(entries.size>MAX_ENTRIES){const sorted=[...entries.values()].sort((a,b)=>a.position-b.position);if(wasFollowing&&!older){while(entries.size>MAX_ENTRIES)entries.delete(sorted.shift().id);cursor=sorted[0]?.position||cursor;}else{while(entries.size>MAX_ENTRIES)entries.delete(sorted.pop().id);$('notice').textContent='Loaded history limit reached. Latest loads the current messages.';}}
  render();if(older)restore(saved);else if((initial&&!restoringHistory)||wasFollowing||reset){stream.scrollTop=stream.scrollHeight;pending=0;}else{restore(saved);pending+=added;}
  $('new-messages').hidden=!pending;$('new-messages').textContent=pending+' new records ↓';$('updated').textContent='Updated '+new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});version=d.sourceVersion;viewVersion=d.viewVersion;initial=false;
 }catch(e){$('state').textContent='Read unavailable';$('state').classList.add('error');$('notice').textContent=e.message+' · Keeping displayed messages.';}finally{const settled=anchor();busy=false;olderControl();if(!older&&wasFollowing)stream.scrollTop=stream.scrollHeight;else restore(settled);restoreReading();}
}
$('older').onclick=()=>load(true);$('refresh').onclick=()=>load(false,false,true);$('search').oninput=()=>{render();$('search-count').textContent=$('search').value?visibleRows().length+' groups match':'';rememberReading();};
function latest(){restoringHistory=false;following=true;pending=0;$('new-messages').hidden=true;$('search').value='';$('search-count').textContent='';if(entries.size>=MAX_ENTRIES)load(false,true);else{render();stream.scrollTop=stream.scrollHeight;}}
$('latest').onclick=latest;$('new-messages').onclick=latest;
stream.addEventListener('scroll',()=>{if(stream.clientHeight===0)return;following=stream.scrollHeight-stream.scrollTop-stream.clientHeight<90;if(following){pending=0;$('new-messages').hidden=true;}clearTimeout(readingTimer);readingTimer=setTimeout(rememberReading,150);},{passive:true});
if(new URLSearchParams(location.search).get('embedded'))document.body.classList.add('embedded');
load();setInterval(()=>{if(!document.hidden)load();},3000);
