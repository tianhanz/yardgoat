"""M8a contracts: isolated stores and fake local processes, never a model or tmux."""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")
sys.path.insert(0, SRC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"sessions": {"agents": []}}))
    for key in ("YARDGOAT_STATION", "YARDGOAT_MAILBOX", "YARDGOAT_MAILBOX_HOME", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YARDGOAT_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("YARDGOAT_CONFIG", str(cfg))
    monkeypatch.setenv("YARDGOAT_NO_TMUX", "1")
    monkeypatch.setenv("PYTHONPATH", SRC)
    # Keep fixture failure reprs free of host credentials and unrelated agent settings.
    return {k: v for k, v in os.environ.items()
            if k in ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "YARDGOAT_HOME", "YARDGOAT_CONFIG", "YARDGOAT_NO_TMUX")}


def call(env, *args, rc=0):
    p = subprocess.run([sys.executable, "-m", "yardgoat.cli", *map(str, args)], env=env,
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == rc, (p.returncode, p.stdout, p.stderr)
    return json.loads(p.stdout) if p.stdout.strip() else p.stderr


def test_unknown_whoami_is_policy_refusal(env):
    result = call(env, "whoami", "--json", rc=3)
    assert result["session_id"] is None and result["reason"]


def test_project_initial_epoch_and_immutable_id(env):
    call(env, "project", "add", "demo", "--label", "Example")
    result = call(env, "project", "show", "demo", "--json")
    assert result["controller"]["session_id"] is None
    assert result["controller"]["epoch"] == 0
    call(env, "project", "add", "demo", "--label", "Changed", rc=3)


def test_reconcile_writes_health(env):
    call(env, "reconcile")
    health = json.loads((Path(env["YARDGOAT_HOME"]) / "reconcile-health.json").read_text())
    assert health["last_started_at"] and health["last_finished_at"] and health["last_success_at"]
    assert health["errors"]["count"] == 0


AGENT = r'''
import ctypes, json, os, pathlib, subprocess, sys, time

def execute(request):
    if request.get("comm"):
        ctypes.CDLL(None).prctl(15, request["comm"].encode(), 0, 0, 0)
    args = [sys.executable, "-m", "yardgoat.cli", *request["args"]]
    if request.get("nested"):
        args = [sys.executable, __file__, "--once", json.dumps({"args": request["args"]})]
    p = subprocess.run(args, env={**os.environ, **request.get("env", {})}, capture_output=True, text=True, timeout=15)
    if request.get("nested"):
        return json.loads(p.stdout)
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr, "parent_pid": os.getpid()}

if sys.argv[1] == "--launch":
    pid = os.fork()
    if pid:
        sys.exit(0)
    os.setsid()
    with open(os.devnull, "r+b", buffering=0) as f:
        for n in (0, 1, 2): os.dup2(f.fileno(), n)
    os.execv(sys.argv[2], [sys.argv[2], __file__, sys.argv[3]])
elif sys.argv[1] == "--once":
    print(json.dumps(execute(json.loads(sys.argv[2]))))
else:
    root = pathlib.Path(sys.argv[1])
    (root / "pid").write_text(str(os.getpid()))
    until = time.monotonic() + 120
    while time.monotonic() < until and not (root / "stop").exists():
        for request in root.glob("*.request"):
            result = execute(json.loads(request.read_text()))
            temp = request.with_suffix(".tmp")
            temp.write_text(json.dumps(result))
            temp.replace(request.with_suffix(".response"))
            request.unlink()
        time.sleep(0.01)
'''


class Agent:
    def __init__(self, root):
        self.root = root
        self.pid = int((root / "pid").read_text())

    def raw(self, *args, **options):
        name = self.root / uuid.uuid4().hex
        name.with_suffix(".tmp").write_text(json.dumps({"args": list(map(str, args)), **options}))
        name.with_suffix(".tmp").replace(name.with_suffix(".request"))
        end = time.monotonic() + 20
        response = name.with_suffix(".response")
        while not response.exists() and time.monotonic() < end:
            time.sleep(0.01)
        result = json.loads(response.read_text())
        response.unlink()
        return result

    def call(self, *args, rc=0, **options):
        result = self.raw(*args, **options)
        assert result["rc"] == rc, result
        return json.loads(result["out"]) if result["out"].strip() else result["err"]

    def stop(self):
        (self.root / "stop").touch()
        from yardgoat.sessions import liveness
        from yardgoat.sessions import ProcFS
        try:
            proc = ProcFS()
            rec = {"identity": {"boot_id": proc.boot_id(), "pid": self.pid,
                                 "start_ticks": proc.stat(self.pid)["start_ticks"]}}
        except FileNotFoundError:
            return
        end = time.monotonic() + 3
        while liveness(rec) == "alive" and time.monotonic() < end:
            time.sleep(0.01)
        assert liveness(rec) == "dead"


@pytest.fixture
def agents(tmp_path, env):
    config = {"sessions": {"agents": [{"name": "fixture", "exe": "fake-agent"}]},
              "project_admins": ["operator"]}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(config))
    executable = tmp_path / "fake-agent"
    shutil.copyfile(sys.executable, executable)
    executable.chmod(0o700)
    script = tmp_path / "fake_agent.py"
    script.write_text(AGENT)
    workers = []
    try:
        for i in range(2):
            root = tmp_path / f"agent-{i}"
            root.mkdir()
            # Detach from the real station running this suite; the test process has its
            # own proc ancestry and a configuration that replaces all real agent names.
            subprocess.run([sys.executable, str(script), "--launch", str(executable), str(root)],
                           env=env, check=True, timeout=5)
            end = time.monotonic() + 5
            while not (root / "pid").exists() and time.monotonic() < end:
                time.sleep(0.01)
            workers.append(Agent(root))
        yield workers
    finally:
        for agent in workers:
            agent.stop()


