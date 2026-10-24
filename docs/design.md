# Design (P0 + P1)

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
5. **Exited ≠ verified.** The runner only writes a receipt with `status: exited`. `close` runs the verifier: exit 0 → `passed`, exit 1 → `failed` (retry allowed within `budget.max_attempts`), anything else → `verifier_error` (a crashing verifier is not a failing deliverable).
6. **Bounded attempts.** When the budget is spent the task becomes `exhausted` and needs a human.

7. **Lane policy at the dispatch edge.** If a spec lists `allowed_lanes`, the executor lane must be in it; lanes in the local config's `forbidden_lanes` are refused on that machine regardless of the spec.
8. **Asking is not failing.** A worker that writes `out/BLOCKED.md` (or a pool card that lands in `parked/`) parks the task with that text as the question. `answer` records a reply; `reconcile` resumes the task on the owner's behalf, and the answer is appended to the prompt. Parked runs do not use the attempt budget.
9. **Infrastructure failures are not attempts.** When the local launcher exits with one of `launcher_failure_codes` before any agent started, the run is `launch_failed` and does not count.
10. **The worker sees the caller's environment.** Yardgoat's own import path is never passed to workers.
11. **Queue first, then start.** Every dispatch is accepted as a queued run. Direct runs start immediately only while
    fewer than `max_concurrent` direct runs are alive (local config); otherwise they wait and `reconcile` starts them
    oldest first, re-checking drift and lane policy at launch. A queued run counts as in flight (no duplicates) but
    not as an attempt; `cancel` removes it. Adapter runs are capped by the fleet they go to.

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
