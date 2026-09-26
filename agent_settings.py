"""Versioned future-assignment settings. Never starts or changes a provider turn.

Identity is supplied by a trusted host adapter, never by request JSON or the
board's shared CSRF token. The shipped HTTP/CLI adapter denies writes until
such an authenticated principal is available. See SETTINGS-INTERFACE.md.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import sqlite3
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode
from workspace import Conflict
from workflow_layout import active, team_of

FIELDS = ('model', 'effort', 'personalContext', 'minutes', 'artifacts')
SCHEMA = '''CREATE TABLE IF NOT EXISTS agent_settings(
 agent_id TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_settings_history(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
 version INTEGER NOT NULL, created REAL NOT NULL, body TEXT NOT NULL,
 UNIQUE(agent_id,version));'''
AUTH_MISSING = 'Authenticated settings principal unavailable. A trusted host identity adapter is required; the shared board token is not an identity.'

def local_models():
    return {
        'ok': True,
        'provider': 'codex',
        'observedAt': time.time(),
        'models': [
            {'id': 'gpt-5', 'efforts': ['minimal', 'low', 'medium', 'high']},
            {'id': 'gpt-5-codex', 'efforts': ['low', 'medium', 'high', 'xhigh']},
        ],
    }

class Denied(PermissionError): pass
class Unavailable(RuntimeError): pass

def encoded(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
def fingerprint(value): return hashlib.sha256(encoded(value).encode()).hexdigest()

def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def defaults_from_db(db, agent_id):
    row = db.execute('SELECT body FROM managed_agents WHERE id=? AND status!=?', (agent_id, 'DRAFT')).fetchone()
    if not row: raise ValueError('Only existing managed agents have editable assignment defaults')
    agent = json.loads(row[0]); latest = {}
    for order in db.execute('SELECT body FROM work_orders WHERE agent_id=? ORDER BY rowid DESC', (agent_id,)):
        item = json.loads(order[0])
        if item.get('approvedAt'):
            latest = item.get('spec', {}); break
    values = {k: copy.deepcopy(latest.get(k, agent.get(k))) for k in FIELDS}
    values['personalContext'] = values['personalContext'] or ''
    row = db.execute('SELECT version,body FROM agent_settings WHERE agent_id=?', (agent_id,)).fetchone() if _table(db, 'agent_settings') else None
    return {'version': row[0] if row else 0,
            'values': json.loads(row[1]) if row else values,
            'basis': 'Saved future-assignment defaults' if row else 'Latest approved assignment / managed record',
            'agent': agent, 'latestSpec': latest}

def read_defaults(root, agent_id):
    path = Path(root)/'runtime/production.sqlite3'
    if not path.is_file() or path.is_symlink(): raise Unavailable('Managed configuration store unavailable')
    from store import query_only_existing
    with query_only_existing(path) as db:
        return defaults_from_db(db, agent_id)

@dataclass(frozen=True)
class Principal:
    """Construct only in a host-owned authenticated identity resolver.

    Grants must be issuer verified and bound to subject, lane and saved team.
    No HTTP/CLI code constructs this from caller supplied fields.
    """
    subject: str
    kind: str
    issuer: str
    expires_at: float
    lanes: tuple = ()
    team_id: str = ''
    duty: str = ''
    capabilities: tuple = ()

class AgentSettings:
    # Serialize HTTP handler writers; SQLite revisions still protect external writers.
    _write_lock = threading.RLock()

    def __init__(self, production, catalog=None):
        self.production = production
        self.flow = production.flow
        self.catalog = catalog or local_models

    def models(self):
        try: data = self.catalog()
        except Exception as exc: raise Unavailable('Authenticated model catalog unavailable; saved settings are preserved') from exc
        if (not isinstance(data, dict) or data.get('ok') is not True or data.get('provider') != 'codex'
                or not isinstance(data.get('observedAt'), (int, float))
                or not 0 <= time.time()-data['observedAt'] <= 300
                or not isinstance(data.get('models'), list) or not data['models']):
            raise Unavailable('A fresh authenticated Codex model catalog is required')
        for m in data['models']:
            if not isinstance(m, dict) or not isinstance(m.get('id'), str) or not isinstance(m.get('efforts'), list) or not m['efforts'] or not all(isinstance(e,str) for e in m['efforts']):
                raise Unavailable('Authenticated model catalog is incomplete')
        return copy.deepcopy(data)

    def authorize(self, principal, state, agent_id):
        if not isinstance(principal, Principal) or not principal.subject or not principal.issuer or not isinstance(principal.expires_at,(int,float)) or not math.isfinite(principal.expires_at) or principal.expires_at <= time.time():
            raise Denied(AUTH_MISSING)
        if 'agent-settings:write' not in principal.capabilities: raise Denied('This authenticated principal has no settings grant')
        seat = next((p for p in state['placements'] if p['agentId'] == agent_id), None)
        lane = next((l for l in state['lanes'] if seat and l['id'] == seat['laneId']), None)
        if not state.get('enabled') or not lane or not active(lane): raise Denied('Settings require a saved placement in an active lane')
        if principal.kind == 'human': return seat
        if principal.kind != 'agent': raise Denied('Unsupported principal kind')
        caller = next((p for p in state['placements'] if p['agentId'] == principal.subject), None)
        if not caller or caller['laneId'] != seat['laneId'] or seat['laneId'] not in principal.lanes:
            raise Denied('Agent settings updates are restricted to the authenticated agent’s saved lane')
        owner = caller['role'] == 'coordinator' and any(
            l['agentId'] == principal.subject and l['laneId'] == seat['laneId'] and l['teamId'] == 'coordinator'
            for l in state.get('teamLeads', []))
        doctor = (principal.duty == 'lane-doctor' and caller['role'] == 'worker'
                  and principal.team_id == team_of(caller)
                  and any(t['id'] == team_of(caller) and t['laneId'] == caller['laneId'] and t['role'] == 'worker' for t in state.get('teams', [])))
        if not (owner or doctor): raise Denied('Saved role/team does not match an authorized Workstream Owner or Lane Doctor')
        return seat

    def read(self, agent_id, principal=None):
        state = self.flow.snapshot()
        session = next((s for s in state['sessions'] if s['agent_id'] == agent_id), None)
        if not session: raise ValueError('Unknown original agent identity')
        seat = next((p for p in state['placements'] if p['agentId'] == agent_id), None)
        lane = next((l for l in state['lanes'] if seat and l['id'] == seat['laneId']), None)
        team = next((t for t in state.get('teams', []) if seat and t['id'] == team_of(seat) and t['laneId'] == seat['laneId']), None)
        managed = bool(session.get('managed'))
        defaults = read_defaults(self.production.root, agent_id) if managed else None
        orders = []
        if managed:
            from store import query_only_existing
            with query_only_existing(self.production.path) as db:
                orders = [json.loads(r[0]) for r in db.execute('SELECT body FROM work_orders WHERE agent_id=? ORDER BY rowid DESC', (agent_id,))]
        current = next((o for o in orders if o['status'] in ('STARTING','RUNNING','CANCELLING','UNCERTAIN')), None)
        last = current or next((o for o in orders if o.get('approvedAt')), None)
        spec = last.get('spec', {}) if last else {}
        can_edit, reason = False, 'Imported sessions are read-only here; their provider settings cannot be changed by this board.'
        if managed:
            try: self.authorize(principal, state, agent_id); can_edit, reason = True, ''
            except Denied as exc: reason = str(exc)
        history = []
        if managed:
            from store import query_only_existing
            with query_only_existing(self.production.path) as db:
                if _table(db, 'agent_settings_history'):
                    history = [json.loads(r[0]) for r in db.execute('SELECT body FROM agent_settings_history WHERE agent_id=? ORDER BY version DESC LIMIT 20', (agent_id,))]
        query = urlencode({'project': lane['projectId'] if lane else '', 'lane': lane['id'] if lane else '', 'team': team_of(seat) if seat else ''})
        values = defaults['values'] if defaults else {'model':session.get('model'), 'effort':session.get('reasoning')}
        return {'schemaVersion':'ke.agent-settings.v1', 'agentId':agent_id, 'name':session.get('title',agent_id),
                'provider':session.get('provider'), 'providerThreadId':defaults['agent'].get('providerThreadId') if managed else session.get('endpoint'), 'managed':managed,
                'placement':seat, 'laneName':lane.get('name') if lane else None, 'teamName':team.get('name') if team else (seat.get('role') if seat else None),
                'workflowRevision':state['revision'], 'version':defaults['version'] if defaults else None,
                'baseHash':fingerprint(values), 'configured':values,
                'configuredBasis':defaults['basis'] if defaults else 'Saved provider-session metadata; not a live execution observation',
                'canEdit':can_edit, 'editBlockedReason':reason, 'editableFields':list(FIELDS) if managed else [],
                'effect':'Future work drafts only. Existing drafts and active work remain immutable.',
                'currentAssignment':{'id':last['id'],'status':last['status'],'active':bool(current),
                    'model':spec.get('model'),'effort':spec.get('effort'),'minutes':spec.get('minutes'),
                    'objective':spec.get('objective'),'instructions':spec.get('personalContext'),
                    'workspace':spec.get('workspace'),'artifacts':spec.get('artifacts',[]),
                    'approvalPolicy':spec.get('approvalPolicy'),'sandbox':spec.get('sandbox')} if last else None,
                'observedExecution':copy.deepcopy(session.get('activity') or {}),
                'savedDirection':next((n.get('text') for n in state.get('notes',[]) if n.get('agentId')==agent_id), None),
                'contextFiles':spec.get('sources',[]), 'libraryUrl':'/library?'+query,
                'skills':{'configured':None,'note':'Per-agent skill enrollment is not exposed by this runtime. Library references are context, not enabled skills.'},
                'roots':{'allowedOutput':spec.get('workspace'), 'providerCwd':session.get('cwd'),
                         'enforcedSandbox':False,'note':'Managed output location is a work-contract boundary; full-access tools are not filesystem-isolated.'},
                'permissions':{'approvalPolicy':spec.get('approvalPolicy'),'sandbox':spec.get('sandbox'),'editable':False},
                'history':history}

    def update(self, item, principal=None):
        with self._write_lock:
            return self._update(item, principal)

    def _update(self, item, principal=None):
        required = {'agentId','version','workflowRevision','baseHash','changes'}
        if not isinstance(item,dict) or set(item) != required: raise ValueError('Use only agentId, version, workflowRevision, baseHash and changes')
        aid = item['agentId']; changes = item['changes']
        if not isinstance(aid,str) or type(item['version']) is not int or type(item['workflowRevision']) is not int or not isinstance(item['baseHash'],str): raise ValueError('Invalid settings identity or revision')
        if not isinstance(changes,dict) or not changes or set(changes)-set(FIELDS): raise ValueError('Unsupported or empty settings changes')
        self.authorize(principal, self.flow.snapshot(), aid)
        catalog = self.models()  # Never creates a provider process; reads an admitted cached catalog.
        path = self.production.path
        if not path.is_file() or path.is_symlink(): raise Unavailable('Managed store unavailable')
        with sqlite3.connect(path, timeout=10) as db:
            # Lock topology and configuration together before final authorization.
            for alias, p in [('topology', self.flow.path), ('workspace_catalog', self.flow.workspace.path)]:
                if not p.is_file() or p.is_symlink(): raise Unavailable('Saved workflow store unavailable')
                db.execute('ATTACH DATABASE ? AS '+alias, (str(p),))
            db.execute('BEGIN IMMEDIATE')
            state = self.flow.snapshot()
            self.authorize(principal, state, aid)
            if state['revision'] != item['workflowRevision']: raise Conflict('Workflow changed. Reload settings before saving.')
            old = defaults_from_db(db, aid)
            if old['version'] != item['version'] or fingerprint(old['values']) != item['baseHash']: raise Conflict('Settings changed. Reload and review your edits before saving.')
            values = {**old['values'], **copy.deepcopy(changes)}
            model = next((m for m in catalog['models'] if m['id']==values['model']), None)
            if not model or values['effort'] not in model['efforts']: raise ValueError('Model and reasoning must be advertised by the authenticated catalog')
            if not isinstance(values['personalContext'],str) or len(values['personalContext'])>8000: raise ValueError('Instructions must be text of at most 8000 characters')
            if type(values['minutes']) is not int or not 1<=values['minutes']<=120: raise ValueError('Time limit must be 1–120 minutes')
            names=values['artifacts']
            if not isinstance(names,list) or not 1<=len(names)<=20 or any(not isinstance(n,str) for n in names): raise ValueError('Name 1–20 relative output files')
            if len(set(names))!=len(names): raise ValueError('Output names must be unique')
            for n in names:
                p=Path(n)
                if not n or len(n)>240 or n.endswith('/') or any(ord(c)<32 for c in n) or p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]=='inputs' or n in ('AGENTS.md','BRIEF.md','switchboard-result.json','runtime.stderr.log'): raise ValueError('Outputs must be relative files inside the next work directory')
            if values==old['values']: return self.read(aid,principal)
            if not 0<=time.time()-catalog['observedAt']<=300: raise Unavailable('Authenticated model catalog expired before save')
            # No executescript here: sqlite executescript implicitly commits.
            for statement in SCHEMA.split(';'):
                if statement.strip(): db.execute(statement)
            version=old['version']+1
            event={'agentId':aid,'version':version,'at':time.time(),'actor':principal.subject,'issuer':principal.issuer,
                   'workflowRevision':state['revision'],'before':old['values'],'after':values,'changedFields':sorted(k for k in changes if values[k]!=old['values'][k]),'effect':'future-drafts-only'}
            db.execute('INSERT OR REPLACE INTO agent_settings VALUES(?,?,?)',(aid,version,encoded(values)))
            db.execute('INSERT INTO agent_settings_history(agent_id,version,created,body) VALUES(?,?,?,?)',(aid,version,event['at'],encoded(event)))
        self.flow._cached=None
        return self.read(aid,principal)
