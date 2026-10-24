"""M4 review regressions: isolated shell stations and frozen local verifiers only."""
import json
import subprocess
from pathlib import Path

import pytest

from test_contract import env, make_task, wait_exit, yg  # noqa: F401
from test_m4 import finding, rig as original_rig  # noqa: F401
from yardgoat import cli, doctor, pool, review, scheduling
from yardgoat.store import TaskDir, read_json


@pytest.fixture
def rig(original_rig, tmp_path):
    # Model the channel/root recorded by the station wrapper at launch.
    for name in ("work", "audit"):
        pool.update_station(name, f"{name}-st-1", channel={"host": "shell", "lane": name},
                            root=str(tmp_path / name))
    return original_rig


def config_change(env, change):
    path = Path(env["YARDGOAT_CONFIG"])
    cfg = json.loads(path.read_text())
    change(cfg)
    path.write_text(json.dumps(cfg))


def one_round(strategy="park"):
    return {"pool": "audit", "rounds": 1, "blocking": ["major"],
            "after_max_rounds": strategy, "path": "src"}


def assert_parked(rig, text):
    st = TaskDir("parent").state()
    assert st["status"] == "parked", st
    assert text in st["question"]["text"]
    assert any(x.get("task_id") == "parent" for x in rig.call("inbox", "--json"))
    return st


def test_review_channel_frozen_at_submit(rig):
    rig.submit()
    assert TaskDir("parent").spec()["review"].get("channel") == {"host": "shell", "lane": "audit"}


def test_config_change_cannot_relabel_execution_channel(rig, env):
    rig.submit()
    rig.dispatch()
    rig.execute()
    config_change(env, lambda c: (c["pools"]["work"].update(lane="another"),
                                  c["pools"]["audit"].update(lane="work")))
    assert cli.claim_card("audit", "audit-st-1")[1]["result"] == "EMPTY"
    rig.call("reconcile")
    assert_parked(rig, "channel")
    assert TaskDir("parent").state()["runs"][0]["channel"]["lane"] == "work"


def test_run_receipt_uses_launch_channel_when_pool_changes(rig, env):
    rig.submit()
    rig.dispatch()
    rc, claim = cli.claim_card("work", "work-st-1")
    assert rc == 0
    config_change(env, lambda c: c["pools"]["work"].update(lane="relabeled"))
    assert cli.finish_card("parent", claim["run_id"], "work-st-1")[0] == 0
    receipt = read_json(TaskDir("parent").run_dir(claim["run_id"]) / "receipt.json")
    assert receipt["lane"] == "work"
    rig.call("reconcile")
    rig.review(1, [])
    assert TaskDir("parent").state()["status"] == "verified"


def test_review_channel_host_drift_before_child_creation(rig, env):
    rig.submit()
    rig.dispatch()
    claim = cli.claim_card("work", "work-st-1")[1]
    assert cli.finish_card("parent", claim["run_id"], "work-st-1")[0] == 0
    config_change(env, lambda c: c["pools"]["audit"].update(host="codex"))
    rig.call("reconcile")
    assert_parked(rig, "channel")
    assert not TaskDir("parent-review-1").exists


@pytest.mark.parametrize("name", ["work", "audit"])
def test_actual_station_channel_is_checked(rig, name):
    rig.submit()
    rig.dispatch()
    if name == "audit":
        rig.execute()
    pool.update_station(name, f"{name}-st-1", channel={"host": "shell", "lane": "audit" if name == "work" else "work"})
    assert cli.claim_card(name, f"{name}-st-1")[1]["result"] == "EMPTY"
    rig.call("reconcile")
    assert_parked(rig, "channel")


@pytest.mark.parametrize("scope", [["."], [".yardgoat-reviews/*"], ["alias"]])
def test_submit_rejects_review_workspace_in_parent_write_set(rig, tmp_path, env, scope):
    sp = make_task(tmp_path / "work" / "overlap", task_id="overlap")
    wd = sp.parent / "work"
    (wd / "src").mkdir()
    if scope == ["alias"]:
        (wd / "alias").symlink_to(wd / ".yardgoat-reviews", target_is_directory=True)
    config_change(env, lambda c: c["pools"]["audit"].update(root=str(wd)))
    spec = json.loads(sp.read_text())
    spec.update(scope=scope, executor={"host": "shell", "command": "true", "lane": "work"},
                adapter={"kind": "pool", "pool": "work"}, review=one_round())
    sp.write_text(json.dumps(spec))
    rc, _, err = yg(env, "submit", str(sp), "--as", "owner")
    assert rc == 2 and "write set" in err, err
    assert not TaskDir("overlap").exists


