"""M3 review regressions: real local processes, isolated stores, no model calls."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_contract import SRC, env, wait_exit, yg  # noqa: F401
from test_m3 import approve, execute, state, status, submit


def popen(env, *args):
    return subprocess.Popen([sys.executable, "-m", "yardgoat.cli", *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def until(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def cleanup(processes, pid_file=None):
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)
    if pid_file and pid_file.exists():
        for pid in pid_file.read_text().splitlines():
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def local_env(monkeypatch, env):
    for key in ("YARDGOAT_HOME", "YARDGOAT_CONFIG"):
        monkeypatch.setenv(key, env[key])
    for key in ("YARDGOAT_MAILBOX", "YARDGOAT_MAILBOX_HOME", "YARDGOAT_STATION"):
        monkeypatch.delenv(key, raising=False)


def test_F1_orphan_group_blocks_retry_until_deadline_then_is_killed(tmp_path, env):
    submit(tmp_path, env, on_pass_timeout=2, scope=["out"], on_pass=[{
        "name": "slow", "command": "echo $$ >> out/pids; echo start >> out/order; "
        "if test ! -e out/retry; then sleep 30; fi; echo done >> out/order"}])
    execute(env)
    proc = popen(env, "close", "t-demo", "--as", "owner")
    try:
        until(lambda: (tmp_path / "work/out/order").exists())
        proc.kill()
        proc.communicate(timeout=5)
        assert yg(env, "takeover", "t-demo", "--from", "owner", "--as", "new")[0] == 0
        rc, _, err = yg(env, "deliver", "t-demo", "--as", "new")
        assert rc == 3, err
        st = status(env)
        first = st["runs"][0]["receipt"]["on_pass"][0]
        assert first["pgid"] and first["deadline"]
        assert str(first["pgid"]) in st["delivery_reason"] and "截止时间" in st["delivery_reason"]
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start"]
        (tmp_path / "work/out/retry").touch()
        time.sleep(max(0, first["deadline"] - time.time()) + 0.05)
        assert yg(env, "deliver", "t-demo", "--as", "new")[0] == 0
        from yardgoat.runner import pgid_alive
        assert not pgid_alive(first["pgid"])
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start", "start", "done"]
        events = [json.loads(line) for line in yg(env, "events", "t-demo")[1].splitlines()]
        finished = [e for e in events if e["kind"] == "task.on_pass_finished"]
        assert [(e["attempt"], e["exit_code"]) for e in finished] == [(1, 124), (2, 0)]
    finally:
        cleanup([proc], tmp_path / "work/out/pids")


def test_F2_delivery_reserves_scope_without_holding_schedule_lock(tmp_path, env):
    submit(tmp_path, env, scope=["out"], on_pass_timeout=10, on_pass=[{
        "name": "hold", "command": "echo $$ > out/pids; touch out/started; "
        "while test ! -e out/release; do sleep .05; done"}])
    execute(env)
    submit(tmp_path, env, task_id="overlap", scope=["out"],
           executor={"host": "shell", "command": "touch out/overlap"})
    submit(tmp_path, env, task_id="independent", scope=["other"],
           executor={"host": "shell", "command": "mkdir -p other; touch other/done"})
    proc = popen(env, "close", "t-demo", "--as", "owner")
    contender = None
    try:
        until(lambda: (tmp_path / "work/out/started").exists())
        contender = popen(env, "deliver", "t-demo", "--as", "owner")
        assert yg(env, "dispatch", "overlap", "--as", "owner")[0] == 0
        blocked = json.loads(yg(env, "status", "overlap", "--json")[1])
        assert blocked["runs"][0]["state"] == "queued"
        assert blocked["runs"][0]["scope_conflicts"][0]["task_id"] == "t-demo"
        assert not (tmp_path / "work/out/overlap").exists()
        assert yg(env, "dispatch", "independent", "--as", "owner")[0] == 0
        until(lambda: (tmp_path / "work/other/done").exists())
        assert proc.poll() is None
        (tmp_path / "work/out/release").touch()
        proc.communicate(timeout=5)
        contender.communicate(timeout=5)
        assert proc.returncode == contender.returncode == 0
        assert yg(env, "reconcile")[0] == 0
        until(lambda: (tmp_path / "work/out/overlap").exists())
    finally:
        cleanup([p for p in (proc, contender) if p], tmp_path / "work/out/pids")


def test_F2_delivery_checks_worker_reservation(tmp_path, env):
    submit(tmp_path, env, scope=["out"], on_pass=[{"name": "save", "command": "touch out/delivered"}])
    execute(env)
    submit(tmp_path, env, task_id="worker", scope=["out"], executor={"host": "shell", "command":
           "echo $$ > out/pids; touch out/started; while test ! -e out/release; do sleep .05; done"})
    try:
        assert yg(env, "dispatch", "worker", "--as", "owner")[0] == 0
        until(lambda: (tmp_path / "work/out/started").exists())
        assert yg(env, "close", "t-demo", "--as", "owner")[0] == 3
        assert not (tmp_path / "work/out/delivered").exists()
        assert "worker" in status(env)["delivery_reason"]
        (tmp_path / "work/out/release").touch()
        wait_exit(env, "worker")
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
    finally:
        (tmp_path / "work/out/release").touch()
        cleanup([], tmp_path / "work/out/pids")


@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("problem", ["timeout", "approval"])
def test_F3_preflight_failures_remain_visible_everywhere(tmp_path, env, auto, problem):
    submit(tmp_path, env, auto_close=auto, risk="l3" if problem == "approval" else "l2",
           on_pass=[{"name": "save", "command": "touch out/delivered"}])
    if problem == "approval":
        approve(env)
    execute(env)
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(
        {"on_pass_timeout": 0} if problem == "timeout" else {"approvers": ["different"]}))
    if auto:
        rc, out, err = yg(env, "reconcile")
        assert rc == 0, err
        action = next(a for a in json.loads(out)["actions"] if "auto_closed" in a)
        assert action["auto_closed"] == "PASS"
        assert action.get("delivery_failed") is True
        assert action["delivery_exit_code"] in (2, 3)
        assert action["delivery_reason"]
    else:
        rc, out, _ = yg(env, "close", "t-demo", "--as", "owner")
        assert rc in (2, 3)
        assert json.loads(out)["delivery_failed"] is True
    st = status(env)
    assert st["status"] == "verified" and st["verdict"]["verdict"] == "PASS"
    assert st["delivery_failed"] and st["delivery_reason"]
    assert ("on_pass_timeout" if problem == "timeout" else "approve") in st["delivery_reason"]
    items = json.loads(yg(env, "inbox", "--json")[1])
    assert items[0]["question"] == st["delivery_reason"]
    rc, out, _ = yg(env, "close", "t-demo", "--as", "owner")
    assert rc == 1 and json.loads(out)["delivery_reason"] == st["delivery_reason"]
    assert not (tmp_path / "work/out/delivered").exists()
    Path(env["YARDGOAT_CONFIG"]).write_text("{}")
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
    assert json.loads(yg(env, "inbox", "--json")[1]) == []


@pytest.mark.parametrize("boundary", ["before_first", "between_steps"])
def test_F3_crash_boundaries_keep_all_declared_steps_pending(tmp_path, env, monkeypatch, boundary):
    from yardgoat import cli
    from yardgoat.store import TaskDir
    local_env(monkeypatch, env)
    submit(tmp_path, env, on_pass=[{"name": str(i), "command": f"echo {i} >> out/order"} for i in (1, 2)])
    execute(env)
    class Crash(BaseException):
        pass
    with monkeypatch.context() as patch:
        if boundary == "before_first":
            def crash(*args, **kwargs):
                raise Crash()
            patch.setattr(cli, "do_deliver", crash)
        else:
            original = TaskDir.event
            def crash(self, kind, actor, **data):
                result = original(self, kind, actor, **data)
                if kind == "task.on_pass_finished" and data["step"] == 1:
                    raise Crash()
                return result
            patch.setattr(TaskDir, "event", crash)
        with pytest.raises(Crash):
            cli.do_close(TaskDir("t-demo"), "owner")
    assert state(env).get("delivery_failed") is True
    assert status(env)["delivery_failed"] is True
    assert json.loads(yg(env, "inbox", "--json")[1])[0]["delivery_failed"]
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 1
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
    assert (tmp_path / "work/out/order").read_text().splitlines() == ["1", "2"]


@pytest.mark.parametrize("first_rc", [0, 7])
@pytest.mark.parametrize("after_append", [False, True])
def test_F4_receipt_result_replays_event_once_before_retry(tmp_path, env, monkeypatch, first_rc, after_append):
    from yardgoat import cli
    from yardgoat.store import TaskDir
    local_env(monkeypatch, env)
    submit(tmp_path, env, on_pass=[{"name": "one", "command":
           f"echo once >> out/order; test -e out/retry || exit {first_rc}"}])
    execute(env)
    class Crash(BaseException):
        pass
    original = TaskDir.event
    with monkeypatch.context() as patch:
        def crash(self, kind, actor, **data):
            if kind == "task.on_pass_finished":
                if after_append:
                    original(self, kind, actor, **data)
                raise Crash()
            return original(self, kind, actor, **data)
        patch.setattr(TaskDir, "event", crash)
        with pytest.raises(Crash):
            cli.do_close(TaskDir("t-demo"), "owner")
    (tmp_path / "work/out/retry").touch()
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
    records = [e for e in TaskDir("t-demo").events() if e["kind"] == "task.on_pass_finished"]
    assert [(r["attempt"], r["exit_code"]) for r in records] == ([(1, 0)] if first_rc == 0 else [(1, 7), (2, 0)])
    assert len({r["event_id"] for r in records}) == len(records)
    assert len((tmp_path / "work/out/order").read_text().splitlines()) == (1 if first_rc == 0 else 2)


def test_F5_report_counts_human_approve_separately(tmp_path, env, monkeypatch):
    from yardgoat.report import build_report
    from yardgoat.store import TaskDir
    local_env(monkeypatch, env)
    for i in range(5):
        task_id = f"approved-{i}"
        submit(tmp_path, env, task_id=task_id)
        assert yg(env, "approve", task_id, "--as", "reviewer")[0] == 0
        TaskDir(task_id).event("task.approved", "runner")
        TaskDir(task_id).event("task.approved", "codex-st-99")
    humans = build_report()["groups"][0]["human_interventions"]
    assert humans["total"] == 5 and humans["approve"] == 5
    assert humans["answer"] == humans["takeover"] == 0


def test_F6_dependencies_wait_for_failed_and_running_delivery_then_release(tmp_path, env):
    submit(tmp_path, env, on_pass=[{"name": "publish", "command": "echo $$ > out/pids; "
           "test -e out/retry || exit 1; touch out/started; "
           "while test ! -e out/release; do sleep .05; done; touch out/published"}])
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 1
    submit(tmp_path, env, task_id="downstream", depends_on=["t-demo"],
           executor={"host": "shell", "command": "test -e out/published && touch out/downstream"})
    assert yg(env, "dispatch", "downstream", "--as", "owner")[0] == 0
    st = json.loads(yg(env, "status", "downstream", "--json")[1])
    assert st["status"] == "waiting_deps"
    assert "t-demo" in st["runs"][0]["reason"] and "收尾" in st["runs"][0]["reason"]
    (tmp_path / "work/out/retry").touch()
    proc = popen(env, "deliver", "t-demo", "--as", "owner")
    try:
        until(lambda: (tmp_path / "work/out/started").exists())
        assert yg(env, "reconcile")[0] == 0
        assert json.loads(yg(env, "status", "downstream", "--json")[1])["status"] == "waiting_deps"
        assert not (tmp_path / "work/out/downstream").exists()
        (tmp_path / "work/out/release").touch()
        proc.communicate(timeout=5)
        assert proc.returncode == 0
        assert yg(env, "reconcile")[0] == 0
        until(lambda: (tmp_path / "work/out/downstream").exists())
        assert len(json.loads(yg(env, "status", "downstream", "--json")[1])["runs"]) == 1
    finally:
        cleanup([proc], tmp_path / "work/out/pids")


def test_F1_crash_before_pgid_commit_cannot_start_shell(tmp_path, env):
    submit(tmp_path, env, on_pass=[{"name": "one", "command": "touch out/should-not-start"}])
    execute(env)
    script = '''
import os
from pathlib import Path
from yardgoat import cli, delivery
from yardgoat.store import TaskDir
original = delivery.write_json
def crash(path, obj):
    records = obj.get('on_pass', [])
    if path.name == 'receipt.json' and records and records[0].get('pgid'):
        Path('gate-pid').write_text(str(records[0]['pgid']))
        os._exit(85)
    original(path, obj)
delivery.write_json = crash
cli.do_close(TaskDir('t-demo'), 'owner')
'''
    proc = subprocess.Popen([sys.executable, "-c", script], env=env, cwd=tmp_path)
    try:
        assert proc.wait(timeout=8) == 85
        from yardgoat.runner import pgid_alive
        pid = int((tmp_path / "gate-pid").read_text())
        until(lambda: not pgid_alive(pid))
        assert not (tmp_path / "work/out/should-not-start").exists()
        assert status(env)["delivery_failed"]
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
        assert (tmp_path / "work/out/should-not-start").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        cleanup([], tmp_path / "gate-pid")


def test_F2_pool_claim_and_second_delivery_respect_orphan_reservation(tmp_path, env, monkeypatch):
    from yardgoat import cli, pool
    local_env(monkeypatch, env)
    submit(tmp_path, env, scope=["out"], on_pass_timeout=8, on_pass=[{
        "name": "hold", "command": "echo $$ > out/pids; touch out/started; "
        "while test ! -e out/release; do sleep .05; done"}])
    execute(env)
    submit(tmp_path, env, task_id="second", scope=["out"],
           on_pass=[{"name": "second", "command": "touch out/second"}])
    assert yg(env, "dispatch", "second", "--as", "owner")[0] == 0
    wait_exit(env, "second")
    cfg = {"pools": {"local": {"host": "shell", "command": "true", "root": str(tmp_path), "max_stations": 0}}}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    submit(tmp_path, env, task_id="pooled", scope=["out"], adapter={"kind": "pool", "pool": "local"})
    assert yg(env, "dispatch", "pooled", "--as", "owner")[0] == 0
    pool.update_station("local", "local-st-1", station="local-st-1", state="running", tasks=[], pid=os.getpid())
    proc = popen(env, "close", "t-demo", "--as", "owner")
    try:
        until(lambda: (tmp_path / "work/out/started").exists())
        proc.kill()
        proc.communicate(timeout=5)
        assert cli.claim_card("local", "local-st-1")[1]["result"] == "EMPTY"
        assert yg(env, "close", "second", "--as", "owner")[0] == 3
        assert not (tmp_path / "work/out/second").exists()
        assert "t-demo" in json.loads(yg(env, "status", "second", "--json")[1])["delivery_reason"]
        (tmp_path / "work/out/release").touch()
        from yardgoat.runner import pgid_alive
        pid = int((tmp_path / "work/out/pids").read_text())
        until(lambda: not pgid_alive(pid))
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
        assert yg(env, "deliver", "second", "--as", "owner")[0] == 0
        assert cli.claim_card("local", "local-st-1")[1]["result"] == "CLAIMED"
        assert cli.finish_card("pooled", "r1", "local-st-1")[0] == 0
    finally:
        cleanup([proc], tmp_path / "work/out/pids")
