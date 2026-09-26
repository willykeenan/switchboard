/* Shared, view-only rendering for exact public provider messages. */
'use strict';
window.SwitchboardMessageRenderer=(()=>{
 const node=(tag,cls='',text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
 const button=(label,fn,cls='')=>{const n=node('button',cls,label);n.type='button';n.onclick=fn;return n;};
 const md=window.markdownit({html:false,linkify:false,typographer:false,breaks:false,maxNesting:40});
 const validLink=url=>{try{return /^(https?:|mailto:)/i.test(url)&&!/[\u0000-\u001f]/.test(url);}catch{return false;}};
 md.validateLink=validLink;
 // Message Markdown never loads remote images or local paths. Attachments have a separate scoped route.
 md.renderer.rules.image=(tokens,i)=>md.utils.escapeHtml(tokens[i].content||'Image reference');
 const linkOpen=md.renderer.rules.link_open||((tokens,i,options,env,self)=>self.renderToken(tokens,i,options));
 md.renderer.rules.link_open=(tokens,i,options,env,self)=>{tokens[i].attrSet('target','_blank');tokens[i].attrSet('rel','noopener noreferrer');return linkOpen(tokens,i,options,env,self);};
 async function copy(text,control){try{await navigator.clipboard.writeText(text);control.textContent='Copied';}catch{control.textContent='Select text to copy';}setTimeout(()=>{if(control.isConnected)control.textContent='Copy';},1600);}
 function markdown(text){const body=node('div','message-body markdown-body');body.innerHTML=md.render(text||'');for(const pre of body.querySelectorAll('pre')){const shell=node('div','code-block'),toolbar=node('div','code-toolbar'),code=pre.querySelector('code'),language=[...code?.classList||[]].find(c=>c.startsWith('language-'))?.slice(9)||'Code';const btn=button('Copy',()=>copy(code?.textContent||pre.textContent,btn));toolbar.append(node('span','',language),btn);pre.before(shell);shell.append(toolbar,pre);}for(const table of body.querySelectorAll('table')){const wrap=node('div','table-scroll');wrap.tabIndex=0;wrap.setAttribute('aria-label','Scrollable table');table.before(wrap);wrap.append(table);}return body;}
 function sourceDetails(entry){const details=node('details','message-source'),summary=node('summary','','Source details');details.append(summary);let loaded=false;details.addEventListener('toggle',()=>{if(!details.open||loaded)return;loaded=true;details.append(node('p','source-note','Original public record · presentation only'));const raw=node('pre','source-text',entry.text||'');details.append(raw);for(const a of entry.attachments||[])if(a.source)details.append(node('div','source-path',a.name+' · '+a.source));});return details;}
 function enlarge(attachment,agent){
  if(window.parent!==window){window.parent.postMessage({type:'switchboard:attachment-preview',agent,attachment:{url:attachment.url,name:attachment.name}},location.origin);return;}
  const dialog=node('dialog','attachment-dialog'),head=node('div','attachment-dialog-head'),img=node('img');img.src=attachment.url;img.alt=attachment.name;head.append(node('strong','',attachment.name),button('Close',()=>dialog.close()));dialog.append(head,img);document.body.append(dialog);dialog.onclose=()=>dialog.remove();dialog.showModal();
 }
 function attachment(spec,agent){const card=node('figure','attachment-card');card.dataset.attachmentState=spec.state;const caption=node('figcaption'),label=node('strong','',spec.name||'Attachment');caption.append(label);
  if(spec.state==='Available'&&spec.kind==='image'&&typeof spec.url==='string'&&spec.url.startsWith('/api/transcript/attachment?')){
   const open=button('',()=>enlarge(spec,agent),'attachment-open');open.setAttribute('aria-label','Enlarge '+spec.name);const img=node('img');img.loading='lazy';img.decoding='async';img.width=spec.width;img.height=spec.height;img.alt=spec.name;img.src=spec.url;open.append(img,node('span','attachment-enlarge','Enlarge'));img.onerror=()=>{open.replaceChildren(node('span','attachment-fallback','Image unavailable · refresh the session to retry'));open.disabled=true;card.dataset.attachmentState='Unavailable';};card.append(open);caption.append(node('span','',Math.max(1,Math.round(spec.bytes/1024))+' KB · '+spec.width+' × '+spec.height));
  }else caption.append(node('span','attachment-fallback',spec.reason||spec.state||'Attachment unavailable'));
  card.append(caption);return card;
 }
 function message(entry,{agent,title='Agent'}={}){
  if(entry.role==='activity'){const row=node('details','message system-record');row.dataset.entryId=entry.id;row.dataset.agentId=agent;row.append(node('summary','','System activity · '+(entry.label||'Recorded event')),node('p','source-note',entry.timestamp?new Date(entry.timestamp).toLocaleString():''),markdown(entry.text),node('p','source-note',entry.sourceNotice||''));return row;}
  const row=node('article','message '+entry.role);row.dataset.entryId=entry.id;row.dataset.agentId=agent;const header=node('header','message-head'),who=node('strong','',entry.role==='user'?'You':'Agent'),time=node('time');if(entry.timestamp){const d=new Date(entry.timestamp);time.dateTime=entry.timestamp;time.textContent=d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});time.title=d.toLocaleString();}who.title=entry.role==='user'?'You':title;header.append(who,time);row.append(header);
  const text=entry.displayText??entry.text??'';
  if(text.length>30000){row.append(markdown(text.slice(0,30000)));const more=node('details','long-message');more.append(node('summary','','Read the remaining message'));let loaded=false;more.ontoggle=()=>{if(more.open&&!loaded){loaded=true;more.append(markdown(text.slice(30000)));}};row.append(more);}else row.append(markdown(text));
  if(entry.attachments?.length){const attachments=node('div','message-attachments');for(const a of entry.attachments)attachments.append(attachment(a,agent));row.append(attachments);}
  if(entry.sources){const sources=node('details','message-sources');sources.append(node('summary','','Memory sources'),node('pre','source-text',entry.sources.citations));if(entry.sources.rolloutIds)sources.append(node('pre','source-text',entry.sources.rolloutIds));row.append(sources);}
  row.append(sourceDetails(entry));return row;
 }
 function tool(entry,callNames){const row=node('details','tool-record');row.dataset.entryId=entry.id;const label=entry.toolKind==='result'?(callNames.get(entry.callId)||'Tool')+' · '+(entry.failed?'Error recorded':'Result recorded'):(entry.label||'Tool')+' · Call recorded';row.append(node('summary','',label));let loaded=false;row.ontoggle=()=>{if(row.open&&!loaded){loaded=true;row.append(node('pre','tool-body',entry.text));}};return row;}
 function refreshAttachments(row,entry,agent){for(const [i,spec]of (entry.attachments||[]).entries()){const card=row.querySelectorAll('.attachment-card')[i];if(!card)continue;if(card.dataset.attachmentState==='Unavailable'&&spec.state==='Available'){card.replaceWith(attachment(spec,agent));continue;}const img=card.querySelector('img'),open=card.querySelector('.attachment-open');if(img&&spec.url){if(img.getAttribute('src')!==spec.url)img.src=spec.url;if(open)open.onclick=()=>enlarge(spec,agent);}}}
 function replaceContent(row,entry,options,callNames){const opened=[...row.querySelectorAll('details')].map(n=>n.open),selfOpen=row.open;const replacement=entry.role==='tool'?tool(entry,callNames):message(entry,options);if(entry.role==='tool'){row.replaceWith(replacement);replacement.open=selfOpen;return replacement;}row.replaceChildren(...replacement.childNodes);[...row.querySelectorAll('details')].forEach((n,i)=>{if(opened[i])n.open=true;});return row;}

 return Object.freeze({message,tool,markdown,node,refreshAttachments,replaceContent});
})();
