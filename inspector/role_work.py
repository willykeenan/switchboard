"""Bounded current-work projection. GET-only; no lifecycle/provider calls.

Uses the installed workflow_reader and workflow_work schema, without constructing
Workflow, reading provider stores, indexing, or opening caller-provided paths.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from contextlib import closing

PROJECT = 'demo'
LANE = 'workstream-demo-work'
def _receipt_root():
    override = os.environ.get('SWITCHBOARD_ROOT') or os.environ.get('SWITCHBOARD_HOME')
    return (Path(override).expanduser() if override else Path.home() / '.switchboard') / 'receipts'


RECEIPT_ROOT = _receipt_root()
PAGE = 10
MAX_RECEIPT = 1048576
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
# Unicode whitespace recognized by the existing contract preparer's str.strip.
SCOPE_WHITESPACE = (' \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680'
                    '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007'
                    '\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000')


def object_json(text, maximum=32768):
    if not isinstance(text, str) or len(text) > maximum:
        raise ValueError('Malformed or oversized work metadata')
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('Malformed work metadata')
    return value


class RoleWork:
    def __init__(self, database, reader, receipt_root=RECEIPT_ROOT, lanes=(LANE,), actor=None, page_size=PAGE):
        self.lanes=tuple(lanes)
        self.actor=actor
        if type(page_size) is not int or not 1<=page_size<=400:raise ValueError("Invalid page bound")
        self.page_size=page_size
        self.database = Path(database)
        self.reader = reader
        self.receipt_root = Path(receipt_root)

    def get(self, query):
        allowed = {'project', 'lane', 'kind', 'offset', 'id'}
        if set(query) - allowed or any(len(v) != 1 for v in query.values()):
            raise ValueError('One value per allowed query field required')
        q = {k: v[0] for k, v in query.items()}
        if q.get('project') != PROJECT or q.get('lane') != LANE:
            raise ValueError('Exact registered project scope required')
        kind = q.get('kind', 'work')
        if kind not in ('work', 'receipts'):
            raise ValueError('Unknown projection kind')
        mid = q.get('id')
        if mid is not None and (not UUID.fullmatch(mid) or 'offset' in q):
            raise ValueError('Exact message ID required; detail cannot page')
        offset = q.get('offset', '0')
        if not re.fullmatch(r'0|[1-9][0-9]{0,4}', offset):
            raise ValueError('Bounded offset required')
        if not self.database.is_file():
            raise RuntimeError('Current-work store unavailable')
        # Same-process reader avoids changing the canonical DB fd mode. SQL is
        # explicitly read-only and bounded in work as well as returned row count.
        with closing(self.reader(self.database, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            ticks = [0]
            def budget():
                ticks[0] += 1
                return ticks[0] > 2000  # interrupt on callback 2001; not a time deadline
            db.set_progress_handler(budget, 1000)
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            # Validate attribution before WHERE scope can discard a damaged row.
            # CASE is lazy: JSON accessors never receive invalid/non-object JSON.
            # This stays in the same read transaction and shared VM budget; no
            # full-store Python materialization or receipt access occurs here.
            # SQLite length(TEXT) and JSON functions can accept only the prefix
            # before raw NUL. instr scans the complete TEXT, so reject that
            # character before either operation (escaped JSON NUL is separate).
            # The -> JSON token retains NUL escapes that older json_extract
            # truncates. Remove escaped backslash pairs before looking for a
            # real NUL escape, preserving literal backslash-u foreign IDs.
            damaged = db.execute('''SELECT 1 FROM workflow_work WHERE CASE
                WHEN typeof(contract) != 'text' THEN 1
                WHEN instr(contract,char(0))>0 THEN 1
                WHEN length(contract)>32768 THEN 1
                WHEN NOT json_valid(contract) THEN 1
                WHEN json_type(contract) != 'object' THEN 1
                WHEN json_type(contract,'$.projectId') IS NOT 'text'
                  OR json_type(contract,'$.laneId') IS NOT 'text' THEN 1
                WHEN length(json_extract(contract,'$.projectId')) NOT BETWEEN 1 AND 200
                  OR length(json_extract(contract,'$.laneId')) NOT BETWEEN 1 AND 200 THEN 1
                WHEN length(trim(json_extract(contract,'$.projectId'),?))=0
                  OR length(trim(json_extract(contract,'$.laneId'),?))=0 THEN 1
                WHEN instr(replace(contract -> '$.projectId',?,''),?)>0
                  OR instr(replace(contract -> '$.laneId',?,''),?)>0 THEN 1
                WHEN (SELECT count(*) FROM json_each(contract)
                      WHERE key IN ('projectId','laneId')) != 2 THEN 1
                ELSE 0 END LIMIT 1''', (SCOPE_WHITESPACE, SCOPE_WHITESPACE,
                                       '\\\\', '\\u0000', '\\\\', '\\u0000')).fetchone()
            if damaged:
                raise ValueError('Work metadata damaged; coverage unavailable')
            where = "json_extract(w.contract,'$.projectId')=? AND json_extract(w.contract,'$.laneId')=?"
            where = "json_extract(w.contract,'$.projectId')=? AND json_extract(w.contract,'$.laneId') IN (" + ','.join('?' for _ in self.lanes) + ')'
            args = [PROJECT, *self.lanes]
            if self.actor:
                where += ' AND (m.sender=? OR m.recipient=?)'; args.extend([self.actor,self.actor])
            if kind == 'receipts':
                where += ' AND w.returned IS NOT NULL'
            if mid:
                where += ' AND w.message_id=?'; args.append(mid)
            query_sql = ("SELECT w.message_id,w.created,w.accepted,w.returned,w.closed,w.return_message_id,"
                         "CASE WHEN length(w.contract)<=32768 THEN w.contract END AS contract,"
                         "CASE WHEN length(w.result)<=32768 THEN w.result END AS result,"
                         "CASE WHEN length(w.closure)<=32768 THEN w.closure END AS closure,"
                         "substr(m.sender,1,200) AS sender,substr(m.recipient,1,200) AS recipient FROM workflow_work w "
                         'JOIN workflow_messages m ON m.id=w.message_id WHERE ' + where +
                         ' ORDER BY w.created DESC,w.message_id LIMIT ? OFFSET ?')
            result = db.execute(query_sql, (*args, 1 if mid else self.page_size + 1, 0 if mid else int(offset))).fetchall()
        if mid and not result:
            raise ValueError('Record unavailable in this project scope')
        rows = [self.project(dict(r)) for r in result[:self.page_size]]
        base = {'schema': 'ke.current-work.v1', 'project': PROJECT, 'lane': LANE,
                'observedAt': time.time(), 'coverage': 'Bounded page of current Birds work contracts; historical Task Board and Library index are separate.',
                'kind': kind, 'limit': self.page_size, 'requestedOffset': int(offset),
                'nextOffset': int(offset) + self.page_size if len(result) > self.page_size else None,
                'records': rows}
        if mid:
            row = rows[0]
            row['receipt'] = self.receipt(row)
            base['record'] = row
        return base

    def project(self, raw):
        contract = object_json(raw['contract'])
        returned = object_json(raw['result']) if raw['result'] else None
        closure = object_json(raw['closure']) if raw['closure'] else None
        if contract.get('schema') != 'ke.work-handoff.v1' or contract.get('projectId') != PROJECT or contract.get('laneId') not in self.lanes:
            raise ValueError('Invalid work contract scope')
        if not UUID.fullmatch(raw['message_id']) or not isinstance(contract.get('assignmentId'), str):
            raise ValueError('Invalid work identity')
        if bool(raw['returned']) != bool(returned) or bool(raw['closed']) != bool(closure):
            raise ValueError('Incomplete lifecycle metadata')
        if returned and returned.get('disposition') not in ('RESULT_REPORTED', 'BLOCKED'):
            raise ValueError('Unknown recorded return disposition')
        if closure and closure.get('outcome') not in ('accepted', 'revision', 'blocked'):
            raise ValueError('Unknown recorded closure disposition')
        if type(contract.get('dueSeconds')) is not int or not 60 <= contract['dueSeconds'] <= 86400:
            raise ValueError('Invalid deadline metadata')
        for key in ('created', 'accepted', 'returned', 'closed'):
            value = raw[key]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError('Invalid lifecycle timestamp')
        state = ('CLOSED / ' + closure['outcome'].upper() if raw['closed'] else
                 'RETURNED / ' + returned['disposition'] if raw['returned'] else
                 'ACTIVE / ACCEPTED' if raw['accepted'] else 'ACTIVE / AWAITING PICKUP')
        return {'id': raw['message_id'], 'project': PROJECT, 'lane': contract['laneId'],
                'assignmentId': contract['assignmentId'], 'title': contract['assignmentId'],
                'sender': raw['sender'], 'recipient': raw['recipient'], 'state': state,
                'createdAt': raw['created'], 'acceptedAt': raw['accepted'], 'returnedAt': raw['returned'],
                'closedAt': raw['closed'], 'closure': closure, 'return': returned,
                'returnMessageId': raw['return_message_id'], 'contract': contract,
                'overdue': not raw['closed'] and time.time() > raw['created'] + contract['dueSeconds'],
                'receipt': {'verification': 'NOT_RETURNED' if not raw['returned'] else 'NOT_CHECKED',
                            'recorded': returned.get('evidence') if returned else None},
                'meaning': 'Lifecycle disposition is not semantic audit, installation or learning.'}

    def receipt(self, row):
        receipt = row['receipt'].copy()
        if not row['returnedAt']:
            return receipt
        expected = receipt['recorded']
        try:
            if not isinstance(expected, dict) or expected.get('path') != row['contract']['resultPath']:
                raise ValueError('Return path does not match original contract')
            if not re.fullmatch(r'[0-9a-f]{64}', expected.get('sha256', '')):
                raise ValueError('No valid recorded SHA-256')
            path = Path(expected['path'])
            if '..' in path.parts or not path.is_absolute():
                raise ValueError('Unsafe bound receipt path')
            path.relative_to(self.receipt_root)
            # Walk from filesystem root with dir_fd + O_NOFOLLOW for every
            # component; never follow a raced parent symlink or a special file.
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in path.parts[1:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd); fd = child
                leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    before = os.fstat(leaf)
                    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_RECEIPT:
                        raise ValueError('Receipt is not a bounded regular file')
                    data = bytearray()
                    while len(data) <= MAX_RECEIPT:
                        block = os.read(leaf, min(65536, MAX_RECEIPT + 1 - len(data)))
                        if not block: break
                        data.extend(block)
                    after = os.fstat(leaf)
                finally:
                    os.close(leaf)
            finally:
                os.close(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or len(data) != before.st_size:
                raise ValueError('Receipt changed during read')
            actual = hashlib.sha256(data).hexdigest()
            receipt['observedSha256'] = actual
            if actual != expected['sha256'] or len(data) != expected.get('bytes'):
                raise ValueError('Receipt differs from recorded return hash or size')
            parsed = object_json(data.decode('utf-8'), MAX_RECEIPT)
            binding = {'actor': row['recipient'], 'assignmentId': row['assignmentId'], 'messageId': row['id']}
            if parsed.get('workHandoff') != binding:
                raise ValueError('Receipt identity does not match original obligation')
            receipt.update(verification='VERIFIED_RECORDED_HASH', checkedAt=time.time(),
                           recordedClaims={k: parsed[k] for k in ('verdict','state','ok','installationCleared','overallUserOutcomeComplete','sourcePackageAccepted') if k in parsed},
                           body=parsed)
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            receipt.update(verification='UNVERIFIED', reason=str(exc)[:240])
        return receipt
