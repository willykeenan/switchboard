import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import demo
from taskflow import Conflict, TaskFlow
from test_taskflow import ARTIFACT_DELIVERY, Flow


class DemoSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        keys = ("SWITCHBOARD_ROOT", "SWITCHBOARD_HOME", "SWITCHBOARD_ROOMS_ROOT", "SWITCHBOARD_DISABLE_ROOM")
        self.saved_env = {key: os.environ.get(key) for key in keys}
        os.environ["SWITCHBOARD_ROOT"] = str(self.root)
        os.environ["SWITCHBOARD_HOME"] = str(self.root)
        os.environ["SWITCHBOARD_ROOMS_ROOT"] = str(self.root / "rooms")
        os.environ["SWITCHBOARD_DISABLE_ROOM"] = "1"

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_seed_invents_agents_rooms_and_workflow(self):
        result = demo.seed(self.root, self.root / "rooms")
        self.assertTrue(result["seeded"])
        self.assertEqual(4, result["agents"])
        with sqlite3.connect(self.root / "board.sqlite3") as db:
            self.assertEqual(4, db.execute("SELECT count(*) FROM agents").fetchone()[0])
        rooms = self.root / "rooms"
        self.assertTrue((rooms / "global" / "messages.jsonl").is_file())
        self.assertTrue((rooms / "demo-research" / "messages.jsonl").is_file())
        body = (rooms / "global" / "messages.jsonl").read_text()
        self.assertIn("codex:demo-owner", body)
        self.assertIn("claude:demo-researcher", body)
        second = demo.seed(self.root, self.root / "rooms")
        self.assertFalse(second["seeded"])

    def test_pid_contracts_are_refused(self):
        flow = Flow()
        store = TaskFlow(self.root, flow, lambda: 1000.0)
        store.save_policy(
            {
                "projectId": "p",
                "laneId": "alpha",
                "enabled": True,
                "autoReady": True,
                "scopes": [str(self.root)],
                "capabilities": ["general"],
                "maxMinutes": 5,
            }
        )
        with self.assertRaisesRegex(Conflict, "PID execution is not included"):
            store.capture(
                {
                    "request": "Write the requested result",
                    "key": "pid-one",
                    "projectId": "p",
                    "laneId": "alpha",
                    "delivery": ARTIFACT_DELIVERY,
                    "pidTask": {"outputs": [{"path": "out.txt"}]},
                }
            )


if __name__ == "__main__":
    unittest.main()
