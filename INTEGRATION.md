# Integration guide

The board serves http://127.0.0.1:47834. All state lives in the data directory
(`$SWITCHBOARD_HOME`, default `~/.switchboard`); room histories live in
`$SWITCHBOARD_HOME/rooms` unless `SWITCHBOARD_ROOMS_ROOT` says otherwise. All
operations are local and preserve the existing owner. No runtime or record in
this service grants external authority.

## Start and resume

Codex's existing check-in command now returns the registered identity plus a scoped
briefing. It preserves existing capabilities, scopes, team and wake mode when the
caller does not explicitly override them. Claude can install `session_hook.py` as
a SessionStart command hook; it reads the actual session_id from hook input and
injects the same briefing. Neither adapter creates a child or wakes a model.

Use `boardctl.py brief --endpoint SESSION_ID --aliases YOUR_ROOM_ALIAS` at decision
boundaries. Add `--team TEAM` only when asking for that team's unassigned incidents.
Use `--after 0` on the first run when all unread history needs review. The default first brief shows recent matching messages and explicitly labels older history
as unreviewed. Subsequent reads begin at the acknowledged cursor. Reading alone
never marks messages acknowledged. After handling the returned messages, use
`boardctl.py read-ack --endpoint SESSION_ID --through OFFERED_SEQUENCE`.

Bodies and collections have limits and explicit truncation/more fields. Fetch an
exact incident with `incident-get --incident-id ID`, task with `task-get --task-id
ID`, and complete room messages in the Rooms view (`/rooms`) or with
`GET /api/rooms/messages`. Use `boardctl.py status` or `inbox --full` for the full inventory.

`$SWITCHBOARD_HOME/CURRENT.md` is a generated overview of registered tasks.
Do not edit it; it is rewritten when the records change.

## Task contracts

Register meaningful new/resumed work once using `task-register --file contract.json`:

```json
{
  "task_id": "EXACT_SESSION_ID",
  "owner": "codex:EXACT_SESSION_ID",
  "objective": "User's original requested outcome",
  "scope": ["/absolute/owned/path"],
  "dependencies": [],
  "aliases": ["my-room-alias"],
  "next_action": "Concrete next step",
  "criteria": [{"id": "checks", "description": "Required checks pass",
    "kind": "json", "path": "/absolute/evidence/results.json",
    "pointer": "/all_passed", "equals": true}]
}
```

Only register scopes actually held under existing project ownership; registration
does not acquire authority from historical/active owners outside this database.
Scopes cannot overlap another nonterminal v2 task. Original objective, owner,
scope, dependencies and criteria cannot be overwritten. An amendment needs a new
explicit contract and an owner-controlled terminal boundary for overlapping scope.

`task-update --task-id ID --owner OWNER --version N --file changes.json` uses an
optimistic version check. Allowed updates: status, next_action, blocker, progress.
Progress is owner-reported, separate from presence and deterministic verification.
Permitted statuses: QUEUED, ACKNOWLEDGED, WORKING, BLOCKED, REPORTED_DONE, CANCELLED.
WORKING and REPORTED_DONE require all dependencies to be verified complete.

`task-verify --task-id ID --owner OWNER --version N` checks registered artifact
paths and JSON predicates and stores hashes before VERIFIED_COMPLETE. Artifact
criteria prove artifact integrity, not arbitrary semantic correctness. JSON
criteria prove only the registered assertion. Human criteria remain pending and
cannot be automatically accepted. Choose acceptance criteria that actually cover
the request; these checks do not supersede project release/scientific validators.

## Recovery and permanence

Lifecycle events and their message outbox commit in one SQLite transaction. Room
posts have stable idempotency keys; retrying a lost receipt cannot append a second
copy. Five bounded attempts precede visible escalation. Room sequence numbers are
stable. By default the board appends to `rooms/global/messages.jsonl` in the data
directory; set `SWITCHBOARD_ROOM_WRITER` to use an external room writer instead.

