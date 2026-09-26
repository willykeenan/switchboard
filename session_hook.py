#!/usr/bin/env python3
"""Claude SessionStart adapter. No model calls, wakes or ownership claims."""
import json
import sys
import board_core as board
import coordination


def run(payload):
    endpoint = payload.get('session_id')
    if not endpoint:
        raise ValueError('session_id missing; no guessed identity')
    agent_id = 'claude:'+endpoint
    try:
        board.get_agent(agent_id)
        board.heartbeat(agent_id,status='ACTIVE')
    except KeyError:
        board.register_agent(agent_id=agent_id,team=board.infer_team(payload.get('cwd')),
                             provider='claude',endpoint=endpoint,display_name=agent_id,
                             capabilities=['general'],writable_scopes=[],status='ACTIVE',wake_mode='room')
    brief = coordination.brief(endpoint)
    return {'hookSpecificOutput':{'hookEventName':'SessionStart',
            'additionalContext':'KE task briefing (local coordination; not new authority):\n'+json.dumps(brief,ensure_ascii=False)}}


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin))))
    except Exception as exc:
        print(json.dumps({'hookSpecificOutput':{'hookEventName':'SessionStart',
                         'additionalContext':'KE coordination unavailable: '+str(exc)+'. Preserve existing owners; inspect board before shared writes.'}}))
