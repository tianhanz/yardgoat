"""M1 contracts: launch-chain probes, card metadata and provider refusals. No model calls."""
import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401
from test_pool import pool_cfg, pool_task, stations, wait_all

sys.path.insert(0, SRC)


def configure(env, **cfg):
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))


def executable(path, body):
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def fake_host(tmp_path, env, launcher_body='exec "$@"', version_rc=0):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(bindir / "codex", f'test "$*" = --version || exit 99\necho fake-host-version\nexit {version_rc}')
    source = tmp_path / "env.sh"
    source.write_text(f"export PATH={shlex.quote(str(bindir))}:/usr/bin:/bin\n")
    launcher = executable(tmp_path / "launcher", launcher_body)
    configure(env, source=[str(source)], launcher=[launcher])
    return source


def checks(report):
    return {c["name"]: c for c in report["checks"]}


def test_doctor_shell_probe_is_persisted_and_does_not_run_a_card(tmp_path, env):
    pool_cfg(env, tmp_path, command=f"touch {tmp_path}/MODEL_WAS_CALLED")
    rc, out, err = yg(env, "doctor", "--pool", "p", "--json")
    assert rc == 0, err
    report = json.loads(out)
    assert report["ok"] and report["checked_at"]
    assert all(checks(report)[k]["ok"] for k in ("host_path", "host_version", "launcher", "mailbox"))
    saved = Path(env["YARDGOAT_HOME"]) / "doctor/pool/p.json"
    assert json.loads(saved.read_text()) == report
    assert not (tmp_path / "MODEL_WAS_CALLED").exists()
    assert not list((tmp_path / "root/.yardgoat-mailbox").rglob("*.probe"))
    assert stations(env) == []


def test_doctor_uses_sources_launcher_and_host_version_only(tmp_path, env):
    fake_host(tmp_path, env, 'test "$1" = lane-test && test "$2" = account-test || exit 17\nshift 2\nexec "$@"')
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg["launcher"] += ["{lane}", "{acct}"]
    configure(env, **cfg)
    rc, out, err = yg(env, "doctor", "--host", "codex", "--lane", "lane-test", "--acct", "account-test", "--json")
    assert rc == 0, err
    report = json.loads(out)
    assert report["ok"] and "fake-host-version" in checks(report)["host_version"]["reason"]
    assert (Path(env["YARDGOAT_HOME"]) / "doctor/direct/codex-lane-test.json").exists()


def test_doctor_reports_launcher_failure(tmp_path, env):
    fake_host(tmp_path, env, "echo 'unknown channel' >&2\nexit 17")
    rc, out, _ = yg(env, "doctor", "--host", "codex", "--lane", "bad", "--json")
    assert rc == 1 and not json.loads(out)["ok"]
    c = checks(json.loads(out))["launcher"]
    assert not c["ok"] and "unknown channel" in c["reason"]


def test_doctor_reports_missing_host(tmp_path, env):
    fake_host(tmp_path, env)
    (tmp_path / "bin/codex").unlink()
    rc, out, _ = yg(env, "doctor", "--host", "codex", "--json")
    assert rc == 1
    assert not checks(json.loads(out))["host_path"]["ok"]
    assert "codex" in checks(json.loads(out))["host_path"]["reason"]


def test_doctor_reports_host_version_failure(tmp_path, env):
    fake_host(tmp_path, env, version_rc=8)
    rc, out, _ = yg(env, "doctor", "--host", "codex", "--json")
    assert rc == 1
    c = checks(json.loads(out))
    assert c["host_path"]["ok"] and not c["host_version"]["ok"]
    assert c["launcher"]["ok"]  # probe collects failures without making the launcher fail


