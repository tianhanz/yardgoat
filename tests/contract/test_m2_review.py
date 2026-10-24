"""M2 review regressions: isolated stores and fake adapters, never real models."""
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_contract import env, wait_exit, yg  # noqa: F401
from test_m2 import configure_adapter, dispatch, pool_stations, setup, status  # noqa: F401


@pytest.mark.parametrize("scope", ["src", "src/**"])
@pytest.mark.parametrize("target_kind", ["file", "directory"])
@pytest.mark.parametrize("first", ["alias-writer", "target-writer"])
def test_existing_symlink_inside_scope_conflicts_with_external_target(
        tmp_path, env, setup, scope, target_kind, first):
    adapter = configure_adapter(tmp_path, env, "plugin")
    wd = tmp_path / "shared"
    (wd / "src").mkdir(parents=True)
    (wd / "external").mkdir()
    (wd / "external/file.txt").write_text("original")
    target = "../external/file.txt" if target_kind == "file" else "../external"
    (wd / "src/alias").symlink_to(target, target_is_directory=target_kind == "directory")
    setup("alias-writer", workdir=str(wd), scope=[scope], adapter=adapter)
    setup("target-writer", workdir=str(wd), scope=["external/file.txt"], adapter=adapter)
    second = "target-writer" if first == "alias-writer" else "alias-writer"

    assert dispatch(env, first)["result"] == "dispatched"
    assert dispatch(env, second)["result"] == "queued"
    st = status(env, second)
    assert st["status"] == "queued" and not st["runs"][0].get("started_at")
    assert st["runs"][0]["scope_conflicts"][0]["task_id"] == first
    if first == "alias-writer":
        reserved = status(env, first)["runs"][0]["write_set"]
        assert str(wd / "src") in reserved  # retain protection for future files
        assert str((wd / "src/alias").resolve()) in reserved


def test_symlink_cycles_and_nested_targets_keep_external_writes_reserved(tmp_path, env, setup):
    adapter = configure_adapter(tmp_path, env, "plugin")
    wd = tmp_path / "shared"
    for directory in ("src", "external", "elsewhere"):
        (wd / directory).mkdir(parents=True)
    (wd / "src/alias").symlink_to("../external", target_is_directory=True)
    (wd / "external/back").symlink_to("../src", target_is_directory=True)
    (wd / "src/loop-a").symlink_to("loop-b")
    (wd / "src/loop-b").symlink_to("loop-a")
    (wd / "external/nested").symlink_to("../elsewhere/future.txt")
    setup("alias-writer", workdir=str(wd), scope=["src/**"], adapter=adapter)
    setup("target-writer", workdir=str(wd), scope=["elsewhere/future.txt"], adapter=adapter)
    assert dispatch(env, "alias-writer")["result"] == "dispatched"
    assert dispatch(env, "target-writer")["result"] == "queued"
    assert str(wd / "elsewhere/future.txt") in status(env, "alias-writer")["runs"][0]["write_set"]


def test_scope_scan_limit_records_event_and_still_starts(tmp_path, env, setup, monkeypatch):
    from yardgoat import cli, scheduling
    from yardgoat.store import TaskDir

    adapter = configure_adapter(tmp_path, env, "plugin")
    wd = tmp_path / "shared"
    (wd / "src").mkdir(parents=True)
    for n in range(6):
        (wd / "src" / f"file-{n}").touch()
    setup("limited", workdir=str(wd), scope=["src/**"], adapter=adapter)
    monkeypatch.setattr(scheduling, "WRITE_SET_SCAN_LIMIT", 3, raising=False)
    td = TaskDir("limited")
    assert cli.do_dispatch(td, "owner", echo=False) == 0
    st = td.state()
    assert st["status"] == "running"
    assert str(wd / "src") in st["runs"][0]["write_set"]
    events = [e for e in td.events() if e["kind"] == "task.scope_scan_limited"]
    assert events, "a truncated scan must explain the incomplete symlink reservation"
    assert all(e["limit"] == 3 and e["scanned"] == 3 and e["reason"] for e in events)


# Signal either completion (the old bug) or actual contention on a kernel lock.
# No timing-based 'archive probably has not run yet' assertion is needed.
ARCHIVE_PROBE = r'''
import fcntl
import os
import sys
from pathlib import Path
from yardgoat import cli

waiting, done = map(Path, sys.argv[1:])
original_flock = fcntl.flock
def observed_flock(fd, operation):
    if operation == fcntl.LOCK_EX:
        try:
            return original_flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            number = fd if isinstance(fd, int) else fd.fileno()
            waiting.write_text(os.readlink(f"/proc/self/fd/{number}"))
    return original_flock(fd, operation)
fcntl.flock = observed_flock
rc = cli.main(["archive", "upstream", "--as", "owner", "--reason", "withdrawn"])
done.write_text(str(rc))
raise SystemExit(rc)
'''


