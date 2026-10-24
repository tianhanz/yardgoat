# Design (P0 + P1 + pools + engineering review)

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
2. **One owner.** Legacy cards use the current string owner and `takeover --from <current owner>`. Project cards use the current controller session/epoch through a stable `owner_ref`; control moves through project handoff. System continuation is explicitly attributed to the reconciler.
3. **No duplicate dispatch.** While a run is alive, `dispatch` returns the existing run. A run that exited must be verified before another attempt.
4. **Frozen verification.** The card and every file listed in `verifier.freeze` are hashed at submit; any drift blocks both dispatch and close.
5. **Exited ≠ verified.** A normal worker exit only writes a receipt with `status: exited`. `close` runs the verifier: exit 0 → `passed` (or `reviewing` when review is configured), exit 1 → `failed` (retry allowed within `budget.max_attempts`), anything else → `verifier_error` (a crashing verifier is not a failing deliverable).
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
14. **`auto_close` defaults to true.** `reconcile` runs the frozen verifier on the current owner's behalf once a
    run exits. Explicit false keeps manual close; configured review retains its existing automatic policy.
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
    absolute path in `status` and reconcile output. Every worker outcome releases its run reservation; review-enabled
    tasks also hold a task-level reservation until final verification or archive. Reconcile
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
24. **Review is part of acceptance.** A frozen verifier PASS starts the configured independent review;
    it does not verify the parent. While reviewing or repairing, dependencies and delivery remain gated,
    and the task-level write set stays reserved. Only a clean review or the explicit round-limit
    `pass_with_followups` policy reaches verified. Review child verification failures cannot produce PASS.

## Launch-chain health checks

`yardgoat doctor --pool P` or `yardgoat doctor --host H --lane L --acct A` runs a probe through the same
tmux/background launcher and daemon environment as real work. It reuses `runner.script_for` for direct work and
`pool.station_argv` plus the shared station script for pools, including sources, cwd, launcher placeholders and
`pass_env`. Only the final host command is replaced: the probe checks command lookup and `--version`; it never
sends a prompt to a model. The launcher must return 0 and actually execute the probe. For `host: shell`, the host
is `bash`, and the launcher is bypassed just as for real shell workers.

Pool probes also create and remove a file in a mailbox under the pool root. Probes use the next station's account,
including account rotation. `--json` returns each check and its reason; a failed check exits 1. Results with timestamps
live in `$YARDGOAT_HOME/doctor/pool/<pool>.json` or `doctor/direct/<host-lane>.json`; these saved reports exactly match
the printed JSON. Without `--pool`, the report also includes the global reconcile freshness check described below.
Probe scripts, logs and raw launch-chain results live under `doctor/runs/`. Pool health
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

## Run prompts and retry feedback

Direct/file-pool launch and pool claim capture the same prompt composition in
`tasks/<id>/runs/<run>/prompt.md`: the frozen card, all recorded human answers and engineering
review feedback in order, then an optional `上一次尝试的验收结果` section. The section names
the latest judged attempt when its verdict is FAIL, followed by at most the last 4000
characters of Yardgoat's own `runs/<previous>/verify.out`. Longer output is explicitly marked
as truncated, with the full log's path. Workspace files and worker logs are never sources.
Empty or whitespace-only output is replaced with a fixed explanation that the verifier returned
a nonzero exit code without output, rather than an empty code block; the log path is still shown.
Only the latest recorded judgment is included, regardless of how many runs preceded it.
Queued and cancelled entries are not attempts. PASS, ERROR, no recorded judgment, or missing verifier
output add no section. A preceding launch failure or provider refusal also suppresses feedback,
even when an older failure is recorded. Answering a parked question retains the latest judgment.
Manual retries and configured review's automatic retries share this behavior. Existing retry
gates remain unchanged: a verifier ERROR blocks dispatch and pool claim until its environment is
fixed and `close` is retried on the same run. If that returns FAIL, the next attempt includes the
new FAIL output, not the earlier ERROR output. A verifier timeout writes its duration and any
captured output (or an explicit no-output explanation) to `verify.out` before recording ERROR.

The run also captures `goal.md`, containing only the frozen card and ordered answers. Review
children quote this snapshot with its `goal.md` path and the current card/answers without retry
diagnostics; the full execution prompt remains recorded at its original path. For runs predating
this snapshot, review quotes their original `prompt.md` with that file's path. Neither prompt file
changes the accepted card, spec, verifier files or frozen hashes. File-pool adapters transport the
captured `prompt.md`.

