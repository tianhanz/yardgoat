"""M3 approval and delivery contracts, using isolated stores and local shell commands."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401

sys.path.insert(0, SRC)


def state(env):
    return json.loads((Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/task.json").read_text())


def status(env):
    return json.loads(yg(env, "status", "t-demo", "--json")[1])


def submit(tmp_path, env, **changes):
    sp = make_task(tmp_path)
    spec = json.loads(sp.read_text())
    spec.update(changes)
    sp.write_text(json.dumps(spec))
    assert yg(env, "submit", str(sp), "--as", "owner")[0] == 0
    return sp


def execute(env):
    assert yg(env, "dispatch", "t-demo", "--as", "owner")[0] == 0
    wait_exit(env, "t-demo")


def approve(env, who="reviewer"):
    rc, out, err = yg(env, "approve", "t-demo", "--as", who, "--note", "Reviewed frozen card")
    assert rc == 0, (out, err)


def test_l3_requires_approval_and_carries_it_on_every_receipt(tmp_path, env):
    submit(tmp_path, env, risk="l3", verifier={"command": "false", "freeze": []})
    rc, _, err = yg(env, "dispatch", "t-demo", "--as", "owner")
    assert rc == 3 and "需要 approve" in err
    assert state(env)["runs"] == []
    assert "等审批" in yg(env, "inbox", "--json")[1]
    approve(env)
    approval = state(env)["approval"]
    assert approval["by"] == "reviewer" and approval["at"] and approval["note"]
    assert approval["content_hash"] == state(env)["content_hash"]
    for _ in range(2):
        execute(env)
        assert yg(env, "close", "t-demo", "--as", "owner")[0] == 1
    assert all(r["receipt"]["approval"] == approval for r in status(env)["runs"])
    events = [json.loads(line) for line in yg(env, "events", "t-demo")[1].splitlines()]
    assert any(e["kind"] == "task.approved" and e["actor"] == "reviewer" and e["approval"] == approval for e in events)


@pytest.mark.parametrize("allowed", [["reviewer"], []])
def test_only_configured_approvers_are_accepted(tmp_path, env, allowed):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"approvers": allowed}))
    submit(tmp_path, env, risk="l3")
    assert yg(env, "approve", "t-demo", "--as", "owner")[0] == 3
    assert not state(env).get("approval")
    if allowed:
        approve(env)
        execute(env)


@pytest.mark.parametrize("file", ["card.md", "verify.sh"])
def test_observed_drift_invalidates_approval_even_after_restoring_bytes(tmp_path, env, file):
    submit(tmp_path, env, risk="l3")
    approve(env)
    path = tmp_path / file
    original = path.read_bytes()
    path.write_bytes(original + b"\n# changed\n")
    assert yg(env, "dispatch", "t-demo", "--as", "owner")[0] == 3
    assert yg(env, "approve", "t-demo", "--as", "reviewer")[0] == 3
    path.write_bytes(original)
    rc, _, err = yg(env, "dispatch", "t-demo", "--as", "owner")
    assert rc == 3 and "approve" in err
    approve(env)
    execute(env)


def test_approval_and_answer_do_not_replace_each_other(tmp_path, env):
    submit(tmp_path, env, risk="l3")
    assert yg(env, "park", "t-demo", "--as", "owner", "--question", "Which input?")[0] == 0
    question = state(env)["question"]
    approve(env)
    assert state(env)["status"] == "parked" and state(env)["question"] == question
    assert yg(env, "answer", "t-demo", "--as", "reviewer", "--text", "Use sample")[0] == 0
    assert state(env)["approval"]["by"] == "reviewer"
    # A different task with only an answer still cannot dispatch.
    other = tmp_path / "other"
    other.mkdir()
    other_env = {**env, "YARDGOAT_HOME": str(other / "home")}
    submit(other, other_env, risk="l3")
    assert yg(other_env, "park", "t-demo", "--as", "owner", "--question", "Which input?")[0] == 0
    assert yg(other_env, "answer", "t-demo", "--as", "reviewer", "--text", "Approved")[0] == 0
    assert yg(other_env, "dispatch", "t-demo", "--as", "owner")[0] == 3
    assert not state(other_env).get("approval")


def test_queue_release_rechecks_current_approver_policy(tmp_path, env):
    cfg = Path(env["YARDGOAT_CONFIG"])
    cfg.write_text(json.dumps({"max_concurrent": 0, "approvers": ["reviewer"]}))
    submit(tmp_path, env, risk="l3")
    approve(env)
    assert yg(env, "dispatch", "t-demo", "--as", "owner")[0] == 0
    cfg.write_text(json.dumps({"approvers": ["someone-else"]}))
    assert yg(env, "reconcile")[0] == 0
    assert not (tmp_path / "work/out/result.txt").exists()
    assert "等审批" in yg(env, "inbox")[1]


def test_pool_claim_checks_approval_and_receipt_keeps_starting_approver(tmp_path, env, monkeypatch):
    from yardgoat import cli, pool
    for key in ("YARDGOAT_HOME", "YARDGOAT_CONFIG"):
        monkeypatch.setenv(key, env[key])
    cfg = {"pools": {"local": {"host": "shell", "command": "true", "root": str(tmp_path), "max_stations": 0}}}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    submit(tmp_path, env, risk="l3", adapter={"kind": "pool", "pool": "local"})
    assert yg(env, "dispatch", "t-demo", "--as", "owner")[0] == 3
    approve(env)
    assert yg(env, "dispatch", "t-demo", "--as", "owner")[0] == 0
    pool.update_station("local", "local-st-1", station="local-st-1", state="running", tasks=[], pid=os.getpid())
    cfg["approvers"] = ["another-reviewer"]
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    assert cli.claim_card("local", "local-st-1")[1]["result"] == "EMPTY"
    assert not (tmp_path / "work/out/result.txt").exists()
    approve(env, "another-reviewer")
    assert yg(env, "dispatch", "t-demo", "--as", "owner")[0] == 0
    assert cli.claim_card("local", "local-st-1")[1]["result"] == "CLAIMED"
    cfg["approvers"].append("later-reviewer")
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    approve(env, "later-reviewer")
    assert cli.finish_card("t-demo", "r2", "local-st-1")[0] == 0
    assert status(env)["runs"][-1]["receipt"]["approval"]["by"] == "another-reviewer"


def steps():
    return [{"name": str(n), "command": f"echo {n} >> out/order"} for n in (1, 2, 3)]


def test_on_pass_runs_in_order_only_after_pass_and_is_idempotent(tmp_path, env):
    submit(tmp_path, env, on_pass=steps())
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 3
    execute(env)
    order = tmp_path / "work/out/order"
    assert not order.exists()
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 0
    assert order.read_text().splitlines() == ["1", "2", "3"]
    st = status(env)
    records = st["runs"][0]["receipt"]["on_pass"]
    assert [r["exit_code"] for r in records] == [0, 0, 0]
    assert all(r["by"] == "owner" and r["finished_at"] and "output_tail" in r for r in records)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 0
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
    assert order.read_text().splitlines() == ["1", "2", "3"]
    events = [json.loads(line) for line in yg(env, "events", "t-demo")[1].splitlines()]
    done = [e for e in events if e["kind"] == "task.on_pass_finished"]
    assert [e["step"] for e in done] == [1, 2, 3]
    assert all(e["actor"] == "owner" and e["exit_code"] == 0 for e in done)


def test_delivery_failure_stops_and_retry_resumes_failed_step_under_new_owner(tmp_path, env):
    actions = steps()
    actions[1]["command"] = "echo attempt >> out/attempts; echo failure >&2; test -f out/retry && echo 2 >> out/order"
    submit(tmp_path, env, on_pass=actions)
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 1
    st = status(env)
    assert st["status"] == "verified" and st["delivery_failed"] is True
    assert st["verdict"]["verdict"] == "PASS"
    rec = st["runs"][0]["receipt"]
    assert rec["status"] == "passed" and rec["delivery_failed"] is True
    assert len(rec["on_pass"]) == 2 and rec["on_pass"][1]["exit_code"] == 1
    assert "failure" in rec["on_pass"][1]["output_tail"]
    assert "收尾第 2 步 2 失败" in yg(env, "inbox")[1]
    (tmp_path / "work/out/retry").touch()
    assert yg(env, "takeover", "t-demo", "--from", "owner", "--as", "new-owner")[0] == 0
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 3
    assert yg(env, "deliver", "t-demo", "--as", "new-owner")[0] == 0
    assert (tmp_path / "work/out/order").read_text().splitlines() == ["1", "2", "3"]
    assert (tmp_path / "work/out/attempts").read_text().splitlines() == ["attempt", "attempt"]
    assert not status(env)["delivery_failed"] and json.loads(yg(env, "inbox", "--json")[1]) == []
    assert [r["by"] for r in status(env)["runs"][0]["receipt"]["on_pass"]] == ["owner", "new-owner", "new-owner"]


@pytest.mark.parametrize("verify", ["false", "exit 7"])
def test_fail_or_verifier_error_never_delivers(tmp_path, env, verify):
    submit(tmp_path, env, on_pass=steps(), verifier={"command": verify, "freeze": []})
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] in (1, 2)
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 3
    assert not (tmp_path / "work/out/order").exists()


def test_auto_close_delivers_as_owner_with_explicit_cwd(tmp_path, env):
    submit(tmp_path, env, auto_close=True, on_pass=[{"name": "save", "command": "echo $YARDGOAT_OWNER > owner", "cwd": "work/out"}])
    execute(env)
    rc, out, err = yg(env, "reconcile")
    assert rc == 0, err
    assert json.loads(out)["actions"][0]["auto_closed"] == "PASS"
    assert (tmp_path / "work/out/owner").read_text().strip() == "owner"


def test_on_pass_changes_content_hash_and_cannot_rewrite_accepted_task(tmp_path, env):
    from yardgoat.contract import content_hash, freeze, load_spec_file
    sp = submit(tmp_path, env, on_pass=steps())
    first = load_spec_file(sp)
    raw = json.loads(sp.read_text())
    raw["on_pass"][0]["command"] = "echo changed"
    sp.write_text(json.dumps(raw))
    second = load_spec_file(sp)
    assert content_hash(first, freeze(first)) != content_hash(second, freeze(second))
    assert yg(env, "submit", str(sp), "--as", "owner")[0] == 3


@pytest.mark.parametrize("actions", [None, {}, [1], [{}], [{"name": "n", "command": 7}],
                                       [{"name": "", "command": "true"}], [{"name": "n", "command": "true", "cwd": 3}]])
def test_invalid_on_pass_is_rejected_at_submit(tmp_path, env, actions):
    sp = make_task(tmp_path)
    raw = json.loads(sp.read_text())
    raw["on_pass"] = actions
    sp.write_text(json.dumps(raw))
    rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
    assert rc == 2 and "on_pass" in err


def test_on_pass_timeout_kills_descendants_and_records_tail(tmp_path, env):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"on_pass_timeout": 0.15}))
    submit(tmp_path, env, on_pass=[{"name": "slow", "command": "sleep 30 & echo $! > out/child; echo before-timeout; wait"}])
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 1
    item = status(env)["runs"][0]["receipt"]["on_pass"][0]
    assert item["exit_code"] == 124 and item["timed_out"] and "before-timeout" in item["output_tail"]
    child = Path(f"/proc/{int((tmp_path / 'work/out/child').read_text())}/stat")
    assert not child.exists() or child.read_text().split(") ")[1][0] == "Z"


def test_concurrent_close_and_deliver_do_not_repeat_success(tmp_path, env):
    submit(tmp_path, env, on_pass=[{"name": "one", "command": "sleep 0.2; echo once >> out/order"}])
    execute(env)
    procs = [subprocess.Popen([sys.executable, "-m", "yardgoat.cli", cmd, "t-demo", "--as", "owner"],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for cmd in ("close", "close", "deliver")]
    for p in procs:
        p.communicate(timeout=15)
    assert (tmp_path / "work/out/order").read_text().splitlines() == ["once"]


def test_delivery_recovers_receipt_progress_without_repeating_commands(tmp_path, env):
    actions = steps()
    actions[1]["command"] = "test -f out/retry && echo 2 >> out/order"
    submit(tmp_path, env, on_pass=actions)
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 1
    path = Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/task.json"
    st = state(env)
    st["on_pass"] = []  # receipt committed before controller crashed saving task.json
    path.write_text(json.dumps(st))
    (tmp_path / "work/out/retry").touch()
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
    assert (tmp_path / "work/out/order").read_text().splitlines() == ["1", "2", "3"]
    assert len(state(env)["on_pass"]) == 3


def test_delivery_cannot_run_changed_frozen_commands(tmp_path, env):
    submit(tmp_path, env, on_pass=[{"name": "retry", "command": "test -f out/retry && echo done > out/delivered"}])
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 1
    accepted = Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/spec.json"
    data = json.loads(accepted.read_text())
    data["on_pass"][0]["command"] = "echo changed > out/delivered"
    accepted.write_text(json.dumps(data))
    (tmp_path / "work/out/retry").touch()
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 3
    assert not (tmp_path / "work/out/delivered").exists()


@pytest.mark.parametrize("timeout", [0, -1, True, "600", float("inf")])
def test_invalid_delivery_timeout_is_rejected(tmp_path, env, timeout):
    sp = make_task(tmp_path)
    raw = json.loads(sp.read_text())
    raw.update(on_pass=steps(), on_pass_timeout=timeout)
    sp.write_text(json.dumps(raw))
    rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
    assert rc == 2 and "on_pass_timeout" in err
