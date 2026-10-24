"""M4: isolated stores, fake executing/review stations, real frozen verifiers."""
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_contract import SRC, env, make_task, yg  # noqa: F401

sys.path.insert(0, SRC)
from yardgoat import cli, pool
from yardgoat.store import TaskDir, read_json


@pytest.fixture
def rig(tmp_path, env, monkeypatch):
    for key in ("YARDGOAT_HOME", "YARDGOAT_CONFIG"):
        monkeypatch.setenv(key, env[key])
    for key in ("YARDGOAT_MAILBOX", "YARDGOAT_MAILBOX_HOME", "YARDGOAT_STATION"):
        monkeypatch.delenv(key, raising=False)
    cfg = {"pools": {n: {"host": "shell", "command": "true", "lane": n,
                         "root": str(tmp_path / n), "max_stations": 0,
                         "max_tasks_per_station": 100} for n in ("work", "audit")}}
    for name in cfg["pools"]:
        (tmp_path / name).mkdir()
        pool.update_station(name, f"{name}-st-1", station=f"{name}-st-1", pool=name,
                            channel={"host": "shell", "lane": name}, root=str(tmp_path / name),
                            state="running", tasks=[], pid=os.getpid())
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))

    class Rig:
        owner = "owner"

        def call(self, *args, rc=0):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                actual = cli.main(list(args))
            assert actual == rc, (args, actual, out.getvalue(), err.getvalue())
            return json.loads(out.getvalue()) if out.getvalue().strip() else None

        def submit(self, tid="parent", **changes):
            sp = make_task(tmp_path / "work" / tid, task_id=tid, verify="test -s src/code.py", attempts=1)
            wd = sp.parent / "work"
            (wd / "src").mkdir(exist_ok=True)
            (wd / "src/code.py").write_text("value = 1\n")
            spec = json.loads(sp.read_text())
            spec.update(adapter={"kind": "pool", "pool": "work"}, scope=["src"],
                        executor={"host": "shell", "command": "true", "lane": "work"},
                        acceptance="Preserve the result and test concurrent updates",
                        review={"pool": "audit", "rounds": 3, "blocking": ["blocker", "major"],
                                "after_max_rounds": "park", "path": "src"})
            spec.update(changes)
            if spec.get("review") is None:
                spec.pop("review", None)
            sp.write_text(json.dumps(spec))
            self.call("submit", str(sp), "--as", self.owner)
            return wd

        def dispatch(self, tid="parent"):
            return self.call("dispatch", tid, "--as", self.owner)

        def execute(self, tid="parent"):
            rc, claim = cli.claim_card("work", "work-st-1")
            assert rc == 0 and claim.get("task_id") == tid, claim
            assert cli.finish_card(tid, claim["run_id"], "work-st-1")[0] == 0
            self.call("reconcile")
            return claim

        def review(self, n, findings, finish=True):
            rc, claim = cli.claim_card("audit", "audit-st-1")
            assert rc == 0 and claim.get("task_id") == f"parent-review-{n}", claim
            wd = Path(claim["workdir"])
            (wd / "out").mkdir(exist_ok=True)
            (wd / "out/findings.json").write_text(json.dumps(findings))
            (wd / "out/REVIEW.md").write_text("Reviewed concurrency, consistency, error handling and test coverage.\n")
            if finish:
                assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
                self.call("reconcile")
            return claim

    return Rig()


def finding(**changes):
    return {"id": "F1", "severity": "major", "file": "code.py", "line": 1,
            "title": "Lost update", "scenario": "Two writers read the same value",
            "fix": "Serialize the read and write", **changes}