## Handover

A controller that disappears mid-run loses nothing: the run keeps going, and a new controller needs only
the task id. `yardgoat status <id>` shows owner, runs, liveness and receipts; `takeover` moves
ownership; `close` verifies. The old owner can no longer dispatch or close.

### Automatic verification and upgrades

`submit` requires an explicit `auto_close` to be a JSON boolean (strings such as
`"false"` are rejected). An omitted `auto_close` is interpreted as true when reconciling, rather than inserted into an accepted
spec. Exited runs use the existing `do_close` path outside the scheduling lock, attributed to the
current string owner for legacy cards or the explicit reconciler for project cards.
The owner's interactive session is not required. `do_close` rechecks ownership,
task state and run state under the task lock, so two reconcilers observing the same exit execute the
verifier only once. Frozen-file drift still refuses verification. Worker exit codes do not bypass
the verifier: exit 0 and nonzero exits receive the same frozen acceptance check.

PASS continues through normal engineering review, frozen `on_pass` and downstream dependency release.
FAIL and verifier ERROR retain their existing states and attempt accounting. Ordinary failed tasks
are not automatically dispatched again, and verifier errors still need explicit close after repair.
Review-enabled tasks retain their existing review retry/repair policy, including automatic verification
even with `auto_close: false`; this upgrade does not alter that loop or l3 approval/write-set gates.

**M7 upgrade behavior:** already accepted tasks lacking `auto_close` now advance automatically after
exit on the next reconcile. This includes their already authorized frozen review and delivery steps.
No accepted card, spec, content hash or frozen material is migrated or rewritten. Existing explicit
false values keep ordinary tasks manual. To keep new ordinary tasks manual, submit that explicit
value or use `quick --no-auto-close`. `quick --auto-close` stays compatible and means true; omitting
both flags uses the default without injecting it into the spec. Supplying both is a usage error.
Changing an existing immutable spec still requires a new task ID; this default is not a migration
mechanism for adding or changing permissions.

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
For project cards, recovery explicitly runs with system audit context even when
manual `deliver` or `archive` triggers it. The replay event records the current
system `on_behalf_of`, and `execution_control` preserves the original session/epoch
or original system `on_behalf_of`. An immediately completed step still uses its
original execution snapshot directly, including when handoff happened during execution.

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

## 会话与项目主控 (sessions and project control)

M8b connects opt-in tasks to the stable project controller role. Registering a project
alone does not migrate tasks whose spec happens to use the same project string.
Legacy tasks without `owner_ref` retain their string-owner permissions.

### Session identity and trust boundary

A session is one local Linux agent CLI process instance, identified by
`(boot_id, pid, start_ticks)`. Its deterministic ID is `s-` plus the SHA-256 of the
compact JSON array `[boot_id, pid, start_ticks]` (PID integer, ticks string).
No process-group-leader assumption is made. `/proc/<pid>/stat` is parsed after its
last `)`, so spaces and parentheses in comm do not shift field 22.

Identity inspection starts at the CLI's parent, skipping Yardgoat's own interpreter.
The first matching agent is the caller, including nested agent invocations. Inspection
continues up to PID 1 to reject station ancestors, without substituting an outer agent.
PID 1 is the root boundary and only its stat is required. An unreadable, vanished,
cyclic or ambiguous chain returns unknown and a reason, never a guessed identity.
Reparenting normally fails closed; the bounded foreign-ancestor exception below
re-inspects the complete current chain. Each ancestor's real UID comes from `/proc/<pid>/status` and is compared
with the caller's `getuid()`. Other users' processes cannot be this user's agents
or stations: only their stat/status are read when accessible, skipping environ, cmdline and
exe while continuing through their parents. Same-user ancestors above them are
still inspected for station markers. An unreadable same-user process or unreadable
UID evidence remains unknown with the read error. `stat` and `status` are not always
public: a `/proc` mount with `hidepid` can restrict them too. UID read errors mention
this possibility. Ancestry, UIDs, start ticks and boot identity are checked again
before returning. If an observed foreign ancestor disappears during revalidation,
its absence carries no authority: walk the current ancestry again, at most once.
The nearest agent identity must remain unchanged; UID/start-time changes and
unreadable same-user evidence still return unknown.

