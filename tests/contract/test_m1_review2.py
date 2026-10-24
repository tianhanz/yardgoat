"""Second M1 review regressions: isolated stores and synthetic logs, no model calls."""
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_contract import env, yg  # noqa: F401
from test_m1_review import ERROR, SUCCESS, bind
from test_pool import pool_cfg, pool_task


def claimed(tmp_path, env, monkeypatch, old_log=""):
    from yardgoat import cli, pool, runner
    pool_cfg(env, tmp_path, max_stations=0, acct="limited")
    pool_task(tmp_path, env, "t-one", budget={"max_attempts": 1, "max_wall_min": 1})
    assert yg(env, "dispatch", "t-one", "--as", "A")[0] == 0
    bind(monkeypatch, env)
    pool.update_station("p", "p-st-1", station="p-st-1", state="running", tasks=[],
                        pid=os.getpid(), acct="limited")
    log = pool.station_log("p", "p-st-1")
    log.write_text(old_log)
    assert cli.claim_card("p", "p-st-1")[1]["task_id"] == "t-one"
    return log, runner.config()


def receipt(env):
    return json.loads((Path(env["YARDGOAT_HOME"]) / "tasks/t-one/runs/r1/receipt.json").read_text())


def rewrite_log(log, mode):
    text = ERROR + "\n"
    if mode == "replace_large":
        text += "padding\n" * 1000
    if mode.startswith("replace"):
        log.rename(log.with_suffix(".old"))
    log.write_text(text)


@pytest.mark.parametrize("mode", ["truncate", "replace_small", "replace_large"])
@pytest.mark.parametrize("path", ["finish", "recovery", "idle"])
def test_F1_log_boundary_recovers_after_truncation_or_replacement(tmp_path, env, monkeypatch, mode, path):
    from yardgoat import cli, pool
    log, cfg = claimed(tmp_path, env, monkeypatch, "previous card\n" * 100)
    if path == "idle":
        assert cli.finish_card("t-one", "r1", "p-st-1")[0] == 0
    rewrite_log(log, mode)
    if path == "finish":
        assert cli.finish_card("t-one", "r1", "p-st-1")[1]["result"] == "provider_refused"
    elif path == "recovery":
        pool.update_station("p", "p-st-1", state="gone", pid=None)
        assert yg(env, "reconcile")[0] == 0
        assert receipt(env)["status"] == "provider_refused"
    else:
        pool.update_station("p", "p-st-1", pid=None)
        assert pool.live_stations("p") == []
        assert pool.provider_backoff("p", cfg)["reason"] == "Usage limit reached"


def test_F1_unchanged_log_does_not_replay_previous_card_refusal(tmp_path, env, monkeypatch):
    from yardgoat import cli, pool
    log, cfg = claimed(tmp_path, env, monkeypatch, ERROR + "\n")
    assert pool.unfinished_outcome("p", "p-st-1", cfg)["state"] == "lost"
    assert cli.finish_card("t-one", "r1", "p-st-1")[1]["result"] == "exited"
    assert pool.idle_outcome("p", pool.get_station("p", "p-st-1"), cfg)["state"] == "lost"


@pytest.mark.parametrize("when", ["before_finish", "before_commit", "recovery_first", "recovery_before_commit"])
def test_F2_finish_preserves_persisted_timeout(tmp_path, env, monkeypatch, when):
    from yardgoat import cli, pool, runner
    log, cfg = claimed(tmp_path, env, monkeypatch)
    log.write_text(ERROR + "\n")
    cur = pool.get_station("p", "p-st-1")["current"]

    def timeout():
        pool.record_execution("p", "p-st-1", cur, exit_code=124, timed_out=True)

    if when in ("before_commit", "recovery_before_commit"):
        original = runner.record_outcome

        def race(*args, **kwargs):
            timeout()  # timeout wins after finish's initial snapshot, before its commit lock
            return original(*args, **kwargs)

        monkeypatch.setattr(runner, "record_outcome", race)
    else:
        timeout()
    if when in ("recovery_first", "recovery_before_commit"):
        pool.update_station("p", "p-st-1", state="gone", pid=None)
        assert cli.cmd_reconcile(SimpleNamespace()) == 0
    rc, _ = cli.finish_card("t-one", "r1", "p-st-1")
    assert rc == (3 if when in ("recovery_first", "recovery_before_commit") else 0)
    rec = receipt(env)
    assert (rec["status"], rec["exit_code"], rec["timed_out"]) == ("exited", 124, True)
    assert yg(env, "close", "t-one", "--as", "A")[0] == 1
    assert yg(env, "dispatch", "t-one", "--as", "A")[0] == 3  # the timeout spent its attempt


