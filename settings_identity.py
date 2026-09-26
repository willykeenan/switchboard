"""Local board control and revocable settings-only agent credentials.

Same-UID full-access processes are inside the existing board trust boundary.
This is neither human-presence attestation nor authority to approve/start work.
"""
import hashlib
import ipaddress
import json
import math
import os
import secrets
import stat
import time
from pathlib import Path
from agent_settings import AgentSettings, Principal, Denied
from workflow_layout import active, team_of

CAP = 'agent-settings:write'
ISSUER = 'local-board-settings:v1'

def binding(state, subject, lane, doctor_teams=('minigame-recovery', 'doctor')):
    """Resolve an exact saved ID or its unique original provider ID, never a role claim."""
    seats = {p['agentId']: p for p in state['placements']}
    candidates = [s for s in state['sessions'] if s['agent_id'] in seats and
                  (s['agent_id'] == subject or s.get('provider', '')+':'+str(s.get('endpoint', '')) == subject)]
    if len(candidates) != 1:
        raise Denied('Registered identity is missing, stale or ambiguous')
    session = candidates[0]; seat = seats[session['agent_id']]
    if not state.get('enabled') or seat['laneId'] != lane or not any(l['id'] == lane and active(l) for l in state['lanes']):
        raise Denied('Registered identity has no active grant in this lane')
    team = team_of(seat)
    owner = seat['role'] == 'coordinator' and any(x['agentId'] == session['agent_id'] and x['laneId'] == lane and x['teamId'] == 'coordinator' for x in state.get('teamLeads', []))
    doctor = seat['role'] == 'worker' and team in doctor_teams and any(t['id'] == team and t['laneId'] == lane and t['role'] == 'worker' for t in state.get('teams', []))
    if not owner and not doctor:
        raise Denied('Saved role/team is not an authorized owner or Lane Doctor')
    if not session.get('endpoint') or session.get('provider') != 'codex':
        raise Denied('Original registered provider binding is unavailable')
    return dict(subject=session['agent_id'], provider=session['provider'], endpoint=session['endpoint'],
                lane=lane, role=seat['role'], team=team, duty='lane-doctor' if doctor else 'owner')

