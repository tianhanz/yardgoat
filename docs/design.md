# Design (P0 + P1 + pools)

Yardgoat puts one dispatch contract in front of the agent fleets you already run. P0 is the smallest
piece that makes the contract real: a file-backed task store, a CLI, and a direct runner.

## Objects

| Object | Where it lives | Mutable? |
|---|---|---|
| **Task spec** (`schemas/task.schema.json`) | `tasks/<id>/spec.json` | no — accepted once |
| **Task state** | `tasks/<id>/task.json` | yes, only under the task lock |
| **Run** | an entry in `task.json` + `tasks/<id>/runs/<run_id>/` | appended, never rewritten |
| **Receipt** (`schemas/receipt.schema.json`) | `runs/<run_id>/receipt.json` | written by the runner; verifier result added by `close` |
| **Event** (`schemas/event.schema.json`) | `tasks/<id>/events.jsonl` | append-only |

The store root is `$YARDGOAT_HOME` (default `~/.local/share/yardgoat`). Machine-local wiring — which
wrapper picks a model lane and account, which env files to source, which tmux session hosts runs —
lives in `$YARDGOAT_CONFIG` (default `~/.config/yardgoat/config.json`), never in a task spec.

## Invariants (each one has a contract test)

1. **Immutable task ids.** Submitting identical content again is a no-op; different content under the same id is refused.
2. **One owner.** Only the current owner can dispatch or close. Ownership moves only through `takeover --from <current owner>`, which records whether a run was in flight.
3. **No duplicate dispatch.** While a run is alive, `dispatch` returns the existing run. A run that exited must be verified before another attempt.
4. **Frozen verification.** The card and every file listed in `verifier.freeze` are hashed at submit; any drift blocks both dispatch and close.
5. **Exited ≠ verified.** A normal worker exit only writes a receipt with `status: exited`. `close` runs the verifier: exit 0 → `passed`, exit 1 → `failed` (retry allowed within `budget.max_attempts`), anything else → `verifier_error` (a crashing verifier is not a failing deliverable).
6. **Bounded attempts.** When the budget is spent the task becomes `exhausted` and needs a human.

7. **Lane policy at the dispatch edge.** If a spec lists `allowed_lanes`, the executor lane must be in it; lanes in the local config's `forbidden_lanes` are refused on that machine regardless of the spec.
8. **Asking is not failing.** A worker that writes `out/BLOCKED.md` (or a pool card that lands in `parked/`) parks the task with that text as the question. `answer` records a reply; `reconcile` resumes the task on the owner's behalf, and the answer is appended to the prompt. Parked runs do not use the attempt budget.
9. **Infrastructure failures are not attempts.** When the local launcher exits with one of `launcher_failure_codes` before any agent started, the run is `launch_failed` and does not count.
10. **The worker sees the caller's environment.** Yardgoat's own import path is never passed to workers.
11. **Queue first, then start.** Every eligible dispatch is accepted as a queued run (unverified dependencies wait
    under invariant 20). Direct runs start immediately only while
    fewer than `max_concurrent` direct runs are alive (local config); otherwise they wait and `reconcile` starts them
    oldest first, re-checking drift and lane policy at launch. A queued run counts as in flight (no duplicates) but
    not as an attempt; `cancel` removes it. Adapter runs are capped by the fleet they go to.

12. **Pools: queue first, stations pull.** (Stations never write the task store: their `claim`/`finish` go through a
    mailbox inside the pool root and are executed by the station wrapper outside any sandbox; the wrapper also cuts a
    card off at its `max_wall_min`, ending the station's whole process group.) A task with `adapter.kind = "pool"` is only queued at dispatch. Stations
    (one agent CLI session each, started by Yardgoat within `max_stations` per pool and `max_concurrent` per machine)
    run a loop prompt: `claim` the oldest card → do it → `finish` → claim again (a pool adds one station per `spawn_per_queued` waiting
    cards, default 1, so small batches stay on one warm station), until `EMPTY` or
    `max_tasks_per_station`. Warm sessions are reused across cards; nothing is ever typed into a station. A card whose
    station dies is marked `lost`, unless its log identifies a provider refusal (invariant 19). Task workdirs must live under the pool's `root` (the station's writable root).
