"""M2 scheduling contracts. Workers are shell scripts or local fake adapters, never models."""
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401

sys.path.insert(0, SRC)


@pytest.fixture
def setup(tmp_path, env, monkeypatch):
    env.pop("YARDGOAT_MAILBOX", None)
    monkeypatch.delenv("YARDGOAT_MAILBOX", raising=False)
    for key in ("YARDGOAT_HOME", "YARDGOAT_CONFIG"):
        monkeypatch.setenv(key, env[key])
    releases = []

    def task(tid, *, hold=False, **changes):
        release = tmp_path / (tid + ".release")
        releases.append(release)
        worker = f"while ! test -e {shlex.quote(str(release))}; do sleep 0.05; done" if hold else "true"
        sp = make_task(tmp_path / tid, task_id=tid, worker=worker, verify="true", attempts=1)
        spec = json.loads(sp.read_text())
        spec.update(changes)
        Path(spec["workdir"] if Path(spec["workdir"]).is_absolute() else sp.parent / spec["workdir"]).mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(spec))
        rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
        assert rc == 0, err
        return sp

    yield task
    for release in releases:
        release.touch()


def status(env, tid):
    return json.loads(yg(env, "status", tid, "--json")[1])


def dispatch(env, tid):
    rc, out, err = yg(env, "dispatch", tid, "--as", "owner")
    assert rc == 0, err
    return json.loads(out)


def reconcile(env):
    rc, out, err = yg(env, "reconcile")
    assert rc == 0, err
    return json.loads(out)["actions"]


def test_dependency_chain_releases_only_after_verification(tmp_path, env, setup):
    setup("dep-a", auto_close=True)
    setup("dep-b", depends_on=["dep-a"], auto_close=True)
    setup("dep-c", depends_on=["dep-b"], auto_close=True)
    assert dispatch(env, "dep-c")["result"] == "waiting_deps"
    assert dispatch(env, "dep-b")["result"] == "waiting_deps"
    assert "dep-a" in yg(env, "status", "dep-b")[1]
    assert reconcile(env)
    assert status(env, "dep-c")["status"] == "waiting_deps"
    dispatch(env, "dep-a")
    wait_exit(env, "dep-a")
    assert status(env, "dep-b")["status"] == "waiting_deps"  # exited is insufficient
    end = time.monotonic() + 15
    while time.monotonic() < end:
        reconcile(env)
        if status(env, "dep-c")["status"] == "verified":
            break
        time.sleep(0.05)
    for tid in ("dep-a", "dep-b", "dep-c"):
        st = status(env, tid)
        assert st["status"] == "verified" and len(st["runs"]) == 1


@pytest.mark.parametrize("terminal", ["archived", "exhausted"])
def test_terminal_upstream_parks_downstream_without_starting(tmp_path, env, setup, terminal):
    setup("upstream", verifier={"command": "false", "freeze": []})
    setup("downstream", depends_on=["upstream"])
    assert dispatch(env, "downstream")["result"] == "waiting_deps"
    if terminal == "archived":
        assert yg(env, "archive", "upstream", "--as", "owner", "--reason", "withdrawn")[0] == 0
    else:
        dispatch(env, "upstream")
        wait_exit(env, "upstream")
        assert yg(env, "close", "upstream", "--as", "owner")[0] == 1
    assert status(env, "upstream")["status"] == terminal
    reconcile(env)
    st = status(env, "downstream")
    assert st["status"] == "parked" and not st["runs"][0].get("started_at")
    inbox = json.loads(yg(env, "inbox", "--json")[1])
    item = next(i for i in inbox if i["task_id"] == "downstream")
    assert "上游 upstream 已终止" in item["question"]
    reconcile(env)
    assert len(status(env, "downstream")["runs"]) == 1
    assert yg(env, "answer", "downstream", "--as", "human", "--text", "retry")[0] == 0
    reconcile(env)
    reconcile(env)
    assert status(env, "downstream")["status"] == "parked"  # an answer cannot bypass dependencies


def test_retryable_upstream_failure_keeps_waiting(tmp_path, env, setup):
    flag = tmp_path / "approved"
    setup("upstream", budget={"max_attempts": 2}, verifier={"command": f"test -f {flag}", "freeze": []})
    setup("downstream", depends_on=["upstream"])
    dispatch(env, "downstream")
    dispatch(env, "upstream")
    wait_exit(env, "upstream")
    assert yg(env, "close", "upstream", "--as", "owner")[0] == 1
    assert status(env, "upstream")["status"] == "failed"
    reconcile(env)
    assert status(env, "downstream")["status"] == "waiting_deps"
    flag.touch()
    dispatch(env, "upstream")
    wait_exit(env, "upstream")
    assert yg(env, "close", "upstream", "--as", "owner")[0] == 0
    reconcile(env)
    assert wait_exit(env, "downstream")["runs"][0]["state"] == "exited"