`sessions.agents` is an array of rules such as `{"name":"custom","exe":"custom-cli"}`.
`exe` and `argv0` match exact basenames; `comm` matches an exact process name. Selectors
within one rule are alternatives. Two rules matching the same nearest process are
ambiguous, even if they have the same name. Setting the array replaces all defaults;
an empty array recognizes nobody. The default rules match only explicit `claude`,
`codex` and `kimi` names. Generic `node` and `python` interpreters are not defaults.
Configure an explicit suitable name when an installation does not expose those names.

The current process's inherited `YARDGOAT_STATION`, `YARDGOAT_MAILBOX`, and
`YARDGOAT_MAILBOX_HOME` are checked before any proc reads. Those keys on a same-user
ancestor, or a same-user ancestor's
`yardgoat _station` / `python -m yardgoat.cli _station` invocation reject session
identification, including wrappers with global `--session` arguments before
`_station`, and administrator PID inventory. Changing the target store
does not remove the station restriction. Permission failures for required evidence
are unknown, including restricted proc mounts. A session list reports `alive`,
`dead`, or `unknown`:
boot/start-time mismatch, confirmed PID absence, or a zombie is dead; inability to
read the evidence is unknown. An absent proc entry requires a readable proc root
and an existence check (`kill(pid, 0)`, which sends no signal) reporting no such
process before it counts as dead. Hidden entries for existing or inaccessible
processes stay unknown. Neither status automatically changes project control.

The endpoint comes from `TMUX` and `TMUX_PANE`. A bounded read-only tmux lookup
checks whether the pane PID is the agent itself or an ancestor of it. Mismatch,
missing tmux or lookup failure leaves it unverified. The endpoint never contributes
to identity or permissions. The lookup is injectable in tests; no message is sent.
Administrator registration uses the target process's endpoint environment.

`sessions/<session_id>.json` records the identity, label, endpoint, initial registration
time, and append-only registration provenance (operator, reason, identity evidence).
Self-registration is idempotent; a supplied label can update the display label.
`session register --pid` checks that exact live PID against agent rules and uses
`project_admins` for administrator authorization. **Administrator registration can
only register processes belonging to the caller's same OS user**, including when
the administrator is root; it cannot inventory another user's agent as itself. `whoami --json` returns `session_id`,
identity evidence, endpoint, registration state, label and controlled project IDs.
`project_task_counts` maps those IDs to unfinished task counts; `unfinished_tasks`
is their total (verified, archived and exhausted tasks are excluded).
Unknown identification exits 3. `--session` is an assertion against mechanical
identity, never impersonation; conflicts also exit 3. It may precede the command or
follow a session/project leaf command. On `project assign`, the leaf `--session`
selects the **target**; an optional caller assertion must precede `project`.

Keep three separate states in mind:

| State | Meaning |
|---|---|
| Registered | A process identity is recorded; it has no project permission yet. |
| Controller | A project's control record names that session and current epoch. |
| Managed by an external handover tool | An external tool manages session startup/exit; registration and controller status imply no such management. |

`project_admins` is independent of l3 `approvers`. Missing, empty or malformed admin
configuration grants no administrator permission. Administrator `--as` and reason
are cooperative assertions, just like existing owner identities. This is **not**
security isolation between mutually hostile processes under the same OS user:
that user can modify config, process names, environments and store files. Transfer
tokens are bearer credentials. No tmux endpoint, project root, session label, health
record or external handover label grants control.

### Project state, epochs and crash recovery

Each `projects/<id>/project.json` holds immutable ID and creation metadata, display
label/root, claim policy (`open` by default), controller `{session_id, epoch, since,
how}`, current transfer ID, transfer records and append-only history. IDs are never
reused by a delete/rename command. Root is display information only. History records
actor/time, old/new controller (including epoch), old/new status, old transfer and
reason. The controller starts at null with epoch 0.

Claim requires an identified, registered caller, open policy and epoch 0. Every
claim/handoff/assign compares `--expect-epoch` under the project lock. Handoff also
requires the current controller and a registered, confirmed-alive target. Success
advances epoch exactly once. A dead controller never reopens claim; an administrator
uses assign to initialize or recover control. Assign also cancels a pending transfer
and records the previous state and recovery reason.

