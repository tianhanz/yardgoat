"""M8b repair regressions: isolated stores, fake agents and local hooks only."""
import json
from pathlib import Path

import pytest

from test_m8b import (agents, call, config, controlled, endpoint, env, hook,
                      specfile, station, submit, wait_exit)
from yardgoat import cli, projects, review, sessions
from yardgoat.store import TaskDir, read_json


@pytest.mark.parametrize("placeholder", ["controller", "controller_session"])
@pytest.mark.parametrize("field", ["command", "recipient"])
def test_live_controller_notification_needs_no_endpoint(env, agents, tmp_path, placeholder, field):
    ids = controlled(env, agents)
    submit(tmp_path, agents)
    agents[0].call("park", "t-demo", "--as", "same", "--question", "question")
    template = "{" + placeholder + "}"
    route = hook(tmp_path, "agent", template if field == "recipient" else "fixed",
                 template if field == "command" else "message")
    config(env, notify=[route, hook(tmp_path, "human", "human")])
    assert not sessions.record(ids[0])["endpoint"]["verified"]
    result = call(env, "reconcile")
    assert [r["name"] for r in result["notified"]] == ["agent", "human"]
    value = "fixture-0" if placeholder == "controller" else ids[0]
    if field == "command":
        assert json.loads((tmp_path / "agent.jsonl").read_text()) == [value]
    else:
        assert result["notified"][0]["recipient"] == value
        agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
        assert [r["name"] for r in call(env, "reconcile")["notified"]] == ["agent"]
    assert call(env, "reconcile")["notified"] == []
    assert len((tmp_path / "human.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("unavailable", ["dead", "unknown", "missing", "unassigned"])
def test_controller_notification_waits_without_recording_sent(env, agents, tmp_path, monkeypatch, unavailable):
    from yardgoat import notify
    ids = controlled(env, agents)
    submit(tmp_path, agents)
    agents[0].call("park", "t-demo", "--as", "same", "--question", "question")
    cfg = config(env, notify=[hook(tmp_path, "agent", "{controller_session}", "{controller}"),
                              hook(tmp_path, "human", "human")])
    with monkeypatch.context() as patch:
        if unavailable in ("dead", "unknown"):
            patch.setattr(sessions, "liveness", lambda rec: unavailable)
        elif unavailable == "missing":
            patch.setattr(sessions, "record", lambda sid: None)
        else:
            state = projects.show("demo")
            state["controller"]["session_id"] = None
            patch.setattr(projects, "show", lambda project: state)
        assert [r["name"] for r in notify.notify_pending(cfg)] == ["human"]
        assert notify.notify_pending(cfg) == []
    sent = read_json(Path(env["YARDGOAT_HOME"]) / "notified/t-demo.json")["sent"]
    assert len(sent) == 1 and next(iter(sent.values()))["name"] == "human"
    assert [r["recipient"] for r in notify.notify_pending(cfg)] == [ids[0]]


@pytest.mark.parametrize("field", ["command", "recipient"])
def test_endpoint_placeholder_waits_for_verified_complete_endpoint(env, agents, tmp_path, field):
    ids = controlled(env, agents)
    submit(tmp_path, agents)
    agents[0].call("park", "t-demo", "--as", "same", "--question", "question")
    config(env, notify=hook(tmp_path, "endpoint", "{endpoint}" if field == "recipient" else "{controller_session}",
                           "{endpoint}" if field == "command" else "message"))
    sent = Path(env["YARDGOAT_HOME"]) / "notified/t-demo.json"
    endpoint(env, ids[0], verified=False)
    assert call(env, "reconcile")["notified"] == [] and not sent.exists()
    session_path = Path(env["YARDGOAT_HOME"]) / "sessions" / (ids[0] + ".json")
    record = read_json(session_path)
    record["endpoint"] = {"verified": True, "socket": "/tmp/fixture-socket"}
    session_path.write_text(json.dumps(record))
    assert call(env, "reconcile")["notified"] == [] and not sent.exists()
    endpoint(env, ids[0])
    assert len(call(env, "reconcile")["notified"]) == 1
    assert call(env, "reconcile")["notified"] == []


def review_config(env, tmp_path):
    root = tmp_path / "review-pool"
    root.mkdir()
    config(env, pools={"reviewers": {"host": "shell", "lane": "reviewers", "command": "true",
                                    "root": str(root), "max_stations": 0}})
    return root


def partial_followups(env, tmp_path, monkeypatch):
    root = review_config(env, tmp_path)
    path = specfile(tmp_path, review={"pool": "reviewers", "rounds": 1,
                                     "after_max_rounds": "pass_with_followups"})
    call(env, "submit", path, "--as", "same")
    call(env, "dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    call(env, "reconcile")
    worker = station("reviewers", root)
    rc, card = cli.claim_card("reviewers", worker)
    assert rc == 0 and card["task_id"] == "t-demo-review-1"
    wd = Path(card["workdir"])
    finding = {"id": "f1", "severity": "major", "file": "out/result.txt", "line": 1,
               "title": "fixture finding", "scenario": "fixture scenario", "fix": "fixture fix"}
    (wd / "out/findings.json").write_text(json.dumps([finding, {**finding, "id": "f2"}]))
    (wd / "out/REVIEW.md").write_text("Fixture review")
    assert cli.finish_card(card["task_id"], card["run_id"], worker)[0] == 0
    original = review.accept_generated

    def interrupt(raw, *args):
        if raw["task_id"] == "t-demo-followup-1-2":
            raise OSError("second follow-up interrupted")
        return original(raw, *args)

    with monkeypatch.context() as patch:
        patch.setattr(review, "accept_generated", interrupt)
        assert cli.main(["reconcile"]) == 0
    assert TaskDir("t-demo").state()["status"] == "parked"
    assert TaskDir("t-demo-followup-1-1").exists
    assert not TaskDir("t-demo-followup-1-2").exists


def frozen_identity(td):
    return (td.frozen_path.read_bytes(), (td.path / "spec.json").read_bytes(), td.state()["content_hash"])


def test_partial_followups_resume_after_parent_binding(env, agents, tmp_path, monkeypatch):
    ids = controlled(env, agents)
    partial_followups(env, tmp_path, monkeypatch)
    parent, first = TaskDir("t-demo"), TaskDir("t-demo-followup-1-1")
    before = frozen_identity(parent), frozen_identity(first)
    agents[0].call("project", "bind", parent.task_id, "--project", "demo", "--as", "same")
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    call(env, "answer", parent.task_id, "--as", "anyone", "--text", "Resume partial result")
    call(env, "reconcile")
    call(env, "reconcile")
    assert parent.state()["status"] == "verified", parent.state()["question"]
    assert before == (frozen_identity(parent), frozen_identity(first))
    second = TaskDir("t-demo-followup-1-2")
    assert all(td.state()["owner_ref"] == {"project": "demo"} for td in (first, second))
    assert first.state()["owner_history"] == []
    assert len([e for e in first.events() if e["kind"] == "task.accepted"]) == 1
    assert parent.state()["review"]["rounds"][0]["followups"] == [first.task_id, second.task_id]
    agents[0].call("dispatch", first.task_id, "--as", "same", rc=3)
    agents[1].call("dispatch", first.task_id, "--as", "fixture-1")


@pytest.mark.parametrize("conflict", ["takeover", "role", "content", "relation", "foreign_id"])
def test_bound_parent_does_not_adopt_conflicting_followup(env, agents, tmp_path, monkeypatch, conflict):
    controlled(env, agents)
    partial_followups(env, tmp_path, monkeypatch)
    parent, first = TaskDir("t-demo"), TaskDir("t-demo-followup-1-1")
    if conflict == "takeover":
        call(env, "takeover", first.task_id, "--from", "same", "--as", "specialist")
    elif conflict == "role":
        call(env, "project", "add", "other")
        agents[0].call("project", "claim", "other", "--expect-epoch", 0)
        agents[0].call("project", "bind", first.task_id, "--project", "other", "--as", "same")
    elif conflict == "content":
        Path(first.spec()["card"]).write_text("changed frozen card")
    else:
        # A valid but foreign accepted card at the deterministic id must remain untouched.
        from yardgoat import contract
        auth = read_json(first.frozen_path)
        if conflict == "foreign_id":
            auth["spec"].pop("review_followup")
        else:
            auth["spec"]["review_followup"]["task_id"] = "another-parent"
        auth["content_hash"] = contract.content_hash(auth["spec"], auth["frozen"])
        first.frozen_path.write_text(json.dumps(auth))
        (first.path / "spec.json").write_text(json.dumps(auth["spec"]))
        with first.locked() as box:
            box["state"]["content_hash"] = auth["content_hash"]
    original = first.state()
    agents[0].call("project", "bind", parent.task_id, "--project", "demo", "--as", "same")
    call(env, "answer", parent.task_id, "--as", "anyone", "--text", "Retry")
    call(env, "reconcile")
    assert parent.state()["status"] == "parked"
    assert first.state() == original
    assert parent.state()["review"]["rounds"][0]["status"] == "pending"
    if conflict == "foreign_id":
        assert "already exists" in parent.state()["question"]["text"]
    if conflict == "takeover":
        assert "independently taken over" in parent.state()["question"]["text"]
        call(env, "takeover", first.task_id, "--from", "specialist", "--as", "same")
        call(env, "answer", parent.task_id, "--as", "anyone", "--text", "Ownership coordinated")
        call(env, "reconcile")
        assert parent.state()["status"] == "verified"


@pytest.mark.parametrize("command", ["deliver", "archive"])
def test_manual_recovery_of_system_delivery_preserves_execution_control(env, agents, tmp_path, command):
    ids = controlled(env, agents)
    submit(tmp_path, agents, auto_close=True, on_pass=[{"name": "done", "command": "echo once >> delivered"}])
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    call(env, "reconcile")
    td = TaskDir("t-demo")
    records = read_json(td.run_dir("r1") / "receipt.json")["on_pass"]
    assert records[0]["on_behalf_of"] == {"project": "demo", "epoch": 1}
    events = [e for e in td.events() if e["kind"] != "task.on_pass_finished"]
    (td.path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    extra = ["--reason", "done"] if command == "archive" else []
    agents[1].call(command, "t-demo", "--as", "fixture-1", *extra)
    agents[1].call("deliver", "t-demo", "--as", "fixture-1")
    replayed = [e for e in td.events() if e["kind"] == "task.on_pass_finished"]
    assert len(replayed) == 1
    event = replayed[0]
    assert event["actor"] == "reconciler" and "actor_session" not in event and "epoch" not in event
    assert event["on_behalf_of"] == {"project": "demo", "epoch": 2}
    assert event["execution_control"] == {"on_behalf_of": {"project": "demo", "epoch": 1}}
    assert (tmp_path / "work/delivered").read_text().splitlines() == ["once"]


def legacy_review_task(env, agents, tmp_path, monkeypatch):
    review_config(env, tmp_path)
    path = specfile(tmp_path, owner={"project": "demo"}, review={"pool": "reviewers"})
    caller = agents[0].call("whoami", "--json")
    with monkeypatch.context() as patch:
        patch.setattr(sessions, "identify", lambda *a, **kw: caller)
        patch.setattr(review, "prepare", lambda *a: None)
        assert cli.main(["submit", str(path), "--as", "same"]) == 0
    return TaskDir("t-demo")


def test_new_controller_can_confirm_legacy_review_with_own_label(env, agents, tmp_path, monkeypatch):
    ids = controlled(env, agents)
    td = legacy_review_task(env, agents, tmp_path, monkeypatch)
    before = td.state()["content_hash"]
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    agents[1].call("confirm-review-channel", td.task_id, "--as", "fixture-1")
    event = next(e for e in td.events() if e["kind"] == "task.review_channel_confirmed")
    assert event["actor"] == "fixture-1" and event["actor_session"] == ids[1] and event["epoch"] == 2
    assert td.state()["owner"] == "fixture-0" and td.state()["content_hash"] == before
    assert td.spec()["review"]["channel"] == {"host": "shell", "lane": "reviewers"}


@pytest.mark.parametrize("legacy_record", [False, True])
def test_review_confirmation_crash_replay_preserves_original_controller(env, agents, tmp_path, monkeypatch, legacy_record):
    ids = controlled(env, agents)
    td = legacy_review_task(env, agents, tmp_path, monkeypatch)
    caller = agents[0].call("whoami", "--json")
    original = TaskDir.event

    class Crash(BaseException):
        pass

    def crash(self, kind, actor, **fields):
        if kind == "task.review_channel_confirmed":
            raise Crash()
        return original(self, kind, actor, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "identify", lambda *a, **kw: caller)
        patch.setattr(TaskDir, "event", crash)
        with pytest.raises(Crash):
            cli.main(["confirm-review-channel", td.task_id, "--as", "fixture-0"])
    confirmation = read_json(td.frozen_path)["review_confirmation"]
    assert confirmation["control"] == {"actor_session": ids[0], "epoch": 1}
    if legacy_record:
        auth = read_json(td.frozen_path)
        auth["review_confirmation"].pop("control")
        td.frozen_path.write_text(json.dumps(auth))
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    agents[1].call("confirm-review-channel", td.task_id, "--as", "fixture-1")
    agents[1].call("confirm-review-channel", td.task_id, "--as", "fixture-1")
    events = [e for e in td.events() if e["kind"] == "task.review_channel_confirmed"]
    assert len(events) == 1
    event = events[0]
    assert event["actor"] == "fixture-0"
    if legacy_record:
        assert "actor_session" not in event and "epoch" not in event
    else:
        assert event["actor_session"] == ids[0] and event["epoch"] == 1
    assert event["at"] == confirmation["at"] and event["channel"] == confirmation["channel"]
    assert event["replayed_by"] == {"actor": "fixture-1", "actor_session": ids[1], "epoch": 2}


@pytest.mark.parametrize("system", [False, True])
def test_verifier_receipt_freezes_identity_before_event(env, agents, tmp_path, monkeypatch, system):
    ids = controlled(env, agents)
    submit(tmp_path, agents, auto_close=system)
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    td = TaskDir("t-demo")
    caller = agents[0].call("whoami", "--json")
    original = TaskDir.event
    expected = ({"actor": "reconciler", "on_behalf_of": {"project": "demo", "epoch": 1}} if system
                else {"actor_session": ids[0], "epoch": 1})

    def handoff_before_event(self, kind, actor, **fields):
        if kind == "run.verified" and self.task_id == td.task_id:
            verifier = read_json(td.run_dir("r1") / "receipt.json")["verifier"]
            assert verifier["checked_by"] == ("reconciler" if system else "same")
            assert {k: verifier[k] for k in expected} == expected
            assert td.state()["status"] == "verified"
            projects.handoff("demo", caller, ids[1], 1)
        return original(self, kind, actor, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "identify", lambda *a, **kw: caller)
        patch.setattr(TaskDir, "event", handoff_before_event)
        assert cli.main(["reconcile"] if system else ["close", td.task_id, "--as", "same"]) == 0
    event = next(e for e in td.events() if e["kind"] == "run.verified")
    assert {k: event[k] for k in expected} == expected
    assert ("actor_session" not in event) == system
