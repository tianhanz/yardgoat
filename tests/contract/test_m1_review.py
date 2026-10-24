"""M1 review regressions. Isolated stores, local shell workers, no model requests."""
import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401
from test_pool import pool_cfg, pool_task, stations

sys.path.insert(0, SRC)

ERROR = json.dumps({"type": "error", "error": {"code": "rate_limit_exceeded", "message": "Usage limit reached"}})
SUCCESS = json.dumps({"type": "result", "is_error": False, "subtype": "success"})


def bind(monkeypatch, env):
    for key in ("YARDGOAT_HOME", "YARDGOAT_CONFIG"):
        monkeypatch.setenv(key, env[key])


def emit(*lines):
    return "printf '%s\\n' " + " ".join(shlex.quote(line) for line in lines)


def run_direct(tmp_path, env, command, wall=1):
    sp = make_task(tmp_path, worker=command, attempts=1)
    data = json.loads(sp.read_text())
    data["budget"]["max_wall_min"] = wall
    sp.write_text(json.dumps(data))
    assert yg(env, "submit", str(sp), "--as", "A")[0] == 0
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 0
    return wait_exit(env, "t-demo")


@pytest.mark.parametrize("lines", [
    ["Added a test for rate limit exceeded handling"],
    ["Usage limit reached"],  # no provenance: do not infer a provider error from plain text
    [json.dumps({"type": "assistant", "message": {"content": "Usage limit reached"}})],
    [json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": ERROR}})],
    [ERROR, SUCCESS],
])
def test_F1_successful_content_and_recovered_errors_are_not_refusals(tmp_path, env, lines):
    st = run_direct(tmp_path, env, emit(*lines) + "; echo done > out/result.txt")
    assert st["runs"][0]["state"] == "exited"
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 0


def test_F1_explicit_provider_code_can_refuse_even_with_exit_zero(tmp_path, env):
    event = json.dumps({"type": "turn.failed", "error": {"code": "insufficient_quota", "message": "Capacity unavailable"}})
    st = run_direct(tmp_path, env, emit(event))
    assert st["runs"][0]["state"] == "provider_refused"
    assert st["runs"][0]["receipt"]["timed_out"] is False


def test_F2_claim_fences_shared_log_for_next_card(tmp_path, env, monkeypatch):
    from yardgoat import cli, pool
    pool_cfg(env, tmp_path, max_stations=0)
    for tid in ("t-first", "t-second"):
        pool_task(tmp_path, env, tid)
        assert yg(env, "dispatch", tid, "--as", "A")[0] == 0
    bind(monkeypatch, env)
    pool.update_station("p", "p-st-1", station="p-st-1", state="running", tasks=[], pid=os.getpid())
    log = pool.pool_dir("p") / "stations/p-st-1.log"
    log.write_text("")
    assert cli.claim_card("p", "p-st-1")[1]["task_id"] == "t-first"
    log.write_text(ERROR + "\n" + SUCCESS + "\n")
    assert cli.finish_card("t-first", "r1", "p-st-1")[0] == 0
    assert cli.claim_card("p", "p-st-1")[1]["task_id"] == "t-second"
    assert pool.unfinished_outcome("p", "p-st-1", json.loads(Path(env["YARDGOAT_CONFIG"]).read_text()))["state"] == "lost"
    # Normal finish uses the same boundary/classifier, including a genuine current-card refusal.
    with log.open("a") as stream:
        stream.write(ERROR + "\n")
    assert cli.finish_card("t-second", "r1", "p-st-1")[1]["result"] == "provider_refused"


def test_F3_real_timeout_beats_provider_refusal(tmp_path, env):
    st = run_direct(tmp_path, env, emit(ERROR) + "; sleep 10", wall=0.002)
    rec = st["runs"][0]["receipt"]
    assert rec["status"] == "exited" and rec["timed_out"] is True
    assert yg(env, "close", "t-demo", "--as", "A")[0] == 1
    assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 3


def test_F3_exit_124_without_timeout_handler_is_not_a_timeout(tmp_path, env):
    st = run_direct(tmp_path, env, "exit 124")
    assert st["runs"][0]["receipt"]["timed_out"] is False


def test_F4_preclaim_refusal_pauses_pool_and_account_without_fake_receipt(tmp_path, env, monkeypatch):
    pool_cfg(env, tmp_path, command=emit(ERROR) + "; exit 1", acct="limited")
    pool_task(tmp_path, env, "t-wait")
    assert yg(env, "dispatch", "t-wait", "--as", "A")[0] == 0
    deadline = time.time() + 10
    while time.time() < deadline and not all(s.get("state") != "running" for s in stations(env)):
        time.sleep(0.1)
    for _ in range(3):
        assert yg(env, "reconcile")[0] == 0
        time.sleep(0.3)
    station_count = len(stations(env))
    assert station_count == 1
    items = json.loads(yg(env, "inbox", "--json")[1])
    assert any(i.get("pool") == "p" and i["status"] == "provider_refused" and i["account_alias"] == "limited" for i in items)
    st = json.loads(yg(env, "status", "t-wait", "--json")[1])
    assert st["runs"][0]["state"] == "queued" and st["runs"][0]["receipt"] is None
    assert st["pool_backoff"]["account_alias"] == "limited"
    from yardgoat import pool
    bind(monkeypatch, env)
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg["pools"]["shared"] = dict(cfg["pools"]["p"])
    assert pool.provider_backoff("shared", cfg, "limited")["account_alias"] == "limited"
    # Expiry of both pool and account entries releases scheduling without another doctor mutation.
    root = Path(env["YARDGOAT_HOME"])
    for p in [root / "pools/p/provider-backoff.json", *root.glob("provider-backoffs/*.json")]:
        record = json.loads(p.read_text())
        record["until"] = 0
        p.write_text(json.dumps(record))
    assert pool.provider_backoff("shared", cfg, "limited") is None
    cfg["pools"].pop("shared")
    cfg["pools"]["p"]["command"] = "true"
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    result = json.loads(yg(env, "reconcile")[1])
    assert any(a.get("station_started") == "p-st-2" for a in result["actions"])
    deadline = time.time() + 5
    while time.time() < deadline and any(s.get("state") == "running" for s in stations(env)):
        time.sleep(0.1)


def test_F4_dead_preclaim_wrapper_records_backoff_on_reconcile(tmp_path, env):
    pool_cfg(env, tmp_path, command="true", max_stations=0)
    pool_task(tmp_path, env, "t-wait")
    assert yg(env, "dispatch", "t-wait", "--as", "A")[0] == 0
    root = Path(env["YARDGOAT_HOME"])
    sd = root / "pools/p/stations"
    (sd / "p-st-1.json").write_text(json.dumps({"station": "p-st-1", "state": "running", "tasks": [], "acct": ""}))
    (sd / "p-st-1.log").write_text(ERROR + "\n")
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg["pools"]["p"]["max_stations"] = 1
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    result = json.loads(yg(env, "reconcile")[1])
    assert not any(a.get("station_started") for a in result["actions"])
    assert json.loads(yg(env, "inbox", "--json")[1])[0]["status"] == "provider_refused"


def test_F5_direct_doctor_cannot_replace_failed_pool_health(tmp_path, env, monkeypatch):
    from yardgoat import doctor
    pool_cfg(env, tmp_path, max_stations=0)
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg["pools"]["shell-"] = cfg["pools"].pop("p")
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    (tmp_path / "root/.yardgoat-mailbox").write_text("not a directory")
    assert yg(env, "doctor", "--pool", "shell-", "--json")[0] == 1
    assert yg(env, "doctor", "--host", "shell", "--json")[0] == 0
    bind(monkeypatch, env)
    assert doctor.ensure("shell-", cfg, "")["ok"] is False


@pytest.mark.parametrize("field,value", [("host", "codex"), ("lane", "wrong-lane"), ("pool", "another-pool"), ("checks", [])])
def test_F5_cache_identity_and_required_checks_are_verified(tmp_path, env, monkeypatch, field, value):
    from yardgoat import doctor
    pool_cfg(env, tmp_path)
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    bind(monkeypatch, env)
    report = doctor.run(cfg, name="p", acct="")
    good = report.copy()
    report[field] = value
    doctor.record_path("p").write_text(json.dumps(report))
    refreshed = doctor.ensure("p", cfg, "")
    assert refreshed[field] == good[field]


def test_F6_dead_direct_runner_keeps_launch_failure_priority(tmp_path, env, monkeypatch):
    from yardgoat import adapters
    from yardgoat.store import TaskDir
    sp = make_task(tmp_path)
    assert yg(env, "submit", str(sp), "--as", "A")[0] == 0
    bind(monkeypatch, env)
    td = TaskDir("t-demo")
    rd = td.run_dir("r1")
    rd.mkdir(parents=True)
    (rd / "log").write_text(ERROR + "\nyardgoat-launch-failed: source missing.sh\n")
    out = adapters.Direct().poll(td, {"run_id": "r1"}, td.spec(), {})
    assert out["state"] == "launch_failed"


def test_F6_direct_recovery_uses_persisted_timeout_evidence(tmp_path, env, monkeypatch):
    from yardgoat import adapters
    from yardgoat.store import TaskDir
    sp = make_task(tmp_path)
    assert yg(env, "submit", str(sp), "--as", "A")[0] == 0
    bind(monkeypatch, env)
    td = TaskDir("t-demo")
    rd = td.run_dir("r1")
    rd.mkdir(parents=True)
    (rd / "log").write_text(ERROR + "\n")
    (rd / "execution.json").write_text(json.dumps({"exit_code": 124, "timed_out": True, "elapsed": 1}))
    out = adapters.Direct().poll(td, {"run_id": "r1"}, td.spec(), {})
    assert out["state"] == "exited" and out["timed_out"] is True


@pytest.mark.parametrize("record", [[1], {"ok": True, "account_alias": "", "checked_epoch": time.time()}])
def test_F7_malformed_cache_is_rechecked_and_safe_to_display(tmp_path, env, monkeypatch, record):
    from yardgoat import doctor
    pool_cfg(env, tmp_path)
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    bind(monkeypatch, env)
    path = doctor.record_path("p")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    report = doctor.ensure("p", cfg, "")
    assert isinstance(report, dict) and report.get("pool") == "p" and report["ok"] is True
    assert doctor.failure_message("p", record) is None


@pytest.mark.parametrize("field,value", [("scope", []), ("risk", "l2"), ("acceptance", "pass")])
def test_F8_legacy_resubmit_is_idempotent_but_explicit_metadata_changes_are_not(tmp_path, env, field, value):
    from yardgoat import contract
    sp = make_task(tmp_path)
    assert yg(env, "submit", str(sp), "--as", "A")[0] == 0
    root = Path(env["YARDGOAT_HOME"])
    authpath = root / "frozen/t-demo.json"
    auth = json.loads(authpath.read_text())
    # Exact pre-M1 normalized shape/hash, without changing the original input bytes.
    auth["spec"].pop("scope", None)
    auth["spec"].pop("risk", None)
    auth["content_hash"] = contract.content_hash(auth["spec"], auth["frozen"])
    authpath.write_text(json.dumps(auth))
    (root / "tasks/t-demo/spec.json").write_text(json.dumps(auth["spec"]))
    statepath = root / "tasks/t-demo/task.json"
    state = json.loads(statepath.read_text())
    state["content_hash"] = auth["content_hash"]
    statepath.write_text(json.dumps(state))
    before = authpath.read_bytes()
    rc, out, err = yg(env, "submit", str(sp), "--as", "A")
    assert rc == 0, err
    assert json.loads(out)["result"] == "already-accepted" and authpath.read_bytes() == before
    raw = json.loads(sp.read_text())
    raw[field] = value
    sp.write_text(json.dumps(raw))
    assert yg(env, "submit", str(sp), "--as", "A")[0] == 3
