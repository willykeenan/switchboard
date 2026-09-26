#!/usr/bin/env python3
"""LOCAL OPERATOR ONLY: issue/revoke routine scoped credentials. No HTTP grants or starts.
Same-UID full-access processes are trusted by this local boundary, not isolated.
"""
import argparse,os,stat,json
from pathlib import Path
from cycle_identity import CycleGrants

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True)
 sub=p.add_subparsers(dest='op',required=True);issue=sub.add_parser('issue');issue.add_argument('--subject',required=True);issue.add_argument('--lane',required=True);issue.add_argument('--ttl',type=int,default=3600);issue.add_argument('--output',required=True)
 revoke=sub.add_parser('revoke');revoke.add_argument('--grant-id',required=True)
 a=p.parse_args();root=Path(a.root).resolve();grants=CycleGrants(root/'runtime/cycle-identity/grants.json')
 if a.op=='revoke':print(json.dumps({'revoked':grants.revoke(a.grant_id)}));return
 from taskflow import TaskFlow
 from workflow import Workflow
 from workspace import Workspace
 from room_reader import RoomStore
 target=Path(a.output)
 if not target.is_absolute() or target.resolve()!=target or any(p.is_symlink() for p in [target,*target.parents]):raise ValueError('Exact private credential path required')
 s=target.parent.stat()
 if s.st_uid!=os.getuid() or s.st_mode&0o077:raise ValueError('Create an operator-owned 0700 output directory first')
 fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);key=None
 try:
  from store import rooms_root
  store=TaskFlow(root,Workflow(root,Workspace(root,RoomStore(rooms_root(root)))))
  token,key=grants.issue_cycle(store,a.subject,a.lane,a.ttl)
  with os.fdopen(fd,'w') as f:f.write(token+'\n');f.flush();os.fsync(f.fileno())
 except BaseException:
  if key:grants.revoke(key)
  target.unlink(missing_ok=True);raise
 print(json.dumps({'grantId':key,'credentialFile':str(target),'purpose':'routine-taskflow-v1','startsAuthorized':False}))
if __name__=='__main__':main()