13. **The accepted spec and frozen hashes have an authoritative copy** outside the task store
    (`$YARDGOAT_HOME/frozen/`); verification reads the spec from there, and any disagreement with `spec.json` or the
    hashes in `task.json` is treated as drift.
14. **`auto_close`** in a spec lets `reconcile` run the frozen verifier on the owner's behalf once a run exits.
15. **Concurrency.** Every "check capacity, then start" decision (dispatch, reconcile, station scheduling) holds one
    machine-wide schedule lock; lock order is schedule → pool → task. Outcomes are committed exactly once, receipt and
    state together under the task lock; a late or duplicate outcome is ignored, and a receipt whose state never got
    written is replayed by `reconcile`. Archive takes delivery → schedule → task locks, so it cannot revoke a verified
    dependency between the final dependency check and start/claim. Other mutation paths either refuse verified
    tasks or preserve their status; verifier failure/exhaustion starts from an unverified task and does not revoke
    eligibility. The potentially slow verifier remains outside the schedule lock.
16. **Process groups.** A direct worker runs in its own process group; a timeout ends the whole group, and liveness is
    judged by the group, so a worker that outlives its runner still blocks a retry.
17. **Unknown submits are not retried blindly.** If an adapter's submit fails, Yardgoat asks the adapter whether the
    work landed (`probe`); if it cannot tell, the run is `submit_unknown`, blocks further dispatch and shows in the inbox.
18. **Environment.** Workers get the tmux server's environment plus the local config's `source` files and launcher —
    deliberately not the dispatcher's environment; `pass_env` is an explicit allowlist.
19. **Provider refusals are not attempts.** A direct run whose final log matches a configured refusal pattern, or an
    unfinished pool card whose exiting station logs such a refusal, becomes `provider_refused`. The receipt and
    `run.provider_refused` event preserve the matching line. This state never spends the attempt budget and appears
    in the inbox with a request to reword or switch lanes before resending. Reconcile does not automatically retry it.
    Lost-wrapper recovery and receipt replay preserve the same classification.

20. **Dependencies require verification and declared delivery.** `depends_on` lists accepted upstream task ids. Submission validates
    self-references, missing ids and cycles by traversing authoritative accepted specs, under the schedule lock.
    Dispatch uses the same dependency gate as queue release and claim: terminated upstreams immediately park the
    task; otherwise it creates one `waiting_deps` run if any upstream is not `verified` or has unfinished `on_pass` steps. Waiting counts only as in flight:
    it prevents duplicate dispatch and survives takeover, but uses no attempt, direct slot or pool station.
    `status` names the upstreams and distinguishes verification from unfinished delivery; `cancel` can cancel the wait without spending an attempt.
    Reconcile moves the same run to `queued` once all upstreams are verified and all their declared delivery steps succeeded, then applies ordinary capacity,
    lane, drift and pool rules. Worker exit or a retryable verifier failure is insufficient. An `archived` or
    `exhausted` upstream instead parks the task with a `blocked_deps` run and an inbox question naming the
    terminated upstream. No worker or receipt is created for that run. Answering does not waive the dependency;
    an impossible dependency requires a replacement task spec under a new id. Auto-verification stays outside
    the schedule lock; downstream tasks observe its result on the next reconcile tick.
21. **Declared writes exclude overlapping work.** Before direct start, file-pool/plugin submit, and pool claim,
    the scheduler compares `workdir + scope` against every running task and active delivery write set, across all adapters and
    pools. The check, reservation and start share the machine schedule lock; claim follows schedule → pool →
    task lock order. Conflicts keep the same run queued, spend no attempt, and name the conflicting task and
    absolute path in `status` and reconcile output. Every worker outcome releases its reservation; reconcile
    then starts eligible queued work (or makes pool cards available for claim). An uncertain remote submit
    retains its reservation because it may already be executing. Scope waits alone do not spawn stations.
22. **Approval is separate from answers.** A frozen `risk: l3` card needs `approve` before dispatch, queued
    start and pool claim. The optional local `approvers` list restricts accepted identities. Approval stores
    `by`, `at`, `note` and `content_hash`; each starting run snapshots it into its eventual receipt.
    `answer` never grants approval, and `approve` never edits the question or its state. Frozen-content
    drift blocks execution; mutation checks mark an existing approval invalid permanently, even if the
    bytes are later restored. `status` and `inbox` remain read-only and report current drift/approval needs.