Transfer begin requires the current controller and expected epoch. It saves a random
transfer ID and only the SHA-256 of a 256-bit token, returning the token once to the
caller. Tokens carry a fixed `yg-` prefix before the random URL-safe payload to
prevent argparse from treating a leading `-` as an option in `--token TOKEN`.
Existing hashes remain valid; older tokens beginning with `-` can be passed as
`--token="$TOKEN"` for commit or abort. The project is `transferring`; claim,
handoff, another begin and control authorization are refused. The old controller's
epoch is retained until commit.
Commit authenticates the token and moves to a live registered target (default: the
identified caller), incrementing epoch once. An explicit `--to` permits a token
holder to finish for that target. The saved result makes duplicate commit return
exactly the same result without another event or epoch increment. A changed target,
cancelled/aborted transfer, or any later handoff/assign/transfer rejects an old commit.
A replay of an already completed result does not perform another liveness-dependent
assignment. Abort accepts the token or current controller, restores normal status
and retains epoch. Repeating that abort is also harmless until control changes.

The state, token hash, saved result and history are in **one atomic document**.
The writer uses a unique temporary file, flush/fsync, atomic rename and directory
fsync under an independent project lock. Crashes before rename leave the old state;
after rename leave the complete new state. Partial temporary files are never read.
Thus commit/abort can replay without a separate event-file reconciliation window.
An interrupted begin can be inspected with project show. If the begin committed but
the process died before its one-time token reached the caller, the token cannot be
reprinted: the old controller can abort without it and begin again, or an admin can
assign if that controller also exited. This deliberate recovery path avoids storing
plaintext/recoverable tokens. The store does not provide distributed leases.

### Locks and library interface

Total lock order when an operation needs multiple domains is:
**delivery mutex → schedule → pool → project → task** (parent task before review
child). Multiple project locks must be acquired by sorted project ID. Session
registration and reconcile-health locks are standalone and never nest into this
chain; project operations only read atomic session snapshots. Existing paths that
do not touch projects keep their current locks. No external command, tmux lookup,
model call or network request runs while a project lock is held.

`sessions.identify(config)` produces the caller object for
`projects.authorize_control(project_id, caller)`, which returns `(session_id, epoch)`
or raises `sessions.Refused` with the missing-project/identity/registration/control/
transfer reason. `projects.recheck(project_id, session_id, epoch)` rejects stale
epochs, including A→B→A. Both wrappers hold a short project lock and are point-in-time
checks, not durable grants. At a task commit, control operations acquire the project lock **before**
the task lock and keep it through the task write, using `projects.locked(id)` plus
`authorize_control_locked(state, caller)` / `recheck_locked(state, sid, epoch)`.
Do not reacquire the same flock through a wrapper while already holding it. These
helpers are used by `control.operation` / `control.locked`. A context-local ticket
retains the original session/epoch across nested manual steps. A distinct internal
system context authorizes the reconciler; an `--as reconciler` label grants nothing.
Verification has its own per-task mutex before project/task locks. The frozen
verifier runs outside both locks; the verdict commit reacquires them, checks the
original ticket and the same exited run, and refuses stale results.

### Project-owned tasks and operation permissions

A new spec opts in with `"owner": {"project": "example"}`. `submit --project-owned`
uses the spec's `project` value for that object. The project must exist and the
mechanically identified, registered caller must be its current controller.
Submission freezes the spec normally, stores `owner_ref = {"project": id}` in task
state, and stores the controller's submission-time label in the existing `owner`
string. That string is display/compatibility data only for project cards.

`project bind TASK --project ID --as LEGACY_OWNER` requires both the exact current
string owner and a caller session controlling ID. It binds one previously unbound
card and records `task.project_bound`; it never rewrites the accepted spec,
content hash or frozen materials. A project handoff updates only project state;
it never scans or edits individual cards. Generated review children and follow-ups
inherit the parent's state `owner_ref`, including from bound legacy parents.
`takeover` always refuses a project-owned card with exit 3 and directs the caller
to project handoff/transfer.

| Operation on a project card | Authority |
|---|---|
| submit / bind | Current project controller; bind also requires the legacy owner string |
| dispatch, manual close, park, archive, deliver, confirm-review-channel, cancel | Current registered controller session; unknown is refused, with no `--as` fallback |
| Automatic verification, answered-task resume, review/repair/retry, delivery continuation and recovery | Internal `reconciler` executor, independent of controller liveness |
| Station claim / finish | Existing station/mailbox identity |
| answer | Existing answer rules; need not be the owner |
| approve | Existing approver list and frozen-content rules |

