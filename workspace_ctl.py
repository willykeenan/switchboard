#!/usr/bin/env python3
"""Manage project/lane membership or post one message scoped to several lanes."""
import argparse
import json
import sys
import board_core as board
from room_reader import RoomStore
from store import rooms_root
from workspace import Workspace

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('show')
    c = sub.add_parser('context'); c.add_argument('--endpoint', required=True)
    a = sub.add_parser('apply'); a.add_argument('--file',required=True); a.add_argument('--revision',type=int,required=True); a.add_argument('--actor',required=True)
    m = sub.add_parser('post')
    for key in ('project','author','to'):
        m.add_argument('--'+key, required=True)
    m.add_argument('--lane',action='append',required=True)
    m.add_argument('--message'); m.add_argument('--kind',default='message'); m.add_argument('--key')
    args = p.parse_args(); w = Workspace(board.ROOT, RoomStore(rooms_root(board.ROOT)))
    if args.command == 'show': result = w.snapshot()
    elif args.command == 'context': result = w.context(args.endpoint)
    elif args.command == 'apply':
        with open(args.file) as f: document = json.load(f)
        result = w.save(document, args.revision, args.actor)
    else: result = w.post(args.project,args.lane,args.author,args.message or sys.stdin.read(),args.kind,args.to.split(','),args.key)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
