"""Project-owned tasks, using isolated stores and M8a's detached fake agents."""
import json
from pathlib import Path

import pytest

from test_m8a import env, agents, call, controlled
from test_contract import make_task, wait_exit


def config(env, **changes):
    path = Path(env["YARDGOAT_CONFIG"])
    value = json.loads(path.read_text())
    value.update(changes)
    path.write_text(json.dumps(value))
    return value


def specfile(tmp_path, **changes):
    path = make_task(tmp_path, verify="true")
    value = json.loads(path.read_text())
    value.update(changes)
    path.write_text(json.dumps(value))
    return path


def submit(tmp_path, agents, **changes):
    path = specfile(tmp_path, owner={"project": "demo"}, **changes)
    agents[0].call("submit", path, "--as", "same")
    return path


def test_role_reference_and_display_owner(env, agents, tmp_path):
    controlled(env, agents)
    submit(tmp_path, agents)
    state = call(env, "status", "t-demo", "--json")
    assert state["owner_ref"] == {"project": "demo"}
    assert state["owner"] == "fixture-0"


def test_same_label_is_not_project_permission(env, agents, tmp_path):
    controlled(env, agents)
    config(env, max_concurrent=0)
    submit(tmp_path, agents)
    agents[1].call("dispatch", "t-demo", "--as", "same", rc=3)
    agents[1].call("archive", "t-demo", "--as", "same", "--reason", "done", rc=3)


def test_project_takeover_refused(env, agents, tmp_path):
    controlled(env, agents)
    submit(tmp_path, agents)
    call(env, "takeover", "t-demo", "--from", "same", "--as", "new", rc=3)


def test_auto_close_must_be_boolean(env, tmp_path):
    path = specfile(tmp_path, auto_close="false")
    call(env, "submit", path, "--as", "same", rc=2)


def test_bind_preserves_frozen_content(env, agents, tmp_path):
    from yardgoat.store import TaskDir
    controlled(env, agents)
    path = specfile(tmp_path)
    call(env, "submit", path, "--as", "same")
    td = TaskDir("t-demo")
    before = (td.path / "spec.json").read_bytes(), td.frozen_path.read_bytes(), td.state()["content_hash"]
    agents[1].call("project", "bind", "t-demo", "--project", "demo", "--as", "same", rc=3)
    agents[0].call("project", "bind", "t-demo", "--project", "demo", "--as", "wrong", rc=3)
    agents[0].call("project", "bind", "t-demo", "--project", "demo", "--as", "same")
    assert td.state()["owner_ref"] == {"project": "demo"}
    assert before == ((td.path / "spec.json").read_bytes(), td.frozen_path.read_bytes(), td.state()["content_hash"])


def test_submit_project_requires_controller_and_flag_is_idempotent(env, agents, tmp_path):
    controlled(env, agents)
    path = specfile(tmp_path)
    call(env, "submit", path, "--project-owned", "--as", "same", rc=3)
    agents[1].call("submit", path, "--project-owned", "--as", "same", rc=3)
    agents[0].call("submit", path, "--project-owned", "--as", "same")
    assert agents[0].call("submit", path, "--project-owned", "--as", "same")["result"] == "already-accepted"
    assert call(env, "status", "t-demo", "--json")["owner_ref"] == {"project": "demo"}


@pytest.mark.parametrize("command,extra", [
    ("dispatch", []), ("close", []), ("archive", ["--reason", "done"]),
    ("park", ["--question", "question"]), ("cancel", []), ("deliver", []),
    ("confirm-review-channel", []),
])
def test_all_project_control_operations_refuse_same_string_noncontroller(env, agents, tmp_path, command, extra):
    controlled(env, agents)
    submit(tmp_path, agents)
    result = agents[1].raw(command, "t-demo", "--as", "fixture-0", *extra)
    assert result["rc"] == 3 and "controller" in result["err"]
    result = call(env, command, "t-demo", "--as", "fixture-0", *extra, rc=3)
    unknown = call(env, "whoami", "--json", rc=3)
    assert unknown["session_id"] is None and unknown["reason"] in result


