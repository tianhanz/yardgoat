"""Regression tests for the third codex review (2026-10-24): defects introduced by the first two rounds of fixes."""
import json
import os
import signal
import sys
import time
from pathlib import Path

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401
from test_pool import pool_cfg, pool_task, wait_all  # noqa: F401

sys.path.insert(0, SRC)


def setenv(env):
    os.environ["YARDGOAT_HOME"] = env["YARDGOAT_HOME"]; os.environ["YARDGOAT_CONFIG"] = env["YARDGOAT_CONFIG"]


def test_R3F1_auto_close_runs_outside_the_schedule_lock(tmp_path, env):
    flag = tmp_path / "in-verifier"
    pool_cfg(env, tmp_path)
    pool_task(tmp_path, env, "t-slowv", auto_close=True)
    s = json.loads((Path(env["YARDGOAT_HOME"]) / "frozen/t-slowv.json").read_text())
    # replace the verifier by a slow one: resubmit under a new id with a sleeping verifier
    wd = tmp_path / "root" / "t-slowv2"; (wd / "out").mkdir(parents=True)
    sp = make_task(tmp_path / "sv", task_id="t-slowv2", verify=f"touch {flag}; sleep 4; test -s {wd}/out/result.txt")
    j = json.loads(sp.read_text()); j.update(workdir=str(wd), adapter={"kind": "pool", "pool": "p"}, auto_close=True)
    j["verifier"]["command"] = f"bash {tmp_path / 'sv/verify.sh'}"; sp.write_text(json.dumps(j))
    yg(env, "submit", str(sp), "--as", "A"); yg(env, "dispatch", "t-slowv2", "--as", "A")
    wait_all(env, ["t-slowv2"])
    import subprocess
    rec = subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "reconcile"], env=env, stdout=subprocess.PIPE, text=True)
    end = time.time() + 10
    while time.time() < end and not flag.exists():
        time.sleep(0.1)
    assert flag.exists()
    import fcntl
    with open(Path(env["YARDGOAT_HOME"]) / ".schedule.lock", "w") as lk:   # the lock must be free while verifying
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rec.communicate()


def test_R3F2_a_rejected_late_finish_does_not_clear_the_current_card(tmp_path, env):
    pool_cfg(env, tmp_path)
    for t in ("t-c1", "t-c2"):
        pool_task(tmp_path, env, t)
        tj = Path(env["YARDGOAT_HOME"]) / f"tasks/{t}/task.json"
        st = json.loads(tj.read_text())
        st["runs"].append({"run_id": "r1", "state": "queued", "dispatched_by": "A", "queued_at": "q" + t, "mode": "pool", "pool": "p"})
        st["status"] = "queued"; tj.write_text(json.dumps(st))
    setenv(env)
    from yardgoat import cli, pool
    pool.update_station("p", "p-st-5", station="p-st-5", pool="p", state="running", tasks=[], pid=os.getpid())
    assert cli.claim_card("p", "p-st-5")[1]["task_id"] == "t-c1"
    (tmp_path / "root/t-c1/out/result.txt").write_text("x")
    assert cli.finish_card("t-c1", "r1", "p-st-5")[0] == 0
    assert cli.claim_card("p", "p-st-5")[1]["task_id"] == "t-c2"
    assert cli.finish_card("t-c1", "r1", "p-st-5")[0] == 3            # late duplicate of the first card
    assert pool.get_station("p", "p-st-5")["current"]["task_id"] == "t-c2"


def test_R3F3_direct_run_with_a_live_worker_group_is_not_lost(tmp_path, env):
    spec = make_task(tmp_path, worker="sleep 20")
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A")
    time.sleep(1.5)
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    for pid in {st["runs"][0].get("worker_pid"), st["runs"][0].get("pid")} - {None}:   # kill only the runner
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    acts = json.loads(yg(env, "reconcile")[1])["actions"]
    assert not any(a.get("observed") == "lost" for a in acts)
    assert json.loads(yg(env, "status", "t-demo", "--json")[1])["runs"][0]["state"] == "running"
    os.killpg(st["runs"][0]["worker_pgid"], signal.SIGKILL)


def test_R3F6_an_existing_receipt_wins_over_a_later_outcome(tmp_path, env):
    home = tmp_path / "work"
    worker = "echo 'which file?' > out/BLOCKED.md"
    spec = make_task(tmp_path, worker=worker)
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    tj = Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/task.json"; st = json.loads(tj.read_text())
    st["runs"][0]["state"] = "running"; st["status"] = "running"; tj.write_text(json.dumps(st))   # crash before state write
    setenv(env)
    from yardgoat import runner
    from yardgoat.store import TaskDir
    runner.record_outcome(TaskDir("t-demo"), "r1", {"state": "lost", "reason": "late"})
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    assert st["status"] == "parked" and st["runs"][0]["receipt"]["status"] == "blocked"


def test_R3F7_replaying_a_receipt_keeps_an_archived_task_archived(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    tj = Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/task.json"; st = json.loads(tj.read_text())
    st["runs"][0]["state"] = "running"; st["status"] = "archived"; tj.write_text(json.dumps(st))
    yg(env, "reconcile")
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    assert st["status"] == "archived" and st["runs"][0]["state"] == "exited"


def test_R3F8_expired_mailbox_request_is_not_executed(tmp_path, env):
    pool_cfg(env, tmp_path); pool_task(tmp_path, env, "t-exp")
    tj = Path(env["YARDGOAT_HOME"]) / "tasks/t-exp/task.json"; st = json.loads(tj.read_text())
    st["runs"].append({"run_id": "r1", "state": "queued", "dispatched_by": "A", "queued_at": "x", "mode": "pool", "pool": "p"})
    st["status"] = "queued"; tj.write_text(json.dumps(st))
    setenv(env)
    from yardgoat import cli, pool
    pool.update_station("p", "p-st-6", station="p-st-6", pool="p", state="running", tasks=[], pid=os.getpid())
    mb = tmp_path / "mb"; mb.mkdir()
    (mb / "req-old.json").write_text(json.dumps({"op": "claim", "expires": time.time() - 1}))
    pool.serve_mailbox("p", "p-st-6", mb, cli)
    assert json.loads((mb / "resp-old.json").read_text())["out"]["result"] == "REFUSED"
    assert json.loads(tj.read_text())["runs"][-1]["state"] == "queued"
