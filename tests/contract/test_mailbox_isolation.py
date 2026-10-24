"""A worker may run nested yardgoat commands (its own test suite, a demo) against a scratch store.
Those must not reach the real station wrapper: a stray `claim` there would hand a real card to a test."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_contract import SRC, env  # noqa: F401


def run_cli(env, *args, timeout=15):
    return subprocess.run([sys.executable, "-m", "yardgoat.cli", *args], env=env,
                          capture_output=True, text=True, timeout=timeout)


def test_nested_command_with_another_store_bypasses_the_station_mailbox(env, tmp_path):
    mb = tmp_path / "real-mailbox"
    mb.mkdir()
    e = dict(env, YARDGOAT_MAILBOX=str(mb), YARDGOAT_MAILBOX_HOME=str(tmp_path / "real-home"), YARDGOAT_STATION="s-1")
    try:
        p = run_cli(e, "claim", "--pool", "nowhere", "--station", "s-1")
    except subprocess.TimeoutExpired:
        pytest.fail("claim waited on the real station mailbox instead of using its own store")
    assert not list(mb.glob("req-*.json")), "request leaked into the real station mailbox"
    assert p.returncode != 2 or "did not answer" not in p.stdout


def test_command_for_the_served_store_still_uses_the_mailbox(env, tmp_path):
    mb = tmp_path / "mb"
    mb.mkdir()
    e = dict(env, YARDGOAT_MAILBOX=str(mb), YARDGOAT_MAILBOX_HOME=env["YARDGOAT_HOME"], YARDGOAT_STATION="s-1")
    p = subprocess.Popen([sys.executable, "-m", "yardgoat.cli", "claim", "--pool", "p", "--station", "s-1"],
                         env=e, stdout=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            reqs = list(mb.glob("req-*.json"))
            if reqs:
                break
            time.sleep(0.1)
        assert reqs, "no mailbox request from inside the station"
        req = json.loads(reqs[0].read_text())
        assert Path(req["home"]).resolve() == Path(env["YARDGOAT_HOME"]).resolve()
        (mb / reqs[0].name.replace("req-", "resp-")).write_text(json.dumps({"rc": 0, "out": {"result": "EMPTY"}}))
        out, _ = p.communicate(timeout=15)
        assert p.returncode == 0 and json.loads(out)["result"] == "EMPTY"
    finally:
        p.kill()


@pytest.mark.parametrize("op", [{"op": "claim"}, {"op": "finish", "task_id": "t", "run_id": "r1"}])
def test_station_wrapper_refuses_requests_for_another_store(env, tmp_path, monkeypatch, op):
    monkeypatch.setenv("YARDGOAT_HOME", env["YARDGOAT_HOME"])
    sys.path.insert(0, SRC)
    from yardgoat import pool
    mb = tmp_path / "mb"
    mb.mkdir()
    (mb / "req-x.json").write_text(json.dumps({**op, "home": str(tmp_path / "scratch-store"), "expires": time.time() + 60}))
    calls = []

    class FakeCli:
        def claim_card(self, *a, **k):
            calls.append(("claim", a)); return 0, {"result": "CLAIMED"}

        def finish_card(self, *a, **k):
            calls.append(("finish", a)); return 0, {"result": "exited"}

    pool.serve_mailbox("p", "s-1", mb, FakeCli())
    resp = json.loads((mb / "resp-x.json").read_text())
    assert calls == [] and resp["rc"] == 3 and "task store" in resp["out"]["reason"]
