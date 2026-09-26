#!/usr/bin/env python3

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


BOARD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOARD_DIR))


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ["SWITCHBOARD_ROOT"] = self.temp.name
        os.environ["SWITCHBOARD_DISABLE_ROOM"] = "1"
        sys.modules.pop("board_core", None)
        self.board = importlib.import_module("board_core")
        self.board.init_db()
        self.board.upsert_team("RESEARCH", "RESEARCH", "test")
        # These tests cover the legacy automatic owner assignment, which is off
        # until the operator switches routing to auto.
        from workflow import set_routing_mode
        set_routing_mode(self.temp.name, False, "test")

    def tearDown(self):
        self.temp.cleanup()

    def register(self, agent_id, priority=100, capabilities=("local-repair",), status="ACTIVE"):
        return self.board.register_agent(
            agent_id=agent_id,
            team="RESEARCH",
            provider="codex",
            endpoint=agent_id.split(":", 1)[-1],
            display_name=agent_id,
            capabilities=capabilities,
            writable_scopes=("/tmp/demo-board",),
            priority=priority,
            status=status,
            wake_mode="resume",
        )

    def incident(self, fingerprint="same"):
        return self.board.create_incident(
            team="RESEARCH",
            title="Dataset stopped",
            details="875 of 946 days completed",
            severity="critical",
            source="test",
            safe_action="Audit and resume only the failed dates",
            required_capability="local-repair",
            fingerprint=fingerprint,
        )

    def test_duplicate_incident_is_one_open_record(self):
        first = self.incident()
        second = self.incident()
        self.assertEqual(first["incident_id"], second["incident_id"])
        snapshot = self.board.snapshot()
        self.assertEqual(1, len(snapshot["incidents"]))
        self.assertEqual(1, snapshot["counts"]["open"])

    def test_dispatch_selects_exactly_one_best_capable_owner(self):
        self.register("codex:secondary", priority=50)
        self.register("codex:primary", priority=1)
        incident = self.incident()
        routed = self.board.wake_incident(incident["incident_id"])
        self.assertEqual("codex:primary", routed["assigned_agent_id"])
        self.assertEqual(1, routed["wake_attempts"])
        self.assertEqual("OWNER_ALREADY_ACTIVE", routed["wake_status"])
        agents = {item["agent_id"] for item in self.board.snapshot()["agents"]}
        self.assertEqual({"codex:primary", "codex:secondary"}, agents)

    def test_capability_filter_does_not_route_to_wrong_team_member(self):
        self.register("codex:research", priority=1, capabilities=("research",))
        self.register("codex:builder", priority=50, capabilities=("local-repair",))
        incident = self.incident("capability")
        routed = self.board.wake_incident(incident["incident_id"])
        self.assertEqual("codex:builder", routed["assigned_agent_id"])

    def test_human_gate_never_wakes_agent(self):
        self.register("codex:primary")
        incident = self.board.create_incident(
            team="RESEARCH",
            title="Purchase required",
            details="A paid source is required",
            safe_action="The operator decides whether to buy it",
            required_capability="local-repair",
            needs_operator=True,
        )
        routed = self.board.wake_incident(incident["incident_id"])
        self.assertEqual("BLOCKED_HUMAN", routed["status"])
        self.assertIsNone(routed["assigned_agent_id"])
        self.assertEqual(0, routed["wake_attempts"])

    def test_idle_owner_is_woken_once_without_fallback(self):
        self.register("codex:idle-owner", status="IDLE")
        incident = self.incident("idle-wake")
        with mock.patch.dict(os.environ, {"SWITCHBOARD_ALLOW_LAUNCH": "1"}), \
                mock.patch.object(self.board, "_wake_codex", return_value={"status": "WAKE_QUEUED", "pid": 42}) as wake:
            first = self.board.wake_incident(incident["incident_id"])
            second = self.board.wake_incident(incident["incident_id"])
        self.assertEqual(1, wake.call_count)
        self.assertEqual("WAKE_QUEUED", first["wake_status"])
        self.assertEqual(1, second["wake_attempts"])

    def test_resolved_fingerprint_can_open_again_later(self):
        first = self.incident("repeatable")
        self.board.resolve(first["incident_id"], actor="test", resolution="fixed")
        second = self.incident("repeatable")
        self.assertNotEqual(first["incident_id"], second["incident_id"])
        self.assertEqual(2, len(self.board.snapshot()["incidents"]))

    def test_terminal_monitor_publishes_once(self):
        state = Path(self.temp.name) / "worker-state.json"
        state.write_text(json.dumps({"status": "BLOCKED_SAFE", "detail": "71 failed", "record_hash": "abc"}))
        self.board.register_monitor(
            monitor_id="demo-test",
            team="RESEARCH",
            source_path=str(state),
            title="Dataset build",
            severity="critical",
            required_capability="local-repair",
            safe_action="Repair failed dates",
        )
        first = self.board.scan_monitors()
        event_count = len(self.board.snapshot()["events"])
        second = self.board.scan_monitors()
        self.assertEqual("FAILURE", first[0]["kind"])
        self.assertIsNotNone(first[0]["incident_id"])
        self.assertIsNone(second[0]["incident_id"])
        self.assertEqual(event_count, len(self.board.snapshot()["events"]))
        self.assertEqual(1, len(self.board.snapshot()["incidents"]))

    def test_stale_active_monitor_deduplicates(self):
        state = Path(self.temp.name) / "active.json"
        state.write_text(json.dumps({"status": "RUNNING", "detail": "working", "record_hash": "abc"}))
        old = time.time() - 30
        os.utime(state, (old, old))
        self.board.register_monitor(
            monitor_id="stale-test",
            team="RESEARCH",
            source_path=str(state),
            title="Worker",
            severity="warning",
            required_capability="local-repair",
            safe_action="Inspect the owner before any restart",
            stale_after_seconds=5,
        )
        first = self.board.scan_monitors()
        second = self.board.scan_monitors()
        self.assertEqual("STALE", first[0]["kind"])
        self.assertIsNone(second[0]["incident_id"])
        self.assertEqual(1, len(self.board.snapshot()["incidents"]))


if __name__ == "__main__":
    unittest.main()