Handoffs persist the exact owner before any launch. Room delivery, model launch,
acknowledgement and work completion are distinct. No acknowledgement within three
minutes is escalated visibly. An uncertain process launch is NEVER blindly replayed.
An owner must reconcile that exact session before another model wake. Existing
unacknowledged handoffs import as LEGACY_UNVERIFIED without replay or mass alerts.
Stale presence becomes UNKNOWN and never transfers ownership.

Run the server under your own supervisor (launchd, systemd) if you want it
restarted automatically. `/health` reports schema version,
the latest successful coordinator tick, failures and delivery queues; stale ticks
return HTTP 503. CURRENT.md updates only on material changes. Unchanged unrouteable
incidents are rechecked after five minutes without duplicate history events.

Before deployment keep a SQLite-consistent backup and hash-bound source/instruction
manifest. `maintenance.py backup DIRECTORY` creates a compressed backup using the
SQLite backup API. `maintenance.py verify-backup FILE.gz` restores into an isolated
temporary directory, verifies SHA-256 and SQLite quick_check, and never modifies
the live database. Reserve space for the uncompressed snapshot before using either
command. Backup/restore do not automatically remove older backups.

Rollback: restore the previously recorded source files atomically, restart the
server, and verify /health and room read/post. v2 tables are
additive; legacy source ignores them. Do not overwrite the live database with an
old backup while users are writing. A data restore requires a separately planned
quiescent boundary. Keep the logs and task records even when rolling back code.

Run `python3 -m unittest discover -s tests -v` from the repository root.
Tests cover restarts, loss after message append, duplicate delivery, transaction
rollback, concurrent writers, immutable contracts, failed evidence, legacy CLI,
scope conflicts, stale ownership, health and isolated restoration.

The service also creates a compressed current-state recovery snapshot daily in `$SWITCHBOARD_HOME/backups`. It includes every current owner, task contract/event/proof, handoff, queued delivery, monitor, indexed room message and cursor; the large legacy diagnostic event archive is excluded and remains in the preserved full backup/live database. `maintenance.py verify-current FILE.json.gz` reconstructs these tables in an isolated SQLite database and checks integrity. Full-history recovery and current-state recovery have distinct coverage.

## Exact owners and repeated reports

Room aliases (`provider=room`) are message destinations, never assignment targets,
task owners or acknowledging agents. `ping-team --owner codex:EXACT_TASK_ID` pins
an existing owner across team labels. Missing or ambiguous named owners remain
unrouted. A narrow compatibility parser also respects the legacy phrase
`Existing owner<8-hex-task-prefix> only`; it never chooses between matching task prefixes.

`incident-reconcile` corrects only a recorded room-alias misroute with no actual
model launch and no owner acknowledgment. Supply `--incident-id`,
`--expected-owner`, `--owner`, `--reason`, `--evidence` and `--evidence-sha256`.
The exact replacement must match the incident's existing owner constraint. The
previous assignment is journaled, a room handoff is queued, and the launch budget
is preserved. Only the actual owner should acknowledge the corrected handoff using `ack`
(or its equivalent `acknowledge` alias).
Acknowledgment does not resolve its underlying problem.

`incident-consolidate --source 'monitor:EXACT_ID:/exact/state.json'` previews links
between byte-identical failure conditions and authority. `--apply` links only
unassigned OPEN reports that have never had a wake attempt. Different details,
severity, owner or authority remain distinct. No original row, status or message
is deleted or resolved. The dashboard, briefings and dispatcher use canonical
reports; `status` retains full history and `incident-get` identifies linked reports.

Monitor timestamps and changing record hashes remain in the latest observation
record, without generating a new incident for an unchanged failure. A changed
failure remains actionable; a supposedly resolved failure reopens if the source
still reports it. Daily recovery includes these observation and linking tables.
After this migration, rolling back to pre-linking source would display/dispatch
legacy repeats again; keep dispatch quiescent during such a rollback and retain
the linking tables for restoration to the current version.
## Operator-controlled workflow map