def test_two_rounds_gate_delivery_dependencies_and_scope(rig):
    wd = rig.submit(risk="l3", on_pass=[{"name": "deliver", "command": "echo done >> out/delivered"}])
    rig.call("approve", "parent", "--as", "approver")
    rig.submit("dependent", depends_on=["parent"], review=None)
    # Omission is the backwards compatible way to disable review.
    rig.dispatch("dependent")
    rig.dispatch()
    rig.execute()
    assert TaskDir("parent").state()["status"] == "reviewing"
    assert not (wd / "out/delivered").exists()
    assert TaskDir("dependent").state()["status"] == "waiting_deps"
    from yardgoat.scheduling import scope_conflicts
    assert scope_conflicts("other", {"workdir": str(wd), "scope": ["src"]})
    rig.call("deliver", "parent", "--as", "owner", rc=3)
    rig.call("dispatch", "parent", "--as", "owner", rc=3)
    rig.review(1, [finding()])
    assert TaskDir("parent").state()["status"] in ("answered", "queued")
    rig.call("reconcile")
    claim = rig.execute()
    assert "Lost update" in Path(claim["prompt"]).read_text()
    assert not (wd / "out/delivered").exists()
    rig.review(2, [])
    st = TaskDir("parent").state()
    assert st["status"] == "verified" and len(st["runs"]) == 2
    assert (wd / "out/delivered").read_text().splitlines() == ["done"]
    assert not scope_conflicts("other", {"workdir": str(wd), "scope": ["src"]})
    for _ in range(3):
        rig.call("reconcile")
    assert TaskDir("dependent").state()["status"] == "queued"
    receipts = [read_json(TaskDir("parent").run_dir(r["run_id"]) / "receipt.json") for r in st["runs"]]
    assert [r["review_round"] for r in receipts] == [1, 2]
    assert all(r["approval"]["by"] == "approver" for r in receipts)
    assert [r["findings_count"] for r in receipts[-1]["review"]["rounds"]] == [1, 0]
    child = read_json(TaskDir("parent-review-1").run_dir("r1") / "receipt.json")
    assert child["review_round"] == 1 and child["findings_count"] == 1


@pytest.mark.parametrize("strategy", ["park", "pass_with_followups"])
def test_three_round_limit(rig, strategy):
    wd = rig.submit(review={"pool": "audit", "rounds": 3, "blocking": ["major"],
                            "after_max_rounds": strategy, "path": "src"},
                    on_pass=[{"name": "deliver", "command": "touch out/delivered"}])
    rig.dispatch()
    for n in range(1, 4):
        rig.execute()
        rig.review(n, [finding(), finding(id="F2", severity="minor")])
        rig.call("reconcile")
    st = TaskDir("parent").state()
    assert len(st["runs"]) == 3
    assert not TaskDir("parent-review-4").exists
    assert st["status"] == ("parked" if strategy == "park" else "verified")
    assert (wd / "out/delivered").exists() == (strategy == "pass_with_followups")
    inbox = rig.call("inbox", "--json")
    if strategy == "park":
        assert any(i["task_id"] == "parent" and "F1" in i["question"] for i in inbox)
    else:
        followups = [i for i in inbox if "followup" in i["task_id"]]
        assert len(followups) == 2
        assert all(TaskDir(i["task_id"]).state()["runs"] == [] for i in followups)