def test_handoff_changes_permissions_without_touching_cards(env, agents, tmp_path):
    from yardgoat.store import TaskDir
    ids = controlled(env, agents)
    submit(tmp_path, agents)
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    td = TaskDir("t-demo")
    before = (td.path / "task.json").read_bytes()
    agents[1].call("close", "t-demo", "--as", "same", rc=3)
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    assert (td.path / "task.json").read_bytes() == before
    agents[0].call("close", "t-demo", "--as", "same", rc=3)
    agents[1].call("close", "t-demo", "--as", "same")
    agents[0].call("archive", "t-demo", "--as", "same", "--reason", "done", rc=3)
    agents[1].call("archive", "t-demo", "--as", "same", "--reason", "done")
    event = td.events()[-1]
    assert event["actor_session"] == ids[1] and event["epoch"] == 2
    assert td.state()["owner"] == "fixture-0"


def test_unknown_legacy_owner_answer_and_approval_unchanged(env, agents, tmp_path):
    controlled(env, agents)
    submit(tmp_path, agents, risk="l3")
    config(env, max_concurrent=0, approvers=["reviewer"])
    call(env, "approve", "t-demo", "--as", "same", rc=3)
    call(env, "approve", "t-demo", "--as", "reviewer")
    agents[0].call("park", "t-demo", "--as", "same", "--question", "continue?")
    call(env, "answer", "t-demo", "--as", "anyone", "--text", "yes")
    call(env, "reconcile")
    from yardgoat.store import TaskDir
    td = TaskDir("t-demo")
    assert td.state()["status"] == "queued"
    event = next(e for e in td.events() if e["kind"] == "run.queued")
    assert event["actor"] == "reconciler" and event["on_behalf_of"] == {"project": "demo", "epoch": 1}
    path = specfile(tmp_path, task_id="legacy-card")
    call(env, "submit", path, "--as", "same")
    call(env, "dispatch", "legacy-card", "--as", "same")
    call(env, "cancel", "legacy-card", "--as", "same")
    call(env, "archive", "legacy-card", "--as", "same", "--reason", "done")


@pytest.mark.parametrize("command", ["dispatch", "close", "archive"])
def test_aba_between_authorization_and_commit_refused(env, agents, tmp_path, monkeypatch, command):
    from yardgoat import cli, projects, sessions
    from yardgoat.store import TaskDir
    ids = controlled(env, agents)
    submit(tmp_path, agents)
    if command == "close":
        agents[0].call("dispatch", "t-demo", "--as", "same")
        wait_exit(env, "t-demo")
    first, second = [a.call("whoami", "--json") for a in agents]
    td = TaskDir("t-demo")
    before = (td.path / "task.json").read_bytes(), (td.path / "events.jsonl").read_bytes()
    original = projects.authorize_control
    def moved(project, caller):
        ticket = original(project, caller)
        projects.handoff(project, first, ids[1], 1)
        projects.handoff(project, second, ids[0], 2)
        return ticket
    monkeypatch.setattr(sessions, "identify", lambda *a, **kw: first)
    monkeypatch.setattr(projects, "authorize_control", moved)
    args = [command, "t-demo", "--as", "same"]
    if command == "archive":
        args.extend(["--reason", "done"])
    assert cli.main(args) == 3
    assert before == ((td.path / "task.json").read_bytes(), (td.path / "events.jsonl").read_bytes())


