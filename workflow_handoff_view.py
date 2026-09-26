"""Read-only presentation for Birds. Labels never change routing or work state."""
import re
import sqlite3
from workflow import workflow_reader


def readable_assignment(value):
    """Make a display label from the existing key; preserve its exact key elsewhere."""
    value = str(value or '')
    lower = value.lower()
    if 'role-only' in lower:
        return 'Review task naming' if 'audit' in lower or 'review' in lower else 'Keep task names consistent'
    if 'birds-research-cold-resume' in lower:
        return 'Restore Research handoff pickup'
    parts = value.split(':')
    label = parts[-1] if len(parts) > 1 else value
    label = re.sub(r'@[0-9a-f]{7,64}', '', label)
    label = re.sub(r'-v\d+(?:-\d{8})?$', '', label)
    label = re.sub(r'-\d{8}$', '', label)
    label = re.sub(r'\b(?:RO|SD|CS)\d?-F\d+[-:]?', '', label, flags=re.I)
    label = re.sub(r'[-_:]+', ' ', label).strip()
    words = {'pid': 'PID', 'ui': 'UI', 'api': 'API', 'json': 'JSON', 'nul': 'NUL'}
    label = ' '.join(words.get(w.lower(), w) for w in label.split())
    return (label[:1].upper() + label[1:]) or 'Work handoff'