@pytest.mark.parametrize("dependencies", [["bad-deps"], ["missing-task"], "upstream", [3], None])
def test_invalid_dependencies_are_rejected(tmp_path, env, dependencies):
    sp = make_task(tmp_path, task_id="bad-deps")
    spec = json.loads(sp.read_text())
    spec["depends_on"] = dependencies
    sp.write_text(json.dumps(spec))
    rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
    assert rc == 2 and "depend" in err.lower()
    assert not (Path(env["YARDGOAT_HOME"]) / "tasks/bad-deps/task.json").exists()


def test_cycle_in_accepted_dependency_graph_is_rejected(tmp_path, env, setup):
    setup("cycle-a")
    setup("cycle-b", depends_on=["cycle-a"])
    # Pre-M2 accepted specs could contain unchecked dependencies. Seed such an authoritative
    # legacy spec; new submissions must traverse it, not just check their direct dependencies.
    root = Path(env["YARDGOAT_HOME"])
    auth_path = root / "frozen/cycle-a.json"
    auth = json.loads(auth_path.read_text())
    auth["spec"]["depends_on"] = ["cycle-c"]
    auth_path.write_text(json.dumps(auth))
    (root / "tasks/cycle-a/spec.json").write_text(json.dumps(auth["spec"]))
    sp = make_task(tmp_path / "cycle-c", task_id="cycle-c")
    spec = json.loads(sp.read_text())
    spec["depends_on"] = ["cycle-b"]
    sp.write_text(json.dumps(spec))
    rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
    assert rc == 2 and "cycl" in err.lower()


def test_waiting_uses_no_capacity_or_attempt_and_supports_takeover_cancel(tmp_path, env, setup):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"max_concurrent": 1}))
    setup("upstream")
    setup("waiting", depends_on=["upstream"])
    assert dispatch(env, "waiting")["result"] == "waiting_deps"
    rc, out, _ = yg(env, "dispatch", "waiting", "--as", "owner")
    assert rc == 3 and json.loads(out)["result"] == "already-waiting_deps"
    assert yg(env, "takeover", "waiting", "--from", "owner", "--as", "new-owner")[0] == 0
    assert status(env, "waiting")["owner_history"][0]["run_in_flight"] == "r1"
    assert yg(env, "cancel", "waiting", "--as", "owner")[0] == 3
    assert yg(env, "cancel", "waiting", "--as", "new-owner")[0] == 0
    assert yg(env, "dispatch", "waiting", "--as", "new-owner")[0] == 0
    assert dispatch(env, "upstream")["result"] == "dispatched"
    wait_exit(env, "upstream")
    assert yg(env, "close", "upstream", "--as", "owner")[0] == 0
    reconcile(env)
    st = wait_exit(env, "waiting")
    assert [r["state"] for r in st["runs"]] == ["cancelled", "exited"]
    assert yg(env, "close", "waiting", "--as", "new-owner")[0] == 0


def test_released_dependency_joins_fifo_after_already_queued_work(tmp_path, env, setup):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"max_concurrent": 1}))
    setup("upstream", hold=True)
    setup("waiting", hold=True, depends_on=["upstream"])
    setup("eligible", hold=True)
    dispatch(env, "waiting")
    dispatch(env, "upstream")
    dispatch(env, "eligible")
    # Give the queued records distinct timestamps without a wall-clock sleep. The dependency
    # wait was dispatched first, but only becomes eligible after the existing queue entry.
    from yardgoat.store import TaskDir
    for tid, queued_at in (("waiting", "2000-01-01"), ("eligible", "2000-01-02")):
        with TaskDir(tid).locked() as box:
            box["state"]["runs"][-1]["queued_at"] = queued_at
    (tmp_path / "upstream.release").touch()
    wait_exit(env, "upstream")
    assert yg(env, "close", "upstream", "--as", "owner")[0] == 0
    reconcile(env)
    assert status(env, "eligible")["status"] == "running"
    assert status(env, "waiting")["status"] == "queued"


