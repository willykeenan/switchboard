# Task Board dispatch

The board captures requests immediately and retains every task by its own ID.
Workstream Owners define scope, dependencies and integration, and read the event
updates in their normal workflow context. Routine delivery to a permitted worker
does not wait for an owner acknowledgment.

## Enable a lane deliberately

Open **Task board → Dispatch**. Save the lane's allowed source paths,
capabilities, worker limit and runtime limit, then enroll each exact existing
worker. Enrollment is separate from placement. A moved card does not grant
execution authority. Pausing enrollment prevents new pickup and preserves active
ownership. An ambiguous request can remain captured until **Define ready work**
records a scoped outcome and dependencies.

Imported sessions use cooperative pickup at their own safe boundary. Managed
automatic starts require both the lane switch and explicit enrollment of an
existing Switchboard-managed session. The adapter never creates a thread, resumes
an imported session, changes its model, or replays an uncertain start. No model
work runs merely to keep five software couriers on standby.

## Worker pickup

Use the installed `taskflowctl.py` from the current provider environment; it
resolves the exact identity from CODEX_THREAD_ID or CLAUDE_SESSION_ID. There is no
alternate-actor argument and no agent-facing policy approval command.

1. Read `taskflowctl.py context` at a safe boundary. Preserve your current live
   assignment. Only when ready for an enrolled task, call `taskflowctl.py idle`.
2. Read the returned assignment and save its **payload object** as a JSON file.
   Read its request, amendments, scoped outcome, dependencies and pinned inputs.
3. Call `taskflowctl.py accept ASSIGNMENT_ID --payload-file /absolute/payload.json`.
   The board checks the exact payload digest and current source hashes again.
4. Read `context` before decisions. It includes the frozen accepted payload,
   current task, instruction-version changes and current dispatch policy. Stop
   at a safe boundary when instructions or authority conflict. Do not silently
   apply amended work to an old task version.
5. Report `progress --file /absolute/progress.json` containing `assignmentId`
   and `message`. Return `return --file /absolute/result.json` containing
   `assignmentId`, `summary` and absolute nonempty `artifacts` paths inside the
   assignment's output directory or authorized source scope.

A cooperative acknowledgment is **Accepted**, not an observed provider start.
An existing Codex worker can then use `start --file` with its `assignmentId`,
`threadId`, `turnId` and exact `payloadHash`. The installed reader must observe
that registered worker's exact current turn, open and fresh, in the provider
event log. This binds the worker's existing authorized turn; it issues no start,
resume or wake. Missing IDs, stale events and general session presence fail closed.

A worker that bound its current turn returns artifacts with `sourceReleased: true`
at its own safe boundary. That receipt explicitly releases the task's source;
it does **not** claim the whole provider turn ended. The exact bound turn must
still be observed, and independent result review remains mandatory. Another turn
cannot release that reservation by asserting the old turn finished. Managed
supervisors retain their separate verified provider-terminal requirement.

Managed runs record actual thread/turn IDs, keep a bounded supervisor and retain
the task's received payload manifest. Public worker messages belong to that
assignment; unrelated session history is not displayed as the task conversation.

## Return and review

Returned work enters **Needs review**, retaining worker identity and content
hashes. An independent project auditor first uses `audit-claim --file` with `taskId`
(or an empty object to claim the oldest eligible task), reads `audit-library`,
and pins context with `audit-context` (`taskId`, `libraryItemIds`, `assessment`).
Then `review --file` takes `taskId`,
`version`, `verdict` (`accept` or `revise`), written `summary` and evidence paths.
the operator can review through the task inspector. Review checks the retained hashes;
the reviewer must assess the requested outcome, installation and limitations.
Mechanical file checks alone do not establish that a product is installed.

Workers lead their own tasks and may define a bounded next step within the
approved paths and capabilities. The original required outcome remains in the
payload. A rejected audit returns corrections to the same task automatically.
Workers cannot impersonate an owner or broaden scope. Each handoff retains prior result manifests, original worker accountability
and the current task ID. Saved blocked peer connections remain blocking.

