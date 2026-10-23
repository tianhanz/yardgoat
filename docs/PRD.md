# Yardgoat — product requirements (v0.1)

## One sentence

Say "dispatch this" once — from a person or from any coding agent — and Yardgoat hands the work to a fleet or agent CLI you already run, follows it until a frozen verifier passes, and returns a receipt. If a controller stops or changes hands midway, nothing is lost and nothing runs twice.

## Problems it solves

1. **Every fleet dispatches differently** (a file queue, an issue tracker plus a cron dispatcher, a queue file plus a launch script, a directory of prompt cards). Agents cannot hand work to each other without learning each one.
2. **"Done" means four things** — process exited, CI green, change merged, someone read the logs. Most often it only means the process exited.
3. **Typing into agent terminals is unreliable.** Instructions get stuck in input boxes; a quit command gets appended to someone else's half-written message.
4. **When a controller session stops, its work has no owner.** Hand-off notes get written, but control is never actually transferred.
5. **Routine supervision burns the most expensive subscription** because the controller is the only thing that knows the state.
6. **Nobody records which model did what**, how many retries it took, or how often a human stepped in — so model assignment is guesswork.
7. **Knowing what is running, what landed, what is stuck and what needs a decision** means opening every terminal.

## Users

| User | Needs |
|---|---|
| The person running the fleets | State a goal once; see only deliverables, blockers, and decisions that are actually theirs |
| Controller agents (Claude Code, Codex, Kimi, … sessions) | Dispatch sub-tasks, check progress, collect results without knowing each fleet's mechanics |
| A controller taking over | Continue from the task id alone: what is running, where results are, without dispatching again |
| Dashboards | One trustworthy, read-only state to display; missing data shown as missing, never as zero |

## Core scenarios

1. **Dispatch** — a task card (goal, inputs, outputs, verifier, allowed lanes, budget, in-card authority) is submitted and started on an allowed lane.
2. **Verify** — exit is not success; only the frozen verifier passing is. Failures retry within budget, then stop and ask.
3. **Change controllers** — the run continues; the new owner takes over and verifies; the old owner can no longer act.
4. **Get stuck** — missing authority or input parks the task with one concrete question; an answer resumes it automatically.
5. **See results** — one place answers running / delivered / stuck / waiting on whom, each with evidence and a timestamp.
6. **Keep the books** — per task: actual model, lane, account alias, time, cost, rework count, human interventions.
7. **Use existing fleets** — submit into the fleet's own queue; never add a competing one.

## Non-goals

- A new agent runtime. Workers stay the vendors' own CLIs.
- A new authoritative queue. Existing queues remain the source of truth.
- Research or product judgment. Yardgoat routes decisions to a human; it does not make them.
- Typing into worker terminals. Workers change state only when they start and when they exit.
- Automatic model routing or leaderboards before there is per-task evidence (fewer than 5 samples shows "insufficient data").
- A hosted service. It runs next to the fleets; accepted work keeps moving when dashboards are offline.

## Requirements

| Phase | Requirement | Status |
|---|---|---|
| P0 | Task contract; idempotent submit; one owner; no duplicate dispatch; frozen verifier; bounded attempts; receipts and events; direct runner (codex / claude / kimi / shell) | done |
| P0 | Hand-over after the run ended, using only the task id | verified on a real task |
| P0 | Hand-over **while the run is in flight** | verification in progress |
| P1 | Lane allowlist per task and forbidden lanes per machine, enforced at dispatch | done |
| P1 | Park with a question (worker writes `out/BLOCKED.md`); answer; automatic resume with the answer in the prompt; parked runs don't use the budget | done |
| P1 | `file-pool` adapter for file-queue fleets; plugin adapters for private systems | done |
| P1 | Reconciler (cron): poll adapters, mark vanished runs lost, resume answered tasks | done |
| P1 | Launch failures (launcher refused before any agent started) don't count as attempts | done |
| P1 | "Dispatch" skill so an agent turns a request into a task card and calls the CLI | in progress |
| P2 | Issue-driven and queue-file adapters; MCP entry point; cost / model-call budget gates; dashboard projection | planned |
| P3 | Per-task evaluation facts and grouped summaries with sample sizes; routing suggestions only | planned |

## Success measures

| Measure | Target |
|---|---|
| Tasks with a receipt and a verifier run | 100% |
| Duplicate dispatches | 0 (contract + reconciler) |
| Work lost on controller change, including mid-run | 0 |
| Typing paths into worker terminals | 0 |
| Human interventions per task | only the decisions that are genuinely the human's |

## Design constraints

One owner per task. State lives next to the fleets, not in a synced folder. Programs execute, models judge, humans decide what is out of policy. In-card authority covers only the actions, lanes and budget written on that card. Exit is not success. Adapt existing fleets; do not replace them.
