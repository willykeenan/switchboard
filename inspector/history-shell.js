'use strict';
(()=>{
 const initial=new URLSearchParams(window.SwitchboardHistoryInitialQuery||location.search);let opened=false,currentProject=null,currentLane=null;
 const button=document.createElement('button');button.type='button';button.className='wf-history-entry';button.textContent='Work & history';button.onclick=()=>{window.WorkflowLibraryView?.hide();window.WorkflowDesk.showHistory({project:currentProject,lane:currentLane||''},'work');};document.querySelector('.canvas-actions').prepend(button);
 const openQuery=q=>{window.WorkflowLibraryView?.hide();window.WorkflowDesk.showHistory({project:q.get('project')||currentProject||'',lane:q.get('lane')||''},q.get('history')||'work');};
 window.WorkflowHistoryEntry={update(project,lane){currentProject=project;currentLane=lane;if(!opened&&initial.has('history')){opened=true;openQuery(initial);}}};
 document.addEventListener('click',e=>{const a=e.target.closest('a[href]');if(e.defaultPrevented||!a||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;const u=new URL(a.href,location.href);if(u.origin===location.origin&&u.pathname==='/constellations'&&u.searchParams.has('history')){e.preventDefault();history.pushState(null,'',u);openQuery(u.searchParams);}});
})();