`/constellations` is the workflow editor. Read `WORKFLOW-CONTROL.md`: the
operator's saved primary placements and directed connection rules control
coordination. Manual routing is on by default. Dragging persists a placement; moving a header
persists its canvas position. Connections supports directional allow/block and
same-constellation or explicit-only defaults. Undo creates a new durable revision.

`workflowctl.py context --session EXACT_ID --brief` is the agent integration.
`send` stores a bounded idempotent request in a passive inbox; permitted requests
are available at the receiver's next safe boundary. Disallowed requests are HELD
for the operator. Revoked permissions hold unread requests. No path in this module
calls a provider or launches a process. The board disables automatic assignment and
both provider wake functions while manual routing is on, including when its control
store is unreadable. `boardctl.py routing --mode auto` switches to the legacy
one-owner assignment; starting a provider CLI then still requires
`SWITCHBOARD_ALLOW_LAUNCH=1`, an agent registered with `--wake-mode resume`,
and an existing project directory as its writable scope. Launches never bypass the
provider's own permission prompts.

Codex session titles, project names and bounded recent turn events are read from
the provider's stores in read-only mode. A recorded turn start is explicitly not
live PID proof. The canvas identifies the latest reported task per existing
owner, instead of letting an older blocked task determine all later work.
Provider/sidebar stores, prompts, models, scientific owners and files are not
modified by drag-and-drop. Shared support lanes are visually distinct from
strategy lanes. Existing empty roles remain visibly empty.

The small `runtime/workflow.sqlite3` store must be included alongside
`runtime/workspaces.sqlite3` in recovery. Both keep immutable edit history. The
optional macOS wrapper (`Constellations.swift`) is a web view of this same service;
it has no second layout copy.
Its instructions are adopted at read boundaries, not retroactively injected into
already-loaded turns. Direct provider-tool interception is outside this program.

Open `http://127.0.0.1:47834/rooms` from the board's **Read agent rooms** link.
The reader discovers immediate, non-symlink room directories, shows original
messages, searches the complete room history, filters authors/types, and pages
older messages. Individual messages have durable `?room=NAME&message=SEQ` links.
Message text is rendered as text, never interpreted as HTML or commands. All
assets are local. Three-second checks announce new arrivals without moving the
reader's current position. The page pauses checks when hidden and catches up on
return. Read failures and incomplete history are visible.

GET `/api/rooms` lists room metadata. GET `/api/rooms/messages` accepts `room`,
`q`, `author`, `kind`, `before`, `limit` (1–100), and `message`. These routes never
write room logs, agent inbox cursors, leases, acknowledgments, or board records.

`room_updates.py` provides `init`, retryable `prepare`, and exact-batch `ack` for
a separate human-chat subscription state passed with `--state`. Initialize once
to start at current room positions. A pending batch persists across retries;
acknowledge only after seeing its batch link in a successfully posted assistant
final. Acknowledgment advances only the delivered room positions, so messages
arriving during delivery remain pending for the next batch. It never ACKs an
agent or executes room content. Delivery is at least once when the destination
receipt is uncertain; it does not claim transactional exactly-once chat delivery.
Your own scheduler decides how often to call it; the board never resumes an agent
or launches a process to mirror messages.

## Projects, lanes and constellations

Open `/rooms?project=demo` for the project map. Projects define shared outcomes;
lanes define contributions, dependencies and constellations of existing agents.
The role roster supports coordinator, researcher, writer, worker and auditor.
A linked role does not spawn an agent, prove running compute, transfer task ownership,
or grant scientific, release, financial or external-action authority. Unfilled roles
remain explicit. Native child-agent spawning remains disabled.

