"""M7 contracts: isolated local stores, shell workers and file-only notification hooks."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from test_contract import SRC, env, wait_exit, yg  # noqa: F401


def call(env, *args, code=0):
    rc, out, err = yg(env, *args)
    assert rc == code, (args, rc, out, err)
    return json.loads(out) if out.strip() else None


def task(tmp_path, env, tid="m7-task", worker="echo done > out/result.txt", verify=None, **extra):
    directory = tmp_path / tid
    wd = directory / "work"
    (wd / "out").mkdir(parents=True)
    (directory / "card.md").write_text("Write the result in out/result.txt.\n")
    (directory / "verify.sh").write_text(verify or "echo checked >> checks\ntest -s out/result.txt\n")
    spec = {"task_id": tid, "project": "demo", "goal": "write a result", "card": "card.md",
            "workdir": "work", "outputs": ["out/result.txt"],
            "executor": {"host": "shell", "command": worker},
            "verifier": {"command": f"bash {shlex.quote(str(directory / 'verify.sh'))}", "freeze": ["verify.sh"]},
            "budget": {"max_attempts": 2, "max_wall_min": 1}, **extra}
    path = directory / "spec.json"
    path.write_text(json.dumps(spec))
    call(env, "submit", str(path), "--as", "owner-a")
    return wd, path


def state(env, tid="m7-task"):
    return json.loads((Path(env["YARDGOAT_HOME"]) / "tasks" / tid / "task.json").read_text())


def execute(env, tid="m7-task"):
    call(env, "dispatch", tid, "--as", "owner-a")
    assert wait_exit(env, tid)["status"] == "exited"


def reconciled(env):
    return call(env, "reconcile")


def checked_events(env, tid="m7-task"):
    path = Path(env["YARDGOAT_HOME"]) / "tasks" / tid / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line)["kind"] == "run.verified"]


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("worker_rc", [0, 9])
def test_default_reconcile_verifies_even_nonzero_worker_exit(tmp_path, env, worker_rc):
    wd, _ = task(tmp_path, env, worker=f"echo done > out/result.txt; exit {worker_rc}")
    execute(env)
    result = reconciled(env)
    assert result["actions"] == [{"task_id": "m7-task", "run_id": "r1", "auto_closed": "PASS"}]
    reconciled(env)
    assert state(env)["status"] == "verified"
    assert state(env)["verdict"]["by"] == "owner-a"
    assert len(checked_events(env)) == 1 and lines(wd / "checks") == ["checked"]
    receipt = json.loads((Path(env["YARDGOAT_HOME"]) / "tasks/m7-task/runs/r1/receipt.json").read_text())
    assert receipt["exit_code"] == worker_rc


def test_explicit_false_keeps_manual_close(tmp_path, env):
    wd, _ = task(tmp_path, env, auto_close=False)
    execute(env)
    assert reconciled(env)["actions"] == []
    assert reconciled(env)["actions"] == []
    assert state(env)["status"] == "exited" and not (wd / "checks").exists()
    assert call(env, "close", "m7-task", "--as", "owner-a")["verdict"] == "PASS"
    assert len(checked_events(env)) == 1


@pytest.mark.parametrize("verifier_rc, expected", [(1, "failed"), (2, "exited")])
def test_default_failure_or_error_does_not_retry(tmp_path, env, verifier_rc, expected):
    wd, _ = task(tmp_path, env, verify=f"echo checked >> checks\nexit {verifier_rc}\n")
    execute(env)
    reconciled(env)
    reconciled(env)
    st = state(env)
    assert st["status"] == expected
    assert st["runs"][0]["state"] == ("failed" if verifier_rc == 1 else "verify_error")
    assert len(st["runs"]) == 1 and lines(wd / "checks") == ["checked"]


def test_legacy_accepted_exited_snapshot_upgrades_without_rewriting_frozen_data(tmp_path, env):
    # Baseline f2cde80 store shape, seeded independently of the current submit/normalizer.
    # The only worker is a real shell; no auto_close field is present in the accepted spec.
    wd = tmp_path / "legacy-work"
    wd.mkdir()
    card, verifier = tmp_path / "legacy-card.md", tmp_path / "legacy-verify.sh"
    card.write_text("Write result.\n")
    verifier.write_text("echo checked >> checks\ntest -s result\n")
    subprocess.run(["bash", "-c", "echo done > result"], cwd=wd, check=True)
    accepted = {"task_id": "m7-task", "project": "demo", "goal": "write a result", "card": str(card),
                "workdir": str(wd), "outputs": ["result"], "executor": {"host": "shell", "command": "echo done > result"},
                "verifier": {"command": f"bash {shlex.quote(str(verifier))}", "cwd": str(wd), "freeze": [str(verifier)]},
                "adapter": {"kind": "direct"}, "budget": {"max_attempts": 2, "max_wall_min": 1},
                "scope": [], "risk": "l2", "authority": "", "escalate_when": []}
    frozen = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (card, verifier)}
    digest = hashlib.sha256(json.dumps({"spec": accepted, "frozen": frozen}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    root = Path(env["YARDGOAT_HOME"])
    td = root / "tasks/m7-task"
    rd = td / "runs/r1"
    rd.mkdir(parents=True)
    (root / "frozen").mkdir()
    documents = {
        td / "spec.json": accepted,
        root / "frozen/m7-task.json": {"spec": accepted, "frozen": frozen, "content_hash": digest},
        td / "task.json": {"task_id": "m7-task", "content_hash": digest, "frozen": frozen,
                            "owner": "owner-a", "owner_history": [], "status": "exited", "question": None,
                            "verdict": None, "accepted_at": "2026-01-01T00:00:00+0000",
                            "runs": [{"run_id": "r1", "state": "exited", "mode": "process", "dispatched_by": "owner-a"}]},
        rd / "receipt.json": {"task_id": "m7-task", "run_id": "r1", "status": "exited", "exit_code": 0},
    }
    for path, document in documents.items():
        path.write_text(json.dumps(document))
    immutable = (td / "spec.json", root / "frozen/m7-task.json", card, verifier)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in immutable}
    reconciled(env)
    reconciled(env)
    assert state(env)["status"] == "verified"
    assert state(env)["content_hash"] == digest and state(env)["frozen"] == frozen
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in immutable}
    assert lines(wd / "checks") == ["checked"] and len(checked_events(env)) == 1


def test_two_reconcilers_observe_same_exited_run_and_verify_once(tmp_path, env):
    wd, _ = task(tmp_path, env)
    execute(env)
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    script = '''import os, sys, time
from pathlib import Path
from yardgoat import cli
original = cli.do_close
barrier = Path(sys.argv[1])
def close(*args, **kwargs):
    (barrier / str(os.getpid())).touch()
    end = time.monotonic() + 10
    while len(list(barrier.iterdir())) < 2:
        if time.monotonic() > end: raise RuntimeError("second reconciler did not reach close")
        time.sleep(0.01)
    return original(*args, **kwargs)
cli.do_close = close
sys.exit(cli.main(["reconcile"]))
'''
    processes = [subprocess.Popen([sys.executable, "-c", script, str(barrier)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for process in processes:
            out, err = process.communicate(timeout=20)
            assert process.returncode == 0, err
            json.loads(out)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert len(list(barrier.iterdir())) == 2
    assert state(env)["status"] == "verified"
    assert lines(wd / "checks") == ["checked"] and len(checked_events(env)) == 1


def test_default_close_uses_current_owner_delivery_and_dependencies(tmp_path, env):
    wd, _ = task(tmp_path, env, risk="l3", on_pass=[{"name": "deliver", "command": "echo $YARDGOAT_OWNER > delivered"}])
    call(env, "dispatch", "m7-task", "--as", "owner-a", code=3)
    reconciled(env)
    assert state(env)["runs"] == []
    call(env, "approve", "m7-task", "--as", "reviewer")
    child, _ = task(tmp_path, env, tid="m7-child", depends_on=["m7-task"])
    call(env, "dispatch", "m7-child", "--as", "owner-a")
    assert state(env, "m7-child")["status"] == "waiting_deps"
    execute(env)
    call(env, "takeover", "m7-task", "--as", "owner-b", "--from", "owner-a")
    reconciled(env)
    assert state(env)["verdict"]["by"] == "owner-b"
    assert lines(wd / "delivered") == ["owner-b"]
    assert state(env, "m7-child")["status"] == "waiting_deps"
    reconciled(env)
    wait_exit(env, "m7-child")
    reconciled(env)
    assert state(env, "m7-child")["status"] == "verified"
    assert lines(child / "checks") == ["checked"]


@pytest.mark.parametrize("flags, expected", [([], True), (["--auto-close"], True), (["--no-auto-close"], False)])
def test_quick_auto_close_switches_without_launching_a_model(tmp_path, env, flags, expected):
    # Parser/spec test only: --no-dispatch guarantees that no configured host starts.
    call(env, "quick", "write a result", "--verify", "true", "--no-dispatch", "--id", "m7-quick",
         "--as", "owner-a", *flags)
    spec = json.loads((Path(env["YARDGOAT_HOME"]) / "tasks/m7-quick/spec.json").read_text())
    assert spec.get("auto_close", True) is expected
    assert state(env, "m7-quick")["runs"] == []


def test_auto_close_schema_documents_default():
    schema = json.loads((Path(SRC) / "yardgoat/schemas/task.schema.json").read_text())
    assert schema["properties"]["auto_close"]["default"] is True


def pending(tmp_path, env, question="Which variant?"):
    task(tmp_path, env)
    call(env, "park", "m7-task", "--as", "owner-a", "--question", question)


def hook(tmp_path, name, **extra):
    script = tmp_path / "hook.py"
    script.write_text("import json, pathlib, sys, time\n"
                      "sink, flag = map(pathlib.Path, sys.argv[1:3])\n"
                      "if flag.exists():\n"
                      "    if flag.read_text() == 'timeout': time.sleep(10)\n"
                      "    else: sys.exit(7)\n"
                      "print('captured hook output')\n"
                      "with sink.open('a') as f: f.write(json.dumps(sys.argv[3:]) + '\\n')\n")
    return {"name": name, "command": [sys.executable, str(script), str(tmp_path / f"{name}.jsonl"),
            str(tmp_path / f"{name}.fail"), "{task_id}", "{status}", "{owner}", "{question}", "{hint}"], **extra}


def configure(env, settings):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"notify": settings}))


def test_notification_routes_dedupe_by_recipient_after_takeover(tmp_path, env):
    pending(tmp_path, env)
    configure(env, [hook(tmp_path, "owner", recipient="{owner}"), hook(tmp_path, "fixed")])
    assert len(reconciled(env)["notified"]) == 2
    assert reconciled(env)["notified"] == []
    call(env, "takeover", "m7-task", "--as", "owner-b", "--from", "owner-a")
    assert len(reconciled(env)["notified"]) == 1
    assert reconciled(env)["notified"] == []
    assert [json.loads(row)[2] for row in lines(tmp_path / "owner.jsonl")] == ["owner-a", "owner-b"]
    assert len(lines(tmp_path / "fixed.jsonl")) == 1
    record = json.loads((Path(env["YARDGOAT_HOME"]) / "notified/m7-task.json").read_text())
    assert len(record["sent"]) == 3


def test_single_object_recipient_uses_all_command_placeholders(tmp_path, env):
    question = "literal {owner} $(false) `false`"
    pending(tmp_path, env, question)
    configure(env, hook(tmp_path, "route", recipient="{task_id}:{status}:{owner}:{question}:{hint}"))
    assert len(reconciled(env)["notified"]) == 1
    call(env, "takeover", "m7-task", "--as", "owner-b", "--from", "owner-a")
    assert len(reconciled(env)["notified"]) == 1
    assert json.loads(lines(tmp_path / "route.jsonl")[0])[3] == question


def test_baseline_notified_record_is_reused_for_legacy_single_object(tmp_path, env):
    question = "Long question " + "x" * 600
    pending(tmp_path, env, question)
    configure(env, hook(tmp_path, "legacy"))  # even an optional name must preserve the legacy object key
    digest = hashlib.sha256(question.encode()).hexdigest()
    path = Path(env["YARDGOAT_HOME"]) / "notified/m7-task.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"task_id": "m7-task", "sent": {
        f"parked:{digest}": {"status": "parked", "question_hash": digest, "at": "2026-01-01T00:00:00+0000"}}}))
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert reconciled(env)["notified"] == []
    call(env, "takeover", "m7-task", "--as", "owner-b", "--from", "owner-a")
    assert reconciled(env)["notified"] == []
    assert not (tmp_path / "legacy.jsonl").exists()
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


@pytest.mark.parametrize("failure", ["exit", "timeout", "template"])
def test_notification_route_failure_does_not_block_others_and_retries_only_failed(tmp_path, env, monkeypatch, failure):
    sys.path.insert(0, SRC)
    from yardgoat import notify
    monkeypatch.setenv("YARDGOAT_HOME", env["YARDGOAT_HOME"])
    pending(tmp_path, env)
    first, second = hook(tmp_path, "retry", recipient="{owner}"), hook(tmp_path, "ok")
    flag = tmp_path / "retry.fail"
    if failure == "template":
        first["recipient"] = "{unknown}"
    else:
        flag.write_text(failure)
    if failure == "timeout":
        original = subprocess.Popen.communicate
        def short(process, input=None, timeout=None):
            return original(process, input=input, timeout=0.2 if timeout == 30 else timeout)
        monkeypatch.setattr(subprocess.Popen, "communicate", short)
    cfg = {"notify": [first, second]}
    assert len(notify.notify_pending(cfg)) == 1
    assert not (tmp_path / "retry.jsonl").exists() and len(lines(tmp_path / "ok.jsonl")) == 1
    record = Path(env["YARDGOAT_HOME"]) / "notified/m7-task.json"
    assert len(json.loads(record.read_text())["sent"]) == 1
    flag.unlink(missing_ok=True)
    first["recipient"] = "{owner}"
    assert len(notify.notify_pending(cfg)) == 1
    assert notify.notify_pending(cfg) == []
    assert len(lines(tmp_path / "retry.jsonl")) == len(lines(tmp_path / "ok.jsonl")) == 1


@pytest.mark.parametrize("names", [("same", "same"), (None, "default"), (None, None)])
def test_duplicate_route_names_reject_entire_configuration(tmp_path, env, names):
    pending(tmp_path, env)
    settings = [hook(tmp_path, "first"), hook(tmp_path, "second")]
    for route, name in zip(settings, names):
        if name is None:
            route.pop("name")
        else:
            route["name"] = name
    configure(env, settings)
    rc, out, err = yg(env, "reconcile")
    assert rc == 0 and json.loads(out)["notified"] == []
    assert "unique" in err or "duplicate" in err
    assert not (tmp_path / "first.jsonl").exists() and not (tmp_path / "second.jsonl").exists()
    assert not (Path(env["YARDGOAT_HOME"]) / "notified").exists()


def test_concurrent_reconcilers_deliver_each_route_once(tmp_path, env):
    pending(tmp_path, env)
    configure(env, [hook(tmp_path, "owner", recipient="{owner}"), hook(tmp_path, "fixed")])
    processes = [subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "reconcile"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    count = 0
    for process in processes:
        out, err = process.communicate(timeout=20)
        assert process.returncode == 0, err
        count += len(json.loads(out)["notified"])
    assert count == 2
    assert len(lines(tmp_path / "owner.jsonl")) == len(lines(tmp_path / "fixed.jsonl")) == 1
