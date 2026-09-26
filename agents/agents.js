'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => typeof value === 'number' && Number.isFinite(value);
const pct = value => num(value) ? value.toFixed(1)+'%' : '—';
const bytes = value => !num(value) ? '—' : value >= 1073741824 ? (value/1073741824).toFixed(1)+' GB' : (value/1048576).toFixed(0)+' MB';
const age = seconds => !num(seconds) ? 'Unobserved' : seconds < 60 ? Math.floor(seconds)+'s ago' : seconds < 3600 ? Math.floor(seconds/60)+'m ago' : Math.floor(seconds/3600)+'h ago';
let snapshot = null, received = 0, pending = false, failed = false, limit = 100, selected = null;
const activeStates = new Set(['thinking','working','tool activity','responding']);
const activityLabel = value => ({thinking:'Thinking observed',working:'Work observed','tool activity':'Tool activity',responding:'Responding observed',completed:'Completed',interrupted:'Interrupted',failed:'Failed',unknown:'No recent signal',unavailable:'Unavailable'}[value] || value || 'Unknown');

function renderSessions() {
  if (!snapshot) return;
  const all = snapshot.sessions.rows;
  const active = all.filter(r => r.fresh && activeStates.has(r.activity));
  const rows = $('include-idle').checked ? all : active;
  $('session-count').textContent = active.length;
  $('activity-count').textContent = rows.length;
  $('session-caption').textContent = 'Recent signals · '+all.length+' sessions checked';
  $('session-dots').innerHTML = '<i></i>'.repeat(Math.min(40,active.length));
  $('session-grid').innerHTML = rows.map(r => `<article class="session-card" title="${esc(r.id)}"><div class="session-top"><span>${esc(r.provider)}</span><span class="badge ${r.fresh && activeStates.has(r.activity)?'active':''}">${esc(activityLabel(r.activity))}</span></div><p class="session-title">${esc(r.title)}</p><div class="session-model">${esc(r.model || 'Model not exposed')}</div><div class="session-bottom"><span>${r.pid?'PID '+esc(r.pid):r.provider==='Codex'?'Shared runtime · no task PID':'Task PID unavailable'}</span><time title="${esc(r.observedAt)}">${esc(age(r.ageSeconds))}</time></div></article>`).join('') || '<p class="empty">No recent agent activity observed. Quiet sessions and running processes remain available below.</p>';
  $('session-coverage').textContent = snapshot.sessions.sources.map(s => s.source+': '+s.state+(s.limited?' · recent list limited':'')).join(' · ')+' · Shared app processes appear in the process table.';
}

function progress(p) {
  if (!p?.available) return `<div class="progress-row">${p?.state==='stale'?'Progress is stale':'Progress not published'}</div>`;
  return `<div class="progress-row">${esc(p.processed)} / ${esc(p.total)} ${esc(p.unit)} <span>· ${esc(p.percent)}%</span><progress max="100" value="${p.percent}" aria-label="Published progress ${p.percent}%"></progress></div>`;
}