def test_duplicate_reconcile_close_and_takeover(rig, env):
    rig.submit()
    rig.dispatch()
    rig.execute()
    for _ in range(3):
        rig.call("close", "parent", "--as", "owner")
    assert len(TaskDir("parent-review-1").state()["runs"]) == 1
    claim = rig.review(1, [finding()], finish=False)
    rig.call("takeover", "parent", "--from", "owner", "--as", "next")
    rig.owner = "next"
    assert TaskDir("parent-review-1").state()["owner"] == "next"
    rig.call("close", "parent-review-1", "--as", "owner", rc=3)
    assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    procs = [subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "reconcile"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    for p in procs:
        out, err = p.communicate(timeout=15)
        assert p.returncode == 0, err
        json.loads(out)
    for _ in range(3):
        rig.call("reconcile")
    assert len(TaskDir("parent").state()["runs"]) == 2
    answers = list((TaskDir("parent").path / "answers").glob("*.md"))
    assert len(answers) == 1
    rig.execute()
    rig.review(2, [])
    assert TaskDir("parent").state()["status"] == "verified"
    assert len(TaskDir("parent").state()["review"]["rounds"]) == 2


@pytest.mark.parametrize("bad", [None, {}, [finding(line=99)], [finding(file="../missing")],
                                 [finding(severity="unknown")], [finding(line=True)],
                                 [finding(), finding()]])
def test_invalid_review_never_means_clean(rig, bad):
    rig.submit()
    rig.dispatch()
    rig.execute()
    rig.review(1, bad)
    for _ in range(2):
        rig.call("reconcile")
    assert TaskDir("parent").state()["status"] != "verified"
    assert TaskDir("parent-review-1").state()["verdict"]["verdict"] == "FAIL"
    assert len(TaskDir("parent").state()["runs"]) == 1


@pytest.mark.parametrize("target", ["original", "snapshot"])
def test_review_verifier_detects_source_changes(rig, target):
    wd = rig.submit()
    rig.dispatch()
    rig.execute()
    claim = rig.review(1, [], finish=False)
    source = wd / "src" if target == "original" else Path(claim["workdir"]) / "target"
    (source / "code.py").write_text("changed\n")
    assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    rig.call("reconcile")
    assert TaskDir("parent-review-1").state()["verdict"]["verdict"] == "FAIL"
    assert TaskDir("parent").state()["status"] != "verified"


@pytest.mark.parametrize("same", ["pool", "lane"])
def test_execution_review_channel_must_differ(rig, tmp_path, env, same):
    if same == "lane":
        path = Path(env["YARDGOAT_CONFIG"])
        cfg = json.loads(path.read_text())
        cfg["pools"]["audit"]["lane"] = "work"
        path.write_text(json.dumps(cfg))
    sp = make_task(tmp_path / "work" / "reject")
    spec = json.loads(sp.read_text())
    spec.update(adapter={"kind": "pool", "pool": "work"}, review={"pool": "work" if same == "pool" else "audit",
                 "rounds": 3, "blocking": ["major"], "after_max_rounds": "park"})
    sp.write_text(json.dumps(spec))
    rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
    assert rc in (2, 3) and "review" in err


def test_failed_fix_uses_failure_budget(rig):
    wd = rig.submit(budget={"max_attempts": 1})
    rig.dispatch()
    rig.execute()
    rig.review(1, [finding()])
    rig.call("reconcile")
    (wd / "src/code.py").unlink()
    rig.execute()
    assert TaskDir("parent").state()["status"] == "exhausted"
    assert len(TaskDir("parent").state()["runs"]) == 2
    assert not TaskDir("parent-review-2").exists


def test_fix_failure_can_retry_automatically_without_spending_a_round(rig):
    wd = rig.submit(budget={"max_attempts": 2})
    rig.dispatch()
    rig.execute()
    rig.review(1, [finding()])
    (wd / "src/code.py").unlink()
    rig.execute()
    assert TaskDir("parent").state()["status"] == "failed"
    rig.call("reconcile")
    (wd / "src/code.py").write_text("value = 2\n")
    rig.execute()
    rig.review(2, [])
    st = TaskDir("parent").state()
    assert st["status"] == "verified"
    assert [r["review_round"] for r in st["runs"]] == [1, 2, 2]


@pytest.mark.parametrize("missing", ["findings.json", "REVIEW.md"])
def test_missing_review_output_is_a_failure(rig, missing):
    rig.submit()
    rig.dispatch()
    rig.execute()
    claim = rig.review(1, [], finish=False)
    (Path(claim["workdir"]) / "out" / missing).unlink()
    assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    rig.call("reconcile")
    assert TaskDir(claim["task_id"]).state()["verdict"]["verdict"] == "FAIL"
    assert TaskDir("parent").state()["status"] != "verified"


@pytest.mark.parametrize("boundary", ["child_accepted", "answer_written", "result_receipt"])
def test_interrupted_review_replays_once(rig, monkeypatch, boundary):
    from yardgoat import review

    class Crash(BaseException):
        pass

    rig.submit()
    rig.dispatch()
    if boundary != "child_accepted":
        rig.execute()
        claim = rig.review(1, [finding()], finish=False)
        assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    with monkeypatch.context() as patch:
        if boundary == "result_receipt":
            original = review.write_json

            def crash(path, obj):
                original(path, obj)
                if path.name == "receipt.json" and obj.get("status") == "reviewed":
                    raise Crash()

            patch.setattr(review, "write_json", crash)
        else:
            original = TaskDir.event

            def crash(self, kind, actor, **data):
                event = original(self, kind, actor, **data)
                if ((boundary == "child_accepted" and self.task_id == "parent-review-1" and kind == "task.accepted")
                        or (boundary == "answer_written" and self.task_id == "parent" and kind == "task.answered")):
                    raise Crash()
                return event

            patch.setattr(TaskDir, "event", crash)
        with pytest.raises(Crash):
            if boundary == "child_accepted":
                rig.execute()
            else:
                rig.call("reconcile")
    for _ in range(3):
        rig.call("reconcile")
    if boundary == "child_accepted":
        assert len(TaskDir("parent-review-1").state()["runs"]) == 1
        rig.review(1, [finding()])
    assert len(TaskDir("parent").state()["runs"]) == 2
    assert len(list((TaskDir("parent").path / "answers").glob("*.md"))) == 1
    assert len([e for e in TaskDir("parent").events() if e["kind"] == "task.answered"]) == 1
    rig.execute()
    rig.review(2, [])
    assert TaskDir("parent").state()["status"] == "verified"


def test_result_consumption_uses_verified_receipt(rig, monkeypatch):
    from yardgoat import review
    rig.submit()
    rig.dispatch()
    rig.execute()
    claim = rig.review(1, [finding()], finish=False)
    assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    with monkeypatch.context() as patch:
        patch.setattr(review, "advance", lambda *a, **kw: 0)
        rig.call("close", claim["task_id"], "--as", "owner")
    (Path(claim["workdir"]) / "out/findings.json").write_text("[]")
    rig.call("reconcile")
    assert TaskDir("parent").state()["status"] == "queued"
    assert TaskDir("parent").state()["review"]["rounds"][0]["findings_count"] == 1


@pytest.mark.parametrize("value", [None, [], {}, {"pool": "audit", "rounds": True},
    {"pool": "audit", "rounds": 0}, {"pool": "audit", "blocking": "major"},
    {"pool": "audit", "blocking": ["unknown"]}, {"pool": "audit", "path": "../outside"},
    {"pool": "audit", "after_max_rounds": "ignore"}])
def test_invalid_review_config_rejected(rig, tmp_path, env, value):
    sp = make_task(tmp_path / "invalid")
    spec = json.loads(sp.read_text())
    spec["review"] = value
    sp.write_text(json.dumps(spec))
    rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
    assert rc == 2 and "review" in err


def test_changed_review_lane_cannot_be_claimed(rig, env):
    rig.submit()
    rig.dispatch()
    rig.execute()
    path = Path(env["YARDGOAT_CONFIG"])
    cfg = json.loads(path.read_text())
    cfg["pools"]["audit"]["lane"] = "work"
    path.write_text(json.dumps(cfg))
    assert cli.claim_card("audit", "audit-st-1")[1]["result"] == "EMPTY"
    assert TaskDir("parent-review-1").state()["runs"][0]["state"] == "launch_failed"


@pytest.mark.parametrize("field", ["review_of", "review_followup"])
def test_user_submit_cannot_impersonate_generated_review(rig, tmp_path, env, field):
    sp = make_task(tmp_path / "forged")
    spec = json.loads(sp.read_text())
    spec[field] = {"task_id": "parent", "round": 1, "run_id": "r1"}
    sp.write_text(json.dumps(spec))
    rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
    assert rc == 2 and "reserved" in err
