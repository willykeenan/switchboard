"""Read-only room history. No board database, agent cursors, or room writes."""
from __future__ import annotations

import json
import os
import re
import stat
import threading
from pathlib import Path


class RoomStore:
    def __init__(self, root):
        self.root = Path(root)
        self.cache = {}
        self.lock = threading.RLock()

    def _open(self, room, filename):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", room):
            raise ValueError("Invalid room")
        rootfd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            roomfd = os.open(room, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
            try:
                fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=roomfd)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    os.close(fd)
                    raise ValueError("Room history must be a regular file")
                return os.fdopen(fd, "rb")
            finally:
                os.close(roomfd)
        finally:
            os.close(rootfd)

    def snapshot(self, room):
        with self.lock, self._open(room, "messages.jsonl") as stream:
            info = os.fstat(stream.fileno())
            signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            cached = self.cache.get(room)
            if cached and cached[0] == signature:
                return cached[1]
            raw = stream.read(info.st_size)
            partial = bool(raw and not raw.endswith(b"\n"))
            lines = raw.split(b"\n")[:-1]
            messages, seen, invalid = [], set(), 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    m = json.loads(line)
                    seq = m["seq"]
                    if (type(seq) is not int or seq < 1 or seq in seen or
                            not isinstance(m.get("body"), str) or
                            not isinstance(m.get("author"), str)):
                        raise ValueError("Invalid message")
                    seen.add(seq)
                    messages.append(m)
                except (ValueError, KeyError, TypeError):
                    invalid += 1
            messages.sort(key=lambda m: m["seq"])
            result = {"messages": messages, "invalidLines": invalid, "partialTail": partial,
                      "latestSeq": messages[-1]["seq"] if messages else 0,
                      "total": len(messages)}
            self.cache[room] = (signature, result)
            return result

    def catalog(self):
        rooms = []
        for path in sorted(self.root.iterdir(), key=lambda p: (p.name != "global", p.name)):
            if path.is_symlink() or not path.is_dir() or not (path / "messages.jsonl").exists():
                continue
            try:
                s = self.snapshot(path.name)
                title = path.name.replace("-", " ").title()
                try:
                    with self._open(path.name, "room.json") as meta:
                        title = str(json.load(meta).get("title") or title)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                rooms.append({"name": path.name, "title": title, "total": s["total"],
                              "latestSeq": s["latestSeq"], "invalidLines": s["invalidLines"],
                              "partialTail": s["partialTail"],
                              "lastMessageAt": s["messages"][-1].get("createdAt") if s["messages"] else None})
            except (OSError, ValueError) as exc:
                rooms.append({"name": path.name, "title": path.name, "error": type(exc).__name__})
        return {"rooms": rooms, "readOnly": True}

    def query(self, room, q="", author="", kind="", before=0, limit=30, message=0):
        if not 1 <= limit <= 100 or before < 0 or message < 0 or len(q) > 500:
            raise ValueError("Invalid page or search")
        s = self.snapshot(room)
        needle = q.casefold().strip()
        matched = [m for m in s["messages"]
                   if (not author or m["author"] == author)
                   and (not kind or m.get("kind") == kind)
                   and (not message or m["seq"] == message)
                   and (not needle or needle in " ".join([m["body"], m["author"],
                         str(m.get("recipients", "")), str(m["seq"])]).casefold())]
        older = [m for m in matched if not before or m["seq"] < before]
        page = list(reversed(older[-limit:]))
        return {"room": room, "messages": page, "total": s["total"], "matched": len(matched),
                "latestSeq": s["latestSeq"], "nextBefore": page[-1]["seq"] if len(older) > limit else None,
                "authors": sorted({m["author"] for m in s["messages"]}),
                "invalidLines": s["invalidLines"], "partialTail": s["partialTail"], "readOnly": True}
