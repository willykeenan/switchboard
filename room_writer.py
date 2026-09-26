"""Append-only room posts. No provider access."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def post(room_dir, author, body, recipients=None, kind="message", reply_to=None, key=None, workspace=None):
    room_dir = Path(room_dir)
    room_dir.mkdir(parents=True, exist_ok=True)
    log = room_dir / "messages.jsonl"
    if not log.exists():
        log.write_text("")
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    if key:
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("idempotencyKey") == key:
                if workspace:
                    previous = row.get("workspace") or {}
                    if (
                        previous.get("projectId") != workspace.get("projectId")
                        or sorted(previous.get("laneIds") or []) != sorted(workspace.get("laneIds") or [])
                    ):
                        raise SystemExit("idempotency key already used")
                return {"id": row.get("id"), "seq": row.get("seq")}
    seq = 1
    for line in lines:
        try:
            seq = max(seq, int(json.loads(line)["seq"]) + 1)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    message = {
        "seq": seq,
        "id": str(uuid.uuid4()),
        "author": str(author),
        "kind": kind or "message",
        "body": str(body),
        "recipients": list(recipients or ["everyone"]),
        "createdAt": _now(),
        "replyTo": reply_to,
        "idempotencyKey": key,
    }
    if workspace:
        message["workspace"] = workspace
    with log.open("a") as handle:
        handle.write(json.dumps(message, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {"id": message["id"], "seq": seq}


def read_after(room_dir, after=0):
    log = Path(room_dir) / "messages.jsonl"
    if not log.exists():
        return ""
    lines = []
    for raw in log.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if int(row.get("seq") or 0) > int(after):
            lines.append("#%s %s %s" % (row.get("seq"), row.get("author"), row.get("body", "").splitlines()[0] if row.get("body") else ""))
    return "\n".join(lines) + ("\n" if lines else "")


def default_room():
    """The data directory's global room (same rule as store.rooms_root(); kept
    inline so this script also runs standalone)."""
    override = os.environ.get("SWITCHBOARD_ROOMS_ROOT")
    if override:
        return Path(override).expanduser() / "global"
    home = os.environ.get("SWITCHBOARD_ROOT") or os.environ.get("SWITCHBOARD_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".switchboard") / "rooms" / "global"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["post", "read"])
    parser.add_argument("--author")
    parser.add_argument("--to", default="everyone")
    parser.add_argument("--kind", default="message")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--after", default="0")
    parser.add_argument("--room", default=None)
    args = parser.parse_args(argv)
    room = Path(args.room) if args.room else default_room()
    if args.command == "read":
        sys.stdout.write(read_after(room, args.after))
        return 0
    if not args.author:
        parser.error("--author is required for post")
    body = sys.stdin.read()
    result = post(room, args.author, body, [part.strip() for part in args.to.split(",") if part.strip()], args.kind, None, args.idempotency_key)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