def registered(agents):
    return [a.call("session", "register", "--label", f"fixture-{i}")["session_id"]
            for i, a in enumerate(agents)]


def controlled(env, agents, policy="open"):
    ids = registered(agents)
    call(env, "project", "add", "demo", "--claim", policy)
    if policy == "open":
        agents[0].call("project", "claim", "demo", "--expect-epoch", "0")
    return ids


def test_distinct_processes_same_as_and_registration_idempotent(env, agents):
    identities = [a.call("whoami", "--as", "same", "--json") for a in agents]
    assert identities[0]["session_id"] != identities[1]["session_id"]
    assert [i["identity"]["pid"] for i in identities] == [a.pid for a in agents]
    assert all(not i["registered"] for i in identities)
    for a in agents:
        first = a.call("session", "register", "--label", "sample")
        assert first == a.call("session", "register", "--label", "sample")
        identity = a.call("whoami", "--json")
        assert identity["registered"] and identity["label"] == "sample" and identity["projects"] == []
        a.call("whoami", "--session", identities[1 if a is agents[0] else 0]["session_id"], "--json", rc=3)
    assert len(call(env, "session", "list", "--json")) == 2


def test_nearest_nested_agent_and_parentheses_in_comm(agents):
    outer = agents[0].call("whoami", "--json")
    nested = agents[0].raw("whoami", "--json", nested=True)
    inner = json.loads(nested["out"])
    assert nested["rc"] == 0 and inner["identity"]["pid"] == nested["parent_pid"]
    assert inner["session_id"] != outer["session_id"]
    unusual = agents[0].call("whoami", "--json", comm="a (b) c)")
    assert unusual["session_id"] == outer["session_id"]
    assert unusual["evidence"]["comm"] == "a (b) c)"


def test_station_env_refused_even_with_separate_store(agents):
    a = agents[0]
    for key in ("YARDGOAT_STATION", "YARDGOAT_MAILBOX", "YARDGOAT_MAILBOX_HOME"):
        result = a.call("whoami", "--json", rc=3, env={key: "fixture"})
        assert "station" in result["reason"]
        assert "station" in a.call("session", "register", rc=3, env={key: "fixture"})


