# Yardgoat

*The yard engine for heterogeneous coding agents.*
调车机：把异构 coding agent 编组、发车、签回执。

> **Status: pre-alpha (P1).** Task contract, CLI, direct runner, a file-queue adapter, park/answer/resume and a reconciler exist and are covered by contract tests. Issue-driven adapters, MCP and budget gates are next.

A **yard goat** is the small locomotive that assembles trains in a rail yard: it doesn't haul the long-distance freight, it sorts the cars and sends each one down the right track. Yardgoat does that for coding agents — you describe a task once, it starts the agent CLI you choose (Codex, Claude Code, Kimi, or a shell command), and it tracks the run until a frozen verifier says it is actually done.

## Three guarantees

1. **One owner per task.** Only the current owner can dispatch or verify; ownership moves explicitly and survives a controller going away mid-run.
2. **No duplicate dispatch.** Re-dispatching a running task returns the existing run; an exited run must be verified before another attempt.
3. **A receipt, not a vibe.** *Process exited* and *verified* are different states. A task passes only when its verifier — frozen by hash at submit time — exits 0.

## Quick start

```bash
pip install git+https://github.com/tianhanz/yardgoat     # or: uv tool install git+…

yardgoat submit examples/reconcile.json --as controller-a   # accept the task (idempotent)
yardgoat dispatch example-reconcile-01 --as controller-a    # start run r1
yardgoat status example-reconcile-01                        # owner, runs, liveness, receipts

# controller-a goes away; controller-b only needs the task id
yardgoat takeover example-reconcile-01 --as controller-b --from controller-a
yardgoat close example-reconcile-01 --as controller-b       # runs the frozen verifier
```

When a worker needs a human, it writes `out/BLOCKED.md`; the task parks with that text as its question:

```bash
yardgoat inbox                                              # what is waiting for a human
yardgoat answer example-reconcile-01 --as me --text "use the 2024 records"
yardgoat reconcile                                          # (cron) resumes answered tasks, polls adapters
```

For one-off work there is a one-liner that writes the card, a frozen verifier and the spec for you:

```bash
yardgoat quick "Write the 200 smallest primes to out/primes.txt" --verify "test $(wc -l < out/primes.txt) -eq 200" --as me
yardgoat quick "Fix the flaky test in tests/test_io.py" --verify "pytest -q tests/test_io.py" --pool research --as me
yardgoat quick "Update the guide" --verify "test -s docs/guide.md" --scope docs/guide.md --risk l1 --acceptance "Guide describes the new command" --as me
```

Pools reuse a warm agent session across cards — every card is queued first, and a station claims one after another:

```json
"pools": {"codex": {"host": "codex", "lane": "codex", "root": "~/work/yardgoat-tasks", "max_stations": 3, "max_tasks_per_station": 5}}
```

```bash
yardgoat quick "Summarise CHANGELOG.md into out/summary.md" --verify "test -s out/summary.md" --pool codex --as me
```

A task spec names the prompt file (`card`), the working directory, the expected `outputs`, the `executor`
(`host`, plus optional `lane`/`acct` that your local launcher understands), the `verifier`, and a `budget`.
Schemas: [task](src/yardgoat/schemas/task.schema.json) · [receipt](src/yardgoat/schemas/receipt.schema.json) · [event](src/yardgoat/schemas/event.schema.json).
Invariants and store layout: [docs/design.md](docs/design.md) · What it is for: [docs/PRD.md](docs/PRD.md).

## Approval and delivery

Tasks with `"risk": "l3"` need explicit approval before dispatch. They appear in `inbox` as `等审批`;
dispatch without approval exits 3 with `需要 approve`.

```bash
yardgoat approve release-example --as reviewer --note "Reviewed the frozen card and verifier"
yardgoat dispatch release-example --as controller
```

Optional local config `"approvers": ["reviewer"]` restricts who may approve (an empty list allows nobody).
Without that setting, any nonempty identity may approve. Approval is bound to the frozen content hash;
drift blocks execution and revokes approval when a mutation command checks it. Restoring the original
files then requires a fresh approval. Changed content must be submitted under a new task id.
`answer` supplies information only; `approve` leaves parked questions and their state unchanged.
The approval identity, time and note are saved in task state, events and subsequent run receipts.

A spec can declare ordered commands to execute as the owner after the frozen verifier returns PASS:

```json
"on_pass": [
  {"name": "stage", "command": "mkdir -p delivered && cp out/result.txt delivered/result.txt"},
  {"name": "check", "command": "test -s delivered/result.txt"}
],
"on_pass_timeout": 600
```

Commands use a shell and default to the task workdir. An optional step `cwd` is resolved relative to the
spec directory. The per-step timeout comes from spec `on_pass_timeout`, then the same local config
setting, then 600 seconds. Commands and their working directories are frozen as part of the task.
`auto_close` executes the same steps on behalf of the current owner.

The first nonzero step stops delivery, leaves the task `verified` with `delivery_failed: true`, and adds
`收尾第 N 步 <name> 失败` to `inbox`. Its exit code and output tail are in the receipt and events.
After resolving the failure, run `yardgoat deliver release-example --as controller` to retry that step
and continue. Successful steps are preserved across retries and repeated `close`/`deliver` calls.
Repeated `close` reports the existing result; retry delivery explicitly. Commands should also be
idempotent across a controller crash between an external side effect and recording its success.