23. **Delivery follows PASS.** Only the current owner can run frozen `on_pass` steps. A step failure keeps
    the verification verdict PASS and task status verified, sets `delivery_failed`, stops later steps,
    and appears in inbox. `deliver` retries the first unsuccessful step; successful recorded steps are
    skipped. Auto-close follows exactly the same verification and delivery path.

## Launch-chain health checks

`yardgoat doctor --pool P` or `yardgoat doctor --host H --lane L --acct A` runs a probe through the same
tmux/background launcher and daemon environment as real work. It reuses `runner.script_for` for direct work and
`pool.station_argv` plus the shared station script for pools, including sources, cwd, launcher placeholders and
`pass_env`. Only the final host command is replaced: the probe checks command lookup and `--version`; it never
sends a prompt to a model. The launcher must return 0 and actually execute the probe. For `host: shell`, the host
is `bash`, and the launcher is bypassed just as for real shell workers.

Pool probes also create and remove a file in a mailbox under the pool root. Probes use the next station's account,
including account rotation. `--json` returns each check and its reason; a failed check exits 1. Results with timestamps
live in `$YARDGOAT_HOME/doctor/pool/<pool>.json` or `doctor/direct/<host-lane>.json`; probe scripts and logs live under `doctor/runs/`. Pool health
is also stored in `pools/<pool>/state.json` and surfaced by `status` and `inbox`.

Before starting a station, `pool.schedule` requires a successful result for that account newer than local
`doctor_ttl_h` (default 12 hours). Cache reuse also checks the pool, host, lane, account, timestamp, boolean result and
required checks (including mailbox for pools). Missing, failed, malformed or expired records trigger a new probe. Old
unnamespaced cache files are ignored. A failed probe
leaves cards queued and starts no station. Probes time out after 20 seconds; rerun doctor after changing local wiring
to refresh the cached result immediately.

The local `provider_refusal_patterns` list replaces the default case-insensitive regexes: `flagged for possible`,
`usage limit`, `You've reached your`, `rate limit exceeded`. Patterns apply only to host error payloads, never plain
text, assistant content or nested tool output. Recognized envelopes are `error`, `turn.failed`, and `result` with
`is_error: true`; known quota/policy error codes also count. A later `turn.completed` or successful `result` clears an
intermediate refusal. Codex launches request JSON events. Unknown host formats remain ordinary exits/lost runs rather
than guessing from prose; shell fixtures simulate the same error envelopes. A final refusal can still exit 0.

One classifier handles direct completion/recovery and pool completion/recovery. Priority is explicit launch failure,
then a real timeout handler, then provider refusal, then ordinary exit/lost. Returning 124 alone is not timeout evidence.
Direct execution evidence is stored in each run's `execution.json`; pool evidence is stored on the run and station.
Claim records the shared log's byte offset and completion captures its end, so earlier cards cannot supply refusal
evidence. Legacy pool runs without a boundary conservatively skip shared-log classification. Each scan is bounded to
20 KB. BLOCKED files never override a timeout.

If a station is refused before claiming a card (or in the idle interval after finishing one), its pool and host/lane/account
enter a visible backoff. Records live in `pools/<pool>/provider-backoff.json` and `provider-backoffs/`; `status` and `inbox`
show the account, reason and expiry. Scheduling waits until both applicable records expire, including other pools using
the same host/lane/account. `provider_backoff_seconds` defaults to 300. A successful doctor probe does not clear this
backoff, and queued cards receive no fabricated receipt. Owners can explicitly dispatch a refused task again; changing
the frozen card requires a new task id.

## Card metadata