@pytest.mark.parametrize("path", ["finish", "reconcile"])
def test_F3_station_timeout_survives_crash_before_task_write(tmp_path, env, monkeypatch, path):
    from yardgoat import cli, pool
    from yardgoat.store import TaskDir
    log, cfg = claimed(tmp_path, env, monkeypatch)
    log.write_text(ERROR + "\n")
    cur = pool.get_station("p", "p-st-1")["current"]

    @contextmanager
    def crash(self):
        raise RuntimeError("crash between station and task writes")
        yield

    with monkeypatch.context() as patch:
        patch.setattr(TaskDir, "locked", crash)
        with pytest.raises(RuntimeError, match="crash between"):
            pool.record_execution("p", "p-st-1", cur, exit_code=124, timed_out=True)
    assert pool.get_station("p", "p-st-1")["execution"]["timed_out"] is True
    assert not TaskDir("t-one").state()["runs"][0].get("execution")
    if path == "finish":
        assert cli.finish_card("t-one", "r1", "p-st-1")[0] == 0
    else:
        pool.update_station("p", "p-st-1", state="gone", pid=None)
        assert yg(env, "reconcile")[0] == 0
    rec = receipt(env)
    assert (rec["status"], rec["exit_code"], rec["timed_out"]) == ("exited", 124, True)


@pytest.mark.parametrize("reference", [None, {"task_id": "t-other", "run_id": "r1"},
                                     {"task_id": "t-one", "run_id": "r0"}])
def test_F3_unbound_or_other_run_timeout_is_not_inherited(tmp_path, env, monkeypatch, reference):
    from yardgoat import cli, pool
    log, cfg = claimed(tmp_path, env, monkeypatch)
    log.write_text(ERROR + "\n")
    pool.update_station("p", "p-st-1", execution={"exit_code": 124, "timed_out": True}, execution_ref=reference)
    assert pool.unfinished_outcome("p", "p-st-1", cfg)["state"] == "provider_refused"
    assert cli.finish_card("t-one", "r1", "p-st-1")[1]["result"] == "provider_refused"
    assert receipt(env)["timed_out"] is False


@pytest.mark.parametrize("path", ["finish", "recovery", "idle"])
def test_F4_generic_failure_keeps_prior_provider_refusal(tmp_path, env, monkeypatch, path):
    from yardgoat import cli, pool
    log, cfg = claimed(tmp_path, env, monkeypatch)
    if path == "idle":
        assert cli.finish_card("t-one", "r1", "p-st-1")[0] == 0
    log.write_text(ERROR + '\n' + json.dumps({"type": "turn.failed", "error": {"message": "Request failed"}}) + '\n')
    if path == "finish":
        assert cli.finish_card("t-one", "r1", "p-st-1")[1]["result"] == "provider_refused"
    elif path == "recovery":
        assert pool.unfinished_outcome("p", "p-st-1", cfg)["state"] == "provider_refused"
    else:
        assert pool.idle_outcome("p", pool.get_station("p", "p-st-1"), cfg)["state"] == "provider_refused"


@pytest.mark.parametrize("success", [SUCCESS, json.dumps({"type": "turn.completed"})])
def test_F4_only_success_clears_prior_provider_refusal(tmp_path, success):
    from yardgoat import runner
    log = tmp_path / "log"
    log.write_text(ERROR + '\n' + success + '\n' + json.dumps({"type": "error", "message": "Request failed"}) + '\n')
    assert runner.classify_outcome(log, {}, exit_code=1, default="exited")["state"] == "exited"


@pytest.mark.parametrize("command", [("status", "t-one"), ("status",), ("inbox",)])
@pytest.mark.parametrize("as_json", [False, True])
def test_F5_removed_pool_keeps_historical_status_readable(tmp_path, env, monkeypatch, command, as_json):
    from yardgoat import cli
    claimed(tmp_path, env, monkeypatch)
    assert cli.finish_card("t-one", "r1", "p-st-1")[0] == 0
    before = receipt(env)
    Path(env["YARDGOAT_CONFIG"]).write_text("{}")
    rc, out, err = yg(env, *command, *(["--json"] if as_json else []))
    assert rc == 0, err
    assert "池子已不在配置中" in out
    if as_json and command[0] == "status":
        data = json.loads(out)
        item = data if isinstance(data, dict) else next(i for i in data if i.get("task_id") == "t-one")
        assert item["pool_backoff"] is None
        assert item["runs"][0]["receipt"] == before


@pytest.mark.parametrize("command", ["status", "inbox"])
@pytest.mark.parametrize("with_task", [False, True])
def test_F6_text_backoff_identifies_pool_account_and_deadline(tmp_path, env, monkeypatch, command, with_task):
    from yardgoat import pool, runner
    pool_cfg(env, tmp_path, max_stations=0, acct="limited")
    if with_task:
        pool_task(tmp_path, env, "t-one")
    bind(monkeypatch, env)
    pool.pause_provider("p", {"station": "p-st-1", "acct": "limited"}, runner.config(), "Usage limit reached")
    deadline = pool.provider_backoff("p", runner.config())["until"]
    rc, out, err = yg(env, command)
    assert rc == 0, err
    assert any(all(s in line for s in ("pool=p", "provider_refused", "account=limited", f"until={deadline}",
                                       "Usage limit reached")) for line in out.splitlines())