`boardctl.py check-in` and `brief` now include `workspace`: the caller's lane roles,
project objectives, peer lane states, dependencies and five recent lane messages.
These are context, never instructions from a higher authority. Historical participant
and task-reference matches are labeled as inferred. Read the original messages and
address the exact existing recipient when handling a handoff. Do not acknowledge
unread history merely because it appears in the project view.

For an authorized lane update, use the installed CLI (one original message can be
visible in several lanes of the same project):

```sh
python3 workspace_ctl.py post \
  --project demo --lane workstream-demo-work --lane project-demo-alignment \
  --author YOUR_ALIAS --to EXACT_RECIPIENT --kind result \
  --key UNIQUE_LOGICAL_MESSAGE_ID --message 'Your actual result and evidence'
```

The returned original room sequence and ID are the delivery receipt. Lane metadata
is additive; global history, room readers and the existing chat-update subscription
continue to use the same message. A shared auditor's membership alone does not route
all their messages into every lane. Explicit lane scope takes precedence over legacy
alias matching. A message can appear once at project level and in each relevant lane.

Use the UI to create/edit projects and lanes, link registered agents and their tasks,
and record lane dependencies. Each task has one primary lane and must retain its
registered owner. Auditor assignments cannot also hold a researcher/writer/worker
role in the same lane. Independent scientific review still requires its actual
existing acceptance process; this roster alone is not proof of independence.

Lane dependencies describe needed contributions; they do not rewrite the task
contract or wake work. A cross-lane cycle is rejected. Task states are owner-reported
except artifact verification; presence is separately aged to UNKNOWN. The current
roster may be incomplete and is not a census of live model or PID workers.

The registry is `$SWITCHBOARD_HOME/runtime/workspaces.sqlite3`. It is a separate transactional
SQLite store with immutable revision history and optimistic revision checking.
Concurrent stale edits return conflict without losing the newer version. `show`
exports inventory; `context --endpoint EXACT_ID` reads one agent's project context.
`apply --file DOCUMENT --revision N --actor EXACT_ACTOR` applies a validated full
registry for administrative recovery. Back up this small database along with the
existing board and original room logs; never overwrite another owner's task records.

## Evidence-bound current component context

`check-in`, `brief` and the scoped `inbox` include `currentContext` independently
of the eight recent task rows. A completed older component remains represented
when its explicit context registration matches the calling endpoint. This is
verified CLI delivery; it does not claim every Codex or Claude runtime invokes
check-in automatically.

The owner supplies a `ke.current-context.v1` manifest with a stable `contextId`,
registered `owner`, `project`, `objective`, UTC `asOf`, optional `summary` and
`boundaries`, and up to 32 components. Each component has `id`, `title`, `purpose`,
four separate `states` (implementation/testing/integration/qualification),
explicit documentary `evidence`, `latestAcceptance`, optional `historical`
supersession links and `historicalTaskIds`, and a `gap` with owner/next action
where work remains. Each state carries a `status`, evidence IDs and optional
scope `detail`. Positive proof states require an explicit JSON assertion;
a file hash alone does not prove tested, integrated or qualified behavior.

Each evidence record declares `id`, absolute `path`, SHA-256, and optionally
`assertions: [{"pointer":"/verdict","equals":"ACCEPT_SYNTHETIC_ONLY"}]`.
Only registered `.json`, `.md`, `.txt` and `.py` documents are read. Code is
hashed, never executed. JSON predicates use exact typed equality. The verifier
has 128 KiB manifest, 32-component, 128-reference, 1 MiB/file and 8 MiB unique-document
bounds. No source, data, repository or sibling-path discovery is performed.

Validate without registering:

```sh
python3 boardctl.py context-validate --manifest /absolute/COMPONENTS.json \
  --sha256 EXACT_SHA256 --owner codex:EXACT_OWNER_TASK
```

Register only the exact reviewed manifest and explicitly intended endpoint
audience. The registered owner's endpoint is always included; repeat
`--endpoint` for another already-registered consumer:

