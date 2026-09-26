#!/usr/bin/env python3
"""Local operator credential custody only. Never starts work; no live grants without authority."""
import argparse
import json
import os
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlparse
from settings_identity import GrantStore

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True);p.add_argument('--url',default='http://127.0.0.1:47834')
    sub=p.add_subparsers(dest='command',required=True)
    issue=sub.add_parser('issue');issue.add_argument('--subject',required=True);issue.add_argument('--lane',required=True);issue.add_argument('--out',required=True);issue.add_argument('--ttl',type=int,default=3600)
    revoke=sub.add_parser('revoke');revoke.add_argument('--grant-id',required=True)
    a=p.parse_args();url=a.url.rstrip('/');u=urlparse(url)
    if u.scheme!='http' or u.hostname not in ('127.0.0.1','localhost') or u.path or u.query or u.fragment or u.username: p.error('Exact local board origin required')
    store=GrantStore(Path(a.root)/'runtime/settings-identity/grants.json')
    if a.command=='revoke': print(json.dumps({'revoked':store.revoke(a.grant_id)}));return
    # Create credential output exclusively before issuing, so existing secrets survive.
    fd=os.open(a.out,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);key=None
    try:
        with urlopen(url+'/api/workflow',timeout=15) as r:state=json.load(r)
        token,key=store.issue(state,a.subject,a.lane,a.ttl)
        with os.fdopen(fd,'w') as f:fd=None;f.write(token+'\n');f.flush();os.fsync(f.fileno())
        print(json.dumps({'grantId':key,'credentialFile':a.out,'effect':'settings-only; no work approval or start'}))
    except Exception:
        if key:store.revoke(key)
        if fd is not None:os.close(fd)
        Path(a.out).unlink(missing_ok=True)
        raise

if __name__=='__main__':main()
