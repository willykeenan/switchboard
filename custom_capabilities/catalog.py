"""Bounded, read-only inventory of the custom local skill sources on this machine.

No provider is contacted and no skill is executed. Vendor plugin caches, project
files, memories, credentials and employment work product are not catalog inputs.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

PROVIDERS = ("Codex", "Claude", "Grok")
MAX_SOURCE_BYTES = 262144
MAX_SOURCES = 512
FEATURED_COUNT = 4

# Every entry comes from a discovered SKILL.md front matter. Nothing is hardcoded,
# so the catalog only ever describes skills that exist on this machine.


def metadata(text):
    """Parse only simple skill front matter; do not evaluate YAML or instructions."""
    if not text.startswith("---\n"):
        return {}
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return {}
    out, key = {}, None
    for line in parts[0].splitlines()[1:]:
        match = re.match(r"^(name|description):\s*(.*)$", line)
        if match:
            key, value = match.groups()
            out[key] = "" if value in (">", ">-", "|", "|-") else value.strip("\"'")
        elif line.startswith("  ") and key:
            out[key] = (out[key] + " " + line.strip()).strip()
        else:
            key = None
    return out


def enabled_personal_plugins(home):
    # Extract only the exact personal-plugin enable flags. No secret/config values
    # are returned. This also works with the board's system Python 3.9.
    path = home / ".codex/config.toml"
    try:
        raw = path.read_text()
    except OSError:
        return {}, ["Codex plugin enablement could not be checked."]
    enabled = {}
    for block in re.split(r"(?m)^\[", raw)[1:]:
        header, _, body = block.partition("]")
        match = re.fullmatch(r'plugins\."([a-z0-9-]+)@personal"', header.strip())
        if match:
            flag = re.search(r"(?m)^\s*enabled\s*=\s*(true|false)\s*(?:#.*)?$", body)
            enabled[match.group(1)] = bool(flag and flag.group(1) == "true")
    return enabled, []


def discover(home):
    roots = [("Codex", ".codex/skills"), ("Codex", ".agents/skills"),
             ("Claude", ".claude/skills"), ("Grok", ".grok/skills")]
    paths, warnings, coverage = [], [], []
    for provider, relative in roots:
        root = home / relative
        try:
            found = sorted(p for p in root.glob("*/SKILL.md") if not p.parent.name.startswith("."))
            coverage.append({"path": str(root), "status": "scanned" if root.is_dir() else "absent", "files": len(found)})
            paths.extend((provider, p, "local-skill") for p in found)
        except OSError:
            warnings.append("Could not scan " + str(root))
    enabled, notices = enabled_personal_plugins(home)
    warnings.extend(notices)
    plugin_root = home / ".codex/plugins/cache/personal"
    for plugin, active in sorted(enabled.items()):
        if not active:
            continue
        versions = sorted((plugin_root / plugin).glob("*/skills"), key=lambda p: p.parent.name)
        if not versions:
            warnings.append("Enabled custom plugin has no skill source: " + plugin)
            continue
        # Only one version per plugin, so old caches do not create duplicates.
        version = versions[-1]
        found = sorted(version.glob("*/SKILL.md"))
        coverage.append({"path": str(version), "status": "enabled-plugin", "files": len(found)})
        paths.extend(("Codex", p, "enabled-plugin") for p in found)
    if len(paths) > MAX_SOURCES:
        warnings.append("Custom source limit reached; inventory is incomplete.")
    return paths[:MAX_SOURCES], warnings, coverage


def build_catalog(home=None):
    home = Path(home) if home else Path.home()
    found, warnings, coverage = discover(home)
    entries = {}
    for provider, path, kind in found:
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                warnings.append("Oversize custom skill omitted: " + str(path))
                continue
            raw = path.read_bytes()
            front = metadata(raw.decode("utf-8"))
            name = front.get("name", path.parent.name)
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:_-]{0,95}", name):
                warnings.append("Invalid skill name omitted: " + str(path))
                continue
            key = name
            entry = entries.setdefault(key, {"id": key, "sources": [], "registeredProviders": []})
            entry["sources"].append({"path": str(path), "provider": provider, "kind": kind,
                                     "name": name, "sha256": hashlib.sha256(raw).hexdigest()})
            if provider not in entry["registeredProviders"]:
                entry["registeredProviders"].append(provider)
            entry.setdefault("discoveredDescription", front.get("description") or "Read the installed skill for usage.")
        except (OSError, UnicodeError):
            warnings.append("Unreadable custom skill omitted: " + str(path))
    for key, entry in entries.items():
        summary = entry.pop("discoveredDescription", "Installed custom skill.")[:400]
        entry.update(title=key.replace("-", " ").replace("_", " ").title(), category="Custom skills",
                     summary=summary, invocation="$" + key, scope="Machine",
                     policy="Read the current skill and host instructions before use.", featured=False,
                     state="Installed", sharedSourceReadable=True,
                     providerAccess=[{"provider": p, "status": "Skill installed" if p in entry["registeredProviders"] else "Shared source"} for p in PROVIDERS])
        entry["registeredProviders"].sort()
        entry["sources"].sort(key=lambda x: (x["provider"], x["path"]))
    items = sorted(entries.values(), key=lambda e: (e["title"].casefold(), e["id"]))
    for entry in items[:FEATURED_COUNT]:
        entry["featured"] = True
    identity = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    return {"schemaVersion": "ke.custom-capabilities.v1", "generatedAt": datetime.now(timezone.utc).isoformat(),
            "revision": hashlib.sha256(identity).hexdigest(), "capabilities": items,
            "total": len(items), "sourceCount": sum(len(e["sources"]) for e in items),
            "categories": sorted(set(e["category"] for e in items)), "providers": list(PROVIDERS),
            "coverage": coverage, "warnings": warnings, "complete": not warnings,
            "accessNote": "Shared local source is readable by agents running as this OS user. Skill installed means a source exists in that runtime's configured skill location; it does not prove this live turn loaded it or that its provider tools are connected.",
            "scopeNote": "Custom local skills and enabled personal Codex plugins. Vendor bundles and project work product are excluded.",
            "policySource": "WORKFLOW-CONTROL.md"}