All policy refusals exit 3. Manual control events include `actor_session` and
`epoch`. Reconciliation events use `actor: reconciler` and
`on_behalf_of: {project, epoch}`, never an invented active session identity.
When replaying a delivery result, `execution_control` retains the original grant's
session/epoch or system `on_behalf_of` as provenance. Verification persists the
same control snapshot into `receipt.verifier` at its verdict commit, before
writing `run.verified`. The event uses that snapshot without resolving a later
controller or system epoch. Manual receipts retain `checked_by` plus
`actor_session`/`epoch`; system receipts retain `checked_by: reconciler`,
`actor: reconciler` and `on_behalf_of`.
A control entry point captures its ticket before waiting; task commits and delivery
pipe grants recheck the original epoch. A→B→A therefore cannot revive an old call.

Handoff does not revoke an already accepted queued/running execution or a delivery
step that received its pipe grant. Stations can finish after any handoff. A running
delivery step commits its outcome using its original audit data, but the next step
needs another check of the original ticket; a stale manual controller stops with
exit 3. The new controller can `deliver` to resume, skipping persisted successes.
System continuation uses the same ordinary approval, dependency, review and drift
gates without impersonating a controller. A pending transfer blocks new manual
control; it does not stop these previously authorized system actions.

`status` and `inbox` expose `owner_ref`, the role project, current controller label,
session, epoch and liveness, and `transferring`. These projections are read-only;
`owner` remains its original display string. `report --by project` groups project
cards by `owner_ref.project`, falling back to the legacy spec project otherwise.

### Reconcile health

Every reconcile round writes `$YARDGOAT_HOME/reconcile-health.json` at start and end,
under its own short lock: `last_started_at`, `last_finished_at`, `last_success_at`,
and `errors: {count, first}` (up to five bounded summaries). Only a round with no
action errors advances last_success_at. Adapter exceptions, failed command results,
review/delivery/auto-close refusals, failed pool probes, caught station launch errors
and notification failures are included. A context-local error collector also covers
station launches reached through nested dispatch/review continuations; failed
launches do not appear in `station_started`. The collector holds no lock and is
removed at the end of each round. A fatal action exception records the end/error and exits 2; existing
best-effort errors keep their original command exit behavior. SIGKILL leaves a
start without its finish. Concurrent rounds merge fields under the health lock;
the most recent completion supplies its error summary and does not erase a newer
start. This is observational bookkeeping, not a lock on reconciliation.

`status --json` preserves the existing task/list shapes and adds `reconcile_health`
to each row (a health-only row for an otherwise empty list). `reconcile` returns the
same health record it just persisted. These are command projections; neither adds
fields to stored task specs, task state, receipts or events.

`doctor` **without `--pool`** (including `--host`) adds a global freshness check:
a recorded round with no end, an unreadable record, or an end
older than `reconcile_stale_min` (default 10 minutes) alarms. A new store with no
record reports that no round has run yet. The check, aggregate `ok` and failure
`reason` are finalized before saving `doctor/direct/<host-lane>.json`; the CLI
prints that returned report unchanged. Full health data remains available through
`status --json`, rather than a second top-level field in doctor reports.
`doctor --pool P`, its saved pool report and automatic station admission contain
only launch-chain checks: global reconcile staleness cannot make a healthy pool
fail or invalidate its probe cache. Health is never read by project authorization
and is never treated as a controller heartbeat.

## Engineering review loop

The accepted `review` object names `pool`, optional `path` (default `.` relative to workdir),
`rounds` (default 3), `blocking` (default blocker/major) and `after_max_rounds` (default park).
Submission adds the effective review `channel` (`host`, `lane`); all fields participate in the
content hash. Pool names and effective lanes must differ from the execution channel; if both
lanes are absent the hosts must differ. A station wrapper records its channel and writable root
from the configuration used to launch that process. Claim copies the actual channel into the
run, and receipts retain that identity even when configuration later changes. Direct/file-pool
dispatch also records its effective channel. Review creation selects the execution run by the
round's `run_id`, compares it with the frozen review channel, and rejects changed review pool
configuration. Review claim additionally checks the actual station channel and launch root;
legacy stations missing that identity must be restarted. Task configuration/frozen-material
errors retain task-level failure handling. Station incompatibility instead returns EMPTY with
`restart_required`, `restart_reason` and a timestamp on the station, leaving the run/task queued.
The scheduler excludes these processes from available stations but includes them in physical
capacity while alive; the normal EMPTY response lets the worker exit. A healthy station can
claim the unchanged card during a rolling upgrade, without answering each task.
Configured review enables auto-close for execution and repairs.