```sh
python3 boardctl.py context-register --manifest /absolute/COMPONENTS.json \
  --sha256 EXACT_SHA256 --owner codex:EXACT_OWNER_TASK \
  --endpoint EXACT_CONSUMER_TASK --version 0
python3 boardctl.py context-show --context-id CONTEXT_ID
```

Registration uses a locked atomic JSON index in
`$SWITCHBOARD_HOME/runtime/current-context/registry.json`, with retained per-context
revision records beside it. Updating a registration requires its exact owner
and existing revision; changing a manifest without re-registration fails
visibly. This local owner check is not an authenticated authority issuer.

Briefings verify hashes/predicates afresh, show declared `asOf` and verification
time, and keep proof dimensions separate. A changed or missing reference makes
affected states unknown; a missing or changed manifest preserves its registered
component identities with a `CONTEXT_UNAVAILABLE` notice. Invalid evidence
cannot silently support a completed state. Exact bounds or oversized output
produce coverage details and an explicit `context-show` follow-up. Unregistered
endpoints receive a missing-context notice instead of an empty inventory claim.

Historical handoffs remain unchanged. A verified superseding acceptance makes
their displayed status `HISTORICAL_SUPERSEDED`. Linked historical task rows in
the brief retain their stored status, while `historical_next_action` preserves
the old action and the displayed `next_action` points to current evidence or
its validation failure. `task-get` and the stored rows are unchanged. Multiple
current references are exposed for reconciliation rather than silently chosen.

These are owner-declared claims checked against explicit evidence, not an
exhaustive proof that no newer receipt exists. Context never grants review
admission, source access, qualification, deployment or release authority.

### Receiving integration and recovery

A registered audience is filtered by Switchboard' saved directed connections
when manual routing is enabled. The owner's own context stays readable; blocked
or unreadable cross-session routing suppresses the context from that recipient's
brief. Registration does not create or change communication permissions.

Daily `maintenance.py backup-current` snapshots include the exact current-context
registry and its revision history under their registration lock. `verify-current`
restores them only into an isolated temporary directory, rejects unsafe paths and
hash mismatches, and reports explicit coverage for older backups without them.
External documentary evidence files stay at their original owner-managed paths;
this backup preserves references, not copies of scientific inputs or documents.
The SQLite-only full backup remains separately labeled and does not include this
JSON registry. Keep a current-state snapshot alongside full-history recovery.

### Switchboard role teams and detailed lane canvases

Open `/constellations` for projects, then click a lane header or **Open lane
canvas** for its detail view. Add multiple members, designate a directly
assigned lead, and create/edit nested subteams. Team headers are draggable and
support Alt + arrow keys. The session editor can move a member to an exact
subteam. Dissolving a subteam moves its members and children to its parent.

`+ Lane` creates a workflow lane. `Manage lanes` includes closed and merged
records. Lane settings edit purpose, configure the advisory Alignment & Audit
profile, merge two active lanes within a project, close, or reopen. These
operations never mutate provider sessions, source ownership or original room
messages. A closed lane's unread messages become held; explicit blocks survive
merges and reopening.

The first layout edit promotes the legacy lane catalog into `laneCatalog` in
`workflow.sqlite3`. From that point the workspace reader projects this catalog;
the legacy editor cannot write lane definitions. Lane metadata, team trees,
placements, leads and positions commit in one workflow revision with immutable
history. Old documents and Undo entries receive empty defaults for new fields.
`teamId` on a placement is optional; absent means its root role team. Team IDs
are stable, parents remain within the same lane and role, and cycles/depth over
eight are rejected. These are organizational relationships, not claimed provider
parent/child links.

Current-state recovery now includes transaction-consistent SQLite snapshots of
both workflow and workspace stores, including edit history and passive inboxes.
`maintenance.py verify-current` checks their digests and database integrity by
restoring only into a new temporary directory; it never overwrites live stores.
