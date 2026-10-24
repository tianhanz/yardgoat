"""P1 contract tests: lane allowlist, park/answer/resume, file-pool adapter, lost-run reconcile. No model calls."""
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
import subprocess

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


def test_archive_hides_a_dead_task_from_the_inbox_and_blocks_dispatch(tmp_path, env):
    spec = make_task(tmp_path, worker="true", attempts=1)
    yg(env, "submit", str(spec), "--as", "A")
    yg(env, "dispatch", "t-demo", "--as", "A"); wait_exit(env, "t-demo")
    yg(env, "close", "t-demo", "--as", "A")
    assert json.loads(yg(env, "inbox", "--json")[1])[0]["status"] == "exhausted"
    assert yg(env, "archive", "t-demo", "--as", "B", "--reason", "x")[0] == 3
    assert yg(env, "archive", "t-demo", "--as", "A", "--reason", "resubmitted as t-demo-b")[0] == 0
    assert json.loads(yg(env, "inbox", "--json")[1]) == []
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 3


def test_cap_queues_beyond_the_limit_and_reconcile_starts_it_when_a_slot_frees(tmp_path, env):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"max_concurrent": 1}))
    a = make_task(tmp_path / "a", task_id="t-aaa", worker="sleep 2; echo done > out/result.txt")
    b = make_task(tmp_path / "b", task_id="t-bbb")
    yg(env, "submit", str(a), "--as", "A"); yg(env, "submit", str(b), "--as", "A")
    assert json.loads(yg(env, "dispatch", "t-aaa", "--as", "A")[1])["result"] == "dispatched"
    assert json.loads(yg(env, "dispatch", "t-bbb", "--as", "A")[1])["result"] == "queued"
    rc, out, _ = yg(env, "dispatch", "t-bbb", "--as", "A")
    assert rc == 3 and json.loads(out)["result"] == "already-queued"          # queued counts as in flight
    assert json.loads(yg(env, "reconcile")[1])["actions"] == []               # slot still taken
    wait_exit(env, "t-aaa")
    acts = json.loads(yg(env, "reconcile")[1])["actions"]
    assert {"task_id": "t-bbb", "started_from_queue": True} in acts
    st = wait_exit(env, "t-bbb")
    assert len(st["runs"]) == 1 and st["runs"][0]["state"] == "exited"


def test_cancel_a_queued_run_does_not_use_the_budget(tmp_path, env):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"max_concurrent": 0}))
    spec = make_task(tmp_path, attempts=1)
    yg(env, "submit", str(spec), "--as", "A")
    assert json.loads(yg(env, "dispatch", "t-demo", "--as", "A")[1])["result"] == "queued"
    assert yg(env, "cancel", "t-demo", "--as", "B")[0] == 3
    assert yg(env, "cancel", "t-demo", "--as", "A")[0] == 0
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({}))
    assert json.loads(yg(env, "dispatch", "t-demo", "--as", "A")[1])["result"] == "dispatched"


def test_quick_writes_card_verify_and_spec_then_dispatches(tmp_path, env):
    wd = tmp_path / "wd"; wd.mkdir()
    rc, out, err = yg(env, "quick", "Write hello into out/h.txt", "--verify", "grep -q hello out/h.txt", "--as", "A",
                      "--id", "q-hello", "--host", "shell", "--workdir", str(wd))
    assert rc == 2 and "executor.command" in err      # host=shell needs a command; quick is for agent hosts
    rc, out, err = yg(env, "quick", "Write hello into out/h.txt", "--verify", "grep -q hello out/h.txt", "--as", "A",
                      "--id", "q-hello2", "--host", "codex", "--lane", "cheap", "--workdir", str(wd), "--no-dispatch")
    assert rc == 0, err
    card = Path(env["YARDGOAT_HOME"]) / "cards/q-hello2"
    assert "grep -q hello out/h.txt" in (card / "verify.sh").read_text()
    assert "out/BLOCKED.md" in (card / "CARD.md").read_text()
    spec = json.loads((card / "spec.json").read_text())
    assert spec["allowed_lanes"] == ["cheap"] and spec["workdir"] == str(wd)
    (wd / "out").mkdir(); (wd / "out/h.txt").write_text("hello\n")
    assert subprocess.run(spec["verifier"]["command"], shell=True, cwd=str(wd)).returncode == 0


def test_quick_into_a_file_pool(tmp_path, env):
    pool = tmp_path / "pool"
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"adapters": {"file-pool": {"root": str(pool),
        "pools": {"p1": {"lane": "cheap", "host": "claude", "workdir": str(tmp_path)}}}}}))
    rc, out, err = yg(env, "quick", "do the thing", "--verify", "true", "--as", "A", "--id", "q-pool", "--pool", "p1")
    assert rc == 0, err
    card = pool / "p1/pending/q-pool-r1.md"
    assert card.exists() and "verify: true" in card.read_text()
