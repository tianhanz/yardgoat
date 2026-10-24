"""Regression tests for the 2026-10-24 codex review (findings F1–F15). Each test is the reviewer's failure scenario."""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401

sys.path.insert(0, SRC)


def setj(path, **kw):
    s = json.loads(path.read_text()); s.update(kw); path.write_text(json.dumps(s)); return s


def task_json(env, tid):
    return Path(env["YARDGOAT_HOME"]) / "tasks" / tid / "task.json"


def inproc(env):
    os.environ["YARDGOAT_HOME"] = env["YARDGOAT_HOME"]; os.environ["YARDGOAT_CONFIG"] = env["YARDGOAT_CONFIG"]
    from yardgoat import runner
    from yardgoat.store import TaskDir
    return runner, TaskDir


def test_F1_late_duplicate_outcome_cannot_undo_a_verification(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 0
    runner, TaskDir = inproc(env)
    assert runner.record_outcome(TaskDir("t-demo"), "r1", {"state": "exited", "exit_code": 0}) is None
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    assert st["status"] == "verified" and st["runs"][0]["receipt"]["status"] == "passed"


def test_F2_timeout_ends_the_whole_process_group(tmp_path, env):
    spec = make_task(tmp_path, worker="sleep 300 & echo $! > out/child.pid; wait")
    s = setj(spec); s["budget"]["max_wall_min"] = 0.05; spec.write_text(json.dumps(s))
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A")
    st = wait_exit(env, "t-demo", timeout=40)
    assert st["runs"][0]["receipt"]["timed_out"] is True
    child = int((tmp_path / "work/out/child.pid").read_text())
    assert not Path(f"/proc/{child}").exists() or Path(f"/proc/{child}/stat").read_text().split(") ")[1][0] == "Z"


def test_F3_two_concurrent_dispatches_respect_the_cap(tmp_path, env):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"max_concurrent": 1}))
    for t in ("t-aaa", "t-bbb"):
        yg(env, "submit", str(make_task(tmp_path / t, task_id=t, worker="sleep 3")), "--as", "A")
    ps = [subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "dispatch", t, "--as", "A"], env=env,
                           stdout=subprocess.PIPE, text=True) for t in ("t-aaa", "t-bbb")]
    results = sorted(json.loads(p.communicate()[0])["result"] for p in ps)
    assert results == ["dispatched", "queued"]


def test_F4_reconcile_does_not_resurrect_an_archived_task(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A")
    assert yg(env, "park", "t-demo", "--as", "A", "--question", "q?")[0] == 0
    assert yg(env, "answer", "t-demo", "--as", "H", "--text", "a")[0] == 0
    assert yg(env, "archive", "t-demo", "--as", "A", "--reason", "dropped")[0] == 0
    yg(env, "reconcile")
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    assert st["status"] == "archived" and st["runs"] == []
    # the race itself: a reconcile that read "answered" before the archive now tries to resume — must re-check
    _, TaskDir = inproc(env)
    from yardgoat import cli
    assert cli.do_dispatch_locked(TaskDir("t-demo"), "A", "resumed by reconcile after an answer") == 3
    assert json.loads(yg(env, "status", "t-demo", "--json")[1])["status"] == "archived"


def test_F5_missing_workdir_and_failing_source_are_launch_failures(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A")
    os.rename(tmp_path / "work", tmp_path / "gone")
    rc, _, err = yg(env, "dispatch", "t-demo", "--as", "A")
    assert rc == 3 and "workdir does not exist" in err
    os.rename(tmp_path / "gone", tmp_path / "work")
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"source": [str(tmp_path / "no-such-env.sh")]}))
    yg(env, "dispatch", "t-demo", "--as", "A")
    st = wait_exit(env, "t-demo")
    assert st["runs"][-1]["state"] == "launch_failed" and "source" in st["runs"][-1]["receipt"]["reason"]


PLUGIN = '''
class Remote:
    kind = "remote"
    def submit(self, td, run_id, spec, cfg):
        open(cfg["count_file"], "a").write("x")
        return {"mode": "remote", "pid": None, "window": None}
    def poll(self, td, run, spec, cfg):
        return None
ADAPTERS = {"remote": Remote}
'''


