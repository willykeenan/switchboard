/* Shared shell: page navigation and project context never alter workflow state. */
(() => {
  'use strict';
  if(new URLSearchParams(location.search).get('embedded')==='1')return;
  const bar = document.querySelector('.topbar');
  if (!bar || bar.dataset.sharedHeader) return;
  bar.dataset.sharedHeader = 'true';
  const workflow = /^\/constellations\/?$/.test(location.pathname);
  let project = new URLSearchParams(location.search).get('project') || 'demo';
  const nav = document.createElement('nav');
  nav.id = 'capabilities-nav'; nav.setAttribute('aria-label', 'Switchboard');
  const tabs = [['Workflow', '/constellations'], ['Live', '/live'], ['Agents', '/agents'], ['Custom capabilities', '/capabilities'], ['Library', '/library']];
  for (const [title, href] of tabs) {
    const a = document.createElement('a'); a.textContent = title; a.dataset.path = href;
    if (location.pathname === href || location.pathname === href + '/') a.setAttribute('aria-current', 'page');
    nav.append(a);
  }
  const oldNav = bar.querySelector('nav');
  const rooms = oldNav?.querySelector('a[href^="/rooms"]');
  if (oldNav) oldNav.replaceWith(nav); else bar.querySelector('.brand').after(nav);
  let actions = bar.querySelector('.top-actions');
  if (!actions) { actions = document.createElement('div'); actions.className = 'top-actions'; bar.append(actions); }
  for (const status of bar.querySelectorAll(':scope > .read-only, :scope > .local-indicator')) actions.append(status);
  if (rooms) { rooms.className = 'header-rooms'; actions.prepend(rooms); }
  const mode = bar.querySelector('.project-control .mode');
  if (mode) actions.prepend(mode);
  function updateLinks() {
    for(const a of nav.children){const q=new URLSearchParams({project});if(a.dataset.path==='/constellations'){try{const saved=JSON.parse(localStorage.getItem('switchboard.workflow-route.v1'));if(saved?.project===project&&typeof saved.lane==='string'&&/^[a-z0-9][a-z0-9_-]{0,79}$/.test(saved.lane)){q.set('lane',saved.lane);if(['owner','research','build','execution','evidence','review'].includes(saved.section))q.set('section',saved.section);}}catch{}}a.href=a.dataset.path+'?'+q;}
    const brand = bar.querySelector('a.brand');
    if (brand) brand.href = '/constellations?project=' + encodeURIComponent(project);
  }
  updateLinks();
  if (workflow) {
    const name = document.getElementById('project-name');
    new MutationObserver(() => {
      project = new URLSearchParams(location.search).get('project') || project;
      updateLinks();
    }).observe(name, { childList: true, subtree: true, characterData: true });
    return;
  }
  const control = bar.querySelector('.project-control');
  const trigger = control.querySelector('button');
  const name = control.querySelector('#project-name');
  let projects = [];
  const dialog = document.createElement('dialog'); dialog.className = 'header-project-dialog';
  dialog.setAttribute('aria-labelledby', 'header-project-title');
  const title = document.createElement('h2'); title.id = 'header-project-title'; title.textContent = 'Project context';
  const hint = document.createElement('p'); hint.textContent = 'Choose the project to return to in Workflow. Agents and custom capabilities are shared across projects.';
  const list = document.createElement('div'); list.className = 'header-project-options';
  const close = document.createElement('button'); close.textContent = 'Close'; close.onclick = () => dialog.close();
  dialog.append(title, hint, list, close); document.body.append(dialog);
  trigger.onclick = () => dialog.showModal();
  dialog.addEventListener('click', e => { if (e.target === dialog) { const b = dialog.getBoundingClientRect(); if (e.clientX < b.left || e.clientX > b.right || e.clientY < b.top || e.clientY > b.bottom) dialog.close(); } });
  function renderProjects() {
    name.textContent = projects.find(p => p.id === project)?.name || 'Choose project';
    list.replaceChildren();
    for (const p of projects) {
      const b = document.createElement('button'); b.textContent = p.name; b.setAttribute('aria-pressed', String(p.id === project));
      b.onclick = () => {
        project = p.id;
        const url = new URL(location.href); url.searchParams.set('project', project); history.replaceState(null, '', url);
        renderProjects(); updateLinks(); dialog.close();window.dispatchEvent(new CustomEvent('switchboard-project',{detail:{project}}));
      };
      list.append(b);
    }
  }
  fetch('/api/workflow', {cache: 'no-store'}).then(r => { if (!r.ok) throw new Error('Project list unavailable'); return r.json(); }).then(data => {
    projects = data.projects || [];
    if (!projects.some(p => p.id === project)) project = projects[0]?.id || '';
    renderProjects(); updateLinks();
  }).catch(() => { name.textContent = 'Projects unavailable'; const p = document.createElement('p'); p.textContent = 'The project list could not be loaded. Return to Workflow to retry.'; list.replaceChildren(p); });
})();