def decorate_snapshot(flow, snap, sessions):
    """Add display fields using one read-only message query, no transport calls."""
    ids = {str(w['message_id']) for w in snap.get('work', [])}
    ids.update(str(h['id']) for h in snap.get('handoffs', []))
    messages = {}
    if ids:
        with workflow_reader(flow.path) as db:
            db.row_factory = sqlite3.Row
            marks = ','.join('?' for _ in ids)
            messages = {r['id']: r['body'] for r in db.execute(
                'SELECT id,body FROM workflow_messages WHERE id IN (' + marks + ')', sorted(ids))}
    for w in snap.get('work', []):
        w['displayTitle'] = readable_assignment(w['contract']['assignmentId'])
        w['senderName'] = flow.display_identity(w['sender'], sessions)
        w['recipientName'] = flow.display_identity(w['recipient'], sessions)
        w['nextActorName'] = flow.display_identity(w['nextActor'], sessions)
        w['messageBody'] = messages.get(w['message_id'], '')
        w['dueAt'] = w['created'] + w['contract']['dueSeconds']
    for h in snap.get('handoffs', []):
        h['messageBody'] = messages.get(h['id'], '')
    return snap


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent handoffs · Birds</title>
<style>
:root{color-scheme:dark;--bg:#101c20;--panel:#16262b;--line:#30464b;--text:#e4ece7;--muted:#a4b8b8;--green:#a1dec3;--amber:#f3cf8d;--red:#f0aba5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select{font:inherit}button,summary,select{cursor:pointer}button,input,select{color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:9px}button{padding:9px 14px}button[aria-busy=true]{opacity:.65;cursor:progress}button:hover,summary:hover{background:#20373c}a{color:var(--green)}:focus-visible{outline:2px solid var(--green);outline-offset:4px}main{max-width:1360px;margin:0 auto;padding:34px 32px 70px}.top{display:flex;justify-content:space-between;gap:24px;align-items:flex-start}.eyebrow{color:var(--green);font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin:0 0 7px}h1{font-size:32px;line-height:1.15;letter-spacing:-.8px;margin:0 0 10px}p{margin:0}.intro{color:var(--muted)}.service{text-align:right;flex-shrink:0;font-size:13px}.service-line{display:flex;align-items:center;justify-content:flex-end;gap:9px;margin-bottom:6px}.dot{width:7px;height:7px;background:var(--green);border-radius:50%;display:inline-block}.dot.warn{background:var(--amber)}.updated{color:var(--muted);font-size:12px}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:28px 0 20px}.tab{border-color:transparent;background:transparent;color:var(--muted);display:flex;align-items:center;gap:12px}.tab[aria-pressed=true]{background:#223c3b;border-color:#467167;color:var(--text)}.count{font-size:12px;border-radius:5px;background:#0d181b;padding:1px 7px;min-width:23px;text-align:center}.toolbar{display:flex;gap:12px;align-items:center;margin-bottom:18px}.search{flex:1;position:relative}.search input{width:100%;padding:11px 14px;min-width:0}.toolbar select{padding:11px 12px;max-width:240px}.list-heading{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin:22px 0 12px}.list-heading h2{font-size:17px;margin:0;font-weight:600}.list-heading span{font-size:12px;color:var(--muted)}.notice{border:1px solid #806a3e;background:#302b21;color:var(--amber);padding:12px 16px;border-radius:9px;margin-top:18px}.notice[hidden]{display:none}.rows{border-top:1px solid var(--line)}.row{border-bottom:1px solid var(--line)}.row summary{list-style:none;display:grid;grid-template-columns:minmax(0,1fr) 255px 152px;gap:26px;padding:20px 8px;border-radius:6px}.row summary::-webkit-details-marker{display:none}.title-line{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:7px}.title{font-size:17px;font-weight:600;line-height:1.35;overflow-wrap:anywhere}.badge{font-size:11px;line-height:1.5;font-weight:600;border-radius:5px;padding:2px 7px;color:var(--muted);background:#26383e}.badge.attention{color:var(--amber);background:#383222}.badge.blocked{color:var(--red);background:#3b2a2c}.badge.good{color:var(--green);background:#213b33}.route{font-size:12px;color:var(--muted);line-height:1.7;overflow-wrap:anywhere}.route .arrow{padding:0 7px;color:#6d9187}.excerpt{color:var(--muted);font-size:13px;margin-top:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}.small-label{color:var(--muted);text-transform:uppercase;letter-spacing:.07em;font-size:10px;margin-bottom:4px}.next-name{font-size:13px;line-height:1.55;overflow-wrap:anywhere}.next-action{font-size:12px;color:var(--muted);margin-top:4px}.due{font-size:13px}.due.overdue{color:var(--amber)}.due-date{font-size:11px;color:var(--muted);margin-top:3px}.open-label{font-size:11px;color:var(--green);display:block;margin-top:10px}.row[open]{background:#142329}.row[open] .open-label{color:var(--muted)}.when-open{display:none}.row[open] .when-open{display:inline}.row[open] .when-closed{display:none}.expanded{padding:4px 24px 24px;max-width:100%;border-top:1px dashed var(--line);margin:0 8px}.expanded h3{font-size:12px;color:var(--green);text-transform:uppercase;letter-spacing:.06em;margin:20px 0 8px}.message{font-size:13px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}.meta{display:grid;grid-template-columns:130px minmax(0,1fr);gap:9px;font-size:12px;margin:0}.meta dt{color:var(--muted)}.meta dd{margin:0;overflow-wrap:anywhere}.meta code{font-size:11px}.empty{text-align:center;padding:50px 20px;color:var(--muted)}.empty strong{display:block;color:var(--text);font-size:17px;margin-bottom:8px}.footer{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-top:18px;color:var(--muted);font-size:12px}.footer p{max-width:820px}.loading{padding:35px 8px;color:var(--muted)}#more[hidden],#delivery-filter[hidden]{display:none}
@media(min-width:1800px){main{padding-top:42px}}@media(max-width:960px){.row summary{grid-template-columns:minmax(0,1fr) 210px;gap:16px}.main-col{grid-column:1/-1}.toolbar select{max-width:200px}}
@media(max-width:650px){main{padding:24px 16px 40px}.top{display:block}h1{font-size:28px}.service{text-align:left;margin-top:16px}.service-line{justify-content:flex-start}.tabs{margin:22px 0 16px;gap:3px}.tab{padding:8px 9px;gap:6px}.toolbar{flex-wrap:wrap;gap:8px}.search{flex-basis:100%}.toolbar select{max-width:none;flex:1;min-width:0}.toolbar button{flex-shrink:0}.row summary{display:grid;grid-template-columns:1fr 1fr;padding:18px 2px;gap:15px}.main-col{grid-column:1/-1;grid-row:auto;padding:0}.owner-col,.time-col{grid-row:auto;grid-column:auto;padding:0;margin:0;pointer-events:auto}.title{font-size:16px}.expanded{padding:2px 6px 20px;margin:0}.meta{grid-template-columns:1fr;gap:4px}.meta dd{margin-bottom:9px}.list-heading{align-items:flex-start}.list-heading span{text-align:right}.footer{align-items:flex-start}.intro{font-size:13px}}
.flow{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:24px 0 4px}.flow[hidden]{display:none}.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.tile .v{font-size:24px;font-weight:650;letter-spacing:-.4px;margin:2px 0}.tile .s{color:var(--muted);font-size:12px}.tile .good{color:var(--green)}.tile .warn{color:var(--amber)}@media(max-width:650px){.flow{grid-template-columns:1fr 1fr}}
</style></head><body><main>
<header class="top"><div><p class="eyebrow">Birds · Work in transit</p><h1>Agent handoffs</h1><p class="intro">See who has the work, what needs attention, and what came back.</p></div><div class="service"><div class="service-line"><span id="dot" class="dot warn"></span><span id="health">Connecting…</span></div><p id="updated" class="updated">Read-only · refreshes automatically</p></div></header>
<div id="error" class="notice" role="status" hidden></div>
<nav class="tabs" aria-label="Handoff views"><button class="tab" data-view="attention" aria-pressed="true">Needs attention <span class="count" id="n-attention">–</span></button><button class="tab" data-view="active" aria-pressed="false">Active work <span class="count" id="n-active">–</span></button><button class="tab" data-view="history" aria-pressed="false">Accepted returns <span class="count" id="n-history">–</span></button><button class="tab" data-view="delivery" aria-pressed="false">Delivery log <span class="count" id="n-delivery">–</span></button></nav>
<div class="toolbar"><label class="search"><input type="search" id="search" aria-label="Search work, agents or message IDs" placeholder="Search work or agents…" autocomplete="off"></label><select id="agent" aria-label="Filter by agent"><option value="">All agents</option></select><button id="delivery-filter" hidden>Show all deliveries</button><button id="refresh">Refresh</button></div>
<div class="list-heading"><h2 id="heading">Needs attention</h2><span id="result-count" aria-live="polite"></span></div><div id="rows" class="rows"><p class="loading">Loading handoffs…</p></div>
<div class="footer"><p id="footnote">A picked-up assignment is still in progress. A returned result needs its sender’s review.</p><button id="more" hidden>Show more</button></div>
</main><script>
'use strict';
const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let data=null, view='attention', shown=30, loading=false, lastGood=null, timer=null, deliveryIssuesOnly=true;
const headings={attention:'Needs attention',active:'Work moving through the team',history:'Returns accepted by the sender',delivery:'Recent delivery activity'};
const labels={AWAITING_PICKUP:'Awaiting pickup',ACCEPTED:'Picked up',RESULT_REPORTED:'Ready for review',BLOCKED:'Blocked',REVISION:'Changes requested',RUNNING:'Delivery active',RETURNED:'Turn ended',ACKNOWLEDGED:'Message acknowledged',BUSY:'Waiting for idle',UNAVAILABLE:'Delivery unavailable',UNCERTAIN:'Delivery uncertain',FAILED:'Delivery failed',HELD:'Held',QUEUED:'Queued',CANCELLED:'Cancelled',DUPLICATE:'Duplicate suppressed'};
function accepted(w){return !!w.closed&&w.closure?.outcome==='accepted'}
function attention(w){return w.needsAttention===true}
function group(w){return attention(w)?'attention':accepted(w)?'history':'active'}
function friendly(v){return labels[v]||String(v||'Unknown').replaceAll('_',' ').toLowerCase()}
function stamp(s){return Number.isFinite(Number(s))&&Number(s)>0?new Date(Number(s)*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'Not recorded'}
function relative(s){const n=Math.round(Math.abs(s)/60);return n<1?'less than a minute':n<60?n+' min':n<1440?Math.floor(n/60)+' hr '+(n%60?n%60+' min':''):Math.floor(n/1440)+' days'}
function deadline(w){if(w.closed)return ['Closed',stamp(w.closed),false];let due=Number(w.dueAt??(w.created+w.contract.dueSeconds)),delta=due-Date.now()/1000;if(!Number.isFinite(due))return ['Not recorded','',false];return [delta<0?relative(delta)+' overdue':'Due in '+relative(delta),stamp(due),delta<0]}
function next(w){if(accepted(w))return ['No action required','Return accepted'];if(w.closed&&w.closure?.outcome==='revision')return [w.nextActorName,'Route the correction'];if(w.workState==='BLOCKED')return [w.nextActorName,'Resolve the blocker'];if(w.overdue)return [w.nextActorName,'Check overdue work'];if(w.returned)return [w.nextActorName,'Review the returned result'];return [w.nextActorName,w.accepted?'Return the result':'Accept the assignment']}
function badge(text,kind){return '<span class="badge '+kind+'">'+esc(text)+'</span>'}
function metadata(pairs){return '<dl class="meta">'+pairs.map(([k,v])=>'<dt>'+esc(k)+'</dt><dd><code>'+esc(v)+'</code></dd>').join('')+'</dl>'}
function excerptText(value){let text=String(value||'').replace(/\s+/g,' ').trim();text=(text.match(/^.*?[.!?](?=\s|$)/)||[text])[0];text=text.replace(/\/(?:Users|Volumes|var|private)\/.*$/g,'[file in details]').replace(/\b[0-9a-f]{32,64}\b/gi,'[reference in details]');return text.length>240?text.slice(0,237).replace(/\s+\S*$/,'')+'…':text}
function row(w,delivery){let id=delivery?w.id:w.message_id,state=delivery?w.status:accepted(w)?'RETURN_ACCEPTED':w.workState;
let title=delivery?friendly(state):w.displayTitle||w.contract.assignmentId;
let status=state==='RETURN_ACCEPTED'?'Return accepted':friendly(state),kind=state==='BLOCKED'||['FAILED','UNCERTAIN','UNAVAILABLE'].includes(state)?'blocked':(delivery?w.needsAttention:attention(w))?'attention':accepted(w)?'good':'';
let [actor,action]=delivery?[w.recipientName,w.detail]:next(w),[due,date,late]=delivery?['Updated',stamp(w.updated),false]:deadline(w);
let excerpt=delivery?w.detail:(w.result?.summary||w.closure?.note||w.messageBody||'');
let pairs=delivery?[['Message ID',id],['Delivery state',state],['Attempts',w.attempts],['Sender ID',w.sender],['Recipient ID',w.recipient],['Created',stamp(w.created)]]:[['Assignment',w.contract.assignmentId],['Message ID',id],['Project',w.contract.projectId],['Sender ID',w.sender],['Recipient ID',w.recipient],['Next actor ID',w.nextActor],['Result path',w.contract.resultPath],['Created',stamp(w.created)],['Picked up',stamp(w.accepted)],['Returned',stamp(w.returned)],['Deadline',stamp(w.dueAt??(w.created+w.contract.dueSeconds))]];
return '<details class="row" data-key="'+esc(id)+'"><summary id="summary-'+esc(id)+'"><div class="main-col"><div class="title-line"><span class="title">'+esc(title)+'</span>'+(!delivery?badge(status,kind):'')+'</div><p class="route"><span>'+esc(w.senderName||w.sender)+'</span><span class="arrow" aria-label="to">→</span><span>'+esc(w.recipientName||w.recipient)+'</span></p><p class="excerpt">'+esc(excerptText(excerpt))+'</p></div><div class="owner-col"><p class="small-label">'+(delivery?'Delivery status':accepted(w)?'Disposition':'Next responsible')+'</p><p class="next-name">'+(delivery?badge(status,kind):esc(actor||'Unresolved identity'))+'</p><p class="next-action">'+esc(delivery?w.detail:action)+'</p></div><div class="time-col"><p class="due '+(late?'overdue':'')+'">'+esc(due)+'</p><p class="due-date">'+esc(date)+'</p><span class="open-label"><span class="when-closed">View handoff ↓</span><span class="when-open">Close handoff ↑</span></span></div></summary><div class="expanded"><h3>Original message</h3><p class="message">'+esc(w.messageBody||'Original message is not available.')+'</p>'+(w.result?'<h3>Returned result</h3><p class="message">'+esc(w.result.summary||w.result.disposition)+'</p>':'')+(w.closure?'<h3>Sender disposition</h3><p class="message">'+esc(w.closure.note||w.closure.outcome)+'</p>':'')+'<h3>Exact record</h3>'+metadata(pairs)+'</div></details>'}
function render(){if(!data)return;let works=data.work||[],deliveries=data.handoffs||[];let counts={attention:works.filter(attention).length,active:works.filter(w=>group(w)==='active').length,history:works.filter(accepted).length,delivery:deliveries.filter(attention).length};for(let k in counts)$('n-'+k).textContent=counts[k]+(k==='delivery'?' alerts':'');$('delivery-filter').hidden=view!=='delivery';$('delivery-filter').textContent=deliveryIssuesOnly?'Show all deliveries':'Show issues only';
document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===view)));$('heading').textContent=headings[view];
let q=$('search').value.trim().toLocaleLowerCase(),a=$('agent').value,items=(view==='delivery'?(deliveryIssuesOnly?deliveries.filter(attention):deliveries):works.filter(w=>group(w)===view)).filter(w=>(!a||[w.sender,w.recipient,w.nextActor].includes(a))&&(!q||JSON.stringify(w).toLocaleLowerCase().includes(q)));
if(view==='attention')items.sort((a,b)=>Number(!!b.overdue)-Number(!!a.overdue)||a.created-b.created);
let opens=new Set([...document.querySelectorAll('.row[open]')].map(e=>e.dataset.key)),focus=document.activeElement?.id,scroll=window.scrollY;
let html=items.slice(0,shown).map(w=>row(w,view==='delivery')).join('');if(!html)html='<div class="empty"><strong>'+(q||a?'No matching handoffs':view==='attention'?'Nothing needs attention':'No handoffs in this view')+'</strong>'+(q||a?'Try another search or choose All agents.':'Use the other views to follow active work and accepted returns.')+'</div>';
$('rows').innerHTML=html;document.querySelectorAll('.row').forEach(e=>{e.open=opens.has(e.dataset.key)});if(focus?.startsWith('summary-'))$(focus)?.focus({preventScroll:true});window.scrollTo(0,scroll);
$('result-count').textContent=(items.length>shown?'Showing '+shown+' of ':'')+items.length+' handoff'+(items.length===1?'':'s');$('more').hidden=items.length<=shown;
$('footnote').textContent=view==='delivery'?'Delivery tracks the message. A turn ending or message acknowledgement does not mean the assigned work is accepted.':view==='history'?'These returns were accepted by their sender. Installation and the underlying task’s completion remain separate.':'Picked up means the agent accepted the assignment. Returned results still need the sender’s review.';
}
function agents(){let entries=new Map;for(let w of [...(data.work||[]),...(data.handoffs||[])]){entries.set(w.sender,w.senderName||w.sender);entries.set(w.recipient,w.recipientName||w.recipient)}let value=$('agent').value;let options='<option value="">All agents</option>'+[...entries].sort((a,b)=>a[1].localeCompare(b[1])).map(([id,name])=>'<option value="'+esc(id)+'">'+esc(name)+'</option>').join('');if($('agent').innerHTML!==options){$('agent').innerHTML=options;$('agent').value=entries.has(value)?value:''}}
async function refresh(){if(loading)return;loading=true;$('refresh').setAttribute('aria-busy','true');clearTimeout(timer);try{let res=await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(12000)});if(!res.ok)throw Error('HTTP '+res.status);let next=await res.json();if(!Array.isArray(next.work)||!Array.isArray(next.handoffs))throw Error('Invalid response');data=next;lastGood=new Date();$('error').hidden=true;$('error').textContent='';$('health').textContent=data.daemonHealthy?'Dispatcher online':'Dispatcher needs attention';$('dot').classList.toggle('warn',!data.daemonHealthy);$('updated').textContent='Updated '+lastGood.toLocaleTimeString()+' · read-only';agents();render()}catch(e){$('error').hidden=false;$('error').textContent='Unable to refresh. '+(lastGood?'Showing the last successful update from '+lastGood.toLocaleTimeString()+'.':'No live data is available yet.')+' Refresh to try again.';$('health').textContent='Live connection unavailable';$('dot').classList.add('warn')}finally{loading=false;$('refresh').removeAttribute('aria-busy');timer=setTimeout(refresh,5000)}}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>{view=b.dataset.view;shown=30;render()}));$('search').addEventListener('input',()=>{shown=30;render()});$('agent').addEventListener('change',()=>{shown=30;render()});$('refresh').addEventListener('click',refresh);$('delivery-filter').addEventListener('click',()=>{deliveryIssuesOnly=!deliveryIssuesOnly;shown=30;render()});$('more').addEventListener('click',()=>{shown+=30;render()});refresh();
</script></body></html>'''