`confirm-review-channel <id> --as <current-owner>` migrates legacy accepted/running/reviewing
tasks explicitly, under schedule → task locks. The authoritative frozen record gains a
`review_confirmation` extension, containing actor/time, the review pool channel at confirmation,
recorded run channels and any owner-supplied `--run-channel RUN HOST LANE` declarations.
The original accepted spec/hash, IDs, dependencies and rounds are untouched. `TaskDir.spec`
overlays only this explicit extension for a legacy missing review channel. No ordinary read,
dispatch, answer or reconcile derives that missing field from current config. Missing historical
execution identities require individual owner declarations; existing identities cannot be
overwritten. Channel separation and workspace checks run before recording the confirmation.
The extension is written before run updates and the deduplicated `task.review_channel_confirmed`
event; repeating the command repairs an interrupted write using the original actor/time/channel,
not a newly read pool config. Confirmation never dispatches, changes task status or consumes a
round. A parked task still needs its normal answer; a running task finishes its existing run.
The frozen confirmation also stores `control`, the original controller identity
snapshot. Event recovery uses its original actor, time, session and epoch, and
separately records the invoking controller in `replayed_by`. Old confirmations
without a snapshot keep their identity unknown; recovery never fills it with the
new controller's identity.

After frozen acceptance, `review.begin` records a pending round containing the exact child id
`<parent>-review-<n>` and the execution run id. Parent/run/receipt become `reviewing` with the
verifier PASS retained as evidence. Successful reviewed runs transition to `reviewed` when a
repair is requested. These successful rounds are excluded from the failure retry counter;
failed repairs still consume the original `max_attempts`. The review round budget advances
only after frozen acceptance succeeds, including when a repair needed failure retries.
Dispatch and `begin` independently enforce the immutable round limit, even if a legacy or
interrupted state has an incorrect `complete` flag.

`review.advance` holds schedule → parent → child locks for child creation and result decisions.
Generated cards, snapshot manifests and standalone verifier copies live in owner-side
`reviews/<parent>/<round>/`; accepted specs have the ordinary authoritative frozen copies.
Review workdirs live under the review pool root in `.yardgoat-reviews/<child>/`, with `target/`
containing a snapshot and `out/` containing the review. `pool.effective_root` is shared by review
placement, station argv/scripts, mailboxes and doctor; the default is
`$YARDGOAT_HOME/pools/<pool>/stations`. Doctor probes `.yardgoat-reviews` writability through the
same launch chain; cached health also includes the effective root and this check.
Submission checks every possible child workdir and its output scope against the parent's full
write set (canonical glob prefixes and symlink targets). Creation checks again against the union
of current scope and retained run reservations; existing children and claims are rechecked too.
Overlaps are rejected with paths, never left queued against their parent's reservation.
The original directory and snapshot are
both checked for additions, removals, file bytes, modes, empty directories and symlink destinations.
Links are copied without traversal; findings must name ordinary files inside the snapshot.
The target must not contain the review workspace or owner-side assets, avoiding recursive snapshots.

Manifest comparisons ignore untracked build/test caches in either tree: directories named
`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.hypothesis` and `.benchmarks`
at any depth, and ordinary `*.pyc` files. Snapshots and stored manifests still include those
entries; ignoring affects comparison only and never deletes or rewrites files. Existing
entries' type and permission changes still fail, and symlinks are never ignored.
Reviewers should copy the snapshot to a temporary directory outside the review workdir before
running tests, or at least use `PYTHONDONTWRITEBYTECODE=1` and pytest `-p no:cacheprovider`
and leave no artifacts in the snapshot.

Machine-local config can add `"review_ignore": [".host-state/", "logs/**/*.tmp"]`.
These are relative to the reviewed directory: `*`, `?` and character classes match within
one path component, and `**` matches zero or more components. A matching directory ignores
its subtree; a trailing `/` matches directories only. Patterns are anchored at the reviewed
root, so use `**/.host-state/` to match that directory at any depth. Absolute paths and `..`
components are rejected. Broad rules can hide real changes in untracked files; prefer narrow
tool-specific paths. This setting is read and **frozen at review child creation**, together
with the Git-tracked paths, in the child's hashed owner-side settings. Later configuration or
index changes cannot alter that child's policy, including retries and interrupted creation
resumed from saved settings. Each later child freezes its own current policy; existing frozen
children retain their original behavior.

