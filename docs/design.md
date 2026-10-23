# Design (P0)

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

## Handover

A controller that disappears mid-run loses nothing: the run keeps going, and a new controller needs only
the task id. `yardgoat status <id>` shows owner, runs, liveness and receipts; `takeover` moves
ownership; `close` verifies. The old owner can no longer dispatch or close.

## Not in P0

Adapters that submit into existing queues (file pools, issue-driven dispatchers, queue files), host entry
points (skill / MCP), policy and budget gates beyond attempt counts, leases for multi-host stores, and
evaluation data. See the roadmap in the README.