def test_workspace_overlap_after_submit_parks_without_child(rig, env):
    wd = rig.submit(scope=["."])
    rig.dispatch()
    config_change(env, lambda c: c["pools"]["audit"].update(root=str(wd)))
    rig.execute()
    assert_parked(rig, "write set")
    assert not TaskDir("parent-review-1").exists


@pytest.mark.parametrize("failure", ["second_followup", "receipt", "event"])
def test_result_side_effect_failure_replays_same_round(rig, monkeypatch, failure):
    wd = rig.submit(review=one_round("pass_with_followups"))
    rig.dispatch()
    rig.execute()
    with monkeypatch.context() as patch:
        if failure == "second_followup":
            original = review.accept_generated

            def fail(raw, *a):
                if raw["task_id"] == "parent-followup-1-2":
                    raise OSError("injected disk full")
                return original(raw, *a)

            patch.setattr(review, "accept_generated", fail)
        elif failure == "receipt":
            patch.setattr(review, "save_receipt", lambda *a: (_ for _ in ()).throw(OSError("injected disk full")))
        else:
            original = review.event_once

            def fail(td, kind, *a, **kw):
                if kind == "task.review_processed":
                    raise OSError("injected disk full")
                return original(td, kind, *a, **kw)

            patch.setattr(review, "event_once", fail)
        rig.review(1, [finding(), finding(id="F2")])
    st = assert_parked(rig, "injected disk full")
    assert st["review"]["rounds"][0]["status"] == "pending"
    assert not st["review"]["complete"]
    assert scheduling.scope_conflicts("other", {"workdir": str(wd), "scope": ["src"]})
    rig.call("answer", "parent", "--as", "owner", "--text", "Retry the same result")
    for _ in range(3):
        rig.call("reconcile")
    st = TaskDir("parent").state()
    assert st["status"] == "verified"
    assert len(st["runs"]) == len(st["review"]["rounds"]) == 1
    assert not TaskDir("parent-review-2").exists
    for n in (1, 2):
        child = TaskDir(f"parent-followup-1-{n}")
        assert len([e for e in child.events() if e["kind"] == "task.accepted"]) == 1


def test_followup_id_collision_keeps_result_pending(rig):
    rig.submit("parent-followup-1-1", review=None)
    rig.submit(review=one_round("pass_with_followups"))
    rig.dispatch()
    rig.execute()
    rig.review(1, [finding()])
    for _ in range(2):
        st = assert_parked(rig, "already exists")
        assert st["review"]["rounds"][0]["status"] == "pending"
        rig.call("answer", "parent", "--as", "owner", "--text", "Retry")
        rig.call("reconcile")
    assert len(TaskDir("parent").state()["runs"]) == 1


def test_review_frozen_drift_parks_parent_and_restored_result_replays(rig):
    rig.submit()
    rig.dispatch()
    rig.execute()
    claim = rig.review(1, [], finish=False)
    child = TaskDir(claim["task_id"])
    verifier = Path(child.spec()["verifier"]["freeze"][0])
    original = verifier.read_bytes()
    verifier.write_bytes(original + b"\n# drift\n")
    assert cli.finish_card(child.task_id, claim["run_id"], "audit-st-1")[0] == 0
    for _ in range(3):
        rig.call("reconcile")
    st = assert_parked(rig, "frozen")
    assert st["review"]["rounds"][0]["status"] == "pending"
    verifier.write_bytes(original)
    rig.call("answer", "parent", "--as", "owner", "--text", "Frozen verifier restored")
    rig.call("reconcile")
    assert TaskDir("parent").state()["status"] == "verified"
    assert len(child.state()["runs"]) == 1