function renderJobs() {
  const open = new Set([...document.querySelectorAll('details.job[open]')].map(d=>d.dataset.job));
  const cpu = snapshot.cpu;
  const live = new Set(cpu.pools.flatMap(p=>p.workers.filter(w=>w.alive).map(w=>w.pid))).size;
  const shownPools = $('include-inactive').checked?cpu.pools:cpu.pools.filter(p=>p.live>0);
  $('worker-count').textContent = cpu.ok?live:'—';
  $('pool-count').textContent = cpu.ok?cpu.pools.filter(p=>p.live>0).length+' active pools · '+cpu.pools.length+' recorded':'Worker observer unavailable';
  $('worker-dots').innerHTML = '<i></i>'.repeat(Math.min(60,live));
  $('cpu-count').textContent = cpu.ok?live+' live':'Unavailable';
  $('cpu-pools').innerHTML = !cpu.ok ? '<p class="empty">CPU Workers observer unavailable. The process census remains independent.</p>' : shownPools.map((p,i)=>`<details class="job" data-job="${esc(p.id)}" ${open.has(p.id)||(!received&&i===0)?'open':''}><summary><div><span class="job-title">${esc(p.title||p.id)}</span><p class="job-meta">Parent PID ${esc(p.parentPid??'—')} · ${p.registered?'Registered job':'Discovered pool'}</p></div><span class="badge ${p.live?'active':''}">${p.live} / ${p.workers.length} live</span></summary>${progress(p.progress)}${p.workers.map(w=>`<div class="worker"><div><button data-pid="${esc(w.pid)}">PID ${esc(w.pid)}</button> <span>${esc(w.label||'Worker')}</span><div class="muted">${esc(w.assignment||w.state)}</div></div><div class="num"><span>${w.alive?pct(w.cpuPercent):'Unverified'}</span><div class="muted">${bytes(w.memoryBytes)}</div></div></div>`).join('')}</details>`).join('') || '<p class="empty">No CPU worker pools observed. Unregistered processes still appear below.</p>';
  $('gpu-jobs').innerHTML = !snapshot.gpu.ok?'<p class="empty">GPU job registry unavailable.</p>':snapshot.gpu.jobs.map(j=>`<div class="job"><b class="job-title">${esc(j.title||j.id)}</b><p class="job-meta">GPU · ${esc(j.framework||'Framework unknown')} · ${esc(j.state||'Unknown')}</p><p class="job-meta">${j.pids.map(pid=>`<button data-pid="${pid}">PID ${pid}</button>`).join(' ')||'No PID published'} · ${j.livePids.length} observed</p>${progress(j.progress)}</div>`).join('')||'<p class="empty">No registered GPU jobs. Per-job GPU usage is not exposed by macOS.</p>';
  $('swarm-jobs').innerHTML = `<div class="job"><b>Swarm runs</b><span class="badge"> · ${snapshot.swarm.ok?esc(snapshot.swarm.counts?.live??0)+' live processes':'Unavailable'}</span></div>`+(snapshot.swarm.runs.map(r=>`<div class="job"><b class="job-title">${esc(r.title||r.runId||'Recorded run')}</b><p class="job-meta">${esc(r.state||'Unknown')} · ${esc(r.runId)}</p></div>`).join('')||'<p class="empty">No recent run ledger. Swarm runtime processes remain visible below.</p>');
}

function filteredProcesses() {
  const query = $('process-search').value.toLowerCase().trim(), filter = $('process-filter').value;
  const rows = snapshot.processes.filter(r => (filter==='all'||filter==='busy'&&(r.cpuPercent??0)>0||filter==='agents'&&(r.provider||r.groups.length)) && (!query||[r.name,r.script,r.provider,r.category,r.pid,r.ppid,...r.groups].join(' ').toLowerCase().includes(query)));
  const sort = $('process-sort').value;
  rows.sort((a,b)=>sort==='cpu'?(b.cpuPercent??-1)-(a.cpuPercent??-1):sort==='memory'?(b.memoryBytes??-1)-(a.memoryBytes??-1):sort==='pid'?a.pid-b.pid:a.name.localeCompare(b.name));
  return rows;
}

