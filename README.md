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

## Sessions and project controllers

Session registration identifies a local agent process; project control is a separate
explicit grant. These commands do not change task ownership or task permissions.

```bash
yardgoat whoami --json
yardgoat session register --label "Planning session"
yardgoat session list --json
# Administrative inventory (requires project_admins in local config):
yardgoat session register --pid 12345 --as operator --reason "Session inventory"

yardgoat project add example --label "Example" --root /path/to/work --claim open
yardgoat project list --json
yardgoat project show example --json
yardgoat project claim example --expect-epoch 0
yardgoat project handoff example --to SESSION_ID --expect-epoch 1

# Old session begins; retain the printed transfer_id and one-time token:
yardgoat project transfer begin example --expect-epoch 2
# New registered session completes (or supply --to SESSION_ID):
yardgoat project transfer commit example --transfer TRANSFER_ID --token TOKEN
yardgoat project transfer abort example --transfer TRANSFER_ID --token TOKEN
# The current controller can also abort without a token.

# Administrator initialization or recovery; also cancels an unfinished transfer:
yardgoat project assign example --session SESSION_ID --expect-epoch 3 --as operator --reason "Recovery"
```

Optional local configuration:

```json
{
  "sessions": {"agents": [{"name": "custom", "exe": "custom-agent"}]},
  "project_admins": ["operator"],
  "reconcile_stale_min": 10
}
```

When supplied, `sessions.agents` completely replaces the default exact names
`claude`, `codex`, `kimi`. Each rule supports exact `exe`, `comm` or `argv0` names;
generic interpreters are not recognized by default. The closest matching agent is
the caller. Other users' ancestors are skipped using their public UID and parent
metadata; their environments are not read. Ambiguous or unreadable required
evidence (including same-user ancestry) is unknown (exit 3), and pool stations
cannot register. `--session ID` asserts the caller identity; it cannot
select somebody else's identity. `project assign --session` instead names its target.
Session lists distinguish `alive`, `dead` and `unknown`; a hidden or unreadable
process entry cannot by itself establish death.
Tmux endpoints and project roots are display/routing metadata, never permission.

`project_admins` is separate from l3 `approvers` and fails closed when absent.
Administrator `--as` is a cooperative identity, not OS-user authentication. Claims
work only for open projects at epoch 0; controller death requires admin recovery.
Epoch comparisons fence stale calls even after A→B→A. Transfer commit is replayable;
tokens are stored only as hashes. New tokens have a `yg-` prefix so they work as
separate `--token TOKEN` arguments. For an older token beginning with `-`, use
`--token="$TOKEN"` with commit or abort. If a begin committed but its token output was lost,
the current controller can abort and begin again, or an administrator can assign.
Details and library APIs: [design](docs/design.md#会话与项目主控-sessions-and-project-control).
Schemas: [session](src/yardgoat/schemas/session.schema.json),
[project](src/yardgoat/schemas/project.schema.json),
[control configuration](src/yardgoat/schemas/control-config.schema.json).

Each `reconcile` records start/end, last error-free completion and an error summary
in `reconcile-health.json`. JSON status includes `reconcile_health`; doctor without
`--pool` (for example, `yardgoat doctor --host shell --json`) alarms when a recorded
completion is older than `reconcile_stale_min` (default 10 minutes). Pool doctor
reports only the pool's launch chain. Both modes print the same report they save.
Caught station launch failures are included in the error summary and retain the
previous error-free completion time.
This record never grants or revokes project control.

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
excluding exactly the listed outputs. In either reviewed tree, comparisons ignore untracked
`__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.hypothesis/`, `.benchmarks/`
directories at any depth and ordinary `*.pyc` files. Files are still copied and are never
removed or rewritten by the ignore policy. Other persistent additions, deletions and content
changes fail; existing entries' mode/type changes and all symlink changes still fail.

Machine-local config may add `"review_ignore": [".host-state/", "logs/**/*.tmp"]`.
Globs are relative to the reviewed root; `*` stays within one component, `**` spans zero or
more components, and matching directories cover their subtrees. The rules and Git-tracked
paths are frozen when each review child is created. Editing config afterwards does not change
that child's checks or retries. Tracked paths and their parent directories remain protected
even if they match a rule; matching tracked paths generate a warning. Keep custom rules narrow:
broad patterns can conceal real changes in untracked files. Ignores do not permit extra files
outside `target/` in the review workdir. See [review integrity](docs/design.md#engineering-review-loop)
for the full policy.

To run tests, copy the snapshot to a temporary directory outside the review workdir first,
or at least set `PYTHONDONTWRITEBYTECODE=1` and add pytest `-p no:cacheprovider`. Leave no
test/build artifacts in the snapshot. An invalid or failed review
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
