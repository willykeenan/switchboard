"""SQLite helpers shared by the local board stores. No provider access."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def busy(exc):
    return isinstance(exc, sqlite3.OperationalError) and any(
        x in str(exc).lower()
        for x in ("database is locked", "database is busy", "database table is locked")
    )


def annotate(exc, path, operation=None):
    if operation is None:
        operation, path = path, None
    if isinstance(exc, sqlite3.Error):
        if not hasattr(exc, "production_operation"):
            exc.production_operation = operation
        if not getattr(exc, "productionOperation", None):
            exc.productionOperation = operation
        if path is not None and not getattr(exc, "productionDatabase", None):
            exc.productionDatabase = str(path)
    return exc


class ClosingConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        try:
            return super().execute(sql, *args, **kwargs)
        except sqlite3.Error as exc:
            annotate(exc, "execute:" + str(sql).strip().split(None, 1)[0].upper())
            raise

    def executescript(self, *args, **kwargs):
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.Error as exc:
            annotate(exc, "schema-script")
            raise

    def __exit__(self, *args):
        try:
            try:
                return super().__exit__(*args)
            except sqlite3.Error as exc:
                annotate(exc, "rollback" if args[0] else "commit")
                raise
        finally:
            self.close()


CONNECTION_PHASES = frozenset(
    ("synchronous-read", "synchronous-full", "schema-probe", "schema-create", "ready")
)


@contextmanager
def connection_phase(db, phase):
    if phase not in CONNECTION_PHASES:
        raise ValueError("Unknown store connection diagnostic phase")
    db.productionPhase = phase
    try:
        yield
    except sqlite3.Error as exc:
        connection_fault(exc, db)
        raise


def connection_fault(exc, db):
    if hasattr(exc, "productionConnection") or not getattr(db, "productionConnectionId", None):
        return
    try:
        phase = getattr(db, "productionPhase", "ready")
        created = getattr(db, "productionOpenedAt", None)
        try:
            in_transaction = bool(db.in_transaction)
        except sqlite3.Error:
            in_transaction = None
        exc.productionConnection = {
            "phase": phase if phase in CONNECTION_PHASES else "ready",
            "connectionId": getattr(db, "productionConnectionId", None),
            "ageSeconds": max(0, time_monotonic() - created) if created is not None else None,
            "inTransaction": in_transaction,
            "busyTimeoutSeconds": 1,
            "lastSuccess": getattr(db, "productionLastSuccess", None),
        }
    except Exception:
        pass


def time_monotonic():
    import time

    return time.monotonic()


def statement_kind(sql):
    head = str(sql).lstrip().split(None, 1)[0].upper() if str(sql).strip() else ""
    return (
        head
        if head
        in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
            "PRAGMA",
            "CREATE",
        )
        else "SQL"
    )


class DiagnosticCursor(sqlite3.Cursor):
    def execute(self, sql, *args, **kwargs):
        self.operation = statement_kind(sql)
        try:
            result = super().execute(sql, *args, **kwargs)
            self.connection.productionLastSuccess = "execute:" + self.operation
            return result
        except sqlite3.Error as exc:
            annotate(exc, self.connection.productionDatabase, "execute:" + self.operation)
            connection_fault(exc, self.connection)
            raise

    def fetchone(self):
        try:
            return super().fetchone()
        except sqlite3.Error as exc:
            annotate(
                exc,
                self.connection.productionDatabase,
                "fetch:" + getattr(self, "operation", "SQL"),
            )
            connection_fault(exc, self.connection)
            raise

    def fetchall(self):
        try:
            return super().fetchall()
        except sqlite3.Error as exc:
            annotate(
                exc,
                self.connection.productionDatabase,
                "fetch:" + getattr(self, "operation", "SQL"),
            )
            connection_fault(exc, self.connection)
            raise

    def __next__(self):
        try:
            return super().__next__()
        except sqlite3.Error as exc:
            annotate(
                exc,
                self.connection.productionDatabase,
                "iterate:" + getattr(self, "operation", "SQL"),
            )
            connection_fault(exc, self.connection)
            raise


class DiagnosticConnection(ClosingConnection):
    def cursor(self, factory=DiagnosticCursor):
        return super().cursor(factory)

    def execute(self, sql, *args, **kwargs):
        try:
            return self.cursor().execute(sql, *args, **kwargs)
        except sqlite3.Error as exc:
            annotate(exc, self.productionDatabase, "execute:" + statement_kind(sql))
            raise

    def executescript(self, sql, *args, **kwargs):
        try:
            return super().executescript(sql, *args, **kwargs)
        except sqlite3.Error as exc:
            annotate(exc, self.productionDatabase, "schema-script")
            connection_fault(exc, self)
            raise

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        except sqlite3.Error as exc:
            annotate(exc, self.productionDatabase, "transaction-exit")
            raise


def query_only_existing(path, timeout=5):
    db = sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=rw",
        uri=True,
        timeout=timeout,
        factory=ClosingConnection,
    )
    try:
        db.execute("PRAGMA query_only=ON")
        return db
    except BaseException:
        db.close()
        raise


def safe_path(path):
    p = Path(path).absolute()
    if p.is_symlink() or any(q.is_symlink() for q in p.parents):
        raise ValueError("Symlink path rejected")
    return p.resolve()


ordinary = safe_path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    with tmp.open("x") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, path)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def read_managed_agents(_root):
    """Production runner removed; managed hire records are not part of this release."""
    return []


def data_root():
    """Configurable data directory. Tests may set SWITCHBOARD_ROOT."""
    override = os.environ.get("SWITCHBOARD_ROOT") or os.environ.get("SWITCHBOARD_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".switchboard"


def rooms_root(root=None):
    """Room history directory. Always inside the data directory unless
    SWITCHBOARD_ROOMS_ROOT points elsewhere explicitly."""
    override = os.environ.get("SWITCHBOARD_ROOMS_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(root if root is not None else data_root()) / "rooms"


LAUNCH_ENV = "SWITCHBOARD_ALLOW_LAUNCH"


def launch_allowed():
    """Starting provider CLI processes (codex/claude) is off unless the operator
    explicitly opts in with SWITCHBOARD_ALLOW_LAUNCH=1 (or --allow-launch)."""
    return os.environ.get(LAUNCH_ENV, "").strip().lower() in ("1", "true", "yes")


def cpu_registry():
    """Optional directory where handoff/index jobs publish a small status file."""
    override = os.environ.get("SWITCHBOARD_CPU_REGISTRY")
    if override:
        return Path(override).expanduser()
    return data_root() / "runtime" / "cpu-workers" / "jobs"
