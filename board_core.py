#!/usr/bin/env python3
"""Durable local incident board and one-owner agent router.

The board is deliberately provider-light. It records every incident before any
wake attempt, assigns one eligible registered owner transactionally, mirrors
the event to the global agent room, and never creates fallback/companion agents.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import coordination


from store import data_root, launch_allowed, LAUNCH_ENV

ROOT = data_root()
DB_PATH = ROOT / "board.sqlite3"
ROOM_WRITER = Path(os.environ.get("SWITCHBOARD_ROOM_WRITER", ""))
WAKE_LOG_DIR = ROOT / "runtime" / "wake-logs"
ACTIVE_HEARTBEAT_SECONDS = 180


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def json_list(value: Optional[Iterable[str]]) -> str:
    return canonical(sorted({str(item).strip().lower() for item in (value or []) if str(item).strip()}))


def decode_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _enable_wal(connection) -> None:
    """Resolve competing first-open mode switches within the existing 30s limit.

    SQLite can return BUSY immediately while another connection changes journal
    mode. Only this idempotent setup statement is retried; no task or write
    transaction is replayed. Each call consumes the same monotonic deadline.
    """
    from store import busy
    deadline = time.monotonic() + 30
    while True:
        remaining = max(0, deadline - time.monotonic())
        connection.execute("PRAGMA busy_timeout=" + str(int(remaining * 1000)))
        try:
            cursor = connection.execute("PRAGMA journal_mode=WAL")
            try:
                mode = cursor.fetchone()[0]
            finally:
                cursor.close()
            if str(mode).lower() != "wal":
                raise sqlite3.OperationalError("Board database did not enter WAL mode")
            return
        except sqlite3.OperationalError as exc:
            remaining = deadline - time.monotonic()
            if not busy(exc) or remaining <= 0:
                raise
            time.sleep(min(.01, remaining))


def connect() -> sqlite3.Connection:
    from store import ClosingConnection
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(DB_PATH, timeout=30, factory=ClosingConnection)
    try:
        connection.row_factory = sqlite3.Row
        _enable_wal(connection)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection
    except BaseException:
        connection.close()
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    team TEXT NOT NULL REFERENCES teams(name),
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    display_name TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    writable_scopes_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'IDLE',
    wake_mode TEXT NOT NULL DEFAULT 'room',
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    team TEXT NOT NULL REFERENCES teams(name),
    title TEXT NOT NULL,
    details TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    safe_action TEXT NOT NULL,
    required_capability TEXT NOT NULL,
    needs_operator INTEGER NOT NULL DEFAULT 0,
    assigned_agent_id TEXT REFERENCES agents(agent_id),
    wake_status TEXT,
    wake_attempts INTEGER NOT NULL DEFAULT 0,
    last_wake_at TEXT,
    acknowledged_at TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_open_fingerprint
ON incidents(fingerprint) WHERE status != 'RESOLVED';

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    team TEXT,
    incident_id TEXT,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitors (
    monitor_id TEXT PRIMARY KEY,
    team TEXT NOT NULL REFERENCES teams(name),
    source_path TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    required_capability TEXT NOT NULL,
    safe_action TEXT NOT NULL,
    status_field TEXT NOT NULL,
    detail_field TEXT NOT NULL,
    record_hash_field TEXT NOT NULL,
    failure_statuses_json TEXT NOT NULL,
    success_statuses_json TEXT NOT NULL,
    active_statuses_json TEXT NOT NULL,
    stale_after_seconds INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fingerprint TEXT,
    last_scan_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _schema_ready(connection) -> bool:
    """Probe only metadata, never history; no process cache or mutation on reads.

    Re-evaluate the declared schema each time so a new object, replaced database,
    or incomplete migration takes the ordinary initializer below.
    """
    expected={(name,kind.lower()) for kind,name in re.findall(
        r'CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+(\w+)',
        SCHEMA + coordination.SCHEMA, re.IGNORECASE)}
    present={(row[0],row[1]) for row in connection.execute(
        "SELECT name,type FROM sqlite_master WHERE type IN ('table','index')")}
    if not expected <= present:return False
    if not {'event_type','incident_id'} <= {r[1] for r in connection.execute('PRAGMA table_info(room_outbox)')}:return False
    if not {'requested_owner','needs_operator'} <= {r[1] for r in connection.execute('PRAGMA table_info(incidents)')}:return False
    return bool(connection.execute("SELECT 1 FROM coordination_meta WHERE key='schema_version' AND value='2'").fetchone()
                and connection.execute("SELECT 1 FROM coordination_meta WHERE key='legacy_handoffs_imported'").fetchone()
                and connection.execute("SELECT 1 FROM teams WHERE name='GENERAL'").fetchone())


def init_db() -> None:
    with connect() as connection:
        if _schema_ready(connection):return
        connection.executescript(SCHEMA + coordination.SCHEMA)
        coordination.migrate_legacy(connection)
        now = utc_now()
        connection.execute(
            "INSERT OR IGNORE INTO teams(name, display_name, description, created_at, updated_at) VALUES(?,?,?,?,?)",
            ("GENERAL", "General", "Unclassified local agent operations", now, now),
        )


def event(
    connection: sqlite3.Connection,
    event_type: str,
    *,
    actor: str,
    team: Optional[str] = None,
    incident_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    event_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO events(event_id, created_at, event_type, team, incident_id, actor, payload_json) VALUES(?,?,?,?,?,?,?)",
        (event_id, utc_now(), event_type, team, incident_id, actor, canonical(payload or {})),
    )

    coordination.enqueue_event(connection, event_id, event_type, incident_id, team, payload or {})


def upsert_team(name: str, display_name: str, description: str, actor: str = "system") -> dict[str, Any]:
    init_db()
    name = name.strip().upper()
    if not name:
        raise ValueError("team name is required")
    now = utc_now()
    with connect() as connection:
        old = connection.execute("SELECT display_name,description FROM teams WHERE name=?", (name,)).fetchone()
        if old and old[0] == (display_name.strip() or name.title()) and old[1] == description.strip():
            return {"team": name, "display_name": display_name, "description": description}
        connection.execute(
            """
            INSERT INTO teams(name, display_name, description, created_at, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
              display_name=excluded.display_name,
              description=excluded.description,
              updated_at=excluded.updated_at
            """,
            (name, display_name.strip() or name.title(), description.strip(), now, now),
        )
        event(connection, "TEAM_UPSERTED", actor=actor, team=name, payload={"display_name": display_name})
    return {"team": name, "display_name": display_name, "description": description}


def infer_team(path: Optional[str] = None) -> str:
    text = (path or os.getcwd()).lower()
    if "career" in text:
        return "CAREER"
    return "GENERAL"


def register_agent(
    *,
    agent_id: str,
    team: str,
    provider: str,
    endpoint: str,
    display_name: str,
    capabilities: Iterable[str],
    writable_scopes: Iterable[str],
    priority: int = 100,
    status: str = "ACTIVE",
    wake_mode: str = "room",
    actor: str = "system",
) -> dict[str, Any]:
    init_db()
    team = team.strip().upper()
    with connect() as connection:
        if not connection.execute("SELECT 1 FROM teams WHERE name=?", (team,)).fetchone():
            now = utc_now()
            connection.execute(
                "INSERT INTO teams(name, display_name, description, created_at, updated_at) VALUES(?,?,?,?,?)",
                (team, team.title(), "Auto-registered team", now, now),
            )
        now = utc_now()
        connection.execute(
            """
            INSERT INTO agents(
              agent_id, team, provider, endpoint, display_name, capabilities_json,
              writable_scopes_json, priority, status, wake_mode, last_seen_at, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET
              team=excluded.team, provider=excluded.provider, endpoint=excluded.endpoint,
              display_name=excluded.display_name, capabilities_json=excluded.capabilities_json,
              writable_scopes_json=excluded.writable_scopes_json, priority=excluded.priority,
              status=excluded.status, wake_mode=excluded.wake_mode,
              last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at
            """,
            (
                agent_id, team, provider.lower(), endpoint, display_name,
                json_list(capabilities), canonical(list(writable_scopes)), int(priority),
                status.upper(), wake_mode.lower(), now, now, now,
            ),
        )
        event(
            connection,
            "AGENT_CHECKED_IN",
            actor=actor,
            team=team,
            payload={"agent_id": agent_id, "provider": provider, "status": status},
        )
    return get_agent(agent_id)


def get_agent(agent_id: str) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        row = connection.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    if not row:
        raise KeyError(agent_id)
    return row_to_agent(row)


def row_to_agent(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["capabilities"] = decode_json(item.pop("capabilities_json"), [])
    item["writable_scopes"] = decode_json(item.pop("writable_scopes_json"), [])
    return item


def heartbeat(agent_id: str, status: str = "ACTIVE", actor: str = "system") -> dict[str, Any]:
    init_db()
    with connect() as connection:
        row = connection.execute("SELECT team,status FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            raise KeyError(agent_id)
        now = utc_now()
        connection.execute(
            "UPDATE agents SET status=?, last_seen_at=?, updated_at=? WHERE agent_id=?",
            (status.upper(), now, now, agent_id),
        )
        if row["status"] != status.upper():
            event(connection, "AGENT_PRESENCE_CHANGED", actor=actor, team=row["team"], payload={"agent_id": agent_id, "status": status.upper()})
    return get_agent(agent_id)


def _room_post(body: str, *, team: str, kind: str = "system", recipients: str = "codex,claude,operator") -> dict[str, Any]:
    # Lifecycle events now enqueue the mirror within their database transaction.
    # This compatibility helper never sends a second, untracked copy.
    return {"posted": False, "reason": "transactional_outbox"}


def create_incident(
    *,
    team: str,
    title: str,
    details: str,
    severity: str = "warning",
    source: str = "manual",
    safe_action: str,
    required_capability: str = "general",
    needs_operator: bool = False,
    fingerprint: Optional[str] = None,
    requested_owner: Optional[str] = None,
    monitor_condition: bool = False,
    actor: str = "system",
) -> dict[str, Any]:
    init_db()
    team = team.strip().upper()
    title = title.strip()
    details = details.strip()
    if not title or not details or not safe_action.strip():
        raise ValueError("title, details, and safe_action are required")
    upsert_team(team, team.title(), "Agent Operations Board team", actor=actor)
    calculated = fingerprint or digest(
        {
            "team": team,
            "title": title,
            "details": details,
            "source": source,
            "required_capability": required_capability,
            "requested_owner": requested_owner,
        }
    )
    now = utc_now()
    incident_id = f"inc-{uuid.uuid4().hex[:16]}"
    created = False
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM incidents WHERE fingerprint=? AND status != 'RESOLVED'",
            (calculated,),
        ).fetchone()
        if not existing and monitor_condition:
            if not source.startswith('monitor:'):
                raise ValueError('monitor condition requires monitor source')
            existing = connection.execute(
                "SELECT * FROM incidents WHERE source=? AND team=? AND title=? AND details=? AND safe_action=? AND required_capability=? AND severity=? AND needs_operator=? AND requested_owner IS ? AND status!='RESOLVED' AND "+coordination.visible_incidents_sql()+" ORDER BY created_at,incident_id LIMIT 1",
                (source,team,title,details,safe_action.strip(),required_capability.strip().lower(),severity.lower(),int(bool(needs_operator)),requested_owner),
            ).fetchone()
        if existing:
            connection.execute(
                "UPDATE incidents SET updated_at=? WHERE incident_id=?",
                (now, existing["incident_id"]),
            )
            incident_id = existing["incident_id"]
            event(
                connection,
                "INCIDENT_DEDUPED",
                actor=actor,
                team=team,
                incident_id=incident_id,
                payload={"fingerprint": calculated},
            )
        else:
            status = "BLOCKED_HUMAN" if needs_operator else "OPEN"
            connection.execute(
                """
                INSERT INTO incidents(
                  incident_id, fingerprint, team, title, details, severity, status,
                  source, safe_action, required_capability, needs_operator, created_at, updated_at, requested_owner
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    incident_id, calculated, team, title, details, severity.lower(), status,
                    source, safe_action.strip(), required_capability.strip().lower(),
                    int(bool(needs_operator)), now, now, requested_owner,
                ),
            )
            event(
                connection,
                "INCIDENT_OPENED",
                actor=actor,
                team=team,
                incident_id=incident_id,
                payload={"title": title, "severity": severity, "needs_operator": needs_operator},
            )
            created = True
    if created:
        room_result = _room_post(
            f"INCIDENT {incident_id}: {title}. {details} Safe next action: {safe_action}. "
            f"Operator needed: {'yes' if needs_operator else 'no'}. The board will assign at most one owner.",
            team=team,
        )
        with connect() as connection:
            event(
                connection,
                "ROOM_MIRROR_ATTEMPTED",
                actor="agent-board",
                team=team,
                incident_id=incident_id,
                payload=room_result,
            )
    return get_incident(incident_id)


def get_incident(incident_id: str) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        row = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        item = coordination.incident_annotations(connection,dict(row)) if row else None
    if not row:
        raise KeyError(incident_id)
    item["needs_operator"] = bool(item["needs_operator"])
    return item


def _owner_hints(incident):
    import re
    hints=[]
    if incident['requested_owner']:hints.append(incident['requested_owner'])
    # Only explicit custody phrases count. New callers should supply the field.
    identity=r'((?:codex:|claude:)?[0-9a-f]{8}(?:-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})?)'
    text=incident['safe_action']+'\n'+incident['details']
    hints.extend(re.findall(r'\bowner\s*:?\s*'+identity+r'\s+only\b',text,re.I))
    hints.extend(re.findall(r'\b(?:exact|existing)\s+(?:task\s+)?owner\s*:?\s*'+identity+r'\b',text,re.I))
    return list(dict.fromkeys(hints))


def _owner_matches(agent,hint):
    if hint in (agent['agent_id'],agent['endpoint']):return True
    return len(hint)==8 and agent['endpoint'].startswith(hint+'-')


def _eligible_agents(connection: sqlite3.Connection, incident: sqlite3.Row) -> list[sqlite3.Row]:
    hints=_owner_hints(incident)
    rows = connection.execute(
        "SELECT * FROM agents WHERE provider IN ('codex','claude') AND status NOT IN ('DISABLED','OFFLINE') AND (? OR team=?) ORDER BY priority ASC, updated_at DESC",
        (bool(hints),incident["team"]),
    ).fetchall()
    if hints:
        rows=[r for r in rows if all(_owner_matches(r,h) for h in hints)]
        if len(rows)!=1:return [] # Missing/ambiguous exact identity never falls back.
    required = incident["required_capability"].lower()
    eligible = []
    for row in rows:
        capabilities = decode_json(row["capabilities_json"], [])
        if required in ("", "general") or required in capabilities or "all" in capabilities:
            eligible.append(row)
    return eligible


def assign_incident(incident_id: str, actor: str = "dispatcher") -> dict[str, Any]:
    from workflow import manual_routing
    if manual_routing(ROOT):
        return dict(get_incident(incident_id), routing_status='HELD_FOR_OPERATOR', routing_note='Manual constellations: no automatic assignment or wake.')
    init_db()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        incident = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if not incident:
            raise KeyError(incident_id)
        if connection.execute('SELECT 1 FROM incident_links WHERE incident_id=?',(incident_id,)).fetchone():
            return coordination.incident_annotations(connection,dict(incident))
        if incident["status"] not in ("RESOLVED", "BLOCKED_HUMAN") and not incident["assigned_agent_id"]:
            eligible = _eligible_agents(connection, incident)
            if not eligible:
                observed = connection.execute("SELECT fingerprint FROM route_observations WHERE incident_id=?", (incident_id,)).fetchone()
                fingerprint = digest({"team": incident["team"], "capability": incident["required_capability"],"owner_hints":_owner_hints(incident)})
                if not observed or observed[0] != fingerprint:
                    event(connection, "INCIDENT_UNROUTED", actor=actor, team=incident["team"], incident_id=incident_id,
                          payload={"required_capability": incident["required_capability"]})
                connection.execute("INSERT OR REPLACE INTO route_observations VALUES(?,?,?)", (incident_id, fingerprint, time.time()+300))
            else:
                owner = eligible[0]
                now = utc_now()
                connection.execute(
                    "UPDATE incidents SET assigned_agent_id=?, status='ASSIGNED', updated_at=? WHERE incident_id=?",
                    (owner["agent_id"], now, incident_id),
                )
                event(
                    connection,
                    "INCIDENT_ASSIGNED",
                    actor=actor,
                    team=incident["team"],
                    incident_id=incident_id,
                    payload={"agent_id": owner["agent_id"], "provider": owner["provider"]},
                )
    return get_incident(incident_id)


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _launch_workspace(agent: dict[str, Any]) -> Optional[Path]:
    """The agent's own first writable scope, never the operator's home directory."""
    for scope in agent.get("writable_scopes") or []:
        path = Path(str(scope)).expanduser()
        if path.is_absolute() and path.is_dir() and path.resolve() != Path.home().resolve():
            return path
    return None


def _launch_gate(agent: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Every provider process start needs manual routing off AND an explicit opt-in."""
    from workflow import manual_routing
    if manual_routing(ROOT):
        return {'status': 'HELD_FOR_OPERATOR', 'launched': False}
    if not launch_allowed():
        return {'status': 'LAUNCH_DISABLED', 'launched': False,
                'detail': 'Provider process launch is off. Set ' + LAUNCH_ENV + '=1 (or --allow-launch) to opt in.'}
    if not agent.get("endpoint"):
        return {"status": "NO_ENDPOINT", 'launched': False}
    if _launch_workspace(agent) is None:
        return {'status': 'NO_WORKSPACE', 'launched': False,
                'detail': 'Register an existing project directory as the agent writable scope before launch.'}
    return None


def _wake_codex(agent: dict[str, Any], incident: dict[str, Any]) -> dict[str, Any]:
    refused = _launch_gate(agent)
    if refused:
        return refused
    endpoint = agent["endpoint"]
    WAKE_LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = WAKE_LOG_DIR / f"{int(time.time())}-{incident['incident_id']}-codex.log"
    prompt = (
        f"AGENT OPERATIONS BOARD TEAM PING — {incident['team']} incident {incident['incident_id']}. "
        f"{incident['title']}. {incident['details']} Safe next action: {incident['safe_action']}. "
        "Read the shared board and global room, acknowledge this incident before changing state, "
        "preserve one-writer ownership, and fix it if locally authorized. Do not spawn native children."
    )
    with log_path.open("wb") as stream:
        process = subprocess.Popen(
            ["codex", "exec", "resume", "--skip-git-repo-check", endpoint, prompt],
            cwd=str(_launch_workspace(agent)),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(2)
    if process.poll() is None:
        return {"status": "WAKE_QUEUED", "pid": process.pid, "log": str(log_path)}
    text = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    if "active writer" in text.lower() or "currently active" in text.lower():
        return {"status": "OWNER_ALREADY_ACTIVE", "log": str(log_path)}
    return {"status": "WAKE_FAILED", "returncode": process.returncode, "log": str(log_path), "detail": text[-500:]}


def _wake_claude(agent: dict[str, Any], incident: dict[str, Any]) -> dict[str, Any]:
    refused = _launch_gate(agent)
    if refused:
        return refused
    endpoint = agent["endpoint"]
    WAKE_LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = WAKE_LOG_DIR / f"{int(time.time())}-{incident['incident_id']}-claude.log"
    prompt = (
        f"AGENT OPERATIONS BOARD TEAM PING — {incident['team']} incident {incident['incident_id']}. "
        f"{incident['title']}. {incident['details']} Safe next action: {incident['safe_action']}. "
        "Read the shared board and global room, acknowledge first, preserve the existing writer, "
        "and do only the locally authorized repair."
    )
    with log_path.open("wb") as stream:
        process = subprocess.Popen(
            # No permission bypass: the resumed session keeps its normal
            # permission prompts, which a --print run cannot approve.
            ["claude", "--print", "--resume", endpoint, prompt],
            cwd=str(_launch_workspace(agent)),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(2)
    if process.poll() is None:
        return {"status": "WAKE_QUEUED", "pid": process.pid, "log": str(log_path)}
    text = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    if "already in use" in text.lower() or "active" in text.lower():
        return {"status": "OWNER_ALREADY_ACTIVE", "log": str(log_path)}
    return {"status": "WAKE_FAILED", "returncode": process.returncode, "log": str(log_path), "detail": text[-500:]}


def wake_incident(incident_id: str, actor: str = "dispatcher") -> dict[str, Any]:
    from workflow import manual_routing
    if manual_routing(ROOT):
        return dict(get_incident(incident_id), routing_status='HELD_FOR_OPERATOR', routing_note='Manual constellations: no automatic assignment or wake.')
    incident = assign_incident(incident_id, actor=actor)
    if incident.get('linked_to') or incident["status"] in ("RESOLVED", "BLOCKED_HUMAN") or not incident["assigned_agent_id"]:
        return incident
    agent = get_agent(incident["assigned_agent_id"])
    if agent['provider'] not in ('codex','claude'):
        return incident # Historical aliases require explicit reconciliation, never a launch.
    skip_wake = False
    with connect() as connection:
        locked = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if int(locked["wake_attempts"]) >= 1:
            return get_incident(incident_id)
        connection.execute("BEGIN IMMEDIATE")
        locked = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if int(locked["wake_attempts"]) >= 1:
            skip_wake = True
        else:
            now = utc_now()
            connection.execute(
                "UPDATE incidents SET wake_attempts=1, wake_status='ATTEMPTING', last_wake_at=?, updated_at=? WHERE incident_id=?",
                (now, now, incident_id),
            )
            connection.execute("INSERT OR IGNORE INTO handoffs(incident_id,owner,stage,attempts,due_at,detail,updated_at) VALUES(?,?,'QUEUED',1,?,'launch not yet reconciled',?)", (incident_id,agent['agent_id'],time.time()+180,now))
            event(
                connection,
                "WAKE_ATTEMPT_STARTED",
                actor=actor,
                team=incident["team"],
                incident_id=incident_id,
                payload={"agent_id": agent["agent_id"]},
            )
    if skip_wake:
        return get_incident(incident_id)
    active = time.time() - _parse_time(agent["last_seen_at"]) <= ACTIVE_HEARTBEAT_SECONDS and agent["status"] == "ACTIVE"
    if active:
        result = {"status": "OWNER_ALREADY_ACTIVE"}
    elif agent["wake_mode"] != "resume":
        result = {"status": "ROOM_PING_ONLY"}
    elif not launch_allowed():
        result = {"status": "LAUNCH_DISABLED"}
    elif agent["provider"] == "codex":
        result = _wake_codex(agent, incident)
    elif agent["provider"] == "claude":
        result = _wake_claude(agent, incident)
    else:
        result = {"status": "UNSUPPORTED_PROVIDER"}
    now = utc_now()
    with connect() as connection:
        connection.execute(
            "UPDATE incidents SET wake_status=?, updated_at=? WHERE incident_id=?",
            (result["status"], now, incident_id),
        )
        stage = "QUEUED" if result['status'] in ("OWNER_ALREADY_ACTIVE", "ROOM_PING_ONLY", "LAUNCH_DISABLED") else "LAUNCH_PENDING"
        connection.execute("UPDATE handoffs SET stage=?,pid=?,detail=?,updated_at=? WHERE incident_id=?", (stage,result.get('pid'),canonical(result),now,incident_id))
        event(
            connection,
            "WAKE_ATTEMPT_FINISHED",
            actor=actor,
            team=incident["team"],
            incident_id=incident_id,
            payload={"agent_id": agent["agent_id"], **result},
        )
    _room_post(
        f"ASSIGNED {incident_id} to {agent['display_name']} ({agent['agent_id']}); wake result {result['status']}. "
        "No duplicate owner or fallback agent was created.",
        team=incident["team"],
    )
    return get_incident(incident_id)


def dispatch_open(actor: str = "dispatcher") -> list[dict[str, Any]]:
    from workflow import manual_routing
    if manual_routing(ROOT): return []
    init_db()
    with connect() as connection:
        ids = [
            row["incident_id"]
            for row in connection.execute(
                "SELECT i.incident_id FROM incidents i LEFT JOIN route_observations r USING(incident_id) WHERE i.status='OPEN' AND i.incident_id NOT IN (SELECT incident_id FROM incident_links) AND (r.incident_id IS NULL OR r.next_at<=strftime('%s','now')) ORDER BY i.created_at ASC LIMIT 32"
            ).fetchall()
        ]
    return [wake_incident(incident_id, actor=actor) for incident_id in ids]


def acknowledge(incident_id: str, agent_id: str, note: str = "", actor: str = "agent") -> dict[str, Any]:
    init_db()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if not row:
            raise KeyError(incident_id)
        if connection.execute('SELECT 1 FROM incident_links WHERE incident_id=?',(incident_id,)).fetchone():
            raise ValueError('linked historical report; acknowledge its canonical incident')
        agent=connection.execute('SELECT * FROM agents WHERE agent_id=?',(agent_id,)).fetchone()
        if not agent or agent['provider'] not in ('codex','claude'):
            raise ValueError('exact registered task owner required; room aliases cannot acknowledge')
        hints=_owner_hints(row)
        if hints and any(not _owner_matches(agent,h) for h in hints):
            raise ValueError('acknowledgement violates named owner constraint')
        if row["assigned_agent_id"] and row["assigned_agent_id"] != agent_id:
            raise ValueError(f"incident belongs to {row['assigned_agent_id']}")
        if row["status"] == "RESOLVED":
            return get_incident(incident_id)
        now = utc_now()
        connection.execute(
            "UPDATE incidents SET assigned_agent_id=?, status='ACKNOWLEDGED', acknowledged_at=?, updated_at=? WHERE incident_id=?",
            (agent_id, now, now, incident_id),
        )
        connection.execute("UPDATE handoffs SET stage='ACKNOWLEDGED',updated_at=? WHERE incident_id=?", (now,incident_id))
        event(
            connection,
            "INCIDENT_ACKNOWLEDGED",
            actor=actor,
            team=row["team"],
            incident_id=incident_id,
            payload={"agent_id": agent_id, "note": note},
        )
    _room_post(f"ACKNOWLEDGED {incident_id} by {agent_id}. {note}".strip(), team=row["team"], kind="ack")
    return get_incident(incident_id)


def resolve(incident_id: str, actor: str, resolution: str) -> dict[str, Any]:
    init_db()
    if not resolution.strip():
        raise ValueError("resolution is required")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if not row:
            raise KeyError(incident_id)
        now = utc_now()
        connection.execute(
            "UPDATE incidents SET status='RESOLVED', resolved_at=?, updated_at=? WHERE incident_id=?",
            (now, now, incident_id),
        )
        connection.execute("UPDATE handoffs SET stage='RESOLVED',updated_at=? WHERE incident_id=?", (now,incident_id))
        event(
            connection,
            "INCIDENT_RESOLVED",
            actor=actor,
            team=row["team"],
            incident_id=incident_id,
            payload={"resolution": resolution},
        )
    _room_post(f"RESOLVED {incident_id}: {resolution}", team=row["team"], kind="result")
    return get_incident(incident_id)


def register_monitor(
    *,
    monitor_id: str,
    team: str,
    source_path: str,
    title: str,
    severity: str,
    required_capability: str,
    safe_action: str,
    status_field: str = "status",
    detail_field: str = "detail",
    record_hash_field: str = "record_hash",
    failure_statuses: Iterable[str] = ("FAILED", "ERROR", "BLOCKED_SAFE", "CRASHED"),
    success_statuses: Iterable[str] = ("COMPLETE", "SUCCEEDED", "PASS"),
    active_statuses: Iterable[str] = ("RUNNING", "ACTIVE", "WAITING"),
    stale_after_seconds: int = 0,
    actor: str = "system",
) -> dict[str, Any]:
    init_db()
    team = team.upper()
    upsert_team(team, team.title(), "Agent Operations Board team", actor=actor)
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO monitors(
              monitor_id, team, source_path, title, severity, required_capability, safe_action,
              status_field, detail_field, record_hash_field, failure_statuses_json,
              success_statuses_json, active_statuses_json, stale_after_seconds, enabled,
              created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
            ON CONFLICT(monitor_id) DO UPDATE SET
              team=excluded.team, source_path=excluded.source_path, title=excluded.title,
              severity=excluded.severity, required_capability=excluded.required_capability,
              safe_action=excluded.safe_action, status_field=excluded.status_field,
              detail_field=excluded.detail_field, record_hash_field=excluded.record_hash_field,
              failure_statuses_json=excluded.failure_statuses_json,
              success_statuses_json=excluded.success_statuses_json,
              active_statuses_json=excluded.active_statuses_json,
              stale_after_seconds=excluded.stale_after_seconds, enabled=1, updated_at=excluded.updated_at
            """,
            (
                monitor_id, team, str(Path(source_path).expanduser()), title, severity,
                required_capability.lower(), safe_action, status_field, detail_field,
                record_hash_field, json_list(failure_statuses), json_list(success_statuses),
                json_list(active_statuses), int(stale_after_seconds), now, now,
            ),
        )
        event(connection, "MONITOR_REGISTERED", actor=actor, team=team, payload={"monitor_id": monitor_id})
    return {"monitor_id": monitor_id, "team": team, "source_path": source_path}


def _field(data: dict[str, Any], dotted: str) -> Any:
    value: Any = data
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def scan_monitors(actor: str = "monitor") -> list[dict[str, Any]]:
    init_db()
    outcomes: list[dict[str, Any]] = []
    with connect() as connection:
        monitors = connection.execute("SELECT * FROM monitors WHERE enabled=1 ORDER BY monitor_id").fetchall()
    for monitor in monitors:
        path = Path(monitor["source_path"])
        scan_at = utc_now()
        if not path.is_file():
            missing_fingerprint = digest({"monitor_id": monitor["monitor_id"], "source_missing": str(path)})
            incident = None
            if missing_fingerprint != (monitor["last_fingerprint"] or ""):
                incident = create_incident(
                    team=monitor["team"],
                    title=f"{monitor['title']}: state file missing",
                    details=f"The registered worker state file is missing: {path}",
                    severity=monitor["severity"],
                    source=f"monitor:{monitor['monitor_id']}:{path}",
                    safe_action=monitor["safe_action"],
                    required_capability=monitor["required_capability"],
                    fingerprint=missing_fingerprint,
                    actor=actor,
                )
                with connect() as connection:
                    connection.execute(
                        "UPDATE monitors SET last_fingerprint=?, last_scan_at=?, updated_at=? WHERE monitor_id=?",
                        (missing_fingerprint, scan_at, scan_at, monitor["monitor_id"]),
                    )
                    event(
                        connection,
                        "MONITOR_SCANNED",
                        actor=actor,
                        team=monitor["team"],
                        incident_id=incident["incident_id"],
                        payload={"monitor_id": monitor["monitor_id"], "status": "SOURCE_MISSING", "kind": "FAILURE"},
                    )
            outcomes.append(
                {
                    "monitor_id": monitor["monitor_id"],
                    "status": "SOURCE_MISSING",
                    "kind": "FAILURE",
                    "incident_id": incident["incident_id"] if incident else None,
                }
            )
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            data = {monitor["status_field"]: "UNREADABLE", monitor["detail_field"]: f"{type(exc).__name__}:{exc}"}
        status = str(_field(data, monitor["status_field"]) or "UNKNOWN")
        detail = str(_field(data, monitor["detail_field"]) or "")
        record_hash = str(_field(data, monitor["record_hash_field"]) or "")
        failures = {item.upper() for item in decode_json(monitor["failure_statuses_json"], [])}
        successes = {item.upper() for item in decode_json(monitor["success_statuses_json"], [])}
        active = {item.upper() for item in decode_json(monitor["active_statuses_json"], [])}
        stale_age = max(0, time.time() - path.stat().st_mtime)
        stale = (
            status.upper() in active
            and int(monitor["stale_after_seconds"]) > 0
            and stale_age > int(monitor["stale_after_seconds"])
        )
        failed=status.upper() in failures or status.upper() == 'UNREADABLE'
        # Producer record hashes may include timestamps. Keep that provenance,
        # but an identical failure condition does not become a new assignment.
        condition={'status':status,'detail':detail,'stale':stale,'safe_action':monitor['safe_action'],
                   'severity':monitor['severity'],'required_capability':monitor['required_capability'],'team':monitor['team'],'title':monitor['title']}
        source_fingerprint = digest({**condition,'record_hash':None if failed else record_hash})
        with connect() as connection:
            connection.execute('INSERT INTO monitor_observations VALUES(?,?,?,?,1,?) ON CONFLICT(monitor_id) DO UPDATE SET status=excluded.status,detail=excluded.detail,record_hash=excluded.record_hash,observations=observations+1,last_observed_at=excluded.last_observed_at',(monitor['monitor_id'],status,detail,record_hash,scan_at))
        changed = source_fingerprint != (monitor["last_fingerprint"] or "")
        if failed and not changed:
            with connect() as connection:
                changed=not connection.execute("SELECT 1 FROM incidents WHERE source=? AND title=? AND details=? AND status!='RESOLVED' AND "+coordination.visible_incidents_sql()+" LIMIT 1",(f"monitor:{monitor['monitor_id']}:{path}",f"{monitor['title']}: {status}",detail or f"{path} reported {status}")).fetchone()
        state_kind = "OBSERVED"
        incident = None
        if (status.upper() in failures or status.upper() in {"UNREADABLE"}) and changed:
            state_kind = "FAILURE"
            fp = digest({"monitor_id": monitor["monitor_id"], **condition})
            incident = create_incident(
                team=monitor["team"],
                title=f"{monitor['title']}: {status}",
                details=detail or f"{path} reported {status}",
                severity=monitor["severity"],
                source=f"monitor:{monitor['monitor_id']}:{path}",
                safe_action=monitor["safe_action"],
                required_capability=monitor["required_capability"],
                fingerprint=fp,
                monitor_condition=True,
                actor=actor,
            )
        elif stale and changed:
            age = stale_age
            if stale:
                state_kind = "STALE"
                fp = digest({"monitor_id": monitor["monitor_id"], "status": status, "stale_mtime": int(path.stat().st_mtime)})
                incident = create_incident(
                    team=monitor["team"],
                    title=f"{monitor['title']}: stopped updating",
                    details=f"State remained {status} but has not updated for {int(age)} seconds.",
                    severity=monitor["severity"],
                    source=f"monitor:{monitor['monitor_id']}:{path}",
                    safe_action=monitor["safe_action"],
                    required_capability=monitor["required_capability"],
                    fingerprint=fp,
                    actor=actor,
                )
        elif status.upper() in successes:
            state_kind = "SUCCESS"
        fingerprint = source_fingerprint
        with connect() as connection:
            connection.execute(
                "UPDATE monitors SET last_fingerprint=?, last_scan_at=?, updated_at=? WHERE monitor_id=?",
                (fingerprint, scan_at, scan_at, monitor["monitor_id"]),
            )
            if changed or incident:
                event(
                    connection,
                    "MONITOR_SCANNED",
                    actor=actor,
                    team=monitor["team"],
                    incident_id=incident["incident_id"] if incident else None,
                    payload={"monitor_id": monitor["monitor_id"], "status": status, "kind": state_kind},
                )
        outcomes.append(
            {
                "monitor_id": monitor["monitor_id"],
                "status": status,
                "kind": state_kind,
                "incident_id": incident["incident_id"] if incident else None,
            }
        )
    return outcomes


def snapshot(event_limit: int = 100) -> dict[str, Any]:
    init_db()
    with connect() as connection:
        teams = [dict(row) for row in connection.execute("SELECT * FROM teams ORDER BY name").fetchall()]
        agents = [coordination.presence(row_to_agent(row)) for row in connection.execute("SELECT * FROM agents ORDER BY team, priority, agent_id").fetchall()]
        incidents = [dict(row) for row in connection.execute("SELECT * FROM incidents ORDER BY created_at DESC").fetchall()]
        for item in incidents:
            item["needs_operator"] = bool(item["needs_operator"])
        events = [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY sequence DESC LIMIT ?", (event_limit,)).fetchall()]
        for item in events:
            item["payload"] = decode_json(item.pop("payload_json"), {})
        monitors = [dict(row) for row in connection.execute("SELECT * FROM monitors ORDER BY monitor_id").fetchall()]
    return {
        "contract": "ke.agent-operations-board.snapshot.v1",
        "generated_at": utc_now(),
        "database": str(DB_PATH),
        "teams": teams,
        "agents": agents,
        "incidents": incidents,
        "events": events,
        "monitors": monitors,
        "counts": {
            "open": sum(item["status"] != "RESOLVED" for item in incidents),
            "working": sum(item["status"] == "ACKNOWLEDGED" for item in incidents),
            "needs_operator": sum(item["status"] == "BLOCKED_HUMAN" for item in incidents),
        },
    }


__all__ = [
    "DB_PATH", "acknowledge", "create_incident", "dispatch_open", "get_incident",
    "heartbeat", "infer_team", "init_db", "register_agent", "register_monitor",
    "resolve", "scan_monitors", "snapshot", "upsert_team", "wake_incident",
]