function renderProcesses() {
  if (!snapshot) return;
  const rows=filteredProcesses();
  $('process-count').textContent=rows.length+' matched / '+snapshot.coverage.observed+' observed';
  $('process-body').innerHTML=rows.slice(0,limit).map(r=>`<tr><td><button data-pid="${r.pid}" title="Inspect PID ${r.pid}">${esc(r.name)}</button><span class="process-sub">${esc(r.script||r.provider||r.executable)}</span></td><td>${esc(r.category)}</td><td class="num ${r.cpuPercent>1?'cpu-hot':''}">${pct(r.cpuPercent)}</td><td class="num">${bytes(r.memoryBytes)}</td><td class="num">${r.pid}</td><td class="num">${r.ppid??'—'}</td><td>${esc(r.state)}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">No matching processes.</td></tr>';
  $('more').hidden=rows.length<=limit;
  $('process-coverage').textContent=`Showing ${Math.min(rows.length,limit)} of ${rows.length} · ${snapshot.coverage.restricted} with restricted fields · ${snapshot.coverage.exitedDuringScan} exited during scan`;
}

function renderDetail() {
  if (!selected||!snapshot) return;
  const r=snapshot.processes.find(p=>p.pid===selected.pid&&p.startedAt===selected.startedAt);
  $('detail-name').textContent=selected.name+' · PID '+selected.pid;
  if (!r) {$('detail-content').innerHTML='<p class="empty">This exact process has exited or is no longer visible. A reused PID is a different process.</p>';return;}
  const parent=snapshot.processes.find(p=>p.pid===r.ppid),children=snapshot.processes.filter(p=>p.ppid===r.pid);
  $('detail-content').innerHTML=`<dl><dt>CPU</dt><dd>${pct(r.cpuPercent)}</dd><dt>Memory</dt><dd>${bytes(r.memoryBytes)}</dd><dt>State</dt><dd>${esc(r.state)}</dd><dt>Threads</dt><dd>${r.threads??'—'}</dd><dt>Started</dt><dd>${r.startedAt?esc(new Date(r.startedAt*1000).toLocaleString()):'Unknown'}</dd><dt>Observed identity</dt><dd>${esc(r.provider||'Unclassified process')}</dd><dt>Evidence</dt><dd>${esc(r.evidence)}</dd><dt>Job references</dt><dd>${esc(r.groups.join(' · ')||'None published')}</dd><dt>Parent</dt><dd>${parent?`<button data-pid="${parent.pid}">${esc(parent.name)} · PID ${parent.pid}</button>`:esc(r.ppid??'Unknown')}</dd></dl><p class="detail-note">${children.length} observed child processes</p><div class="detail-links">${children.map(c=>`<button data-pid="${c.pid}">${esc(c.name)} · ${c.pid}</button>`).join('')||'None'}</div><p class="detail-note">Runtime ancestry identifies a relationship, not a task assignment. Registry PID references do not independently verify process start time. No command arguments or environment values are displayed.</p>`;
}

function render() {
  $('host-cpu').textContent=pct(snapshot.host.cpuPercent);
  $('host-cores').textContent=snapshot.host.logicalCpus+' logical cores · sampled interval';
  $('cpu-meter').style.width=(snapshot.host.cpuPercent??0)+'%';
  $('memory-value').textContent=pct(snapshot.host.memoryPercent);
  $('memory-caption').textContent=bytes(snapshot.host.memoryAvailable)+' available of '+bytes(snapshot.host.memoryTotal);
  $('memory-meter').style.width=snapshot.host.memoryPercent+'%';
  renderSessions();renderJobs();renderProcesses();renderDetail();
}

function freshness() {
  const seconds=snapshot?(Date.now()-Date.parse(snapshot.generatedAt))/1000:Infinity;
  const stale=failed||seconds>20;
  document.body.classList.toggle('stale',stale&&!!snapshot);
  $('freshness').textContent=pending?'Refreshing…':!snapshot?'Waiting for observation':(stale?'Stale · ':'Observed ')+age(seconds);
}

async function refresh() {
  if (pending) return;
  pending=true;freshness();$('refresh').disabled=true;
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),18000);
  try {
    const response=await fetch('/api/agents',{cache:'no-store',signal:controller.signal});
    if(!response.ok)throw new Error('Observer unavailable');
    const data=await response.json();
    if(data.schemaVersion!=='ke.switchboard-agents.v1')throw new Error('Unsupported observation');
    snapshot=data;failed=false;$('notice').hidden=true;render();received=Date.now();
  }catch(error){failed=true;$('notice').hidden=false;$('notice').textContent=snapshot?'Refresh failed. The last observation is shown dimmed until fresh data returns.':'The worker observer is unavailable. Refresh to try again.';}
  finally{clearTimeout(timer);pending=false;$('refresh').disabled=false;freshness();}
}
$('refresh').addEventListener('click',refresh);
$('include-idle').addEventListener('change',renderSessions);
$('include-inactive').addEventListener('change',()=>{if(snapshot)renderJobs();});
for(const id of ['process-search','process-filter','process-sort'])$(id).addEventListener(id==='process-search'?'input':'change',()=>{limit=100;renderProcesses();});
$('more').addEventListener('click',()=>{limit+=100;renderProcesses();});
$('close-detail').addEventListener('click',()=>$('inspector').close());
document.addEventListener('click',event=>{const button=event.target.closest('[data-pid]');if(!button||!snapshot)return;const pid=Number(button.dataset.pid),row=snapshot.processes.find(r=>r.pid===pid);selected=row?{pid,startedAt:row.startedAt,name:row.name}:{pid,startedAt:null,name:'Process'};renderDetail();if(!$('inspector').open)$('inspector').showModal();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{freshness();if(!document.hidden)refresh();},5000);
refresh();
