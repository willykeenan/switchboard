"""All-agents live view: passive snapshot, buckets, caching, privacy, and the page renders every state safely."""
import json
import re
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from live_view import LiveView, bucket

BOARD = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 23, 0, tzinfo=timezone.utc)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


class StubWorkflow:
    def __init__(self):
        self.calls = 0
        self.placements = [{'agentId': 'codex:work', 'laneId': 'core', 'role': 'worker'}]
        self.sessions = [
            {'agent_id': 'codex:work', 'endpoint': '01a0dbd5-3da0-7690-9ad1-a88a22a7286e', 'title': '🔵 Core Engineer', 'provider': 'codex',
             'projectName': 'Mobile app', 'updatedAt': NOW.timestamp()},
            {'agent_id': 'codex:ask', 'title': '🟣 Workstream Owner', 'provider': 'codex', 'projectName': 'Payments', 'updatedAt': NOW.timestamp()},
            {'agent_id': 'claude:done', 'title': 'Board fixes', 'provider': 'claude', 'projectName': '', 'updatedAt': NOW.timestamp() - 600},
            {'agent_id': 'codex:backlog', 'title': 'Install & Verify', 'provider': 'codex', 'projectName': 'Platform', 'updatedAt': NOW.timestamp() - 14 * 86400},
            {'agent_id': 'claude:1111aaaa-2222-3333-4444-555566667777', 'title': 'claude:1111aaaa-2222-3333-4444-555566667777', 'provider': 'claude', 'projectName': '', 'updatedAt': NOW.timestamp()},
            {'agent_id': 'codex:old', 'title': 'Last week', 'provider': 'codex', 'projectName': 'Mobile app', 'updatedAt': NOW.timestamp() - 7 * 86400},
            {'agent_id': 'codex:<img src=x onerror=alert(1)>', 'title': '<b>markup</b> & friends', 'provider': 'codex',
             'projectName': 'Mobile app', 'updatedAt': NOW.timestamp()},
        ]
        self.activities = {
            'codex:work': {'phase': 'command', 'label': 'Running a command…', 'active': True, 'turnStatus': 'open', 'ageSeconds': 2,
                           'observedAt': iso(0), 'durationSeconds': 2222, 'publicAction': 'Checking the app records',
                           'planProgress': {'completed': 2, 'total': 5, 'basis': 'x'}, 'detail': 'A command was requested',
                           'prompt': 'SECRET-PROMPT', 'command': 'SECRET-COMMAND'},
            'codex:ask': {'phase': 'input', 'label': 'Needs your input', 'active': True, 'observedAt': iso(1)},
            'codex:backlog': {'phase': 'review', 'label': 'Awaiting your review', 'observedAt': iso(14 * 24 * 60)},
            'claude:1111aaaa-2222-3333-4444-555566667777': {'phase': 'command', 'label': 'Running a command…', 'active': True, 'observedAt': iso(0)},
            'claude:done': {'phase': 'finished', 'label': 'Idle · turn ended', 'observedAt': iso(10), 'lastFinishedDurationSeconds': 11.655999898910522},
            'codex:old': {'phase': 'finished', 'label': 'Idle · turn ended', 'observedAt': iso(7 * 24 * 60)},
            'codex:<img src=x onerror=alert(1)>': {'phase': 'thinking', 'label': 'Thinking…', 'active': True, 'observedAt': iso(0)},
        }

    def activity_snapshot(self, targets=None):
        self.calls += 1
        return {'activities': dict(self.activities)}

    def catalog(self):
        return {'sessions': [dict(s) for s in self.sessions], 'errors': []}

    def read(self):
        return {'placements': self.placements}


class LiveViewTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._home = tempfile.TemporaryDirectory()
        (Path(self._home.name) / 'sessions').mkdir()
        (Path(self._home.name) / 'sessions' / '123.json').write_text(json.dumps(
            {'pid': 123, 'sessionId': '1111aaaa-2222-3333-4444-555566667777', 'name': 'Fixing the board'}))

    def tearDown(self):
        self._home.cleanup()

    def view(self, wf=None, **kw):
        return LiveView(wf or StubWorkflow(), clock=lambda: NOW.timestamp(), claude_home=self._home.name, **kw)

    def test_buckets_and_order(self):
        snap = self.view().snapshot()
        ids = [s['id'] for s in snap['sessions']]
        self.assertEqual(ids[0], 'codex:ask')  # needs you first
        self.assertEqual({s['bucket'] for s in snap['sessions'] if s['id'] in ('codex:work', 'codex:<img src=x onerror=alert(1)>')}, {'working'})
        self.assertNotIn('codex:old', ids)  # quiet sessions are hidden unless asked for
        self.assertEqual(snap['hiddenQuiet'], 1)
        self.assertEqual(snap['counts'], {'needs-you': 1, 'working': 3, 'recent': 1, 'old-request': 1, 'quiet': 1})
        backlog = next(s for s in snap['sessions'] if s['id'] == 'codex:backlog')
        self.assertEqual(backlog['bucket'], 'old-request')  # a two-week-old review is not a live interruption
        claude = next(s for s in snap['sessions'] if s['id'].startswith('claude:1111'))
        self.assertEqual(claude['title'], 'Fixing the board')  # the name Claude Code shows, not the raw id
        self.assertTrue(snap['passive'])
        work = next(s for s in snap['sessions'] if s['id'] == 'codex:work')
        self.assertEqual(work['role'], 'worker')
        self.assertEqual(work['activity']['planProgress'], {'completed': 2, 'total': 5})
        everything = self.view().snapshot(include_all=True)
        self.assertIn('codex:old', [s['id'] for s in everything['sessions']])

    def test_never_carries_prompts_commands_or_raw_detail(self):
        text = json.dumps(self.view().snapshot(include_all=True))
        for secret in ('SECRET-PROMPT', 'SECRET-COMMAND', 'A command was requested'):
            self.assertNotIn(secret, text)

    def test_one_snapshot_serves_every_viewer(self):
        wf = StubWorkflow()
        ticks = [0.0]
        view = LiveView(wf, ttl=2.0, clock=lambda: NOW.timestamp(), monotonic=lambda: ticks[0])
        for _ in range(10):
            view.snapshot()
        self.assertEqual(wf.calls, 1)
        ticks[0] = 2.5
        view.snapshot()
        self.assertEqual(wf.calls, 2)

    def test_unreadable_layout_does_not_break_the_view(self):
        wf = StubWorkflow()
        wf.read = lambda: (_ for _ in ()).throw(ValueError('Workflow state is incomplete'))
        snap = self.view(wf).snapshot()
        self.assertTrue(snap['sessions'])
        self.assertIsNone(snap['sessions'][0]['role'])

    def test_bucket_rules(self):
        self.assertEqual(bucket({'phase': 'review'}), 'needs-you')
        self.assertEqual(bucket({'phase': 'failed'}), 'needs-you')
        self.assertEqual(bucket({'phase': 'thinking', 'stale': False}), 'working')
        self.assertEqual(bucket({'phase': 'quiet', 'stale': True}), 'recent')
        self.assertEqual(bucket({'phase': 'unknown'}), 'quiet')
        self.assertEqual(bucket(None), 'quiet')


class NoteTests(unittest.TestCase):
    def test_note_saves_against_the_latest_layout(self):
        import tempfile
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
            flow.mutate({'operation': 'default', 'revision': 1, 'item': {'value': 'explicit-only'}})  # someone else edits first
            result = view.save_note('codex:a', '  Finish the records check first.  ')
            self.assertEqual(result['note'], 'Finish the records check first.')
            self.assertEqual(flow.context('a')['assignmentNote'], 'Finish the records check first.')
            self.assertEqual(flow.read()['defaultCommunication'], 'explicit-only')  # the other edit survived
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
        self.assertIn('visibilitychange', js)  # hidden tabs stop polling

    def test_renders_every_state_in_chromium(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('playwright not installed')
        snap = LiveView(StubWorkflow(), clock=lambda: NOW.timestamp()).snapshot()
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
                    page.wait_for_selector('.card')
                    text = page.inner_text('body').lower()
                    for needle in ('Needs you', 'Working now', 'Finished recently', 'Core Engineer', 'Running a command…',
                                   'Checking the app records', 'Mobile app', '<b>markup</b> & friends', '1 need you', '3 working now',
                                   'Waiting on you for days', '1 older request'):
                        self.assertIn(needle.lower(), text, f'{needle!r} at {width}px')
                    self.assertIn('last turn 12s', text)  # durations are rounded
                    self.assertNotIn('11.65', text)
                    self.assertFalse(page.evaluate("document.querySelector('.card b') !== null"))
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1'), f'overflow at {width}px')
                    if width == 1280:
                        page.click("text=Core Engineer")
                        link = page.get_attribute(".card.open a:has-text('Open in Codex')", 'href')
                        self.assertEqual(link, 'codex://threads/01a0dbd5-3da0-7690-9ad1-a88a22a7286e')
                        page.click(".card.open button:has-text('Leave a note')")
                        page.fill('.editor textarea', 'Please finish the records check first.')
                        page.wait_for_timeout(3500)  # a refresh must not wipe the note being written
                        self.assertEqual(page.input_value('.editor textarea'), 'Please finish the records check first.')
                        page.click("button:has-text('Save note')")
                        page.wait_for_timeout(500)
                        self.assertEqual(posted[-1], ('/api/live/note', 'fixture-token',
                                                      {'agentId': 'codex:work', 'text': 'Please finish the records check first.'}))
                    page.click("button[data-filter='now']")
                    self.assertNotIn('Board fixes', page.inner_text('main'))
                    page.fill('#search', 'owner')
                    self.assertEqual(page.eval_on_selector_all('.card', 'els => els.filter(e => e.offsetParent).length'), 1)
                    page.close()
                browser.close()
        finally:
            server.shutdown()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
