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