## Visual meaning

Rows, stationed icons, moving icons and the right inspector address the same
task ID. New durable transfer events animate through measured desks, doorways
and corridors. A 6.2s path replay may display a recorded transfer; it is not
the visual model of work, and ending it does not roost a bird or start or
finish work. Reconnection restores current placement without replaying history.
Reduced motion uses stationed icons. Unroutable visual travel does not invent a
shortcut through a wall or change the backend task status.

The task icon's visible body scales with the canvas; its transparent pointer
target stays at least 44 pixels. When these targets overlap at distant zoom,
pointer down selects the nearest visible task center and retains that identity
until pointer up. Keyboard activation selects the focused task directly.

## Courier lifetime

Finch, Wren, Lark, Kite and Swift live in the birdhouse (the shared roost above
the canvas). An offered transfer occupies one bird until drop-off; the bird then
flies back to its perch. The document stays in the worker's inventory, the
review queue or the board. Later pickups leave the house the same way. Each
bird retains assignment history across reuse and restart. A replay never starts
or completes model work. Standby means the bird is home and does not run a
model. Only an in-flight (OFFERED) bird is occupied.

## Restart, pause and rollback

TaskFlow tables are additive in `board.sqlite3`; original task contracts and
events are not rewritten. Legacy task-to-lane links and explicit lane aliases
take precedence over owner placement. Saved intake records retain their original
request text, canonical task references and acceptance criteria.

An unreceived offer expires safely. Accepted or potentially running work retains
its reservation on timeout. An uncertain start is never automatically replayed.
Do not delete assignments to clear a queue. Inspect the exact task, supervisor
PID and provider turn first. **Cancel task** waits for a running worker's terminal
observation before releasing execution ownership.

Before rolling back application code, pause dispatch in every enabled lane and
wait for active tasks to return or reach an explicitly reconciled terminal state.
Retain a SQLite-consistent backup including all taskflow tables. Old code can
ignore these additive tables, but must not be used to resume their work.
The existing snapshot command includes the task board database once TaskFlow is
initialized. Each database retains its original absolute board root; restoring
it to a different directory disarms TaskFlow there, preserving history and active
claims without reading or restarting the original provider sessions.

## Candidate verification

Run `python3 -m unittest discover -s tests -v` from the repository root. The
TaskFlow modules are covered by `tests/test_taskflow*.py`. Provider-protocol tests
use explicit simulations; they are not authenticated live worker-start or
installed-app evidence.

# Existing intake queue

Typed `recordKind: task-intake` records are adopted into this queue under their
existing task IDs. Original board rows and history remain intact. Only an explicit
attendant inventory relationship is removed from execution dependencies; real
dependencies remain. Canonical implementation links remain linked to their existing
work and cannot be redispatched as duplicates. Related documents remain context.

An enabled automatic-readiness policy covers both new requests and previously
captured intake. Without one, the task is captured with a specific readiness need.
Source revisions and pinned attachments reach the worker in the task payload;
changed instructions invalidate an unaccepted offer. No migration starts a provider.

## Courier artwork and receipt inspection

The shared roost stays visible above the canvas in ordinary project view. Its
five stable names (Finch, Wren, Lark, Kite and Swift) are views of the existing
software courier slots, not additional agents. Each opens its own persisted
assignment delivery history and links to the original task. History is scoped
to the selected project and excludes private runtime secrets. The moving task
parcel still opens the exact task. A returned result is not a completion verdict.

The20px bird copies the exact silhouette and mesh edges from the approved
company-whitepaper/build_whitepaper.py bird() vector, SHA256
0ae89ba4a0cd2e0aac147737b5051da622b2d5b85195eb014b6843642e9f673f,
with only the SVG y-axis reversed and white stroke applied. The former artwork
was24px. The shared roost uses44px minimum unscaled button height. After
drop-off the bird flies back to its perch without the parcel. Replay of a
recorded handoff is not task execution.

## Current-state backup and recovery