def test_child_launch_refusal_can_retry_after_parent_answer(rig):
    rig.submit()
    rig.dispatch()
    rig.execute()
    pool.update_station("audit", "audit-st-1", channel={"host": "shell", "lane": "work"})
    assert cli.claim_card("audit", "audit-st-1")[1]["result"] == "EMPTY"
    rig.call("reconcile")
    assert_parked(rig, "channel")
    pool.update_station("audit", "audit-st-1", channel={"host": "shell", "lane": "audit"})
    rig.call("answer", "parent", "--as", "owner", "--text", "Station restarted with correct channel")
    rig.call("reconcile")
    rig.review(1, [])
    assert TaskDir("parent").state()["status"] == "verified"
    assert len(TaskDir("parent").state()["review"]["rounds"]) == 1


@pytest.mark.parametrize("error", ["exit", "spawn"])
def test_child_verifier_error_immediately_parks_parent(rig, monkeypatch, error):
    rig.submit()
    rig.dispatch()
    rig.execute()
    claim = rig.review(1, [], finish=False)
    assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    with monkeypatch.context() as patch:
        if error == "exit":
            patch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 2, "", "failure"))
        else:
            patch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("cannot spawn")))
        rig.call("close", claim["task_id"], "--as", "owner", rc=2)
    assert_parked(rig, "verify_error")


@pytest.mark.parametrize("terminal", ["exhausted", "archived", "parked", "provider_refused", "verify_error", "cancelled"])
def test_child_terminal_outcomes_leave_reviewing(rig, terminal):
    rig.submit()
    rig.dispatch()
    rig.execute()
    child = TaskDir("parent-review-1")
    with child.locked() as box:
        box["state"]["status"] = "exited" if terminal == "verify_error" else terminal
        box["state"]["runs"][-1]["state"] = terminal
    rig.call("reconcile")
    assert_parked(rig, "parent-review-1")


def test_exhausted_review_answer_is_refused_and_reservation_visible(rig):
    wd = rig.submit(review=one_round())
    rig.dispatch()
    rig.execute()
    rig.review(1, [finding()])
    rig.call("answer", "parent", "--as", "owner", "--text", "Try again", rc=3)
    assert_parked(rig, "round")
    status = rig.call("status", "parent", "--json")
    assert str(wd / "src") in status["review_reservation"]["write_set"]
    rig.call("archive", "parent", "--as", "owner", "--reason", "Use a new task")
    assert not scheduling.scope_conflicts("other", {"workdir": str(wd), "scope": ["src"]})


def test_dispatch_round_limit_independent_of_complete_flag(rig):
    rig.submit(review=one_round())
    rig.dispatch()
    rig.execute()
    rig.review(1, [finding()])
    with TaskDir("parent").locked() as box:
        box["state"]["review"]["complete"] = False
        box["state"]["status"] = "answered"
    rig.call("dispatch", "parent", "--as", "owner", rc=3)
    assert len(TaskDir("parent").state()["runs"]) == 1


def test_begin_refuses_a_late_pass_beyond_round_limit(rig):
    rig.submit(review=one_round())
    rig.dispatch()
    rig.execute()
    rig.review(1, [finding()])
    parent = TaskDir("parent")
    with parent.locked() as box:
        st = box["state"]
        extra = {**st["runs"][-1], "run_id": "r2", "state": "exited"}
        st["runs"].append(extra)
        st["status"] = "exited"
        st["review"]["complete"] = False
    parent.run_dir("r2").mkdir()
    (parent.run_dir("r2") / "receipt.json").write_text((parent.run_dir("r1") / "receipt.json").read_text())
    rig.call("close", "parent", "--as", "owner")
    assert_parked(rig, "round budget")
    assert len(parent.state()["review"]["rounds"]) == 1


def test_default_review_root_matches_station_root(rig, env):
    config_change(env, lambda c: c["pools"]["audit"].pop("root"))
    rig.submit()
    rig.dispatch()
    rig.execute()
    wd = Path(TaskDir("parent-review-1").spec()["workdir"])
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    args = pool.station_argv("audit", "audit-st-1", {**cfg["pools"]["audit"], "host": "codex"}, cfg)
    assert wd.is_relative_to(Path(args[args.index("-C") + 1]))
    pool.update_station("audit", "audit-st-1", root=args[args.index("-C") + 1])
    rig.review(1, [])
    assert TaskDir("parent").state()["status"] == "verified"