class GrantStore:
    """Private local grant registry. Tokens are returned once; only digests persist.

    No HTTP grant/approval/start endpoint. The local operator uses identityctl.
    """
    def __init__(self, path): self.path = Path(path)

    def _check_parent(self):
        p = self.path.parent
        if not p.exists(): p.mkdir(mode=0o700)  # existing runtime parent required
        s = p.lstat()
        if not stat.S_ISDIR(s.st_mode) or p.is_symlink() or s.st_uid != os.getuid() or s.st_mode & 0o077:
            raise Denied('Settings grant directory must be owner-only and not a symlink')

    def read(self):
        if not self.path.exists(): return {}
        self._check_parent()
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            s = os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > 1024*1024:
                raise Denied('Settings grant registry must be a bounded owner-only regular file')
            data = json.load(f)
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('grants'), dict):
            raise Denied('Settings grant registry is invalid')
        return data['grants']

    def _edit(self, edit):
        self._check_parent(); lock = self.path.with_suffix('.lock')
        try: fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError: raise Denied('Settings grant writer busy; preserve its lock')
        tmp = self.path.with_name(self.path.name+'.tmp-'+secrets.token_hex(8))
        try:
            data = self.read(); result = edit(data)
            with os.fdopen(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as f:
                json.dump({'version':1,'grants':data}, f); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return result
        finally:
            os.close(fd); lock.unlink()
            if tmp.exists(): tmp.unlink()

    def issue(self, state, subject, lane, ttl=3600, doctor_teams=('minigame-recovery','doctor')):
        if type(ttl) is not int or not 1 <= ttl <= 28800: raise ValueError('Credential lifetime must be 1–28800 seconds')
        bound = binding(state, subject, lane, doctor_teams)
        token = secrets.token_urlsafe(32); key = hashlib.sha256(token.encode()).hexdigest()
        entry = dict(bound, expires=time.time()+ttl)
        self._edit(lambda d: d.__setitem__(key, entry))
        return token, key

    def revoke(self, key):
        if len(key) != 64: raise ValueError('Use the grant ID, not its secret')
        return self._edit(lambda d: d.pop(key, None) is not None)

class LocalSettingsIdentity:
    def __init__(self, settings, token, store, doctor_teams=('minigame-recovery','doctor')):
        self.settings, self.token, self.store, self.doctor_teams = settings, token, store, doctor_teams

    def fresh_sessions(self):
        # The author's inventory caches provider mappings for five seconds.
        # Authorization needs a fresh mapping even inside the save transaction.
        flow = self.settings.flow
        with flow._catalog_lock:
            flow._cached = None
            return flow.catalog()['sessions']

    def _local(self, h):
        host = h.headers.get('Host', '')
        if host not in (f'127.0.0.1:{h.server.server_port}', f'localhost:{h.server.server_port}'):
            raise Denied('Same-origin loopback Host required')
        if not ipaddress.ip_address(h.client_address[0]).is_loopback:
            raise Denied('Local board connection required')
        origin = 'http://'+host
        if h.headers.get('Sec-Fetch-Site') == 'cross-site' or h.headers.get('Origin', origin) != origin:
            raise Denied('Untrusted settings request origin')
        return origin

    def _grant(self, key, state):
        grant = self.store.read().get(key)
        if not grant: raise Denied('Settings credential is unknown or revoked')
        expires = grant.get('expires')
        if not isinstance(expires, (int,float)) or not math.isfinite(expires) or expires <= time.time():
            raise Denied('Settings credential expired')
        current = binding(state, grant.get('subject'), grant.get('lane'), self.doctor_teams)
        if any(grant.get(k) != val for k,val in current.items()):
            raise Denied('Settings credential identity, provider, lane or role binding is stale')
        return grant

    def resolve(self, h):
        origin = self._local(h)
        auth = h.headers.get('Authorization')
        if auth is not None:
            if not auth.startswith('Bearer ') or not 20 <= len(auth[7:]) <= 256:
                raise Denied('Invalid settings credential')
            key = hashlib.sha256(auth[7:].encode()).hexdigest()
            state = self.settings.flow.snapshot()
            state['sessions'] = self.fresh_sessions()
            g = self._grant(key, state)
            return Principal(g['subject'], 'agent', ISSUER+':'+key, g['expires'],
                             lanes=(g['lane'],), team_id=g['team'], duty=g['duty'], capabilities=(CAP,))
        # Existing local operator boundary: browser GET can inspect capability;
        # every mutation still requires explicit Origin + current control token.
        supplied = h.headers.get('X-KE-Board-Token', '')
        trusted_token = bool(supplied) and secrets.compare_digest(supplied, self.token())
        if h.command == 'GET' and h.headers.get('Sec-Fetch-Site') == 'same-origin':
            return Principal('local-board-operator','human',ISSUER,time.time()+60,capabilities=(CAP,))
        if h.headers.get('Origin') == origin and trusted_token:
            return Principal('local-board-operator','human',ISSUER,time.time()+60,capabilities=(CAP,))
        if h.command == 'GET': return None  # existing diagnostic read boundary
        raise Denied('Explicit local Save requires same Origin and current board control token')

    def validate(self, principal, state):
        if not isinstance(principal, Principal): raise Denied('Settings principal missing')
        if principal.kind == 'human' and principal.issuer == ISSUER and principal.subject == 'local-board-operator': return
        if principal.kind != 'agent' or not principal.issuer.startswith(ISSUER+':'):
            raise Denied('Unsupported settings principal issuer')
        key = principal.issuer[len(ISSUER)+1:]; g = self._grant(key, state)
        if (principal.subject != g['subject'] or principal.expires_at != g['expires'] or
            principal.lanes != (g['lane'],) or principal.team_id != g['team'] or principal.duty != g['duty'] or principal.capabilities != (CAP,)):
            raise Denied('Settings principal no longer matches its server grant')

class LocalControlSettings(AgentSettings):
    """Use the author's real persistence service, with revocation rechecked inside its transaction."""
    def __init__(self, production, token, catalog=None, grant_path=None, doctor_teams=('minigame-recovery','doctor')):
        super().__init__(production, catalog)
        store = GrantStore(grant_path or Path(production.root)/'runtime/settings-identity/grants.json')
        self.identity = LocalSettingsIdentity(self, token, store, doctor_teams)

    def authorize(self, principal, state, agent_id):
        if isinstance(principal, Principal) and principal.kind == 'agent':
            state = dict(state, sessions=self.identity.fresh_sessions())
        self.identity.validate(principal, state)
        return super().authorize(principal, state, agent_id)

    def read(self,agent_id,principal=None):
        import sqlite3
        result=super().read(agent_id,principal)
        path=Path(self.production.root)/'board.sqlite3'
        if not path.is_file() or path.is_symlink():return result
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='taskflow_assignments'").fetchone():return result
            row=db.execute("SELECT body FROM taskflow_assignments WHERE worker=? AND state IN ('OFFERED','ACCEPTED','STARTING','RUNNING','UNCERTAIN','CANCELLING')",(agent_id,)).fetchone()
        if row:
            a=json.loads(row[0]);result['currentAssignment']={'id':a['id'],'taskId':a['taskId'],'kind':'taskflow',
                'status':a['state'],'active':True,'model':a.get('model'),'effort':a.get('effort'),
                'minutes':a['maxMinutes'],'objective':a['payload']['request'],'instructions':a['payload'].get('definitionOfDone'),
                'workspace':a['workdir'],'artifacts':a['payload'].get('expectedArtifacts',[]),
                'approvalPolicy':'never' if a['mode']=='managed' else None,'sandbox':'danger-full-access' if a['mode']=='managed' else None,
                'providerThreadId':a.get('providerThreadId'),'providerTurnId':a.get('providerTurnId'),'phase':a.get('phase','work')}
        result['effect']='Future drafts and newly prepared TaskFlow assignments only. Existing drafts, prepared assignments and active turns remain pinned.'
        return result