Daily recovery exports every taskflow_* table, including audit claims, questions,
complete TaskFlow history, indexes and sequence high-water marks, from one pinned
read-only transaction into a small side database. It never backs up or reads the
board's unrelated legacy events archive. The side database is limited to32MiB
and five seconds; exceeding either budget fails explicitly without dropping rows
or replacing the previous backup. A failed daily attempt records its error and
waits one hour before retry, so the normal service loop can keep progressing.
The full historical archive remains with the existing full-backup path.

Restore remains restricted to a new directory. The original taskflow_instance
root is retained, so active/uncertain assignments and audits survive while a
restored copy stays disarmed. Individual stores are consistent snapshots; no
cross-store atomicity is claimed. These bounds cover the new TaskFlow component,
not a redesign of pre-existing Library or coordination retention.


## Shared audit queue and recovery

Returned work stays in Needs review on its original task. Review offers are stored
in this project queue, so a missing individual inbox connection never moves the
task to a terminal hold. Only registered, independent Codex auditors in permitted
project seats can receive offers. An existing reviewer uses `audit-idle --file`
with `{}` at its safe boundary to offer a five-minute availability window;
`audit-pause --file` stops unaccepted offers. Neither operation wakes a provider.
The service scheduler makes atomic, oldest-first offers, one per task and reviewer.
`context` exposes `audit.offers`; `audit-claim` accepts an exact offered task.
Recorded context assessment starts the Reviewing phase; assignment alone does not
claim that model work has started. A review verdict frees capacity for the next
queued review during the availability window.

An unaccepted offer expires after sixty seconds or when its permission is revoked;
the scheduler then tries another available authorized reviewer. If none exists,
the original task remains Waiting for auditor. The Owner sees a recorded exception,
not a routine approval request. Accepted/stale reviews retain ownership until the
reviewer or operator releases them; a stale heartbeat never authorizes duplicate
review work. Explicit blocks, project isolation and independent source custody
remain in force. Restore copies remain disarmed.

A failed or revised audit returns findings and pinned evidence to the same task,
queued for its responsible worker. Busy workers keep their current work. An
operator or the task owner can select a qualified enrolled correction successor.
Only acceptance of the required result completes a task; an artifact review does
not establish installation or full workflow acceptance.

`recover --file` accepts an exact `assignmentId` and `reason` under the current
worker identity. It releases uncertain assignments only when durable records prove
no start was issued or the exact registered provider turn is observed terminal.
Cooperative terminal observations come from the registered event-log reader;
managed observations use the existing transport. A missing managed start response can be reconciled by the unique provider
userMessage.clientId matching the assignment ID. Recovery acquires the existing
supervisor lock and requires that correlated turn to be terminal. Missing or
ambiguous correlation stays uncertain; there is no force release or automatic replay. `fail --file` records inability to complete while
preserving the task. `cancelled` requires an existing cancellation request.

Service duties each run at most one call at a time. An exception or hang in taskflow
or backup cannot skip monitoring or mirroring; health reports each duty's freshness,
failure and overdue state. New projects adopt the shared audit routes. Editing an
existing project does not silently adopt routes; explicit company-workflow adoption
remains available, and the permission view shows the same effective rules as delivery.


## Independent review correction loop

A REVISE or FAIL verdict returns the same task to its lane Task Board. It does
not send work directly to a worker. The atomic review record pins the reviewed
assignment, artifact digest, instruction version, findings and evidence. The
next bird payload carries the complete prior result and review history. Changed
pinned evidence holds delivery; replaying a verdict cannot create another task.

Corrections retain their responsible worker. While that worker is busy, stale,
disabled or uncertain, the task stays queued at the board and no peer is woken
or interrupted. The worker's existing safe-boundary availability allows the
ordinary scoped dispatcher to offer one new bird trip. If the worker is no
longer available, the operator or the task owner can explicitly choose another
enrolled worker in Define ready work; source, capability and connection checks
still apply. Returned corrections go through independent review again.

The worker `fail --file` action retains an unfinished task and reason on the
board. `cancelled` acknowledges a previously recorded cancellation request; it
cannot independently cancel the user's task.
