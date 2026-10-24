"""M4 second review: only temporary stores, fake stations and local verifiers."""
import copy

import pytest

from test_contract import env  # noqa: F401
from test_m4 import finding, rig  # noqa: F401
from test_m4_review import config_change, one_round
from yardgoat import cli, pool, review
from yardgoat.store import TaskDir, read_json


def legacy_submit(rig, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(review, "prepare", lambda *a: None)
        rig.submit()


def forget_run_channel(td):
    with td.locked() as box:
        box["state"]["runs"][-1].pop("channel", None)


@pytest.mark.parametrize("phase", ["accepted", "running", "reviewing"])
def test_legacy_review_confirmation_preserves_identity_and_rounds(rig, monkeypatch, phase):
    legacy_submit(rig, monkeypatch)
    parent = TaskDir("parent")
    frozen = copy.deepcopy(read_json(parent.frozen_path))
    if phase != "accepted":
        # Reproduce old dispatch/advance without using the new channel requirements.
        with monkeypatch.context() as patch:
            patch.setattr(review, "channel_problem", lambda *a, **k: None)
            rig.dispatch()
            claim = cli.claim_card("work", "work-st-1")[1]
            if phase == "reviewing":
                patch.setattr(review, "advance", lambda *a, **k: 0)
                assert cli.finish_card("parent", claim["run_id"], "work-st-1")[0] == 0
                rig.call("reconcile")
        forget_run_channel(parent)
    before = parent.state()
    rig.submit("dependent", review=None, depends_on=["parent"])
    flags = ["--run-channel", "r1", "shell", "work"] if phase != "accepted" else []
    rig.call("confirm-review-channel", "parent", "--as", "owner", *flags)
    confirmed = parent.state()
    assert confirmed["status"] == before["status"]
    assert confirmed.get("review") == before.get("review")
    assert len(confirmed["runs"]) == len(before["runs"])
    assert confirmed["content_hash"] == frozen["content_hash"]
    assert read_json(parent.frozen_path)["spec"] == frozen["spec"]
    assert parent.spec()["review"]["channel"] == {"host": "shell", "lane": "audit"}
    assert TaskDir("dependent").spec()["depends_on"] == ["parent"]
    events = [e for e in parent.events() if e["kind"] == "task.review_channel_confirmed"]
    assert len(events) == 1 and events[0]["actor"] == "owner" and events[0]["at"]
    assert events[0]["channel"] == parent.spec()["review"]["channel"]
    rig.call("confirm-review-channel", "parent", "--as", "owner", *flags)
    assert len([e for e in parent.events() if e["kind"] == "task.review_channel_confirmed"]) == 1
    if phase == "accepted":
        rig.dispatch()
        rig.execute()
    elif phase == "running":
        assert cli.finish_card("parent", "r1", "work-st-1")[0] == 0
        rig.call("reconcile")
    else:
        rig.call("reconcile")
    rig.review(1, [])
    assert parent.state()["status"] == "verified"
    rig.dispatch("dependent")
    assert TaskDir("dependent").state()["status"] == "queued"


def test_confirmation_requires_owner_and_explicit_missing_run_identity(rig, monkeypatch):
    legacy_submit(rig, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(review, "channel_problem", lambda *a, **k: None)
        rig.dispatch()
        cli.claim_card("work", "work-st-1")
    td = TaskDir("parent")
    forget_run_channel(td)
    for who, flags in [("stranger", ["--run-channel", "r1", "shell", "work"]), ("owner", [])]:
        rig.call("confirm-review-channel", "parent", "--as", who, *flags, rc=3)
        assert "channel" not in td.spec()["review"]
        assert "channel" not in td.state()["runs"][0]
    rig.call("confirm-review-channel", "parent", "--as", "owner",
             "--run-channel", "r1", "shell", "audit", rc=3)
    assert "channel" not in td.spec()["review"]


def test_confirmation_freezes_current_config_once_and_resumes_parked_task(rig, monkeypatch, env):
    legacy_submit(rig, monkeypatch)
    rig.call("dispatch", "parent", "--as", "owner", rc=3)
    assert "channel" not in TaskDir("parent").spec()["review"]
    config_change(env, lambda c: c["pools"]["audit"].update(lane="audit-new"))
    rig.call("confirm-review-channel", "parent", "--as", "owner")
    assert TaskDir("parent").spec()["review"]["channel"]["lane"] == "audit-new"
    rig.call("answer", "parent", "--as", "owner", "--text", "Confirmed")
    rig.dispatch()
    config_change(env, lambda c: c["pools"]["audit"].update(lane="audit-later"))
    rig.call("confirm-review-channel", "parent", "--as", "owner")
    assert TaskDir("parent").spec()["review"]["channel"]["lane"] == "audit-new"
    assert cli.claim_card("work", "work-st-1")[1]["result"] == "EMPTY"


def test_legacy_confirmation_resumes_existing_review_child(rig, monkeypatch):
    legacy_submit(rig, monkeypatch)
    original = TaskDir.spec

    def legacy_spec(self):
        spec = original(self)
        if spec.get("review"):
            spec["review"]["channel"] = {"host": "shell", "lane": "audit"}
        return spec

    with monkeypatch.context() as patch:
        patch.setattr(TaskDir, "spec", legacy_spec)
        rig.dispatch()
        rig.execute()
    parent = TaskDir("parent")
    child = TaskDir("parent-review-1")
    frozen_child = child.frozen_path.read_bytes()
    forget_run_channel(parent)
    rig.call("reconcile")
    assert parent.state()["status"] == "parked"
    assert "channel" not in parent.spec()["review"]
    rig.call("confirm-review-channel", "parent", "--as", "owner", "--run-channel", "r1", "shell", "work")
    rig.call("answer", "parent", "--as", "owner", "--text", "Legacy identities confirmed")
    rig.call("reconcile")
    rig.review(1, [])
    assert parent.state()["status"] == "verified"
    assert len(parent.state()["review"]["rounds"]) == 1
    assert child.frozen_path.read_bytes() == frozen_child


def test_confirmation_replays_interruption_without_refreezing(rig, monkeypatch, env):
    legacy_submit(rig, monkeypatch)
    original = TaskDir.event

    class Crash(BaseException):
        pass

    def crash(self, kind, actor, **fields):
        if kind == "task.review_channel_confirmed":
            raise Crash()
        return original(self, kind, actor, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(TaskDir, "event", crash)
        with pytest.raises(Crash):
            rig.call("confirm-review-channel", "parent", "--as", "owner")
    confirmation = read_json(TaskDir("parent").frozen_path)["review_confirmation"]
    config_change(env, lambda c: c["pools"]["audit"].update(lane="changed-after-confirmation"))
    rig.call("confirm-review-channel", "parent", "--as", "owner")
    events = [e for e in TaskDir("parent").events() if e["kind"] == "task.review_channel_confirmed"]
    assert len(events) == 1
    assert events[0]["channel"] == confirmation["channel"]
    assert events[0]["at"] == confirmation["at"]


def test_confirmation_uses_declared_historical_channel_not_current_pool(rig, monkeypatch, env):
    legacy_submit(rig, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(review, "channel_problem", lambda *a, **k: None)
        rig.dispatch()
        cli.claim_card("work", "work-st-1")
    parent = TaskDir("parent")
    forget_run_channel(parent)
    config_change(env, lambda c: c["pools"]["work"].update(lane="different-current-lane"))
    rig.call("confirm-review-channel", "parent", "--as", "owner", "--run-channel", "r1", "shell", "work")
    assert parent.state()["runs"][0]["channel"]["lane"] == "work"


@pytest.mark.parametrize("name", ["work", "audit"])
@pytest.mark.parametrize("bad", ["missing_channel", "missing_root", "stale_root", "wrong_channel"])
def test_incompatible_station_leaves_cards_for_healthy_station(rig, tmp_path, name, bad):
    rig.submit()
    rig.dispatch()
    if name == "audit":
        rig.execute()
        tid = "parent-review-1"
    else:
        rig.submit("second")
        rig.dispatch("second")
        tid = "parent"
    healthy = pool.get_station(name, f"{name}-st-1")
    pool.update_station(name, f"{name}-st-2", **{**healthy, "station": f"{name}-st-2"})
    fields = {"missing_channel": {"channel": None}, "missing_root": {"root": None},
              "stale_root": {"root": str(tmp_path / "stale")},
              "wrong_channel": {"channel": {"host": "shell", "lane": "other"}}}[bad]
    pool.update_station(name, f"{name}-st-1", **fields)
    queued = [t for _, t in pool.queued_cards(name)]
    assert cli.claim_card(name, f"{name}-st-1")[1]["result"] in ("EMPTY", "REFUSED")
    for t in queued:
        assert TaskDir(t).state()["status"] == "queued"
        assert TaskDir(t).state()["runs"][-1]["state"] == "queued"
    assert pool.get_station(name, f"{name}-st-1").get("restart_required")
    rig.call("reconcile")
    assert cli.claim_card(name, f"{name}-st-2")[1]["task_id"] == tid
    if name == "work":
        assert TaskDir("second").state()["status"] == "queued"


def test_restart_required_station_does_not_hide_demand_for_new_station(rig, monkeypatch, env):
    rig.submit()
    rig.dispatch()
    pool.update_station("work", "work-st-1", channel=None)
    assert cli.claim_card("work", "work-st-1")[1]["result"] == "EMPTY"
    from yardgoat import doctor, runner
    config_change(env, lambda c: c["pools"]["work"].update(max_stations=2))
    monkeypatch.setattr(doctor, "ensure", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(pool, "start_station", lambda *a: "work-st-2")
    with pool.schedule_lock():
        assert pool.schedule(runner.config(), lambda: 0) == ["work-st-2"]
    assert pool.station_alive(pool.get_station("work", "work-st-1"))  # still charged to physical capacity


def test_frozen_task_problem_still_changes_task_state(rig, tmp_path):
    rig.submit()
    rig.dispatch()
    (tmp_path / "work/parent/verify.sh").write_text("exit 0\n")
    assert cli.claim_card("work", "work-st-1")[1]["result"] == "EMPTY"
    assert TaskDir("parent").state()["status"] == "parked"
    assert not pool.get_station("work", "work-st-1").get("restart_required")


def partial_followups(rig, monkeypatch, crash=False):
    rig.submit(review=one_round("pass_with_followups"))
    rig.dispatch()
    rig.execute()
    original = review.accept_generated

    class Crash(BaseException):
        pass

    def fail(raw, *args):
        if raw["task_id"] == "parent-followup-1-2":
            raise OSError("second followup failed")
        child = original(raw, *args)
        if crash and raw["task_id"] == "parent-followup-1-1":
            raise Crash()
        return child

    with monkeypatch.context() as patch:
        patch.setattr(review, "accept_generated", fail)
        if crash:
            with pytest.raises(Crash):
                rig.review(1, [finding(), finding(id="F2")])
        else:
            rig.review(1, [finding(), finding(id="F2")])
    assert TaskDir("parent-followup-1-1").exists
    assert not TaskDir("parent-followup-1-2").exists


@pytest.mark.parametrize("crash", [False, True])
def test_partial_followups_takeover_and_replay_keep_one_owner(rig, monkeypatch, crash):
    partial_followups(rig, monkeypatch, crash)
    rig.call("takeover", "parent", "--from", "owner", "--as", "next")
    first = TaskDir("parent-followup-1-1")
    assert first.state()["owner"] == "next"
    assert first.state()["owner_history"][-1]["from"] == "owner"
    assert first.state()["owner_history"][-1]["to"] == "next"
    if not crash:
        rig.call("answer", "parent", "--as", "next", "--text", "Retry")
    for _ in range(2):
        rig.call("reconcile")
    assert TaskDir("parent").state()["status"] == "verified"
    assert TaskDir("parent-followup-1-2").state()["owner"] == "next"
    assert len(first.state()["owner_history"]) == 1
    rig.call("dispatch", first.task_id, "--as", "next")


def test_pre_journal_partial_followups_recover_on_takeover(rig, monkeypatch):
    partial_followups(rig, monkeypatch)
    from yardgoat.store import home
    path = home() / "reviews/parent/1/followups.json"
    if path.exists():
        path.unlink()  # model a partial round written before the journal existed
    rig.call("takeover", "parent", "--from", "owner", "--as", "next")
    assert TaskDir("parent-followup-1-1").state()["owner"] == "next"
    rig.call("answer", "parent", "--as", "next", "--text", "Retry")
    rig.call("reconcile")
    assert TaskDir("parent-followup-1-2").state()["owner"] == "next"


def test_rebuilt_followup_journal_preserves_inherited_ownership_history(rig, monkeypatch):
    partial_followups(rig, monkeypatch)
    rig.call("takeover", "parent", "--from", "owner", "--as", "next")
    from yardgoat.store import home
    path = home() / "reviews/parent/1/followups.json"
    if path.exists():
        path.unlink()
    rig.call("takeover", "parent", "--from", "next", "--as", "third")
    state = TaskDir("parent-followup-1-1").state()
    assert state["owner"] == "third"
    assert [(x["from"], x["to"]) for x in state["owner_history"]] == [("owner", "next"), ("next", "third")]


def test_unreadable_followup_journal_parks_pending_result(rig, monkeypatch):
    partial_followups(rig, monkeypatch)
    rig.call("answer", "parent", "--as", "owner", "--text", "Retry")
    original = review.read_json

    def fail(path):
        if path.name == "followups.json":
            raise OSError("followup journal unavailable")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(review, "read_json", fail)
        rig.call("reconcile")
    state = TaskDir("parent").state()
    assert state["status"] == "parked"
    assert "followup journal unavailable" in state["question"]["text"]
    assert state["review"]["rounds"][0]["status"] == "pending"
    rig.call("answer", "parent", "--as", "owner", "--text", "Journal readable again")
    rig.call("reconcile")
    assert TaskDir("parent").state()["status"] == "verified"


@pytest.mark.parametrize("take_parent", [False, True])
def test_independent_followup_takeover_needs_coordination(rig, monkeypatch, take_parent):
    partial_followups(rig, monkeypatch)
    first = TaskDir("parent-followup-1-1")
    rig.call("takeover", first.task_id, "--from", "owner", "--as", "specialist")
    before = first.state()
    if take_parent:
        rig.call("takeover", "parent", "--from", "owner", "--as", "next")
    who = "next" if take_parent else "owner"
    rig.call("answer", "parent", "--as", who, "--text", "Retry")
    rig.call("reconcile")
    assert first.state()["owner"] == "specialist"
    assert first.state()["owner_history"] == before["owner_history"]
    st = TaskDir("parent").state()
    assert st["status"] == "parked"
    assert first.task_id in st["question"]["text"] and "specialist" in st["question"]["text"]
    assert any(x.get("task_id") == "parent" for x in rig.call("inbox", "--json"))
    # Explicitly align the independent owner; a normal answer can now replay.
    rig.call("takeover", first.task_id, "--from", "specialist", "--as", who)
    rig.call("answer", "parent", "--as", who, "--text", "Ownership coordinated")
    rig.call("reconcile")
    assert TaskDir("parent").state()["status"] == "verified"


@pytest.mark.parametrize("status", ["verified", "archived", "exhausted"])
def test_inbox_ignores_retired_pool_for_finished_tasks(rig, env, status):
    rig.submit(review=None)
    with TaskDir("parent").locked() as box:
        box["state"]["status"] = status
    config_change(env, lambda c: c["pools"].pop("work"))
    assert not any(x.get("status") == "pool_unconfigured" for x in rig.call("inbox", "--json"))
    assert "池子已不在配置中" in rig.call("status", "parent", "--json")["pool_problem"]


def test_inbox_still_reports_retired_pool_for_unfinished_task(rig, env):
    rig.submit(review=None)
    config_change(env, lambda c: c["pools"].pop("work"))
    assert any(x.get("status") == "pool_unconfigured" for x in rig.call("inbox", "--json"))
