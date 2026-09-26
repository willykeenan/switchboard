"""Candidate Library service references. No launcher, placement writer or approvals.

The integrator supplies trusted topology and authorization adapters. A reference
limits Library scope; it does not override communication, source or runtime gates.
All mutations are serialized in the supplied database and retain audit history.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import sqlite3
import time


VERSION = "1.0.0"
SCHEMA = 1
KINDS = {"context", "research"}
OCCUPIED = {"active", "suspended"}


class Conflict(ValueError):
    pass


class Denied(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 400:
        raise Denied("Invalid " + name)
    return value


def safe_path(path):
    p = Path(path).absolute()
    if any(part.is_symlink() for part in (p, *p.parents)):
        raise Denied("Database paths must not traverse symlinks")
    return p


class LibraryServices:
    """One state store, one authoritative home, many scoped service references.

    topology() returns normalized current projects, lanes, research teams,
    provider identities, primary placements and authoritative queue references.
    authorize(principal, operation, payload, topology, scope) must be supplied by the
    trusted operator/dispatcher. An agent-authored principal is not authentication.
    scope is resolved from current stored identities, not client-supplied project claims.
    Schema initialization is explicit, so a reader never migrates a live store.
    """

    def __init__(self, database, topology, authorize, clock=time.time):
        if not callable(topology) or not callable(authorize):
            raise Denied("Trusted topology and authorization adapters required")
        self.path = safe_path(database)
        self.topology = topology
        self.authorize = authorize
        self.clock = clock

    @contextmanager
    def connection(self, write=False):
        safe_path(self.path)
        if not self.path.exists():
            raise Denied("Initialize the service schema at the migration boundary")
        db = sqlite3.connect(str(self.path) if write else self.path.as_uri() + "?mode=ro",
                             uri=not write, isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if write:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
        else:
            db.execute("BEGIN")
        try:
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        """Trusted schema migration only; never invoked by a read or constructor."""
        safe_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_service_meta'").fetchone()
            if exists:
                meta = db.execute("SELECT schema_version FROM library_service_meta WHERE singleton=1").fetchone()
                if not meta or meta[0] != SCHEMA:
                    raise Denied("Unsupported service schema version")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS library_service_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    schema_version INTEGER NOT NULL, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO library_service_meta VALUES(1,1,0);
                CREATE TABLE IF NOT EXISTS librarian_homes (
                    agent TEXT PRIMARY KEY, provider TEXT NOT NULL UNIQUE,
                    queue TEXT NOT NULL UNIQUE, project TEXT NOT NULL,
                    lane TEXT NOT NULL, team TEXT NOT NULL, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS library_services (
                    id TEXT PRIMARY KEY, agent TEXT NOT NULL REFERENCES librarian_homes(agent),
                    project TEXT NOT NULL, lane TEXT NOT NULL, team TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('context','research')),
                    state TEXT NOT NULL CHECK(state IN ('active','suspended','released')),
                    reason TEXT NOT NULL, successor TEXT NOT NULL,
                    version INTEGER NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS unique_library_service_seat
                    ON library_services(project,lane,team,kind)
                    WHERE state IN ('active','suspended');
                CREATE TABLE IF NOT EXISTS library_service_requests (
                    actor TEXT NOT NULL, request_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY(actor,request_key));
                CREATE TABLE IF NOT EXISTS library_service_events (
                    revision INTEGER PRIMARY KEY, body TEXT NOT NULL,
                    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
                COMMIT;
            """)
            if db.execute("SELECT schema_version FROM library_service_meta").fetchone()[0] != SCHEMA:
                raise Denied("Unsupported service schema version")

    @staticmethod
    def _lane(topology, project, lane, allow_inactive=False):
        if project not in topology["projects"]:
            raise Denied("Unknown project")
        item = topology["lanes"].get(lane)
        if not item or item["project"] != project:
            raise Denied("Unknown lane or cross-project service")
        if not allow_inactive and item.get("state") != "active":
            raise Denied("Lane is closed or merged")
        return item

    @classmethod
    def _home_valid(cls, topology, row):
        lane = cls._lane(topology, row["project"], row["lane"])
        if lane.get("function") != "library":
            raise Denied("Primary home must be a Library function")
        person = topology["agents"].get(row["agent"])
        if not person or person.get("provider") != row["provider"] or person.get("queue") != row["queue"]:
            raise Denied("Provider or queue is not the current authoritative identity")
        if person.get("primary") != {"project": row["project"], "lane": row["lane"], "team": row["team"]}:
            raise Denied("Primary Library placement differs from authoritative topology")
        team = topology["teams"].get(row["team"])
        if not team or team["lane"] != row["lane"]:
            raise Denied("Library home team is unavailable")

    @classmethod
    def _target_valid(cls, topology, row):
        lane = cls._lane(topology, row["project"], row["lane"])
        if lane.get("function") in {"library", "chief", "alignment", "audit"}:
            raise Denied("Target must be a lane research department or specialist team")
        if row["kind"] not in KINDS:
            raise Denied("Unknown librarian specialization")
        if row["team"] != "researcher":
            team = topology["teams"].get(row["team"])
            if not team or team["lane"] != row["lane"] or team.get("role") != "researcher" or team.get("serviceSeat"):
                raise Denied("Unknown research team or recursive librarian seat")

    @staticmethod
    def _entity(db, identifier):
        row = db.execute("SELECT * FROM library_services WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise Denied("Unknown service reference")
        return dict(row)

    @staticmethod
    def _state_hash(db):
        return digest({"homes": [dict(r) for r in db.execute("SELECT * FROM librarian_homes ORDER BY agent")],
                       "services": [dict(r) for r in db.execute("SELECT * FROM library_services ORDER BY id")]})

    def _authorization_scope(self, db, operation, payload):
        if operation in {"release", "resume", "retarget"}:
            text(payload.get("id"), "service id")
            row = self._entity(db, payload["id"])
            scope = {"project": row["project"], "agent": row["agent"],
                     "source": {"lane": row["lane"], "team": row["team"], "kind": row["kind"]}}
            if operation == "retarget":
                scope["target"] = {"lane": payload.get("lane"), "team": payload.get("team"), "kind": row["kind"]}
            return scope
        if operation not in {"register_home", "bind", "reconcile_lane"}:
            raise Denied("Unsupported operation")
        text(payload.get("project"), "project")
        return {"project": payload["project"], "agent": payload.get("agent"),
                "target": {k: payload[k] for k in ("lane", "team", "kind") if k in payload}}

    def apply(self, principal, key, expected_revision, operation, payload, *, expected_topology_revision):
        text(principal, "principal"); text(key, "request key")
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(payload, dict):
            raise Denied("Explicit revision and object payload required")
        request_hash = digest({"operation": operation, "payload": payload})
        with self.connection(write=True) as db:
            topology = self.topology()
            scope = self._authorization_scope(db, operation, payload)
            if self.authorize(principal, operation, payload, topology, scope) is not True:
                raise Denied("Current principal is not authorized")
            previous = db.execute("SELECT * FROM library_service_requests WHERE actor=? AND request_key=?", (principal, key)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("Request key already belongs to different work")
                return {**json.loads(previous["response"]), "replayed": True}
            if expected_topology_revision != topology["revision"]:
                raise Conflict("Workflow topology changed; reload before editing")
            revision = db.execute("SELECT revision FROM library_service_meta WHERE singleton=1").fetchone()[0]
            if revision != expected_revision:
                raise Conflict("Service store changed; reload before editing")
            try:
                result = self._mutate(db, topology, operation, payload)
            except sqlite3.IntegrityError as exc:
                raise Conflict("Identity, queue or service seat already has an owner") from exc
            revision += 1
            response = {"revision": revision, "result": result, "replayed": False}
            row = db.execute("SELECT event_hash FROM library_service_events ORDER BY revision DESC LIMIT 1").fetchone()
            previous_hash = row[0] if row else ""
            body = encoded({"revision": revision, "principal": principal, "requestKey": key,
                            "operation": operation, "payload": payload, "result": result,
                            "observedAt": self.clock(), "topologyRevision": topology["revision"],
                            "stateHash": self._state_hash(db)})
            event_hash = digest({"previous": previous_hash, "body": body})
            db.execute("INSERT INTO library_service_events VALUES(?,?,?,?)", (revision, body, previous_hash, event_hash))
            db.execute("UPDATE library_service_meta SET revision=? WHERE singleton=1", (revision,))
            db.execute("INSERT INTO library_service_requests VALUES(?,?,?,?)", (principal, key, request_hash, encoded(response)))
            return response

    def _mutate(self, db, topology, op, p):
        if op == "register_home":
            if set(p) != {"agent", "provider", "queue", "project", "lane", "team"}:
                raise Denied("Exact home fields required")
            for k, value in p.items(): text(value, k)
            self._home_valid(topology, p)
            existing = db.execute("SELECT * FROM librarian_homes WHERE agent=?", (p["agent"],)).fetchone()
            if existing:
                if any(existing[k] != v for k, v in p.items()):
                    raise Conflict("Existing authoritative home is immutable; use a reviewed migration")
                return dict(existing)
            db.execute("INSERT INTO librarian_homes VALUES(?,?,?,?,?,?,1)", tuple(p[k] for k in ("agent", "provider", "queue", "project", "lane", "team")))
            return {**p, "version": 1}
        if op == "bind":
            if set(p) != {"id", "agent", "project", "lane", "team", "kind"}:
                raise Denied("Exact service-reference fields required")
            for k, value in p.items(): text(value, k)
            home = db.execute("SELECT * FROM librarian_homes WHERE agent=?", (p["agent"],)).fetchone()
            if not home or home["project"] != p["project"]:
                raise Denied("Librarian has no home in this project")
            self._home_valid(topology, home); self._target_valid(topology, p)
            db.execute("INSERT INTO library_services VALUES(?,?,?,?,?,?,'active','','',1)", tuple(p[k] for k in ("id", "agent", "project", "lane", "team", "kind")))
            return self._entity(db, p["id"])
        if op == "reconcile_lane":
            if set(p) != {"project", "lane"}:
                raise Denied("Exact lane reconciliation fields required")
            for k, value in p.items(): text(value, k)
            lane = self._lane(topology, p["project"], p["lane"], allow_inactive=True)
            state = lane.get("state")
            if state not in {"active", "closed", "merged"}:
                raise Denied("Unknown lane lifecycle")
            changed = []
            if state != "active":
                rows = db.execute("SELECT id,state,reason,successor FROM library_services WHERE project=? AND lane=? AND state IN ('active','suspended')", (p["project"], p["lane"])).fetchall()
                reason, successor = "lane_" + state, lane.get("mergedInto", "") if state == "merged" else ""
                for row in rows:
                    if (row["state"], row["reason"], row["successor"]) == ("suspended", reason, successor):
                        continue
                    db.execute("UPDATE library_services SET state='suspended',reason=?,successor=?,version=version+1 WHERE id=?", (reason, successor, row["id"]))
                    changed.append(self._entity(db, row["id"]))
            return {"changed": changed, "automaticRetarget": False, "automaticResume": False}
        if op not in {"release", "resume", "retarget"}:
            raise Denied("Unsupported operation")
        expected_fields = {"id", "version", "reason"} | ({"lane", "team"} if op == "retarget" else set())
        if set(p) != expected_fields:
            raise Denied("Exact lifecycle fields required")
        text(p["reason"], "reason")
        if op == "retarget":
            text(p["lane"], "lane"); text(p["team"], "team")
        row = self._entity(db, p["id"])
        if type(p["version"]) is not int or row["version"] != p["version"]:
            raise Conflict("Service reference changed")
        if row["state"] == "released":
            raise Conflict("Released history cannot be reactivated")
        if op != "release":
            home = db.execute("SELECT * FROM librarian_homes WHERE agent=?", (row["agent"],)).fetchone()
            self._home_valid(topology, home)
            if op == "retarget": row.update(lane=p["lane"], team=p["team"])
            self._target_valid(topology, row)
        db.execute("UPDATE library_services SET lane=?,team=?,state=?,reason=?,successor='',version=version+1 WHERE id=?",
                   (row["lane"], row["team"], "released" if op == "release" else "active", p["reason"], row["id"]))
        return self._entity(db, row["id"])

    def snapshot(self, principal, project):
        """Authorized project read; current scope is checked without implicit writes."""
        topology = self.topology()
        text(principal, "principal"); text(project, "project")
        if self.authorize(principal, "read", {"project": project}, topology, {"project": project}) is not True:
            raise Denied("Project read is not authorized")
        if project not in topology["projects"]: raise Denied("Unknown project")
        with self.connection() as db:
            homes = [dict(r) for r in db.execute("SELECT * FROM librarian_homes WHERE project=? ORDER BY agent", (project,))]
            services = [dict(r) for r in db.execute("SELECT * FROM library_services WHERE project=? ORDER BY id", (project,))]
            for row in services:
                try:
                    home = next(h for h in homes if h["agent"] == row["agent"])
                    self._home_valid(topology, home); self._target_valid(topology, row)
                    row["scopeUsable"] = row["state"] == "active"
                    row["scopeBlocker"] = "" if row["scopeUsable"] else row["state"]
                except (Denied, StopIteration) as exc:
                    row.update(scopeUsable=False, scopeBlocker=str(exc))
            return {"revision": db.execute("SELECT revision FROM library_service_meta").fetchone()[0],
                    "topologyRevision": topology["revision"], "homes": homes, "services": services,
                    "capacityReferences": [{"agent": h["agent"], "provider": h["provider"], "queue": h["queue"]} for h in homes],
                    "runtimeReservationsCreated": 0, "primaryPlacementsCreated": 0}

    def can_serve(self, principal, agent, project, lane, team, kind):
        return any(r["agent"] == agent and r["lane"] == lane and r["team"] == team and r["kind"] == kind and r["scopeUsable"]
                   for r in self.snapshot(principal, project)["services"])

    def verify(self):
        with self.connection() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise Denied("Invalid recovered service database")
            previous = ""; revision = 0; last_body = None
            for row in db.execute("SELECT * FROM library_service_events ORDER BY revision"):
                revision += 1
                if row["revision"] != revision or row["previous_hash"] != previous or row["event_hash"] != digest({"previous": previous, "body": row["body"]}):
                    raise Denied("Service history failed integrity verification")
                body = json.loads(row["body"])
                request = db.execute("SELECT * FROM library_service_requests WHERE actor=? AND request_key=?", (body["principal"], body["requestKey"])).fetchone()
                if not request or request["request_hash"] != digest({"operation": body["operation"], "payload": body["payload"]}) or request["response"] != encoded({"revision": revision, "result": body["result"], "replayed": False}):
                    raise Denied("Request replay does not match retained history")
                last_body = body
                previous = row["event_hash"]
            meta = db.execute("SELECT * FROM library_service_meta").fetchone()
            if meta["schema_version"] != SCHEMA or meta["revision"] != revision:
                raise Denied("Service revision does not match retained history")
            expected_state = last_body["stateHash"] if last_body else digest({"homes": [], "services": []})
            if expected_state != self._state_hash(db):
                raise Denied("Current service state differs from retained history")
            if db.execute("SELECT COUNT(*) FROM library_service_requests").fetchone()[0] != revision:
                raise Denied("Unmatched request replay records")
            return {"ok": True, "schema": SCHEMA, "revision": revision, "historyHash": previous}

    def backup(self, destination):
        target = safe_path(destination)
        if target.exists(): raise Denied("Snapshot destination already exists")
        with self.connection() as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        return {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    @classmethod
    def restore(cls, snapshot, destination, topology, authorize):
        source, target = safe_path(snapshot), safe_path(destination)
        if target.exists(): raise Denied("Restore never overwrites an existing store")
        # Writable new target initializes any WAL sidecars before validation on macOS.
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
        result = cls(target, topology, authorize)
        result.verify()
        return result