def test_all_upstreams_are_required_and_waiting_takes_no_pool_station(tmp_path, env, setup):
    from yardgoat import pool
    cfg = {"pools": {"p": {"host": "shell", "command": "true", "root": str(tmp_path), "max_stations": 1}}}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    setup("upstream-a")
    setup("upstream-b")
    setup("downstream", depends_on=["upstream-a", "upstream-b"], adapter={"kind": "pool", "pool": "p"})
    assert dispatch(env, "downstream")["result"] == "waiting_deps"
    reconcile(env)
    assert not list((pool.pool_dir("p") / "stations").glob("*.json"))
    dispatch(env, "upstream-a")
    wait_exit(env, "upstream-a")
    assert yg(env, "close", "upstream-a", "--as", "owner")[0] == 0
    reconcile(env)
    assert status(env, "downstream")["runs"][0]["waiting_for"] == ["upstream-b"]
    assert not list((pool.pool_dir("p") / "stations").glob("*.json"))


def configure_adapter(tmp_path, env, kind):
    if kind == "file-pool":
        Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"adapters": {"file-pool": {"root": str(tmp_path / "fleet")}}}))
        return {"kind": kind, "pool": "external"}
    if kind == "plugin":
        plugin = tmp_path / "fake.py"
        plugin.write_text('''
class Fake:
    def submit(self, td, run_id, spec, cfg):
        return {"mode": "plugin", "pid": None}
    def poll(self, td, run, spec, cfg):
        return None
ADAPTERS = {"plugin": Fake}
''')
        Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"plugins": [str(plugin)]}))
    return {"kind": kind}


@pytest.mark.parametrize("kind", ["direct", "file-pool", "plugin"])
def test_overlapping_scopes_queue_then_release_in_same_run(tmp_path, env, setup, kind):
    adapter = configure_adapter(tmp_path, env, kind)
    wd = tmp_path / "shared"
    setup("writer-a", hold=True, workdir=str(wd), scope=["src/**"], adapter=adapter)
    setup("writer-b", hold=True, workdir=str(wd), scope=["src/new/file.py"], adapter=adapter)
    dispatch(env, "writer-a")
    assert dispatch(env, "writer-b")["result"] == "queued"
    assert status(env, "writer-a")["status"] == "running"
    assert status(env, "writer-b")["status"] == "queued"
    assert "撞了 writer-a" in yg(env, "status", "writer-b")[1]
    assert str(wd / "src") in yg(env, "status", "writer-b")[1]
    assert "writer-a" in json.dumps(reconcile(env))
    if kind == "direct":
        (tmp_path / "writer-a.release").touch()
        wait_exit(env, "writer-a")
    elif kind == "file-pool":
        done = tmp_path / "fleet/external/done"
        done.mkdir()
        (tmp_path / "fleet/external/pending/writer-a-r1.md").rename(done / "writer-a-r1.md")
        assert not (tmp_path / "fleet/external/pending/writer-b-r1.md").exists()
    else:
        from yardgoat.runner import record_outcome
        from yardgoat.store import TaskDir
        record_outcome(TaskDir("writer-a"), "r1", {"state": "exited", "exit_code": 0})
    reconcile(env)
    st = status(env, "writer-b")
    assert st["status"] == "running" and len(st["runs"]) == 1
    assert not st["runs"][0].get("scope_conflicts")


@pytest.mark.parametrize("left,right", [(["src/a/**"], ["src/b/**"]), (["src"], ["src-other"]),
                                       ([], ["src"]), (["src"], [])])
def test_disjoint_subdirectories_and_unspecified_scope_can_run_together(tmp_path, env, setup, left, right):
    adapter = configure_adapter(tmp_path, env, "plugin")
    for tid, scope in (("writer-a", left), ("writer-b", right)):
        setup(tid, workdir=str(tmp_path / "shared"), scope=scope, adapter=adapter)
        assert dispatch(env, tid)["result"] == "dispatched"
    assert all(status(env, tid)["status"] == "running" for tid in ("writer-a", "writer-b"))


@pytest.mark.parametrize("scopes", [(["src/new.py"], ["src"]), (["src/*.py"], ["src/future.py"]),
                                   (["src/a?/x"], ["src/ab/y"]), (["src/[ab]/x"], ["src/a/y"])])
def test_scopes_reserve_ancestors_and_globs_before_files_exist(tmp_path, env, setup, scopes):
    adapter = configure_adapter(tmp_path, env, "plugin")
    for tid, scope in zip(("writer-a", "writer-b"), scopes):
        setup(tid, workdir=str(tmp_path / "shared"), scope=scope, adapter=adapter)
    dispatch(env, "writer-a")
    assert dispatch(env, "writer-b")["result"] == "queued"


def test_canonical_paths_conflict_across_workdir_aliases(tmp_path, env, setup):
    adapter = configure_adapter(tmp_path, env, "plugin")
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    setup("writer-a", workdir=str(real), scope=["src"], adapter=adapter)
    setup("writer-b", workdir=str(alias / "src"), scope=["file.py"], adapter=adapter)
    dispatch(env, "writer-a")
    assert dispatch(env, "writer-b")["result"] == "queued"


