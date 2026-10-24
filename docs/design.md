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
    written is replayed by `reconcile`.
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

20. **Dependencies require verification.** `depends_on` lists accepted upstream task ids. Submission validates
    self-references, missing ids and cycles by traversing authoritative accepted specs, under the schedule lock.
    Dispatch creates one `waiting_deps` run if any upstream is not `verified`. Waiting counts only as in flight:
    it prevents duplicate dispatch and survives takeover, but uses no attempt, direct slot or pool station.
    `status` names the unverified upstreams; `cancel` can cancel the wait without spending an attempt.
    Reconcile moves the same run to `queued` once all upstreams are verified, then applies ordinary capacity,
    lane, drift and pool rules. Worker exit or a retryable verifier failure is insufficient. An `archived` or
    `exhausted` upstream instead parks the task with a `blocked_deps` run and an inbox question naming the
    terminated upstream. No worker or receipt is created for that run. Answering does not waive the dependency;
    an impossible dependency requires a replacement task spec under a new id. Auto-verification stays outside
    the schedule lock; downstream tasks observe its result on the next reconcile tick.
21. **Declared writes exclude overlapping work.** Before direct start, file-pool/plugin submit, and pool claim,
    the scheduler compares `workdir + scope` against every running task's write set, across all adapters and
    pools. The check, reservation and start share the machine schedule lock; claim follows schedule → pool →
    task lock order. Conflicts keep the same run queued, spend no attempt, and name the conflicting task and
    absolute path in `status` and reconcile output. Every worker outcome releases its reservation; reconcile
    then starts eligible queued work (or makes pool cards available for claim). An uncertain remote submit
    retains its reservation because it may already be executing. Scope waits alone do not spawn stations.

## Launch-chain health checks

`yardgoat doctor --pool P` or `yardgoat doctor --host H --lane L --acct A` runs a probe through the same
tmux/background launcher and daemon environment as real work. It reuses `runner.script_for` for direct work and
`pool.station_argv` plus the shared station script for pools, including sources, cwd, launcher placeholders and
`pass_env`. Only the final host command is replaced: the probe checks command lookup and `--version`; it never
sends a prompt to a model. The launcher must return 0 and actually execute the probe. For `host: shell`, the host
is `bash`, and the launcher is bypassed just as for real shell workers.

Pool probes also create and remove a file in a mailbox under the pool root. Probes use the next station's account,
including account rotation. `--json` returns each check and its reason; a failed check exits 1. Results with timestamps
live in `$YARDGOAT_HOME/doctor/<pool or host-lane>.json`; probe scripts and logs live under `doctor/runs/`. Pool health
is also stored in `pools/<pool>/state.json` and surfaced by `status` and `inbox`.

Before starting a station, `pool.schedule` requires a successful result for that account newer than local
`doctor_ttl_h` (default 12 hours). Missing, failed, malformed or expired records trigger a new probe. A failed probe
leaves cards queued and starts no station. Probes time out after 20 seconds; rerun doctor after changing local wiring
to refresh the cached result immediately.

The local `provider_refusal_patterns` list replaces the default case-insensitive regexes: `flagged for possible`,
`usage limit`, `You've reached your`, `rate limit exceeded`. Matching scans the last 20 KB of the log and records the
last matching line, even if the host exits 0. An explicit source/cwd launch failure retains its infrastructure
classification. A pool wall-time cutoff retains its timeout classification. Owners can explicitly dispatch again;
changing the frozen card requires a new task id.

## Card metadata

Specs may record `scope` (a list of paths/globs relative to workdir), `risk` (`l1` documentation, `l2` ordinary work,
the default, or `l3` migrations/releases/permissions/CI), and optional `acceptance` (a sentence describing success).
`quick --scope PATH --scope GLOB --risk l1 --acceptance TEXT` includes these in the spec and at the end of `CARD.md`.
M2 uses `scope` for scheduling; metadata does not grant permissions. Each literal scope path becomes a
canonical absolute prefix (`Path.resolve`, including existing symlinks). A glob reserves the directory before
its first wildcard component: `src/**/*.py` reserves `workdir/src`, and `*.md` reserves the entire workdir.
This is deliberately conservative and covers future files as well as existing matches; it does not enumerate
only files that currently exist. Prefix comparison respects path-component boundaries, so `src/a` and `src/b`
or `src` and `src-other` can run together. The reservation is saved on the running record until its outcome.
Omitted or empty `scope` opts out for backwards compatibility. `risk` and `acceptance` remain descriptive.

`depends_on: ["prepare-data", "build-tool"]` requires both upstream tasks to be verified before starting.
The accepted dependency graph is immutable just like the rest of the spec; changing dependencies needs a new id.

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

## Not yet

Issue-driven and queue-file adapters (beyond `file-pool`), an MCP entry point, cost/model-call budget
gates, leases for multi-host stores, and evaluation data. See the roadmap in the README.
