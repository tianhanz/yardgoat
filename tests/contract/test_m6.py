"""M6: factual accounting and optional pending-task hooks; only local fake commands."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401


def put_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def seed(env, tid, outcomes=(), costs=(), status=None, question="Which input?", **metadata):
    """Write a historical task, without launching an agent or consulting live configuration."""
    td = Path(env["YARDGOAT_HOME"]) / "tasks" / tid
    runs = []
    for i, outcome in enumerate(outcomes, 1):
        run = {"run_id": f"r{i}", "state": outcome, "station": "arbitrary-worker",
               "started_at": "2026-10-20T00:00:00+0000", "finished_at": "2026-10-20T00:00:10+0000"}
        runs.append(run)
        receipt = {"run_id": run["run_id"], "task_id": tid, "attempt": i,
                   "status": {"verified": "passed", "verify_error": "verifier_error"}.get(outcome, outcome),
                   "finished_at": run["finished_at"], "cost_basis": costs[i - 1] if i <= len(costs) else "unknown",
                   "actual_model": "observed-model", "account_alias": "observed-account",
                   "lane": "observed-lane", "host": "observed-host", **metadata}
        if outcome in ("verified", "failed", "verify_error"):
            receipt["verifier"] = {"result": {"verified": "PASS", "failed": "FAIL", "verify_error": "ERROR"}[outcome]}
        put_json(td / "runs" / run["run_id"] / "receipt.json", receipt)
    state = {"task_id": tid, "owner": "owner", "status": status or (outcomes[-1] if outcomes else "accepted"),
             "accepted_at": "2026-10-20T01:00:00+0000", "runs": runs, "question": {"text": question}}
    put_json(td / "task.json", state)
    put_json(td / "spec.json", {"task_id": tid, "project": "demo", "adapter": {"kind": "pool", "pool": "sample-pool"},
                                "executor": {"host": "requested-host", "lane": "requested-lane", "acct": "requested-account"}})
    return td


def change(td, **changes):
    st = json.loads((td / "task.json").read_text())
    st.update(changes)
    put_json(td / "task.json", st)


def events(td, rows):
    (td / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def report(env, *args):
    rc, out, err = yg(env, "report", *args, "--json")
    assert rc == 0, err
    return json.loads(out)


def test_report_pass_attempts_wall_humans_cost_and_abnormal_outcomes(env):
    a = seed(env, "t-first", ["verified"], ["usd=0.10 tokens=100"])
    seed(env, "t-second", ["failed", "verified"], ["usd=0.20", "tokens=2,000"])
    seed(env, "t-exhausted", ["failed", "failed"], ["unknown", "usd=0.30 tokens=50"], status="exhausted")
    seed(env, "t-error", ["verify_error"], ["shared-station"], status="exited")
    seed(env, "t-launch", ["launch_failed", "verified"], ["usd=0", "usd=0.40"])
    rows = [{"kind": kind, "actor": actor} for kind, actor in [
        ("task.answered", "person"), ("task.owner_changed", "person"),
        ("task.archived", "person"), ("run.cancelled", "person"),
        ("task.answered", "runner"), ("task.owner_changed", "reconciler"),
        ("task.archived", "arbitrary-worker"), ("run.cancelled", "sample-st-99"),
        ("task.answered", "recorded-worker"), ("task.accepted", "person")]]
    # A station whose id does not follow the default naming convention still is not human.
    put_json(Path(env["YARDGOAT_HOME"]) / "pools/sample-pool/stations/recorded-worker.json",
             {"station": "recorded-worker"})
    events(a, rows)
    g = report(env)["groups"][0]
    assert g["count"] == 5 and g["insufficient"] is False
    assert g["first_pass_count"] == 2 and g["first_pass_rate"] == pytest.approx(2 / 5)
    assert g["avg_attempts"] == pytest.approx(7 / 5)  # launch failures consume no attempt
    assert g["avg_wall_seconds"] == 10
    assert g["human_interventions"] == {"total": 4, "answer": 1, "takeover": 1, "archive": 1, "cancel": 1, "approve": 0}
    assert g["abnormal"] == {"lost": 0, "launch_failed": 1, "submit_unknown": 0,
                             "verify_error": 1, "provider_refused": 0, "other": 0}
    assert g["cost"] == {"usd": 1.0, "tokens": 2150, "unknown_tasks": 2}


@pytest.mark.parametrize("by,expected", [("pool", "sample-pool"), ("lane", "observed-lane"),
    ("model", "observed-model"), ("account", "observed-account"), ("project", "demo"), ("host", "observed-host")])
def test_report_by_each_dimension(env, by, expected):
    seed(env, "t-dimension", ["verified"], ["usd=0"])
    data = report(env, "--by", by)
    assert data["by"] == by and [g["name"] for g in data["groups"]] == [expected]


def test_report_small_samples_are_suppressed_and_names_sorted(env):
    seed(env, "t-z", ["verified"], ["usd=99"], actual_model="z-model")
    seed(env, "t-a", ["failed"], ["usd=20"], actual_model="a-model")
    data = report(env, "--by", "model")
    assert [g["name"] for g in data["groups"]] == ["a-model", "z-model"]
    for g in data["groups"]:
        assert g["count"] == 1 and g["insufficient"] is True
        for key in ("first_pass_count", "first_pass_rate", "avg_attempts", "avg_wall_seconds",
                    "human_interventions", "abnormal", "cost"):
            assert g[key] is None
    rc, out, err = yg(env, "report", "--by", "model")
    assert rc == 0, err
    assert "数据不足" in out and "|" in out and "%" not in out
    assert out.index("a-model") < out.index("z-model")


def test_report_since_filters_acceptance_date_inclusively_and_empty_store(env):
    assert report(env)["groups"] == []
    older = seed(env, "t-old", ["verified"])
    change(older, accepted_at="2026-10-19T23:59:59+0000")
    seed(env, "t-included", ["verified"])
    assert report(env, "--since", "2026-10-20")["groups"][0]["count"] == 1
    assert report(env, "--since", "2026-10-21")["groups"] == []
    rc, out, err = yg(env, "report", "--since", "2026-02-30")
    assert rc == 2 and "YYYY-MM-DD" in err and not out


def test_report_history_deduplication_unknown_states_and_unparseable_costs(env):
    a = seed(env, "t-history", ["verified"], ["usd=0.25 tokens=bad"])
    events(a, [{"kind": "run.verified", "actor": "owner", "run_id": "r1", "verdict": "ERROR"},
               {"kind": "run.verified", "actor": "owner", "run_id": "r1", "verdict": "PASS"}])
    b = seed(env, "t-lost", ["lost"], ["usd=oops"])
    events(b, [{"kind": "run.lost", "actor": "runner", "run_id": "r1"}] * 2)
    seed(env, "t-unknown", ["submit_unknown"], ["tokens=1.5"])
    seed(env, "t-refused", ["provider_refused"], ["usd=NaN"])
    seed(env, "t-future", ["future_outcome"], ["usd=2junk"])
    g = report(env)["groups"][0]
    assert g["first_pass_rate"] == 0  # the first verification was ERROR, even though the receipt was replaced
    assert g["abnormal"] == {"lost": 1, "launch_failed": 0, "submit_unknown": 1,
                             "verify_error": 1, "provider_refused": 1, "other": 1}
    assert g["cost"] == {"usd": 0.25, "tokens": 0, "unknown_tasks": 5}


def test_report_wall_ignores_queue_time_and_missing_or_negative_intervals(env):
    for i in range(5):
        td = seed(env, f"t-time-{i}", ["verified"], ["tokens=0"])
        st = json.loads((td / "task.json").read_text())
        st["runs"][0]["queued_at"] = "2026-10-19T00:00:00Z"
        st["runs"][0]["started_at"] = ["2026-10-20T08:00:00+0800", "bad", None,
                                            "2026-10-20T00:00:20Z", "2026-10-20T00:00:00Z"][i]
        put_json(td / "task.json", st)
    g = report(env)["groups"][0]
    assert g["avg_wall_seconds"] == 10


def fake_hook(tmp_path, env, **settings):
    sink = tmp_path / "notifications.jsonl"
    fail = tmp_path / "fail-hook"
    script = tmp_path / "fake_notify.py"
    script.write_text("import json, pathlib, sys\n"
                      "print('hook stdout must not escape')\n"
                      "print('hook stderr', file=sys.stderr)\n"
                      "if pathlib.Path(sys.argv[2]).exists(): sys.exit(7)\n"
                      "with open(sys.argv[1], 'a') as f: f.write(json.dumps(sys.argv[3:]) + '\\n')\n")
    notify = {"command": [sys.executable, str(script), str(sink), str(fail),
                          "{task_id}", "{status}", "{owner}", "{question}", "{hint}"], **settings}
    put_json(Path(env["YARDGOAT_CONFIG"]), {"notify": notify})
    return sink, fail


def reconciled(env):
    rc, out, err = yg(env, "reconcile")
    assert rc == 0, err
    return json.loads(out)  # stdout must be exactly one JSON document, including with a noisy hook


def messages(sink):
    return [json.loads(line) for line in sink.read_text().splitlines()] if sink.exists() else []


def test_notify_deduplicates_full_question_and_status_and_persists(env, tmp_path):
    # Shell metacharacters and braces stay literal; changes beyond character 500 still matter for dedupe.
    question = "$(touch SHOULD_NOT_EXIST) {owner} `echo x`\n" + "问" * 520
    td = seed(env, "t-pending", status="parked", question=question)
    sink, _ = fake_hook(tmp_path, env)
    assert reconciled(env)["notified"] == [{"task_id": "t-pending", "status": "parked"}]
    assert reconciled(env)["notified"] == []
    sent = messages(sink)
    assert len(sent) == 1 and sent[0][:4] == ["t-pending", "parked", "owner", question[:500]]
    assert sent[0][4].startswith("yardgoat answer t-pending --as ")
    assert (Path(env["YARDGOAT_HOME"]) / "notified/t-pending.json").exists()
    assert not (Path.cwd() / "SHOULD_NOT_EXIST").exists()
    change(td, question={"text": question + "new"})
    reconciled(env)
    change(td, status="exhausted")
    reconciled(env)
    change(td, status="parked", question={"text": question})  # an already delivered tuple stays delivered
    reconciled(env)
    assert len(messages(sink)) == 3


def test_notify_failure_is_logged_and_retried_next_reconcile(env, tmp_path):
    seed(env, "t-retry", status="parked")
    sink, fail = fake_hook(tmp_path, env)
    fail.touch()
    rc, out, err = yg(env, "reconcile")
    assert rc == 0 and json.loads(out)["notified"] == []
    assert "notify" in err.lower() and not messages(sink)
    assert not (Path(env["YARDGOAT_HOME"]) / "notified/t-retry.json").exists()
    fail.unlink()
    assert reconciled(env)["notified"] == [{"task_id": "t-retry", "status": "parked"}]
    reconciled(env)
    assert len(messages(sink)) == 1


def test_notify_unconfigured_is_inert_and_custom_on_is_respected(env, tmp_path):
    seed(env, "t-parked", status="parked")
    seed(env, "t-exhausted", status="exhausted")
    assert reconciled(env).get("notified", []) == []
    assert not (Path(env["YARDGOAT_HOME"]) / "notified").exists()
    sink, _ = fake_hook(tmp_path, env, on=["exhausted"])
    assert reconciled(env)["notified"] == [{"task_id": "t-exhausted", "status": "exhausted"}]
    assert len(messages(sink)) == 1


def test_notify_default_states_legacy_verify_error_and_terminal_tasks(env, tmp_path):
    for status in ("parked", "exhausted", "submit_unknown", "verify_error", "provider_refused"):
        seed(env, "t-" + status, status=status)
    seed(env, "t-legacy", ["verify_error"], status="exited")
    seed(env, "t-archived", ["verify_error"], status="archived")
    seed(env, "t-verified", ["verify_error"], status="verified")
    sink, _ = fake_hook(tmp_path, env)
    data = reconciled(env)
    assert len(data["notified"]) == 6
    sent = messages(sink)
    assert next(m for m in sent if m[0] == "t-legacy")[1] == "verify_error"
    assert next(m for m in sent if m[0] == "t-legacy")[4].startswith("yardgoat close ")


def test_notify_concurrent_reconcile_delivers_once(env, tmp_path):
    seed(env, "t-concurrent", status="parked")
    sink, _ = fake_hook(tmp_path, env)
    ps = [subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "reconcile"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    data = []
    for p in ps:
        out, err = p.communicate(timeout=15)
        assert p.returncode == 0, err
        data.append(json.loads(out))
    assert len(messages(sink)) == 1
    assert sum(len(d["notified"]) for d in data) == 1


@pytest.mark.parametrize("notify", [{"command": "not-an-argv"}, {"command": []},
    {"command": ["/no/such/local-hook"]}, {"command": ["{unsupported}"]},
    {"command": ["unused"], "on": "parked"}])
def test_notify_bad_config_or_missing_command_never_breaks_reconcile(env, tmp_path, notify):
    seed(env, "t-bad-hook", status="parked")
    put_json(Path(env["YARDGOAT_CONFIG"]), {"notify": notify})
    rc, out, err = yg(env, "reconcile")
    assert rc == 0 and json.loads(out)["notified"] == [] and "notify" in err.lower()


def test_notify_timeout_is_30_seconds_and_runs_outside_schedule_lock(env, tmp_path, monkeypatch):
    import fcntl
    sys.path.insert(0, SRC)
    from yardgoat import cli, notify
    for key in ("YARDGOAT_HOME", "YARDGOAT_CONFIG"):
        monkeypatch.setenv(key, env[key])
    seed(env, "t-timeout", status="parked")
    fake_hook(tmp_path, env)
    calls = []

    real_popen = notify.subprocess.Popen

    class TimingOut(real_popen):
        """Behavioural check: 30-second timeout, no shell, schedule lock free while the hook runs."""
        def __init__(self, argv, **kwargs):
            assert kwargs.get("shell", False) is False
            calls.append(argv)
            super().__init__(["sleep", "5"], **{k: v for k, v in kwargs.items() if k != "shell"})

        def communicate(self, input=None, timeout=None):
            if timeout is not None:
                assert timeout == 30
                with open(Path(env["YARDGOAT_HOME"]) / ".schedule.lock", "a") as lk:
                    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise subprocess.TimeoutExpired(self.args, 30)
            return super().communicate(input=input)

    monkeypatch.setattr(notify.subprocess, "Popen", TimingOut)
    assert cli.main(["reconcile"]) == 0
    assert cli.main(["reconcile"]) == 0
    assert len(calls) == 2
    assert not (Path(env["YARDGOAT_HOME"]) / "notified/t-timeout.json").exists()


def test_notify_failure_keeps_auto_close_actions_and_single_json(env, tmp_path):
    spec = make_task(tmp_path)
    data = json.loads(spec.read_text())
    data["auto_close"] = True
    put_json(spec, data)
    assert yg(env, "submit", str(spec), "--as", "owner")[0] == 0
    assert yg(env, "dispatch", "t-demo", "--as", "owner")[0] == 0
    wait_exit(env, "t-demo")
    seed(env, "t-needs-answer", status="parked")
    _, fail = fake_hook(tmp_path, env)
    fail.touch()
    out = reconciled(env)
    assert out["notified"] == []
    assert out["actions"] == [{"task_id": "t-demo", "run_id": "r1", "auto_closed": "PASS"}]


def test_review_F1_hook_timeout_ends_the_spawned_sender_too(tmp_path, env):
    """Review of M6, F1: a hook that spawns a delayed sender and then blocks must not send after its timeout."""
    import os, sys, time
    import subprocess as sp
    sys.path.insert(0, SRC)
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A")
    assert yg(env, "park", "t-demo", "--as", "A", "--question", "q?")[0] == 0
    os.environ["YARDGOAT_HOME"] = env["YARDGOAT_HOME"]
    from yardgoat import notify
    sink = tmp_path / "sink"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"(sleep 2; echo sent >> {sink}) & sleep 120\n")
    real = sp.Popen.communicate
    def short(self, input=None, timeout=None):
        return real(self, input=input, timeout=1 if timeout == 30 else timeout)
    sp.Popen.communicate = short
    try:
        assert notify._notify_one("t-demo", ["bash", str(hook)], ["parked"]) is None
    finally:
        sp.Popen.communicate = real
    time.sleep(3)
    assert not sink.exists()


def test_review_F2_absurd_cost_makes_that_field_unknown_not_the_report_fail():
    import sys
    sys.path.insert(0, SRC)
    from yardgoat import report
    assert report._cost("usd=1e9999999999999999999999999 tokens=12")[1] == 12
    assert report._cost("tokens=" + "9" * 5000)[2] is True
    assert report._cost("usd=0.5 tokens=10")[:2] == (report.Decimal("0.5"), 10)


@pytest.mark.parametrize("adapter,expected", [({"kind": "direct"}, "direct"), ({}, "direct"),
                                               ({"kind": "file-pool"}, "file-pool")])
def test_report_cards_without_a_pool_group_under_their_adapter(env, adapter, expected):
    td = seed(env, "t-nopool", ["verified"], ["usd=0"])
    spec = json.loads((td / "spec.json").read_text())
    spec["adapter"] = adapter
    put_json(td / "spec.json", spec)
    assert [g["name"] for g in report(env, "--by", "pool")["groups"]] == [expected]
