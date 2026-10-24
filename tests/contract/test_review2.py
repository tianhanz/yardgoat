"""Regression tests for the 2026-10-24 second codex review (pool code), findings F1–F11."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401
from test_pool import STATION, pool_cfg, pool_task, stations, wait_all  # noqa: F401

sys.path.insert(0, SRC)


def setenv(env):
    os.environ["YARDGOAT_HOME"] = env["YARDGOAT_HOME"]; os.environ["YARDGOAT_CONFIG"] = env["YARDGOAT_CONFIG"]


def test_R2F1_rewriting_spec_json_cannot_swap_the_verifier(tmp_path, env):
    spec = make_task(tmp_path, verify="exit 1")
    yg(env, "submit", str(spec), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    sj = Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/spec.json"
    s = json.loads(sj.read_text()); s["verifier"]["command"] = "true"; sj.write_text(json.dumps(s))
    rc, _, err = yg(env, "close", "t-demo", "--as", "A")
    assert rc == 3 and "spec.json was altered" in err
    assert json.loads(yg(env, "status", "t-demo", "--json")[1])["status"] != "verified"


def test_R2F2_codex_station_gets_no_extra_writable_dirs(tmp_path, env):
    setenv(env)
    from yardgoat import pool
    argv = pool.station_argv("p", "p-st-1", {"host": "codex", "root": str(tmp_path)}, {})
    assert "--add-dir" not in argv and argv[argv.index("-C") + 1] == str(tmp_path)


def test_R2F3_late_finish_after_lost_is_refused_and_does_not_touch_the_new_run(tmp_path, env):
    pool_cfg(env, tmp_path, command="st=$0; python3 -m yardgoat.cli claim --pool p --station $st >/dev/null; sleep 30")
    pool_task(tmp_path, env, "t-late")
    yg(env, "dispatch", "t-late", "--as", "A")
    end = time.time() + 20
    while time.time() < end and json.loads(yg(env, "status", "t-late", "--json")[1])["runs"][-1]["state"] != "running":
        time.sleep(0.3)
    setenv(env)
    from yardgoat import runner
    from yardgoat.store import TaskDir
    from yardgoat import cli
    assert runner.record_outcome(TaskDir("t-late"), "r1", {"state": "lost", "reason": "test"})  # reconcile wins
    rc, out = cli.finish_card("t-late", "r1", "p-st-1")                                           # late finish
    assert rc == 3 and json.loads(yg(env, "status", "t-late", "--json")[1])["runs"][-1]["state"] == "lost"


def test_R2F4_cap_holds_across_pools_scheduled_concurrently(tmp_path, env):
    """Non-regression guard only. The reviewer's interleaving (A skips an empty pool, a card lands there, B starts it)
    needs a timing this test cannot force; on the pre-fix code it passes too. The fix is structural: one machine-wide
    schedule lock around every capacity check + start."""
    names = ["p1", "p2", "p3", "p4", "p5", "p6"]
    c = {"max_concurrent": 1, "pools": {n: {"host": "shell", "command": "sleep 6", "root": str(tmp_path / "root"), "max_stations": 1}
                                        for n in names}}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(c)); (tmp_path / "root").mkdir()
    for n in names:
        t = f"t-{n}"
        wd = tmp_path / "root" / t; (wd / "out").mkdir(parents=True)
        sp = make_task(tmp_path / f"s-{t}", task_id=t); s = json.loads(sp.read_text())
        s.update(workdir=str(wd), adapter={"kind": "pool", "pool": n}); sp.write_text(json.dumps(s))
        yg(env, "submit", str(sp), "--as", "A")
    ps = [subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "dispatch", f"t-{n}", "--as", "A"], env=env,
                           stdout=subprocess.PIPE, text=True) for n in names]
    outs = [json.loads(p.communicate()[0]) for p in ps]
    assert sum(len(o["stations_started"]) for o in outs) == 1


def test_R2F5_station_record_updates_merge_instead_of_overwriting(tmp_path, env):
    setenv(env)
    from yardgoat import pool
    pool.update_station("p", "p-st-9", state="running", tasks=[])
    pool.update_station("p", "p-st-9", state="exited", exit_code=0)      # child finishes first
    pool.update_station("p", "p-st-9", launcher_pid=12345)               # parent's late write
    rec = pool.get_station("p", "p-st-9")
    assert rec["state"] == "exited" and rec["exit_code"] == 0 and rec["launcher_pid"] == 12345


def test_R2F6_a_card_past_its_wall_time_is_cut_off(tmp_path, env):
    pool_cfg(env, tmp_path, command="st=$0; python3 -m yardgoat.cli claim --pool p --station $st >/dev/null; sleep 120")
    spec = pool_task(tmp_path, env, "t-slow")
    s = json.loads((Path(env["YARDGOAT_HOME"]) / "frozen/t-slow.json").read_text())
    # budget is frozen at submit: resubmit a fresh task with a short wall time instead
    wd = tmp_path / "root" / "t-slow2"; (wd / "out").mkdir(parents=True)
    sp = make_task(tmp_path / "s2", task_id="t-slow2"); j = json.loads(sp.read_text())
    j.update(workdir=str(wd), adapter={"kind": "pool", "pool": "p"}); j["budget"]["max_wall_min"] = 0.05; sp.write_text(json.dumps(j))
    yg(env, "submit", str(sp), "--as", "A"); yg(env, "dispatch", "t-slow2", "--as", "A")
    sts = wait_all(env, ["t-slow2"], timeout=40)
    rec = sts[0]["runs"][-1]["receipt"]
    assert rec["exit_code"] == 124 and rec["timed_out"] is True
    assert stations(env)[-1].get("timed_out") is True


def test_R2F8_auto_close_leaves_an_archived_task_alone(tmp_path, env):
    pool_cfg(env, tmp_path, command="st=$0; out=$(python3 -m yardgoat.cli claim --pool p --station $st); "
             "read tid rid <<<\"$(python3 -c \"import json,sys;d=json.loads(sys.argv[1]);print(d['task_id'],d['run_id'])\" \"$out\")\"; "
             "python3 -m yardgoat.cli finish $tid --run $rid --station $st >/dev/null")
    pool_task(tmp_path, env, "t-arch", auto_close=True)
    tj = Path(env["YARDGOAT_HOME"]) / "tasks/t-arch/task.json"
    yg(env, "dispatch", "t-arch", "--as", "A")
    end = time.time() + 20
    while time.time() < end and json.loads(tj.read_text())["runs"][-1]["state"] != "exited":
        time.sleep(0.2)
    assert yg(env, "archive", "t-arch", "--as", "A", "--reason", "stop")[0] == 0
    yg(env, "reconcile")
    assert json.loads(yg(env, "status", "t-arch", "--json")[1])["status"] == "archived"


def test_R2F9_an_exited_station_cannot_claim(tmp_path, env):
    pool_cfg(env, tmp_path); pool_task(tmp_path, env, "t-x")
    setenv(env)
    from yardgoat import cli, pool
    pool.update_station("p", "p-st-7", station="p-st-7", pool="p", state="exited", tasks=[])
    rc, out = cli.claim_card("p", "p-st-7")
    assert rc == 3 and out["result"] == "REFUSED"


def test_R2F10_a_station_holding_a_card_cannot_claim_another(tmp_path, env):
    pool_cfg(env, tmp_path); pool_task(tmp_path, env, "t-y"); pool_task(tmp_path, env, "t-z")
    for t in ("t-y", "t-z"):
        tj = Path(env["YARDGOAT_HOME"]) / f"tasks/{t}/task.json"   # queue without starting a station
        st = json.loads(tj.read_text())
        st["runs"].append({"run_id": "r1", "state": "queued", "dispatched_by": "A", "queued_at": "x" + t, "mode": "pool", "pool": "p"})
        st["status"] = "queued"; tj.write_text(json.dumps(st))
    setenv(env)
    from yardgoat import cli, pool
    pool.update_station("p", "p-st-8", station="p-st-8", pool="p", state="running", tasks=[], pid=os.getpid())
    assert cli.claim_card("p", "p-st-8")[1]["result"] == "CLAIMED"
    rc, out = cli.claim_card("p", "p-st-8")
    assert rc == 3 and "still holds" in out["reason"]


def test_R2F11_reconcile_prints_exactly_one_json_document_with_auto_close(tmp_path, env):
    pool_cfg(env, tmp_path)
    pool_task(tmp_path, env, "t-a1", auto_close=True); pool_task(tmp_path, env, "t-a2", auto_close=True)
    yg(env, "dispatch", "t-a1", "--as", "A"); yg(env, "dispatch", "t-a2", "--as", "A")
    end = time.time() + 30
    while time.time() < end:
        out = yg(env, "reconcile")[1]
        d = json.loads(out)  # must parse as one document every time
        sts = [json.loads(yg(env, "status", t, "--json")[1])["status"] for t in ("t-a1", "t-a2")]
        if sts == ["verified", "verified"]:
            break
        time.sleep(0.5)
    assert sts == ["verified", "verified"]
