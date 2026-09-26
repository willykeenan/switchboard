'use strict';
const $ = id => document.getElementById(id);
const STORE = 'ke.switchboard.capability-bookmarks.v1';
let featuredIds = [];
const icons = {'Custom skills':'✳'};
let catalog = null, activeCategory = '', selected = null, toastTimer;
let remembered;
try { const raw = localStorage.getItem(STORE); const value = raw === null ? [] : JSON.parse(raw); remembered = new Set(Array.isArray(value) ? value.filter(x=>typeof x === 'string') : []); }
catch { remembered = new Set(); }

function el(tag, cls, text) { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; }
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,2600); }
function notify(message) { $('notice').textContent=message; $('notice').hidden=!message; }
function bookmark(entry) {
  const saved=remembered.has(entry.id), b=el('button','bookmark',saved?'★':'☆');
  b.type='button'; b.setAttribute('aria-label',(saved?'Forget ':'Remember ')+entry.title); b.setAttribute('aria-pressed',String(saved));
  b.onclick=()=>{ const focusId=entry.id; if(remembered.has(entry.id))remembered.delete(entry.id);else remembered.add(entry.id);
    try{localStorage.setItem(STORE,JSON.stringify([...remembered]));}catch{toast('Remembered for this visit; browser storage is unavailable.');}
    render(); document.querySelector(`[data-capability="${CSS.escape(focusId)}"] .bookmark`)?.focus(); };
  return b;
}
function renderCategories() {
  const root=$('categories'); root.replaceChildren();
  const options=[['','All capabilities'],['remembered','★ Remembered'],...catalog.categories.map(x=>[x,x])];
  for(const [key,title] of options){const count=catalog.capabilities.filter(e=>!key||(key==='remembered'?remembered.has(e.id):e.category===key)).length;
    const b=el('button','category',title); b.type='button'; b.setAttribute('aria-pressed',String(activeCategory===key)); b.append(el('span','',String(count)));
    b.onclick=()=>{activeCategory=key;render();}; root.append(b);
  }
}
function renderFeatures() {
  const root=$('featured'); root.replaceChildren();
  featuredIds=catalog.capabilities.filter(e=>e.featured).map(e=>e.id);
  if(!featuredIds.length)root.append(el('p','loading','No custom skills found yet. Add a SKILL.md under ~/.claude/skills, ~/.codex/skills or ~/.agents/skills.'));
  for(const id of featuredIds){const entry=catalog.capabilities.find(e=>e.id===id);if(!entry)continue;
    const article=el('article','feature');const top=el('div','feature-top');
    top.append(el('span','feature-icon',icons[entry.category]||'+'),el('code','command',entry.invocation));
    article.append(top,el('h3','',entry.title),el('p','',entry.summary),el('span','arrow','↗'));
    const b=el('button','feature-open');b.type='button';b.setAttribute('aria-label','View '+entry.title);b.onclick=()=>openDetails(entry);
    article.append(b); root.append(article);
  }
}
function matches(entry) {
  if(activeCategory==='remembered'&&!remembered.has(entry.id))return false;
  if(activeCategory&&activeCategory!=='remembered'&&entry.category!==activeCategory)return false;
  if($('scope').value&&entry.scope!==$('scope').value)return false;
  const tokens=$('search').value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const corpus=[entry.id,entry.title,entry.summary,entry.invocation,entry.category,entry.scope,entry.state,...entry.registeredProviders].join(' ').toLowerCase();
  return tokens.every(t=>corpus.includes(t));
}
function render() {
  if(!catalog)return;
  renderCategories();
  const entries=catalog.capabilities.filter(matches), root=$('results');root.replaceChildren();
  for(const entry of entries){const row=el('article','capability');row.dataset.capability=entry.id;
    const main=el('div','cap-main'),title=el('div','cap-title'),b=el('button','',entry.title);b.type='button';b.onclick=()=>openDetails(entry);
    title.append(b,el('code','command',entry.invocation));main.append(title,el('p','',entry.summary));
    const meta=el('div','cap-meta');meta.append(el('span','badge '+entry.state.toLowerCase(),entry.state==='Installed'?entry.scope:entry.state));
    meta.append(el('span','provider-label',entry.registeredProviders.join(' · ')+' skill'+(entry.registeredProviders.length>1?'s':'')));
    main.append(meta);row.append(el('span','cap-icon',icons[entry.category]||'+'),main,bookmark(entry));root.append(row);
  }
  $('empty').hidden=entries.length>0;$('count').textContent=entries.length+' of '+catalog.total;
  $('list-title').textContent=activeCategory==='remembered'?'Remembered':activeCategory||'All capabilities';
}
function openDetails(entry) {
  selected=entry;$('detail-category').textContent=entry.category.toUpperCase();$('detail-title').textContent=entry.title;
  $('detail-summary').textContent=entry.summary;$('detail-command').textContent=entry.invocation;$('detail-policy').textContent=entry.policy;
  const access=$('provider-access');access.replaceChildren();
  for(const p of entry.providerAccess){const tile=el('div','access');tile.append(el('strong','',p.provider),el('span','',p.status));access.append(tile);}
  const sources=$('detail-sources');sources.replaceChildren();
  for(const source of entry.sources){const s=el('div','source');s.append(el('span','small',source.provider+' · '+source.kind),el('code','',source.path));
    const copy=el('button','','Copy source path');copy.type='button';copy.onclick=()=>copyText(source.path);s.append(copy);sources.append(s);}
  $('details').showModal();
}
async function copyText(text) {
  try { if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(text);toast('Copied');return;} } catch {}
  const field=el('textarea','copy-fallback');field.value=text;field.setAttribute('aria-label','Copy text');
  const parent=document.querySelector('dialog[open]')||document.body;const previous=document.activeElement;parent.append(field);field.focus();field.select();
  let ok=false;try{ok=document.execCommand('copy');}catch{}field.remove();previous?.focus();
  toast(ok?'Copied':'Copy unavailable. Select the displayed command or path to copy it.');
}
async function refresh() {
  const b=$('refresh');b.disabled=true;b.textContent='↻ Scanning…';
  try{
    const response=await fetch('/api/capabilities',{cache:'no-store',signal:AbortSignal.timeout(15000)});
    if(!response.ok)throw new Error('The inventory service is unavailable ('+response.status+').');
    const next=await response.json();
    if(next.schemaVersion!=='ke.custom-capabilities.v1'||!Array.isArray(next.capabilities))throw new Error('The inventory response could not be read.');
    catalog=next;$('total').textContent=String(catalog.total);$('freshness').textContent='Checked '+new Date(catalog.generatedAt).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    $('coverage').textContent=catalog.sourceCount+' installed sources · '+catalog.total+' unique capabilities · Custom sources only';
    $('access-note').textContent=catalog.accessNote;$('scope-note').textContent=catalog.scopeNote;
    $('scan-roots').replaceChildren(...catalog.coverage.map(r=>el('code','',r.path+' ('+r.status+')')));
    notify(catalog.warnings.length?'Inventory is incomplete: '+catalog.warnings.join(' '):'');renderFeatures();render();
  }catch(error){notify(error.message+(catalog?' Showing the last loaded inventory; it may be stale.':' Use Refresh inventory to try again.'));
    $('freshness').textContent=catalog?'Last loaded inventory · refresh failed':'Inventory unavailable';
    if(!catalog){$('featured').replaceChildren(el('p','loading','Inventory unavailable. Refresh to retry.'));$('count').textContent='Unavailable';}
  }finally{b.disabled=false;b.textContent='↻ Refresh inventory';}
}
$('search').addEventListener('input',render);$('scope').addEventListener('change',render);$('refresh').onclick=refresh;
$('reset').onclick=()=>{activeCategory='';$('scope').value='';$('search').value='';render();$('search').focus();};
$('close-details').onclick=()=>$('details').close();$('copy-command').onclick=()=>selected&&copyText(selected.invocation);
$('access-info').onclick=()=>$('access-dialog').showModal();$('close-access').onclick=()=>$('access-dialog').close();
document.addEventListener('keydown',event=>{if(event.key==='/'&&!event.metaKey&&!event.ctrlKey&&!event.altKey&&!document.querySelector('dialog[open]')&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){event.preventDefault();$('search').focus();}});
refresh();
