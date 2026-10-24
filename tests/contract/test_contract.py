"""Contract tests. They use host=shell with a fake worker, so no model is ever called."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.fixture
def env(tmp_path):
    e = dict(os.environ, YARDGOAT_HOME=str(tmp_path / "home"), YARDGOAT_NO_TMUX="1",
             YARDGOAT_CONFIG=str(tmp_path / "no-config.json"), PYTHONPATH=SRC)
    for k in ("YARDGOAT_MAILBOX", "YARDGOAT_MAILBOX_HOME", "YARDGOAT_STATION"):
        e.pop(k, None)  # the suite may itself run inside a station; never talk to the real wrapper
    return e


def yg(env, *args):
    p = subprocess.run([sys.executable, "-m", "yardgoat.cli", *args], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def make_task(tmp_path, task_id="t-demo", worker="echo done > out/result.txt", verify="test -s out/result.txt",
              attempts=2):
    wd = tmp_path / "work"
    (wd / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "card.md").write_text("write out/result.txt\n")
    (tmp_path / "verify.sh").write_text(verify + "\n")
    spec = {"task_id": task_id, "project": "demo", "goal": "g", "card": "card.md", "workdir": "work",
            "outputs": ["out/result.txt"], "executor": {"host": "shell", "command": worker},
            "verifier": {"command": f"bash {tmp_path / 'verify.sh'}", "freeze": ["verify.sh"]},
            "budget": {"max_attempts": attempts, "max_wall_min": 1}}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec))
    return p


def wait_exit(env, task_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = json.loads(yg(env, "status", task_id, "--json")[1])
        if st["runs"] and st["runs"][-1]["state"] != "running":
            return st
        time.sleep(0.2)
    raise AssertionError("run did not exit")


def test_submit_is_idempotent_and_ids_are_immutable(tmp_path, env):
    spec = make_task(tmp_path)
    assert yg(env, "submit", str(spec), "--as", "A")[0] == 0
    rc, out, _ = yg(env, "submit", str(spec), "--as", "A")
    assert rc == 0 and json.loads(out)["result"] == "already-accepted"
    s = json.loads(spec.read_text()); s["goal"] = "different"; spec.write_text(json.dumps(s))
    assert yg(env, "submit", str(spec), "--as", "A")[0] == 3


def test_only_owner_dispatches_and_no_duplicate_while_running(tmp_path, env):
    spec = make_task(tmp_path, worker="sleep 2; echo done > out/result.txt")
    yg(env, "submit", str(spec), "--as", "A")
    assert yg(env, "dispatch", "t-demo", "--as", "B")[0] == 3
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 0
    rc, out, _ = yg(env, "dispatch", "t-demo", "--as", "A")
    assert rc == 3 and json.loads(out)["result"] == "already-running"
    st = wait_exit(env, "t-demo")
    assert len(st["runs"]) == 1
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 3  # exited but unverified


def test_takeover_mid_run_then_new_owner_verifies(tmp_path, env):
    spec = make_task(tmp_path, worker="sleep 2; echo done > out/result.txt")
    yg(env, "submit", str(spec), "--as", "A")
    yg(env, "dispatch", "t-demo", "--as", "A")
    assert yg(env, "takeover", "t-demo", "--as", "B", "--from", "A")[0] == 0
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    assert st["owner"] == "B" and st["owner_history"][0]["run_in_flight"] == "r1"
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 3  # old owner can no longer act
    wait_exit(env, "t-demo")
    rc, out, _ = yg(env, "close", "t-demo", "--as", "B")
    assert rc == 0 and json.loads(out)["verdict"] == "PASS"
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    rec = st["runs"][0]["receipt"]
    assert st["status"] == "verified" and len(st["runs"]) == 1
    assert rec["status"] == "passed" and rec["verifier"]["checked_by"] == "B" and rec["artifacts"]["out/result.txt"]


def test_failing_verifier_allows_retry_within_budget_then_exhausts(tmp_path, env):
    spec = make_task(tmp_path, worker="true", attempts=2)  # never writes the output
    yg(env, "submit", str(spec), "--as", "A")
    for i in (1, 2):
        assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 0
        wait_exit(env, "t-demo")
        assert yg(env, "close", "t-demo", "--as", "A")[0] == 1
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 3
    assert json.loads(yg(env, "status", "t-demo", "--json")[1])["status"] == "exhausted"


def test_frozen_verifier_tamper_blocks_dispatch_and_close(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A")
    (tmp_path / "verify.sh").write_text("exit 0\n")
    rc, _, err = yg(env, "dispatch", "t-demo", "--as", "A")
    assert rc == 3 and "frozen files changed" in err


def test_crashing_verifier_is_not_a_fail(tmp_path, env):
    spec = make_task(tmp_path, verify="exit 7")
    yg(env, "submit", str(spec), "--as", "A")
    yg(env, "dispatch", "t-demo", "--as", "A")
    wait_exit(env, "t-demo")
    rc, out, _ = yg(env, "close", "t-demo", "--as", "A")
    assert rc == 2 and json.loads(out)["verdict"] == "ERROR"
    st = json.loads(yg(env, "status", "t-demo", "--json")[1])
    assert st["runs"][0]["receipt"]["status"] == "verifier_error"


def test_events_are_append_only_and_cover_the_lifecycle(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A")
    yg(env, "dispatch", "t-demo", "--as", "A")
    wait_exit(env, "t-demo")
    yg(env, "close", "t-demo", "--as", "A")
    kinds = [json.loads(l)["kind"] for l in yg(env, "events", "t-demo")[1].splitlines()]
    assert kinds == ["task.accepted", "run.dispatched", "run.started", "run.exited", "run.verified"]


def test_receipt_carries_every_field_the_schema_requires(tmp_path, env):
    schema = json.loads((Path(SRC) / "yardgoat/schemas/receipt.schema.json").read_text())
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A")
    yg(env, "dispatch", "t-demo", "--as", "A")
    rec = wait_exit(env, "t-demo")["runs"][0]["receipt"]
    assert set(schema["required"]) <= set(rec)
    assert rec["status"] in schema["properties"]["status"]["enum"]


def test_worker_environment_does_not_inherit_yardgoats_import_path(tmp_path, env):
    """Regression (2026-10-23): the runner leaked its own sys.path as PYTHONPATH and broke every Python
    the worker started. The worker must see the caller's PYTHONPATH (here: unset), nothing else."""
    env.pop("PYTHONPATH", None)
    env["YARDGOAT_TEST_SRC"] = SRC
    spec = make_task(tmp_path, worker="python3 -c \"import os;open('out/result.txt','w').write(os.environ.get('PYTHONPATH','<unset>'))\"")
    p = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {SRC!r}); from yardgoat.cli import main; sys.exit(main())",
                        "submit", str(spec), "--as", "A"], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    p = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {SRC!r}); from yardgoat.cli import main; sys.exit(main())",
                        "dispatch", "t-demo", "--as", "A"], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    end = time.time() + 20
    out = tmp_path / "work/out/result.txt"
    while time.time() < end and not out.exists():
        time.sleep(0.2)
    assert out.read_text() == "<unset>"
