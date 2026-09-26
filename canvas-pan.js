'use strict';
// Navigation only. Logical camera coordinates never change workflow placements.
window.SwitchboardCanvasPan=(()=>{
 const viewport=document.getElementById('viewport'),world=document.getElementById('world');let gesture=null,suppressUntil=0;
 if(!viewport||!world)return {get active(){return false}};
 const surface=document.createElement('div'),origin=document.createElement('div');surface.id='pan-surface';origin.id='pan-origin';viewport.insertBefore(surface,world);surface.append(origin);origin.append(world);const empty=document.getElementById('canvas-empty');if(empty)origin.append(empty);
 let ox=6000,oy=6000,width=0,height=0,adjusting=false,pendingCamera=null;
 // Styles can settle after deferred scripts in WebKit. Keep an unapplied
 // logical request until the scroll container can represent it.
 function camera(){return pendingCamera?{...pendingCamera}:{x:viewport.scrollLeft-ox,y:viewport.scrollTop-oy};}
 function bounds(x,y){const nextX=Math.max(ox,6000-x),nextY=Math.max(oy,6000-y),scale=Number(world.dataset.scale)||Number(world.style.zoom)||1;ox=nextX;oy=nextY;origin.style.left=ox+'px';origin.style.top=oy+'px';width=Math.max(width,ox+world.offsetWidth*scale+6000,ox+x+viewport.clientWidth+6000);height=Math.max(height,oy+world.offsetHeight*scale+6000,oy+y+viewport.clientHeight+6000);surface.style.width=width+'px';surface.style.height=height+'px';}
 function scrollTo(x,y){if(!Number.isFinite(x)||!Number.isFinite(y))return;pendingCamera={x,y};adjusting=true;bounds(x,y);viewport.scrollTo(ox+x,oy+y);if(Math.abs(viewport.scrollLeft-ox-x)<1&&Math.abs(viewport.scrollTop-oy-y)<1)pendingCamera=null;adjusting=false;}
 scrollTo(0,0);viewport.tabIndex=0;viewport.setAttribute('aria-label','Workflow canvas. Drag blank space or use arrow keys to pan.');
 const blank=target=>['viewport','world','lane-layer','edges','canvas-empty','pan-surface','pan-origin'].includes(target.id);
 function end(event){if(!gesture||(event?.pointerId!=null&&event.pointerId!==gesture.id))return;const previous=gesture;gesture=null;suppressUntil=previous.moved&&event?.type==='pointerup'?performance.now()+250:0;viewport.classList.remove('is-panning');if(viewport.hasPointerCapture(previous.id))viewport.releasePointerCapture(previous.id);}
 viewport.addEventListener('pointerdown',event=>{if(gesture||event.button!==0||!event.isPrimary||event.pointerType==='touch'||!blank(event.target))return;event.preventDefault();suppressUntil=0;const p=camera();gesture={id:event.pointerId,x:event.clientX,y:event.clientY,left:p.x,top:p.y,moved:false};viewport.setPointerCapture(event.pointerId);viewport.classList.add('is-panning');});
 viewport.addEventListener('pointermove',event=>{if(!gesture||event.pointerId!==gesture.id)return;if(!(event.buttons&1)){end(event);return;}const dx=event.clientX-gesture.x,dy=event.clientY-gesture.y;if(!gesture.moved&&Math.abs(dx)+Math.abs(dy)<4)return;gesture.moved=true;event.preventDefault();scrollTo(gesture.left-dx,gesture.top-dy);});
 for(const type of ['pointerup','pointercancel','lostpointercapture'])viewport.addEventListener(type,end);
 viewport.addEventListener('click',event=>{if(performance.now()<suppressUntil&&blank(event.target)){event.preventDefault();event.stopImmediatePropagation();}suppressUntil=0;},true);
 viewport.addEventListener('keydown',e=>{if(e.target!==viewport||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)||e.altKey||e.ctrlKey||e.metaKey)return;e.preventDefault();const p=camera(),step=e.shiftKey?200:60;scrollTo(p.x+(e.key==='ArrowRight'?step:e.key==='ArrowLeft'?-step:0),p.y+(e.key==='ArrowDown'?step:e.key==='ArrowUp'?-step:0));});
 viewport.addEventListener('scroll',()=>{if(adjusting)return;const p=camera();if(viewport.scrollLeft<1000||viewport.scrollTop<1000||viewport.scrollLeft+viewport.clientWidth>width-1000||viewport.scrollTop+viewport.clientHeight>height-1000)scrollTo(p.x,p.y);},{passive:true});
 function preserveCamera(){const p=camera();scrollTo(p.x,p.y);}
 const layoutObserver=new ResizeObserver(preserveCamera);layoutObserver.observe(world);layoutObserver.observe(viewport);
 window.addEventListener('load',preserveCamera,{once:true});
 window.addEventListener('blur',()=>end());document.addEventListener('visibilitychange',()=>{if(document.hidden)end();});
 return {camera,scrollTo,get active(){return !!gesture}};
})();
