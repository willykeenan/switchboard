# Individually addressed agent handoffs

Handoffs are exact-recipient messages sent along saved allowed connections. The
handoff daemon can deliver an action handoff to an idle recipient once the operator
enables it and a provider transport is configured. It never interrupts busy agents,
creates replacement tasks, changes models or grants installation rights. No
provider transport ships in this repository, so out of the box handoffs stay
visible in the recipient's passive inbox.

Use the existing `workflowctl.py send --from EXACT --to EXACT --message TEXT
--key STABLE` for a concrete next action. The dispatcher prepends the recipient's
real name and exact provider identity, preserving the original sender, body and
message ID. Reusing an ID with different content or intent fails.

Use `send --notification` for information that requires no action; use `read-ack`
for acknowledgment. Do not send acknowledgment-only action handoffs back and
forth. A handoff is not a task-completion receipt.

Inspect `workflowctl.py handoffs --session EXACT` or
http://127.0.0.1:47836/ for actual delivery. WAITING, BUSY, HELD, UNAVAILABLE,
MANAGED_POLICY, SENDING, UNCERTAIN, ACCEPTED, RUNNING and RETURNED are distinct.
RETURNED means the exact provider turn ended; the original task still needs its
own review, installation and completion evidence. A stopped or failed turn is
FAILED. An uncertain write is reconciled from the exact provider transcript and
is never resent automatically.

`python3 workflow_handoffd.py` watches the same data directory as the board
(`$SWITCHBOARD_HOME`, or `--root DIR`) and checks every five seconds. One process lock and
an atomic per-recipient reservation prevent overlapping delivery. Busy recipients
do not block other eligible recipients. A missing desktop owner, unavailable
Claude session, unknown activity or missing managed runtime assignment is
surfaced for repair, not silently bypassed. Desktop resume of a host-pinned
Codex binary is not bundled; inject a transport in tests or a local plugin.

Only messages created after enablement are enrolled automatically. Historical
messages must be explicitly selected after checking current scope; never mass
wake an old inbox. The original message and recipient remain immutable.

Recovery: inspect the health endpoint and `$SWITCHBOARD_HOME/runtime/workflow-handoffs.sqlite3`.
Pause future starts by setting enabled=false in the product-owned
runtime/workflow-handoffs-policy.json, preserving its cutoff and authority.
Restart the handoff service only; never restart provider tasks or delete
SENDING/UNCERTAIN records. A restart reconciles outstanding writes. No automatic
retry may clear an uncertain send. Keep the monitor available while paused.
