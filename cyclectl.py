#!/usr/bin/env python3
"""Routine scoped cycle commands. No policy, enrollment, approval/start or hire calls."""
import argparse,json,os,stat,sys
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.parse import urlparse
from urllib.error import HTTPError

def secret(path):
 with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW)) as f:
  s=os.fstat(f.fileno())
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077 or s.st_size>512:raise ValueError('Credential must be a bounded owner-only regular file')
  return f.read().strip()

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',default='http://127.0.0.1:47834');p.add_argument('--credential-file',required=True)
 p.add_argument('operation',choices=['bind-legacy-origin','context','capture','ready','delivery-configure','review-setup-return','accept-setup-return','audit-library','audit-context','audit-question','audit-resolve','review','progress','return','fail']);p.add_argument('--file')
 a=p.parse_args();url=urlparse(a.url)
 if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:raise ValueError('Exact loopback board URL required')
 item=json.loads(Path(a.file).read_text()) if a.file else {}
 req=Request(a.url.rstrip('/')+'/api/taskflow-cycle',data=json.dumps({'operation':a.operation,'item':item}).encode(),headers={'Content-Type':'application/json','Origin':a.url.rstrip('/'),'Authorization':'Bearer '+secret(a.credential_file)})
 try:
  with urlopen(req,timeout=30) as response:print(json.dumps(json.load(response),indent=2))
 except HTTPError as e:print(e.read().decode(),file=sys.stderr);raise SystemExit(2)
if __name__=='__main__':main()