Specs may record `scope` (a list of paths/globs relative to workdir), `risk` (`l1` documentation, `l2` ordinary work,
the default, or `l3` migrations/releases/permissions/CI), and optional `acceptance` (a sentence describing success).
`quick --scope PATH --scope GLOB --risk l1 --acceptance TEXT` includes these in the spec and at the end of `CARD.md`.
M2 uses `scope` for scheduling; metadata does not grant permissions. Each literal scope path becomes a
canonical absolute prefix (`Path.resolve`, including existing symlinks). A glob reserves the directory before
its first wildcard component: `src/**/*.py` reserves `workdir/src`, and `*.md` reserves the entire workdir.
This is deliberately conservative and covers future files as well as existing matches; it does not enumerate
only files that currently exist. Existing symlinks below these prefixes add their resolved absolute target
prefixes, including targets outside workdir and dangling links to future files. Directory targets are scanned
too, so nested aliases cannot escape the reservation. Directory device/inode identities prevent traversal
cycles; symlink-only loops keep an absolute prefix and are skipped when they cannot be traversed.
Prefix comparison respects path-component boundaries, so `src/a` and `src/b`
or `src` and `src-other` can run together. The reservation is saved on the running record until its outcome.
Omitted or empty `scope` opts out for backwards compatibility. `risk: l3` enforces the approval gate;
`acceptance` remains descriptive.

`depends_on: ["prepare-data", "build-tool"]` requires both upstream tasks to be verified before starting.
If an upstream declares nonempty `on_pass`, every declared step must also have a successful receipt
record. Pending, running, interrupted, preflight-blocked or failed delivery keeps dependents waiting;
`status` explicitly says it is waiting for that upstream’s 收尾. Without `on_pass`, verification alone
continues to release dependents. Dispatch, queue release and pool claim all use this same gate.
The accepted dependency graph is immutable just like the rest of the spec; changing dependencies needs a new id.

### Known limitations of scope scans

Each write-set calculation inspects at most 50,000 directory entries across all scope roots and linked
directories (one extra entry may be read to detect truncation). On truncation it emits a
`task.scope_scan_limited` task event with the limit, scanned count and explanation, then continues scheduling
with the prefixes collected so far. Checks and reservation can each emit this event. Unvisited symlink
targets may therefore be unreserved; the original prefixes still protect future ordinary files. Unreadable
directories retain their prefix but their links cannot be inspected. Scanning is not a filesystem snapshot:
links created or retargeted after the scan are not covered by the saved reservation.
An unchanged pre-M1 spec can be resubmitted without rewriting its frozen snapshot: comparison also tries the original
shape without injected defaults. Explicitly adding scope/risk/acceptance, even default values, remains a content change.

## Adapters

`adapter.kind` picks how work is started and observed: `direct` (Yardgoat starts the agent CLI) or
`file-pool` (Yardgoat writes a card into an existing file-queue fleet and watches it move through
`pending/claimed/done/parked`; the fleet's own workers claim it). Adapters that need private hosts or
paths are loaded from local files listed in the config's `plugins`. `yardgoat reconcile` (run it from
cron) polls adapter runs, marks runs whose process vanished as `lost`, and resumes answered tasks —
so accepted work keeps moving when no controller session is alive.

## Handover

A controller that disappears mid-run loses nothing: the run keeps going, and a new controller needs only
the task id. `yardgoat status <id>` shows owner, runs, liveness and receipts; `takeover` moves
ownership; `close` verifies. The old owner can no longer dispatch or close.

## Approval and post-PASS commands

`approve <id> --as <identity> [--note text]` runs under the task lock and binds its record to the accepted
content hash. This is a logical identity like the existing owner identity, not OS-user impersonation or
authentication. Configured `approvers` is checked both when approving and when starting; an empty or
malformed list grants nobody access. The owner and approver need not be the same identity. Approval
does not waive ownership, dependencies, lane rules, frozen checks or attempt limits. An unapproved
l3 task needs no run to appear in inbox (`reason: 等审批`). Approval events are `task.approved` and
`task.approval_invalidated`; each receipt contains the approval snapshot used to start that run.

`on_pass` is a list of objects with nonempty `name` and `command` strings and optional `cwd`. Omitted
cwd defaults to workdir; explicit relative cwd resolves against the spec directory. The normalized steps
are part of `content_hash`, so changing their commands, order or directories requires a new task id.
An optional positive `on_pass_timeout` in the spec overrides the local config value (default 600 seconds).
Delivery rechecks owner, PASS state, drift and l3 approval before invoking shell commands. They run in
the controller's OS environment, attributed to the current task owner, with `YARDGOAT_OWNER`,
`YARDGOAT_TASK_ID` and `YARDGOAT_RUN_DIR` set. No worker or adapter executes these commands.

