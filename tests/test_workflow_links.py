"""Agents open their own links; the operator's Blocks and closed lanes always win."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from test_workspace import fixture
from workflow import Workflow, allowed

BOARD = Path(__file__).resolve().parents[1]


class WorkflowLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'board'; self.w, _ = fixture(self.root)
        self.flow = Workflow(self.root, self.w, Path(self.tmp.name) / 'no-provider-store')
        state = {k: v for k, v in self.flow.read().items() if k != 'revision'}
        state.update(enabled=True, placements=[{'agentId': 'codex:a', 'laneId': 'alpha', 'role': 'writer'},
                                               {'agentId': 'codex:b', 'laneId': 'beta', 'role': 'worker'},
                                               {'agentId': 'codex:review', 'laneId': 'alpha', 'role': 'auditor'}])
        self.flow.save(state, 0, 'fixture')

    def tearDown(self): self.tmp.cleanup()

    def room(self):
        return [json.loads(l) for l in (self.root / 'rooms' / 'global' / 'messages.jsonl').read_text().splitlines() if l.strip()]

    def test_agent_links_itself_both_ways_and_held_work_releases(self):
        self.assertEqual('HELD', self.flow.send('codex:a', 'codex:b', 'Please build the renderer', 'k1')['status'])
        r = self.flow.link('codex:a', 'codex:b', 'Renderer integration needs Beta')
        self.assertEqual([{'from': 'codex:a', 'to': 'codex:b'}, {'from': 'codex:b', 'to': 'codex:a'}], r['added'])
        self.assertTrue(r['mayMessage']); self.assertTrue(r['mayReceive'])
        state = self.flow.read()
        self.assertTrue(allowed(state, 'codex:a', 'codex:b')); self.assertTrue(allowed(state, 'codex:b', 'codex:a'))
        self.assertEqual('QUEUED', self.flow.messages()[0]['status'])  # the earlier held request now delivers
        self.assertEqual(1, len(self.flow.context('b')['inbox']))
        self.assertEqual(2, state['revision'])
        import sqlite3
        with sqlite3.connect(self.flow.path) as db:
            self.assertEqual('agent-link:codex:a', db.execute('SELECT actor FROM workflow_history WHERE revision=2').fetchone()[0])
        self.assertEqual(r['id'], self.flow.links('codex:b')[0]['id'])
        self.assertEqual('Renderer integration needs Beta', self.flow.links()[0]['reason'])

    def test_link_is_announced_to_the_operator(self):
        r = self.flow.link('codex:a', 'codex:b', 'Need Beta for integration')
        self.assertTrue(r['roomNotice']['posted'], r['roomNotice'])
        post = self.room()[-1]
        self.assertEqual(['operator'], post['recipients'])
        self.assertIn('Need Beta for integration', post['body'])
        self.assertIn('Block', post['body'])
        self.assertEqual(r['roomNotice']['seq'], self.flow.links()[0]['room_seq'])

    def test_operator_block_always_wins(self):
        self.flow.mutate({'operation': 'connection', 'revision': 1, 'item': {'from': 'codex:b', 'to': 'codex:a', 'allow': False}})
        with self.assertRaisesRegex(ValueError, 'operator blocked codex:b -> codex:a'):
            self.flow.link('codex:a', 'codex:b', 'Try anyway')
        state = self.flow.read()
        self.assertEqual(2, state['revision'])  # nothing partially applied
        self.assertFalse(allowed(state, 'codex:a', 'codex:b'))
        self.assertEqual([], self.flow.links())
        r = self.flow.link('codex:a', 'codex:b', 'One way is enough', both=False)  # the unblocked direction is still allowed
        self.assertEqual([{'from': 'codex:a', 'to': 'codex:b'}], r['added'])
        self.assertFalse(allowed(self.flow.read(), 'codex:b', 'codex:a'))

    def test_closed_lane_wins(self):
        from workflow_layout import lanes_for
        state = self.flow.read(); data = {k: state[k] for k in state if k != 'revision'}
        lanes = lanes_for(data, self.w.read()['lanes'])
        for lane in lanes:
            if lane['id'] == 'beta': lane['lifecycle'] = 'closed'
        data['laneCatalog'] = lanes
        try:
            self.flow.save(data, state['revision'], 'fixture')
        except ValueError as exc:
            self.skipTest('fixture cannot close a lane: ' + str(exc))
        with self.assertRaisesRegex(ValueError, 'closed lane'):
            self.flow.link('codex:a', 'codex:b', 'Blocked by closure')

    def test_idempotent_and_validated(self):
        self.flow.link('codex:a', 'codex:b', 'First')
        again = self.flow.link('codex:a', 'codex:b', 'Second')
        self.assertEqual([], again['added']); self.assertEqual(1, len(self.flow.links()))
        with self.assertRaises(ValueError): self.flow.link('codex:a', 'codex:a', 'Self')
        with self.assertRaises(ValueError): self.flow.link('codex:a', 'codex:nobody', 'Unknown')
        with self.assertRaises(ValueError): self.flow.link('codex:a', 'codex:review', '   ')
        with self.assertRaises(ValueError): self.flow.link('codex:a', 'codex:review', 'x' * 1001)

    def test_unlink_removes_only_agent_opened_edges(self):
        self.flow.mutate({'operation': 'connection', 'revision': 1, 'item': {'from': 'codex:b', 'to': 'codex:review', 'allow': True}})
        self.flow.link('codex:b', 'codex:a', 'Pair up')
        self.flow.link('codex:b', 'codex:review', 'Already allowed one way')  # adds only review -> b
        r = self.flow.unlink('codex:b', 'codex:review')
        self.assertEqual(1, r['unlinked'])
        state = self.flow.read()
        self.assertTrue(allowed(state, 'codex:b', 'codex:review'))  # the operator's own edge stays
        self.assertFalse(allowed(state, 'codex:review', 'codex:b'))  # the agent-opened direction is gone (different lanes)
        self.assertTrue(allowed(state, 'codex:a', 'codex:b'))  # the other link stays
        self.flow.unlink('codex:a', 'codex:b')  # either side of the pair may remove it
        self.assertFalse(allowed(self.flow.read(), 'codex:a', 'codex:b'))
        self.assertEqual(0, self.flow.unlink('codex:a', 'codex:b')['unlinked'])

    def test_context_teaches_self_link(self):
        ctx = self.flow.context('a')
        self.assertIn('workflowctl.py link --to', ctx['selfLink']['command'])
        self.assertIn('Blocks', ctx['selfLink']['rule'])

    def ctl(self, *args, env_extra=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ('CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID') and not k.startswith('SWITCHBOARD_')}
        env.update(SWITCHBOARD_ROOT=str(self.root), SWITCHBOARD_WORKFLOW_CODEX_HOME=str(Path(self.tmp.name) / 'no-provider-store'),
                   SWITCHBOARD_WORKFLOW_FIXTURE='1', **(env_extra or {}))
        r = subprocess.run([sys.executable, str(BOARD / 'workflowctl.py'), *args], env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, json.loads(r.stdout or '{}')

    def test_cli_links_only_the_calling_agent(self):
        code, out = self.ctl('link', '--to', 'codex:b', '--reason', 'No identity')
        self.assertEqual(1, code); self.assertIn('identity', out['error'])
        code, out = self.ctl('link', '--from', 'codex:review', '--to', 'codex:b', '--reason', 'Spoof', env_extra={'CODEX_THREAD_ID': 'a'})
        self.assertEqual(1, code); self.assertIn('identity', out['error'])
        self.assertEqual([], self.flow.links())
        code, out = self.ctl('link', '--to', 'codex:b', '--reason', 'Real', env_extra={'CODEX_THREAD_ID': 'a'})
        self.assertEqual(0, code, out); self.assertTrue(out['linked'])
        self.assertEqual('codex:a', self.flow.links()[0]['actor'])
        code, out = self.ctl('links', '--session', 'codex:b')
        self.assertEqual(1, len(out['links']))
        code, out = self.ctl('unlink', '--to', 'codex:a', env_extra={'CODEX_THREAD_ID': 'b'})
        self.assertEqual(0, code, out); self.assertEqual(2, out['unlinked'])


if __name__ == '__main__':
    unittest.main()