def test_review_cannot_use_station_with_stale_writable_root(rig, tmp_path):
    rig.submit()
    rig.dispatch()
    rig.execute()
    pool.update_station("audit", "audit-st-1", root=str(tmp_path / "unrelated"))
    assert cli.claim_card("audit", "audit-st-1")[1]["result"] == "EMPTY"
    rig.call("reconcile")
    assert_parked(rig, "writable root")


def test_doctor_checks_review_workspace_writability(rig, env, tmp_path):
    (tmp_path / "audit/.yardgoat-reviews").write_text("not a directory")
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    report = doctor.run(cfg, name="audit")
    assert not report["ok"]
    assert any(c["name"] == "review_workspace" and not c["ok"] for c in report["checks"])


def test_review_card_carries_answers_and_prior_findings(rig):
    rig.submit()
    rig.call("park", "parent", "--as", "owner", "--question", "Clarify acceptance")
    rig.call("answer", "parent", "--as", "owner", "--text", "Concurrent writes must preserve audit log order")
    rig.dispatch()
    execution = rig.execute()
    child = TaskDir("parent-review-1")
    card = Path(child.spec()["card"]).read_text()
    assert Path(execution["prompt"]).read_text() in card
    assert "Concurrent writes must preserve audit log order" in card
    rig.review(1, [finding()])
    rig.execute()
    card = Path(TaskDir("parent-review-2").spec()["card"]).read_text()
    assert "Concurrent writes must preserve audit log order" in card
    assert "Lost update" in card
    assert "confirm" in card.lower() and "previous" in card.lower()


def test_direct_execution_prompt_is_frozen_into_review_card(rig, env):
    rig.submit(adapter={"kind": "direct"})
    rig.call("park", "parent", "--as", "owner", "--question", "Clarify")
    rig.call("answer", "parent", "--as", "owner", "--text", "Preserve audit order")
    rig.dispatch()
    wait_exit(env, "parent")
    rig.call("reconcile")
    card = Path(TaskDir("parent-review-1").spec()["card"]).read_text()
    prompt = TaskDir("parent").run_dir("r1") / "prompt.md"
    assert prompt.read_text() in card and "Preserve audit order" in card
    rig.review(1, [])
    assert TaskDir("parent").state()["status"] == "verified"


@pytest.mark.parametrize("mutation", ["new_file", "out_extra", "empty_dir", "mode", "symlink"])
def test_review_verifier_rejects_unlisted_workspace_writes(rig, mutation):
    rig.submit()
    rig.dispatch()
    rig.execute()
    claim = rig.review(1, [], finish=False)
    wd = Path(claim["workdir"])
    if mutation == "new_file":
        (wd / "unexpected.txt").write_text("unauthorized")
    elif mutation == "out_extra":
        (wd / "out/unlisted.txt").write_text("unauthorized")
    elif mutation == "empty_dir":
        (wd / "unexpected").mkdir()
    elif mutation == "mode":
        (wd / "out").chmod(0o700)
    else:
        (wd / "unexpected").symlink_to(wd / "out")
    assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    rig.call("reconcile")
    assert TaskDir(claim["task_id"]).state()["verdict"]["verdict"] == "FAIL"
    assert TaskDir("parent").state()["status"] != "verified"


@pytest.mark.parametrize("mutation", ["delete", "modify"])
def test_review_verifier_preserves_existing_workspace_files(rig, tmp_path, mutation):
    rig.submit()
    wd = tmp_path / "audit/.yardgoat-reviews/parent-review-1"
    wd.mkdir(parents=True)
    note = wd / "read-only-note.txt"
    note.write_text("baseline")
    rig.dispatch()
    rig.execute()
    claim = rig.review(1, [], finish=False)
    if mutation == "delete":
        note.unlink()
    else:
        note.write_text("changed")
    assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    rig.call("reconcile")
    assert TaskDir(claim["task_id"]).state()["verdict"]["verdict"] == "FAIL"
