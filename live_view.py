"""All agents at once: a compact, passive snapshot of what every session is doing now.

Read-only by construction. It uses the same local event-log observer as the rest of the board: it never
attaches to, resumes or takes ownership of a session, so it can stay open in several places (this board,
Codex's side panel, a browser) while the agents keep working. One cached snapshot serves every viewer.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 'ke.live-agents.v1'
RECENT_SECONDS = 12 * 3600
OLD_REQUEST_SECONDS = 48 * 3600  # a "needs you" older than this is a backlog item, not a live interruption
ACTIVITY_FIELDS = ('phase', 'label', 'ageSeconds', 'fresh', 'stale', 'active', 'turnStatus', 'durationSeconds',
                   'lastAction', 'lastActionAt', 'publicAction', 'publicActionAt', 'observedAt', 'lastFinishedAt',
                   'lastFinishedDurationSeconds', 'model', 'reasoning')
ATTENTION = ('input', 'approval', 'review', 'uncertain', 'failed')
WORKING = ('working', 'thinking', 'tools', 'command', 'editing', 'searching', 'responding', 'compacting', 'starting',
           'waiting', 'queued', 'cancelling')


def _epoch(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e12 else 1)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def claude_names(home=None):
    """Names Claude Code shows for its live sessions (the desktop app's session registry), by session id."""
    names = {}
    base = Path(home or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'sessions'
    try:
        for f in list(base.glob('*.json'))[:500]:
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get('sessionId') and isinstance(rec.get('name'), str) and rec['name'].strip():
                names[rec['sessionId']] = ' '.join(rec['name'].split())[:120]
    except OSError:
        pass
    return names


def friendly_title(session, names):
    aid = session.get('agent_id') or ''
    title = session.get('title') or session.get('display_name') or ''
    raw = aid.split(':', 1)[-1]
    if title and title not in (aid, raw):
        return title
    if aid.startswith('claude:'):
        return names.get(raw) or 'Claude Code session'
    return title or aid


# Role colours the teams use in their session names, in the order Codex's sidebar groups them.
ROLE_GROUPS = (('🟣', 'Owner'), ('🔵', 'Engineering'), ('🟢', 'Research & records'), ('🔴', 'Audit'),
               ('🟤', 'Lane doctor'), ('⚪', 'Transport'), ('🟠', 'Install'), ('🟡', 'Support'))
PROJECT_PREFIXES = tuple(p for p in os.environ.get('SWITCHBOARD_ASSIGNMENT_PREFIXES', '').split(',') if p)
OPEN_WORK = ('AWAITING_PICKUP', 'ACCEPTED', 'REVISION', 'BLOCKED', 'RESULT_REPORTED')
OPEN_TASKS = ('CAPTURED', 'LINKED', 'QUEUED', 'REVIEW', 'BLOCKED', 'NEEDS_COORDINATION')


def role_group(title):
    for rank, (mark, label) in enumerate(ROLE_GROUPS):
        if (title or '').startswith(mark):
            return rank, label
    return len(ROLE_GROUPS), 'Sessions'


def natural(text):
    import re
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', text or '')]


def humanize_assignment(value):
    """integrated-core-20260926-v1 -> Integrated core (the id is the only title some contracts carry)."""
    import re
    words = [w for w in re.split(r'[-_]+', str(value or '')) if w]
    words = [w for w in words if not re.fullmatch(r'\d{6,}|v\d+|[0-9a-f]{7,}', w)]
    if len(words) > 2 and words[0] in PROJECT_PREFIXES:
        words = words[1:]
    text = ' '.join(words).strip()
    return (text[:1].upper() + text[1:])[:120] if text else ''


def codex_projects(codex_home=None):
    """thread id -> (project name, sidebar position), straight from Codex's own store, read-only."""
    import sqlite3
    path = Path(codex_home or os.environ.get('KE_WORKFLOW_CODEX_HOME') or Path.home() / '.codex') / 'state_5.sqlite'
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3) as db:
            cols = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
            if 'project_id' not in cols:
                return {}
            rows = db.execute('SELECT t.id, p.name, p.position, t.created_at FROM threads t '
                              'LEFT JOIN projects p ON p.id = t.project_id WHERE COALESCE(t.archived,0)=0').fetchall()
    except sqlite3.Error:
        return {}
    return {tid: (name or '', pos if isinstance(pos, int) else 999, created or 0) for tid, name, pos, created in rows}


def bucket(activity):
    """needs-you > working > recent > quiet, from the observed phase only (no guessing from process lists)."""
    phase = (activity or {}).get('phase')
    if phase in ATTENTION:
        return 'needs-you'
    if (activity or {}).get('active') or (phase in WORKING and not (activity or {}).get('stale')):
        return 'working'
    return 'recent' if phase in ('finished', 'stopped', 'accepted', 'rejected', 'quiet') else 'quiet'


class LiveView:
    def __init__(self, workflow, ttl=2.0, clock=time.time, monotonic=time.monotonic, claude_home=None, codex_home=None,
                 structure_ttl=15.0, background=True):
        self.workflow, self.ttl, self.clock, self.monotonic = workflow, ttl, clock, monotonic
        self.claude_home, self.codex_home = claude_home, codex_home
        self.structure_ttl, self.background = structure_ttl, background
        self._refreshing = False
        self._structure = None
        self._structure_at = -1e18
        self.lock = threading.Lock()
        self._cached = None
        self._cached_at = -1e18

    def snapshot(self, include_all=False):
        """Serve the latest snapshot at once; rebuild it in the background when stale (one rebuild at a time)."""
        with self.lock:
            stale = self._cached is None or self.monotonic() - self._cached_at >= self.ttl
            if stale and (self._cached is None or not self.background):
                self._cached = self._build()
                self._cached_at = self.monotonic()
            elif stale and not self._refreshing:
                self._refreshing = True
                threading.Thread(target=self._refresh, name='live-view-refresh', daemon=True).start()
            payload = self._cached
        if include_all:
            return payload
        live = ('needs-you', 'working', 'recent')
        active_projects = {s['project'] for s in payload['sessions'] if s['bucket'] in live and s['project']}
        # Whole rosters for projects with recent activity (idle members keep the team's shape visible); an old
        # request surfaces on its own without dragging its idle project along.
        shown = [s for s in payload['sessions'] if s['bucket'] in live + ('old-request',) or s['project'] in active_projects]
        return {**payload, 'sessions': shown, 'hiddenQuiet': len(payload['sessions']) - len(shown)}

    def _refresh(self):
        try:
            built = self._build()
        except Exception:  # keep serving the last good snapshot
            built = None
        with self.lock:
            if built is not None:
                self._cached, self._cached_at = built, self.monotonic()
            self._refreshing = False

    def invalidate(self):
        with self.lock:
            self._cached = None
            self._structure = None

    def save_note(self, agent_id, text, attempts=3):
        """Set the operator's direction note for one agent against the latest layout (the agent reads it at its next step)."""
        if not isinstance(agent_id, str) or not isinstance(text, str) or len(text) > 4000:
            raise ValueError('A note is text up to 4000 characters for one exact agent')
        from workspace import Conflict
        for attempt in range(attempts):
            try:
                result = self.workflow.mutate({'operation': 'note', 'revision': self.workflow.read()['revision'],
                                               'item': {'agentId': agent_id, 'text': text.strip()}})
                break
            except Conflict:
                if attempt == attempts - 1:
                    raise
        self.invalidate()
        return {'ok': True, 'revision': result['revision'], 'agentId': agent_id, 'note': text.strip()}

    def _work(self):
        try:
            return [w for w in (self.workflow.handoff_status() or {}).get('work', []) if w.get('workState') in OPEN_WORK]
        except Exception:
            return []

    def _tasks(self):
        try:
            flow = getattr(self.workflow, 'taskflow', None)
            return [t for t in (flow.list() if flow else []) if t.get('state') in OPEN_TASKS]
        except Exception:
            return []

    @staticmethod
    def _short(aid, titles):
        return (titles.get(aid) or 'another agent').lstrip('🟣🔵🟢🔴🟤⚪🟠🟡 ').strip()

    def _working_on(self, aid, tasks, work, titles):
        """The agent's own open task title, else the newest open work handed to it (with who handed it)."""
        mine = sorted((t for t in tasks if t.get('ownerId') == aid and t.get('title')), key=lambda t: -(_epoch(t.get('updatedAt')) or 0))
        if mine:
            return {'text': str(mine[0]['title'])[:160], 'state': mine[0].get('state'), 'from': None}
        handed = sorted((w for w in work if w.get('recipient') == aid and w.get('workState') != 'RESULT_REPORTED'),
                        key=lambda w: -(_epoch(w.get('created')) or 0))
        if handed:
            w = handed[0]
            return {'text': humanize_assignment((w.get('contract') or {}).get('assignmentId')) or 'Assigned work',
                    'state': w.get('workState'), 'from': self._short(w.get('sender'), titles)}
        return None

    def _waiting_on(self, aid, work, titles):
        """Agents holding work this agent handed out and is still waiting for."""
        names = []
        for w in work:
            if w.get('sender') == aid and w.get('workState') in ('AWAITING_PICKUP', 'ACCEPTED', 'REVISION'):
                name = self._short(w.get('recipient'), titles)
                if name not in names:
                    names.append(name)
        return names[:4]

    def _build(self):
        now = self.clock()
        activities = self.workflow.activity_snapshot().get('activities', {})
        catalog = self.workflow.catalog()
        try:
            state = self.workflow.read()
            placements = {p['agentId']: p for p in state.get('placements', [])}
            notes = {n['agentId']: n['text'] for n in state.get('notes', [])}
            team_names = {t['id']: t.get('name') for t in state.get('teams', []) if t.get('id')}
        except Exception:  # the view must keep working while the layout is being edited or is unreadable
            placements, notes, team_names = {}, {}, {}
        # Work, tasks, projects and names change slowly; refresh them less often than activity (2 s).
        if self._structure is None or self.monotonic() - self._structure_at >= self.structure_ttl:
            self._structure = (self._work(), self._tasks(), codex_projects(self.codex_home), claude_names(self.claude_home))
            self._structure_at = self.monotonic()
        work, tasks, projects, names = self._structure
        titles = {s.get('agent_id'): s.get('title') for s in catalog.get('sessions', [])}
        rows = []
        for s in catalog.get('sessions', []):
            aid = s.get('agent_id')
            if not aid:
                continue
            raw = activities.get(aid) or s.get('activity') or {}
            activity = {k: raw.get(k) for k in ACTIVITY_FIELDS if k in raw}
            progress = raw.get('planProgress')
            if isinstance(progress, dict) and isinstance(progress.get('total'), int):
                activity['planProgress'] = {'completed': progress.get('completed', 0), 'total': progress['total']}
            b = bucket(activity)
            last = max(filter(None, (_epoch(activity.get('observedAt')), _epoch(s.get('updatedAt')))), default=None)
            if b == 'recent' and (last is None or now - last > RECENT_SECONDS):
                b = 'quiet'
            if b == 'needs-you' and (last is None or now - last > OLD_REQUEST_SECONDS):
                b = 'old-request'
            place = placements.get(aid) or {}
            title = friendly_title(s, names)
            endpoint = s.get('endpoint') or aid.split(':', 1)[-1]
            proj_name, proj_pos, created = projects.get(endpoint, (s.get('projectName') or '', 999, 0))
            rank, group = role_group(title)
            rows.append({'id': aid, 'endpoint': endpoint, 'note': notes.get(aid, ''),
                         'projectOrder': proj_pos, 'group': group, 'groupOrder': rank, 'created': created,
                         'team': team_names.get(place.get('teamId')) or None,
                         'workingOn': self._working_on(aid, tasks, work, titles), 'waitingOn': self._waiting_on(aid, work, titles),
                         'title': title,
                         'provider': s.get('provider') or aid.split(':', 1)[0],
                         'project': proj_name or s.get('projectName') or ('Claude Code' if aid.startswith('claude:') else ''),
                         'managed': bool(s.get('managed')), 'role': place.get('role'), 'lane': place.get('laneId'),
                         'bucket': b, 'lastSeenAt': datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
                         'activity': activity})
        # Every row of a project shares that project's sidebar position, even if only the board labels it.
        positions = {}
        for r in rows:
            if r['project']:
                positions[r['project']] = min(positions.get(r['project'], 999), r['projectOrder'])
        for r in rows:
            r['projectOrder'] = positions.get(r['project'], r['projectOrder'])
        order = {'needs-you': 0, 'working': 1, 'recent': 2, 'old-request': 3, 'quiet': 4}
        # Stable structural order: project (Codex sidebar), role group, then name. Rows never reshuffle by status.
        rows.sort(key=lambda r: (r['projectOrder'], r['project'].lower(), r['groupOrder'], natural(r['title']), r['created'], r['id']))
        counts = {k: sum(r['bucket'] == k for r in rows) for k in order}
        return {'schemaVersion': SCHEMA, 'sampledAt': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                'pollSeconds': 3, 'passive': True, 'counts': counts, 'sessions': rows,
                'errors': list(catalog.get('errors') or []),
                'basis': 'Local event logs, read passively. No session is attached, resumed or interrupted.'}