def test_admin_register_validates_exact_pid_and_audits(env, agents):
    # Run the administrator from the detached fixture too, to avoid the host station ancestry.
    result = agents[0].call("session", "register", "--pid", agents[1].pid,
                            "--as", "operator", "--reason", "inventory")
    assert result["identity"]["pid"] == agents[1].pid
    assert result["registrations"][0]["by"] == "operator"
    assert result["registrations"][0]["reason"] == "inventory"
    agents[0].call("session", "register", "--pid", "1", "--as", "operator", "--reason", "inventory", rc=3)
    agents[0].call("session", "register", "--pid", agents[1].pid, "--as", "outsider", "--reason", "inventory", rc=3)
    with subprocess.Popen(["sleep", "5"]) as process:
        try:
            agents[0].call("session", "register", "--pid", process.pid,
                           "--as", "operator", "--reason", "inventory", rc=3)
        finally:
            process.terminate()


def test_alive_dead_and_dead_controller_does_not_reopen_claim(env, agents):
    ids = controlled(env, agents)
    assert all(r["liveness"] == "alive" for r in call(env, "session", "list", "--json"))
    agents[0].stop()
    listed = {r["session_id"]: r for r in call(env, "session", "list", "--json")}
    assert listed[ids[0]]["liveness"] == "dead" and listed[ids[1]]["liveness"] == "alive"
    agents[1].call("project", "claim", "demo", "--expect-epoch", "1", rc=3)
    agents[1].call("project", "handoff", "demo", "--to", ids[0], "--expect-epoch", "1", rc=3)
    call(env, "project", "assign", "demo", "--session", ids[1], "--expect-epoch", "1",
         "--as", "operator", "--reason", "recover")


def test_concurrent_claim_has_one_winner(env, agents):
    ids = registered(agents)
    call(env, "project", "add", "demo")
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda a: a.raw("project", "claim", "demo", "--expect-epoch", "0"), agents))
    assert sorted(r["rc"] for r in results) == [0, 3]
    state = call(env, "project", "show", "demo", "--json")
    assert state["controller"]["epoch"] == 1 and state["controller"]["session_id"] in ids
    assert len(state["history"]) == 1


def test_closed_claim_policy_and_unregistered_refused(env, agents):
    call(env, "project", "add", "demo")
    agents[0].call("project", "claim", "demo", "--expect-epoch", "0", rc=3)
    registered(agents)
    call(env, "project", "add", "private", "--claim", "admin")
    agents[0].call("project", "claim", "private", "--expect-epoch", "0", rc=3)


def test_handoff_cas_and_epoch_fences_aba(env, agents):
    from yardgoat import projects, sessions
    ids = controlled(env, agents)
    identity = agents[0].call("whoami", "--json")
    assert identity["projects"] == ["demo"]
    ticket = projects.authorize_control("demo", identity)
    assert ticket == (ids[0], 1)
    agents[1].call("project", "handoff", "demo", "--to", ids[0], "--expect-epoch", "1", rc=3)
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", "0", rc=3)
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", "1")
    agents[1].call("project", "handoff", "demo", "--to", ids[0], "--expect-epoch", "2")
    with pytest.raises(sessions.Refused, match="epoch"):
        projects.recheck("demo", *ticket)
    assert projects.authorize_control("demo", identity) == (ids[0], 3)
    with projects.locked("demo") as state:
        assert projects.recheck_locked(state, ids[0], 3) == (ids[0], 3)
    with pytest.raises(sessions.Refused, match="not identified"):
        projects.authorize_control("demo", "some-label")
    with pytest.raises(sessions.Refused, match="does not exist"):
        projects.authorize_control("missing", identity)


