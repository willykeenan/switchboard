"""Read-only, bounded worker census. No job controls or provider requests.

CPU is measured over consecutive samples keyed by PID AND process start time.
Session activity is observed from recent event metadata, never inferred from CPU.
Shared desktop runtimes are intentionally not assigned to individual sessions.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    psutil = None

class _EmptySwarm:
    def snapshot(self):
        return {'ok': False, 'runs': [], 'counts': {'live': 0}, 'processes': [], 'recentRuns': []}

ROOT = Path(__file__).resolve().parent
MAX_TAIL = 192 * 1024
MAX_SESSIONS = 96
FRESH_SECONDS = 90
SIGNATURES = [
    ('Swarm runtime', r'director[-_ ]swarm'),
    ('Codex', r'\bcodex\b|\bchatgpt\b'),
    ('Claude', r'\bclaude\b|claudefordesktop'),
    ('Grok', r'\bgrok(?:code)?\b'),
    ('Ollama', r'\bollama\b'),
    ('LM Studio', r'lm[ _-]?studio|\blms\b'),
    ('Local model', r'llama|vllm|mlx[_-]lm|comfyui|localai|text-generation'),
    ('ML workload', r'pytorch|tensorflow|transformers|diffusers|mps[_-]'),
    ('Agent service', r'switchboard|agent[-_ ]daemon|cpu-workers-service'),
]
PATTERNS = [(label, re.compile(pattern, re.I)) for label, pattern in SIGNATURES]


def iso(value=None):
    return datetime.fromtimestamp(value or time.time(), timezone.utc).isoformat()


def epoch(value):
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def text(value, limit=180):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or ''))[:limit]


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def progress_view(progress, now):
    """Only fresh publisher counters produce a progress bar; never from PID/CPU."""
    p = progress if isinstance(progress, dict) else {}
    processed, total = number(p.get('processed')), number(p.get('total'))
    stamp = epoch(p.get('observedAt') or p.get('updatedAt'))
    fresh = stamp is not None and -60 <= now - stamp <= 600
    valid = processed is not None and total is not None and total > 0 and 0 <= processed <= total
    return {'available': bool(p.get('available') and fresh and valid),
            'processed': processed, 'total': total,
            'percent': round(100 * processed / total, 1) if valid and fresh and p.get('available') else None,
            'unit': text(p.get('unit'), 40), 'updatedAt': p.get('observedAt') or p.get('updatedAt'),
            'state': 'published' if fresh and valid and p.get('available') else 'stale' if valid else 'unavailable'}


def read_events(path):
    """Bounded tail read. Partial first and final lines fail soft."""
    with Path(path).open('rb') as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - MAX_TAIL))
        lines = f.read(MAX_TAIL).splitlines()
    if size > MAX_TAIL:
        lines = lines[1:]
    events = []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                events.append(item)
        except (ValueError, UnicodeError):
            pass
    return events


def session_activity(events, now, provider):
    state, observed, model = 'unknown', None, None
    for event in events:
        stamp = epoch(event.get('timestamp'))
        if stamp is None or stamp > now + 60:
            continue
        kind, payload = event.get('type'), event.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        activity = None
        if provider == 'Codex':
            if kind == 'turn_context':
                model = payload.get('model') or model
            if kind == 'event_msg':
                etype = payload.get('type')
                if etype in ('task_complete', 'turn_aborted', 'task_failed'):
                    activity = {'task_complete': 'completed', 'turn_aborted': 'interrupted', 'task_failed': 'failed'}[etype]
                elif etype in ('task_started', 'user_message'):
                    activity = 'working'
            if kind == 'response_item' and state not in ('completed', 'interrupted', 'failed'):
                if payload.get('type') == 'reasoning' or payload.get('channel') == 'analysis':
                    activity = 'thinking'
                elif payload.get('type') in ('function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output'):
                    activity = 'tool activity'
                elif payload.get('type') == 'message' and payload.get('role') == 'assistant':
                    activity = 'responding'
        elif provider == 'Claude':
            message = event.get('message') or {}
            if not isinstance(message, dict):
                continue
            if kind == 'assistant':
                model = message.get('model') or model
                content = message.get('content') or []
                types = {c.get('type') for c in content if isinstance(c, dict)}
                activity = 'thinking' if 'thinking' in types else 'tool activity' if 'tool_use' in types else 'responding'
                if message.get('stop_reason') == 'end_turn':
                    activity = 'completed'
            elif kind == 'user':
                activity = 'working'
            elif kind == 'system' and event.get('subtype') == 'turn_duration':
                activity = 'completed'
            elif kind == 'progress':
                activity = 'tool activity'
        if activity:
            state, observed = activity, stamp
    age = max(0, now - observed) if observed else None
    fresh = age is not None and age <= FRESH_SECONDS
    return {'activity': state if fresh or state in ('completed', 'interrupted', 'failed') else 'unknown',
            'lastActivity': state, 'observedAt': iso(observed) if observed else None,
            'ageSeconds': round(age) if age is not None else None,
            'fresh': fresh, 'model': text(model, 100) or None}


def collect_sessions(home, now):
    rows, sources = [], []
    dbpath = home / '.codex/state_5.sqlite'
    try:
        with sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True, timeout=.4) as db:
            db.execute('PRAGMA query_only=ON')
            entries = db.execute('SELECT id, name, title, rollout_path FROM threads WHERE archived=0 AND updated_at>? ORDER BY updated_at DESC LIMIT ?', (now-86400, MAX_SESSIONS + 1)).fetchall()
        sources.append({'source': 'Codex', 'state': 'available', 'limited': len(entries) > MAX_SESSIONS})
        for sid, name, title, path in entries[:MAX_SESSIONS]:
            try:
                resolved = Path(path).resolve()
                if not resolved.is_relative_to(home / '.codex/sessions'):
                    continue
                activity = session_activity(read_events(resolved), now, 'Codex')
                rows.append({'id': 'codex:' + sid, 'provider': 'Codex', 'title': text(name or title),
                             'pid': None, 'pidEvidence': 'Shared runtime; task PID not exposed', **activity})
            except (OSError, ValueError):
                rows.append({'id': 'codex:' + sid, 'provider': 'Codex', 'title': text(name or title),
                             'pid': None, 'activity': 'unavailable', 'fresh': False, 'ageSeconds': None})
    except (OSError, sqlite3.Error):
        sources.append({'source': 'Codex', 'state': 'unavailable'})
    try:
        candidates = []
        projects = home / '.claude/projects'
        scanned, capped = 0, False
        for project in projects.iterdir():
            if not project.is_dir() or project.is_symlink():
                continue
            for file in project.glob('*.jsonl'):
                scanned += 1
                if scanned > 12000:
                    capped = True
                    break
                if not file.is_symlink() and file.stat().st_mtime > now-86400:
                    candidates.append((file.stat().st_mtime, file))
            if capped:
                break
        candidates.sort(reverse=True)
        sources.append({'source': 'Claude', 'state': 'available', 'limited': capped or len(candidates) > MAX_SESSIONS})
        for _, file in candidates[:MAX_SESSIONS]:
            try:
                events = read_events(file)
                activity = session_activity(events, now, 'Claude')
                slug = next((e.get('slug') for e in reversed(events) if e.get('slug')), None)
                rows.append({'id': 'claude:' + file.stem, 'provider': 'Claude', 'title': text(slug or 'Claude ' + file.stem[:8]),
                             'pid': None, 'pidEvidence': 'Session PID not exposed', **activity})
            except (OSError, ValueError):
                continue
    except OSError:
        sources.append({'source': 'Claude', 'state': 'unavailable'})
    rows.sort(key=lambda r: (not r.get('fresh', False), r.get('ageSeconds') if r.get('ageSeconds') is not None else float('inf')))
    return {'rows': rows, 'sources': sources, 'scope': 'Recent local session event metadata; no remote model telemetry'}


def collect_workers(processes):
    try:
        # launchd does not load the interactive shell's Homebrew PATH.
        node = shutil.which('node') or next((str(p) for p in [Path('/opt/homebrew/bin/node'),Path('/usr/local/bin/node'),Path.home()/'.local/bin/node'] if p.is_file() and os.access(p,os.X_OK)),None)
        if not node:
            raise RuntimeError('Node unavailable')
        result = subprocess.run([node, str(ROOT/'worker_snapshot.mjs')], input=json.dumps(processes).encode(), capture_output=True, timeout=6, check=True)
        if len(result.stdout) > 4*1024*1024:
            raise ValueError('Oversize observer response')
        payload = json.loads(result.stdout)
        if payload.get('schemaVersion') != 'ke.activity-monitor-agents.v1':
            raise ValueError('Observer schema mismatch')
        return payload
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
        return {'cpu': {'ok': False}, 'gpu': {'ok': False}}


class Observer:
    def __init__(self, home=None, cache_seconds=5):
        self.home = Path(home or Path.home())
        self.cache_seconds = cache_seconds
        self.lock = threading.Lock()
        self.previous = {}
        self.cached = None
        self.cached_at = 0
        self.swarm = _EmptySwarm()
        self.previous_host = None
        self.observer_rows = []

    def processes(self, now):
        rows, denied, vanished = [], 0, 0
        observer_rows = []
        current = {}
        if psutil is None:
            self.previous = current
            self.observer_rows = observer_rows
            return rows, {'observed': 0, 'restricted': 0, 'exitedDuringScan': 0, 'scope': 'Process census requires optional psutil'}
        attrs = ['pid', 'ppid', 'name', 'cmdline', 'cpu_times', 'create_time', 'memory_info', 'num_threads', 'status', 'username', 'uids']
        for process in psutil.process_iter():
            try:
                p = process.as_dict(attrs=attrs, ad_value=None)
                # Never serialize command arguments, environments, cwd or prompt content.
                args = p.get('cmdline') or []
                executable = str(args[0]) if args else str(p.get('name') or '')
                script = next((a for a in args[1:] if re.search(r'\.(?:py|mjs|js|sh)$', a) and '\n' not in a), '')
                signature = executable + ' ' + script
                provider = next((label for label, pattern in PATTERNS if pattern.search(signature)), None)
                name = text(p.get('name') or Path(executable).name, 90)
                start, cpu_times = p.get('create_time'), p.get('cpu_times')
                key = (p['pid'], start)
                ticks = cpu_times.user + cpu_times.system if cpu_times else None
                previous = self.previous.get(key)
                cpu = None
                if ticks is not None:
                    current[key] = (now, ticks)
                    if previous and now > previous[0]:
                        cpu = round(max(0, ticks - previous[1]) / (now - previous[0]) * 100, 1)
                memory = p.get('memory_info')
                if start is None or memory is None:
                    denied += 1
                observer_rows.append({'pid':p['pid'],'ppid':p.get('ppid'),'uid':p['uids'].real if p.get('uids') else None,
                                      'executable':executable,'command':' '.join(args),'cpuPercent':cpu or 0,
                                      'rssBytes':memory.rss if memory else 0,'elapsedSeconds':max(0,time.time()-start) if start else None,
                                      'state':'R' if p.get('status')=='running' else 'S', 'startedAt':iso(start) if start else None})
                rows.append({'pid': p['pid'], 'ppid': p.get('ppid'), 'name': name,
                             'executable': text(Path(executable).name, 90), 'script': text(Path(script).name, 90) if script else None,
                             'provider': provider, 'category': 'AI runtime' if provider else 'Process',
                             'cpuPercent': cpu, 'memoryBytes': memory.rss if memory else None,
                             'threads': p.get('num_threads'), 'state': p.get('status') or 'unavailable',
                             'startedAt': start, 'elapsedSeconds': int(max(0, time.time()-start)) if start else None,
                             'user': text(p.get('username'), 80), 'groups': [], 'evidence': 'OS process census'})
            except psutil.NoSuchProcess:
                vanished += 1
            except psutil.AccessDenied:
                denied += 1
        self.previous = current
        self.observer_rows = observer_rows
        # Descendants inherit an observed runtime relationship, not model identity.
        by_pid = {r['pid']: r for r in rows}
        for row in rows:
            ancestor, seen = row, {row['pid']}
            for _ in range(24):
                ancestor = by_pid.get(ancestor.get('ppid'))
                if not ancestor or ancestor['pid'] in seen:
                    break
                seen.add(ancestor['pid'])
                if ancestor['provider'] and not row['provider']:
                    row.update(provider=ancestor['provider'], category='Runtime descendant', evidence='Observed process ancestry')
                    break
        return rows, {'observed': len(rows), 'restricted': denied, 'exitedDuringScan': vanished, 'scope': 'All OS-visible processes on this Mac; restricted fields stay unknown'}

    def snapshot(self):
        with self.lock:
            if self.cached and time.monotonic()-self.cached_at < self.cache_seconds:
                return copy.deepcopy(self.cached)
            start, now = time.monotonic(), time.time()
            # Three independent read-only collectors; one sampler owns interval state.
            with ThreadPoolExecutor(max_workers=3) as pool:
                sessions_f = pool.submit(collect_sessions, self.home, now)
                swarm_f = pool.submit(self.swarm.snapshot)
                rows, coverage = self.processes(start)
                workers_f = pool.submit(collect_workers, self.observer_rows)
                workers, sessions, swarm = workers_f.result(), sessions_f.result(), swarm_f.result()
            by_pid = {r['pid']: r for r in rows}
            pools, gpu_jobs, swarm_runs = [], [], []
            cpu_section, gpu_section = workers.get('cpu') or {}, workers.get('gpu') or {}
            for p in (cpu_section.get('snapshot') or {}).get('pools', []):
                members = []
                for w in p.get('workers', []):
                    pid = w.get('pid')
                    row = by_pid.get(pid)
                    parent_pid = (p.get('parent') or {}).get('pid')
                    associated = bool(row and w.get('processAlive') and (not parent_pid or row['ppid'] == parent_pid))
                    if associated:
                        row['groups'].append(text(p.get('title')))
                        row['category'] = 'CPU worker'
                    assignment = w.get('assignment') or {}
                    assignment = assignment.get('value') or assignment.get('date') if isinstance(assignment, dict) else assignment
                    members.append({'pid': pid, 'label': text(w.get('label')), 'alive': associated and row['state'] != 'zombie',
                                    'state': row['state'] if associated else 'exited or identity unverified', 'assignment': text(assignment),
                                    'cpuPercent': row['cpuPercent'] if associated else None, 'memoryBytes': row['memoryBytes'] if associated else None})
                pools.append({'id': text(p.get('id')), 'title': text(p.get('title')), 'registered': p.get('registered') is True,
                              'parentPid': (p.get('parent') or {}).get('pid'), 'workers': members,
                              'live': sum(w['alive'] for w in members), 'progress': progress_view(p.get('progress'), now),
                              'state': text(p.get('runState')), 'failures': p.get('failures'), 'retries': p.get('retries')})
            for j in (gpu_section.get('snapshot') or {}).get('jobs', []):
                pids = [p for p in [j.get('pid'), *(j.get('childPids') or [])] if isinstance(p, int)]
                for pid in pids:
                    if pid in by_pid:
                        by_pid[pid]['groups'].append(text(j.get('title')))
                        by_pid[pid]['category'] = 'GPU job'
                gpu_jobs.append({'id': text(j.get('id') or j.get('jobId')), 'title': text(j.get('title')),
                                 'pids': pids, 'livePids': [p for p in pids if p in by_pid], 'state': text(j.get('state')),
                                 'owner': text(j.get('ownerTaskId')), 'framework': text(j.get('framework')),
                                 'progress': progress_view(j.get('progress'), now)})
            for p in swarm.get('processes', []):
                pid = p.get('pid')
                if pid in by_pid:
                    by_pid[pid]['category'] = 'Swarm runtime'
                    by_pid[pid]['groups'].append(text(p.get('runId')))
            for run in swarm.get('recentRuns', []):
                swarm_runs.append({'runId':run.get('id'), 'state':run.get('state'), 'title':text(run.get('objective')),
                                   'updatedAt':run.get('updatedAt'), 'workerCount':run.get('workerCount'), 'active':run.get('active')})
            pools.sort(key=lambda p:(-p['live'],p['title']))
            rows.sort(key=lambda r: (-(r['cpuPercent'] or 0), r['name'].lower(), r['pid']))
            if psutil is None:
                host = {'logicalCpus': None, 'cpuPercent': None, 'memoryPercent': None,
                        'memoryAvailable': None, 'memoryTotal': None}
            else:
                host_ticks = psutil.cpu_times()
                total = sum(host_ticks)
                idle = host_ticks.idle
                host_cpu = None
                if self.previous_host and total > self.previous_host[0]:
                    host_cpu = round(max(0, min(100, 100*(1-(idle-self.previous_host[1])/(total-self.previous_host[0])))), 1)
                self.previous_host = (total, idle)
                memory = psutil.virtual_memory()
                host = {'logicalCpus': psutil.cpu_count(), 'cpuPercent': host_cpu, 'memoryPercent': memory.percent,
                        'memoryAvailable': memory.available, 'memoryTotal': memory.total}
            result = {'schemaVersion': 'ke.switchboard-agents.v1', 'generatedAt': iso(), 'readOnly': True,
                      'collectionMs': round((time.monotonic()-start)*1000), 'coverage': coverage,
                      'host': host,
                      'processes': rows, 'sessions': sessions,
                      'cpu': {'ok': cpu_section.get('ok') is True, 'pools': pools},
                      'gpu': {'ok': gpu_section.get('ok') is True, 'jobs': gpu_jobs},
                      'swarm': {'ok': swarm.get('ok') is True, 'state': swarm.get('state'), 'runs': swarm_runs,
                                    'counts': swarm.get('counts'), 'truncation': swarm.get('truncation')},
                      'boundary': 'Process CPU is sampled usage, not proof of model thinking. Session activity is timestamped evidence. PID-only registry associations do not prove ownership across PID reuse. No processes are controlled.'}
            self.cached, self.cached_at = result, time.monotonic()
            return copy.deepcopy(result)
