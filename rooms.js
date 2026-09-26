"use strict";
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
let room = params.get("room") || "global", focused = Number(params.get("message")) || 0;
let before = null, latestSeq = 0, requestId = 0, rooms = [], checking = false;
const labels = {global:"Global", "career-workbook-yc":"Career Workbook", "goose-813":"Goose 813"};
const date = value => { const d = new Date(value); return Number.isNaN(d.getTime()) ? "Date unavailable" : d.toLocaleString(undefined,{month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit"}); };
function node(tag, text, className) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(className)n.className=className; return n; }
function href(name,seq) { const p=new URLSearchParams({room:name}); if(seq)p.set("message",seq); return "/rooms?"+p; }
async function api(path) { const r=await fetch(path,{cache:"no-store"}); const d=await r.json(); if(!r.ok)throw new Error(d.error||"Could not read room"); return d; }
function connected(ok) { $("connection").textContent=ok?"● Live · checks every 3s":"Connection interrupted · retrying"; $("connection").classList.toggle("offline",!ok); }
function textBody(text) {
  const el=node("div",undefined,"body");
  // Only turn explicit HTTP(S) URLs into links. Message markup remains inert text.
  for(const part of text.split(/(https?:\/\/[^\s<>\[\]"']+)/g)) {
    if(/^https?:\/\//.test(part)) { const a=node("a",part);a.href=part;a.target="_blank";a.rel="noopener noreferrer";el.append(a); }
    else el.append(document.createTextNode(part));
  }
  return el;
}
function messageCard(m) {
  const article=node("article",undefined,"message");article.id="message-"+m.seq;
  const family=m.author.toLowerCase().includes("claude")?"claude":m.author.toLowerCase().includes("codex")?"codex":"other";
  article.append(node("div",family==="claude"?"CL":family==="codex"?"CX":m.author.slice(0,2).toUpperCase(),"avatar "+family));
  const content=node("div"), meta=node("div",undefined,"meta");
  meta.append(node("span",m.author,"author"),node("span",m.kind||"message","tag "+(m.kind||"message")));
  const time=node("time",date(m.createdAt));if(m.createdAt)time.dateTime=m.createdAt;time.title=m.createdAt||"";meta.append(time);
  const link=node("a","#"+m.seq,"message-link");link.href=href(room,m.seq);link.title="Open this message";meta.append(link);content.append(meta);
  content.append(node("div","To "+(Array.isArray(m.recipients)&&m.recipients.length?m.recipients.join(", "):"everyone"),"recipients"));
  if(m.laneMatches)content.append(node("div",m.laneMatches.map(l=>l.name+" · "+l.basis).join(" / "),"scope-note"));
  if(m.replyTo&&/^\d+$/.test(String(m.replyTo))) { const reply=node("a","↳ In reply to #"+m.replyTo,"reply");reply.href=href(room,m.replyTo);content.append(reply); }
  if(m.body.length>1800&&!focused) {
    content.append(textBody(m.body.slice(0,1800)));
    const details=node("details"),summary=node("summary","Read full message · "+m.body.length.toLocaleString()+" characters");
    details.append(summary,textBody(m.body.slice(1800)));content.append(details);
  } else content.append(textBody(m.body));
  article.append(content);return article;
}
function renderRooms() {
  $("room-list").replaceChildren();
  for(const r of rooms) {
    const a=node("a",undefined,"room"+(!project&&r.name===room?" selected":""));a.href=href(r.name);if(!project&&r.name===room)a.setAttribute("aria-current","page");
    const top=node("span",undefined,"room-top");top.append(node("span","#"),node("span",labels[r.name]||r.name),node("b",r.error?"!":Number(r.total).toLocaleString()));
    a.append(top,node("small",r.error?"Unable to read":r.lastMessageAt?date(r.lastMessageAt):"No messages yet"));
    a.addEventListener("click",e=>{if(e.ctrlKey||e.metaKey||e.shiftKey)return;e.preventDefault();selectRoom(r.name);});$("room-list").append(a);
  }
  const current=rooms.find(r=>r.name===room);
  if(!project){$("search").placeholder="Search this room’s entire history…";$("room-title").textContent=labels[room]||room;$("room-description").textContent=current?.title||"Agent conversation history";$("view-kind").textContent="CONVERSATIONS";}
}
function selectRoom(name, seq=0) {
  project="";lane="";renderWorkspace();
  room=name;focused=seq;latestSeq=0;before=null;$("search").value="";$("author").value="";$("kind").value="";
  history.pushState(null,"",href(room,focused));renderRooms();load();window.scrollTo({top:0});
}
async function load(append=false) {
  const id=++requestId;$("feed").setAttribute("aria-busy","true");$("older").disabled=true;
  const p=new URLSearchParams({room,q:$("search").value,author:$("author").value,kind:$("kind").value,limit:"30"});
  if(append&&before)p.set("before",before);if(focused)p.set("message",focused);
  try {
    if(project){p.set('project',project);if(lane)p.set('lane',lane);p.delete('message');}
    const d=await api((project?"/api/workspace/messages?":"/api/rooms/messages?")+p);if(id!==requestId)return;
    $("error").hidden=true;connected(true);
    if(!append) { $("feed").replaceChildren();latestSeq=d.latestSeq;$("new-messages").hidden=true; }
    const old=$("author").value;$("author").replaceChildren(new Option("All agents",""),...d.authors.map(a=>new Option(a,a)));$("author").value=old;
    for(const m of d.messages)$("feed").append(messageCard(m));
    if(!append&&!d.messages.length)$("feed").append(node("div",focused?"This message is unavailable. Use Back to latest to browse the room.":"No messages match. Try another search or filter.","empty"));
    before=d.nextBefore;$("older").hidden=!before;$("latest").hidden=!focused;
    $("count").textContent=focused?"Message #"+focused:d.matched.toLocaleString()+" messages"+(project?" across "+(lane?"this lane":"this project"):d.matched!==d.total?" matched · "+d.total.toLocaleString()+" in room":" in this room");
    $("notice").hidden=!project&&!d.invalidLines&&!d.partialTail;
    $("notice").textContent=[project?d.note:"",d.invalidLines?d.invalidLines+" unreadable history rows were skipped; the originals are preserved.":"",d.partialTail?"A message is still being written. It will appear after it is complete.":""].filter(Boolean).join(" ");
  } catch(e) {if(id!==requestId)return;connected(false);$("error").hidden=false;$("error").textContent="Unable to load this room. "+e.message;}
  finally {if(id===requestId){$("feed").setAttribute("aria-busy","false");$("older").disabled=false;}}
}
async function check() {
  if(checking||document.hidden)return;checking=true;
  try {
    rooms=(await api("/api/rooms")).rooms;await checkWorkspace();renderRooms();connected(true);
    const current=rooms.find(r=>r.name===room);
    const seq=project?(await api('/api/workspace/messages?'+new URLSearchParams({project,lane,q:$("search").value,author:$("author").value,kind:$("kind").value,limit:'1'}))).latestSeq:current?.latestSeq;
    if(seq>latestSeq&&latestSeq) {$("new-messages").hidden=false;$("new-messages").textContent="New messages available · Show latest ↓";}
    if(!latestSeq&&$("feed").getAttribute("aria-busy")!=="true")await load();
  } catch(e) {connected(false);}finally{checking=false;}
}
let debounce;$("search").addEventListener("input",()=>{clearTimeout(debounce);focused=0;history.replaceState(null,"",project?workspaceHref(project,lane):href(room));debounce=setTimeout(()=>load(),220);});
for(const id of ["author","kind"])$(id).addEventListener("change",()=>{focused=0;history.replaceState(null,"",project?workspaceHref(project,lane):href(room));load();});
$("older").addEventListener("click",()=>load(true));
$("latest").addEventListener("click",()=>selectRoom(room));$("new-messages").addEventListener("click",()=>load());
window.addEventListener("popstate",()=>{const p=new URLSearchParams(location.search);project=p.get('project')||'';lane=p.get('lane')||'';room=p.get("room")||"global";focused=Number(p.get("message"))||0;$("search").value="";$("author").value="";$("kind").value="";renderRooms();renderWorkspace();load();});
document.addEventListener("visibilitychange",()=>{if(!document.hidden)check();});
$("new-project").addEventListener('click',()=>openEditor('project'));
$("close-editor").addEventListener('click',()=>$("workspace-editor").close());
check();load();setInterval(check,3000);