def test_transfer_commit_token_idempotence_and_stale_replay(env, agents):
    from yardgoat import projects, sessions
    ids = controlled(env, agents)
    identity = agents[0].call("whoami", "--json")
    start = agents[0].call("project", "transfer", "begin", "demo", "--expect-epoch", "1")
    tid, token = start["transfer_id"], start["token"]
    state = call(env, "project", "show", "demo", "--json")
    assert state["status"] == "transferring" and state["controller"]["epoch"] == 1
    assert token not in json.dumps(state)
    for fn in (lambda: projects.authorize_control("demo", identity), lambda: projects.recheck("demo", ids[0], 1)):
        with pytest.raises(sessions.Refused, match="transferring"):
            fn()
    agents[0].call("project", "transfer", "begin", "demo", "--expect-epoch", "1", rc=3)
    agents[0].call("project", "handoff", "demo", "--to", ids[1], "--expect-epoch", "1", rc=3)
    agents[1].call("project", "claim", "demo", "--expect-epoch", "1", rc=3)
    base = ("project", "transfer", "commit", "demo", "--transfer", tid)
    agents[1].call(*base, "--token", "wrong", rc=3)
    agents[0].stop()  # old agent can disappear between the two phases
    result = agents[1].call(*base, "--token", token)
    assert result["controller"] == call(env, "project", "show", "demo", "--json")["controller"]
    assert result["controller"]["epoch"] == 2
    assert agents[1].call(*base, "--token", token) == result
    call(env, *base, "--token", token, "--to", ids[1])  # explicit target + bearer token
    call(env, "project", "assign", "demo", "--session", ids[1], "--expect-epoch", "2",
         "--as", "operator", "--reason", "new term")
    agents[1].call(*base, "--token", token, rc=3)


def test_transfer_abort_both_paths_and_admin_cancels(env, agents):
    ids = controlled(env, agents)
    begin = lambda: agents[0].call("project", "transfer", "begin", "demo", "--expect-epoch", "1")
    first = begin()
    abort = ("project", "transfer", "abort", "demo", "--transfer", first["transfer_id"])
    agents[1].call(*abort, rc=3)
    call(env, *abort, "--token", "wrong", rc=3)
    done = call(env, *abort, "--token", first["token"])
    assert done["controller"]["epoch"] == 1
    assert agents[0].call(*abort) == done
    second = begin()
    agents[0].call("project", "transfer", "abort", "demo", "--transfer", second["transfer_id"])
    third = begin()
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg.pop("project_admins")
    cfg["approvers"] = ["operator"]
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    args = ("project", "assign", "demo", "--session", ids[1], "--expect-epoch", "1",
            "--as", "operator", "--reason", "recovery")
    call(env, *args, rc=3)
    cfg["project_admins"] = ["operator"]
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    assigned = call(env, *args)
    assert assigned["controller"]["epoch"] == 2 and assigned["transfer"] is None
    assert assigned["transfers"][third["transfer_id"]]["status"] == "cancelled"
    assert assigned["history"][-1]["old_status"] == "transferring"
    for previous in (first, second, third):
        agents[1].call("project", "transfer", "commit", "demo", "--transfer", previous["transfer_id"],
                       "--token", previous["token"], rc=3)


def test_concurrent_commit_and_new_transfer_invalidate_old_token(env, agents):
    ids = controlled(env, agents)
    first = agents[0].call("project", "transfer", "begin", "demo", "--expect-epoch", "1")
    args = ("project", "transfer", "commit", "demo", "--transfer", first["transfer_id"],
            "--token", first["token"], "--to", ids[1])
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: call(env, *args), range(2)))
    assert results[0] == results[1] and results[0]["controller"]["epoch"] == 2
    second = agents[1].call("project", "transfer", "begin", "demo", "--expect-epoch", "2")
    call(env, *args, rc=3)
    agents[0].call("project", "transfer", "commit", "demo", "--transfer", second["transfer_id"], "--token", second["token"])
    call(env, *args, rc=3)
    agents[0].call("project", "claim", "demo", "--expect-epoch", "3", "--session", ids[1], rc=3)


@pytest.mark.parametrize("action", ["begin", "abort"])
@pytest.mark.parametrize("after_replace", [False, True])
def test_other_transfer_crash_boundaries(env, agents, monkeypatch, action, after_replace):
    from yardgoat import projects, sessions, statefile
    controlled(env, agents)
    caller = agents[0].call("whoami", "--json")
    if action == "abort":
        start = projects.begin("demo", caller, 1)
    else:
        monkeypatch.setattr(projects.secrets, "token_urlsafe", lambda _: "fixture-only-token")
    original = statefile.os.replace
    def crash(source, target):
        if after_replace:
            original(source, target)
        raise RuntimeError("crash during atomic replace")
    def command():
        return (projects.begin("demo", caller, 1) if action == "begin" else
                projects.abort("demo", start["transfer_id"], caller=caller))
    with monkeypatch.context() as m:
        m.setattr(statefile.os, "replace", crash)
        with pytest.raises(RuntimeError):
            command()
    if action == "begin" and after_replace:
        with pytest.raises(sessions.Refused, match="transferring"):
            command()
        state = projects.show("demo")
        # Token output lost: existing controller can abort without recovering a secret.
        projects.abort("demo", state["transfer"], caller=caller)
    else:
        command()
    assert projects.show("demo")["controller"]["epoch"] == 1