def test_aba_while_verifier_runs_fences_verdict_commit(env, agents, tmp_path, monkeypatch):
    from yardgoat import cli, projects, sessions
    from yardgoat.store import TaskDir
    ids = controlled(env, agents)
    submit(tmp_path, agents)
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    first, second = [a.call("whoami", "--json") for a in agents]
    td = TaskDir("t-demo")
    original = cli.subprocess.run
    def verify(*args, **kwargs):
        projects.handoff("demo", first, ids[1], 1)
        projects.handoff("demo", second, ids[0], 2)
        return original(*args, **kwargs)
    monkeypatch.setattr(sessions, "identify", lambda *a, **kw: first)
    monkeypatch.setattr(cli.subprocess, "run", verify)
    assert cli.do_close(td, "same") == (3, None)
    assert td.state()["verdict"] is None and td.state()["status"] == "exited"
    assert "verifier" not in json.loads((td.run_dir("r1") / "receipt.json").read_text())


def test_reconciler_verifies_and_delivers_without_live_controller(env, agents, tmp_path):
    from yardgoat.store import TaskDir
    controlled(env, agents)
    submit(tmp_path, agents, auto_close=True, on_pass=[{"name": "done", "command": "touch delivered"}])
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    agents[0].stop()
    call(env, "reconcile")
    td = TaskDir("t-demo")
    assert td.state()["status"] == "verified"
    assert (tmp_path / "work/delivered").exists()
    events = [e for e in td.events() if e["kind"] in ("run.verified", "task.on_pass_started", "task.on_pass_finished")]
    assert len(events) == 3
    assert all(e["actor"] == "reconciler" and e["on_behalf_of"] == {"project": "demo", "epoch": 1} for e in events)
    assert all("actor_session" not in e for e in events)


def station(name, root):
    import os
    from yardgoat import pool
    pool.update_station(name, name + "-st-1", station=name + "-st-1", pool=name, state="running",
                        pid=os.getpid(), current=None, tasks=[], channel={"host": "shell", "lane": name}, root=str(root))
    return name + "-st-1"


def test_station_finishes_after_project_handoff(env, agents, tmp_path):
    from yardgoat import cli
    ids = controlled(env, agents)
    config(env, pools={"workers": {"host": "shell", "lane": "workers", "command": "true", "root": str(tmp_path), "max_stations": 0}})
    submit(tmp_path, agents, adapter={"kind": "pool", "pool": "workers"})
    agents[0].call("dispatch", "t-demo", "--as", "same")
    worker = station("workers", tmp_path)
    rc, card = cli.claim_card("workers", worker)
    assert rc == 0 and card["result"] == "CLAIMED"
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    assert cli.finish_card("t-demo", "r1", worker)[0] == 0
    agents[1].call("close", "t-demo", "--as", "same")


def test_review_child_and_followup_inherit_bound_role(env, agents, tmp_path):
    from yardgoat import cli
    from yardgoat.store import TaskDir
    ids = controlled(env, agents)
    root = tmp_path / "review-pool"
    root.mkdir()
    config(env, pools={"reviewers": {"host": "shell", "lane": "reviewers", "command": "true", "root": str(root), "max_stations": 0}})
    # Bind a legacy spec: generated tasks must inherit task.state.owner_ref,
    # including when there is no owner object in the frozen parent spec.
    path = specfile(tmp_path, review={"pool": "reviewers", "rounds": 1, "after_max_rounds": "pass_with_followups"})
    call(env, "submit", path, "--as", "same")
    agents[0].call("project", "bind", "t-demo", "--project", "demo", "--as", "same")
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    call(env, "reconcile")
    child = TaskDir("t-demo-review-1")
    assert child.state()["owner_ref"] == {"project": "demo"}
    before = (child.path / "task.json").read_bytes()
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    assert (child.path / "task.json").read_bytes() == before
    worker = station("reviewers", root)
    rc, card = cli.claim_card("reviewers", worker)
    assert rc == 0 and card["task_id"] == child.task_id
    wd = Path(card["workdir"])
    finding = {"id": "f1", "severity": "major", "file": "out/result.txt", "line": 1,
               "title": "fixture finding", "scenario": "fixture scenario", "fix": "fixture fix"}
    (wd / "out/findings.json").write_text(json.dumps([finding]))
    (wd / "out/REVIEW.md").write_text("Fixture review")
    assert cli.finish_card(child.task_id, "r1", worker)[0] == 0
    call(env, "reconcile")
    followup = TaskDir("t-demo-followup-1-1")
    assert followup.exists and followup.state()["owner_ref"] == {"project": "demo"}
    assert followup.state()["owner"] == "fixture-1"
    assert TaskDir("t-demo").state()["status"] == "verified"
    assert not followup.state()["runs"]
    assert all(e["actor"] == "reconciler" for e in child.events() if e["kind"] in ("task.accepted", "run.queued", "run.verified"))


