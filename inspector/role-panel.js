'use strict';
(() => {
  const $=id=>document.getElementById(id), labels={tasks:'Task Board',documents:'Project Library',skills:'Skills',receipts:'Receipts',pids:'PIDs',work:'Work queue'};
  const query=new URLSearchParams(location.search), project=query.get('project'), task=query.get('task');
  let section=query.get('section')||'work', rows=[], next=null, sample=null, selected=null, detailVersion=0, generation=0, loading=false, lastDetail=null;
  const purpose={owner:'Follow outcomes, dependencies and the next responsible person.',dispatch:'Track pickup, returns and the evidence needed for the next step.',audit:'Your review obligations, candidate versions and original findings.',installer:'Your adoption queue. Inspect returned evidence to distinguish accepted source from installed readbacks.',core:'Your current changes, build results, checks and unresolved dependencies.','learning-tests':'Your test assignments, failures, coverage limits and evidence.','pid-skills':'Project procedures and recorded process results. Qualification is separate from learning.',research:'Project sources, open questions and the evidence behind decisions.',library:'Project documents, procedures, original receipts and recorded process jobs.',birds:'Your local work and evidence. Connected-site transport remains on its existing surface.','lane-doctor':'Recorded project work. Delivery health remains on the existing Lane Doctor surface.'};
  const text=(tag,value,cls)=>{const el=document.createElement(tag);el.textContent=String(value??'Unknown');if(cls)el.className=cls;return el;};
  const date=value=>Number.isFinite(value)?new Date(value*1000).toLocaleString():'Unknown';
  const workSections=new Set(['tasks','work','receipts']);
  const attention=r=>workSections.has(section)?r.lifecycle==='open'&&r.needsAttention===true:/Unavailable|Stale|STALE|UNVERIFIED|Changed since registration/.test(r.state);
  const awaiting=r=>workSections.has(section)&&r.lifecycle==='open'&&r.awaitingReturn===true;
  const say=(message,state='current')=>{$('status').textContent=message;$('status').dataset.state=state;};
  function validate(data,offset,id){
    if(!data||data.schema!=='ke.role-panel.page.v1'||data.project!==project||data.task!==task||data.section!==section||!data.member||data.member.threadId!==task||!Number.isFinite(data.sampledAt)||data.requestedOffset!==offset||typeof data.coverage!=='string'||!Array.isArray(data.records)||data.records.length>20||!Array.isArray(data.limitations)||!Array.isArray(data.members)||data.members.length<1)throw Error('Response identity or coverage is invalid');
    const seen=new Set();for(const r of data.records){if(!r||typeof r.id!=='string'||typeof r.title!=='string'||typeof r.state!=='string'||seen.has(r.id))throw Error('Invalid or duplicate record');if(workSections.has(section)&&(!['open','closed'].includes(r.lifecycle)||typeof r.needsAttention!=='boolean'||typeof r.awaitingReturn!=='boolean'))throw Error('Lifecycle information is unavailable');seen.add(r.id);}
    if(data.nextOffset!==null&&(!Number.isInteger(data.nextOffset)||data.nextOffset<=offset||data.nextOffset>99999))throw Error('Page did not advance');
    if(id&&(!data.record||data.record.id!==id||data.records.length!==1||data.records[0].id!==id))throw Error('Detail does not match the selected record');
    return data;
  }
  async function request(params){
    const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),20000);
    try {const url='/api/role-panels?'+new URLSearchParams({project,task,section,...params}),response=await fetch(url,{signal:abort.signal,cache:'no-store',credentials:'same-origin'});if(!response.ok)throw Error('Local service returned HTTP '+response.status);return await response.json();}finally{clearTimeout(timer);}
  }
  function freshness(){if(sample){const stale=Date.now()/1000-sample>120;$('freshness').textContent=(stale?'Stale sample · ':'Sampled ')+date(sample);}}
  function activeSection(){document.querySelectorAll('#sections button').forEach(b=>b.setAttribute('aria-current',b.dataset.section===section?'page':'false'));$('section-title').textContent=labels[section];}
  function render(){
    const focusedRecord=document.activeElement?.classList.contains('record')?document.activeElement.dataset.id:null;
    const value=$('search').value.toLowerCase().trim(),state=$('state').value,assignee=$('assignee').value,sort=$('sort').value;
    const visible=rows.filter(r=>(!state||r.state===state)&&(!assignee||r.owner===assignee)&&(!value||JSON.stringify([r.title,r.owner,r.summary,r.technicalId,r.id,r.recordedHash]).toLowerCase().includes(value)));
    visible.sort((a,b)=>sort==='title'?a.title.localeCompare(b.title):sort==='oldest'?(a.updatedAt||0)-(b.updatedAt||0):(b.updatedAt||0)-(a.updatedAt||0));
    $('records').replaceChildren();$('count').textContent=rows.length;$('open-count').textContent=rows.filter(awaiting).length;$('attention-count').textContent=rows.filter(attention).length;$('visible-count').textContent=visible.length+' shown';
    $('open-label').textContent=['tasks','work','receipts'].includes(section)?'Awaiting return':'Active not inferred';
    if(!visible.length){$('records').append(text('p',(rows.length||sample&&(value||state||assignee))?'No loaded records match. Clear filters, load the next page, or ask the source owner to register the exact missing source.':sample?'No records in this permitted source page. Coverage may be incomplete.':'Records are not available. This is not an empty-project result.','empty'));}
    for(const r of visible){
      const b=document.createElement('button');b.type='button';b.className='record';b.dataset.id=r.id;b.setAttribute('aria-pressed',String(r.id===selected));b.setAttribute('aria-label','Open '+r.title);
      const line=document.createElement('div');line.className='line';line.append(text('h3',r.title),text('span',r.state,'pill'+(attention(r)?' attention':'')));b.append(line,text('p',r.summary||'Open for source and recorded evidence.'),text('small',(r.owner||r.kind||'Project source')+' · '+date(r.updatedAt)));
      b.addEventListener('click',()=>openDetail(r.id,true));$('records').append(b);
    }
    $('more').hidden=next===null;$('more').setAttribute('aria-disabled',String(loading));
    if(focusedRecord){const replacement=[...document.querySelectorAll('.record')].find(b=>b.dataset.id===focusedRecord);(replacement||$('refresh')).focus({preventScroll:true});}
  }
  function setStates(){
    for(const [id,label,values] of [['assignee','All people',rows.map(r=>r.owner).filter(Boolean)],['state','All states',rows.map(r=>r.state)]]){
      const select=$(id),selectedValue=select.value,present=new Set(values);
      // Keep the user's query when its last match leaves the loaded sample.
      const options=new Set(present);if(selectedValue)options.add(selectedValue);
      select.replaceChildren(new Option(label,''),...[...options].sort().map(value=>new Option(value+(present.has(value)?'':' (no loaded matches)'),value)));
      select.value=selectedValue;
    }
  }
  function factList(parent,items){const dl=document.createElement('dl');for(const [k,v] of items){dl.append(text('dt',k),text('dd',typeof v==='object'?JSON.stringify(v):v??'Unknown'));}parent.append(dl);}
  function disclosure(parent,label,body){const d=document.createElement('details');d.dataset.key=label+'::'+[...parent.querySelectorAll('details')].filter(x=>x.firstElementChild?.textContent===label).length;d.append(text('summary',label),text('pre',typeof body==='string'?body:JSON.stringify(body,null,2)));parent.append(d);}
  function showDetail(r,focus){
    const same=$('detail').dataset.recordId===r.id,top=same?$('detail').scrollTop:0;
    const expanded=same?[...$('detail').querySelectorAll('details')].filter(d=>d.open).map(d=>d.dataset.key):[];
    const active=document.activeElement,detailFocus=same&&$('detail').contains(active)?[...$('detail').querySelectorAll('summary,button,h2')].indexOf(active):-1;
    $('detail').dataset.recordId=r.id;$('detail').replaceChildren();const head=document.createElement('div');head.className='detail-top';head.append(text('span',r.state,'pill'+(attention(r)?' attention':'')));const close=text('button','Close');close.type='button';close.onclick=()=>{const id=selected;selected=null;lastDetail=null;detailVersion++;$('detail').replaceChildren(text('p','Select a record to read its source and evidence.','empty'));render();const target=[...document.querySelectorAll('.record')].find(b=>b.dataset.id===id);(target||$('refresh')).focus({preventScroll:true});};head.append(close);$('detail').append(head,text('h2',r.title),text('p',r.summary));
    factList($('detail'),[['Owner',r.owner||'Project source'],['Updated',date(r.updatedAt)],['Next person',r.lifecycle==='closed'?'No open obligation':r.nextActor||'No next actor recorded'],...(r.dueAt?[['Due',date(r.dueAt)]]:[])]);
    if(r.progressMeaning){$('detail').append(text('p',r.progressMeaning));factList($('detail'),[['Origin',r.origin],['Transport',r.transport],['Open / total obligations',r.openObligations===undefined?'Formal Task Board record':r.openObligations+' / '+r.obligationCount]]);}
    if(r.actions?.length){$('detail').append(text('h3','Open actions'));for(const action of r.actions){factList($('detail'),[['State',action.state],['Reason',action.summary],['Origin',action.origin],['Next person',action.nextActor],['Exact obligation',action.id]]);}}
    if(r.history){$('detail').append(text('h3','Assignment history'));for(const item of r.history){const b=text('button',item.state+' · '+item.origin+' → '+item.recipient);b.className='related';b.type='button';b.onclick=async()=>{await changeSection('work');openDetail(item.id,true);};$('detail').append(b);disclosure($('detail'),'Original obligation · '+date(item.createdAt),item);}}
    if(r.detail?.task)disclosure($('detail'),'Original Task Board task and assignment',r.detail);
    if(r.detail?.text){$('detail').append(text('h3','Document preview'),text('pre',r.detail.text));}
    if(r.qualification){$('detail').append(text('h3','Qualification'));factList($('detail'),[['Recorded',r.qualification.recordedStatus],['Qualified at',r.qualification.qualifiedAt],['Source pins',r.qualification.sourcePinStatus||'Not checked in list'],['Current executable', 'Not requalified by viewing']]);$('detail').append(text('p',r.qualification.meaning));if(r.detail?.guide){$('detail').append(text('h3','Instructions'),text('pre',r.detail.guide));}if(r.detail?.procedure)factList($('detail'),[['Procedure',r.detail.procedure.id],['Input',r.detail.procedure.schemas],['Output',r.detail.procedure.outputName]]);disclosure($('detail'),'Procedure, inputs and qualification evidence',r.detail);}
    if(r.pid||r.progress){$('detail').append(text('h3','Recorded process'));factList($('detail'),[['PID / PGID',(r.pid??'Unknown')+' / '+(r.pgid??'Unknown')],['Progress',r.progress],['ETA',!Number.isFinite(r.etaSeconds)?'Unknown':r.etaSeconds+' seconds']]);$('detail').append(text('p','No liveness probe was made. A recorded PID can be historical or reused.'));disclosure($('detail'),'Job registration',r.detail);if(r.runReceipt)disclosure($('detail'),'Open original run receipt',r.runReceipt);}
    if(r.detail?.receipt){const receipt=r.detail.receipt;$('detail').append(text('h3','Bound receipt'));factList($('detail'),[['Verification',receipt.verification],['Recorded SHA',receipt.recorded?.sha256],['Observed SHA',receipt.observedSha256]]);if(receipt.reason)$('detail').append(text('p',receipt.reason));if(r.facts){$('detail').append(text('h3','Recorded outcome & evidence'));for(const [k,v] of Object.entries(r.facts)){if(['string','boolean','number'].includes(typeof v))factList($('detail'),[[k,v]]);else disclosure($('detail'),k.replace(/([A-Z])/g,' $1'),v);}}disclosure($('detail'),'Original receipt and work details (redacted)',r.detail);}
    if(Array.isArray(r.links)&&r.links.length){$('detail').append(text('h3','Related instructions & evidence'));for(const link of r.links){if(!labels[link.section]||typeof link.id!=='string')continue;const b=text('button',link.label);b.className='related';b.type='button';b.onclick=async()=>{await changeSection(link.section);openDetail(link.id,true);};$('detail').append(b);}}
    if(r.source)disclosure($('detail'),'Source path & version hashes',r.source);
    disclosure($('detail'),'Technical identity',{'recordId':r.id,'assignment':r.technicalId,'task':task,'project':project});
    [...$('detail').querySelectorAll('details')].forEach((d,i)=>d.open=expanded.includes(d.dataset.key));
    $('detail').querySelector('h2').tabIndex=-1;$('detail').scrollTop=top;if(detailFocus>=0&&!focus){$('detail').querySelectorAll('summary,button,h2')[detailFocus]?.focus({preventScroll:true});}
    if(focus){$('detail').querySelector('h2').tabIndex=-1;$('detail').querySelector('h2').focus({preventScroll:true});if(innerWidth<700)$('detail').scrollIntoView({behavior:'smooth'});}
  }
  async function openDetail(id,focus=false){
    selected=id;render();const version=++detailVersion,gen=generation;
    try{const data=validate(await request({id}),0,id);if(version!==detailVersion||gen!==generation||selected!==id)return;const changed=lastDetail?.id===id&&JSON.stringify(lastDetail)!==JSON.stringify(data.record);lastDetail=data.record;showDetail(lastDetail,focus);if(changed){const note=text('p','Selected record changed in the latest sample.');$('detail').append(note);}}catch(e){if(version!==detailVersion||gen!==generation)return;$('detail').replaceChildren(text('h2','Detail unavailable'),text('p',e.message));if(lastDetail?.id===id){$('detail').append(text('p','Previous detail is retained below; its source may have changed.'));disclosure($('detail'),'Previous accepted detail',lastDetail);}}
  }
  async function load(append=false){
    if(loading)return;
    const offset=append?next:0;if(append&&offset===null)return;
    const gen=++generation;loading=true;
    // Keep the initiating button focusable. The loading guard prevents duplicate requests.
    $('refresh').setAttribute('aria-disabled','true');$('more').setAttribute('aria-disabled','true');
    say(sample?'Refreshing the recorded source…':'Loading permitted project records…','loading');
    try{
      let data,proposed=append?[...rows]:[],cursor=offset;
      const wanted=rows.length,selection=selected,seen=new Set(proposed.map(r=>r.id));
      do {
        data=validate(await request({offset:String(cursor)}),cursor);if(gen!==generation)return;
        for(const row of data.records){if(seen.has(row.id))throw Error('Page overlaps previously loaded records; refresh to sample again');seen.add(row.id);proposed.push(row);}
        if(proposed.length>400)throw Error('Loaded-page bound reached; narrow the task view');
        cursor=data.nextOffset;if(cursor!==null&&!data.records.length)throw Error('Empty page cannot advance');
        // Re-read the loaded window atomically; follow a shifted selection within
        // the same400-row bound. Rejected later pages leave the old sample intact.
      }while(!append&&cursor!==null&&proposed.length<400&&(proposed.length<wanted||(selection&&!seen.has(selection))));
      const scroll=window.scrollY;
      rows=proposed;next=cursor;sample=data.sampledAt;$('title').textContent=data.member.label;$('purpose').textContent=purpose[data.member.view]||'Project work and recorded evidence.';$('coverage').textContent=data.coverage;$('binding').textContent='Exact task: '+task+' · workflow revision '+data.workflowRevision;$('limits').replaceChildren(...data.limitations.map(v=>text('li',v)));
      $('external').replaceChildren();if(data.member.externalSurface){const a=text('a','Open existing delivery & health surface');a.href=data.member.externalSurface;a.rel='noreferrer';$('external').append(a);}
      setStates();render();freshness();window.scrollTo(0,scroll);
      if(selected){
        if(rows.some(r=>r.id===selected)){await openDetail(selected,false);}
        else{const old=$('detail').querySelector('[data-disappearance]');if(old)old.remove();const warning=text('p',next===null?'Selected record no longer appears in the sampled scope. Previous detail is retained as history.':'Selected record is outside the400-row refresh bound. Previous detail may be out of date.');warning.dataset.disappearance='true';$('detail').prepend(warning);}
      }
      if(gen!==generation)return;
      say(data.taskCoverage?.formalError||'Source read successfully · direct read, no index. Item dates and version hashes are separate from this sample time. Search covers loaded pages.',data.taskCoverage?.formalError?'error':'current');
    }catch(e){if(gen!==generation)return;say((sample?'Showing the last successful sample. ':'Records unavailable. ')+e.message,'error');if(!sample)render();}
    finally{if(gen===generation){loading=false;$('refresh').setAttribute('aria-disabled','false');$('more').setAttribute('aria-disabled','false');}}
  }
  async function changeSection(value){generation++;detailVersion++;loading=false;section=value;rows=[];sample=null;next=null;selected=null;lastDetail=null;$('search').value='';$('state').value='';$('assignee').value='';$('detail').replaceChildren(text('p','Select a record to read its source and evidence.','empty'));$('freshness').textContent='No successful sample yet';activeSection();history.replaceState(null,'','?'+new URLSearchParams({project,task,section}));render();await load();}
  document.querySelectorAll('#sections button').forEach(b=>b.addEventListener('click',()=>{if(section!==b.dataset.section)changeSection(b.dataset.section);}));
  ['search','state','assignee','sort'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',render));$('refresh').onclick=()=>load();$('more').onclick=()=>load(true);activeSection();setInterval(freshness,1000);
  if(!project||!task||!labels[section]||[...query.keys()].some(k=>!['project','task','section'].includes(k)||query.getAll(k).length!==1)){say('Viewer binding is unavailable. Open the exact registered task URL.','error');$('refresh').disabled=true;return;}
  load();
})();