Delivery takes a per-task delivery mutex before taking schedule → task locks to check and reserve
its write set. It releases both schedule and task locks before invoking commands; unrelated launches,
status and takeover can proceed during a slow step. Every worker launch/claim and other delivery checks
this same persisted `delivery_reservation`, even after its controller dies. Receipt/state updates use
short task locks and preserve current ownership/status. If ownership or status changes during a step,
that step may finish, but later steps stop until the current owner explicitly resumes. Concurrent
deliver calls serialize on the delivery mutex without holding the global scheduler lock.

Each new attempt records `launch_protocol: 2`, its PGID, group leader's `/proc/<pid>/stat` field 22
(`process_identity.start_ticks`), Linux boot ID, absolute deadline and canonical write set before the
shell is allowed to run. A pipe grant gates execution: controller death before the durable record
closes the pipe and prevents command execution. A small delivery worker stays as the identifiable
group leader until the shell and all non-zombie members of its group exit, including background
children left after shell exit. Its TERM handler keeps the leader available for identity checks before
KILL; child commands retain normal signal handling. Commands that deliberately escape the process
group are outside this local process-group contract.

Recovery skips persisted successes before inspecting any historical process fields. For unfinished
attempts, only an exact boot ID/start-time match authorizes waiting or signalling; a reused PGID does
not identify the old attempt. Every signal, including escalation, rechecks identity. A live matching
group refuses retry until its deadline; then delivery cleanup terminates it and confirms exit before
releasing the reservation. Timeout records exit 124; an interrupted attempt whose group has exited
records 125; startup errors record 127.

Recovery preserves the old persisted write set. Any newly resolved paths (for example after a scope
symlink changes target) are added as a union under the schedule lock. Only after old attempts are
confirmed stopped, when a new attempt is actually prepared, can the reservation be replaced with
the new write set; conflicts are rechecked in that same schedule → task section.

`reconcile` performs pure cleanup before taking its scheduling lock, using a nonblocking delivery
mutex acquisition for each task. It skips active controllers, retains live orphan reservations until
the deadline, and releases stopped attempts without automatically rerunning commands. `archive`
acquires delivery first, waits for any existing controller and orphan group to finish (or reach its
deadline and be terminated), and only then archives under schedule → task. Neither path waits while
holding the global schedule/task locks. Cleanup includes already archived tasks; `deliver` on an
archived task is cleanup only. Terminal progress fields and reservation release share one task-state
commit. If receipt writing succeeds but that commit is interrupted, reconcile/archive/explicit
delivery recover the receipt, replay missing result events and finish releasing the reservation.

Legacy unfinished records without process identity and without `finished_at` are unknown, never
automatically retried or released. They preserve (or reconstruct) conservative scope reservations
and appear in inbox with “旧版本留下的收尾记录，进程状态未知”, including older archived records.
After confirming old processes have stopped, the current owner can use `deliver --assume-stopped`.
The human assertion is recorded as `task.delivery_assumed_stopped` before release or retry, with
actor, run, step and attempt. It cannot override a known live process. Legacy successful records
still skip normally. Only a new-protocol record with no PGID can prove the command had no grant.

Combined stdout/stderr goes to `runs/<run>/on-pass-<step>-<attempt>.log`; the last 4000 bytes are decoded
into `output_tail` in the receipt and `task.on_pass_finished` event. `task.on_pass_started` records each
start. Records also carry the step number, command, cwd, owner, attempt, timestamps and timeout flag.

The receipt's `on_pass` list is the progress journal and result-event outbox. It holds the latest attempt
for each started step, while events and logs retain earlier attempts. Recovery replays missing finished
events using stable task/run/step/attempt event IDs, checking existing events before appending. This
happens before skipping a success or replacing a failed attempt, so both success and failure history
survive the receipt-first crash window without duplicate events. Successful steps are skipped, including
duplicate step names. Commands must still tolerate retry after external side effects complete but before
the successful local receipt is committed; those two operations cannot be atomic.

