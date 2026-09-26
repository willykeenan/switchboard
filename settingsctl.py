#!/usr/bin/env python3
"""Read/update through the same settings HTTP API. No direct DB or role override."""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:47834')
    sub=parser.add_subparsers(dest='command',required=True)
    read=sub.add_parser('read');read.add_argument('--agent',required=True)
    sub.add_parser('models')
    update=sub.add_parser('update');update.add_argument('--file',required=True)
    args=parser.parse_args();url=args.url.rstrip('/');parsed=urlparse(url)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.path or parsed.query or parsed.fragment or parsed.username: parser.error('Use the exact loopback board origin')
    headers={'Origin':url,'Content-Type':'application/json'}
    # Supplied transport credential must be verified by the host broker. An
    # arbitrary token, session ID or CODEX_THREAD_ID is never a principal.
    credential=os.environ.get('KE_SETTINGS_CREDENTIAL')
    if credential: headers['Authorization']='Bearer '+credential
    if args.command=='update':
        token=os.environ.get('SWITCHBOARD_CSRF_TOKEN')
        if not token: parser.error('SWITCHBOARD_CSRF_TOKEN is required; this CSRF token does not authenticate you')
        headers['X-KE-Board-Token']=token
        body=json.loads(Path(args.file).read_text());path='/api/agent-settings';payload=json.dumps(body).encode()
    else:
        path='/api/agent-settings/models' if args.command=='models' else '/api/agent-settings?'+urlencode({'agent':args.agent});payload=None
    try:
        with urlopen(Request(url+path,data=payload,headers=headers),timeout=15) as response: result=json.load(response)
    except HTTPError as exc:
        print(json.dumps({'httpStatus':exc.code,**json.load(exc)},indent=2));return 1
    except (URLError, TimeoutError, OSError) as exc:
        print(json.dumps({'error':'Settings backend unavailable; nothing was saved','detail':str(exc)},indent=2));return 1
    print(json.dumps(result,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
