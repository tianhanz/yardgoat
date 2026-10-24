"""Capacity: resource guard, account spreading, project preamble, workdir == pool root."""
import json
import os
import sys
from pathlib import Path

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401
from test_pool import pool_cfg, pool_task  # noqa: F401

sys.path.insert(0, SRC)


def setenv(env):
    os.environ["YARDGOAT_HOME"] = env["YARDGOAT_HOME"]; os.environ["YARDGOAT_CONFIG"] = env["YARDGOAT_CONFIG"]


def test_low_memory_guard_queues_direct_runs_and_starts_no_station(tmp_path, env):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"min_free_gb": 10**6}))
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A")
    assert json.loads(yg(env, "dispatch", "t-demo", "--as", "A")[1])["result"] == "queued"
    pool_cfg(env, tmp_path); c = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text()); c["min_free_gb"] = 10**6
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(c))
    pool_task(tmp_path, env, "t-pg")
    assert json.loads(yg(env, "dispatch", "t-pg", "--as", "A")[1])["stations_started"] == []


def test_stations_alternate_accounts_and_carry_the_preamble(tmp_path, env):
    setenv(env)
    from yardgoat import pool
    c = {"host": "codex", "lane": "codex", "accts": ["a", "b"], "root": str(tmp_path), "preamble": "PROJECT RULES: read CONTRACT.md"}
    cfg = {"launcher": ["L", "--acct", "{acct}", "--"], "pools": {"p": c}, "tmux_session": None}
    os.environ["YARDGOAT_NO_TMUX"] = "1"
    for n, acct in ((1, "a"), (2, "b"), (3, "a")):
        pool.update_station("p", f"p-st-{n}", station=f"p-st-{n}", state="exited", acct=acct, tasks=[])
        argv = pool.station_argv("p", f"p-st-{n}", c, cfg)
        assert argv[2] == acct and argv[-1].startswith("PROJECT RULES")


def test_workdir_equal_to_the_pool_root_is_allowed(tmp_path, env):
    pool_cfg(env, tmp_path)
    spec = make_task(tmp_path / "s", task_id="t-root")
    s = json.loads(spec.read_text()); s.update(workdir=str(tmp_path / "root"), adapter={"kind": "pool", "pool": "p"}); spec.write_text(json.dumps(s))
    yg(env, "submit", str(spec), "--as", "A")
    assert json.loads(yg(env, "dispatch", "t-root", "--as", "A")[1])["result"] == "queued"


def test_spawn_per_queued_keeps_a_small_batch_on_one_station(tmp_path, env):
    pool_cfg(env, tmp_path, max_stations=5, spawn_per_queued=3)
    c = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text()); c["pools"]["p"]["command"] = "sleep 5"
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(c))
    started = []
    for t in ("t-s1", "t-s2", "t-s3", "t-s4"):
        pool_task(tmp_path, env, t)
        started += json.loads(yg(env, "dispatch", t, "--as", "A")[1])["stations_started"]
    assert len(started) == 2          # ceil(4 / 3) = 2 stations for 4 waiting cards, not 4


def test_a_busy_station_does_not_hold_back_a_new_card(tmp_path, env):
    pool_cfg(env, tmp_path, max_stations=3, spawn_per_queued=2)
    setenv(env)
    from yardgoat import pool
    pool.update_station("p", "p-st-1", station="p-st-1", pool="p", state="running", tasks=["x/r1"], pid=os.getpid(),
                        current={"task_id": "x", "run_id": "r1", "deadline": 9e18})
    c = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text()); c["pools"]["p"]["command"] = "sleep 3"
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(c))
    pool_task(tmp_path, env, "t-new")
    assert json.loads(yg(env, "dispatch", "t-new", "--as", "A")[1])["stations_started"] == ["p-st-2"]


def test_quick_passes_depends_on_and_auto_close_into_the_spec(tmp_path, env):
    wd = tmp_path / "wd"; wd.mkdir()
    assert yg(env, "quick", "a", "--verify", "true", "--as", "A", "--id", "q-up", "--workdir", str(wd), "--no-dispatch")[0] == 0
    rc, _, err = yg(env, "quick", "b", "--verify", "true", "--as", "A", "--id", "q-down", "--workdir", str(wd),
                    "--depends-on", "q-up", "--auto-close", "--no-dispatch")
    assert rc == 0, err
    spec = json.loads((Path(env["YARDGOAT_HOME"]) / "cards/q-down/spec.json").read_text())
    assert spec["depends_on"] == ["q-up"] and spec["auto_close"] is True


def test_tmux_socket_comes_from_config_not_the_callers_environment(env):
    setenv(env)
    from yardgoat import runner
    assert runner.tmux_argv({"tmux_socket": "/tmp/some/sock"}) == ["tmux", "-S", "/tmp/some/sock"]
    assert runner.tmux_argv({"tmux_socket": "/tmp/s", "tmux_bin": "/opt/tmux"}) == ["/opt/tmux", "-S", "/tmp/s"]
    os.environ.pop("TMUX", None)
    assert runner.tmux_argv({}) == ["tmux"]


def test_doctor_cache_is_not_reused_under_a_different_launch_environment(tmp_path, env):
    """2026-10-24: a check passed in an interactive shell was trusted by cron, where tmux could not be reached."""
    setenv(env)
    from yardgoat import doctor, runner
    cfg = {"tmux_socket": "/tmp/a"}
    fp_a = runner.launch_env_fingerprint(cfg)
    fp_b = runner.launch_env_fingerprint({"tmux_socket": "/tmp/b"})
    assert fp_a != fp_b
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = "/nonexistent:" + old
    try:
        assert runner.launch_env_fingerprint(cfg) != fp_a
    finally:
        os.environ["PATH"] = old


def test_a_failed_station_launch_backs_the_pool_off(tmp_path, env):
    setenv(env)
    from yardgoat import pool
    os.environ.pop("YARDGOAT_NO_TMUX", None)
    try:
        cfg = {"tmux_session": "no-such-session", "tmux_socket": str(tmp_path / "no-server"),
               "pools": {"p": {"host": "shell", "command": "true", "root": str(tmp_path)}}}
        st = pool.start_station("p", cfg["pools"]["p"], cfg)
        assert pool.get_station("p", st)["state"] == "launch_failed"
        b = pool.provider_backoff("p", cfg)
        assert b and b["status"] == "launch_failed"
    finally:
        os.environ["YARDGOAT_NO_TMUX"] = "1"
