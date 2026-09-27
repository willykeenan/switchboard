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
    if value is None:
        return None
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


def bucket(activity):
    """needs-you > working > recent > quiet, from the observed phase only (no guessing from process lists)."""
    phase = (activity or {}).get('phase')
    if phase in ATTENTION:
        return 'needs-you'
    if (activity or {}).get('active') or (phase in WORKING and not (activity or {}).get('stale')):
        return 'working'
    return 'recent' if phase in ('finished', 'stopped', 'accepted', 'rejected', 'quiet') else 'quiet'


class LiveView:
    def __init__(self, workflow, ttl=2.0, clock=time.time, monotonic=time.monotonic, claude_home=None):
        self.workflow, self.ttl, self.clock, self.monotonic = workflow, ttl, clock, monotonic
        self.claude_home = claude_home
        self.lock = threading.Lock()
        self._cached = None
        self._cached_at = -1e18

    def snapshot(self, include_all=False):
        with self.lock:
            if self._cached is None or self.monotonic() - self._cached_at >= self.ttl:
                self._cached = self._build()
                self._cached_at = self.monotonic()
            payload = self._cached
        if include_all:
            return payload
        shown = [s for s in payload['sessions'] if s['bucket'] != 'quiet']
        return {**payload, 'sessions': shown, 'hiddenQuiet': len(payload['sessions']) - len(shown)}

    def invalidate(self):
        with self.lock:
            self._cached = None

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

    def _build(self):
        now = self.clock()
        activities = self.workflow.activity_snapshot().get('activities', {})
        catalog = self.workflow.catalog()
        try:
            state = self.workflow.read()
            placements = {p['agentId']: p for p in state.get('placements', [])}
            notes = {n['agentId']: n['text'] for n in state.get('notes', [])}
        except Exception:  # the view must keep working while the layout is being edited or is unreadable
            placements, notes = {}, {}
        names = claude_names(self.claude_home)
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
            rows.append({'id': aid, 'endpoint': s.get('endpoint') or aid.split(':', 1)[-1], 'note': notes.get(aid, ''),
                         'title': friendly_title(s, names),
                         'provider': s.get('provider') or aid.split(':', 1)[0], 'project': s.get('projectName') or '',
                         'managed': bool(s.get('managed')), 'role': place.get('role'), 'lane': place.get('laneId'),
                         'bucket': b, 'lastSeenAt': datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
                         'activity': activity})
        order = {'needs-you': 0, 'working': 1, 'recent': 2, 'old-request': 3, 'quiet': 4}
        rows.sort(key=lambda r: (order[r['bucket']], -(_epoch(r['lastSeenAt']) or 0)))
        counts = {k: sum(r['bucket'] == k for r in rows) for k in order}
        return {'schemaVersion': SCHEMA, 'sampledAt': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                'pollSeconds': 3, 'passive': True, 'counts': counts, 'sessions': rows,
                'errors': list(catalog.get('errors') or []),
                'basis': 'Local event logs, read passively. No session is attached, resumed or interrupted.'}
