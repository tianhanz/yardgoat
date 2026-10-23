"""P1 contract tests: lane allowlist, park/answer/resume, file-pool adapter, lost-run reconcile. No model calls."""
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401  (fixtures)

sys.path.insert(0, SRC)


def write_spec(spec_path, **changes):
    s = json.loads(spec_path.read_text()); s.update(changes); spec_path.write_text(json.dumps(s))


def test_lane_outside_allowlist_is_refused(tmp_path, env):
    spec = make_task(tmp_path)
    write_spec(spec, executor={"host": "shell", "command": "true", "lane": "pricey"}, allowed_lanes=["cheap"])
    yg(env, "submit", str(spec), "--as", "A")
    rc, _, err = yg(env, "dispatch", "t-demo", "--as", "A")
    assert rc == 3 and "allowed_lanes" in err


def test_forbidden_lane_in_local_config_is_refused(tmp_path, env):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"forbidden_lanes": ["personal"]}))
    spec = make_task(tmp_path)
    write_spec(spec, executor={"host": "shell", "command": "true", "lane": "personal"})
    yg(env, "submit", str(spec), "--as", "A")
    rc, _, err = yg(env, "dispatch", "t-demo", "--as", "A")
    assert rc == 3 and "forbidden" in err


def test_blocked_worker_parks_then_answer_resumes_with_the_answer(tmp_path, env):
    home = env["YARDGOAT_HOME"]
    worker = (f"if ls {home}/tasks/t-demo/answers/*.md >/dev/null 2>&1; then echo done > out/result.txt; "
              f"else echo 'Which colour should the plot use?' > out/BLOCKED.md; fi")
    spec = make_task(tmp_path, worker=worker, attempts=1)
    yg(env, "submit", str(spec), "--as", "A")
    yg(env, "dispatch", "t-demo", "--as", "A")
    st = wait_exit(env, "t-demo")
    assert st["status"] == "parked" and "colour" in st["question"]["text"]
    inbox = json.loads(yg(env, "inbox", "--json")[1])
    assert inbox[0]["task_id"] == "t-demo" and "colour" in inbox[0]["question"]
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 3          # parked: must be answered first
    assert yg(env, "answer", "t-demo", "--as", "human", "--text", "blue")[0] == 0
    out = json.loads(yg(env, "reconcile")[1])
    assert out["actions"] == [{"task_id": "t-demo", "resumed": True}]  # blocked run did not use the 1-attempt budget
    wait_exit(env, "t-demo")
    rc, o, _ = yg(env, "close", "t-demo", "--as", "A")
    assert rc == 0 and json.loads(o)["verdict"] == "PASS"
    from yardgoat import runner
    from yardgoat.store import TaskDir
    os.environ["YARDGOAT_HOME"] = home
    td = TaskDir("t-demo", root=Path(home))
    assert "blue" in runner.prompt_text(td, td.spec())


def _pool_task(tmp_path, env):
    pool = tmp_path / "pool"
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(
        {"adapters": {"file-pool": {"root": str(pool), "pools": {"p1": {"lane": "cheap", "host": "claude"}}}}}))
    spec = make_task(tmp_path)
    write_spec(spec, adapter={"kind": "file-pool", "pool": "p1", "worker_verify": "test -s out/result.txt"},
               allowed_lanes=["cheap"])
    yg(env, "submit", str(spec), "--as", "A")
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 0
    return pool


def test_file_pool_adapter_submits_card_and_observes_done(tmp_path, env):
    pool = _pool_task(tmp_path, env)
    card = pool / "p1/pending/t-demo-r1.md"
    assert card.exists() and "verify: test -s out/result.txt" in card.read_text()
    assert json.loads(yg(env, "reconcile")[1])["actions"] == []           # still pending
    (pool / "p1/done").mkdir(parents=True)
    (tmp_path / "work/out/result.txt").write_text("done\n")              # the pool's own worker did the work
    card.rename(pool / "p1/done/t-demo-r1.worker7.md")
    assert json.loads(yg(env, "reconcile")[1])["actions"][0]["observed"] == "exited"
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    rec = st["runs"][0]["receipt"]
    assert rec["adapter"] == "file-pool" and rec["worker"] == "worker7" and rec["lane"] == "cheap"
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 0


def test_file_pool_parked_card_becomes_a_question(tmp_path, env):
    pool = _pool_task(tmp_path, env)
    (pool / "p1/parked").mkdir(parents=True)
    src = pool / "p1/pending/t-demo-r1.md"
    src.rename(pool / "p1/parked/t-demo-r1.md")
    (pool / "p1/parked/t-demo-r1.md").write_text(src.name + "\n\n## 待确认\nIs 2 cores enough?\n")
    yg(env, "reconcile")
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    assert st["status"] == "parked" and "2 cores" in st["question"]["text"]


def test_reconcile_marks_a_vanished_direct_run_lost_and_allows_redispatch(tmp_path, env):
    spec = make_task(tmp_path, worker="sleep 30")
    yg(env, "submit", str(spec), "--as", "A")
    yg(env, "dispatch", "t-demo", "--as", "A")
    time.sleep(1.5)
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    for pid in {st["runs"][0].get("worker_pid"), st["runs"][0].get("pid")} - {None}:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    assert json.loads(yg(env, "reconcile")[1])["actions"][0]["observed"] == "lost"
    assert json.loads(yg(env, "status", "t-demo", "--json")[1])["runs"][0]["receipt"]["status"] == "lost"
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 0


def test_launcher_failure_does_not_use_the_attempt_budget(tmp_path, env):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(
        {"launcher": ["bash", "-c", "echo 'unknown lane' >&2; exit 3", "--"], "launcher_failure_codes": [3]}))
    spec = make_task(tmp_path, attempts=1)
    write_spec(spec, executor={"host": "codex", "lane": "nope"})
    yg(env, "submit", str(spec), "--as", "A")
    for _ in range(2):  # twice, with a budget of one: neither launch failure counts
        assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 0
        st = wait_exit(env, "t-demo")
        assert st["runs"][-1]["state"] == "launch_failed" and st["status"] == "accepted"
        assert "unknown lane" in st["runs"][-1]["receipt"]["reason"]
