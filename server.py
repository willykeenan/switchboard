#!/usr/bin/env python3
"""Loopback-only human/agent UI plus monitor and dispatcher loop."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class BoardHTTPServer(ThreadingHTTPServer):
    # A canvas load requests several scripts and styles alongside live polling.
    # The stdlib backlog of five can reset those simultaneous local connections.
    request_queue_size = 64
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import board_core as board
import coordination
from room_reader import RoomStore
import room_reader
import workspace
import workflow
import library_services
from library_service_view import LibraryServiceView
from taskflow import TaskFlow
import taskflow_runtime
from settings_identity import LocalControlSettings
from settings_http import SettingsRoutes
from cycle_identity import CycleRoutes, CycleGrants
from daemon_duties import DutyRunner
from inspector import Inspector
import custom_capabilities
from agents.http import AgentsRoutes
import agents.http as agents_http


class LocalServices:
    """Stand-in for the removed production runner; settings still need a root and flow."""

    def __init__(self, root, flow):
        self.root = Path(root)
        self.path = Path(root) / "runtime" / "managed.sqlite3"
        self.flow = flow


HOST = os.environ.get("SWITCHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("SWITCHBOARD_PORT", "47834"))
SCAN_SECONDS = float(os.environ.get("SWITCHBOARD_SCAN_SECONDS", "5"))
TOKEN_PATH = board.ROOT / "runtime" / "control-token"
ASSETS = {name: (Path(__file__).parent / name).read_bytes() for name in ['workflow-motion.css','workflow-signals.js','workflow-communications.js','canvas-pan.js','canvas-pan.css','workflow-structure.js','workflow-structure.css','rooms.html', 'rooms.css', 'rooms.js', 'workspaces.js', 'constellations.html', 'constellations.css', 'constellations.js','agent-settings.js','agent-settings.css','visual-choices.js','visual-choices.css','activity.js','activity.css','taskflow.js','taskflow.css']}
from store import rooms_root
_rooms_root = rooms_root(board.ROOT)
_rooms_root.mkdir(parents=True, exist_ok=True)
ROOMS = RoomStore(_rooms_root)
WORKSPACE = workspace.Workspace(board.ROOT, ROOMS)
WORKFLOW = workflow.Workflow(board.ROOT, WORKSPACE)
LIBRARY_SERVICES = LibraryServiceView(WORKFLOW)
SERVICES = LocalServices(board.ROOT, WORKFLOW)
TASKFLOW = TaskFlow(board.ROOT, WORKFLOW)
WORKFLOW.taskflow = TASKFLOW
CYCLE_ROUTES = CycleRoutes(TASKFLOW,CycleGrants(board.ROOT/'runtime/cycle-identity/grants.json'))
SETTINGS_SERVICE = LocalControlSettings(SERVICES,lambda:TOKEN)
SETTINGS_ROUTES = SettingsRoutes(SETTINGS_SERVICE,lambda:TOKEN,SETTINGS_SERVICE.identity.resolve)
DUTIES = DutyRunner({
    'taskflow':lambda:taskflow_runtime.tick(TASKFLOW),
    'audit':TASKFLOW.audit.dispatch,
    'delivery':TASKFLOW.delivery.dispatch,
    'monitors':lambda:board.scan_monitors(actor='board-daemon'),
    'legacy-dispatch':lambda:board.dispatch_open(actor='board-daemon'),
    'coordination':coordination.tick,
    'workflow-mirror':WORKFLOW.mirror_pending,
},interval=SCAN_SECONDS)
AGENTS_ROUTES = AgentsRoutes()
INSPECTOR = Inspector(board.ROOT, WORKFLOW, lambda:TOKEN)
LOADED_SOURCE = {Path(p).name:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in [__file__,board.__file__,coordination.__file__,room_reader.__file__]}
LOADED_SOURCE.update({name:hashlib.sha256(body).hexdigest() for name,body in ASSETS.items()})
LOADED_SOURCE['workspace.py'] = hashlib.sha256(Path(workspace.__file__).read_bytes()).hexdigest()
LOADED_SOURCE['workflow_layout.py'] = hashlib.sha256((Path(__file__).parent / 'workflow_layout.py').read_bytes()).hexdigest()
LOADED_SOURCE['activity.py'] = hashlib.sha256((Path(__file__).parent / 'activity.py').read_bytes()).hexdigest()
LOADED_SOURCE['workflow.py'] = hashlib.sha256(Path(workflow.__file__).read_bytes()).hexdigest()
for name in ('library_services.py', 'library_service_view.py'):
    LOADED_SOURCE[name] = hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
import current_context, maintenance
for module in [current_context, maintenance]:
    LOADED_SOURCE[Path(module.__file__).name] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
LOADED_SOURCE['daemon_duties.py']=hashlib.sha256((Path(__file__).parent/'daemon_duties.py').read_bytes()).hexdigest()
for name in ('taskflow.py','taskflow_runtime.py','taskflowctl.py','taskflow_intake.py','taskflow_cooperative.py','taskflow_recovery.py','taskflow_delivery.py','taskflow_annotations.py','audit_team.py','company_workflow.py','agent_settings.py','settings_http.py','settings_identity.py','taskflow_cycle.py','cycle_identity.py','execution_admission.py','taskflow_retention.py','taskflow_intake_owner.py','taskflow_learning.py','taskflow_decisions.py','taskflow_judgment.py','taskflow_learning_library.py'):
    candidate = Path(__file__).parent / name
    if candidate.is_file():
        LOADED_SOURCE[name]=hashlib.sha256(candidate.read_bytes()).hexdigest()
LOADED_SOURCE.update(custom_capabilities.source_hashes())
LOADED_SOURCE.update(INSPECTOR.hashes())
LOADED_SOURCE['librarianctl.py']=hashlib.sha256((Path(__file__).parent/'librarianctl.py').read_bytes()).hexdigest()
for agent_asset in Path(agents_http.__file__).parent.rglob("*"):
    if agent_asset.is_file() and agent_asset.suffix in (".py", ".mjs", ".js", ".css", ".html") and "__pycache__" not in agent_asset.parts:
        LOADED_SOURCE["agents/"+str(agent_asset.relative_to(Path(agents_http.__file__).parent))]=hashlib.sha256(agent_asset.read_bytes()).hexdigest()

from runtime_source import snapshot as runtime_source_snapshot
RUNTIME_SOURCE = runtime_source_snapshot(Path(__file__).parent, generate=False)
LOADED_SOURCE.update({name: sha for name, sha in RUNTIME_SOURCE['files'].items()})


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switchboard</title>
<style>
:root{color-scheme:dark;--bg:#0b0d10;--panel:#15191f;--panel2:#1b2028;--line:#2b3340;--text:#f4f6f8;--muted:#9aa6b2;--blue:#70a7ff;--green:#58d68d;--amber:#ffbf5c;--red:#ff6b6b;--violet:#b590ff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#17233a 0,transparent 34%),var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:3;background:rgba(11,13,16,.88);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);padding:20px 28px}
h1{font-size:21px;margin:0 0 4px}h2{font-size:15px;margin:0}.sub,.muted{color:var(--muted)}
main{max-width:1480px;margin:auto;padding:22px 28px 60px}.metrics{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:12px;margin-bottom:18px}
.metric,.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;box-shadow:0 14px 40px rgba(0,0,0,.18)}
.metric{padding:14px 16px}.metric b{display:block;font-size:24px;margin-top:4px}.grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px}.panel{padding:16px;margin-bottom:16px}
.incident{border:1px solid var(--line);border-left:4px solid var(--amber);border-radius:11px;padding:14px;margin-top:10px;background:#11151b}.incident.critical{border-left-color:var(--red)}.incident.resolved{border-left-color:var(--green);opacity:.68}.incident.human{border-left-color:var(--violet)}
.row{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}.title{font-weight:720;font-size:15px}.tag{display:inline-block;padding:3px 8px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font-size:11px;margin-right:5px}.status{color:var(--amber)}.status.ACKNOWLEDGED{color:var(--blue)}.status.RESOLVED{color:var(--green)}.status.BLOCKED_HUMAN{color:var(--violet)}
.detail{margin:8px 0;color:#d7dde5}.action{padding:9px 11px;background:#0d2035;border:1px solid #24466d;border-radius:8px;color:#cfe3ff}.agents{display:grid;gap:9px}.agent{padding:10px;border:1px solid var(--line);border-radius:9px;background:#11151b}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:var(--muted);margin-right:6px}.dot.ACTIVE{background:var(--green)}.dot.IDLE{background:var(--amber)}
form{display:grid;gap:9px;margin-top:12px}input,select,textarea,button{font:inherit;border-radius:8px;border:1px solid var(--line);background:#0f1318;color:var(--text);padding:9px}textarea{min-height:72px;resize:vertical}button{cursor:pointer;background:#1c4a82;border-color:#376da9;font-weight:650}button.secondary{background:#1b2028}.events{max-height:520px;overflow:auto}.event{padding:9px 0;border-bottom:1px solid var(--line)}.empty{color:var(--muted);padding:20px 0}
@media(max-width:850px){.grid{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}header,main{padding-left:16px;padding-right:16px}}
</style>
</head>
<body>
<header><h1>Switchboard</h1><div class="sub">Local coordination board for Claude Code and Codex. Loopback only. <a href="/rooms" style="color:#b4d68a;margin-left:18px">Read agent rooms ↗</a> · <a href="/constellations" style="color:#b4d68a;margin-left:12px">Workflow map ↗</a></div></header>
<main>
  <section class="metrics">
    <div class="metric"><span class="muted">Needs attention</span><b id="m-open">—</b></div>
    <div class="metric"><span class="muted">Acknowledged</span><b id="m-working">—</b></div>
    <div class="metric"><span class="muted">Needs operator</span><b id="m-human">—</b></div>
    <div class="metric"><span class="muted">Registered agents</span><b id="m-agents">—</b></div>
  </section>
  <section class="panel"><h2>Task coordination</h2><div id="coordination"></div><div id="tasks"></div></section>
  <div class="grid">
    <div>
      <section class="panel"><div class="row"><h2>Incidents</h2><span class="muted" id="updated"></span></div><div id="incidents"></div></section>
      <section class="panel"><h2>Recent activity</h2><div class="events" id="events"></div></section>
    </div>
    <div>
      <section class="panel"><h2>Team agents</h2><div class="agents" id="agents"></div></section>
      <section class="panel">
        <h2>Ping a team</h2>
        <form id="ping-form">
          <select id="team" required></select>
          <input id="title" placeholder="What broke?" required>
          <textarea id="details" placeholder="What happened, in plain words?" required></textarea>
          <input id="action" placeholder="Safe next action" required>
          <input id="capability" placeholder="Needed skill (for example: local-repair)" value="general">
          <select id="severity"><option value="warning">Important</option><option value="critical">Critical</option><option value="info">Information</option></select>
          <label><input id="needs-operator" type="checkbox"> This requires the operator</label>
          <button type="submit">Ping one correct owner</button>
          <span class="muted" id="form-result"></span>
        </form>
      </section>
    </div>
  </div>
</main>
<script>
let token="";
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function humanStatus(s){return {OPEN:"Needs an owner",ASSIGNED:"Owner pinged",ACKNOWLEDGED:"Acknowledged",BLOCKED_HUMAN:"Needs operator",RESOLVED:"Reported resolved"}[s]||s}
async function load(){
  const r=await fetch("/api/snapshot",{cache:"no-store"}); const d=await r.json(); token=d.control_token||token;
  document.getElementById("m-open").textContent=d.counts.open;
  document.getElementById("m-working").textContent=d.counts.working;
  document.getElementById("m-human").textContent=d.counts.needs_operator;
  document.getElementById("m-agents").textContent=d.agent_total;
  document.getElementById("updated").textContent="Updated "+new Date(d.generated_at).toLocaleTimeString();
  const h=d.coordination;
  document.getElementById("coordination").textContent=(h.ok?"Service healthy":"Service needs attention")+" · Last check "+h.last_tick_age_seconds+"s ago · Delivery alerts "+((h.outbox.ESCALATED||0)+(h.handoffs.ESCALATED||0))+" · "+d.view_note;
  document.getElementById("tasks").innerHTML=d.tasks.map(t=>'<article class="incident"><div class="row"><b>'+esc(t.objective)+'</b><span class="tag">'+esc(t.status)+'</span></div><div>Owner: '+esc(t.owner)+'</div><div>Next: '+esc(t.next_action)+'</div></article>').join("")||'<div class="empty">Existing project owners are preserved. New task contracts appear here as owners register them.</div>';
  const incidents=d.incidents.map(i=>{
    const cls=(i.severity==="critical"?" critical":"")+(i.status==="RESOLVED"?" resolved":"")+(i.status==="BLOCKED_HUMAN"?" human":"");
    return '<article class="incident'+cls+'"><div class="row"><div><span class="tag">'+esc(i.team)+'</span><span class="tag">'+esc(i.incident_id)+'</span></div><b class="status '+esc(i.status)+'">'+esc(humanStatus(i.status))+'</b></div><div class="title">'+esc(i.title)+'</div><div class="detail">'+esc(i.details)+'</div><div class="action"><b>Next:</b> '+esc(i.safe_action)+'</div><div class="muted" style="margin-top:8px">Owner: '+esc(i.assigned_agent_id||"not assigned yet")+' · Linked repeat reports: '+esc(i.linked_reports||0)+' · Wake: '+esc(i.wake_status||"not attempted")+'</div></article>'
  }).join("");
  document.getElementById("incidents").innerHTML=incidents||'<div class="empty">Nothing needs attention.</div>';
  document.getElementById("agents").innerHTML=d.agents.map(a=>'<div class="agent"><div><span class="dot '+esc(a.status)+'"></span><b>'+esc(a.display_name)+'</b></div><div class="muted">'+esc(a.team)+' · '+esc(a.provider)+' · '+esc(a.status)+'</div><div>'+a.capabilities.map(x=>'<span class="tag">'+esc(x)+'</span>').join("")+'</div></div>').join("")||'<div class="empty">No agents have checked in.</div>';
  document.getElementById("events").innerHTML=d.events.map(e=>'<div class="event"><div><b>'+esc(e.event_type.replaceAll("_"," "))+'</b> <span class="tag">'+esc(e.team||"SYSTEM")+'</span></div><div class="muted">'+new Date(e.created_at).toLocaleString()+' · '+esc(e.actor)+'</div></div>').join("");
  const sel=document.getElementById("team"); const old=sel.value; sel.innerHTML=d.teams.map(t=>'<option value="'+esc(t.name)+'">'+esc(t.display_name)+'</option>').join(""); if(old)sel.value=old;
}
document.getElementById("ping-form").addEventListener("submit",async e=>{
  e.preventDefault(); const body={team:team.value,title:title.value,details:details.value,safe_action:action.value,required_capability:capability.value,severity:severity.value,needs_operator:document.getElementById('needs-operator').checked};
  const r=await fetch("/api/ping",{method:"POST",headers:{"Content-Type":"application/json","X-KE-Board-Token":token},body:JSON.stringify(body)});
  const d=await r.json(); document.getElementById("form-result").textContent=r.ok?"Ping recorded and routed.":(d.error||"Could not ping."); if(r.ok){title.value="";details.value="";action.value="";await load();}
});
load().catch(console.error); setInterval(()=>load().catch(console.error),2500);
</script>
</body>
</html>
"""


