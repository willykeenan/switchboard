"""Seed invented sample agents, rooms, placements and a starter task."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_room(root, name, title, messages):
    folder = Path(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "room.json").write_text(json.dumps({"title": title, "name": name}, indent=2) + "\n")
    lines = []
    for index, message in enumerate(messages, 1):
        row = {
            "seq": index,
            "id": str(index),
            "author": message["author"],
            "kind": message.get("kind", "message"),
            "body": message["body"],
            "recipients": message.get("recipients") or ["everyone"],
            "createdAt": message.get("createdAt") or _now(),
            "replyTo": message.get("replyTo"),
        }
        lines.append(json.dumps(row, ensure_ascii=False))
    (folder / "messages.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))


def seed(data_dir, rooms_dir=None):
    """Create demo board data if the directory is empty of rooms."""
    data_dir = Path(data_dir)
    rooms_dir = Path(rooms_dir or (data_dir / "rooms"))
    rooms_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / "runtime" / "demo-seeded.json"
    if marker.exists():
        return {"seeded": False, "reason": "Demo data already present"}
    os.environ.setdefault("SWITCHBOARD_DISABLE_ROOM", "1")
    import board_core as board

    board.ROOT = data_dir
    board.DB_PATH = data_dir / "board.sqlite3"
    board.init_db()
    board.upsert_team("RESEARCH", "Research", "Sample research team")
    board.upsert_team("BUILD", "Build", "Sample build team")
    board.register_agent(
        agent_id="codex:demo-owner",
        team="RESEARCH",
        provider="codex",
        endpoint="demo-owner",
        display_name="Demo owner",
        capabilities=("coordination", "local-repair"),
        writable_scopes=(str(data_dir),),
        priority=1,
        status="ACTIVE",
        wake_mode="room",
    )
    board.register_agent(
        agent_id="codex:demo-worker",
        team="BUILD",
        provider="codex",
        endpoint="demo-worker",
        display_name="Demo worker",
        capabilities=("local-repair", "build"),
        writable_scopes=(str(data_dir),),
        priority=10,
        status="IDLE",
        wake_mode="room",
    )
    board.register_agent(
        agent_id="claude:demo-researcher",
        team="RESEARCH",
        provider="claude",
        endpoint="demo-researcher",
        display_name="Demo researcher",
        capabilities=("research",),
        writable_scopes=(str(data_dir),),
        priority=20,
        status="ACTIVE",
        wake_mode="room",
    )
    board.register_agent(
        agent_id="codex:demo-auditor",
        team="RESEARCH",
        provider="codex",
        endpoint="demo-auditor",
        display_name="Demo auditor",
        capabilities=("audit",),
        writable_scopes=(str(data_dir),),
        priority=30,
        status="IDLE",
        wake_mode="room",
    )
    board.create_incident(
        team="RESEARCH",
        title="Welcome to Switchboard",
        details="This invented incident shows how the board assigns one owner.",
        severity="info",
        source="demo",
        safe_action="Read the rooms and workflow map, then resolve this sample.",
        required_capability="coordination",
        fingerprint="demo-welcome",
    )
    created = _now()
    _write_room(
        rooms_dir,
        "global",
        "Global",
        [
            {
                "author": "board",
                "kind": "system",
                "body": "Demo board is live. Rooms, the workflow map, placements and handoffs all share this data directory.",
                "createdAt": created,
            },
            {
                "author": "codex:demo-owner",
                "kind": "handoff",
                "body": "Please draft the sample README outline in the research lane. Exact recipient: claude:demo-researcher.",
                "recipients": ["claude:demo-researcher"],
                "createdAt": created,
            },
            {
                "author": "claude:demo-researcher",
                "kind": "result",
                "body": "Outline ready: pitch, 60-second quickstart, features, configuration, privacy notes.",
                "recipients": ["codex:demo-owner"],
                "createdAt": created,
            },
        ],
    )
    _write_room(
        rooms_dir,
        "demo-research",
        "Demo research",
        [
            {
                "author": "claude:demo-researcher",
                "kind": "message",
                "body": "Sample notes only. Nothing here is a real session transcript.",
                "createdAt": created,
            }
        ],
    )
    from room_reader import RoomStore
    import workspace
    import workflow

    rooms = RoomStore(rooms_dir)
    work = workspace.Workspace(data_dir, rooms)
    source = {
        "projects": [{"id": "demo", "name": "Demo", "objective": "Show a local agent coordination board"}],
        "lanes": [
            {
                "id": "workstream-demo-work",
                "projectId": "demo",
                "name": "Workstream",
                "objective": "Deliver sample tasks through workers and independent audit",
                "kind": "strategy",
                "supportMode": "general",
                "lifecycle": "active",
                "aliases": [],
            },
            {
                "id": "project-demo-librarian",
                "projectId": "demo",
                "name": "Project Library",
                "objective": "Retain sample requirements and evidence",
                "kind": "support",
                "supportMode": "general",
                "lifecycle": "active",
                "aliases": [],
            },
            {
                "id": "project-demo-alignment",
                "projectId": "demo",
                "name": "Audit team",
                "objective": "Share independent reviews",
                "kind": "support",
                "supportMode": "alignment",
                "lifecycle": "active",
                "aliases": [],
            },
        ],
        "members": [
            {"laneId": "workstream-demo-work", "agentId": "codex:demo-owner", "role": "coordinator"},
            {"laneId": "workstream-demo-work", "agentId": "codex:demo-worker", "role": "worker"},
            {"laneId": "workstream-demo-work", "agentId": "claude:demo-researcher", "role": "researcher"},
            {"laneId": "project-demo-alignment", "agentId": "codex:demo-auditor", "role": "auditor"},
        ],
        "taskLinks": [],
        "dependencies": [],
    }
    work.save(source, 0, "demo")
    flow = workflow.Workflow(data_dir, work, data_dir / "no-provider-store")
    state = {k: v for k, v in flow.read().items() if k != "revision"}
    state.update(
        enabled=True,
        defaultCommunication="same-lane",
        placements=[
            {"agentId": "codex:demo-owner", "laneId": "workstream-demo-work", "role": "coordinator"},
            {"agentId": "codex:demo-worker", "laneId": "workstream-demo-work", "role": "worker"},
            {"agentId": "claude:demo-researcher", "laneId": "workstream-demo-work", "role": "researcher"},
            {"agentId": "codex:demo-auditor", "laneId": "project-demo-alignment", "role": "auditor"},
        ],
        connections=[
            {"from": "codex:demo-owner", "to": "claude:demo-researcher", "allow": True},
            {"from": "claude:demo-researcher", "to": "codex:demo-worker", "allow": True},
            {"from": "codex:demo-worker", "to": "codex:demo-auditor", "allow": True},
        ],
        laneCatalog=source["lanes"],
        teams=[],
        teamLeads=[
            {"laneId": "workstream-demo-work", "teamId": "coordinator", "agentId": "codex:demo-owner"}
        ],
    )
    flow.save(state, 0, "demo")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"seededAt": created, "project": "demo"}, indent=2) + "\n")
    return {"seeded": True, "rooms": ["global", "demo-research"], "agents": 4}
