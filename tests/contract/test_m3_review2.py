"""M3 second-review regressions. Isolated stores and real local shells, never models."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_contract import env, wait_exit, yg  # noqa: F401
from test_m3 import execute, state, status, submit
from test_m3_review import local_env, popen, until


def receipt_path(env):
    return Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/runs/r1/receipt.json"


def patch_receipt(env, mutate):
    path = receipt_path(env)
    receipt = json.loads(path.read_text())
    mutate(receipt)
    path.write_text(json.dumps(receipt))


def patch_state(env, **fields):
    path = Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/task.json"
    st = json.loads(path.read_text())
    st.update(fields)
    path.write_text(json.dumps(st))


def identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return {"start_ticks": fields[19], "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def stop(proc=None, pgid=None):
    if proc:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)
    if pgid:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        from yardgoat.runner import pgid_alive
        until(lambda: not pgid_alive(pgid))


def orphan(tmp_path, env, **changes):
    options = dict(scope=["out"], on_pass_timeout=15, on_pass=[{
        "name": "hold", "command": "echo start >> out/order; touch out/started; "
        "if test ! -e out/retry; then sleep 30; fi"}])
    options.update(changes)
    submit(tmp_path, env, **options)
    execute(env)
    controller = popen(env, "close", "t-demo", "--as", "owner")
    until(lambda: (tmp_path / "work/out/started").exists())
    record = status(env)["runs"][0]["receipt"]["on_pass"][0]
    stop(controller)
    return record


@pytest.mark.parametrize("outcome", ["success", "failed", "unfinished", "other_boot"])
def test_F1_reused_group_never_signalled_and_success_never_repeated(tmp_path, env, outcome):
    submit(tmp_path, env, on_pass=[{"name": "once", "command": "echo once >> out/order"}])
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 0
    unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        def change(receipt):
            record = receipt["on_pass"][0]
            record.update(pgid=unrelated.pid, deadline=time.time() - 1, process_identity=identity(unrelated.pid))
            if outcome == "failed":
                record["exit_code"] = 7
            elif outcome in ("unfinished", "other_boot"):
                record.pop("finished_at", None)
                record["exit_code"] = None
                if outcome == "other_boot":
                    record["process_identity"]["boot_id"] = "previous-boot"
                else:
                    record["process_identity"]["start_ticks"] = str(int(record["process_identity"]["start_ticks"]) - 1)
        patch_receipt(env, change)
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
        assert unrelated.poll() is None, "a historical/reused PGID must never authorize a signal"
        count = 1 if outcome == "success" else 2
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["once"] * count
        assert status(env)["runs"][0]["receipt"]["on_pass"][0]["attempt"] == count
    finally:
        stop(unrelated, unrelated.pid)


def test_F1_identity_survives_shell_exit_with_background_children(tmp_path, env):
    record = orphan(tmp_path, env, on_pass=[{"name": "background", "command":
        "echo start >> out/order; if test ! -e out/retry; then sleep 30 & echo $! > out/child; fi; "
        "touch out/started; exit 0"}])
    try:
        assert record.get("process_identity"), "a running attempt needs a durable process identity"
        assert record.get("process_identity") == identity(record["pgid"])
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 3
        assert state(env).get("delivery_reservation")
        (tmp_path / "work/out/retry").touch()
        patch_receipt(env, lambda r: r["on_pass"][0].update(deadline=time.time() - 1))
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
        child = Path(f"/proc/{int((tmp_path / 'work/out/child').read_text())}/stat")
        assert not child.exists() or child.read_text().rsplit(") ", 1)[1].startswith("Z ")
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start", "start"]
    finally:
        stop(pgid=record["pgid"])


@pytest.mark.parametrize("exit_code", [0, 7])
def test_F2_terminal_state_and_release_are_one_commit(tmp_path, env, monkeypatch, exit_code):
    from yardgoat import cli
    from yardgoat.store import TaskDir
    local_env(monkeypatch, env)
    submit(tmp_path, env, scope=["out"], on_pass=[{"name": "one", "command": f"exit {exit_code}"}])
    execute(env)
    class Crash(BaseException):
        pass
    original = TaskDir.event
    def crash(self, kind, actor, **data):
        result = original(self, kind, actor, **data)
        if kind == "task.on_pass_finished":
            raise Crash()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(TaskDir, "event", crash)
        with pytest.raises(Crash):
            cli.do_close(TaskDir("t-demo"), "owner")
    assert not state(env).get("delivery_reservation")
    assert status(env)["runs"][0]["receipt"]["on_pass"][0]["exit_code"] == exit_code


@pytest.mark.parametrize("cleanup", ["archive", "reconcile", "reconcile_archived", "deliver_archived"])
def test_F2_finished_receipt_repairs_stale_reservation_in_all_lifecycles(tmp_path, env, cleanup):
    submit(tmp_path, env, scope=["out"], on_pass=[{"name": "one", "command": "echo once >> out/order"}])
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 0
    # Receipt is durable but task.json predates the result/release transaction.
    patch_state(env, on_pass=[], delivery_pending=True, delivery_failed=True,
                delivery_reservation={"write_set": [str(tmp_path / "work/out")], "run_id": "r1"},
                status="archived" if cleanup.endswith("archived") else "verified")
    if cleanup == "archive":
        args = ("archive", "t-demo", "--as", "owner", "--reason", "done")
    elif cleanup == "deliver_archived":
        args = ("deliver", "t-demo", "--as", "owner")
    else:
        args = ("reconcile",)
    assert yg(env, *args)[0] == 0
    assert not state(env).get("delivery_reservation")
    assert not state(env)["delivery_pending"]
    assert (tmp_path / "work/out/order").read_text().splitlines() == ["once"]
    submit(tmp_path, env, task_id="overlap", scope=["out"],
           executor={"host": "shell", "command": "touch out/overlap"})
    assert yg(env, "dispatch", "overlap", "--as", "owner")[0] == 0
    wait_exit(env, "overlap")
    assert (tmp_path / "work/out/overlap").exists()


def test_F2_receipt_first_crash_is_repaired_without_rerunning(tmp_path, env, monkeypatch):
    from yardgoat import cli, store
    local_env(monkeypatch, env)
    submit(tmp_path, env, scope=["out"], on_pass=[{"name": "one", "command": "echo once >> out/order"}])
    execute(env)
    class Crash(BaseException):
        pass
    original = store.write_json
    def crash(path, obj):
        if path.name == "task.json" and obj.get("on_pass") and not obj.get("delivery_pending"):
            raise Crash()
        return original(path, obj)
    with monkeypatch.context() as patch:
        patch.setattr(store, "write_json", crash)
        with pytest.raises(Crash):
            cli.do_close(store.TaskDir("t-demo"), "owner")
    assert state(env).get("delivery_reservation")
    assert yg(env, "reconcile")[0] == 0
    assert not state(env).get("delivery_reservation")
    assert (tmp_path / "work/out/order").read_text().splitlines() == ["once"]
    finished = [e for e in store.TaskDir("t-demo").events() if e["kind"] == "task.on_pass_finished"]
    assert len(finished) == 1 and finished[0]["exit_code"] == 0


def test_F2_archive_waits_for_orphan_deadline_without_blocking_scheduler(tmp_path, env):
    record = orphan(tmp_path, env, on_pass_timeout=2)
    archiver = None
    try:
        archiver = popen(env, "archive", "t-demo", "--as", "owner", "--reason", "withdrawn")
        time.sleep(0.1)
        assert archiver.poll() is None, "archive must not finish while delivery still writes"
        assert state(env)["status"] == "verified" and state(env).get("delivery_reservation")
        submit(tmp_path, env, task_id="independent", scope=["other"],
               executor={"host": "shell", "command": "mkdir -p other; touch other/done"})
        assert yg(env, "dispatch", "independent", "--as", "owner")[0] == 0
        until(lambda: (tmp_path / "work/other/done").exists())
        archiver.communicate(timeout=6)
        assert archiver.returncode == 0
        from yardgoat.runner import pgid_alive
        assert not pgid_alive(record["pgid"])
        assert state(env)["status"] == "archived" and not state(env).get("delivery_reservation")
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start"]
    finally:
        stop(archiver, record["pgid"])


def test_F2_reconcile_keeps_live_orphan_then_reclaims_expired_group(tmp_path, env):
    record = orphan(tmp_path, env)
    try:
        assert yg(env, "reconcile")[0] == 0
        assert state(env).get("delivery_reservation")
        patch_receipt(env, lambda r: r["on_pass"][0].update(deadline=time.time() - 1))
        assert yg(env, "reconcile")[0] == 0
        assert not state(env).get("delivery_reservation")
        finished = status(env)["runs"][0]["receipt"]["on_pass"][0]
        assert finished["exit_code"] == 124 and finished["finished_at"]
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start"]
    finally:
        stop(pgid=record["pgid"])


def test_F2_reconcile_does_not_wait_for_live_controller(tmp_path, env):
    submit(tmp_path, env, scope=["out"], on_pass=[{"name": "hold", "command":
        "touch out/started; while test ! -e out/retry; do sleep .05; done"}])
    execute(env)
    proc = popen(env, "close", "t-demo", "--as", "owner")
    pgid = None
    try:
        until(lambda: (tmp_path / "work/out/started").exists())
        pgid = status(env)["runs"][0]["receipt"]["on_pass"][0]["pgid"]
        started = time.monotonic()
        assert yg(env, "reconcile")[0] == 0
        assert time.monotonic() - started < 2
        assert state(env).get("delivery_reservation") and proc.poll() is None
        (tmp_path / "work/out/retry").touch()
        proc.communicate(timeout=5)
        assert proc.returncode == 0
    finally:
        stop(proc, pgid)


def test_F3_retargeted_scope_retains_old_paths_until_new_attempt(tmp_path, env):
    work = tmp_path / "work"
    for name in ("a", "b"):
        (work / name).mkdir(parents=True)
    (work / "current").symlink_to(work / "a", target_is_directory=True)
    record = orphan(tmp_path, env, scope=["current"], on_pass=[{"name": "hold", "command":
        "cd current; echo start >> ../out/order; touch ../out/started; "
        "if test ! -e ../out/retry; then sleep 30; fi"}])
    try:
        (work / "current").unlink()
        (work / "current").symlink_to(work / "b", target_is_directory=True)
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 3
        assert set(state(env)["delivery_reservation"]["write_set"]) >= {str(work / "a"), str(work / "b")}
        submit(tmp_path, env, task_id="overlap", scope=["a"],
               executor={"host": "shell", "command": "touch a/overlap"})
        assert yg(env, "dispatch", "overlap", "--as", "owner")[0] == 0
        queued = json.loads(yg(env, "status", "overlap", "--json")[1])
        assert queued["runs"][0]["state"] == "queued"
        assert not (work / "a/overlap").exists()
        stop(pgid=record["pgid"])
        (work / "out/retry").touch()
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
        latest = status(env)["runs"][0]["receipt"]["on_pass"][0]
        assert latest["write_set"] == [str(work / "b")]
        assert not state(env).get("delivery_reservation")
        assert yg(env, "reconcile")[0] == 0
        wait_exit(env, "overlap")
        assert (work / "a/overlap").exists()
    finally:
        stop(pgid=record["pgid"])


@pytest.mark.parametrize("legacy", ["no_pgid", "bare_pgid"])
def test_F4_unknown_legacy_requires_owner_assertion_and_preserves_reservation(tmp_path, env, legacy):
    record = orphan(tmp_path, env)
    try:
        def downgrade(receipt):
            old = receipt["on_pass"][0]
            for key in ("process_identity", "launch_protocol", "write_set"):
                old.pop(key, None)
            if legacy == "no_pgid":
                old.pop("pgid", None)
                old.pop("deadline", None)
        patch_receipt(env, downgrade)
        (tmp_path / "work/out/retry").touch()
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 3
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start"]
        assert "旧版本留下的收尾记录，进程状态未知" in status(env)["delivery_reason"]
        assert "旧版本留下的收尾记录" in yg(env, "inbox", "--json")[1]
        assert yg(env, "reconcile")[0] == 0
        assert state(env).get("delivery_reservation")
        assert yg(env, "archive", "t-demo", "--as", "owner", "--reason", "done")[0] == 3
        stop(pgid=record["pgid"])
        assert yg(env, "takeover", "t-demo", "--from", "owner", "--as", "new")[0] == 0
        assert yg(env, "deliver", "t-demo", "--as", "owner", "--assume-stopped")[0] == 3
        assert yg(env, "deliver", "t-demo", "--as", "new", "--assume-stopped")[0] == 0
        events = [json.loads(line) for line in yg(env, "events", "t-demo")[1].splitlines()]
        confirmations = [e for e in events if e.get("assume_stopped")]
        assert len(confirmations) == 1 and confirmations[0]["actor"] == "new"
        assert confirmations[0]["step"] == confirmations[0]["attempt"] == 1
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start", "start"]
        assert not state(env).get("delivery_reservation")
    finally:
        stop(pgid=record["pgid"])


def test_F4_archived_unknown_stays_in_inbox_until_explicit_cleanup(tmp_path, env):
    record = orphan(tmp_path, env)
    try:
        patch_receipt(env, lambda r: [r["on_pass"][0].pop(k, None)
                                     for k in ("process_identity", "launch_protocol", "pgid", "deadline")])
        patch_state(env, status="archived")
        assert yg(env, "reconcile")[0] == 0
        assert state(env).get("delivery_reservation")
        assert "旧版本留下的收尾记录" in yg(env, "inbox", "--json")[1]
        stop(pgid=record["pgid"])
        assert yg(env, "deliver", "t-demo", "--as", "owner", "--assume-stopped")[0] == 0
        assert state(env)["status"] == "archived" and not state(env).get("delivery_reservation")
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start"]
    finally:
        stop(pgid=record["pgid"])


def test_F4_legacy_success_skips_without_process_fields(tmp_path, env):
    submit(tmp_path, env, on_pass=[{"name": "one", "command": "echo once >> out/order"}])
    execute(env)
    assert yg(env, "close", "t-demo", "--as", "owner")[0] == 0
    patch_receipt(env, lambda r: [r["on_pass"][0].pop(k, None)
                                 for k in ("process_identity", "launch_protocol", "pgid", "deadline", "finished_at")])
    assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 0
    assert (tmp_path / "work/out/order").read_text().splitlines() == ["once"]


def test_F4_missing_legacy_reservation_restored_before_conflicting_preflight(tmp_path, env):
    record = orphan(tmp_path, env)
    worker_pgid = None
    try:
        patch_receipt(env, lambda r: [r["on_pass"][0].pop(k, None)
                                     for k in ("process_identity", "launch_protocol", "pgid", "deadline", "write_set")])
        path = Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/task.json"
        st = json.loads(path.read_text())
        st.pop("delivery_reservation")
        path.write_text(json.dumps(st))
        submit(tmp_path, env, task_id="other", scope=["out"], executor={"host": "shell", "command":
               "touch out/other-started; while test ! -e out/unblock; do sleep .05; done"})
        assert yg(env, "dispatch", "other", "--as", "owner")[0] == 0
        until(lambda: (tmp_path / "work/out/other-started").exists())
        other = json.loads(yg(env, "status", "other", "--json")[1])
        worker_pgid = other["runs"][0]["worker_pgid"]
        assert yg(env, "deliver", "t-demo", "--as", "owner")[0] == 3
        assert state(env).get("delivery_reservation"), "preflight refusal cannot drop legacy protection"
        assert str(tmp_path / "work/out") in state(env)["delivery_reservation"]["write_set"]
        assert "旧版本留下的收尾记录" in status(env)["delivery_reason"]
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start"]
    finally:
        (tmp_path / "work/out/unblock").touch()
        if worker_pgid:
            wait_exit(env, "other")
        stop(pgid=record["pgid"])
        stop(pgid=worker_pgid)


def test_F4_assume_stopped_cannot_override_identified_live_group(tmp_path, env):
    record = orphan(tmp_path, env)
    try:
        (tmp_path / "work/out/retry").touch()
        assert yg(env, "deliver", "t-demo", "--as", "owner", "--assume-stopped")[0] == 3
        assert state(env).get("delivery_reservation")
        assert (tmp_path / "work/out/order").read_text().splitlines() == ["start"]
        events = [json.loads(line) for line in yg(env, "events", "t-demo")[1].splitlines()]
        assert not any(e.get("assume_stopped") for e in events)
    finally:
        stop(pgid=record["pgid"])