## Automatic engineering review

Add `review` to a task spec to run an independent review after its frozen verifier passes:

```json
"review": {
  "pool": "reviewers",
  "path": "src",
  "rounds": 3,
  "blocking": ["blocker", "major"],
  "after_max_rounds": "pass_with_followups"
}
```

Configure the review pool with a different pool name and effective lane from the executor.
If lanes are omitted, different hosts are required. This is checked at submission and launch.
`path` is a directory relative to workdir (default `.`); the review workspace and Yardgoat
store must be outside that directory. The directory can be created by the initial worker.
Reviewers receive a snapshot under their pool root, the original goal/card and acceptance
instructions. They write `out/findings.json` and `out/REVIEW.md`. Findings use
`id`, `severity`, `file`, `line`, `title`, `scenario`, `fix`; file paths are relative to
the reviewed directory, and line numbers start at 1. See the [findings schema](src/yardgoat/schemas/findings.schema.json).

`review` enables automatic verification through `reconcile`, including repair runs.
Each `<task-id>-review-<round>` child has its own frozen verifier checking output structure,
file/line references and unchanged original/snapshot trees. An invalid or failed review
never means clean: it retries within the child's two-attempt budget, then needs attention.
Blocking findings become an answer appended to the original prompt, and a repair is dispatched.
Successful review/repair rounds do not spend the original failure retry budget.

The parent stays `reviewing` while waiting for review. Its write-set lease remains held through
review and repairs; dependents wait and `on_pass` runs only after the full loop passes.
At the round limit, `park` asks for a follow-up decision; `pass_with_followups` verifies the
parent and submits one task per remaining finding. Those follow-ups appear in `inbox`
without runs until their owner chooses to dispatch them. They inherit the original risk
and require their own approval when l3. A parked round-limit result cannot acquire more
rounds by answering; submit a new task for further work.

Take over the parent to transfer its review children too. Repeated close/reconcile does not
duplicate children, answers or repair rounds. Receipts include round numbers, each round's
finding count and the validated findings used to decide the result.

## Local wiring

How a lane or account is chosen is your machine's business, not the task's. Put it in `~/.config/yardgoat/config.json`:

```json
{
  "launcher": ["my-lane-wrapper", "--host", "{host}", "--lane", "{lane}", "--acct", "{acct}", "--"],
  "source": ["~/.config/my-proxy-env.sh"],
  "tmux_session": "agents",
  "host_args": {"claude": ["--permission-mode", "acceptEdits"]}
}
```

With `tmux_session` set, each run gets its own window (handy for watching); otherwise runs are detached background processes.

## Reports and pending reminders

```bash
yardgoat report --since 2026-10-01 --by pool
yardgoat report --by model --json
```

Reports group tasks by `pool` (default), `lane`, `model`, `account`, `project`, or `host`, sorted by name.
They show first-attempt PASS rate, attempts, recorded run wall time, human interventions, abnormal
outcomes, and explicit `usd=` / `tokens=` receipt totals. Unknown costs stay unknown; nothing is estimated.
Groups with fewer than five tasks show only their count and `数据不足` (insufficient data); JSON sets
`insufficient: true` and metric fields to `null`. `--since` includes tasks accepted on or after that date.
See [accounting definitions](docs/design.md#reports-and-pending-reminders) for denominators and attribution.

An optional machine-local `notify` setting runs your own command for pending decisions:

```json
{"notify": {"command": ["/path/to/local-hook", "{task_id}", "{status}", "{owner}", "{question}", "{hint}"]}}
```

`reconcile` invokes the argv directly, with a 30-second timeout. The default `on` statuses are `parked`,
`exhausted`, `submit_unknown`, `verify_error`, and `provider_refused`; set `notify.on` to a list to override
them. The question argument is limited to 500 characters. Successful reminders are remembered across
reconciles; a new status or full question gets a new reminder. Failures log to stderr and retry next time.
Without `notify`, no hook runs. Reconcile still emits one JSON document, with a `notified` array.

Check the actual launch chain without calling a model:

```bash
yardgoat doctor --pool codex --json
yardgoat doctor --host codex --lane fast --acct primary
```

Pools check automatically before starting stations, caching successful checks for `doctor_ttl_h` (default 12 hours).
Failures leave cards queued and show their reasons in `status` and `inbox`. Model-provider refusals are recorded
separately as `provider_refused`, with the matching log line, and do not spend an attempt. Local
`provider_refusal_patterns` can override the default case-insensitive refusal/quota patterns; these tasks wait in
the inbox for an explicit resend after rewording or changing the channel.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| P0 | contract, file store, CLI, direct runner, contract tests | done |
| P1 | lane allowlist, park/answer/resume, `file-pool` adapter, plugin adapters, reconciler, launch-failure accounting, `quick`, queue-first dispatch with a concurrency cap, pools with pulling stations | **here** |
| P2 | issue-driven and queue-file adapters, agent-host skill and MCP entry points, cost budget gates, dashboard projection | planned |
| P3 | per-task evaluation data (actual model, rework, human interventions) — facts first, no rankings | planned |

## Development

```bash
pip install -e '.[test]'
pytest -q tests/contract      # uses a fake shell worker; never calls a model
```

## License

Apache-2.0. See [LICENSE](LICENSE).
