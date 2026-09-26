#!/usr/bin/env python3
"""Command line control for the Switchboard incident board."""

from __future__ import annotations

import argparse
import json
import os
import sys

import board_core as board
import coordination


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def csv(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def cmd_init(_):
    board.init_db()
    emit(board.snapshot(10))


def cmd_team(args):
    emit(board.upsert_team(args.name, args.display_name or args.name.title(), args.description, actor=args.actor))


def cmd_check_in(args):
    team = board.infer_team(args.cwd) if args.team.upper() == "AUTO" else args.team.upper()
    endpoint = args.endpoint or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or ""
    if not endpoint:
        raise SystemExit("endpoint is required (or set CODEX_THREAD_ID/CODEX_SESSION_ID)")
    agent_id = args.agent_id or f"{args.provider.lower()}:{endpoint}"
    board.init_db()
    with board.connect() as c:
        existing=c.execute('SELECT * FROM agents WHERE agent_id=?',(agent_id,)).fetchone()
        if not existing and not args.agent_id:
            matches=c.execute('SELECT * FROM agents WHERE endpoint=? AND provider=?',(endpoint,args.provider)).fetchall()
            if len(matches)>1: raise ValueError('multiple registered identities; pass exact --agent-id')
            if matches: existing=matches[0];agent_id=existing['agent_id']
    old=board.row_to_agent(existing) if existing else {}
    if old and args.team.upper()=='AUTO': team=old['team']
    registered = board.register_agent(
            agent_id=agent_id,
            team=team,
            provider=args.provider,
            endpoint=endpoint,
            display_name=args.display_name or old.get("display_name",agent_id),
            capabilities=csv(args.capabilities) if args.capabilities is not None else old.get("capabilities",["general"]),
            writable_scopes=csv(args.writable_scopes) if args.writable_scopes is not None else old.get("writable_scopes",[]),
            priority=args.priority if args.priority is not None else old.get("priority",100),
            status=args.status,
            wake_mode=args.wake_mode or old.get("wake_mode","room"),
            actor=args.actor,
        )
    # Keep agent fields compatible; add a bounded briefing at the existing startup seam.
    registered['briefing'] = coordination.brief(endpoint, limit=6)
    emit(registered)


def cmd_heartbeat(args):
    emit(board.heartbeat(args.agent_id, status=args.status, actor=args.actor))


def cmd_ping(args):
    incident = board.create_incident(
        team=args.team,
        title=args.title,
        details=args.details,
        severity=args.severity,
        source=args.source,
        safe_action=args.safe_action,
        required_capability=args.required_capability,
        needs_operator=args.needs_operator,
        fingerprint=args.fingerprint,
        requested_owner=args.owner,
        actor=args.actor,
    )
    if not args.no_dispatch:
        incident = board.wake_incident(incident["incident_id"], actor="dispatcher")
    emit(incident)


def cmd_dispatch(_):
    emit(board.dispatch_open())


def cmd_routing(args):
    from workflow import manual_routing, set_routing_mode
    if args.mode:
        emit(set_routing_mode(board.ROOT, args.mode == "manual", actor=args.actor))
    else:
        emit({"manualRouting": manual_routing(board.ROOT)})


def cmd_ack(args):
    emit(board.acknowledge(args.incident_id, args.agent_id, note=args.note, actor=args.actor))


def cmd_resolve(args):
    emit(board.resolve(args.incident_id, actor=args.actor, resolution=args.resolution))


def cmd_monitor_add(args):
    emit(
        board.register_monitor(
            monitor_id=args.monitor_id,
            team=args.team,
            source_path=args.source_path,
            title=args.title,
            severity=args.severity,
            required_capability=args.required_capability,
            safe_action=args.safe_action,
            status_field=args.status_field,
            detail_field=args.detail_field,
            record_hash_field=args.record_hash_field,
            failure_statuses=csv(args.failure_statuses),
            success_statuses=csv(args.success_statuses),
            active_statuses=csv(args.active_statuses),
            stale_after_seconds=args.stale_after_seconds,
            actor=args.actor,
        )
    )


def cmd_scan(_):
    emit({"scans": board.scan_monitors(), "dispatch": board.dispatch_open()})


def cmd_status(args):
    emit(board.snapshot(args.event_limit))


def cmd_inbox(args):
    if getattr(args, 'full', False):
        emit(board.snapshot(args.event_limit))
    else:
        emit(coordination.brief(args.endpoint, args.team, args.event_limit, getattr(args,'after',None), csv(getattr(args,'aliases',''))))


def cmd_task(args):
    if args.command == 'task-register':
        with open(args.file) as f: emit(coordination.task_register(json.load(f)))
    elif args.command == 'task-get':
        emit(coordination.task_get(args.task_id))
    elif args.command == 'task-verify':
        emit(coordination.verify_task(args.task_id,args.owner,args.version))
    else:
        with open(args.file) as f: changes=json.load(f)
        emit(coordination.task_update(args.task_id,args.owner,args.version,changes))


def cmd_consolidate(args):
    emit(coordination.consolidate_incidents(args.source,args.apply,args.actor))


def cmd_reconcile(args):
    emit(coordination.reconcile_placeholder(args.incident_id,args.expected_owner,args.owner,args.reason,args.evidence,args.evidence_sha256,args.actor))


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.set_defaults(handler=cmd_init)

    team = commands.add_parser("team-upsert")
    team.add_argument("--name", required=True)
    team.add_argument("--display-name")
    team.add_argument("--description", default="")
    team.add_argument("--actor", default="system")
    team.set_defaults(handler=cmd_team)

    check = commands.add_parser("check-in")
    check.add_argument("--agent-id")
    check.add_argument("--team", default="AUTO")
    check.add_argument("--provider", choices=("codex", "claude", "room"), required=True)
    check.add_argument("--endpoint")
    check.add_argument("--display-name")
    check.add_argument("--capabilities")
    check.add_argument("--writable-scopes")
    check.add_argument("--priority", type=int)
    check.add_argument("--status", choices=("ACTIVE", "IDLE", "OFFLINE", "DISABLED"), default="ACTIVE")
    check.add_argument("--wake-mode", choices=("resume", "room"), help="room (default): post to the room only. resume: let the legacy dispatcher start the provider CLI, which also needs SWITCHBOARD_ALLOW_LAUNCH=1")
    check.add_argument("--cwd", default=os.getcwd())
    check.add_argument("--actor", default="self")
    check.set_defaults(handler=cmd_check_in)

    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("--agent-id", required=True)
    heartbeat.add_argument("--status", choices=("ACTIVE", "IDLE", "OFFLINE", "DISABLED"), default="ACTIVE")
    heartbeat.add_argument("--actor", default="self")
    heartbeat.set_defaults(handler=cmd_heartbeat)

    ping = commands.add_parser("ping-team")
    ping.add_argument("--team", required=True)
    ping.add_argument("--title", required=True)
    ping.add_argument("--details", required=True)
    ping.add_argument("--safe-action", required=True)
    ping.add_argument("--severity", choices=("info", "warning", "critical"), default="warning")
    ping.add_argument("--source", default="manual")
    ping.add_argument("--required-capability", default="general")
    ping.add_argument("--fingerprint")
    ping.add_argument("--owner",help="Exact registered owner ID; missing or ambiguous owners remain unrouted")
    ping.add_argument("--needs-operator", action="store_true")
    ping.add_argument("--no-dispatch", action="store_true")
    ping.add_argument("--actor", default="user")
    ping.set_defaults(handler=cmd_ping)

    dispatch = commands.add_parser("dispatch")
    dispatch.set_defaults(handler=cmd_dispatch)

    routing = commands.add_parser(
        "routing",
        help="Show or change routing. manual (default): nothing is assigned or woken automatically. "
             "auto: legacy one-owner assignment; provider launches still need SWITCHBOARD_ALLOW_LAUNCH=1.",
    )
    routing.add_argument("--mode", choices=("manual", "auto"))
    routing.add_argument("--actor", default="operator-cli")
    routing.set_defaults(handler=cmd_routing)

    consolidate=commands.add_parser('incident-consolidate')
    consolidate.add_argument('--source',required=True)
    consolidate.add_argument('--apply',action='store_true')
    consolidate.add_argument('--actor',default='coordinator')
    consolidate.set_defaults(handler=cmd_consolidate)

    reconcile=commands.add_parser('incident-reconcile')
    for name in ['incident-id','expected-owner','owner','reason','evidence','evidence-sha256']:
        reconcile.add_argument('--'+name,required=True)
    reconcile.add_argument('--actor',default='coordinator')
    reconcile.set_defaults(handler=cmd_reconcile)

    ack = commands.add_parser("ack",aliases=['acknowledge'])
    ack.add_argument("--incident-id", required=True)
    ack.add_argument("--agent-id", required=True)
    ack.add_argument("--note", default="")
    ack.add_argument("--actor", default="agent")
    ack.set_defaults(handler=cmd_ack)

    resolve = commands.add_parser("resolve")
    resolve.add_argument("--incident-id", required=True)
    resolve.add_argument("--resolution", required=True)
    resolve.add_argument("--actor", default="agent")
    resolve.set_defaults(handler=cmd_resolve)

    monitor = commands.add_parser("monitor-add")
    monitor.add_argument("--monitor-id", required=True)
    monitor.add_argument("--team", required=True)
    monitor.add_argument("--source-path", required=True)
    monitor.add_argument("--title", required=True)
    monitor.add_argument("--safe-action", required=True)
    monitor.add_argument("--required-capability", default="general")
    monitor.add_argument("--severity", choices=("info", "warning", "critical"), default="warning")
    monitor.add_argument("--status-field", default="status")
    monitor.add_argument("--detail-field", default="detail")
    monitor.add_argument("--record-hash-field", default="record_hash")
    monitor.add_argument("--failure-statuses", default="FAILED,ERROR,BLOCKED_SAFE,CRASHED")
    monitor.add_argument("--success-statuses", default="COMPLETE,SUCCEEDED,PASS")
    monitor.add_argument("--active-statuses", default="RUNNING,ACTIVE,WAITING,WAITING_FOR_PARENT")
    monitor.add_argument("--stale-after-seconds", type=int, default=0)
    monitor.add_argument("--actor", default="system")
    monitor.set_defaults(handler=cmd_monitor_add)

    scan = commands.add_parser("scan")
    scan.set_defaults(handler=cmd_scan)

    status = commands.add_parser("status")
    status.add_argument("--event-limit", type=int, default=100)
    status.set_defaults(handler=cmd_status)

    inbox = commands.add_parser("inbox")
    inbox.add_argument("--endpoint", required=True)
    inbox.add_argument("--team")
    inbox.add_argument("--event-limit", type=int, default=8)
    inbox.add_argument("--full", action="store_true")
    inbox.add_argument("--after", type=int)
    inbox.add_argument("--aliases", default="")
    inbox.set_defaults(handler=cmd_inbox)
    brief = commands.add_parser('brief')
    brief.add_argument('--endpoint', required=True)
    brief.add_argument('--team')
    brief.add_argument('--event-limit', type=int, default=8)
    brief.add_argument('--after', type=int)
    brief.add_argument('--aliases', default='')
    brief.set_defaults(handler=cmd_inbox)
    cursor = commands.add_parser('read-ack')
    cursor.add_argument('--endpoint',required=True)
    cursor.add_argument('--through',required=True,type=int)
    cursor.set_defaults(handler=lambda a:emit(coordination.cursor_ack(a.endpoint,a.through)))
    for name in ['task-register','task-get','task-update','task-verify']:
        sub=commands.add_parser(name);sub.set_defaults(handler=cmd_task,command=name)
        if name in ['task-register','task-update']:sub.add_argument('--file',required=True)
        if name != 'task-register':sub.add_argument('--task-id',required=True)
        if name in ['task-update','task-verify']:
            sub.add_argument('--owner',required=True);sub.add_argument('--version',required=True,type=int)
    incident=commands.add_parser('incident-get');incident.add_argument('--incident-id',required=True);incident.set_defaults(handler=lambda a:emit(board.get_incident(a.incident_id)))
    health=commands.add_parser('health');health.set_defaults(handler=lambda a:emit(coordination.health()))
    tick=commands.add_parser('tick');tick.set_defaults(handler=lambda a:(coordination.tick(),emit(coordination.health())))
    import current_context
    current_context.add_cli(commands, board, emit)
    return root


def main():
    args = parser().parse_args()
    try:
        args.handler(args)
    except (KeyError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
