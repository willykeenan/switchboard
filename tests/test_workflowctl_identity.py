"""workflowctl send / read-ack / link act only as the calling Codex or Claude session."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from test_workspace import fixture
from workflow import Workflow

BOARD = Path(__file__).resolve().parents[1]


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'board'; self.w, _ = fixture(self.root)
        with sqlite3.connect(self.root / 'board.sqlite3') as db:
            db.execute('INSERT INTO agents VALUES(?,?,?,?,?,?)', ('claude:c1', 'c1', 'claude', 'Claude one', 'ACTIVE', '2000-01-01T00:00:00Z'))
        self.flow = Workflow(self.root, self.w, Path(self.tmp.name) / 'no-provider-store')
        state = {k: v for k, v in self.flow.read().items() if k != 'revision'}
        state.update(enabled=True, placements=[{'agentId': 'codex:a', 'laneId': 'alpha', 'role': 'writer'},
                                               {'agentId': 'claude:c1', 'laneId': 'alpha', 'role': 'worker'},
                                               {'agentId': 'codex:review', 'laneId': 'alpha', 'role': 'auditor'}])
        self.flow.save(state, 0, 'fixture')

    def tearDown(self): self.tmp.cleanup()

    def ctl(self, *args, **ident):
        env = {k: v for k, v in os.environ.items()
               if k not in ('CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID') and not k.startswith('SWITCHBOARD_')}
        env.update(SWITCHBOARD_ROOT=str(self.root), SWITCHBOARD_WORKFLOW_CODEX_HOME=str(Path(self.tmp.name) / 'no-provider-store'),
                   SWITCHBOARD_WORKFLOW_FIXTURE='1', **ident)
        r = subprocess.run([sys.executable, str(BOARD / 'workflowctl.py'), *args], env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, json.loads(r.stdout or '{}')

    def test_claude_cannot_send_as_another_agent(self):
        code, out = self.ctl('send', '--from', 'codex:a', '--to', 'codex:review', '--message', 'spoof', '--key', 'k1',
                             '--notification', CLAUDE_CODE_SESSION_ID='c1')
        self.assertEqual(1, code); self.assertIn('exact Codex or Claude session', out['error'])
        code, out = self.ctl('send', '--from', 'claude:c1', '--to', 'codex:review', '--message', 'real', '--key', 'k2',
                             '--notification', CLAUDE_CODE_SESSION_ID='c1')
        self.assertEqual(0, code, out); self.assertEqual('QUEUED', out['status'])

    def test_codex_rule_unchanged(self):
        code, out = self.ctl('send', '--from', 'codex:review', '--to', 'codex:a', '--message', 'x', '--key', 'k3',
                             '--notification', CODEX_THREAD_ID='a')
        self.assertEqual(1, code)
        code, out = self.ctl('send', '--from', 'codex:a', '--to', 'codex:review', '--message', 'x', '--key', 'k4',
                             '--notification', CODEX_THREAD_ID='a')
        self.assertEqual(0, code, out)

    def test_claude_read_ack_with_bare_session_id(self):
        mid = self.flow.send('codex:a', 'claude:c1', 'hello', 'k5', intent='notification')['id']
        code, out = self.ctl('read-ack', '--session', 'codex:a', '--message-id', mid, CLAUDE_CODE_SESSION_ID='c1')
        self.assertEqual(1, code); self.assertIn('another session', out['error'])
        code, out = self.ctl('read-ack', '--session', 'c1', '--message-id', mid, CLAUDE_CODE_SESSION_ID='c1')
        self.assertEqual(0, code, out); self.assertEqual(mid, out['acknowledged'])

    def test_no_inherited_identity_keeps_operator_cli_behaviour(self):
        code, out = self.ctl('send', '--from', 'codex:a', '--to', 'codex:review', '--message', 'x', '--key', 'k6', '--notification')
        self.assertEqual(0, code, out)


if __name__ == '__main__':
    unittest.main()