The PASS commit first marks declared delivery pending in both task state and receipt. Only success
records for **every declared step** clear `delivery_pending` / `delivery_failed`; an empty or partial
journal is never success. Preflight refusals and configuration errors persist their concrete reason.
The task and receipt retain `verified`/`passed`, and status, inbox and repeated close show pending delivery
and its reason (also recovering receipt-first state). A successful retry clears the pending flags and
inbox entry. `close` exits 1 for unfinished delivery on repeated close while still reporting `verdict: PASS`;
initial close and `deliver` exit 0 on completion, 1 on a failed step, 2 for configuration errors, or 3 for
policy/live-process/scope refusal. Recovery or retry uses `deliver`, including after ownership changes.
Auto-close uses the same path and adds delivery exit code, pending flags and reason to reconcile's single
JSON output. Existing close behavior is retained for tasks without on_pass.

`report` counts human `task.approved` events as a separate `approve` intervention and includes them in
the total, using the same machine-actor exclusions as other interventions. Delivery step events do not
count as human decisions.

## Not yet

Issue-driven and queue-file adapters (beyond `file-pool`), an MCP entry point, cost/model-call budget
gates, leases for multi-host stores, and richer evaluation data. See the roadmap in the README.

## Reports and pending reminders

`report` reads task state, accepted specs, receipts and events without changing them. It groups each task
once, using its pool/project and latest available receipt's observed lane/model/account/host; absent
lane/account/host fields fall back to the run and requested executor. An unobserved model is `(unknown)`.
All of that task's runs contribute to its group, even if a retry used a different executor. `--since`
filters the inclusive acceptance date as recorded (not the latest activity date). Groups sort by name,
never by performance. Below five tasks, all metrics are null and the table shows count plus `数据不足`.

Attempts use the dispatch budget's counted states: `exited`, `failed`, `verified`, `lost`, `verify_error`.
Queued/running, cancelled, blocked, launch failures and provider refusals do not count. First-pass rate
is tasks whose first counted attempt's first recorded verification is PASS divided by all selected
tasks, including unfinished ones. Events preserve an earlier ERROR when a later verification replaces
the receipt. Average attempts uses the same task denominator; average wall seconds uses completed runs
with valid start/end timestamps, excluding queue time and invalid/missing intervals (no samples → null).
Human intervention counts use `task.answered`, `task.owner_changed`, `task.archived`, `run.cancelled`,
excluding runner/reconciler actors and stations identified by records, run/receipt metadata, claim
events or the standard `*-st-N` name. Other event kinds are not interventions.

Abnormal outcomes are deduplicated per run and status across state/receipt/events: `lost`,
`launch_failed`, `submit_unknown`, `verify_error` (including receipt `verifier_error`), `provider_refused`;
unknown status strings enter `other`. Known lifecycle states such as `failed` are not unknown outcomes.
Cost fields sum independently across receipts; USD uses decimal arithmetic and tokens use integers.
A task counts once as unknown if any run lacks parseable cost data (including a missing receipt), or
if it has no receipts. Partially parseable receipts retain known amounts and still flag the task unknown.
Token counts are never converted to prices, and shared-station costs are never apportioned.

`notify.notify_pending` runs after reconciliation/auto-close and outside the scheduler/task locks. It
also recognizes a last run's `verify_error` when legacy task state remains `exited`, and ignores closed
tasks. Local configuration supplies only an argv template; there is no built-in destination or transport.
The placeholders are `{task_id}`, `{status}`, `{owner}`, `{question}` (first 500 characters), and `{hint}`.
Commands run without a shell, with closed stdin, captured output and a 30-second timeout. Errors only
log to stderr, leaving other reconciliation actions and its single JSON output intact.

`$YARDGOAT_HOME/notified/<task_id>.json` remembers every successful `(task_id, status, SHA-256 of the
full question)` tuple. A separate per-task lock prevents concurrent reconcilers from sending the same
tuple; returning to an already delivered tuple does not resend it. Only exit 0 records success, written
atomically. Failed commands or bookkeeping writes retry on the next reconcile; a crash between external
delivery and its local record can therefore redeliver, so hooks may also deduplicate on their side.
