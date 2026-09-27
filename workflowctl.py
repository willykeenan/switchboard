#!/usr/bin/env python3
"""Exact-agent handoffs and informational notifications. Provider starts run in the supervised handoff daemon."""
import argparse
import json
import os
import sys
from pathlib import Path
from workflow import Workflow, allowed
from workspace import Workspace
from room_reader import RoomStore
from store import data_root, rooms_root

def inherited_identity(w,claimed=None):
    """The calling agent's own registered session, from its inherited provider identity; never from arguments alone."""
    found=[]
    if os.environ.get('CODEX_THREAD_ID'):found.append(w.context(os.environ['CODEX_THREAD_ID']).get('session'))
    if os.environ.get('CLAUDE_CODE_SESSION_ID'):found.append(w.context('claude:'+os.environ['CLAUDE_CODE_SESSION_ID']).get('session'))
    found=[f for f in dict.fromkeys(found) if f]
    if claimed:found=[f for f in found if f==claimed]
    if len(found)!=1:raise ValueError('Exact inherited Codex or Claude session identity required; an agent can only link itself')
    return found[0]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    c=sub.add_parser('context');c.add_argument('--session',default=os.environ.get('CODEX_THREAD_ID'));c.add_argument('--brief',action='store_true')
    c=sub.add_parser('check');c.add_argument('--from',dest='sender',required=True);c.add_argument('--to',dest='recipient',required=True)
    c=sub.add_parser('send');c.add_argument('--from',dest='sender',required=True);c.add_argument('--to',dest='recipient',required=True);c.add_argument('--message',required=True);c.add_argument('--key',required=True);c.add_argument('--notification',action='store_true',help='Information only; do not wake the recipient')
    c=sub.add_parser('handoffs');c.add_argument('--session')
    send_parser=sub.choices['send'];send_parser.add_argument('--work-file',type=Path)
    c=sub.add_parser('work-accept');c.add_argument('--message-id',required=True)
    c=sub.add_parser('work-return');c.add_argument('--message-id',required=True);c.add_argument('--summary',required=True);c.add_argument('--blocked',action='store_true');c.add_argument('--presentation-file',type=Path,help='Optional bounded plain-language title, result, remaining, nextStep and reported availability')
    c=sub.add_parser('work-close');c.add_argument('--message-id',required=True);c.add_argument('--outcome',choices=['accepted','revision','blocked'],required=True);c.add_argument('--note',required=True)
    c=sub.add_parser('link',help='Open your own two-way link to another agent');c.add_argument('--to',dest='recipient',required=True);c.add_argument('--reason',required=True);c.add_argument('--one-way',action='store_true',help='Only you -> them');c.add_argument('--from',dest='sender')
    c=sub.add_parser('unlink',help='Remove links agents opened between you and another agent');c.add_argument('--to',dest='recipient',required=True);c.add_argument('--from',dest='sender')
    c=sub.add_parser('links');c.add_argument('--session')
    c=sub.add_parser('read-ack');c.add_argument('--session',required=True);c.add_argument('--message-id',required=True)
    a=p.parse_args();root=data_root()
    w=Workflow(root,Workspace(root,RoomStore(rooms_root(root))))
    if a.command=='context':
        if not a.session:p.error('Exact session ID required')
        result=w.context(a.session)
        if a.brief:result['inbox']=[{k:m[k] for k in ('id','sender','body','created_at')} for m in result['inbox'][:8]]
    elif a.command=='handoffs':result=w.handoff_status(a.session)
    elif a.command=='link':result=w.link(inherited_identity(w,a.sender),a.recipient,a.reason,both=not a.one_way)
    elif a.command=='unlink':result=w.unlink(inherited_identity(w,a.sender),a.recipient)
    elif a.command=='links':result={'links':w.links(a.session)}
    elif a.command=='check':result={'allowed':allowed(w.read(),a.sender,a.recipient),'selfLink':'If not allowed and not blocked by the operator: workflowctl.py link --to '+a.recipient+' --reason "<why>"','delivery':'exact-agent handoff dispatcher; busy agents are deferred','wakeAllowed':allowed(w.read(),a.sender,a.recipient) and w.handoff_status().get('enabled',False),'interruptAllowed':False}
    elif a.command=='send':
        actual=os.environ.get('CODEX_THREAD_ID')
        canonical=w.context(actual).get('session') if actual else None
        if actual and a.sender not in ('codex:'+actual,canonical):raise ValueError('Sender must match this exact Codex session')
        work=None
        if a.work_file:
            if a.notification:raise ValueError('Work cannot be sent as a passive notification')
            if a.work_file.is_symlink() or not a.work_file.is_file() or a.work_file.stat().st_size>16384:raise ValueError('Use a bounded regular work contract')
            work=json.loads(a.work_file.read_text())
        result=w.send(canonical or a.sender,a.recipient,a.message,a.key,intent="notification" if a.notification else "handoff",work=work)
    elif a.command in ('work-accept','work-return','work-close'):
        actual=os.environ.get('CODEX_THREAD_ID')
        if not actual:raise ValueError('Exact inherited Codex identity required')
        actor=w.context(actual).get('session')
        if not actor:raise ValueError('Current task is not registered')
        from workflow_work import accept,finish,close
        if a.command=='work-accept':result=accept(w,actor,a.message_id)
        elif a.command=='work-return':
            from workflow_handoff_presentation import load
            result=finish(w,actor,a.message_id,a.summary,a.blocked,load(a.presentation_file) if a.presentation_file else None)
        else:result=close(w,actor,a.message_id,a.outcome,a.note)
    else:
        actual=os.environ.get('CODEX_THREAD_ID')
        canonical=w.context(actual).get('session') if actual else None
        if actual and a.session not in (actual,'codex:'+actual,canonical):raise ValueError('Cannot acknowledge another session inbox')
        aid=canonical or (a.session if ':' in a.session else 'codex:'+a.session)
        if not any(m['id']==a.message_id for m in w.messages(aid)):raise ValueError('Message is not in this permitted inbox')
        from workflow import now
        with w._connect() as db:db.execute('UPDATE workflow_messages SET read_at=? WHERE id=? AND recipient=?',(now(),a.message_id,aid))
        result={'acknowledged':a.message_id,'scope':'passive inbox only'}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError) as exc:print(json.dumps({'error':str(exc),'delivery':'HELD'}));sys.exit(1)