def wait_file(path):
    import time
    until = time.monotonic() + 8
    while not path.exists() and time.monotonic() < until:
        time.sleep(0.01)
    assert path.exists(), path


def test_handoff_allows_started_delivery_step_but_refuses_next(env, agents, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from yardgoat import projects
    from yardgoat.store import TaskDir
    ids = controlled(env, agents)
    first = agents[0].call("whoami", "--json")
    submit(tmp_path, agents, on_pass=[
        {"name": "first", "command": "touch started; for n in $(seq 1 500); do test -e release && break; sleep .01; done; touch completed"},
        {"name": "second", "command": "touch next-step"}])
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    wd = tmp_path / "work"
    with ThreadPoolExecutor(1) as executor:
        closing = executor.submit(agents[0].raw, "close", "t-demo", "--as", "same")
        try:
            wait_file(wd / "started")
            projects.handoff("demo", first, ids[1], 1)
        finally:
            (wd / "release").touch()
        assert closing.result()["rc"] == 3
    assert (wd / "completed").exists() and not (wd / "next-step").exists()
    td = TaskDir("t-demo")
    assert td.state()["on_pass"][0]["exit_code"] == 0
    assert not td.state().get("delivery_reservation")
    agents[1].call("deliver", "t-demo", "--as", "same")
    assert (wd / "next-step").exists()
    finished = [e for e in td.events() if e["kind"] == "task.on_pass_finished"]
    assert [e["epoch"] for e in finished] == [1, 2]


def endpoint(env, sid, verified=True):
    path = Path(env["YARDGOAT_HOME"]) / "sessions" / (sid + ".json")
    value = json.loads(path.read_text())
    value["endpoint"] = {"socket": "/tmp/fixture-socket", "pane_id": "%7", "verified": verified}
    path.write_text(json.dumps(value))


def hook(tmp_path, name, recipient="", *arguments):
    import sys
    script = tmp_path / "hook.py"
    script.write_text("import json,sys\nwith open(sys.argv[1], 'a') as f: f.write(json.dumps(sys.argv[2:])+'\\n')\n")
    return {"name": name, "recipient": recipient,
            "command": [sys.executable, str(script), str(tmp_path / (name + ".jsonl")), *arguments]}


def test_notifications_follow_current_controller_and_retry_unavailable_route(env, agents, tmp_path):
    from yardgoat.store import TaskDir
    ids = controlled(env, agents)
    submit(tmp_path, agents)
    agents[0].call("park", "t-demo", "--as", "same", "--question", "question")
    config(env, notify=[hook(tmp_path, "agent", "{controller_session}", "{controller}", "{controller_session}", "{endpoint}"),
                        hook(tmp_path, "human", "human")])
    # Endpoints initially unverified. Only the human route records success.
    result = call(env, "reconcile")
    assert [r["name"] for r in result["notified"]] == ["human"]
    endpoint(env, ids[0])
    assert [r["recipient"] for r in call(env, "reconcile")["notified"]] == [ids[0]]
    endpoint(env, ids[1])
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    assert [r["recipient"] for r in call(env, "reconcile")["notified"]] == [ids[1]]
    assert call(env, "reconcile")["notified"] == []
    messages = [json.loads(line) for line in (tmp_path / "agent.jsonl").read_text().splitlines()]
    assert [row[0] for row in messages] == ["fixture-0", "fixture-1"]
    assert json.loads(messages[-1][2]) == {"socket": "/tmp/fixture-socket", "pane_id": "%7"}
    assert len((tmp_path / "human.jsonl").read_text().splitlines()) == 1
    agents[1].stop()
    with TaskDir("t-demo").locked() as box:
        box["state"]["question"]["text"] = "new question"
    sent = Path(env["YARDGOAT_HOME"]) / "notified/t-demo.json"
    before = len(json.loads(sent.read_text())["sent"])
    assert [r["name"] for r in call(env, "reconcile")["notified"]] == ["human"]
    assert len(json.loads(sent.read_text())["sent"]) == before + 1


@pytest.mark.parametrize("field", ["recipient", "command"])
def test_template_errors_disable_only_bad_route_and_recover(env, tmp_path, caplog, field):
    from yardgoat import notify
    path = specfile(tmp_path)
    call(env, "submit", path, "--as", "same")
    call(env, "park", "t-demo", "--as", "same", "--question", "question")
    bad = hook(tmp_path, "bad", "person")
    if field == "recipient":
        bad[field] = "{missing}"
    else:
        bad[field].append("{missing}")
    cfg = config(env, notify=[bad, hook(tmp_path, "ok", "person")])
    assert [r["name"] for r in notify.notify_pending(cfg)] == ["ok"]
    assert "configuration error" in caplog.text
    caplog.clear()
    assert notify.notify_pending(cfg) == [] and not caplog.records
    diagnostic = call(env, "status", "t-demo", "--json")["notify_errors"]
    assert diagnostic[0]["route"] == "bad" and diagnostic[0]["disabled"]
    report = call(env, "doctor", "--host", "shell", "--json", rc=1)
    assert any(c["name"] == "notify_config" and not c["ok"] for c in report["checks"])
    cfg["notify"][0] = hook(tmp_path, "bad", "person")
    config(env, notify=cfg["notify"])
    assert [r["name"] for r in notify.notify_pending(cfg)] == ["bad"]
    assert call(env, "status", "t-demo", "--json")["notify_errors"] == []


def test_status_inbox_whoami_report_and_transfer_visibility(env, agents, tmp_path):
    controlled(env, agents)
    submit(tmp_path, agents, project="legacy-display-project")
    agents[0].call("park", "t-demo", "--as", "same", "--question", "question")
    identity = agents[0].call("whoami", "--json")
    assert identity["projects"] == ["demo"] and identity["unfinished_tasks"] == 1
    assert identity["project_task_counts"] == {"demo": 1}
    assert agents[1].call("whoami", "--json")["unfinished_tasks"] == 0
    transfer = agents[0].call("project", "transfer", "begin", "demo", "--expect-epoch", 1)
    for row in [call(env, "status", "t-demo", "--json"), call(env, "inbox", "--json")[0]]:
        assert row["project"] == "demo" and row["transferring"]
        assert row["controller"]["label"] == "fixture-0" and row["controller"]["epoch"] == 1
    rendered = agents[0].raw("status", "t-demo")["out"]
    assert "project=demo" in rendered and "epoch=1" in rendered and "transferring=True" in rendered
    assert "project=demo" in agents[0].raw("inbox")["out"]
    assert call(env, "report", "--by", "project", "--json")["groups"][0]["name"] == "demo"
    agents[0].call("cancel", "t-demo", "--as", "same", rc=3)
    call(env, "project", "transfer", "abort", "demo", "--transfer", transfer["transfer_id"], "--token", transfer["token"])
    agents[0].call("archive", "t-demo", "--as", "same", "--reason", "done")
    assert agents[0].call("whoami", "--json")["unfinished_tasks"] == 0


def test_new_controller_dispatches_and_cancels_with_original_display_label(env, agents, tmp_path):
    ids = controlled(env, agents)
    config(env, max_concurrent=0)
    submit(tmp_path, agents)
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    agents[0].call("dispatch", "t-demo", "--as", "fixture-0", rc=3)
    call(env, "dispatch", "t-demo", "--as", "reconciler", rc=3)
    agents[1].call("dispatch", "t-demo", "--as", "fixture-0")
    agents[0].call("cancel", "t-demo", "--as", "fixture-0", rc=3)
    agents[1].call("cancel", "t-demo", "--as", "fixture-0")


def test_legacy_controller_placeholders_are_empty(env, tmp_path):
    path = specfile(tmp_path)
    call(env, "submit", path, "--as", "same")
    call(env, "park", "t-demo", "--as", "same", "--question", "question")
    config(env, notify=hook(tmp_path, "legacy", "{controller_session}", "{controller}", "{controller_session}", "{endpoint}"))
    assert call(env, "reconcile")["notified"][0]["recipient"] == ""
    assert json.loads((tmp_path / "legacy.jsonl").read_text()) == ["", "", ""]


def test_unregistered_or_missing_project_cannot_submit(env, agents, tmp_path):
    path = specfile(tmp_path, owner={"project": "demo"})
    call(env, "project", "add", "demo")
    agents[0].call("submit", path, "--as", "same", rc=3)
    agents[0].call("session", "register", "--label", "fixture")
    agents[0].call("submit", path, "--as", "same", rc=3)
    value = json.loads(path.read_text())
    value["owner"]["project"] = "missing"
    path.write_text(json.dumps(value))
    assert "does not exist" in agents[0].call("submit", path, "--as", "same", rc=3)


def test_delivery_aba_before_pipe_grant_never_executes_shell(env, agents, tmp_path, monkeypatch):
    from yardgoat import cli, delivery, projects, sessions
    from yardgoat.store import TaskDir
    ids = controlled(env, agents)
    submit(tmp_path, agents, on_pass=[{"name": "step", "command": "touch forbidden-step"}])
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    first, second = [a.call("whoami", "--json") for a in agents]
    original = delivery.process_identity
    def moved(pid):
        identity = original(pid)
        projects.handoff("demo", first, ids[1], 1)
        projects.handoff("demo", second, ids[0], 2)
        return identity
    monkeypatch.setattr(sessions, "identify", lambda *a, **kw: first)
    monkeypatch.setattr(delivery, "process_identity", moved)
    # group_alive also calls process_identity after refusal; only inject once.
    def once(pid):
        monkeypatch.setattr(delivery, "process_identity", original)
        return moved(pid)
    monkeypatch.setattr(delivery, "process_identity", once)
    rc, verdict = cli.do_close(TaskDir("t-demo"), "same")
    assert rc == 3 and verdict == "PASS"
    assert not (tmp_path / "work/forbidden-step").exists()
    assert not TaskDir("t-demo").state().get("delivery_reservation")


def test_reconcile_delivery_receipt_replay_does_not_impersonate_old_session(env, agents, tmp_path):
    from yardgoat.store import TaskDir
    ids = controlled(env, agents)
    submit(tmp_path, agents, on_pass=[{"name": "one", "command": "true"}, {"name": "two", "command": "false"}])
    agents[0].call("dispatch", "t-demo", "--as", "same")
    wait_exit(env, "t-demo")
    agents[0].call("close", "t-demo", "--as", "same", rc=1)
    td = TaskDir("t-demo")
    # Simulate receipt-first persistence without the corresponding finished events.
    events = [e for e in td.events() if e["kind"] != "task.on_pass_finished"]
    (td.path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", 1)
    call(env, "reconcile")
    replayed = [e for e in td.events() if e["kind"] == "task.on_pass_finished"]
    assert len(replayed) == 2
    for event in replayed:
        assert event["actor"] == "reconciler" and "actor_session" not in event
        assert event["on_behalf_of"] == {"project": "demo", "epoch": 2}
        assert event["execution_control"] == {"actor_session": ids[0], "epoch": 1}
