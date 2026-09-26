'use strict';
// Embedded in workflow-signals.js. Geometry only: no messages, storage or actions.
window.WorkflowRoads=(()=>{
 const EPS=.001,MAX_ROOMS=80,MAX_VISITS=20000;
 const inside=(p,r)=>p.x>r.left+EPS&&p.x<r.right-EPS&&p.y>r.top+EPS&&p.y<r.bottom-EPS;
 const inflate=(r,n)=>({left:r.left-n,right:r.right+n,top:r.top-n,bottom:r.bottom+n});
 const point=(x,y)=>({x,y});
 const rect=n=>{const r=n.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};};
 const visible=n=>!!n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden'&&getComputedStyle(n).display!=='none';
 const compact=points=>points.filter((p,i)=>!i||Math.hypot(p.x-points[i-1].x,p.y-points[i-1].y)>EPS).filter((p,i,a)=>!i||i===a.length-1||!((Math.abs(a[i-1].x-p.x)<EPS&&Math.abs(p.x-a[i+1].x)<EPS)||(Math.abs(a[i-1].y-p.y)<EPS&&Math.abs(p.y-a[i+1].y)<EPS)));
 const path=points=>points.map((p,i)=>(i?'L':'M')+p.x+','+p.y).join(' ');
 function crosses(a,b,r){return Math.abs(a.y-b.y)<EPS?a.y>r.top+EPS&&a.y<r.bottom-EPS&&Math.max(a.x,b.x)>r.left+EPS&&Math.min(a.x,b.x)<r.right-EPS:a.x>r.left+EPS&&a.x<r.right-EPS&&Math.max(a.y,b.y)>r.top+EPS&&Math.min(a.y,b.y)<r.bottom-EPS;}
 function plan(start,end,obstacles,limit){
  if(![start.x,start.y,end.x,end.y].every(Number.isFinite))return null;
  if(obstacles.some(r=>inside(start,r)||inside(end,r)))return null;
  const pad=20,extent=limit||{left:Math.min(start.x,end.x,...obstacles.map(r=>r.left))-pad,right:Math.max(start.x,end.x,...obstacles.map(r=>r.right))+pad,top:Math.min(start.y,end.y,...obstacles.map(r=>r.top))-pad,bottom:Math.max(start.y,end.y,...obstacles.map(r=>r.bottom))+pad};
  if([start,end].some(p=>p.x<extent.left-EPS||p.x>extent.right+EPS||p.y<extent.top-EPS||p.y>extent.bottom+EPS))return null;
  const xs=[...new Set([start.x,end.x,extent.left,extent.right,...obstacles.flatMap(r=>[r.left,r.right])])].filter(x=>x>=extent.left&&x<=extent.right).sort((a,b)=>a-b);
  const ys=[...new Set([start.y,end.y,extent.top,extent.bottom,...obstacles.flatMap(r=>[r.top,r.bottom])])].filter(y=>y>=extent.top&&y<=extent.bottom).sort((a,b)=>a-b);
  const width=xs.length,id=(x,y)=>y*width+x,position=n=>point(xs[n%width],ys[Math.floor(n/width)]),from=id(xs.indexOf(start.x),ys.indexOf(start.y)),to=id(xs.indexOf(end.x),ys.indexOf(end.y));
  const scores=new Map([[from,0]]),parents=new Map(),closed=new Set(),heap=[];
  const push=v=>{heap.push(v);let i=heap.length-1;while(i){const p=(i-1)>>1;if(heap[p].f<=v.f)break;heap[i]=heap[p];i=p;}heap[i]=v;};
  const pop=()=>{const first=heap[0],last=heap.pop();if(heap.length){let i=0;while(i*2+1<heap.length){let c=i*2+1;if(c+1<heap.length&&heap[c+1].f<heap[c].f)c++;if(heap[c].f>=last.f)break;heap[i]=heap[c];i=c;}heap[i]=last;}return first;};
  push({id:from,f:0});let visits=0;
  while(heap.length&&visits++<MAX_VISITS){
   const current=pop().id;if(closed.has(current))continue;
   if(current===to){const result=[];for(let n=to;n!==undefined;n=parents.get(n))result.push(position(n));return compact(result.reverse());}
   closed.add(current);const p=position(current),x=current%width,y=Math.floor(current/width);
   for(const [nx,ny]of [[x-1,y],[x+1,y],[x,y-1],[x,y+1]]){
    if(nx<0||ny<0||nx>=xs.length||ny>=ys.length)continue;
    const next=id(nx,ny),q=point(xs[nx],ys[ny]);if(closed.has(next)||obstacles.some(r=>crosses(p,q,r)))continue;
    const score=scores.get(current)+Math.abs(p.x-q.x)+Math.abs(p.y-q.y);if(score>=(scores.get(next)??Infinity)-EPS)continue;
    scores.set(next,score);parents.set(next,current);push({id:next,f:score+Math.abs(q.x-end.x)+Math.abs(q.y-end.y)});
   }
  }
  return null;
 }
 function hull(rs){return {left:Math.min(...rs.map(r=>r.left)),right:Math.max(...rs.map(r=>r.right)),top:Math.min(...rs.map(r=>r.top)),bottom:Math.max(...rs.map(r=>r.bottom))};}
 function usable(r){return r.left+EPS<r.right&&r.top+EPS<r.bottom;}
 function around(start,end,obstacles,gap){
  if(![start?.x,start?.y,end?.x,end?.y].every(Number.isFinite))return null;
  const axis=(a,b)=>Math.abs(a.x-b.x)<EPS||Math.abs(a.y-b.y)<EPS;
  const solid=obstacles.filter(r=>!inside(start,r)&&!inside(end,r));
  const walk=points=>{
   const pts=compact(points.filter(p=>p&&Number.isFinite(p.x)&&Number.isFinite(p.y)));
   if(pts.length<2)return Math.hypot(start.x-end.x,start.y-end.y)<EPS?[point(start.x,start.y)]:null;
   for(let i=1;i<pts.length;i++){
    if(!axis(pts[i-1],pts[i]))return null;
    const endLeg=pts.length>=3&&(i===1||i===pts.length-1);
    if((endLeg?solid:obstacles).some(r=>crosses(pts[i-1],pts[i],r)))return null;
   }
   return pts;
  };
  const direct=walk([start,end]);if(direct)return direct;
  const elbow=walk([start,point(start.x,end.y),end])||walk([start,point(end.x,start.y),end]);if(elbow)return elbow;
  const box=obstacles.length?hull(obstacles):{left:Math.min(start.x,end.x),right:Math.max(start.x,end.x),top:Math.min(start.y,end.y),bottom:Math.max(start.y,end.y)};
  const pad=Math.max(gap||0,1),ys=[box.top-pad,box.bottom+pad,start.y,end.y],xs=[box.left-pad,box.right+pad,start.x,end.x];
  for(const y of ys){const p=walk([start,point(start.x,y),point(end.x,y),end]);if(p)return p;}
  for(const x of xs){const p=walk([start,point(x,start.y),point(x,end.y),end]);if(p)return p;}
  const corners=[point(box.left-pad,box.top-pad),point(box.right+pad,box.top-pad),point(box.right+pad,box.bottom+pad),point(box.left-pad,box.bottom+pad)];
  for(const c of corners)for(const d of corners){
   const p=walk([start,point(start.x,c.y),c,point(d.x,c.y),d,point(d.x,end.y),end])||walk([start,point(c.x,start.y),c,point(c.x,d.y),d,point(end.x,d.y),end]);
   if(p)return p;
  }
  return null;
 }
 let cached=null,routeCache=new Map(),networkCache=null;
 function snapshot(){
  const nodes=[...document.querySelectorAll('#world .wf-bunker-room[data-room-id]')].filter(visible),rooms=nodes.slice(0,MAX_ROOMS).map(node=>{
   const r=rect(node),door=[...node.querySelectorAll('[data-room-entrance]')].find(n=>n.dataset.roomEntrance===node.dataset.roomId&&visible(n));
   return {id:node.dataset.roomId,node,rect:r,door,doorRect:door?rect(door):null,desks:[...node.querySelectorAll('[data-desk-agent]')].filter(visible).map(n=>({node:n,rect:rect(n)})),scale:(r.right-r.left)/Math.max(1,node.offsetWidth)};
  });
  const key=JSON.stringify([nodes.length,rooms.map(r=>[r.id,r.rect,r.doorRect,r.desks.map(d=>d.rect)]),innerWidth,innerHeight]);
  if(cached?.key===key&&rooms.every((r,i)=>r.node===cached.rooms[i]?.node&&r.door===cached.rooms[i]?.door&&r.desks.every((d,j)=>d.node===cached.rooms[i]?.desks[j]?.node)))return cached;
  routeCache.clear();networkCache=null;cached={key,rooms,available:nodes.length<=MAX_ROOMS,width:Math.max(2,Math.min(8,...rooms.map(r=>8*r.scale)))};return cached;
 }
 function portal(room,clearance){
  if(!room?.doorRect)return null;const r=room.rect,d=room.doorRect,c=point((d.left+d.right)/2,(d.top+d.bottom)/2);
  const side=[['bottom',Math.abs(c.y-r.bottom)],['top',Math.abs(c.y-r.top)],['left',Math.abs(c.x-r.left)],['right',Math.abs(c.x-r.right)]].sort((a,b)=>a[1]-b[1])[0][0];
  if(side==='bottom'||side==='top'){const x=Math.max(r.left+clearance,Math.min(r.right-clearance,c.x)),edge=side==='bottom'?r.bottom:r.top,sign=side==='bottom'?1:-1;return {inner:point(x,edge-sign*clearance),outer:point(x,edge+sign*clearance),side};}
  const y=Math.max(r.top+clearance,Math.min(r.bottom-clearance,c.y)),edge=side==='right'?r.right:r.left,sign=side==='right'?1:-1;return {inner:point(edge-sign*clearance,y),outer:point(edge+sign*clearance,y),side};
 }
 function roomFor(p,s){const node=p.node?.closest?.('.wf-bunker-room[data-room-id]');return s.rooms.find(r=>r.node===node);}
 function route(a,b){
  const s=snapshot();if(!s.rooms.length)return null;
  const key=JSON.stringify([a.x,a.y,b.x,b.y,a.id,b.id,a.port,b.port]);
  if(routeCache.has(key))return routeCache.get(key);
  const fail=reason=>({available:false,roads:true,reason,d:'M'+a.x+','+a.y,points:[]});
  if(!s.available)return fail('Room geometry exceeds the bounded router');
  const clearance=s.width/2+.75,source=roomFor(a,s),target=roomFor(b,s),obstacles=s.rooms.map(r=>inflate(r.rect,clearance));
  const within=(room,start,end,skip)=>plan(start,end,room.desks.filter(d=>!skip?.contains?.(d.node)&&!d.node.contains(skip)).map(d=>inflate(d.rect,clearance)),inflate(room.rect,-clearance));
  let points;
  if(source&&source===target&&inside(a,source.rect)&&inside(b,source.rect)){
   const blockers=source.desks.filter(d=>!d.node.contains(a.node)&&!d.node.contains(b.node)).map(d=>inflate(d.rect,clearance));
   points=plan(a,b,blockers,inflate(source.rect,-clearance));
  }else{
   const startRoom=source&&inside(a,source.rect)?source:null,endRoom=target&&inside(b,target.rect)?target:null;
   const startGate=startRoom?portal(startRoom,clearance):null,endGate=endRoom?portal(endRoom,clearance):null;
   if(startRoom&&!startGate||endRoom&&!endGate)return fail('A room entrance is not available');
   const first=startRoom?within(startRoom,a,startGate.inner,a.node):[point(a.x,a.y)],last=endRoom?within(endRoom,endGate.inner,b,b.node):[point(b.x,b.y)];
   const middle=first&&last?plan(startGate?.outer||a,endGate?.outer||b,obstacles):null;
   if(middle)points=compact([...first,...(startGate?[startGate.outer]:[]),...middle,...(endGate?[endGate.inner]:[]),...last]);
  }
  const result=points?{available:true,roads:true,d:path(points),points,width:s.width,sourceRoom:source?.id,targetRoom:target?.id}:fail('No clear corridor connects these recorded endpoints');
  if(routeCache.size>=100)routeCache.delete(routeCache.keys().next().value);routeCache.set(key,result);return result;
 }
 function courierRoute(a,b,footprint){
  const s=snapshot();if(!s.rooms.length)return null;
  const f=footprint,fail=(reason,detail={})=>({available:false,roads:true,body:true,reason,d:'M'+a.x+','+a.y,points:[],clearance:{footprint:f,...detail}});
  if(!s.available)return fail('Room geometry exceeds the bounded router');
  if(!f||!['left','right','top','bottom'].every(k=>Number.isFinite(f[k])&&f[k]>0))return fail('Readable courier footprint is unavailable');
  const key='body:'+JSON.stringify([a.id,b.id,a.port,b.port,f,a.x,a.y,b.x,b.y]);if(routeCache.has(key))return routeCache.get(key);
  const source=roomFor(a,s),target=roomFor(b,s);
  if(!source||!target||!a.node||!b.node)return fail('Actual room and desk anchors are required for readable courier travel');
  const measured=p=>{const r=p.node.getBoundingClientRect();return point((r.left+r.right)/2,(r.top+r.bottom)/2);},originalFrom=measured(a),originalTo=measured(b);
  const expand=r=>({left:r.left-f.right,right:r.right+f.left,top:r.top-f.bottom,bottom:r.bottom+f.top});
  const shrink=r=>({left:r.left+f.left,right:r.right-f.right,top:r.top+f.top,bottom:r.bottom-f.bottom});
  const clamp=(p,r)=>point(Math.max(r.left,Math.min(r.right,p.x)),Math.max(r.top,Math.min(r.bottom,p.y)));
  const park=(room,p)=>{const r=shrink(room.rect);return clamp(p,usable(r)?r:room.rect);};
  const start=park(source,originalFrom),end=park(target,originalTo),width=f.left+f.right,height=f.top+f.bottom,gap=Math.max(f.left,f.right,f.top,f.bottom,1);
  const blockers=(room,skip)=>room.desks.filter(d=>!skip.some(n=>d.node.contains(n))).map(d=>expand(d.rect));
  const interior=room=>{const r=shrink(room.rect);return usable(r)?r:room.rect;};
  const stitch=(from,to,block,limit)=>plan(from,to,block,limit)||around(from,to,block,gap)||around(from,to,[],gap);
  const gate=room=>{
   const basic=portal(room,0);if(!basic)return {error:'A room entrance is not available',roomId:room.id};
   const d=room.doorRect,r=room.rect,side=basic.side,horizontal=side==='top'||side==='bottom',required=horizontal?width:height,available=horizontal?d.right-d.left:d.bottom-d.top;
   const inset=shrink(r),lo=horizontal?Math.max(inset.left,d.left+f.left):Math.max(inset.top,d.top+f.top),hi=horizontal?Math.min(inset.right,d.right-f.right):Math.min(inset.bottom,d.bottom-f.bottom);
   const along=horizontal?basic.inner.x:basic.inner.y,doorLo=horizontal?d.left:d.top,doorHi=horizontal?d.right:d.bottom;
   const centre=lo<=hi+EPS?Math.max(lo,Math.min(hi,along)):Math.max(doorLo,Math.min(doorHi,along));
   const inX=x=>Math.max(r.left+EPS,Math.min(r.right-EPS,x)),inY=y=>Math.max(r.top+EPS,Math.min(r.bottom-EPS,y));
   let inner,outer;
   if(side==='bottom'){inner=point(inX(centre),inY(r.bottom-Math.max(f.bottom,1)));outer=point(centre,r.bottom+Math.max(f.top,1));}
   if(side==='top'){inner=point(inX(centre),inY(r.top+Math.max(f.top,1)));outer=point(centre,r.top-Math.max(f.bottom,1));}
   if(side==='right'){inner=point(inX(r.right-Math.max(f.right,1)),inY(centre));outer=point(r.right+Math.max(f.left,1),centre);}
   if(side==='left'){inner=point(inX(r.left+Math.max(f.left,1)),inY(centre));outer=point(r.left-Math.max(f.right,1),centre);}
   if(s.rooms.some(other=>other!==room&&inside(outer,other.rect))){
    if(side==='bottom')outer=point(outer.x,r.bottom+1);
    if(side==='top')outer=point(outer.x,r.top-1);
    if(side==='right')outer=point(r.right+1,outer.y);
    if(side==='left')outer=point(r.left-1,outer.y);
   }
   const conflicts=s.rooms.filter(other=>other!==room&&crosses(inner,outer,expand(other.rect))).map(other=>({roomId:other.id,rect:other.rect}));
   return {inner,outer,roomId:room.id,side,required,available,conflicts};
  };
  let points,sourceGate,targetGate;
  if(source===target)points=stitch(start,end,blockers(source,[a.node,b.node]),interior(source));
  else{
   sourceGate=gate(source);targetGate=gate(target);
   if(sourceGate.error||targetGate.error)return fail(sourceGate.error||targetGate.error,{sourceGate,targetGate,requiredWidth:width,requiredHeight:height});
   const first=stitch(start,sourceGate.inner,blockers(source,[a.node]),interior(source)),last=stitch(targetGate.inner,end,blockers(target,[b.node]),interior(target));
   const inflated=s.rooms.map(room=>expand(room.rect)),walls=s.rooms.map(room=>room.rect);
   const middle=first&&last?(plan(sourceGate.outer,targetGate.outer,inflated)||around(sourceGate.outer,targetGate.outer,walls,gap)):null;
   if(middle)points=compact([...first,sourceGate.outer,...middle,targetGate.inner,...last]);
  }
  const result=points?{available:true,roads:true,body:true,d:path(points),points,width:s.width,sourceRoom:source.id,targetRoom:target.id,originalFrom,originalTo,footprint:f,sourceGate,targetGate}:fail('No corridor fits the readable courier and parcel',{sourceRoom:source.id,targetRoom:target.id,requiredWidth:width,requiredHeight:height,sourceGate,targetGate});
  if(routeCache.size>=100)routeCache.delete(routeCache.keys().next().value);routeCache.set(key,result);return result;
 }
 function network(footprint=null){
  const s=snapshot(),networkKey=s.key+JSON.stringify(footprint);if(networkCache?.key===networkKey)return networkCache;
  if(!s.available||!s.rooms.length)return {key:networkKey,d:'',width:s.width,rooms:s.rooms,unavailable:!s.available};
  const clearance=s.width/2+.75,doors=s.rooms.map(r=>({room:r,gate:portal(r,clearance)})).filter(x=>x.gate),obstacles=s.rooms.map(r=>inflate(r.rect,clearance)),used=new Set(doors.length?[0]:[]),segments=[];let disconnected=0;
  while(used.size<doors.length){
   let edge=null;
   for(const i of used)for(let j=0;j<doors.length;j++)if(!used.has(j)){const a=doors[i].gate.outer,b=doors[j].gate.outer,distance=Math.abs(a.x-b.x)+Math.abs(a.y-b.y);if(!edge||distance<edge.distance)edge={i,j,distance};}
   if(!edge)break;used.add(edge.j);
   let points;if(footprint){const endpoint=item=>({id:item.room.id,port:'door',node:item.room.door,x:item.gate.outer.x,y:item.gate.outer.y}),route=courierRoute(endpoint(doors[edge.i]),endpoint(doors[edge.j]),footprint);points=route?.available?route.points:null;}else points=plan(doors[edge.i].gate.outer,doors[edge.j].gate.outer,obstacles);if(points)segments.push(path(points));else disconnected++;
  }
  return networkCache={key:networkKey,d:segments.join(' '),width:s.width,rooms:s.rooms,disconnected,doorCount:doors.length};
 }
 return {plan,route,courierRoute,network,snapshot,portal,crosses};
})();

 'use strict';
