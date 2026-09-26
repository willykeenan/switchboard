#!/usr/bin/env python3
"""Role-bound library operations. Never creates, assigns or starts an agent."""
import argparse
import json
import sys
from pathlib import Path
from inspector.library import Library
from inspector.context import context, save
from inspector.governance import role_contract

def execute(flow, library, session, action, query='', item=None):
    contract = role_contract(flow, session)
    if not contract or ('context' if action=='workspace' else 'note' if action=='lifecycle' else action) not in contract.get('library', {}).get('actions', []):
        raise ValueError('This exact session has no assigned library duty for this action')
    scope = {k: contract[k] for k in ('project', 'lane', 'team')}
    library=library.for_actor(session)
    if action == 'workspace':
        from inspector.lifecycle import snapshot
        return snapshot(library, **scope)
    if action == 'context':
        return {**context(library, **scope), 'role': contract}
    if action == 'search':
        return library.query(q=query, **scope)
    if not isinstance(item, dict):
        raise ValueError('Provide a JSON record with --file')
    if any(k in item and item[k] != v for k, v in scope.items()):
        raise ValueError('Record scope differs from this session’s assigned library duty')
    if 'actor' in item and item['actor'] != session:
        raise ValueError('Record actor differs from the exact assigned session')
    payload = {**item, **scope, 'actor': session}
    if action == 'save-context':
        return save(library, payload)
    if action == 'lifecycle':
        from inspector.lifecycle import write
        return write(library, payload)
    return library.write(action, payload)

def main():
    from workflow import Workflow
    from workspace import Workspace
    from room_reader import RoomStore
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['context', 'search', 'review', 'note', 'save-context', 'workspace', 'lifecycle'])
    parser.add_argument('--session', required=True)
    parser.add_argument('--query', default='')
    parser.add_argument('--file')
    args = parser.parse_args()
    from store import data_root, rooms_root
    root = data_root()
    flow = Workflow(root, Workspace(root, RoomStore(rooms_root(root))))
    item = json.loads(Path(args.file).read_text()) if args.file else None
    result = execute(flow, Library(root, flow), args.session, args.action, args.query, item)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print(json.dumps({'error': str(error)}))
        sys.exit(2)
