"""Live view: every team in a stable structural order, passive, cached, private, with working-on and waiting-on."""
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from live_view import LiveView, bucket, humanize_assignment, natural, role_group

BOARD = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 23, 0, tzinfo=timezone.utc)
T = {name: f'01a0{n:04d}-0000-7000-8000-00000000{n:04d}' for n, name in enumerate(
    ['owner', 'core', 'audit2', 'audit10', 'learn', 'idle', 'backlog', 'pidowner', 'other'], start=1)}


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def codex_home(tmp):
    """A Codex store whose sidebar puts 'Mobile app' before 'Payments'."""
    home = Path(tmp) / 'codex'
    home.mkdir()
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE projects (id TEXT, name TEXT, position INTEGER)')
        db.execute('CREATE TABLE threads (id TEXT, project_id TEXT, created_at INTEGER, archived INTEGER)')
        db.executemany('INSERT INTO projects VALUES (?,?,?)', [('p2', 'Payments', 1), ('p1', 'Mobile app', 0), ('p9', 'Old stuff', 5)])
        for i, (key, proj) in enumerate([('owner', 'p1'), ('core', 'p1'), ('audit2', 'p1'), ('audit10', 'p1'), ('learn', 'p1'),
                                         ('idle', 'p1'), ('pidowner', 'p2'), ('backlog', 'p9'), ('other', 'p9')]):
            db.execute('INSERT INTO threads VALUES (?,?,?,0)', (T[key], proj, 100 + i))
    return home


class StubWorkflow:
    def __init__(self):
        self.calls = 0
        c = lambda key, title, **kw: dict({'agent_id': 'codex:' + T[key], 'endpoint': T[key], 'title': title, 'provider': 'codex',
                                           'projectName': 'stale label', 'updatedAt': NOW.timestamp()}, **kw)
        self.sessions = [
            c('audit10', '🔴 External Audit 10'), c('core', '🔵 Core Engineer'), c('owner', '🟣 Workstream Owner'),
            c('audit2', '🔴 External Audit 2'), c('learn', '🔵 <b>Learning</b> & Test'), c('idle', '🟢 Library & Receipts', updatedAt=NOW.timestamp() - 5 * 86400),
            c('pidowner', '🟣 Workstream Owner'), c('backlog', 'Install & Verify', updatedAt=NOW.timestamp() - 14 * 86400),
            c('other', 'Last week', updatedAt=NOW.timestamp() - 7 * 86400),
            {'agent_id': 'claude:1111aaaa-2222-3333-4444-555566667777', 'title': 'claude:1111aaaa-2222-3333-4444-555566667777',
             'provider': 'claude', 'projectName': '', 'updatedAt': NOW.timestamp()},
        ]
        a = lambda key: 'codex:' + T[key]
        self.activities = {
            a('core'): {'phase': 'command', 'label': 'Running a command…', 'active': True, 'turnStatus': 'open', 'ageSeconds': 2,
                        'observedAt': iso(0), 'durationSeconds': 2222, 'publicAction': 'Checking the app records',
                        'planProgress': {'completed': 2, 'total': 5}, 'detail': 'A command was requested',
                        'prompt': 'SECRET-PROMPT', 'command': 'SECRET-COMMAND'},
            a('owner'): {'phase': 'input', 'label': 'Needs your input', 'active': True, 'turnStatus': 'open', 'observedAt': iso(1)},
            a('audit2'): {'phase': 'finished', 'label': 'Idle · turn ended', 'observedAt': iso(10), 'lastFinishedDurationSeconds': 11.655999898910522},
            a('audit10'): {'phase': 'quiet', 'label': 'No recent update', 'turnStatus': 'open', 'ageSeconds': 420, 'observedAt': iso(7)},
            a('learn'): {'phase': 'thinking', 'label': 'Thinking…', 'active': True, 'turnStatus': 'open', 'observedAt': iso(0)},
            a('idle'): {'phase': 'finished', 'label': 'Idle · turn ended', 'observedAt': iso(5 * 24 * 60)},
            a('backlog'): {'phase': 'review', 'label': 'Awaiting your review', 'observedAt': iso(14 * 24 * 60)},
            a('other'): {'phase': 'finished', 'label': 'Idle · turn ended', 'observedAt': iso(7 * 24 * 60)},
            a('pidowner'): {'phase': 'finished', 'label': 'Idle · turn ended', 'observedAt': iso(30)},
            'claude:1111aaaa-2222-3333-4444-555566667777': {'phase': 'command', 'label': 'Running a command…', 'active': True, 'observedAt': iso(0)},
        }
        self.work = [
            {'sender': a('owner'), 'recipient': a('core'), 'workState': 'ACCEPTED', 'created': 10,
             'contract': {'assignmentId': 'integrated-core-20260926-v1'}},
            {'sender': a('owner'), 'recipient': a('audit2'), 'workState': 'AWAITING_PICKUP', 'created': 11,
             'contract': {'assignmentId': 'session-visibility-audit-045c28e-v2'}},
            {'sender': a('owner'), 'recipient': a('learn'), 'workState': 'CLOSED', 'created': 12, 'contract': {'assignmentId': 'old-thing'}},
        ]
        self.taskflow = type('TF', (), {'list': lambda _self: [
            {'ownerId': a('learn'), 'title': 'Build the acceptance harness', 'state': 'QUEUED', 'updatedAt': '1788968794.155'},
            {'ownerId': a('learn'), 'title': 'Older queued task', 'state': 'QUEUED', 'updatedAt': '2026-09-01T00:00:00+00:00'},
            {'ownerId': a('core'), 'title': 'Finished long ago', 'state': 'DONE', 'updatedAt': 9}]})()

    def activity_snapshot(self, targets=None):
        self.calls += 1
        return {'activities': dict(self.activities)}

    def catalog(self):
        return {'sessions': [dict(s) for s in self.sessions], 'errors': []}

    def read(self):
        return {'placements': [{'agentId': 'codex:' + T['core'], 'laneId': 'work', 'role': 'worker', 'teamId': 'team-core'}],
                'notes': [], 'teams': [{'id': 'team-core', 'name': 'Core Engineer'}]}

    def handoff_status(self):
        return {'work': self.work}


class LiveViewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        claude = Path(self._tmp.name) / 'claude'
        (claude / 'sessions').mkdir(parents=True)
        (claude / 'sessions' / '123.json').write_text(json.dumps(
            {'pid': 123, 'sessionId': '1111aaaa-2222-3333-4444-555566667777', 'name': 'Fixing the board'}))
        self.claude, self.codex = claude, codex_home(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def view(self, wf=None, **kw):
        kw.setdefault('background', False)
        return LiveView(wf or StubWorkflow(), clock=lambda: NOW.timestamp(), claude_home=self.claude, codex_home=self.codex, **kw)

    def rows(self, snap, key):
        return next(s for s in snap['sessions'] if s['id'] == 'codex:' + T[key])

    def test_structural_order_is_stable(self):
        snap = self.view().snapshot()
        order = [(s['project'], s['title']) for s in snap['sessions']]
        self.assertEqual(order[:6], [
            ('Mobile app', '🟣 Workstream Owner'), ('Mobile app', '🔵 <b>Learning</b> & Test'), ('Mobile app', '🔵 Core Engineer'),
            ('Mobile app', '🟢 Library & Receipts'), ('Mobile app', '🔴 External Audit 2'), ('Mobile app', '🔴 External Audit 10')])
        self.assertEqual(order[6], ('Payments', '🟣 Workstream Owner'))  # Codex's project order, not the stale board label
        stray = StubWorkflow()  # a session only the board labels 'Mobile app' still sorts inside that project
        stray.sessions.append({'agent_id': 'codex:stray', 'endpoint': 'stray', 'title': '🔴 External Audit 3', 'provider': 'codex',
                               'projectName': 'Mobile app', 'updatedAt': NOW.timestamp()})
        stray.activities['codex:stray'] = {'phase': 'finished', 'label': 'Idle', 'observedAt': iso(5)}
        stray_order = [s['title'] for s in self.view(stray).snapshot()['sessions'] if s['project'] == 'Mobile app']
        self.assertEqual(stray_order.index('🔴 External Audit 3'), stray_order.index('🔴 External Audit 2') + 1)
        wf = StubWorkflow()
        wf.activities['codex:' + T['audit2']] = {'phase': 'input', 'label': 'Needs your input', 'active': True, 'observedAt': iso(0)}
        self.assertEqual([s['id'] for s in self.view(wf).snapshot()['sessions']], [s['id'] for s in snap['sessions']])  # status never reorders

    def test_whole_roster_for_active_projects_only(self):
        snap = self.view().snapshot()
        titles = {s['title'] for s in snap['sessions']}
        self.assertIn('🟢 Library & Receipts', titles)  # idle for days, but its team is active
        self.assertNotIn('Last week', titles)  # inactive project stays out
        self.assertIn('Install & Verify', titles)  # an old request still surfaces (the page puts it in the backlog)
        self.assertEqual(self.rows(snap, 'backlog')['bucket'], 'old-request')
        self.assertEqual(snap['counts']['needs-you'], 1)

    def test_working_on_and_waiting_on(self):
        snap = self.view().snapshot()
        self.assertEqual(self.rows(snap, 'core')['workingOn'], {'text': 'Integrated core', 'state': 'ACCEPTED', 'from': 'Workstream Owner'})
        self.assertEqual(self.rows(snap, 'learn')['workingOn']['text'], 'Build the acceptance harness')  # own task wins
        self.assertEqual(self.rows(snap, 'owner')['waitingOn'], ['Core Engineer', 'External Audit 2'])
        self.assertEqual(self.rows(snap, 'core')['team'], 'Core Engineer')
        self.assertEqual(humanize_assignment('session-visibility-audit-045c28e-v2'), 'Session visibility audit')

    def test_privacy_names_and_caching(self):
        snap = self.view().snapshot(include_all=True)
        text = json.dumps(snap)
        for secret in ('SECRET-PROMPT', 'SECRET-COMMAND', 'A command was requested'):
            self.assertNotIn(secret, text)
        claude = next(s for s in snap['sessions'] if s['id'].startswith('claude:'))
        self.assertEqual((claude['title'], claude['project']), ('Fixing the board', 'Claude Code'))
        wf = StubWorkflow()
        ticks = [0.0]
        view = LiveView(wf, ttl=2.0, clock=lambda: NOW.timestamp(), monotonic=lambda: ticks[0], claude_home=self.claude,
                        codex_home=self.codex, background=False)
        for _ in range(10):
            view.snapshot()
        self.assertEqual(wf.calls, 1)
        ticks[0] = 2.5
        view.snapshot()
        self.assertEqual(wf.calls, 2)

    def test_stale_snapshot_is_served_at_once_and_refreshed_in_background(self):
        import time as _time
        wf = StubWorkflow()
        ticks = [0.0]
        view = LiveView(wf, ttl=2.0, clock=lambda: NOW.timestamp(), monotonic=lambda: ticks[0], claude_home=self.claude, codex_home=self.codex)
        first = view.snapshot()
        slow = wf.activity_snapshot
        wf.activity_snapshot = lambda targets=None: (_time.sleep(0.5), slow())[1]
        ticks[0] = 3.0
        started = _time.monotonic()
        again = view.snapshot()
        self.assertLess(_time.monotonic() - started, 0.2)  # no waiting on the rebuild
        self.assertEqual(again['sampledAt'], first['sampledAt'])
        view.snapshot()  # a second stale read does not start a second rebuild
        for _ in range(50):
            if not view._refreshing:
                break
            _time.sleep(0.05)
        self.assertEqual(wf.calls, 2)

    def test_degrades_without_layout_work_or_codex_store(self):
        wf = StubWorkflow()
        wf.read = lambda: (_ for _ in ()).throw(ValueError('incomplete'))
        wf.handoff_status = lambda: (_ for _ in ()).throw(RuntimeError('daemon down'))
        view = LiveView(wf, clock=lambda: NOW.timestamp(), claude_home=self.claude, codex_home=Path(self._tmp.name) / 'missing', background=False)
        snap = view.snapshot()
        self.assertTrue(snap['sessions'])
        self.assertEqual(self.rows(snap, 'core')['project'], 'stale label')  # falls back to the board's label

    def test_helpers(self):
        self.assertEqual(role_group('🔴 External Audit 1'), (3, 'Audit'))
        self.assertEqual(role_group('Plain name')[1], 'Sessions')
        self.assertLess(natural('Audit 2'), natural('Audit 10'))
        self.assertEqual(bucket({'phase': 'review'}), 'needs-you')
        self.assertEqual(bucket({'phase': 'thinking', 'stale': False}), 'working')
        self.assertEqual(bucket(None), 'quiet')


class NoteTests(unittest.TestCase):
    def test_note_saves_against_the_latest_layout(self):
        from test_workspace import fixture
        from workflow import Workflow
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'board'
            w, _ = fixture(root)
            flow = Workflow(root, w, Path(d) / 'no-provider-store')
            state = {k: v for k, v in flow.read().items() if k != 'revision'}
            state.update(enabled=True)
            flow.save(state, 0, 'fixture')
            view = LiveView(flow)
            flow.mutate({'operation': 'default', 'revision': 1, 'item': {'value': 'explicit-only'}})
            result = view.save_note('codex:a', '  Finish the records check first.  ')
            self.assertEqual(result['note'], 'Finish the records check first.')
            self.assertEqual(flow.context('a')['assignmentNote'], 'Finish the records check first.')
            self.assertEqual(flow.read()['defaultCommunication'], 'explicit-only')
            with self.assertRaises(ValueError):
                view.save_note('codex:a', 'x' * 4001)
            with self.assertRaises(ValueError):
                view.save_note('codex:nobody', 'hi')


class PageContract(unittest.TestCase):
    def test_static_rules(self):
        html = (BOARD / 'live.html').read_text()
        js = (BOARD / 'live.js').read_text()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotRegex(html, r"\son[a-z]+\s*=")
        self.assertIn('name="viewport"', html)
        self.assertNotRegex(js, r"innerHTML|insertAdjacentHTML|document\.write")
        self.assertIn('/api/live', js)
        self.assertIn('visibilitychange', js)

    def test_roster_renders_in_chromium(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('playwright not installed')
        with tempfile.TemporaryDirectory() as d:
            claude = Path(d) / 'claude'
            (claude / 'sessions').mkdir(parents=True)
            snap = LiveView(StubWorkflow(), clock=lambda: NOW.timestamp(), claude_home=claude, codex_home=codex_home(d)).snapshot()
        snap['controlToken'] = 'fixture-token'
        payload = json.dumps(snap).encode()
        posted = []

        class H(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                posted.append((self.path, self.headers.get('X-KE-Board-Token'), json.loads(body)))
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def do_GET(self):
                if self.path.startswith('/api/live'):
                    self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(payload); return
                if self.path in ('/live', '/'):
                    self.path = '/live.html'
                return super().do_GET()

        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(H, directory=str(BOARD)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width in (1280, 375):
                    page = browser.new_page(viewport={'width': width, 'height': 900})
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{server.server_address[1]}/live')
                    page.wait_for_selector('.row')
                    text = page.inner_text('body').lower()
                    for needle in ('mobile app', 'payments', 'owner', 'engineering', 'audit', 'on: integrated core',
                                   'from workstream owner', 'waiting on core engineer, external audit 2', 'no update for 7m',
                                   'needs you', 'running a command…', '🔵 <b>learning</b> & test', 'last 12s', 'waiting on you for days'):
                        self.assertIn(needle, text, f'{needle!r} at {width}px')
                    names = page.eval_on_selector_all('.project:first-of-type .row .name', 'els => els.map(e => e.textContent)')
                    self.assertEqual(names[:3], ['🟣 Workstream Owner', '🔵 <b>Learning</b> & Test', '🔵 Core Engineer'])
                    self.assertFalse(page.evaluate("document.querySelector('.row b') !== null"))
                    self.assertFalse(page.evaluate("document.getElementById('empty').offsetParent !== null"))
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1'), f'overflow at {width}px')
                    if width == 1280:
                        page.evaluate("window.__marker = document.querySelector('.row')")
                        page.wait_for_timeout(3500)  # a refresh keeps the same elements in place
                        self.assertTrue(page.evaluate("document.querySelector('.row') === window.__marker && window.__marker.isConnected"))
                        page.click('.att')  # the Needs you strip jumps to and opens the agent
                        self.assertTrue(page.evaluate("document.querySelector('.row.open .name').textContent === '🟣 Workstream Owner'"))
                        page.click(".row.open button:has-text('Leave a note')")
                        page.fill('.editor textarea', 'Unblock Core first.')
                        page.wait_for_timeout(3500)
                        self.assertEqual(page.input_value('.editor textarea'), 'Unblock Core first.')
                        page.click("button:has-text('Save note')")
                        page.wait_for_timeout(500)
                        self.assertEqual(posted[-1][1:], ('fixture-token', {'agentId': 'codex:' + T['owner'], 'text': 'Unblock Core first.'}))
                        self.assertIn('codex://threads/' + T['owner'], page.get_attribute(".row.open a:has-text('Open in Codex')", 'href'))
                    page.fill('#search', 'audit')
                    self.assertEqual(page.eval_on_selector_all('.project .row', 'els => els.filter(e => e.offsetParent).length'), 2)
                    page.close()
                browser.close()
        finally:
            server.shutdown()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