Git-tracked paths, including files inside a reviewed repository subdirectory or worktree,
and their containing directories are always compared, even when an ignore rule matches.
Matching tracked paths produce a warning and a `task.review_ignore_warning` parent event
at child creation. An unreadable Git index fails creation rather than dropping this protection.
The frozen owner-side settings also contain a complete workdir manifest, excluding exactly
`out/findings.json`, `out/REVIEW.md` and the card's explicit `out/BLOCKED.md`. The verifier rejects
other persistent additions, removals, modes, bytes or symlinks; allowed output entries must be
ordinary files. The same frozen ignore policy applies only within its `target/` subtree;
even cache-shaped files elsewhere in the workdir remain unauthorized. This baseline is
retained across child retries and is never refreshed from a
completed worker's files.
This is an integrity check at verification time, not an OS read-only mount or a monitor of
transient writes. Other scoped workers are excluded by the retained lease.

The standalone verifier requires a nonempty REVIEW.md, a JSON array, unique nonempty finding
IDs, known severities, concrete file/line references and nonempty title/scenario/fix strings.
It returns the validated findings on stdout; close saves those bytes as structured receipt
data before marking the child verified. Parent decisions read that receipt, never the mutable
findings output after verification. The child has a two-attempt failure budget. Failed output
validation retries the child; exhaustion, verifier error, archive, refusal or a blocked child
parks the parent with an inspectable reason. Answering cannot skip a pending child result.
Frozen child-file drift is detected even when close refuses without a verdict; a failed close
also advances the parent immediately after releasing the child lock. Restoring frozen files and
answering the parent replays the pending round; verifier errors can be retried with child close
after the environment is fixed. A launch refusal can retry the same round after a parent answer.

Run prompts are captured at launch/claim. The frozen review card embeds the corresponding
execution goal/answers snapshot with its run/path provenance (excluding retry verifier logs),
all answers in recorded order, and the earlier rounds' validated findings. It asks the reviewer
to confirm each previous fix. This material is
read-only review context and grants no authority to execute the original task's changes.

Round records, deterministic child/follow-up IDs and numbered `review-NNN` answer keys make repeated
close and reconcile safe. Answers use the same prompt mechanism as human replies and are
attributed to the reconciler. Numeric prefixes preserve ordering with human answers. A result event has a stable round ID and is replayed without
duplicates. The child and its frozen receipt are the durable source for replay if the parent
controller stops before saving its state; existing accepted child IDs are never overwritten.
Result consumption stages state in a copy: create/reuse all follow-ups, write the receipt and
deduplicated event, then commit `processed`, the final decision and lease release together in
parent state. An ordinary side-effect error parks the original pending state, retaining its
reservation and retry entry point. A crash before state commit replays those idempotent side
effects. Partial follow-up success is reused by deterministic ID plus content hash; a foreign
ID collision stays pending and visible instead of starting a new execution round.
If the parent was bound to a project after a partial follow-up acceptance, replay
compares the follow-up against its original frozen spec (including its historical
owner field), frozen files and exact parent/round/finding relation. Role inheritance
then updates only task state, after the independent ownership-conflict checks;
it never rehashes an existing task with the parent's newly bound role.
Before accepting each follow-up, a round writes its deterministic ID and initial owner into
owner-side `reviews/<parent>/<round>/followups.json`. This journal survives discarded staged
state and crashes immediately after acceptance. The pending parent row exposes accepted IDs.
Takeover takes the schedule lock and changes review child owners with the parent; reconciliation
also repairs interrupted owner propagation. Pending follow-ups are synchronized using their
journal and `owner_history`, with inherited ownership changes marked by parent ID. Older partial
rounds without a journal recover existing associations from the verified review receipt and
deterministic IDs. A foreign ID is never adopted. An independent follow-up takeover is preserved;
the parent parks with the conflicting task/owner until ownership is explicitly coordinated and
the parent answered. Matching owners need no mutation; repeated replay adds no history entry.
After the round is processed follow-ups are independent. Direct review child takeover is refused
in favor of the parent.

Removed-pool inbox warnings consider only tasks outside verified/archived/exhausted. Per-task
status still annotates a removed pool on historical tasks for inspection.