@pytest.mark.parametrize("start_path", ["adapter", "pool"])
def test_archive_serializes_with_dependency_check_and_start(
        tmp_path, env, setup, monkeypatch, start_path):
    from yardgoat import cli
    from yardgoat.store import TaskDir

    setup("upstream")
    dispatch(env, "upstream")
    wait_exit(env, "upstream")
    assert yg(env, "close", "upstream", "--as", "owner")[0] == 0
    if start_path == "pool":
        pool_stations(tmp_path, env)
        adapter = {"kind": "pool", "pool": "p"}
    else:
        adapter = configure_adapter(tmp_path, env, "plugin")
    setup("downstream", depends_on=["upstream"], adapter=adapter)
    if start_path == "pool":
        assert dispatch(env, "downstream")["result"] == "queued"

    upstream, downstream = TaskDir("upstream"), TaskDir("downstream")
    waiting, done = tmp_path / "archive-waiting", tmp_path / "archive-done"
    processes, observed = [], {}
    original_gate = cli._queue_gate
    checks = 0

    def pause_after_final_check(td, st, run, spec, actor):
        nonlocal checks
        ready = original_gate(td, st, run, spec, actor)
        if td.task_id != "downstream" or not ready:
            return ready
        checks += 1
        if checks != (2 if start_path == "adapter" else 1):
            return ready
        process = subprocess.Popen([sys.executable, "-c", ARCHIVE_PROBE, str(waiting), str(done)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        deadline = time.monotonic() + 10
        while not ((waiting.exists() and waiting.read_text()) or done.exists()):
            assert process.poll() is None, "archive exited without publishing its result"
            assert time.monotonic() < deadline, "archive never reached a lock or completed"
            time.sleep(0.01)
        observed["upstream_status"] = upstream.state()["status"]
        observed["waiting_on"] = waiting.read_text() if waiting.exists() else None
        observed["completed"] = done.exists()
        # A contender must wait for the schedule lock before taking the upstream task lock.
        with (upstream.path / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed["task_lock_free"] = True
            except BlockingIOError:
                observed["task_lock_free"] = False
        return ready

    monkeypatch.setattr(cli, "_queue_gate", pause_after_final_check)
    try:
        if start_path == "pool":
            rc, result = cli.claim_card("p", "p-st-1")
            assert rc == 0 and result["result"] == "CLAIMED"
        else:
            assert cli.do_dispatch(downstream, "owner", echo=False) == 0
        assert len(processes) == 1
        out, err = processes[0].communicate(timeout=10)
        assert processes[0].returncode == 0, (out, err)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert observed == {"upstream_status": "verified",
                        "waiting_on": str(Path(env["YARDGOAT_HOME"]) / ".schedule.lock"),
                        "completed": False, "task_lock_free": True}
    assert upstream.state()["status"] == "archived"
    assert downstream.state()["status"] == "running"
    assert len(downstream.state()["runs"]) == 1


@pytest.mark.parametrize("terminal", ["archived", "exhausted"])
def test_first_dispatch_with_terminated_upstream_immediately_enters_inbox(
        tmp_path, env, setup, monkeypatch, terminal):
    from yardgoat import cli
    from yardgoat.store import TaskDir

    setup("upstream", verifier={"command": "false", "freeze": []})
    if terminal == "archived":
        assert yg(env, "archive", "upstream", "--as", "owner", "--reason", "withdrawn")[0] == 0
    else:
        dispatch(env, "upstream")
        wait_exit(env, "upstream")
        assert yg(env, "close", "upstream", "--as", "owner")[0] == 1
    assert status(env, "upstream")["status"] == terminal
    adapter = configure_adapter(tmp_path, env, "plugin")
    setup("downstream", depends_on=["upstream"], adapter=adapter)
    launches = []

    class Fake:
        def submit(self, td, run_id, spec, cfg):
            launches.append((td.task_id, run_id))
            return {"mode": "plugin", "pid": None}

    monkeypatch.setattr(cli.adapters, "get", lambda *args: Fake())
    td = TaskDir("downstream")
    assert cli.do_dispatch(td, "owner", echo=False) == 0
    st = td.state()
    assert st["status"] == "parked"
    assert len(st["runs"]) == 1 and st["runs"][0]["state"] == "blocked_deps"
    assert st["runs"][0]["waiting_for"] == ["upstream"]
    assert not st["runs"][0].get("started_at") and not launches
    assert not (td.run_dir("r1") / "receipt.json").exists()
    assert sum(run["state"] in cli.COUNTED for run in st["runs"]) == 0
    inbox = json.loads(yg(env, "inbox", "--json")[1])
    question = next(item["question"] for item in inbox if item["task_id"] == "downstream")
    assert "上游 upstream 已终止" in question
    assert not any(e["kind"] == "run.waiting_deps" for e in td.events())