def test_schedule_failed_doctor_starts_no_station_and_exposes_reason(tmp_path, env):
    pool_cfg(env, tmp_path)
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg["source"] = [str(tmp_path / "missing-source.sh")]
    configure(env, **cfg)
    pool_task(tmp_path, env, "t-doctor")
    rc, out, err = yg(env, "dispatch", "t-doctor", "--as", "A")
    assert rc == 0, err
    assert json.loads(out)["stations_started"] == []
    assert stations(env) == []
    for command in (("status",), ("status", "t-doctor"), ("inbox",)):
        assert "池子 p 体检失败" in yg(env, *command)[1]
    assert "missing-source.sh" in yg(env, "inbox", "--json")[1]
    report = json.loads((Path(env["YARDGOAT_HOME"]) / "doctor/pool/p.json").read_text())
    assert not report["ok"]
    cfg.pop("source")
    configure(env, **cfg)
    yg(env, "reconcile")  # failures are rechecked without waiting for TTL
    assert wait_all(env, ["t-doctor"])[0]["runs"][-1]["state"] == "exited"
    assert "池子 p 体检失败" not in yg(env, "inbox")[1]


def test_schedule_reuses_fresh_doctor_then_refreshes_expired_record(tmp_path, env):
    pool_cfg(env, tmp_path, max_stations=0)
    assert yg(env, "doctor", "--pool", "p", "--json")[0] == 0
    saved = Path(env["YARDGOAT_HOME"]) / "doctor/pool/p.json"
    before = saved.read_text()
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg["pools"]["p"]["max_stations"] = 1
    configure(env, **cfg)
    pool_task(tmp_path, env, "t-fresh")
    yg(env, "dispatch", "t-fresh", "--as", "A")
    wait_all(env, ["t-fresh"])
    assert saved.read_text() == before
    cfg["doctor_ttl_h"] = 0
    cfg["source"] = [str(tmp_path / "expired-source.sh")]
    configure(env, **cfg)
    pool_task(tmp_path, env, "t-expired")
    yg(env, "dispatch", "t-expired", "--as", "A")
    yg(env, "reconcile")
    assert not json.loads(saved.read_text())["ok"]
    assert json.loads(yg(env, "status", "t-expired", "--json")[1])["runs"][-1]["state"] == "queued"


@pytest.mark.parametrize("field,value", [
    ("scope", "src"), ("scope", [1]), ("scope", None),
    ("scope", ["/etc/file"]), ("scope", ["../outside"]),
    ("risk", "l4"), ("risk", 2), ("risk", None), ("acceptance", ["pass"]),
])
def test_spec_rejects_invalid_card_metadata(tmp_path, env, field, value):
    sp = make_task(tmp_path)
    spec = json.loads(sp.read_text()); spec[field] = value
    from yardgoat.contract import SpecError, normalize
    with pytest.raises(SpecError, match=field):
        normalize(spec, sp.parent)


