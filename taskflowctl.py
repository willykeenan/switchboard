#!/usr/bin/env python3
"""Task-scoped cooperative worker pickup. No policy, enrollment or wake commands."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from taskflow import TaskFlow, Conflict, digest
from store import data_root, rooms_root


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',default=str(data_root()),help='Data directory (default: $SWITCHBOARD_HOME or ~/.switchboard)')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('context');sub.add_parser('idle');sub.add_parser('offers')
    accept=sub.add_parser('accept');accept.add_argument('assignment');accept.add_argument('--payload-file',required=True)
    for op in ('learning-retain','learning-context','learning-prepare','ready','retain-episode','pid-recovery-prepare','annotate','annotation-access','start','progress','return','decline','fail','cancelled','recover','review','owner-ack','audit-idle','audit-pause','audit-claim','audit-library','audit-context','audit-release','audit-question','audit-answer','audit-resolve','audit-heartbeat','waiver-record','waiver-accept','waiver-finish'):
        cmd=sub.add_parser(op);cmd.add_argument('--file',required=True,help='JSON command values')
    for op in ('delivery-idle','delivery-pause','delivery-reconcile-artifact','delivery-configure','delivery-accept','delivery-start','delivery-heartbeat','delivery-return','delivery-fail','delivery-recover'):
        cmd=sub.add_parser(op);cmd.add_argument('--file',required=True)
    a=p.parse_args();identity=os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_SESSION_ID')
    if not identity:raise SystemExit('An exact CODEX_THREAD_ID or CLAUDE_SESSION_ID is required; no alternate actor may be supplied')
    from workflow import Workflow
    from workspace import Workspace
    from room_reader import RoomStore
    root=Path(a.root).expanduser().resolve();flow=Workflow(root,Workspace(root,RoomStore(rooms_root(root))));store=TaskFlow(root,flow)
    matches=[s['agent_id'] for s in flow.catalog()['sessions'] if s.get('endpoint')==identity]
    if len(matches)!=1:raise SystemExit('Exact provider identity is unregistered or ambiguous')
    actor=matches[0]
    if a.command=='context':result=store.context(actor)
    elif a.command=='idle':
        result=store.idle(actor,actor);store.dispatch(actor);result={'worker':result,**store.context(actor)}
    elif a.command=='offers':result=store.offers(actor)
    elif a.command=='accept':
        payload=json.loads(Path(a.payload_file).read_text())
        from taskflow_delivery import payload_files,files_current
        files_current(payload_files(payload))
        result=store.transition(a.assignment,actor,'accept',{'payloadHash':digest(payload)})
    else:
        values=json.loads(Path(a.file).read_text())
        if a.command=='ready':
            task=store.ready(values['taskId'],values['version'],values,actor)
            store.dispatch()
            result={'readyBy':actor,'task':store.get(task['taskId'])}
        elif a.command in ('learning-retain','learning-context','learning-prepare','retain-episode','pid-recovery-prepare','annotate','annotation-access','waiver-record','waiver-accept','waiver-finish'):result=store.mutate({'operation':a.command,'item':values},actor)
        elif a.command.startswith('delivery-'):result=store.delivery.mutate(a.command,values,actor)
        elif a.command.startswith('audit-'):result=store.audit.mutate(a.command,values,actor)
        elif a.command=='recover':result=store.recover(values['assignmentId'],actor,values.get('reason','Recover the exact retained assignment'))
        elif a.command=='review':result=store.review(values['taskId'],values['version'],actor,values['verdict'],values['summary'],values['evidence'],values.get('learningOutcome'))
        elif a.command=='owner-ack':
            through=values['through']
            if type(through) is not int:raise ValueError('Exact last read event sequence required')
            with store.connect() as db:db.execute('UPDATE taskflow_notifications SET read_at=? WHERE owner=? AND seq<=?',(store.clock(),actor,through))
            result={'readThrough':through}
        else:result=store.transition(values['assignmentId'],actor,a.command,values)
    if isinstance(result,dict):result.pop('receiptSecret',None);result.pop('supervisorClaim',None)
    if isinstance(result,list):
        for r in result:r.pop('receiptSecret',None);r.pop('supervisorClaim',None)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
