#!/usr/bin/env python3
"""Background handoff dispatcher and read-only local visibility. Never focuses UI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from workflow import Workflow
from workspace import Workspace
from room_reader import RoomStore
from store import data_root, rooms_root
from workflow_handoffs import Handoffs,atomic_json
from workflow_handoff_transport import DesktopTransport
from workflow_handoff_view import PAGE, decorate_snapshot

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=data_root(),help='Data directory (default: $SWITCHBOARD_HOME or ~/.switchboard)');p.add_argument('--port',type=int,default=47836);p.add_argument('--once',action='store_true');a=p.parse_args()
    loaded={n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest() for n in ['workflow.py','workflow_handoffs.py','workflow_handoff_transport.py','workflow_handoffd.py','workflow_work.py','workflow_handoff_view.py']}
    flow=Workflow(a.root,Workspace(a.root,RoomStore(rooms_root(a.root))));h=Handoffs(flow,DesktopTransport(flow))
    if a.once:h.tick();return
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            if self.path not in ('/','/api/status'):
                self.send_error(404);return
            status=200
            if self.path=='/':
                # The shell must remain available when a snapshot cannot be read.
                body=PAGE.encode();kind='text/html; charset=utf-8'
            else:
                kind='application/json'
                try:
                    snap=h.snapshot();snap['loadedSource']=loaded
                    sessions=flow.identity_catalog()['sessions']
                    for r in snap['handoffs']:
                        r['senderName']=flow.display_identity(r['sender'],sessions)
                        r['recipientName']=flow.display_identity(r['recipient'],sessions)
                    from workflow_work import snapshot
                    snap['work']=snapshot(flow)
                    snap['workNeedsAttention']=sum(w['needsAttention'] for w in snap['work'])
                    decorate_snapshot(flow, snap, sessions)
                    body=json.dumps(snap).encode()
                except Exception:
                    # An unavailable snapshot is never a successful empty queue.
                    status=503
                    body=b'{"error":"Handoff status temporarily unavailable"}'
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

    http=ThreadingHTTPServer(('127.0.0.1',a.port),Handler);threading.Thread(target=http.serve_forever,daemon=True).start()
    while True:
        try:h.tick()
        except Exception as e:atomic_json(h.health_path,{'at':time.time(),'ok':False,'pid':os.getpid(),'error':str(e)[:600]})
        time.sleep(5)
if __name__=='__main__':main()
