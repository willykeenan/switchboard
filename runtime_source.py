"""Startup dependency provenance generated on install or first run."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


PACKAGES = ("agents", "inspector", "custom_capabilities", "switchboard")
SKIP_DIRS = {"tests", "__pycache__", ".git", "vendor"}


def runtime_files(board_root):
    board_root = Path(board_root)
    paths = [p for p in board_root.glob("*.py") if p.is_file() and not p.is_symlink()]
    for package in PACKAGES:
        pkg = board_root / package
        if not pkg.is_dir() or pkg.is_symlink():
            continue
        for path in pkg.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(pkg).parts):
                continue
            if path.suffix in (".pyc", ".pyo"):
                continue
            if path.suffix in (".py", ".js", ".mjs", ".css", ".html", ".json") or path.name in (
                "PROVENANCE.json",
            ):
                paths.append(path)
    return sorted(paths, key=lambda p: str(p))


def write_manifest(board_root):
    board_root = Path(board_root).resolve()
    files = {}
    for path in runtime_files(board_root):
        name = str(path.relative_to(board_root))
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = {"schemaVersion": "ke.runtime-source-manifest.v1", "files": files}
    destination = board_root / "runtime-source-manifest.json"
    tmp = destination.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    tmp.replace(destination)
    return spec


def live_files(board_root):
    board_root = Path(board_root).resolve()
    return {str(path.relative_to(board_root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in runtime_files(board_root)}


def snapshot(board_root, generate=False):
    board_root = Path(board_root).resolve()
    manifest = board_root / "runtime-source-manifest.json"
    if generate:
        spec = write_manifest(board_root)
        raw = manifest.read_bytes()
    elif not manifest.exists():
        files = live_files(board_root)
        return {
            "schemaVersion": "ke.runtime-startup-source.v1",
            "basis": "Live hashed local source files; run python3 -m switchboard --install to pin",
            "manifestSha256": None,
            "files": files,
        }
    else:
        raw = manifest.read_bytes()
        spec = json.loads(raw)
    if (
        set(spec) != {"schemaVersion", "files"}
        or spec["schemaVersion"] != "ke.runtime-source-manifest.v1"
        or not spec["files"]
    ):
        raise ValueError("Runtime dependency manifest is invalid")
    declared = spec["files"]
    actual_names = {str(p.relative_to(board_root)) for p in runtime_files(board_root)}
    if set(declared) != actual_names:
        raise ValueError("Runtime dependency manifest is incomplete or contains absent files")
    values = {}
    for name, expected in sorted(declared.items()):
        path = board_root / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink():
            raise ValueError("Unsafe runtime dependency path")
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != expected:
            raise ValueError("Runtime dependency source drift: " + name)
        values[name] = observed
    return {
        "schemaVersion": "ke.runtime-startup-source.v1",
        "basis": "Complete declared local runtime dependency files read and hashed at server startup; not proof every module was executed",
        "manifestSha256": hashlib.sha256(raw).hexdigest(),
        "files": values,
    }