class FakeProc:
    def __init__(self):
        self.rows = {
            1: self.row(1, 0, "init"), 11: self.row(11, 1, "fake"),
            12: self.row(12, 11, "fake"), 13: self.row(13, 12, "python")}

    @staticmethod
    def row(pid, ppid, exe):
        return {"pid": pid, "ppid": ppid, "comm": exe, "exe": exe, "argv0": exe,
                "argv": [exe], "env": {}, "start_ticks": str(pid * 10), "state": "S"}

    def stat(self, pid):
        if pid not in self.rows:
            raise FileNotFoundError(f"missing {pid}")
        return {k: self.rows[pid][k] for k in ("pid", "comm", "state", "ppid", "start_ticks")}

    def process(self, pid):
        self.stat(pid)
        return dict(self.rows[pid])

    def boot_id(self):
        return "fixture-boot"


def test_proc_ambiguity_station_ancestor_and_no_outer_fallback():
    from yardgoat import sessions
    proc = FakeProc()
    cfg = {"sessions": {"agents": [{"name": "test", "exe": "fake"}]}}
    identify = lambda: sessions.identify(cfg, pid=13, proc=proc, env={})
    assert identify()["identity"]["pid"] == 12
    proc.rows[11]["argv"] = ["python", "-m", "yardgoat.cli", "_station", "example", "example-st-1"]
    assert "station" in identify()["reason"]
    proc.rows[11]["argv"] = ["fake"]
    proc.rows[12]["comm"] = "inner"
    cfg["sessions"]["agents"].append({"name": "ambiguous", "comm": "inner"})
    assert "ambiguous" in identify()["reason"]
    cfg["sessions"]["agents"] = []
    assert identify()["session_id"] is None  # replaces defaults entirely
    assert sessions.matches(FakeProc.row(2, 1, "node"), sessions.DEFAULT_AGENTS) is None
    assert sessions.matches(FakeProc.row(2, 1, "python"), sessions.DEFAULT_AGENTS) is None


def test_proc_permission_chain_break_and_unknown_liveness(monkeypatch):
    from yardgoat import sessions
    proc = FakeProc()
    cfg = {"sessions": {"agents": [{"name": "fixture", "exe": "fake"}]}}
    rec = sessions.identify(cfg, pid=13, proc=proc, env={})
    assert sessions.liveness(rec, proc) == "alive"
    proc.rows[12]["start_ticks"] = "999"
    assert sessions.liveness(rec, proc) == "dead"
    proc.rows.pop(12)
    assert sessions.liveness(rec, proc) == "dead"
    assert sessions.identify(cfg, pid=13, proc=proc, env={})["session_id"] is None
    def denied(*args):
        raise PermissionError("proc denied")
    monkeypatch.setattr(proc, "stat", denied)
    assert sessions.liveness(rec, proc) == "unknown"
    assert "denied" in sessions.identify(cfg, pid=13, proc=proc, env={})["reason"]


def test_endpoint_verification_never_changes_identity():
    from yardgoat import sessions
    proc = FakeProc()
    cfg = {"sessions": {"agents": [{"name": "fixture", "exe": "fake"}]}}
    env = {"TMUX": "/tmp/test-socket,100,1", "TMUX_PANE": "%7"}
    results = [sessions.identify(cfg, pid=13, proc=proc, env=env, lookup=lambda *_: pid)
               for pid in (11, 12, 13, 99)]
    assert [r["endpoint"]["verified"] for r in results] == [True, True, False, False]
    assert len({r["session_id"] for r in results}) == 1
    def unavailable(*args):
        raise FileNotFoundError("no tmux")
    result = sessions.identify(cfg, pid=13, proc=proc, env=env, lookup=unavailable)
    assert result["session_id"] == results[0]["session_id"] and not result["endpoint"]["verified"]