def test_spec_defaults_and_metadata_roundtrip(tmp_path, env):
    from yardgoat.contract import normalize
    sp = make_task(tmp_path)
    spec = json.loads(sp.read_text())
    assert normalize(spec, sp.parent)["risk"] == "l2"
    spec.update(scope=["src/**/*.py", "docs/"], risk="l3", acceptance="All contracts pass")
    sp.write_text(json.dumps(spec))
    assert yg(env, "submit", str(sp), "--as", "A")[0] == 0
    saved = json.loads((Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/spec.json").read_text())
    assert all(saved[k] == spec[k] for k in ("scope", "risk", "acceptance"))
    schema = json.loads((Path(SRC) / "yardgoat/schemas/task.schema.json").read_text())
    assert {"scope", "risk", "acceptance"} <= schema["properties"].keys()


def test_quick_records_metadata_in_spec_and_card(tmp_path, env):
    rc, _, err = yg(env, "quick", "Update docs", "--verify", "true", "--as", "A", "--id", "q-m1",
                    "--scope", "docs/*.md", "--scope", "README.md", "--risk", "l1",
                    "--acceptance", "Links resolve", "--no-dispatch")
    assert rc == 0, err
    card_dir = Path(env["YARDGOAT_HOME"]) / "cards/q-m1"
    spec = json.loads((card_dir / "spec.json").read_text())
    assert spec["scope"] == ["docs/*.md", "README.md"] and spec["risk"] == "l1"
    assert spec["acceptance"] == "Links resolve"
    text = (card_dir / "CARD.md").read_text()
    assert all(v in text for v in ("docs/*.md", "README.md", "l1", "Links resolve"))


@pytest.mark.parametrize("line", ["FLAGGED FOR POSSIBLE policy issue", "Usage limit reached",
                                  "You've reached your quota", "rate limit exceeded"])
def test_direct_provider_refusal_does_not_use_attempts(tmp_path, env, line):
    event = json.dumps({"type": "error", "error": {"message": line}})
    sp = make_task(tmp_path, worker=f"echo {shlex.quote(event)}; exit 1", attempts=1)
    assert yg(env, "submit", str(sp), "--as", "A")[0] == 0
    for _ in range(2):
        assert yg(env, "dispatch", "t-demo", "--as", "A")[0] == 0
        st = wait_exit(env, "t-demo")
        assert st["status"] == st["runs"][-1]["state"] == "provider_refused"
        assert st["runs"][-1]["receipt"]["reason"] == line
        assert "换措辞或换通道后重发" in yg(env, "inbox")[1]
        assert line in json.loads(yg(env, "inbox", "--json")[1])[0]["question"]
        assert json.loads(yg(env, "reconcile")[1])["actions"] == []
    kinds = [json.loads(l)["kind"] for l in yg(env, "events", "t-demo")[1].splitlines()]
    assert kinds.count("run.provider_refused") == 2
    for filename, field, value in (("receipt", "status", "provider_refused"), ("event", "kind", "run.provider_refused")):
        schema = json.loads((Path(SRC) / f"yardgoat/schemas/{filename}.schema.json").read_text())
        assert value in schema["properties"][field]["enum"]


def test_custom_provider_refusal_pattern(tmp_path, env):
    configure(env, provider_refusal_patterns=[r"custom denial \d+"])
    event = json.dumps({"type": "error", "error": {"message": "Custom Denial 42"}})
    sp = make_task(tmp_path, worker=f"echo {shlex.quote(event)}; exit 2", attempts=1)
    yg(env, "submit", str(sp), "--as", "A"); yg(env, "dispatch", "t-demo", "--as", "A")
    assert wait_exit(env, "t-demo")["runs"][-1]["state"] == "provider_refused"


def test_pool_provider_refusal_does_not_use_attempts(tmp_path, env):
    event = json.dumps({"type": "error", "error": {"message": "Rate limit exceeded by provider"}})
    pool_cfg(env, tmp_path, command='python3 -m yardgoat.cli claim --pool p --station "$0" >/dev/null; '
             + f"echo {shlex.quote(event)}; exit 1")
    pool_task(tmp_path, env, "t-refusal", budget={"max_attempts": 1, "max_wall_min": 1})
    for _ in range(2):
        assert yg(env, "dispatch", "t-refusal", "--as", "A")[0] == 0
        st = wait_all(env, ["t-refusal"])[0]
        assert st["status"] == st["runs"][-1]["state"] == "provider_refused"
        assert st["runs"][-1]["receipt"]["reason"] == "Rate limit exceeded by provider"
        assert "换措辞或换通道后重发" in yg(env, "inbox")[1]


def test_doctor_uses_tmux_daemon_environment_and_pass_env(tmp_path, env):
    fake_host(tmp_path, env, 'test "$CHAIN_TOKEN" = allowed || exit 18\nexec "$@"')
    tmux = tmp_path / "bin/tmux"
    tmux.write_text(f'''#!{sys.executable}
import os, subprocess, sys
assert sys.argv[1] == "new-window"
assert "_doctor" in sys.argv[-1]
open({str(tmp_path / "tmux-called")!r}, "w").write(sys.argv[-1])
env = dict(os.environ, PATH="/usr/bin:/bin")
env.pop("CHAIN_TOKEN", None)
p = subprocess.Popen(["/bin/bash", "-c", sys.argv[-1]], env=env,
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
print(p.pid)
''')
    tmux.chmod(0o755)
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg.update(tmux_session="test-server", pass_env=["CHAIN_TOKEN"])
    configure(env, **cfg)
    env.pop("YARDGOAT_NO_TMUX", None)
    env.update(PATH=str(tmp_path / "bin") + os.pathsep + env["PATH"], CHAIN_TOKEN="allowed")
    rc, out, err = yg(env, "doctor", "--host", "codex", "--json")
    assert rc == 0, err
    assert json.loads(out)["ok"] and (tmp_path / "tmux-called").exists()


def test_doctor_pool_checks_actual_lane_and_next_account(tmp_path, env):
    fake_host(tmp_path, env, 'test "$1" = pool-lane && test "$2" = second || exit 19\nshift 2\nexec "$@"')
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg["launcher"] += ["{lane}", "{acct}"]
    cfg["pools"] = {"p": {"host": "codex", "lane": "pool-lane", "accts": ["first", "second"], "root": str(tmp_path / "root")}}
    configure(env, **cfg)
    sd = Path(env["YARDGOAT_HOME"]) / "pools/p/stations"
    sd.mkdir(parents=True)
    (sd / "p-st-1.json").write_text(json.dumps({"state": "exited"}))
    rc, out, err = yg(env, "doctor", "--pool", "p", "--json")
    assert rc == 0, err
    report = json.loads(out)
    assert report["account_alias"] == "second" and checks(report)["mailbox"]["ok"]


def test_doctor_reports_mailbox_failure_even_without_tasks(tmp_path, env):
    pool_cfg(env, tmp_path)
    (tmp_path / "root/.yardgoat-mailbox").write_text("not a directory")
    rc, out, _ = yg(env, "doctor", "--pool", "p", "--json")
    assert rc == 1 and not checks(json.loads(out))["mailbox"]["ok"]
    assert "池子 p 体检失败" in yg(env, "status")[1]
    assert json.loads(yg(env, "status", "--json")[1])[0]["status"] == "doctor_failed"


@pytest.mark.parametrize("action", ["reconcile", "dispatch"])
def test_provider_refusal_survives_a_dead_wrapper(tmp_path, env, action):
    pool_cfg(env, tmp_path, max_stations=0)
    pool_task(tmp_path, env, "t-crash", budget={"max_attempts": 1, "max_wall_min": 1})
    assert yg(env, "dispatch", "t-crash", "--as", "A")[0] == 0
    root = Path(env["YARDGOAT_HOME"])
    statefile = root / "tasks/t-crash/task.json"
    st = json.loads(statefile.read_text())
    st["status"] = "running"
    st["runs"][0].update(state="running", station="p-st-1", log_start=0)
    statefile.write_text(json.dumps(st))
    sd = root / "pools/p/stations"
    (sd / "p-st-1.json").write_text(json.dumps({"state": "gone", "station": "p-st-1"}))
    (sd / "p-st-1.log").write_text(json.dumps({"type": "error", "error": {"message": "Usage limit reached"}}) + "\n")
    args = ("reconcile",) if action == "reconcile" else ("dispatch", "t-crash", "--as", "A")
    assert yg(env, *args)[0] == 0
    st = json.loads(yg(env, "status", "t-crash", "--json")[1])
    assert st["runs"][0]["state"] == "provider_refused"
    assert st["runs"][0]["receipt"]["reason"] == "Usage limit reached"
    if action == "dispatch":
        assert st["runs"][-1]["run_id"] == "r2" and st["runs"][-1]["state"] == "queued"