def test_F7_plugin_runs_without_local_pid_are_not_resubmitted(tmp_path, env):
    (tmp_path / "plug.py").write_text(PLUGIN)
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"plugins": [str(tmp_path / "plug.py")], "count_file": str(tmp_path / "n")}))
    spec = make_task(tmp_path); setj(spec, adapter={"kind": "remote"})
    yg(env, "submit", str(spec), "--as", "A")
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 0
    rc, out, _ = yg(env, "dispatch", "t-demo", "--as", "A")
    assert rc == 3 and json.loads(out)["result"] == "already-running"
    assert (tmp_path / "n").read_text() == "x"


def test_F8_failed_submit_that_landed_is_tracked_not_resubmitted(tmp_path, env):
    pool = tmp_path / "pool"
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"adapters": {"file-pool": {"root": str(pool),
        "add_cmd": ["bash", "-c", f"mkdir -p {pool}/p1/pending && echo card > {pool}/p1/pending/{{pool_task_id}}.md; exit 1"]}}}))
    spec = make_task(tmp_path); setj(spec, adapter={"kind": "file-pool", "pool": "p1"})
    yg(env, "submit", str(spec), "--as", "A")
    yg(env, "dispatch", "t-demo", "--as", "A")
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    assert st["runs"][-1]["state"] == "running" and "submit_error" in st["runs"][-1]
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 3
    assert len(list((pool / "p1/pending").glob("*.md"))) == 1


def test_F8_failed_submit_of_unknown_outcome_blocks_resubmission_and_shows_in_inbox(tmp_path, env):
    pool = tmp_path / "pool"
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"adapters": {"file-pool": {"root": str(pool),
        "add_cmd": ["bash", "-c", "exit 1"]}}}))
    spec = make_task(tmp_path); setj(spec, adapter={"kind": "file-pool", "pool": "p1"})
    yg(env, "submit", str(spec), "--as", "A")
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 2
    rc, out, _ = yg(env, "dispatch", "t-demo", "--as", "A")
    assert rc == 3 and json.loads(out)["result"] == "already-submit_unknown"
    assert json.loads(yg(env, "inbox", "--json")[1])[0]["status"] == "submit_unknown"


def test_F10_receipt_without_committed_state_is_replayed(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    tj = task_json(env, "t-demo"); st = json.loads(tj.read_text())
    st["runs"][0]["state"] = "running"; st["status"] = "running"; tj.write_text(json.dumps(st))  # crash before state write
    acts = json.loads(yg(env, "reconcile")[1])["actions"]
    assert {"task_id": "t-demo", "run_id": "r1", "replayed_receipt": True} in acts
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 0


def test_F11_archived_task_can_neither_be_parked_nor_closed(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    assert yg(env, "archive", "t-demo", "--as", "A", "--reason", "x")[0] == 0
    assert yg(env, "park", "t-demo", "--as", "A", "--question", "q")[0] == 3
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 3


def test_F12_a_run_waiting_for_verification_cannot_be_parked(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    rc, _, err = yg(env, "park", "t-demo", "--as", "A", "--question", "q")
    assert rc == 3 and "close" in err


def test_F13_quick_with_a_taken_id_leaves_the_existing_task_untouched(tmp_path, env):
    wd = tmp_path / "wd"; wd.mkdir()
    assert yg(env, "quick", "first", "--verify", "true", "--as", "A", "--id", "q-demo", "--workdir", str(wd), "--no-dispatch")[0] == 0
    card = Path(env["YARDGOAT_HOME"]) / "cards/q-demo"
    before = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in card.iterdir()}
    assert yg(env, "quick", "second", "--verify", "false", "--as", "A", "--id", "q-demo", "--workdir", str(wd))[0] == 3
    assert {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in card.iterdir()} == before


def test_F14_crashed_verifier_shows_in_inbox_and_can_be_rerun(tmp_path, env):
    flag = tmp_path / "flag"
    spec = make_task(tmp_path, verify=f"test -f {flag} && exit 0 || exit 7")
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 2
    assert json.loads(yg(env, "inbox", "--json")[1])[0]["task_id"] == "t-demo"
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 3
    flag.write_text("")
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 0


def test_F15_bad_budget_is_rejected_at_submit(tmp_path, env):
    spec = make_task(tmp_path)
    s = json.loads(spec.read_text()); s["budget"]["max_wall_min"] = "oops"; spec.write_text(json.dumps(s))
    rc, _, err = yg(env, "submit", str(spec), "--as", "A")
    assert rc == 2 and "max_wall_min" in err
    s["budget"] = {"max_attempts": True}; spec.write_text(json.dumps(s))
    assert yg(env, "submit", str(spec), "--as", "A")[0] == 2