@pytest.mark.parametrize("selector", ["exe", "comm", "argv0"])
def test_each_configured_selector_and_no_process_group_requirement(selector):
    from yardgoat import sessions
    proc = FakeProc()
    cfg = {"sessions": {"agents": [{"name": "only-inner", selector: "fixture"}]}}
    proc.rows[12][selector] = "fixture"
    assert sessions.identify(cfg, pid=13, proc=proc, env={})["identity"]["pid"] == 12
    fields = ["S", "11", "999", *("0" for _ in range(16)), "1234", "0"]
    parsed = sessions.parse_stat("12 (has ) (space)) " + " ".join(fields))
    assert parsed["start_ticks"] == "1234" and parsed["comm"] == "has ) (space)"


@pytest.mark.parametrize("after_replace", [False, True])
def test_atomic_transfer_crash_and_retry(env, agents, monkeypatch, after_replace):
    from yardgoat import projects, statefile
    ids = controlled(env, agents)
    caller = agents[0].call("whoami", "--json")
    start = projects.begin("demo", caller, 1)
    original = statefile.os.replace
    def interrupted(source, target):
        if after_replace:
            original(source, target)
        raise RuntimeError("simulated process crash at rename")
    with monkeypatch.context() as m:
        m.setattr(statefile.os, "replace", interrupted)
        with pytest.raises(RuntimeError):
            projects.commit("demo", start["transfer_id"], start["token"], target=ids[1])
    result = projects.commit("demo", start["transfer_id"], start["token"], target=ids[1])
    assert result["controller"]["epoch"] == 2
    state = projects.show("demo")
    assert len([e for e in state["history"] if e["kind"] == "project.transfer_committed"]) == 1


def test_health_status_doctor_and_failure_does_not_advance_success(env, monkeypatch, capsys):
    from yardgoat import cli, health
    call(env, "reconcile")
    previous = health.read()["last_success_at"]
    cfg = json.loads(Path(env["YARDGOAT_CONFIG"]).read_text())
    cfg["notify"] = {"command": []}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    response = subprocess.run([sys.executable, "-m", "yardgoat.cli", "reconcile"], env=env,
                              capture_output=True, text=True, timeout=10)
    assert response.returncode == 0 and "notify failed" in response.stderr
    result = call(env, "reconcile")["reconcile_health"]
    assert result["last_success_at"] == previous and result["errors"]["count"] == 1
    status = call(env, "status", "--json")
    assert status[0]["reconcile_health"] == result
    result["last_finished_at"] = "2000-01-01T00:00:00+0000"
    (Path(env["YARDGOAT_HOME"]) / "reconcile-health.json").write_text(json.dumps(result))
    # Probe injection avoids real tmux, models, or a launch chain in this unit test.
    monkeypatch.setattr(cli.doctor, "run", lambda *a, **kw: {"checks": [{"name": "launcher", "ok": True}], "ok": True})
    assert cli.cmd_doctor(SimpleNamespace(pool=None, host="shell", lane="", acct=None, json=True)) == 1
    assert "stale" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_reconcile", lambda *a: (_ for _ in ()).throw(RuntimeError("broken action")))
    assert cli.cmd_reconcile(SimpleNamespace()) == 2
    assert health.read()["errors"]["count"] == 1 and "broken action" in health.read()["errors"]["first"][0]


def test_health_is_independent_of_control_and_error_summary_is_bounded(env, agents):
    from yardgoat import health, projects
    controlled(env, agents)
    identity = agents[0].call("whoami", "--json")
    ticket = projects.authorize_control("demo", identity)
    health.start()
    result = health.finish(["x" * 2000 for _ in range(10)])
    assert result["errors"]["count"] == 10
    assert len(result["errors"]["first"]) == 5 and len(result["errors"]["first"][0]) == 1000
    assert result["last_success_at"] is None
    assert projects.recheck("demo", *ticket) == ticket
