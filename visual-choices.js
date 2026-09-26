'use strict';
// Visible, keyboard-operable choices. Selection remains independent of filtering.
class VisualChoices extends HTMLElement {
 constructor(){super();this.items=[];this.selected=new Set();this.query='';this.limit=40;this.config={};this._disabled=false;}
 connectedCallback(){if(!this.list)this.build();}
 build(){
  this.classList.add('visual-choices');
  this.search=document.createElement('input');this.search.type='search';this.search.className='choice-search';
  this.search.addEventListener('input',()=>this.filter(this.search.value));
  this.summary=document.createElement('div');this.summary.className='choice-summary';this.summary.setAttribute('aria-live','polite');
  this.list=document.createElement('div');this.list.className='choice-grid';
  this.more=document.createElement('button');this.more.type='button';this.more.className='choice-more';this.more.textContent='Show more';
  this.more.onclick=()=>{this.limit+=40;this.render()};
  this.append(this.search,this.summary,this.list,this.more);this.render();
 }
 setChoices(items,value,config={}){
  this.items=items.map(x=>({...x,value:String(x.value)}));this.config={...this.config,...config};
  const values=this.config.multiple?(value||[]):[value??this.items[0]?.value];
  this.selected=new Set(values.map(String).filter(v=>this.items.some(x=>x.value===v)));
  this.query='';this.limit=40;if(!this.list)this.build();this.search.value='';this.render();return this;
 }
 get value(){return [...this.selected][0]??'';}
 set value(value){this.selected=new Set(this.items.some(x=>x.value===String(value))?[String(value)]:[]);if(this.list)this.render();}
 get selectedOptions(){return this.items.filter(x=>this.selected.has(x.value));}
 get disabled(){return this._disabled;}
 set disabled(value){this._disabled=!!value;if(this.list)this.render();}
 filter(query){this.query=query.toLowerCase().trim();this.limit=40;this.render();}
 choose(item){
  if(this.disabled||item.disabled)return;
  if(this.config.multiple){if(this.selected.has(item.value))this.selected.delete(item.value);else this.selected.add(item.value)}
  else this.selected=new Set([item.value]);
  this.paint();this.dispatchEvent(new Event('change',{bubbles:true}));
 }
 paint(){
  const selected=this.items.filter(x=>this.selected.has(x.value));
  this.summary.textContent=selected.length?'Selected: '+selected.map(x=>x.label).join(' · '):(this.config.multiple?'No dependencies selected':'Choose an option');
  this.summary.hidden=!!this.config.compact;
  const buttons=[...this.list.querySelectorAll('.choice-card')];
  const stop=buttons.find(b=>this.selected.has(b.dataset.value)&&!b.disabled)||buttons.find(b=>!b.disabled);
  for(const b of buttons){const yes=this.selected.has(b.dataset.value);b.setAttribute('aria-checked',String(yes));b.classList.toggle('chosen',yes);b.querySelector('.choice-check').textContent=yes?'✓':'+';b.tabIndex=this.config.multiple||b===stop?0:-1;}
 }
 render(){
  if(!this.list)return;
  const focus=this.contains(document.activeElement)?document.activeElement.dataset.value:undefined;
  const label=this.getAttribute('aria-label')||this.config.label||'Choices';
  this.search.placeholder=this.config.searchPlaceholder||'Find an option…';this.search.setAttribute('aria-label','Search '+label.toLowerCase());
  this.search.hidden=!this.config.searchable;this.search.disabled=this.disabled;
  this.classList.toggle('compact-choices',!!this.config.compact);this.classList.toggle('list-choices',!!this.config.list);
  this.list.setAttribute('role',this.config.multiple?'group':'radiogroup');this.list.setAttribute('aria-label',label);
  const rows=this.items.filter(x=>(x.label+' '+(x.detail||'')+' '+x.value).toLowerCase().includes(this.query));
  this.list.replaceChildren();
  rows.slice(0,this.limit).forEach(item=>{
   const b=document.createElement('button');b.type='button';b.className='choice-card';b.dataset.value=item.value;b.disabled=this.disabled||!!item.disabled;
   b.setAttribute('role',this.config.multiple?'checkbox':'radio');b.setAttribute('aria-label',item.label);b.title=item.label+(item.detail?'\n'+item.detail:'');
   if(item.tone)b.style.setProperty('--choice-color','var(--'+item.tone+')');
   const check=document.createElement('span');check.className='choice-check';check.setAttribute('aria-hidden','true');
   const copy=document.createElement('span');copy.className='choice-copy';const title=document.createElement('strong');title.textContent=item.label;copy.append(title);
   if(item.detail){const detail=document.createElement('small');detail.textContent=item.detail;copy.append(detail)}
   if(item.badge){const badge=document.createElement('span');badge.className='choice-badge';badge.textContent=item.badge;copy.append(badge)}
   b.append(check,copy);b.onclick=()=>this.choose(item);
   b.onkeydown=e=>{
    if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key))return;
    e.preventDefault();const options=[...this.list.querySelectorAll('.choice-card:not(:disabled)')],i=options.indexOf(b);
    const index=e.key==='Home'?0:e.key==='End'?options.length-1:(i+(['ArrowLeft','ArrowUp'].includes(e.key)?-1:1)+options.length)%options.length;
    options[index]?.focus();if(!this.config.multiple)options[index]?.click();
   };
   this.list.append(b);
  });
  if(!rows.length){const empty=document.createElement('p');empty.className='choice-empty';empty.textContent=this.query?'No matching choices. Your selection is kept.':this.config.empty||'No choices available.';this.list.append(empty)}
  this.more.hidden=rows.length<=this.limit;this.more.disabled=this.disabled;this.more.textContent='Show more · '+Math.min(this.limit,rows.length)+' of '+rows.length;
  this.paint();if(focus!==undefined)[...this.list.querySelectorAll('.choice-card')].find(b=>b.dataset.value===focus)?.focus({preventScroll:true});
 }
}
customElements.define('visual-choices',VisualChoices);
function choices(id,items,value,config={}){return document.getElementById(id).setChoices(items,value,config)}
