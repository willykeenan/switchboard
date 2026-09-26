"""Disposable behavioral tests; never reads or writes Switchboard runtime stores."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from library_services import LibraryServices, Conflict, Denied


def fixture():
    t = {"revision": 10, "projects": ["p1", "p2"], "lanes": {}, "teams": {}, "agents": {}}
    for p in t["projects"]:
        lib = "library-" + p
        t["lanes"][lib] = {"project": p, "state": "active", "function": "library"}
        t["teams"][lib + "-staff"] = {"lane": lib, "role": "coordinator"}
        for n in (1, 2):
            lane = p + "-lane" + str(n)
            t["lanes"][lane] = {"project": p, "state": "active"}
            t["teams"][lane + "-specialists"] = {"lane": lane, "role": "researcher"}
        for suffix in ("a", "b"):
            agent = p + "-" + suffix
            t["agents"][agent] = {"provider": "fixture:" + agent, "queue": "fixture-queue:" + agent,
                "primary": {"project": p, "lane": lib, "team": lib + "-staff"}}
    return t


class ServicesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="library-services-test-")
        self.root = Path(self.temp.name).resolve()
        self.topology = fixture()
        self.allowed = {"operator-p1": {"p1"}, "operator-p2": {"p2"}}
        self.auth_calls = []
        self.s = self.store(self.root / "service.sqlite3")
        self.s.initialize()
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def authorize(self, who, op, payload, topology, scope):
        self.auth_calls.append({"principal": who, "operation": op, "scope": deepcopy(scope)})
        return scope.get("project") in self.allowed.get(who, set())

    def store(self, path):
        return LibraryServices(path, lambda: deepcopy(self.topology), self.authorize, clock=lambda: 100.0)

    def revision(self):
        return self.s.verify()["revision"]

    def mutate(self, op, p, key=None, actor="operator-p1", revision=None, topology_revision=None):
        self.counter += 1
        return self.s.apply(actor, key or "request-" + str(self.counter),
            self.revision() if revision is None else revision, op, p,
            expected_topology_revision=self.topology["revision"] if topology_revision is None else topology_revision)

    def home(self, agent="p1-a", key=None):
        person = self.topology["agents"][agent]
        p = {"agent": agent, "provider": person["provider"], "queue": person["queue"], **person["primary"]}
        return self.mutate("register_home", p, key=key, actor="operator-" + p["project"])

    def bind(self, id="s1", agent="p1-a", lane="p1-lane1", team="researcher", kind="context", key=None):
        return self.mutate("bind", {"id": id, "agent": agent, "project": lane[:2], "lane": lane, "team": team, "kind": kind},
                           key=key, actor="operator-" + lane[:2])

    def snap(self, project="p1"):
        return self.s.snapshot("operator-" + project, project)

    def usable(self, lane="p1-lane1", team="researcher", kind="context"):
        return self.s.can_serve("operator-p1", "p1-a", "p1", lane, team, kind)

    def test_valid_department_and_specialist_service_share_one_home_and_capacity(self):
        self.home()
        self.bind()
        self.bind("s2", team="p1-lane1-specialists", kind="research")
        self.bind("s3", lane="p1-lane2")
        state = self.snap()
        self.assertEqual(len(state["services"]), 3)
        self.assertEqual(len(state["homes"]), 1)
        self.assertEqual(len(state["capacityReferences"]), 1)
        self.assertEqual(state["capacityReferences"][0]["queue"], "fixture-queue:p1-a")
        self.assertEqual(state["primaryPlacementsCreated"], 0)
        self.assertEqual(state["runtimeReservationsCreated"], 0)
        self.assertTrue(self.usable())
        self.assertTrue(self.usable(team="p1-lane1-specialists", kind="research"))
        self.assertFalse(self.usable(kind="research"))
        self.assertEqual(self.topology, fixture())

    def test_duplicate_request_replays_original_without_new_event(self):
        first = self.home(key="same")
        p = {k: v for k, v in first["result"].items() if k != "version"}
        replay = self.mutate("register_home", p, key="same", revision=0, topology_revision=-1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], first["result"])
        self.assertEqual(self.revision(), 1)
        changed = dict(p, queue="different")
        with self.assertRaises(Conflict):
            self.mutate("register_home", changed, key="same")
        self.assertEqual(self.revision(), 1)

    def test_replay_rechecks_revoked_authority(self):
        result = self.home(key="same")
        self.allowed["operator-p1"].clear()
        with self.assertRaises(Denied):
            self.mutate("register_home", {k:v for k,v in result["result"].items() if k != "version"}, key="same")
        self.assertEqual(self.revision(), 1)

    def test_unique_active_seat_does_not_prevent_distinct_specializations(self):
        self.home(); self.home("p1-b"); self.bind()
        before = self.s.verify()
        with self.assertRaises(Conflict): self.bind("s2", agent="p1-b")
        self.assertEqual(self.s.verify(), before)
        self.bind("s2", agent="p1-b", kind="research")
        self.assertEqual(len(self.snap()["services"]), 2)

    def test_home_provider_and_queue_collisions_are_atomic(self):
        self.home()
        for field in ("provider", "queue"):
            original = deepcopy(self.topology["agents"]["p1-b"])
            self.topology["agents"]["p1-b"][field] = self.topology["agents"]["p1-a"][field]
            with self.assertRaises(Conflict): self.home("p1-b")
            self.assertEqual(self.revision(), 1)
            self.topology["agents"]["p1-b"] = original

    def test_changed_authoritative_home_requires_reviewed_migration(self):
        self.home()
        self.topology["agents"]["p1-a"]["queue"] = "replacement-queue"
        with self.assertRaises(Conflict): self.home()
        self.assertEqual(self.revision(), 1)

    def test_unknown_identity_wrong_primary_or_nonlibrary_home_denied(self):
        base = deepcopy(self.topology)
        for scenario in ("provider", "primary", "function", "state"):
            self.topology = deepcopy(base)
            person = self.topology["agents"]["p1-a"]
            p = {"agent": "p1-a", "provider": person["provider"], "queue": person["queue"], **person["primary"]}
            if scenario == "provider": p["provider"] = "invented"
            if scenario == "primary": person["primary"]["team"] = "different"
            if scenario == "function": self.topology["lanes"]["library-p1"]["function"] = "chief"
            if scenario == "state": del self.topology["lanes"]["library-p1"]["state"]
            with self.subTest(scenario=scenario), self.assertRaises(Denied): self.mutate("register_home", p)
            self.assertEqual(self.revision(), 0)

    def test_project_scope_rejects_crossproject_read_bind_and_lifecycle(self):
        self.home(); self.home("p2-a")
        self.bind(id="p2-service", agent="p2-a", lane="p2-lane1")
        with self.assertRaises(Denied): self.s.snapshot("operator-p1", "p2")
        with self.assertRaises(Denied): self.bind(id="cross", lane="p2-lane1", kind="research")
        with self.assertRaises(Denied): self.mutate("release", {"id":"p2-service", "version":1, "reason":"not my project"})
        self.assertEqual(self.auth_calls[-1]["scope"]["project"], "p2")
        self.assertEqual(self.snap("p2")["services"][0]["state"], "active")

    def test_missing_home_wrong_department_and_recursive_service_seat_denied(self):
        with self.assertRaises(Denied): self.bind()
        self.home()
        self.topology["teams"]["wrong-role"] = {"lane":"p1-lane1", "role":"writer"}
        self.topology["teams"]["recursive"] = {"lane":"p1-lane1", "role":"researcher", "serviceSeat":True}
        for team in ("missing", "wrong-role", "recursive", "p2-lane1-specialists"):
            with self.subTest(team=team), self.assertRaises(Denied): self.bind(team=team)
        self.assertEqual(self.revision(), 1)

    def test_store_topology_and_entity_versions_all_reject_stale_edits(self):
        self.home(); self.bind()
        p={"id":"s1", "version":1, "reason":"reviewed"}
        with self.assertRaises(Conflict): self.mutate("release", p, revision=1)
        with self.assertRaises(Conflict): self.mutate("release", p, topology_revision=9)
        with self.assertRaises(Conflict): self.mutate("release", dict(p, version=0))
        self.assertTrue(self.usable())
        self.assertEqual(self.revision(), 2)

    def test_closed_lane_denies_immediately_without_mutating_on_read(self):
        self.home(); self.bind()
        before = self.s.verify()
        self.topology["lanes"]["p1-lane1"]["state"] = "closed"
        self.assertFalse(self.usable())
        self.assertIn("closed", self.snap()["services"][0]["scopeBlocker"])
        self.assertEqual(self.s.verify(), before)
        result = self.mutate("reconcile_lane", {"project":"p1", "lane":"p1-lane1"})
        self.assertEqual(result["result"]["changed"][0]["state"], "suspended")
        self.assertFalse(result["result"]["automaticResume"])

    def test_reopen_needs_explicit_resume_and_suspended_seat_stays_reserved(self):
        self.home(); self.home("p1-b"); self.bind()
        self.topology["lanes"]["p1-lane1"]["state"] = "closed"
        self.mutate("reconcile_lane", {"project":"p1", "lane":"p1-lane1"})
        self.topology["lanes"]["p1-lane1"]["state"] = "active"
        self.mutate("reconcile_lane", {"project":"p1", "lane":"p1-lane1"})
        self.assertFalse(self.usable())
        with self.assertRaises(Conflict): self.bind("s2", "p1-b")
        self.mutate("resume", {"id":"s1", "version":2, "reason":"reviewed reopened scope"})
        self.assertTrue(self.usable())

    def test_merge_retains_history_and_requires_explicit_noncolliding_destination(self):
        self.home(); self.bind(); self.bind("s2", lane="p1-lane2")
        self.topology["lanes"]["p1-lane1"].update(state="merged", mergedInto="p1-lane2")
        self.assertFalse(self.usable())
        self.mutate("reconcile_lane", {"project":"p1", "lane":"p1-lane1"})
        row=self.snap()["services"][0]
        self.assertEqual((row["lane"],row["state"],row["successor"]), ("p1-lane1","suspended","p1-lane2"))
        p={"id":"s1", "version":2, "reason":"reviewed destination", "lane":"p1-lane2", "team":"researcher"}
        before = self.s.verify()
        with self.assertRaises(Conflict): self.mutate("retarget", p)
        self.assertEqual(self.s.verify(), before)
        with self.assertRaises(Denied): self.mutate("retarget", dict(p, lane="p2-lane1"))
        self.mutate("retarget", dict(p, team="p1-lane2-specialists"))
        self.assertTrue(self.usable(lane="p1-lane2", team="p1-lane2-specialists"))
        with self.s.connection() as db:
            history=[json.loads(r[0]) for r in db.execute("SELECT body FROM library_service_events ORDER BY revision")]
        self.assertTrue(any(e["operation"]=="bind" and e["result"]["lane"]=="p1-lane1" for e in history))

    def test_release_preserves_record_allows_replacement_never_resurrects(self):
        self.home(); self.home("p1-b"); self.bind()
        self.mutate("release", {"id":"s1", "version":1, "reason":"service reassigned"})
        self.bind("s2", agent="p1-b")
        with self.assertRaises(Conflict): self.mutate("resume", {"id":"s1", "version":2, "reason":"resurrect"})
        rows=self.snap()["services"]
        self.assertEqual([(r["id"],r["state"]) for r in rows], [("s1","released"),("s2","active")])

    def test_close_then_merge_updates_suspended_metadata_once_and_preserves_history(self):
        self.home(); self.bind()
        p={"project":"p1", "lane":"p1-lane1"}
        self.topology["lanes"]["p1-lane1"]["state"]="closed"
        self.mutate("reconcile_lane",p)
        self.topology["lanes"]["p1-lane1"].update(state="merged",mergedInto="p1-lane2")
        changed=self.mutate("reconcile_lane",p)["result"]["changed"]
        self.assertEqual(len(changed),1)
        row=changed[0]
        self.assertEqual((row["lane"],row["reason"],row["successor"],row["version"]),("p1-lane1","lane_merged","p1-lane2",3))
        self.assertFalse(self.usable())
        self.assertEqual(self.mutate("reconcile_lane",p)["result"]["changed"],[])
        self.assertEqual(self.snap()["services"][0]["version"],3)
        with self.s.connection() as db:
            transitions=[json.loads(r[0])["result"].get("changed",[]) for r in db.execute("SELECT body FROM library_service_events ORDER BY revision")]
        reasons=[row["reason"] for changes in transitions for row in changes]
        self.assertEqual(reasons,["lane_closed","lane_merged"])

    def test_stale_primary_or_deleted_team_invalidates_scope_without_rewriting_history(self):
        self.home(); self.bind(team="p1-lane1-specialists")
        before=self.s.verify()
        self.topology["agents"]["p1-a"]["primary"]["team"]="elsewhere"
        self.assertFalse(self.usable(team="p1-lane1-specialists"))
        self.topology=fixture()
        del self.topology["teams"]["p1-lane1-specialists"]
        self.assertFalse(self.usable(team="p1-lane1-specialists"))
        self.assertEqual(self.s.verify(),before)

    def test_two_concurrent_store_clients_one_seat_one_winner(self):
        self.home(); self.home("p1-b")
        revision=self.revision(); barrier=threading.Barrier(2)
        def compete(n):
            s=self.store(self.s.path)
            p={"id":"race"+str(n), "agent":"p1-"+"ab"[n], "project":"p1", "lane":"p1-lane1", "team":"researcher", "kind":"context"}
            barrier.wait(timeout=5)
            try:
                return s.apply("operator-p1","race"+str(n),revision,"bind",p,expected_topology_revision=10)
            except Conflict as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(compete,range(2)))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(len(self.snap()["services"]),1)
        self.assertEqual(self.revision(),revision+1)

    def test_snapshot_is_a_consistent_read_transaction(self):
        self.home()
        # WAL permits concurrent writer while a reader retains its original snapshot.
        # macOS SQLite requires initialized WAL sidecars for mode=ro access.
        writer = sqlite3.connect(self.s.path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE library_service_meta SET revision=revision")
            writer.commit()
            with self.s.connection() as reader:
                self.assertEqual(reader.execute("SELECT revision FROM library_service_meta").fetchone()[0],1)
                self.bind()
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM library_services").fetchone()[0],0)
                self.assertEqual(reader.execute("SELECT revision FROM library_service_meta").fetchone()[0],1)
        finally:
            writer.close()
        self.assertEqual(len(self.snap()["services"]),1)

    def test_backup_restore_preserves_history_and_replay_without_affecting_source(self):
        first=self.home(key="home"); self.bind()
        expected=self.snap(); before=self.s.verify()
        backup=self.s.backup(self.root/"backup.sqlite3")
        restored=LibraryServices.restore(backup["path"],self.root/"restored.sqlite3",lambda:deepcopy(self.topology),self.authorize)
        self.assertEqual(restored.verify(),before)
        self.assertEqual(restored.snapshot("operator-p1","p1"),expected)
        replay=restored.apply("operator-p1","home",0,"register_home",{k:v for k,v in first["result"].items() if k!="version"},expected_topology_revision=0)
        self.assertTrue(replay["replayed"])
        restored.apply("operator-p1","release",2,"release",{"id":"s1","version":1,"reason":"disposable test"},expected_topology_revision=10)
        self.assertTrue(self.usable())
        self.assertEqual(self.s.verify(),before)
        with self.assertRaises(Denied): LibraryServices.restore(backup["path"],self.s.path,lambda:self.topology,self.authorize)
        with self.assertRaises(Denied): self.s.backup(backup["path"])

    def test_recovery_rejects_changed_state_history_and_replay(self):
        self.home(); self.bind()
        statements=["UPDATE library_services SET reason='unlogged'", "UPDATE library_service_events SET body='{}' WHERE revision=1", "UPDATE library_service_requests SET response='{}' WHERE request_key='request-1'"]
        for n,sql in enumerate(statements):
            backup=self.s.backup(self.root/("corrupt"+str(n)+".sqlite3"))
            with sqlite3.connect(backup["path"]) as db: db.execute(sql)
            with self.subTest(sql=sql), self.assertRaises(Denied):
                LibraryServices.restore(backup["path"],self.root/("invalid"+str(n)+".sqlite3"),lambda:self.topology,self.authorize)
        self.assertTrue(self.usable())

    def test_empty_history_rejects_unlogged_home(self):
        with sqlite3.connect(self.s.path) as db:
            db.execute("INSERT INTO librarian_homes VALUES('ghost','fixture:ghost','queue:ghost','p1','library-p1','library-p1-staff',1)")
        with self.assertRaises(Denied): self.s.verify()

    def test_constructor_and_read_do_not_initialize_missing_store(self):
        other=self.store(self.root/"absent.sqlite3")
        self.assertFalse(other.path.exists())
        with self.assertRaises(Denied): other.snapshot("operator-p1","p1")
        self.assertFalse(other.path.exists())

    def test_symlink_and_unsupported_schema_are_rejected(self):
        link=self.root/"link.sqlite3"; link.symlink_to(self.s.path)
        with self.assertRaises(Denied): self.store(link)
        with sqlite3.connect(self.s.path) as db: db.execute("UPDATE library_service_meta SET schema_version=99")
        with self.assertRaises(Denied): self.s.initialize()
        with self.assertRaises(Denied): self.s.verify()


if __name__ == "__main__":
    unittest.main(verbosity=2)