@pytest.mark.parametrize("outcome", ["exited", "blocked", "lost", "launch_failed", "provider_refused"])
def test_every_worker_outcome_releases_its_scope(tmp_path, env, setup, outcome):
    adapter = configure_adapter(tmp_path, env, "plugin")
    for tid in ("writer-a", "writer-b"):
        setup(tid, workdir=str(tmp_path / "shared"), scope=["src"], adapter=adapter)
    dispatch(env, "writer-a")
    assert dispatch(env, "writer-b")["result"] == "queued"
    from yardgoat.runner import record_outcome
    from yardgoat.store import TaskDir
    record_outcome(TaskDir("writer-a"), "r1", {"state": outcome, "exit_code": 0, "question": "input?"})
    reconcile(env)
    assert status(env, "writer-b")["status"] == "running"


def pool_stations(tmp_path, env, names=("p",)):
    from yardgoat import pool
    cfg = {"pools": {n: {"host": "shell", "command": "true", "root": str(tmp_path), "max_stations": 0} for n in names}}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    for name in names:
        for number in (1, 2):
            station = f"{name}-st-{number}"
            pool.update_station(name, station, station=station, pool=name, state="running", tasks=[], pid=os.getpid())


def test_pool_claim_skips_overlap_and_reconcile_releases_it(tmp_path, env, setup):
    pool_stations(tmp_path, env)
    for tid, scope in (("card-a", ["src"]), ("card-b", ["src/f.py"]), ("card-c", ["docs"])):
        setup(tid, workdir=str(tmp_path / "shared"), scope=scope, adapter={"kind": "pool", "pool": "p"})
        dispatch(env, tid)
    assert json.loads(yg(env, "claim", "--pool", "p", "--station", "p-st-1")[1])["task_id"] == "card-a"
    assert json.loads(yg(env, "claim", "--pool", "p", "--station", "p-st-2")[1])["task_id"] == "card-c"
    assert status(env, "card-b")["status"] == "queued"
    assert "card-a" in json.dumps(reconcile(env))
    assert yg(env, "finish", "card-a", "--run", "r1", "--station", "p-st-1")[0] == 0
    reconcile(env)
    assert json.loads(yg(env, "claim", "--pool", "p", "--station", "p-st-1")[1])["task_id"] == "card-b"


def test_pool_dependency_wait_does_not_start_a_station(tmp_path, env, setup):
    pool_stations(tmp_path, env)
    setup("upstream")
    setup("pool-child", depends_on=["upstream"], adapter={"kind": "pool", "pool": "p"})
    assert dispatch(env, "pool-child")["result"] == "waiting_deps"
    assert json.loads(yg(env, "claim", "--pool", "p", "--station", "p-st-1")[1])["result"] == "EMPTY"
    dispatch(env, "upstream")
    wait_exit(env, "upstream")
    assert yg(env, "close", "upstream", "--as", "owner")[0] == 0
    reconcile(env)
    assert json.loads(yg(env, "claim", "--pool", "p", "--station", "p-st-1")[1])["task_id"] == "pool-child"


def test_claim_obeys_machine_schedule_lock_and_cross_pool_exclusion(tmp_path, env, setup):
    pool_stations(tmp_path, env, ("one", "two"))
    for name in ("one", "two"):
        setup(f"card-{name}", workdir=str(tmp_path / "shared"), scope=["src"], adapter={"kind": "pool", "pool": name})
        dispatch(env, f"card-{name}")
    lockpath = Path(env["YARDGOAT_HOME"]) / ".schedule.lock"
    with lockpath.open("a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        procs = [subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "claim", "--pool", n, "--station", f"{n}-st-1"],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in ("one", "two")]
        try:
            time.sleep(0.3)
            assert all(p.poll() is None for p in procs), "claim bypassed the schedule lock"
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
            outputs = [p.communicate(timeout=10) for p in procs]
    assert sorted(json.loads(out)["result"] for out, err in outputs) == ["CLAIMED", "EMPTY"]


def test_concurrent_direct_dispatches_serialize_overlapping_scopes(tmp_path, env, setup):
    for tid in ("writer-a", "writer-b"):
        setup(tid, hold=True, workdir=str(tmp_path / "shared"), scope=["src"])
    procs = [subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "dispatch", tid, "--as", "owner"],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for tid in ("writer-a", "writer-b")]
    results = [json.loads(p.communicate(timeout=10)[0])["result"] for p in procs]
    assert sorted(results) == ["dispatched", "queued"]