// Screen-space geometry shared by the desk and recorded-event animation.
window.WorkflowRouteGeometry=(()=>{
 const cards=()=>[...document.querySelectorAll('#lane-layer .session,#project-layer .session')];
 const rendered=n=>!!n&&!!n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden'&&getComputedStyle(n).display!=='none';
 function bounds(n){let b={left:0,top:0,right:innerWidth,bottom:innerHeight};
  for(let p=n?.parentElement;p;p=p.parentElement){const s=getComputedStyle(p);if(/auto|scroll|hidden|clip/.test(s.overflow+s.overflowX+s.overflowY)){const r=p.getBoundingClientRect();b={left:Math.max(b.left,r.left),right:Math.min(b.right,r.right),top:Math.max(b.top,r.top),bottom:Math.min(b.bottom,r.bottom)};}}
  return b;
 }
 function endpoint(id,port,options={}){
  const nodes=options.task?[...document.querySelectorAll('[data-assignment-ref]')].filter(n=>n.dataset.assignmentRef===id&&n.closest('[data-work-dock],.wf-work-dock')):[...new Set([...document.querySelectorAll('[data-character-key][data-agent-id],[data-agent-anchor]'),...cards()])].filter(n=>n.dataset.agentId===id||n.dataset.agentAnchor===id);
  const candidates=nodes.filter(n=>rendered(n)&&(!n.hasAttribute('data-port')||n.dataset.port===port)).map(n=>{const p=n.dataset.port===port?n:n.querySelector('[data-port="'+port+'"]')||n,r=p.getBoundingClientRect(),b=bounds(p),x=(r.left+r.right)/2,y=(r.top+r.bottom)/2;return {node:p,x,y,b,inside:x>=b.left&&x<=b.right&&y>=b.top&&y<=b.bottom};});
  const hit=candidates.find(n=>n.inside)||candidates[0];
  if(hit&&hit.b.right>hit.b.left&&hit.b.bottom>hit.b.top){const {x,y,b}=hit;return {...hit,x:hit.inside?x:Math.max(b.left+12,Math.min(b.right-12,x)),y:hit.inside?y:Math.max(b.top+14,Math.min(b.bottom-14,y)),offscreen:!hit.inside,id,port,task:!!options.task,label:options.label||id};}
  const v=document.getElementById('viewport')?.getBoundingClientRect();if(!v||v.width<30||v.height<30)return null;
  return {x:port==='send'?v.left+16:v.right-16,y:Math.max(20,v.top+65)+(port==='send'?0:70),offscreen:true,unlocated:true,id,port,task:!!options.task,label:options.label||id};
 }
 function route(a,b,offset=0){const road=window.WorkflowRoads.route(a,b);if(road)return road;const direction=a.x<=b.x?1:-1,dx=Math.min(180,Math.max(50,Math.abs(b.x-a.x)*.4));return {available:true,roads:false,d:`M${a.x},${a.y} C${a.x+dx*direction},${a.y+offset} ${b.x-dx*direction},${b.y+offset} ${b.x},${b.y}`};}
 function curve(a,b,offset=0){return route(a,b,offset).d;}
 function watch(fn){const v=document.getElementById('viewport'),world=document.getElementById('world');const ro=new ResizeObserver(fn);if(v)ro.observe(v);const main=document.querySelector('.main');if(main)ro.observe(main);
  const mo=new MutationObserver(ms=>{if(ms.some(m=>!m.target.closest?.('.wf-agent-ports,.wf-activity-observation,.wf-current-action,.wf-task-attachments')&&(m.type==='attributes'||[...m.addedNodes,...m.removedNodes].some(n=>n.nodeType===1&&!n.matches('.wf-activity-observation,.wf-current-action,.wf-task-attachments')))))fn();});
  if(world)mo.observe(world,{subtree:true,childList:true,attributes:true,attributeFilter:['style','hidden']});
  window.addEventListener('resize',fn);document.addEventListener('scroll',fn,true);window.addEventListener('switchboard-workflow-scope',fn);
  return ()=>{ro.disconnect();mo.disconnect();window.removeEventListener('resize',fn);document.removeEventListener('scroll',fn,true);window.removeEventListener('switchboard-workflow-scope',fn);};
 }
 return {cards,endpoint,curve,route,watch};
})();
// Read acknowledgements are durable facts; workflow revisions order permissions only.
// A response arriving later (including a receipt refresh) is not necessarily newer.
window.WorkflowReceiptState=(()=>{
 const revision=n=>Number.isSafeInteger(n)&&n>=0?n:null;
 const readTime=m=>typeof m?.read_at==='string'&&Number.isFinite(Date.parse(m.read_at))?Date.parse(m.read_at):null;
 const held=m=>m?.status==='HELD'||m?.status==='BLOCKED'||m?.permittedNow===false;
 function merge(prior,incoming,workflowRevision){
  if(!incoming||typeof incoming.id!=='string'||typeof incoming.sender!=='string'||typeof incoming.recipient!=='string')return null;
  if(prior&&(prior.sender!==incoming.sender||prior.recipient!==incoming.recipient))return prior;
  const nextRevision=revision(workflowRevision)??revision(incoming.workflowRevision),priorRevision=revision(prior?.workflowRevision);
  const older=priorRevision!==null&&nextRevision!==null&&nextRevision<priorRevision;
  const newer=nextRevision!==null&&(priorRevision===null||nextRevision>priorRevision);
  // Equal or absent versions cannot safely release a hold. An unversioned hold
  // may stop travel, but a later authoritative revision is required to release it.
  const blocked=older?held(prior):newer?held(incoming):held(prior)||held(incoming);
  const merged={...prior,...incoming,workflowRevision:older||nextRevision===null?priorRevision:nextRevision};
  const previousRead=readTime(prior),nextRead=readTime(incoming);
  merged.read_at=previousRead!==null&&(nextRead===null||previousRead>=nextRead)?prior.read_at:nextRead!==null?incoming.read_at:null;
  merged.status=blocked?'HELD':merged.read_at?'READ':held(incoming)?prior?.status:incoming.status;
  merged.permittedNow=!blocked;
  return merged;
 }
 return {merge};
})();
window.WorkflowSignals={create(ctx={}){
 const ns='http://www.w3.org/2000/svg',geometry=window.WorkflowRouteGeometry,svg=document.createElementNS(ns,'svg');svg.classList.add('wf-dispatch-signals');svg.setAttribute('aria-hidden','true');svg.style.cssText='position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:17';document.body.append(svg);
 const roadMap=document.createElementNS(ns,'svg');roadMap.classList.add('wf-corridor-map');roadMap.setAttribute('aria-hidden','true');document.body.append(roadMap);const roadMaskId='wf-roads-'+Math.random().toString(36).slice(2);let roadPaintKey=null;
 const boundaryBox=document.createElement('div');boundaryBox.className='wf-motion-boundaries';document.body.append(boundaryBox);const boundaryButtons=new Map();
 const couriers=document.createElement('div');couriers.className='wf-couriers';document.body.append(couriers);const courierNodes=new Map(),parcelNodes=new Map(),pending=new Map(),latestMessages=new Map(),receiptRequests=new Map();let omitted=0;
 const shadows=new WeakMap(),now=ctx.now||Date.now,startedAt=now(),recent=15*60*1000,lifetime=6200;
 const stamp=value=>{const n=typeof value==='number'?value:Date.parse(value);return Number.isFinite(n)?n:null;};
 const cards=geometry.cards,seen=new Map(),events=new Map(),rays=new Map(),taskItems=new Map(),portEdits=new Map();let snapshot=null,activities={},destroyed=false,raf=null,baseline=false,taskBaseline=false;
 const storage='switchboard.motion-seen.v2';try{for(const [id,at] of JSON.parse(localStorage.getItem(storage))||[])if(typeof id==='string'&&Number.isFinite(at))seen.set(id,at);}catch{}
 const make=(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
 const trailBox=make('details','wf-trails wf-motion-log'),trailSummary=make('summary'),trailList=make('div','wf-motion-list'),status=make('p','wf-motion-status');status.setAttribute('role','status');status.setAttribute('aria-live','polite');trailBox.open=false;trailBox.append(trailSummary,status,trailList);(ctx.mount||document.querySelector('.workflow-workspace'))?.append(trailBox);
 const notice=make('span','wf-motion-notice'),routeStatus=make('span','wf-motion-route-status'),queueStatus=make('span','wf-motion-queue-status');status.append(notice,routeStatus,queueStatus);
 const name=id=>snapshot?.sessions?.find(s=>s.agent_id===id)?.title||[...document.querySelectorAll('[data-agent-anchor][data-agent-id]')].find(n=>n.dataset.agentId===id)?.dataset.agentLabel||id;
 function inScope(e){const p=ctx.project?.();return !p||(typeof e.project==='string'?e.project===p:[e.sender,e.recipient].some(id=>{const s=snapshot?.placements?.find(x=>x.agentId===id);return snapshot?.lanes?.find(l=>l.id===s?.laneId)?.projectId===p;}));}
 function saveSeen(){while(seen.size>2000)seen.delete(seen.keys().next().value);try{localStorage.setItem(storage,JSON.stringify([...seen]));}catch{}}
 const maxReceiptReaders=16;
 function pruneReceipts(){
  // Keep every fact still referenced by UI or a reader. Ordinary history churn
  // may exceed 200 receipts only by these bounded live references (at most 419).
  if(latestMessages.size>200){
   const pinned=new Set(receiptRequests.keys());
   for(const source of [rays,pending,events])for(const e of source.values())if(e.message)pinned.add(e.message.id);
   for(const id of latestMessages.keys()){if(latestMessages.size<=200)break;if(!pinned.has(id))latestMessages.delete(id);}
  }
  couriers.dataset.retainedReceipts=String(latestMessages.size);
  couriers.dataset.receiptReaders=String(receiptRequests.size);
 }

 function messageEvent(m,receiptFresh=false){
  if(!m||typeof m.id!=='string'||typeof m.sender!=='string'||typeof m.recipient!=='string')return null;
  const state=m.status==='HELD'||m.status==='BLOCKED'||m.permittedNow===false?'Held':m.status==='FAILED'?'Failed':m.status==='RETRYING'?'Retry waiting':m.read_at||m.status==='READ'?'Read acknowledgement':['QUEUED','DELIVERED'].includes(m.status)?'Queued to inbox':'Unavailable';
  const raw=state==='Read acknowledgement'?m.read_at:state==='Failed'?m.failed_at||m.updated_at||m.created_at:state==='Retry waiting'?m.retry_at||m.updated_at||m.created_at:m.delivered_at||m.queued_at||m.updated_at||m.created_at,at=stamp(raw);if(at===null)return null;
  const mode=state==='Queued to inbox'?'flight':state==='Read acknowledgement'?'ack':state==='Held'?'held':state==='Failed'?'failed':state==='Retry waiting'?'retry':'unknown';
  return {key:JSON.stringify(['message',m.id,m.sender,m.recipient,state,at]),kind:'message',sender:m.sender,recipient:m.recipient,project:m.projectId,at,state,message:{...m},mode,receiptFresh,routable:mode==='flight'||mode==='ack',presentable:mode!=='unknown',replyTo:typeof (m.reply_to||m.replyTo)==='string'?(m.reply_to||m.replyTo):null,unavailable:mode==='held'?'Connection held; no delivery':mode==='failed'?'Recorded failure; no delivery':mode==='retry'?'Recorded retry wait; no new delivery':'No recorded permitted delivery'};
 }
 async function refreshReceipt(e,open=false){
  const original=latestMessages.get(e.message.id)||e.message;let request=receiptRequests.get(original.id);
  if(!request&&receiptRequests.size>=maxReceiptReaders){notice.textContent='Receipt reader busy · showing the latest recorded message';if(!destroyed&&open)ctx.openMessage?.(original);return original;}
  if(!request){latestMessages.set(original.id,original);const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);const run=(async()=>{try{const response=await fetch('/api/messages?agent='+encodeURIComponent(original.sender),{cache:'no-store',signal:controller.signal});if(!response.ok)throw Error('Receipt unavailable');const data=await response.json(),found=data.items?.find(m=>m.id===original.id&&m.sender===original.sender&&m.recipient===original.recipient);if(!found)throw Error('Receipt outside loaded history');if(!destroyed)updateMessages([found],{receiptFresh:true,workflowRevision:data.workflowRevision});return latestMessages.get(original.id)||original;}catch(error){if(!destroyed)notice.textContent='Current receipt unavailable · showing the last recorded message';return latestMessages.get(original.id)||original;}finally{clearTimeout(timeout);receiptRequests.delete(original.id);pruneReceipts();}})();request={run,controller};receiptRequests.set(original.id,request);pruneReceipts();}
  const current=await request.run;if(!destroyed&&open)ctx.openMessage?.(current);return current;
 }
 function details(e){if(e.kind==='message')return refreshReceipt(e,true);if(ctx.openWork)return ctx.openWork(e.assignmentRef,e);notice.textContent=e.assignmentRef+' · '+e.state+' · '+(e.receipt?'Receipt '+e.receipt:'Receipt unavailable');}

 function animate(e,replay=false,fromQueue=false){
  if((!e.routable&&!e.presentable)||!inScope(e)||!Number.isFinite(e.at)||e.at>now()+5000)return false;
  const key=e.key+(replay?':replay':'');for(const [k,active]of rays)if((e.message&&active.message?.id===e.message.id)||(e.kind==='work'&&active.assignmentRef===e.assignmentRef))rays.delete(k);
  if(!rays.size&&!pending.size)omitted=0;if(rays.size>=3){if(pending.size>=200){omitted++;queueStatus.textContent=omitted+' additional events remain in message history';return false;}for(const [k,p]of pending)if(e.message&&p.message?.id===e.message.id)pending.delete(k);if(replay){const rest=[...pending];pending.clear();pending.set(key,{...e,replay});for(const [k,p]of rest)pending.set(k,p);}else pending.set(key,{...e,replay});queueStatus.textContent=pending.size+' recorded events waiting for a courier';return true;}
  rays.set(key,{...e,replay,start:now(),until:now()+lifetime,refreshed:!!e.receiptFresh});notice.textContent=(replay?'Replay · ':'Recorded event · ')+e.state+' · '+new Date(e.at).toLocaleString();redraw();return true;
 }
 function renderTrails(){const rows=[...events.values()].filter(inScope).sort((a,b)=>b.at-a.at||a.key.localeCompare(b.key)).slice(0,30);trailSummary.textContent='Communications & task travel · '+rows.length;
  const focused=document.activeElement,focusKey=focused?.dataset.eventKey,action=focused?.dataset.action;trailList.replaceChildren();
  for(const e of rows){const row=make('div','wf-motion-row');row.dataset.eventKey=e.key;const title=e.kind==='message'?name(e.sender)+' → '+name(e.recipient):e.assignmentRef;const b=make('button','wf-motion-inspect',title);b.type='button';b.title=e.key+' · Original '+new Date(e.at).toISOString();b.onclick=()=>details(e);b.dataset.eventKey=e.key;b.dataset.action='inspect';
   const current=e.message?latestMessages.get(e.message.id):null,currentState=current?messageEvent(current)?.state:null;const info=make('small','',(e.replyTo?'Reply · ':'')+e.state+' · '+new Date(e.at).toLocaleString()+(currentState&&currentState!==e.state?' · Latest receipt: '+currentState:'')+(e.routable?'':' · '+e.unavailable));const replay=make('button','wf-motion-replay','Replay');replay.type='button';replay.disabled=!e.routable&&!e.presentable;if(!e.routable&&e.presentable)replay.textContent='Show status';replay.title=(e.routable||e.presentable)?'Replay recorded event from '+new Date(e.at).toLocaleString():e.unavailable;replay.setAttribute('aria-label',(e.routable?'Replay ':'Show recorded ')+e.state+' · '+title);replay.dataset.eventKey=e.key;replay.dataset.action='replay';replay.onclick=()=>animate(e,true);row.append(b,info,replay);trailList.append(row);
  }
  if(!rows.length)trailList.append(make('p','','No recorded exchanges or task events for this project.'));
  if(focusKey)[...trailList.querySelectorAll('button')].find(b=>b.dataset.eventKey===focusKey&&b.dataset.action===action)?.focus({preventScroll:true});
 }
 function remember(e,initial){events.set(e.key,e);while(events.size>200)events.delete(events.keys().next().value);if(!seen.has(e.key)){seen.set(e.key,e.at);if(!initial&&e.at>=startedAt&&e.at<=now()+5000&&now()-e.at<=recent)animate(e);}}
 function enhanceCards(){for(const [port]of portEdits)if(!port.isConnected)portEdits.delete(port);for(const card of cards()){
  for(const port of card.querySelectorAll('[data-port]')){if(portEdits.has(port)||port.querySelector('.wf-port-label'))continue;portEdits.set(port,{onclick:port.onclick,disabled:port.disabled,title:port.title,aria:port.getAttribute('aria-label')});const kind=port.dataset.port;let label={inbox:'Inbox',send:'Send',results:'Results'}[kind]||kind;
   if(port.disabled&&['send','results'].includes(kind)){label=kind==='send'?'Chat':'Work history';port.disabled=false;port.title=kind==='send'?'Open this conversation in the right panel':'Open recorded work and history';port.onclick=e=>{e.stopPropagation();if(kind==='send')window.WorkflowDesk?.select(card.dataset.agentId);else{const seat=snapshot?.placements?.find(s=>s.agentId===card.dataset.agentId);window.WorkflowDesk?.showHistory({project:ctx.project?.(),lane:seat?.laneId});}};}
   if(port.textContent.trim()!==label)port.append(make('span','wf-port-label',label));port.setAttribute('aria-label',label+' · '+name(card.dataset.agentId));
  }
 }}
 function paintActivity(){
  for(const node of cards()){
   const activity=activities[node.dataset.agentId],at=stamp(activity?.observedAt),expires=stamp(activity?.expiresAt),validTime=at!==null&&at<=now()+5000;
   const supported=['starting','thinking','working','responding','tools','command','editing','searching','compacting','cancelling'].includes(activity?.phase);
   const active=supported&&validTime&&activity?.active===true&&activity.fresh===true&&activity.stale===false&&activity.turnStatus==='open'&&expires!==null&&expires>now()&&now()-at<=60000;
   const terminal=validTime&&['finished','stopped','failed'].includes(activity?.turnStatus)&&activity?.active===false;
   const phase=active?'RUNNING':terminal?'TERMINAL':'UNAVAILABLE';node.dataset.observedWork=phase;
   if(!shadows.has(node))shadows.set(node,node.style.boxShadow);
   const shadow=active?'0 0 0 2px #9ad4ad,0 0 18px #8cdb9950':terminal?'0 0 0 1px #9eacbb':shadows.get(node);if(node.style.boxShadow!==shadow)node.style.boxShadow=shadow;
   node.classList.toggle('wf-observed-running',active);node.classList.toggle('wf-observed-terminal',terminal);
   const taskWorking=active&&typeof activity.taskId==='string'&&typeof activity.assignmentId==='string';node.classList.toggle('wf-task-working',taskWorking);if(taskWorking){node.dataset.taskId=activity.taskId;node.dataset.taskAssignment=activity.assignmentId;}else{delete node.dataset.taskId;delete node.dataset.taskAssignment;}
   let badge=node.querySelector(':scope > .wf-activity-observation');if(!badge){badge=document.createElement('span');badge.className='wf-activity-observation';badge.style.cssText='display:block;font-size:9px;color:#aaceb3;margin-top:5px;line-height:1.4';node.append(badge);}
   const label=active?(activity.label||'Working'):terminal?'Turn '+activity.turnStatus:'Activity unavailable';
   const title=active?'Fresh provider activity, independent of queued messages.':terminal?'Recorded turn state; this does not prove assignment completion.':'No fresh running evidence. A queued or read message is not running activity.';
   if(badge.textContent!==label)badge.textContent=label;badge.title=title;
   let action=node.querySelector(':scope > .wf-current-action');if(!action){action=document.createElement('div');action.className='wf-current-action';node.append(action);}const actionAt=stamp(activity?.lastActionAt);const actionText=activity?.lastAction&&actionAt!==null?'Last action · '+activity.lastAction:'';if(action.textContent!==actionText)action.textContent=actionText;action.hidden=!actionText;action.title=actionText?(activity.source||'Observed public activity')+' · '+new Date(actionAt).toLocaleString():'';
  }
 }

 function sn(tag,attrs={}){const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,String(v));return n;}
 function endpointLabel(g,p,color,key){g.append(sn('circle',{cx:p.x,cy:p.y,r:6,fill:'#162333',stroke:color,'stroke-width':2}));if(!p.offscreen)return;
  let button=boundaryButtons.get(key);if(!button){button=make('button','wf-motion-boundary');button.type='button';boundaryBox.append(button);boundaryButtons.set(key,button);}button.dataset.active='true';
  const text=(p.unlocated?'Outside this view · ':'Offscreen · ')+p.label;button.textContent=text;button.title=text+' · '+p.id;button.setAttribute('aria-label','Inspect '+text);button.onclick=()=>{if(p.task){if(ctx.openWork)ctx.openWork(p.id);else notice.textContent='Task '+p.id+' · Details are in the Work dock';return;}window.WorkflowDesk?.select(p.id);};
  const v=document.getElementById('viewport')?.getBoundingClientRect(),bounds=p.b||v||{left:0,right:innerWidth};const width=Math.min(290,Math.max(130,bounds.right-bounds.left-10));button.style.width=width+'px';button.style.left=Math.max(bounds.left+4,Math.min(bounds.right-width-4,p.x-width/2))+'px';button.style.top=Math.max(5,p.y-43)+'px';button.style.borderColor=color;
 }

 const keaArt='<svg class="wf-kea-art" viewBox="0 0 80 72" aria-hidden="true"><g class="wf-kea-facing"><path d="M27 43 L5 58 L14 37Z" fill="#436f48" stroke="#18342f" stroke-width="2"/><ellipse cx="35" cy="39" rx="22" ry="18" fill="#83a865" stroke="#223f35" stroke-width="2"/><g class="wf-kea-wing"><path d="M41 34 C25 17 12 18 5 25 L22 45 Q34 51 44 39Z" fill="#db8544" stroke="#294937" stroke-width="2"/><path d="M40 34 Q20 20 7 25 L25 40Z" fill="#557b48"/><path d="M24 29 L14 29 M30 34 L19 34" stroke="#a6bc72" stroke-width="2"/></g><path d="M43 39 Q49 27 47 16 C47 4 65 4 69 15 L67 35 Q58 44 43 39Z" fill="#8cab68" stroke="#223f35" stroke-width="2"/><path d="M67 17 Q80 19 75 36 Q72 25 65 26Z" fill="#b3c1ad" stroke="#2b3a35" stroke-width="2"/><circle cx="61" cy="17" r="4" fill="#e6c867"/><circle cx="62" cy="17" r="2.2" fill="#102a27"/><circle cx="63" cy="16" r=".8" fill="white"/><path d="M29 52 L28 61 L21 63 M41 54 L43 61 L49 63" stroke="#d6b87a" stroke-width="3" stroke-linecap="round" fill="none"/><path d="M31 32 L48 51" stroke="#423d2b" stroke-width="3"/></g></svg>';
 let measuredFootprint=null,activeBodyKey=null;
 function courierFootprint(){
  if(measuredFootprint)return measuredFootprint;
  const probe=make('button','wf-courier'),box=make('div','wf-message-parcel');probe.innerHTML=keaArt;probe.append(make('span','wf-courier-state'));probe.style.cssText='left:0;top:0;visibility:hidden;pointer-events:none';box.style.cssText='left:0;top:0;visibility:hidden;pointer-events:none';couriers.append(probe,box);
  const extents={left:0,right:0,top:0,bottom:0};
  const collect=n=>{const r=n.getBoundingClientRect();extents.left=Math.max(extents.left,-r.left);extents.right=Math.max(extents.right,r.right);extents.top=Math.max(extents.top,-r.top);extents.bottom=Math.max(extents.bottom,r.bottom);};
  for(const facing of [-1,1]){probe.style.setProperty('--kea-facing',String(facing));for(let angle=-14;angle<=19;angle++)for(const scale of [.85,1.08]){probe.querySelector('.wf-kea-wing').style.transform='rotate('+angle+'deg) scaleY('+scale+')';for(const node of [probe,...probe.querySelectorAll('.wf-kea-art,.wf-kea-art *,.wf-courier-state')])collect(node);}}
  const parcel=box.getBoundingClientRect();extents.left=Math.max(extents.left,12+parcel.width/2);extents.right=Math.max(extents.right,12+parcel.width/2);extents.top=Math.max(extents.top,parcel.height/2-5);extents.bottom=Math.max(extents.bottom,parcel.height/2+5);
  probe.remove();box.remove();measuredFootprint=Object.fromEntries(Object.entries(extents).map(([key,value])=>[key,Math.ceil(value+2)]));couriers.dataset.footprint=JSON.stringify(measuredFootprint);return measuredFootprint;
 }
 function courier(e,point,phase,facing,stage,bodyRoad=false){
  const key=e.kind==='message'?'message:'+e.message.id:e.key;let node=courierNodes.get(key);if(!node){node=make('button','wf-courier');node.type='button';node.innerHTML=keaArt;node.append(make('span','wf-courier-caption'),make('span','wf-courier-state'));couriers.append(node);courierNodes.set(key,node);}
  node.dataset.active='true';node.dataset.roadRoute=String(bodyRoad);node.dataset.courierPhase=stage;node.dataset.messageId=e.message?.id||'';node.dataset.eventKey=e.key;node.dataset.phase=phase;node.dataset.replay=String(e.replay);node.dataset.deliveryState=e.state;node.dataset.replyTo=e.replyTo||'';node.style.left=point.x+'px';node.style.top=point.y+'px';node.style.setProperty('--kea-facing',String(facing));node.classList.toggle('wf-kea-flying',phase==='travelling');node.classList.toggle('wf-kea-held',['Held','Failed'].includes(e.state));
  const label=stage==='pickup'?'Picking up message parcel':stage==='handoff'?'Handing parcel to inbox':phase==='sending'?'Queued · departing':phase==='travelling'?(e.replyTo?'Carrying reply':'Carrying message'):e.state==='Queued to inbox'?'In inbox · waiting for read':e.state==='Read acknowledgement'?'Read / acknowledged':e.state==='Held'?'Held · no delivery':e.state==='Failed'?'Failed · no delivery':e.state==='Retry waiting'?'Retry waiting · no delivery':e.state;
  node.querySelector('.wf-courier-caption').textContent=(e.replay?'REPLAY · ':'')+label;const caption=node.querySelector('.wf-courier-caption'),width=caption.getBoundingClientRect().width;caption.style.left='calc(50% + '+(Math.max(width/2+8,Math.min(innerWidth-width/2-8,point.x))-point.x)+'px)';node.querySelector('.wf-courier-state').textContent=e.state==='Held'?'Ⅱ':e.state==='Failed'?'!':e.state==='Read acknowledgement'?'✓':e.state==='Retry waiting'?'…':'✉';
  node.title=name(e.sender)+' → '+name(e.recipient)+'\n'+(e.message?.id||e.assignmentRef)+'\n'+label+' · Original '+new Date(e.at).toISOString()+'\nInspect the original message and latest receipt. Inbox arrival does not start a model turn.';
  node.setAttribute('aria-label','Kea courier · '+(e.replay?'Replay · ':'')+label+' · '+name(e.sender)+' to '+name(e.recipient));node.onclick=()=>details(e);return node;
 }

 function parcel(e,position,stage,unclamped=false){let node=parcelNodes.get(e.message.id);if(!node){node=make('button','wf-message-parcel');node.type='button';node.innerHTML='<svg viewBox="0 0 34 25" aria-hidden="true"><rect x="1" y="1" width="32" height="23" rx="4" fill="#f7e9c1" stroke="#826d3e" stroke-width="2"/><path d="M2 3 L17 15 L32 3" stroke="#9c8146" fill="none" stroke-width="2"/></svg>';couriers.append(node);parcelNodes.set(e.message.id,node);}node.dataset.active='true';node.dataset.messageId=e.message.id;node.dataset.parcelPhase=stage;node.dataset.replay=String(e.replay);node.style.left=(unclamped?position.x:Math.max(20,Math.min(innerWidth-20,position.x)))+'px';node.style.top=(unclamped?position.y:Math.max(16,Math.min(innerHeight-16,position.y)))+'px';node.title=e.message.id+' · '+e.state+' · '+name(e.sender)+' → '+name(e.recipient);node.setAttribute('aria-label','Inspect message parcel '+e.message.id+' · '+e.state);node.onclick=()=>details(e);return node;}
 function paintRoads(){const net=window.WorkflowRoads.network(),v=document.getElementById('viewport')?.getBoundingClientRect(),key=net.key+JSON.stringify(v?.toJSON());if(key===roadPaintKey)return;roadPaintKey=key;roadMap.replaceChildren();roadMap.dataset.rooms=String(net.rooms.length);roadMap.dataset.disconnected=String(net.disconnected||0);if(!v||!net.d)return;const mask=sn('mask',{id:roadMaskId,maskUnits:'userSpaceOnUse',x:0,y:0,width:innerWidth,height:innerHeight});mask.append(sn('rect',{x:v.left,y:v.top,width:v.width,height:v.height,fill:'white'}));for(const room of net.rooms)for(const r of [room.rect].filter(Boolean))mask.append(sn('rect',{x:r.left,y:r.top,width:r.right-r.left,height:r.bottom-r.top,fill:'black'}));const defs=sn('defs');defs.append(mask);const group=sn('g',{mask:'url(#'+roadMaskId+')'});group.append(sn('path',{d:net.d,class:'wf-corridor-bed','stroke-width':net.width}),sn('path',{d:net.d,class:'wf-corridor-centre','stroke-width':Math.max(.6,net.width/8)}));roadMap.append(defs,group);}
 function draw(){raf=null;if(destroyed)return;paintRoads();paintActivity();enhanceCards();svg.replaceChildren();for(const b of boundaryButtons.values())delete b.dataset.active;for(const n of courierNodes.values())delete n.dataset.active;for(const n of parcelNodes.values())delete n.dataset.active;const time=now(),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;let moving=false,claimedBodyKey=null,trafficWaiting=0;const occupied=[],routeWarnings=[],clearanceBlocks=[];
  for(const [key,e]of [...rays].sort((a,b)=>a[0]===activeBodyKey?-1:b[0]===activeBodyKey?1:0)){if(e.trafficPausedAt!=null){const paused=time-e.trafficPausedAt;e.start+=paused;e.until+=paused;e.trafficPausedAt=time;}const focused=courierNodes.get(e.message?'message:'+e.message.id:e.key)===document.activeElement||!!e.message&&parcelNodes.get(e.message.id)===document.activeElement;if((e.until<=time&&!focused)||!inScope(e)){rays.delete(key);continue;}if(document.body.classList.contains('library-view'))continue;
   const a=geometry.endpoint(e.sender,'send',{task:e.sourceTask,label:e.sourceTask?e.assignmentRef:name(e.sender)}),b=geometry.endpoint(e.recipient,e.targetPort||'inbox',{task:e.targetTask,label:e.targetTask?e.assignmentRef:name(e.recipient)});if(!a||!b)continue;
   const stationary=e.kind==='message'&&e.mode!=='flight',elapsed=time-e.start,progress=stationary?(e.mode==='ack'?1:0):reduced?1:Math.max(0,Math.min(1,(elapsed-650)/3300)),arrived=progress===1,phase=stationary?(e.mode==='ack'?'arrived':e.mode):arrived?'arrived':progress>0?'travelling':'sending';
   const color=['Held','Failed'].includes(e.state)?'#f3ad95':e.state==='Read acknowledgement'?'#a7d5b8':'#efd096',g=sn('g');g.dataset.eventKey=e.key;g.dataset.messageId=e.message?.id||'';g.dataset.assignmentRef=e.assignmentRef||'';g.dataset.signalState=e.state;g.dataset.sender=e.sender;g.dataset.recipient=e.recipient;g.dataset.replay=String(e.replay);g.dataset.phase=phase;g.dataset.motion=stationary?'stationary':'flight';
   const peers=[...rays.values()].filter(r=>r.sender===e.sender&&r.recipient===e.recipient&&r.mode==='flight'),position=peers.indexOf(e),arc=position<0?0:(position-(peers.length-1)/2)*140;
   let roadRoute=stationary?{available:true,roads:false,d:'M'+a.x+','+a.y+' L'+b.x+','+b.y}:geometry.route(a,b,arc);if(!stationary&&e.kind==='message'&&roadRoute.roads){const bodyPlan=window.WorkflowRoads.courierRoute(a,b,courierFootprint());if(bodyPlan&&bodyPlan.available)roadRoute=bodyPlan;}if(!roadRoute.available){delete e.trafficPausedAt;routeWarnings.push(name(e.sender)+' → '+name(e.recipient)+' · '+roadRoute.reason);if(roadRoute.clearance)clearanceBlocks.push({messageId:e.message?.id,sender:e.sender,recipient:e.recipient,reason:roadRoute.reason,...roadRoute.clearance});endpointLabel(svg,a,color,key+':from');endpointLabel(svg,b,color,key+':to');continue;}if(roadRoute.body){if(claimedBodyKey!==null){e.trafficPausedAt=time;trafficWaiting++;continue;}claimedBodyKey=key;delete e.trafficPausedAt;}g.dataset.roadRoute=String(roadRoute.roads);const path=sn('path',{d:roadRoute.d,fill:'none',stroke:color,'stroke-width':Math.min(2.5,roadRoute.width||2.5),'stroke-opacity':stationary?0:.7});if(!stationary)path.classList.add('wf-dispatch-ray');g.append(path);svg.append(g);if(!stationary||progress===0)endpointLabel(g,a,color,key+':from');if(!stationary||progress===1)endpointLabel(g,b,color,key+':to');
   const length=path.getTotalLength(),point=path.getPointAtLength(length*progress),ahead=path.getPointAtLength(Math.min(length,length*progress+2));const stage=stationary?e.mode:reduced?'waiting':elapsed<650?'pickup':elapsed<3950?'carrying':elapsed<4850?'handoff':'waiting';
   if(e.kind==='message'){const clamp=p=>({x:Math.max(48,Math.min(innerWidth-48,p.x)),y:Math.max(83,Math.min(innerHeight-112,p.y))});const landing=e.kind==='message'&&!stationary?Math.pow(progress,8):0;let bird=roadRoute.body?{x:point.x,y:point.y}:clamp({x:point.x-45*landing-(stage==='pickup'?50*(1-Math.min(1,elapsed/650)):0),y:point.y-30*landing-(stage==='pickup'?30*(1-Math.min(1,elapsed/650)):0)});if(!roadRoute.body&&(phase!=='travelling'||roadRoute.roads)){for(const [dx,dy]of [[0,0],[92,0],[-92,0],[0,-104],[0,104]]){const candidate=clamp({x:bird.x+dx,y:bird.y+dy});if(!occupied.some(q=>Math.abs(q.x-candidate.x)<86&&Math.abs(q.y-candidate.y)<98)){bird=candidate;break;}}}occupied.push(bird);if(Math.hypot(point.x-bird.x,point.y-bird.y)>2)g.append(sn('path',{d:`M${point.x},${point.y} L${bird.x},${bird.y}`,stroke:color,'stroke-width':1,'stroke-dasharray':'3 3'}));const facing=ahead.x===point.x?(b.x>=a.x?1:-1):(ahead.x>=point.x?1:-1);courier(e,bird,phase,facing,stage,!!roadRoute.body);const sourcePoint=roadRoute.originalFrom||a,targetPoint=roadRoute.originalTo||b,held={x:bird.x+12*facing,y:bird.y+5};let parcelPoint=held;if(stage==='pickup'){const t=Math.min(1,elapsed/650);parcelPoint={x:sourcePoint.x+(held.x-sourcePoint.x)*t,y:sourcePoint.y+(held.y-sourcePoint.y)*t};}else if(stage==='handoff'){const t=Math.min(1,(elapsed-3950)/900);parcelPoint={x:held.x+(targetPoint.x-held.x)*t,y:held.y+(targetPoint.y-held.y)*t};}else if(stage==='waiting'||stage==='ack')parcelPoint=targetPoint;else if(['held','failed','retry'].includes(stage))parcelPoint=sourcePoint;parcel(e,parcelPoint,stage,!!roadRoute.body);if((stage==='waiting'||stage==='ack')&&!e.refreshed&&!e.replay){e.refreshed=true;refreshReceipt(e);}}
   else{g.append(sn('circle',{cx:point.x,cy:point.y,r:11,fill:'#162333',stroke:color,'stroke-width':3}));const packet=sn('text',{x:point.x,y:point.y+4,'text-anchor':'middle',fill:color,'font-size':13});packet.textContent='◆';g.append(packet);}
   const label=(e.replay?'REPLAY · ':'')+(e.kind==='message'?'Recorded '+e.state:arrived?e.state:phase)+' · '+new Date(e.at).toLocaleTimeString(),lx=Math.max(140,Math.min(innerWidth-140,point.x)),ly=Math.max(70,point.y-(e.kind==='message'?61:19));const text=sn('text',{x:lx,y:ly,'text-anchor':'middle',fill:color,'font-size':11});text.textContent=label;g.append(text);const title=sn('title');title.textContent=e.key+' · Original '+new Date(e.at).toISOString()+'. Visual travel is not proof of model execution or task acceptance.';g.append(title);
   if(!stationary&&elapsed<4850&&!reduced)moving=true;
  }
  for(const [key,b]of boundaryButtons)if(!b.dataset.active){if(document.activeElement===b)trailSummary.focus();b.remove();boundaryButtons.delete(key);}for(const [key,n]of courierNodes)if(!n.dataset.active){if(document.activeElement===n)trailSummary.focus();n.remove();courierNodes.delete(key);}
  for(const [key,n]of parcelNodes)if(!n.dataset.active){if(document.activeElement===n)trailSummary.focus();n.remove();parcelNodes.delete(key);}
  while(rays.size<3&&pending.size){const [key,e]=pending.entries().next().value;pending.delete(key);if(inScope(e)&&(e.replay||now()-e.at<=recent))animate(e,e.replay,true);}
  pruneReceipts();svg.dataset.visibleRays=String(svg.querySelectorAll('g').length);couriers.dataset.omittedEvents=String(omitted);couriers.dataset.pendingEvents=String(pending.size);activeBodyKey=claimedBodyKey;couriers.dataset.trafficWaiting=String(trafficWaiting);couriers.dataset.clearanceBlocks=JSON.stringify(clearanceBlocks);trailSummary.textContent=trailSummary.textContent.replace(/ · [0-9]+ visual route[s]? unavailable$/,'')+(routeWarnings.length?' · '+routeWarnings.length+' visual route'+(routeWarnings.length===1?'':'s')+' unavailable':'');trailSummary.dataset.unavailableRoutes=String(routeWarnings.length);routeStatus.textContent=[...new Set(routeWarnings)].map(reason=>'Route unavailable · '+reason).join(' | ');queueStatus.textContent=[trafficWaiting?trafficWaiting+' courier'+(trafficWaiting===1?'':'s')+' waiting for clear travel':'',pending.size?pending.size+' recorded events waiting for a courier':'',omitted?omitted+' additional events remain in message history':''].filter(Boolean).join(' · ');if(moving)redraw();
 }
 function redraw(){if(!destroyed&&raf===null)raf=requestAnimationFrame(draw);}
 function updateMessages(messages,options=false){const meta=typeof options==='boolean'?{receiptFresh:options}:options||{},current=[];for(const incoming of messages||[]){const prior=latestMessages.get(incoming?.id),m=window.WorkflowReceiptState.merge(prior,incoming,meta.workflowRevision),e=messageEvent(m,meta.receiptFresh===true);if(!e)continue;const priorEvent=prior&&messageEvent(prior),changed=!!priorEvent&&priorEvent.key!==e.key,wasVisible=[...rays.values()].some(r=>r.message?.id===m.id);latestMessages.set(m.id,m);current.push({...m});
  if(changed||!e.presentable){for(const [key,active]of rays)if(active.message?.id===m.id)rays.delete(key);for(const [key,waiting]of pending)if(waiting.message?.id===m.id)pending.delete(key);}
  remember(e,!baseline);if(changed&&wasVisible&&e.presentable&&(!e.routable||e.mode==='ack'))animate(e); // Permission changes can retain an earlier read timestamp.
  pruneReceipts();
 }saveSeen();renderTrails();redraw();return current;}
 function updateWork(dock){if(!dock||!Array.isArray(dock.events)||typeof dock.project!=='string'||!dock.project)return;
  for(const [key,item] of taskItems)if(item.project===dock.project)taskItems.delete(key);
  for(const item of dock.items||[])if(item.assignmentRef&&(!item.project||item.project===dock.project))taskItems.set(dock.project+':'+item.assignmentRef,{...item,project:dock.project});
  for(const e of dock.events){if(e.project!==dock.project)continue;if(!e.id||!e.assignmentRef||!e.receipt)continue;if(['reviewed','installed','accepted'].includes(e.kind)&&({reviewed:'REVIEWED',installed:'INSTALLED',accepted:'ACCEPTED'})[e.kind]!==e.state)continue;const at=typeof e.occurredAt==='number'?e.occurredAt*1000:stamp(e.occurredAt);if(at===null||!Number.isFinite(at))continue;
   const state=(e.kind==='launch-claimed'&&e.state==='STARTING'?'Claimed · execution not confirmed':e.kind==='admitted'&&e.state==='RESERVED'?'Ready':({reviewed:'Reviewed',installed:'Installed',accepted:'Accepted'})[e.kind])||({RUNNING:'Running',READY_FOR_REVIEW:'Returned · awaiting review',FAILED:'Failed',CANCELLED:'Cancelled',UNCERTAIN:'Uncertain'})[e.state]||'State unavailable';
   // parent/runId identify authority/runtime, not an exact claiming session.
   const claim=e.kind==='launch-claimed'&&e.state==='STARTING',recipient=claim?e.claimantAgentId:e.recipientAgentId,sender=claim?e.assignmentRef:e.senderAgentId;
   const valid=claim||(['reviewed','installed','accepted'].includes(e.kind)&&({reviewed:'REVIEWED',installed:'INSTALLED',accepted:'ACCEPTED'})[e.kind]===e.state)||e.state==='READY_FOR_REVIEW';
   const known=id=>snapshot?.sessions?.some(s=>s.agent_id===id);const routable=valid&&typeof sender==='string'&&typeof recipient==='string'&&(claim||known(sender))&&known(recipient);
   remember({key:'work:'+e.id,kind:'work',project:e.project,assignmentRef:e.assignmentRef,receipt:e.receipt,at,state,sender,recipient,sourceTask:claim,targetPort:claim?'inbox':'results',routable,unavailable:'Exact route endpoints not recorded'},!taskBaseline);
  }
  taskBaseline=true;saveSeen();renderTasks();renderTrails();redraw();
 }
 function renderTasks(){for(const card of cards()){const items=[...taskItems.values()].filter(t=>t.claimantAgentId===card.dataset.agentId&&(!ctx.project?.()||t.project===ctx.project()));let root=card.querySelector(':scope > .wf-task-attachments');if(!items.length){root?.remove();continue;}if(!root){root=make('div','wf-task-attachments');card.append(root);}const contentKey=JSON.stringify(items);if(root.dataset.contentKey===contentKey)continue;const expanded=new Set([...root.querySelectorAll('details[open]')].map(d=>d.dataset.taskRef));root.dataset.contentKey=contentKey;root.replaceChildren();for(const t of items){const d=make('details'),summary=make('summary','',(t.objective||t.assignmentRef)+' · '+t.state),body=make('p','',t.assignmentRef);d.dataset.taskRef=t.assignmentRef;d.open=expanded.has(t.assignmentRef);d.append(summary,body);d.addEventListener('click',e=>e.stopPropagation());if(t.currentAction)d.append(make('p','',t.currentAction));if(Number.isFinite(t.progress?.completed)&&Number.isFinite(t.progress?.total)&&t.progress.total>0)d.append(make('p','',t.progress.completed+' / '+t.progress.total));for(const key of ['requirementIds','inputs','dependencies','resultArtifacts'])if(t[key]?.length)d.append(make('p','',key+': '+JSON.stringify(t[key])));root.append(d);}}}
 function update(next){snapshot=next;activities=Object.fromEntries((next.sessions||[]).map(s=>[s.agent_id,s.activity]));updateMessages(next.requests||[],{workflowRevision:next.revision});baseline=true;if(next.workDock)updateWork(next.workDock);renderTasks();redraw();}
 function updateActivities(next){activities={...activities,...next};redraw();}
 const unwatch=geometry.watch(()=>{renderTasks();redraw();}),timer=setInterval(redraw,1000);let observedProject=ctx.project?.();const scopeChange=()=>{const project=ctx.project?.();if(project!==observedProject){rays.clear();pending.clear();omitted=0;notice.textContent='';observedProject=project;}renderTrails();renderTasks();redraw();};window.addEventListener('switchboard-workflow-scope',scopeChange);
 function destroy(){destroyed=true;clearInterval(timer);if(raf!==null)cancelAnimationFrame(raf);unwatch();window.removeEventListener('switchboard-workflow-scope',scopeChange);svg.remove();roadMap.remove();boundaryBox.remove();couriers.remove();pending.clear();for(const request of receiptRequests.values())request.controller.abort();receiptRequests.clear();latestMessages.clear();events.clear();rays.clear();trailBox.remove();for(const [port,original]of portEdits){port.querySelector('.wf-port-label')?.remove();port.onclick=original.onclick;port.disabled=original.disabled;port.title=original.title;if(original.aria===null)port.removeAttribute('aria-label');else port.setAttribute('aria-label',original.aria);}for(const n of cards()){if(shadows.has(n))n.style.boxShadow=shadows.get(n);n.classList.remove('wf-observed-running','wf-observed-terminal','wf-task-working');delete n.dataset.observedWork;delete n.dataset.taskId;delete n.dataset.taskAssignment;n.querySelector(':scope > .wf-activity-observation')?.remove();n.querySelector(':scope > .wf-current-action')?.remove();n.querySelector(':scope > .wf-task-attachments')?.remove();}}
 return {update,updateMessages,updateActivities,updateWork,redraw,destroy,replayMessage(m){const e=messageEvent(m);if(!e||!e.presentable)return false;remember(e,true);saveSeen();renderTrails();return animate(e,true);}};
}};