Blocking findings before the limit trigger one repair dispatch. Without blocking findings the
parent becomes verified and follows the normal M3 delivery path. At the limit, park leaves the
parent unverified with findings in its question and retains its write set until archive. `answer`
refuses a completed review budget, explaining that further work needs a new task or archive;
the task stays in inbox. JSON and text status display its retained review write set.
`pass_with_followups` accepts one follow-up per remaining
finding (including nonblocking ones), then verifies the parent. Follow-ups inherit execution,
scope, risk, authority and frozen acceptance; they have no review or on_pass automation and no
run until explicitly dispatched. They appear as accepted decisions in inbox. An l3 follow-up
requires a new approval; repairs of the original card retain its valid original approval.

The original task holds `review_reservation` from worker launch/claim, across exits, verification,
review waits and repair queueing. New repair reservations union with the retained prefixes.
Review children write only their separate out directory, so they do not conflict with their
parent. Final verification removes the review lease and invokes normal M3 delivery; archive
also stops reserving it. Each execution/repair receipt has `review_round`; child receipts also
include `review_of`, `review_findings`, `findings_count`. Parent receipts retain round summaries
and the final receipt includes all rounds, decisions, finding counts and follow-up task IDs.

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
tasks. Local configuration supplies one notification object or a list of objects; there is no built-in
destination or transport. Validate every route before sending any: each requires a nonempty string argv
list, an optional list of status strings (`on`), an optional nonempty string `name` (default `default`),
and an optional string `recipient` template. Duplicate names, including duplicate defaults, invalidate
the entire configuration and produce a warning. Empty lists disable notifications.
The placeholders are `{task_id}`, `{status}`, `{owner}`, `{question}` (first 500 characters),
`{hint}`, `{controller}`, `{controller_session}` and `{endpoint}`. The last three
resolve to the current project controller's registered label, session ID and a compact
JSON object `{"socket":"...","pane_id":"..."}`. For legacy cards all three are empty.
A project route referencing any of these fields requires a confirmed-alive current
controller. Only a route referencing `{endpoint}` also requires a verified endpoint
with both socket and pane; label/session routes can reach sessions outside tmux.
An unavailable route records no sent key, and a later reconcile retries.
Routes without these placeholders still
send to their configured human destination. The controller is resolved afresh for
each send attempt, outside project locks; the selected snapshot defines that send's
recipient. A handoff after the send starts cannot retract it; the next reconcile
resolves the new controller. Never guess a terminal/window.
Use `recipient: "{controller_session}"` to deliver the same pending question once
to each new controller; a fixed human recipient does not resend just for handoff.
Both command and recipient use these placeholders. Missing recipient is an empty string; formatting
uses the current owner read under the notification lock. A literal recipient stays unchanged after
takeover, while `{owner}` resolves to the new owner. The recipient is a deduplication identity, not an
implicit argument or transport: the command must still address its own destination. Commands run without
a shell, with closed stdin, captured output and a 30-second timeout. Errors only log to stderr, leaving
other reconciliation actions and its single JSON output intact. Each route has independent status
selection and retry: a command error or timeout does not prevent the remaining routes. Unknown placeholders,
malformed formatting and invalid format specs are configuration errors: disable just
that route, emit one warning per unchanged invalid configuration, and expose
`notify_errors` in JSON status and doctor reports, plus `notify_config` in non-pool doctor.
Pool admission remains based only on launch checks; notification diagnostics do not
prevent workers from starting. The route
recovers automatically when its configuration is fixed; failed formatting is never
retried as a delivery. Structural errors such as duplicate route names still reject
the entire configuration.

`$YARDGOAT_HOME/notified/<task_id>.json` retains its `sent` map. For a list, or a single object containing
`recipient`, keys are `v2:` followed by a compact JSON array of `[name, status, question_hash, recipient]`.
The hash uses the full question, not its 500-character command argument. JSON framing prevents ambiguous
keys when names or recipients contain delimiters. Successful records add name and formatted recipient;
the corresponding `notified` result includes them too. A route with no recipient records an empty
string, so owner takeover alone does not change its key.

**Notification compatibility:** a single object without `recipient` uses exactly the pre-M7
`status:question_hash` key and record/result shape, even if it has an optional name. Existing records
are used in place with no migration and do not resend. Opting into a list or adding recipient creates
a new key space and can deliver once again. A separate per-task lock serializes each route's read,
command and successful record update across reconcilers. Returning to an already delivered tuple does
not resend it. Only exit 0 records success, written atomically; successful routes are skipped while
failed routes retry on the next reconcile. Failed bookkeeping or a crash between external delivery
and its local record can redeliver, so hooks may also deduplicate on their side. This is not an
exactly-once external delivery guarantee.
