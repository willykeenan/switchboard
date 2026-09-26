"""Read the existing routed message ledger without acknowledging or dispatching."""
import base64
import json
import sqlite3
from .transcripts import safe_text


def page_cursor(row, agent, lane):
    raw = json.dumps([row['created_at'], row['id'], agent, lane], separators=(',', ':')).encode()
    return 'v1.' + base64.urlsafe_b64encode(raw).decode().rstrip('=')


def cursor_position(value, agent, lane):
    try:
        if not isinstance(value, str) or not value.startswith('v1.') or len(value) > 4096:
            raise ValueError()
        encoded = value[3:]
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        parts = json.loads(raw)
        if not isinstance(parts, list) or len(parts) != 4 or parts[2:] != [agent, lane]:
            raise ValueError()
        created, identifier = parts[:2]
        if not isinstance(created, str) or not 0 < len(created) <= 128:
            raise ValueError()
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
            raise ValueError()
        return created, identifier
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('Invalid or outdated message cursor. Refresh the message list.') from None


class Messages:
    def __init__(self, workflow):
        self.workflow = workflow

    def page(self, agent='', lane='', before=''):
        state = self.workflow.snapshot()
        ids = {s['agent_id'] for s in state['sessions']}
        if agent and agent not in ids:
            raise ValueError('Unknown agent')
        position = cursor_position(before, agent, lane) if before else None
        members = [p['agentId'] for p in state['placements'] if p['laneId'] == lane] if lane else []
        where, args = [], []
        if agent:
            where.append('(sender=? OR recipient=?)')
            args.extend([agent, agent])
        elif lane:
            if not members:
                return {'items': [], 'nextBefore': None, 'workflowRevision': state['revision']}
            marks = ','.join('?' for _ in members)
            where.append(f'(sender IN ({marks}) OR recipient IN ({marks}))')
            args.extend(members * 2)
        if position:
            created, identifier = position
            where.append('(created_at<? OR (created_at=? AND id<?))')
            args.extend([created, created, identifier])
        from workflow import allowed
        with sqlite3.connect(self.workflow.path.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(
                'SELECT * FROM workflow_messages' + (' WHERE ' + ' AND '.join(where) if where else '')
                + ' ORDER BY created_at DESC,id DESC LIMIT 101', args)]
        for row in rows:
            row['body'] = safe_text(row['body'])
            permitted = allowed(state, row['sender'], row['recipient'])
            row['permittedNow'] = permitted
            row['status'] = 'HELD' if not permitted else 'READ' if row['read_at'] else 'QUEUED'
        return {'items': rows[:100], 'nextBefore': page_cursor(rows[99], agent, lane) if len(rows) > 100 else None,
                'coverage': 'Messages routed through the workflow ledger', 'workflowRevision': state['revision'],
                'cursorVersion': 1}
