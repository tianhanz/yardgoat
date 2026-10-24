# Yardgoat

*The yard engine for heterogeneous coding agents.*
车场山羊：把异构 coding agent 编组、发车、签回执。

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

`auto_close` defaults to `true`: after a run exits, `reconcile` runs its frozen verifier on behalf of
the current owner, including the normal review, `on_pass` and dependency gates. The owner need not have
an active session. A nonzero worker exit still goes to the verifier; a failed verification does not
add automatic retries to ordinary tasks. Set `"auto_close": false` for manual `close`. Configured
`review` retains its existing automatic verification policy, including execution and repairs.
`quick --no-auto-close` selects manual close; `quick --auto-close` remains a supported spelling of
the default. These two switches are mutually exclusive.

**Upgrade:** the default also applies to already accepted tasks that omit `auto_close`. Existing
exited runs will advance on the next reconcile, potentially running their frozen `on_pass` commands.
Accepted specs, hashes and frozen files are not rewritten. See the
[upgrade behavior](docs/design.md#automatic-verification-and-upgrades) before enabling reconciliation.

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
If lanes are omitted, different hosts are required. Submission freezes the review pool's
effective `host` and `lane` in `review.channel`. Each execution run records its actual launch
channel; review creation and claim compare that run with the frozen channel and the review
station's recorded launch channel. Configuration drift parks the parent for attention;
restore the configuration, then answer to retry. An incompatible station (missing identity,
wrong channel or obsolete writable root) receives `EMPTY` and is marked `restart_required`.
Its cards stay queued for a healthy or newly started station, without a parent answer.
Such a station is not counted as available; its live process still counts against capacity
until it exits. Existing stations without launch identity must be restarted for reviewed tasks.
`path` is a directory relative to workdir (default `.`); the review workspace and Yardgoat
store must be outside that directory. The directory can be created by the initial worker.
Submission also rejects review workdirs or write sets overlapping any parent scope prefix,
including glob prefixes and symlink targets; creation rechecks the retained write set.
When the pool omits `root`, reviews and stations both use `$YARDGOAT_HOME/pools/<pool>/stations`.
Doctor checks that the `.yardgoat-reviews` directory is writable through the launch chain.

For tasks accepted by older versions without `review.channel`, the current owner must explicitly
freeze the review pool's **current** channel. This retains the task ID, dependencies and rounds:

```bash
yardgoat confirm-review-channel task-id --as owner
# If an execution has already started but its historical channel is missing:
yardgoat confirm-review-channel task-id --as owner --run-channel r1 codex execution-lane
```

Repeat `--run-channel RUN HOST LANE` for each missing execution identity; use `''` for an empty
lane. The owner must confirm the actual historical identity, not infer it from current config.
If it is unknown, investigate before confirming. Recorded identities cannot be overwritten.
This works for accepted, running and reviewing tasks, including ones parked during upgrade.
Confirmation records who, when, the fixed review channel and confirmed run identities in the
authoritative record and events, without rewriting the accepted spec or hash. Repeating it
replays that same confirmation, never refreezes changed config. It does not dispatch, answer
or spend a round: use normal dispatch/reconcile afterwards, or answer first if already parked.

Reviewers receive a snapshot, the execution run's actual prompt, all recorded answers and
previous rounds' findings, and must confirm whether those findings were fixed.
They write `out/findings.json` and `out/REVIEW.md`; `out/BLOCKED.md` is allowed for a concrete
question. These are the only writable review files. Findings use
`id`, `severity`, `file`, `line`, `title`, `scenario`, `fix`; file paths are relative to
the reviewed directory, and line numbers start at 1. See the [findings schema](src/yardgoat/schemas/findings.schema.json).

`review` enables automatic verification through `reconcile`, including repair runs.
Each `<task-id>-review-<round>` child has its own frozen verifier checking output structure,
file/line references, unchanged original/snapshot trees, and a frozen workspace baseline
excluding exactly the listed outputs. Other persistent additions, deletions, content/mode
changes or symlinks fail verification. An invalid or failed review
never means clean: it retries within the child's two-attempt budget, then needs attention.
Frozen-file rejection, verifier errors, exhausted/archived/cancelled children and launch
refusals park the parent with a reason. Restoring a frozen verifier and answering replays
the same pending review; it does not spend another review round.
Blocking findings become an answer appended to the original prompt, and a repair is dispatched.
Successful review/repair rounds do not spend the original failure retry budget.

The parent stays `reviewing` while waiting for review. Its write-set lease remains held through
review and repairs; dependents wait and `on_pass` runs only after the full loop passes.
At the round limit, `park` asks for a follow-up decision; `pass_with_followups` verifies the
parent and submits one task per remaining finding. Those follow-ups appear in `inbox`
without runs until their owner chooses to dispatch them. They inherit the original risk
and require their own approval when l3. A parked round-limit result cannot acquire more
rounds by answering; submit a new task for further work.
`answer` refuses completed round budgets and keeps the task visible in `inbox`.
`status` shows the retained review write set until verification or archive releases it.

Take over the parent to transfer its review children too. Repeated close/reconcile does not
duplicate children, answers or repair rounds. Receipts include round numbers, each round's
finding count and the validated findings used to decide the result.
If follow-up submission or result recording fails, the round remains pending and its write
set remains reserved. Answering retries that result, reusing any follow-ups already accepted.
The result is marked processed only after all follow-ups, its receipt and its event succeed.
Pending rounds journal follow-up IDs before creation. Parent takeover and result replay also
transfer those follow-ups and record their ownership history. A follow-up taken over separately
is not overwritten: the parent parks with the task and owner to coordinate. Explicitly align
that follow-up's owner with the parent, then answer the parent to retry. After the round is
processed, its follow-ups remain independent tasks.

`inbox` reports a removed pool only when an unfinished task still refers to it. Historical
verified, archived and exhausted tasks do not trigger that pool warning; `status <task-id>`
continues to show it for an individual historical task.

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

For independent routes, `notify` also accepts a list:

```json
{"notify": [
  {"name": "owner", "recipient": "{owner}", "command": ["/path/to/owner-hook", "{owner}", "{task_id}", "{question}"]},
  {"name": "operations", "command": ["/path/to/operations-hook", "{task_id}", "{status}"]}
]}
```

Each route has its own optional `on` list. `name` defaults to `"default"`; names must be unique within
the list, including omitted names. Invalid configuration rejects the entire list and logs a warning.
`recipient` is a string template using the same placeholders as `command`; it identifies the destination
for deduplication, while the command remains responsible for delivery. Missing `recipient` means an
empty destination. Success is remembered by route name, status, full question hash and formatted
recipient. After takeover, a route using `{owner}` sends the pending decision to the new owner; a
route without a recipient does not resend. A failed or timed-out route does not prevent other routes
from running; only that route retries on the next reconcile.

A single-object configuration without `recipient` keeps the exact legacy key, even if it has a `name`.
Existing successful reminders do not resend on upgrade. Switching it to a list opts into route keys
and can send once under the new key. New-route `notified` entries include `name` and formatted
`recipient`; legacy entries keep their original shape. Records remain under
`$YARDGOAT_HOME/notified/<task_id>.json`.

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
