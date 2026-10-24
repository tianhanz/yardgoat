"""Recheck project authority after lock acquisition using an injected proc tree."""
from contextlib import contextmanager

import pytest

from test_m8a import env
from test_m8c import TRANSFER, begin, invoke, ready, world
from yardgoat import projects, sessions


WAIT_REASON = "changed while waiting for project lock"


def after_lock(monkeypatch, change):
    original = projects.locked

    @contextmanager
    def changed(project_id):
        with original(project_id) as state:
            change()
            yield state

    monkeypatch.setattr(projects, "locked", changed)


def refused_without_write(action, reason=WAIT_REASON):
    before = projects.path("demo").read_bytes()
    with pytest.raises(sessions.Refused, match=reason):
        action()
    assert projects.path("demo").read_bytes() == before


def change_supervision(w, mutation):
    if mutation == "exit":
        del w.proc.rows[20]
    elif mutation == "reuse":
        w.proc.rows[20]["start_ticks"] = "201"
    elif mutation == "reparent":
        w.proc.rows[30]["ppid"] = 10
    elif mutation == "uid":
        w.proc.rows[20]["uid"] = sessions.os.getuid() + 1
    elif mutation == "boot":
        w.proc.boot = "next-boot"
    elif mutation == "station":
        w.proc.rows[10]["env"] = {"YARDGOAT_STATION": "fixture"}
    elif mutation == "caller_reparent":
        w.proc.rows[21]["ppid"] = 10


@pytest.mark.parametrize("mutation", ["exit", "reuse", "reparent", "uid", "boot", "station", "caller_reparent"])
def test_supervisor_begin_rechecks_full_authority_after_lock(world, monkeypatch, mutation):
    w = world
    ready(w)
    w.pid = 21
    after_lock(monkeypatch, lambda: change_supervision(w, mutation))
    refused_without_write(lambda: begin(w, for_session=w.a["session_id"], cfg=w.cfg))


@pytest.mark.parametrize("terminal", [False, True], ids=["pending", "aborted"])
def test_supervisor_begin_replay_rechecks_before_reading_result(world, monkeypatch, terminal):
    w = world
    ready(w)
    w.pid = 21
    begin(w, for_session=w.a["session_id"], cfg=w.cfg)
    if terminal:
        projects.abort("demo", TRANSFER, token_file=w.token)
    after_lock(monkeypatch, lambda: change_supervision(w, "exit"))
    refused_without_write(lambda: begin(w, for_session=w.a["session_id"], cfg=w.cfg))


@pytest.mark.parametrize("mutation", ["exit", "reuse"])
def test_supervisor_invalid_before_replay_keeps_initial_refusal(world, mutation):
    w = world
    ready(w)
    w.pid = 21
    begin(w, for_session=w.a["session_id"], cfg=w.cfg)
    change_supervision(w, mutation)
    before = projects.path("demo").read_bytes()
    with pytest.raises(sessions.Refused, match="supervisor refused:") as error:
        begin(w, for_session=w.a["session_id"], cfg=w.cfg)
    assert WAIT_REASON not in str(error.value)
    assert projects.path("demo").read_bytes() == before


@pytest.mark.parametrize("replay", [False, True])
def test_supervisor_abort_rechecks_before_mutation_or_replay(world, monkeypatch, replay):
    w = world
    ready(w)
    w.pid = 21
    begin(w, for_session=w.a["session_id"], cfg=w.cfg)

    def abort():
        return projects.abort("demo", TRANSFER, for_session=w.a["session_id"], cfg=w.cfg)

    if replay:
        abort()
    after_lock(monkeypatch, lambda: change_supervision(w, "exit"))
    refused_without_write(abort)


def session_action(w, action, replay=False):
    if action == "claim":
        sessions.register(w.a)
        projects.add("demo")
        return w.a, lambda: projects.claim("demo", w.a, 0)
    ready(w, "dead")
    if action == "reclaim":
        w.proc.rows[30]["state"] = "Z"
        return w.b, lambda: projects.reclaim("demo", w.b, 1)
    if action == "handoff":
        return w.a, lambda: projects.handoff("demo", w.a, w.b["session_id"], 1)
    if action == "begin":
        if replay:
            begin(w)
        return w.a, lambda: begin(w)
    begin(w)
    if action == "abort":
        call = lambda: projects.abort("demo", TRANSFER, caller=w.a)
        caller = w.a
    else:
        call = lambda: projects.commit("demo", TRANSFER, caller=w.b, token_file=w.token)
        caller = w.b
    if replay:
        call()
    return caller, call


@pytest.mark.parametrize("mutation", ["dead", "reuse", "unknown"])
@pytest.mark.parametrize("action,replay", [
    ("claim", False), ("reclaim", False), ("handoff", False),
    ("begin", False), ("begin", True), ("abort", False), ("abort", True),
    ("commit", False), ("commit", True),
])
def test_registered_caller_rechecked_before_state_or_replay(world, monkeypatch, action, replay, mutation):
    w = world
    caller, call = session_action(w, action, replay)
    pid = caller["identity"]["pid"]

    def change():
        if mutation == "dead":
            w.proc.rows[pid]["state"] = "Z"
        elif mutation == "reuse":
            w.proc.rows[pid]["start_ticks"] += "1"
        else:
            del w.proc.rows[pid]
            w.proc.confirm_absence = False

    after_lock(monkeypatch, change)
    refused_without_write(call)


def test_assign_rechecks_administrator_under_project_lock(world, monkeypatch):
    w = world
    ready(w)
    cfg = {"project_admins": ["operator"]}
    after_lock(monkeypatch, lambda: cfg["project_admins"].clear())
    refused_without_write(lambda: projects.assign("demo", w.b["session_id"], 1, cfg, "operator", "recover"))


@pytest.mark.parametrize("action", ["claim", "reclaim", "begin"])
def test_lock_wait_refusal_keeps_cli_exit_three(world, invoke, monkeypatch, action):
    w = world
    if action == "begin":
        ready(w)
        w.pid = 21
        args = ("project", "transfer", "begin", "demo", "--expect-epoch", 1,
                "--transfer", TRANSFER, "--token-file", w.token, "--for-session", w.a["session_id"])
        change = lambda: change_supervision(w, "exit")
    else:
        caller, _ = session_action(w, action)
        w.pid = 31 if action == "claim" else 41
        args = ("project", action, "demo", "--expect-epoch", 0 if action == "claim" else 1)
        change = lambda: w.proc.rows[caller["identity"]["pid"]].update(state="Z")
    before = projects.path("demo").read_bytes()
    after_lock(monkeypatch, change)
    assert WAIT_REASON in invoke(*args, rc=3)
    assert projects.path("demo").read_bytes() == before


@pytest.mark.parametrize("action", ["abort", "commit"])
def test_bearer_token_paths_keep_their_identity_rules(world, monkeypatch, action):
    w = world
    ready(w)
    begin(w)
    if action == "abort":
        after_lock(monkeypatch, lambda: w.proc.rows[30].update(state="Z"))
        assert projects.abort("demo", TRANSFER, token_file=w.token)["result"] == "aborted"
    else:
        result = projects.commit("demo", TRANSFER, token_file=w.token, target=w.b["session_id"])
        before = projects.path("demo").read_bytes()
        after_lock(monkeypatch, lambda: w.proc.rows[40].update(state="Z"))
        assert projects.commit("demo", TRANSFER, token_file=w.token, target=w.b["session_id"]) == result
        assert projects.path("demo").read_bytes() == before