def control_token() -> str:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if TOKEN_PATH.is_file():
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    descriptor = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return token


TOKEN = control_token()


class Handler(BaseHTTPRequestHandler):
    server_version = "KEAgentBoard/2"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if SETTINGS_ROUTES.handle(self,'GET'):return
        if INSPECTOR.get(self):return
        if custom_capabilities.route(self):return
        if AGENTS_ROUTES.handle_get(self):return
        path = urlparse(self.path).path
        if path in ('/api/taskflow','/api/taskflow/task','/api/taskflow/artifact'):
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'):
                self.send_json({'error':'Loopback host required'},403);return
            try:
                q=parse_qs(urlparse(self.path).query)
                if path.endswith('/artifact'):
                    task=TASKFLOW.detail(q.get('id',[''])[0]);sha=q.get('sha',[''])[0]
                    files=[*task['task'].get('inputs',[]),*(task['task'].get('result') or {}).get('artifacts',[])]
                    files += [f for a in task['assignments'] for f in (a.get('result') or {}).get('artifacts',[])]
                    files += [f for n in task['annotations']['notes'] for f in n['evidence']]
                    artifact=next((f for f in files if f['sha256']==sha),None)
                    if not artifact:raise ValueError('Artifact not attached to this task')
                    p=Path(artifact['path'])
                    if p.resolve()!=p or not p.is_file() or p.stat().st_size>8_000_000:raise ValueError('Artifact unavailable')
                    body=p.read_bytes()
                    if hashlib.sha256(body).hexdigest()!=sha:raise ValueError('Artifact changed after submission')
                    self.send_response(200);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'none'");self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
                if path=='/api/taskflow' and q.get('view')==['world']:
                    from world_snapshot import tasks as world_tasks
                    self.send_json(world_tasks(TASKFLOW,q.get('project',[None])[0],q.get('lane',[None])[0]))
                else:self.send_json(TASKFLOW.detail(q.get('id',[''])[0]) if path.endswith('/task') else TASKFLOW.snapshot(q.get('project',[None])[0],q.get('lane',[None])[0]))
            except (ValueError,workspace.Conflict) as exc:self.send_json({'error':str(exc)},409)
            except Exception as exc:self.send_json({'error':'Task Board unavailable: '+str(exc)},503)
            return
        if path == '/api/library/services':
            if not INSPECTOR.allowed(self):
                self.send_json({'error': 'Loopback access required'}, 403); return
            try:
                query = parse_qs(urlparse(self.path).query)
                self.send_json(LIBRARY_SERVICES.snapshot(*(query.get(k, [''])[0] for k in ('project','lane','team'))))
            except ValueError as exc:
                self.send_json({'error': str(exc)}, 400)
            except Exception:
                self.send_json({'error': 'Library staffing is temporarily unavailable'}, 503)
            return
        if path in ('/api/workflow','/api/workflow/activity'):
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                self.send_json({'error':'Loopback host required'},HTTPStatus.FORBIDDEN);return
            try:
                query=parse_qs(urlparse(self.path).query)
                if path=='/api/workflow' and query.get('view')==['world']:
                    from world_snapshot import structure as world_structure
                    payload=world_structure(WORKFLOW)
                else:payload=WORKFLOW.activity_snapshot(query.get('agent')) if path.endswith('/activity') else WORKFLOW.snapshot()
                if not path.endswith('/activity'):payload['controlToken']=TOKEN
                self.send_json(payload)
            except Exception as exc:self.send_json({'error':'Workflow unavailable: '+str(exc)},HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path in ('/workflow-motion.css','/workflow-signals.js','/workflow-communications.js','/canvas-pan.js','/canvas-pan.css','/workflow-structure.js','/workflow-structure.css','/constellations','/constellations/','/constellations.css','/constellations.js','/agent-settings.js','/agent-settings.css','/visual-choices.js','/visual-choices.css','/activity.js','/activity.css','/taskflow.js','/taskflow.css'):
            name='constellations.html' if path in ('/constellations','/constellations/') else path[1:]
            body=ASSETS[name];self.send_response(200)
            self.send_header('Content-Type',{'html':'text/html','css':'text/css','js':'text/javascript'}[name.rsplit('.',1)[1]]+'; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        if path in ('/api/workspace', '/api/workspace/messages'):
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                self.send_json({'error':'Loopback host required'}, HTTPStatus.FORBIDDEN); return
            try:
                params = parse_qs(urlparse(self.path).query)
                get = lambda key, default='': params.get(key, [default])[0]
                if path == '/api/workspace':
                    payload = WORKSPACE.snapshot()
                    payload['controlToken'] = TOKEN
                else:
                    payload = WORKSPACE.feed(project=get('project'), lane=get('lane'), agent=get('agent'), room=get('room','global'), q=get('q'), author=get('author'), kind=get('kind'), before=int(get('before','0')), limit=int(get('limit','30')))
                self.send_json(payload)
            except (ValueError, KeyError, TypeError) as exc:
                self.send_json({'error':str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception:
                self.send_json({'error':'Workspace unavailable'}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path in ('/rooms', '/rooms/', '/rooms.css', '/rooms.js', '/workspaces.js'):
            name = 'rooms.html' if path in ('/rooms', '/rooms/') else path[1:]
            body = ASSETS[name]
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'text/javascript; charset=utf-8' if name.endswith('.js') else {'rooms.html':'text/html; charset=utf-8','rooms.css':'text/css; charset=utf-8'}[name])
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ('/api/rooms', '/api/rooms/messages'):
            try:
                params = parse_qs(urlparse(self.path).query)
                get = lambda key, default='': params.get(key, [default])[0]
                payload = ROOMS.catalog() if path == '/api/rooms' else ROOMS.query(
                    get('room', 'global'), q=get('q'), author=get('author'), kind=get('kind'),
                    before=int(get('before', '0')), limit=int(get('limit', '30')), message=int(get('message', '0')))
                self.send_json(payload)
            except ValueError as exc:
                self.send_json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
            except OSError:
                self.send_json({'error': 'Room history is unavailable'}, HTTPStatus.NOT_FOUND)
            return
        if path == "/":
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/snapshot":
            payload = coordination.dashboard()
            payload["control_token"] = TOKEN
            self.send_json(payload)
            return
        if path == "/api/brief":
            params=parse_qs(urlparse(self.path).query)
            endpoint=params.get('endpoint',[''])[0]
            if not endpoint:
                self.send_json({'error':'endpoint required'},HTTPStatus.BAD_REQUEST);return
            self.send_json(coordination.brief(endpoint,limit=8));return
        if path == "/health":
            payload=coordination.health()
            payload['daemon']=DUTIES.snapshot()
            payload['ok']=payload['ok'] and payload['daemon']['ok']
            payload["loaded_source_sha256"]=LOADED_SOURCE
            payload["runtime_source_provenance"]=RUNTIME_SOURCE
            self.send_json(payload,HTTPStatus.OK if payload['ok'] else HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if SETTINGS_ROUTES.handle(self,'POST'):return
        if CYCLE_ROUTES.handle(self,'POST'):return
        if INSPECTOR.post(self):return
        if urlparse(self.path).path == '/api/taskflow':
            origin='http://'+self.headers.get('Host','')
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}') or self.headers.get('Origin')!=origin or not secrets.compare_digest(self.headers.get('X-KE-Board-Token',''),TOKEN):
                self.send_json({'error':'Same-origin control token required'},403);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=150000:raise ValueError('Invalid request length')
                self.send_json(TASKFLOW.mutate(json.loads(self.rfile.read(length))))
            except workspace.Conflict as exc:self.send_json({'error':str(exc)},409)
            except (ValueError,KeyError,TypeError) as exc:self.send_json({'error':str(exc)},400)
            except Exception as exc:self.send_json({'error':'Task change uncertain; refresh before retrying: '+str(exc)},503)
            return
        if urlparse(self.path).path == '/api/workflow':
            expected_origin='http://'+self.headers.get('Host','')
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}') or self.headers.get('Origin')!=expected_origin or not secrets.compare_digest(self.headers.get('X-KE-Board-Token',''),TOKEN):
                self.send_json({'error':'Same-origin control token required'},HTTPStatus.FORBIDDEN);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=100000:raise ValueError('Invalid request length')
                result=WORKFLOW.mutate(json.loads(self.rfile.read(length)))
                self.send_json({'ok':True,'revision':result['revision']})
            except workspace.Conflict as exc:self.send_json({'error':str(exc)},HTTPStatus.CONFLICT)
            except (ValueError,KeyError,TypeError) as exc:self.send_json({'error':str(exc)},HTTPStatus.BAD_REQUEST)
            except Exception:self.send_json({'error':'Save failed. Your last saved layout is preserved.'},HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if urlparse(self.path).path == '/api/workspace':
            expected_origin = 'http://' + self.headers.get('Host','')
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}') or self.headers.get('Origin') != expected_origin or not secrets.compare_digest(self.headers.get('X-KE-Board-Token',''), TOKEN):
                self.send_json({'error':'Same-origin control token required'}, HTTPStatus.FORBIDDEN); return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 100000:
                    raise ValueError('Request size must be 1–100000 bytes')
                payload = json.loads(self.rfile.read(length))
                if workflow.manual_routing(board.ROOT) and payload.get('operation') in ('lane','member','unlink-member','task','dependency'):
                    raise workspace.Conflict('The operator controls session assignments and connections in the Switchboard app.')
                WORKSPACE.mutate(payload, actor='operator-ui')
                self.send_json({'ok':True,'revision':WORKSPACE.read()['revision']})
            except workspace.Conflict as exc:
                self.send_json({'error':str(exc)}, HTTPStatus.CONFLICT)
            except (ValueError, KeyError, TypeError, RecursionError) as exc:
                self.send_json({'error':str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if urlparse(self.path).path != "/api/ping":
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if self.headers.get("X-KE-Board-Token") != TOKEN:
            self.send_json({"error": "control token required"}, HTTPStatus.FORBIDDEN)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 100_000)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            incident = board.create_incident(
                team=str(payload["team"]),
                title=str(payload["title"]),
                details=str(payload["details"]),
                safe_action=str(payload["safe_action"]),
                required_capability=str(payload.get("required_capability") or "general"),
                severity=str(payload.get("severity") or "warning"),
                source="human-board-ui",
                needs_operator=bool(payload.get("needs_operator")),
                requested_owner=payload.get('owner'),
                actor="operator-ui",
            )
            incident = board.wake_incident(incident["incident_id"], actor="dispatcher")
            self.send_json({"ok": True, "incident": incident}, HTTPStatus.CREATED)
        except Exception as exc:
            self.send_json({"error": f"{type(exc).__name__}:{exc}"}, HTTPStatus.BAD_REQUEST)


def worker(stop: threading.Event):
    try:
        while not stop.is_set():
            DUTIES.tick()
            stop.wait(SCAN_SECONDS)
    finally:DUTIES.stop()


def main():
    board.init_db()
    stop = threading.Event()
    thread = threading.Thread(target=worker, args=(stop,), name="board-dispatcher", daemon=True)
    thread.start()
    server = BoardHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    def shutdown(_signum, _frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Switchboard listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
