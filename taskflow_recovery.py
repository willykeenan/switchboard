"""Bounded current-state export; never traverse the board's legacy archive.

All TaskFlow tables (including audit and history) share one pinned read
transaction. A restored copy retains its original instance binding and cannot
dispatch. Oversize/slow snapshots fail explicitly without truncating state.
"""
import contextlib
from pathlib import Path
import re
import sqlite3
import time

MAX_BYTES = 32 * 1024 * 1024
MAX_SECONDS = 5
NAME = re.compile(r'^taskflow_[a-z0-9_]+$')


def snapshot(path, target, max_bytes=MAX_BYTES, max_seconds=MAX_SECONDS):
    path, target = Path(path), Path(target)
    if path.is_symlink() or target.exists() or target.is_symlink():
        raise ValueError('Use a regular source and a new TaskFlow snapshot file')
    path = path.resolve()
    started = time.monotonic()
    expired = lambda: int(time.monotonic() - started > max_seconds)
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as src:
            src.set_progress_handler(expired, 1000)
            src.execute('PRAGMA query_only=ON')
            src.execute('BEGIN')
            tables = src.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name GLOB 'taskflow_*' ORDER BY name").fetchall()
            if not tables:
                return None  # Historical board before TaskFlow was installed.
            names = [name for name, _ in tables]
            if 'taskflow_instance' not in names or any(not NAME.fullmatch(name) for name in names):
                raise ValueError('Incomplete or unexpected TaskFlow recovery schema')
            indexes = src.execute("SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND tbl_name GLOB 'taskflow_*' ORDER BY name").fetchall()
            if src.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name GLOB 'taskflow_*'").fetchone():
                raise ValueError('TaskFlow recovery does not support unreviewed triggers')
            with contextlib.closing(sqlite3.connect(target, timeout=1)) as dst:
                dst.set_progress_handler(expired, 1000)
                dst.execute('PRAGMA page_size=4096')
                dst.execute('PRAGMA max_page_count=' + str(max(1, max_bytes // 4096)))
                dst.execute('BEGIN')
                counts = {}
                for name, sql in tables:
                    if not sql or not sql.upper().startswith('CREATE TABLE '):
                        raise ValueError('Unsupported TaskFlow recovery table')
                    dst.execute(sql)
                    # Cursor iteration streams rows; no fetchall of task history.
                    rows = src.execute('SELECT * FROM ' + quote(name))
                    columns = [column[0] for column in rows.description]
                    insert = 'INSERT INTO ' + quote(name) + ' (' + ','.join(map(quote, columns)) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
                    count = 0
                    for row in rows:
                        if expired():
                            raise ValueError('TaskFlow snapshot time budget exceeded')
                        dst.execute(insert, row)
                        count += 1
                    counts[name] = count
                for table, sql in indexes:
                    if table not in names:
                        raise ValueError('Unexpected TaskFlow recovery index')
                    dst.execute(sql)
                # Preserve the monotonic high-water mark even after deleted rows.
                if src.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                    for name, seq in src.execute("SELECT name,seq FROM sqlite_sequence WHERE name GLOB 'taskflow_*'"):
                        if name not in names:
                            raise ValueError('Unexpected TaskFlow sequence')
                        dst.execute('DELETE FROM sqlite_sequence WHERE name=?', (name,))
                        dst.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (name, seq))
                dst.commit()
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('TaskFlow recovery database failed validation')
                if dst.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('TaskFlow recovery has missing referenced state')
            if target.stat().st_size > max_bytes or expired():
                raise ValueError('TaskFlow snapshot size or time budget exceeded')
            return {'kind': 'taskflow-tables', 'tables': counts, 'bytes': target.stat().st_size,
                    'maxBytes': max_bytes, 'maxSeconds': max_seconds,
                    'legacyArchiveCopied': False, 'perStoreSnapshotConsistent': True,
                    'crossStoreAtomicityClaimed': False}
    except (sqlite3.Error, ValueError) as error:
        raise ValueError('Bounded TaskFlow snapshot failed; preserve previous backup and source: ' + str(error)) from error
