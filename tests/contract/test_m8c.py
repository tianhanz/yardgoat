"""Supervisor delegation and recovery, with injected proc and isolated real processes."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_m8a import FakeProc, env
from yardgoat import cli, projects, sessions, statefile
from yardgoat.store import TaskDir, home, write_json


TRANSFER = "t-" + "1" * 32
OTHER_TRANSFER = "t-" + "2" * 32
SECRET = "yg-" + "a" * 40


class Proc(FakeProc):
    def __init__(self):
        # grandparent -> supervisor -> agent A -> command
        #                           -> helper
        #             -> agent B -> command
        #             -> unrelated sibling
        self.rows = {pid: self.row(pid, parent, name) for pid, parent, name in (
            (1, 0, "init"), (10, 1, "shell"), (20, 10, "supervisor"),
            (21, 20, "helper"), (30, 20, "fake"), (31, 30, "python"),
            (40, 10, "fake"), (41, 40, "python"), (50, 10, "shell"))}
        self.boot = "fixture-boot"
        self.confirm_absence = True

    def boot_id(self):
        return self.boot

    def uid(self, pid):
        self.stat(pid)
        return self.rows[pid].get("uid", os.getuid())

    def absence_confirmed(self, pid):
        return self.confirm_absence and super().absence_confirmed(pid)


@pytest.fixture
def world(env, tmp_path, monkeypatch):
    proc = Proc()
    cfg = {"sessions": {"agents": [{"name": "fixture", "exe": "fake"}]}}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(cfg))
    monkeypatch.setattr(sessions, "ProcFS", lambda: proc)
    w = SimpleNamespace(proc=proc, cfg=cfg, pid=31)
    monkeypatch.setattr(sessions.os, "getppid", lambda: w.pid)
    w.a = sessions.identify(cfg, pid=31, env={})
    w.b = sessions.identify(cfg, pid=41, env={})
    w.token = tmp_path / "token"
    w.token.write_text("\n  " + SECRET + "\n")
    w.token.chmod(0o600)
    return w


@pytest.fixture
def invoke(capsys):
    def run(*args, rc=0):
        try:
            actual = cli.main(list(map(str, args)))
        except SystemExit as exc:
            actual = exc.code
        output = capsys.readouterr()
        assert actual == rc, (actual, output.out, output.err)
        return json.loads(output.out) if output.out.strip() else output.err
    return run


def ready(w, reclaim="none"):
    sessions.register(w.a, bind_supervisor=True, cfg=w.cfg)
    sessions.register(w.b)
    projects.add("demo", reclaim=reclaim)
    projects.claim("demo", w.a, 0)


def begin(w, **kwargs):
    return projects.begin("demo", w.a, 1, transfer_id=TRANSFER, token_file=w.token, **kwargs)


def test_binding_is_opt_in_immutable_and_preserved(world):
    w = world
    assert "supervisor" not in sessions.register(w.a)
    bound = sessions.register(w.a, bind_supervisor=True, cfg=w.cfg)
    assert bound["supervisor"] == {"boot_id": w.proc.boot, "pid": 20, "start_ticks": "200", "uid": os.getuid()}
    path = home() / "sessions" / (w.a["session_id"] + ".json")
    before = path.read_bytes()
    assert sessions.register(w.a, bind_supervisor=True, cfg=w.cfg) == bound
    assert sessions.register(w.a) == bound
    assert path.read_bytes() == before
    w.proc.rows[30]["ppid"] = 10
    with pytest.raises(sessions.Refused, match="rebinding"):
        sessions.register(w.a, bind_supervisor=True, cfg=w.cfg)
    assert path.read_bytes() == before
    assert sessions.list_sessions()[0]["supervisor"] == bound["supervisor"]


@pytest.mark.parametrize("change,reason", [
    ({"exe": "fake"}, "agent process"),
    ({"env": {"YARDGOAT_STATION": "fixture"}}, "station"),
    ({"uid": -1}, "different uid"),
    ({"state": "Z"}, "not alive"),
])
def test_binding_rejects_ineligible_parent(world, change, reason):
    world.proc.rows[20].update(change)
    with pytest.raises(sessions.Refused, match=reason):
        sessions.register(world.a, bind_supervisor=True, cfg=world.cfg)
    assert sessions.record(world.a["session_id"]) is None


def test_binding_parent_change_during_inspection_refused(world, monkeypatch):
    original = world.proc.process
    def changed(pid):
        result = original(pid)
        world.proc.rows[30]["ppid"] = 10
        return result
    monkeypatch.setattr(world.proc, "process", changed)
    with pytest.raises(sessions.Refused, match="changed"):
        sessions.register(world.a, bind_supervisor=True, cfg=world.cfg)


def test_supervisor_begin_abort_audit_and_read_only_views(world, invoke, monkeypatch):
    w = world
    ready(w)
    w.pid = 21
    # The delegated CLI path must not call nearest-agent identification.
    monkeypatch.setattr(sessions, "identify", lambda *a, **k: pytest.fail("ordinary identify was called"))
    args = ("project", "transfer", "begin", "demo", "--expect-epoch", 1,
            "--transfer", TRANSFER, "--token-file", w.token, "--for-session", w.a["session_id"])
    result = invoke(*args)
    before = projects.path("demo").read_bytes()
    assert invoke(*args, "--session", w.a["session_id"]) == result
    assert projects.path("demo").read_bytes() == before
    for state in (invoke("project", "show", "demo"), invoke("project", "list")[0]):
        assert state["reclaim"] == "none" and state["reclaim_effective"] == "none"
        assert state["active_transfer"]["from"] == w.a["session_id"]
        assert state["active_transfer"]["via"] == "supervisor"
        ev = state["history"][-1]
        assert ev["actor"] == w.a["session_id"] and ev["via"] == "supervisor"
        assert ev["supervisor"] == sessions.record(w.a["session_id"])["supervisor"]
    assert projects.path("demo").read_bytes() == before
    aborted = invoke("project", "transfer", "abort", "demo", "--transfer", TRANSFER,
                     "--for-session", w.a["session_id"])
    assert aborted["result"] == "aborted"
    assert invoke(*args) == aborted
    ev = projects.show("demo")["history"][-1]
    assert ev["kind"] == "project.transfer_aborted" and ev["via"] == "supervisor"
    assert ev["actor"] == w.a["session_id"] and ev["supervisor"]["pid"] == 20


@pytest.mark.parametrize("pid,reason", [(10, "ancestry"), (50, "ancestry"), (31, "agent process"), (41, "agent process")])
def test_grandparent_sibling_and_agents_cannot_impersonate_supervisor(world, pid, reason):
    w = world
    ready(w)
    w.pid = pid
    with pytest.raises(sessions.Refused, match=reason):
        begin(w, for_session=w.a["session_id"], cfg=w.cfg)
    assert projects.show("demo")["transfer"] is None


@pytest.mark.parametrize("mutation,reason", [
    ("reused", "start_ticks"), ("agent_dead", "alive"), ("reparented", "direct parent"),
    ("uid", "uid"), ("station_above", "station"), ("unbound", "no bound"),
    ("missing", "not registered"), ("not_controller", "controller"),
])
def test_supervisor_checks_cannot_be_skipped(world, mutation, reason):
    w = world
    ready(w)
    w.pid = 21
    if mutation == "reused":
        w.proc.rows[20]["start_ticks"] = "201"
    elif mutation == "agent_dead":
        w.proc.rows[30]["state"] = "Z"
    elif mutation == "reparented":
        w.proc.rows[30]["ppid"] = 10
    elif mutation == "uid":
        w.proc.rows[20]["uid"] = os.getuid() + 1
    elif mutation == "station_above":
        w.proc.rows[10]["env"] = {"YARDGOAT_MAILBOX_HOME": "fixture"}
    elif mutation == "not_controller":
        projects.handoff("demo", w.a, w.b["session_id"], 1)
    else:
        p = home() / "sessions" / (w.a["session_id"] + ".json")
        if mutation == "missing":
            p.unlink()
        else:
            rec = sessions.record(w.a["session_id"])
            rec.pop("supervisor")
            write_json(p, rec)
    with pytest.raises(sessions.Refused, match=reason):
        begin(w, for_session=w.a["session_id"], cfg=w.cfg)


def test_supervisor_current_station_env_and_changing_chain(world, monkeypatch):
    w = world
    ready(w)
    w.pid = 21
    for key in sessions.STATION_KEYS:
        with monkeypatch.context() as m:
            m.setenv(key, "fixture")
            with pytest.raises(sessions.Refused, match="station"):
                begin(w, for_session=w.a["session_id"], cfg=w.cfg)
    original = w.proc.process
    def changed(pid):
        value = original(pid)
        if pid == 20:
            w.proc.rows[21]["ppid"] = 10
        return value
    monkeypatch.setattr(w.proc, "process", changed)
    with pytest.raises(sessions.Refused, match="changed"):
        begin(w, for_session=w.a["session_id"], cfg=w.cfg)


def test_supervisor_has_no_other_control_privileges(world, invoke):
    w = world
    ready(w)
    td = TaskDir("fixture-card")
    with td.locked() as box:
        box["state"] = {"owner_ref": {"project": "demo"}, "owner": "fixture", "status": "accepted"}
    w.pid = 21
    assert invoke("whoami", rc=3)["status"] == "unknown"
    commands = [
        ("project", "claim", "demo", "--expect-epoch", 1),
        ("project", "reclaim", "demo", "--expect-epoch", 1),
        ("project", "handoff", "demo", "--expect-epoch", 1, "--to", w.b["session_id"]),
        ("park", "fixture-card", "--as", "fixture", "--question", "continue"),
    ]
    for args in commands:
        assert "no configured agent" in invoke(*args, rc=3)
        invoke(*args, "--for-session", w.a["session_id"], rc=2)
    invoke("session", "register", "--for-session", w.a["session_id"], rc=2)
    invoke("project", "transfer", "commit", "demo", "--transfer", TRANSFER,
           "--token-file", w.token, "--for-session", w.a["session_id"], rc=2)


def test_pending_begin_is_exactly_repeatable_without_plaintext(world):
    w = world
    ready(w)
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: begin(w), range(2)))
    assert results[0] == results[1] and "token" not in results[0]
    before = projects.path("demo").read_bytes()
    assert begin(w) == results[0] and projects.path("demo").read_bytes() == before
    assert SECRET not in before.decode() and SECRET not in json.dumps(results)
    state = projects.show("demo")
    assert len([e for e in state["history"] if e["kind"] == "project.transfer_begun"]) == 1
    assert "via" not in state["history"][-1]
    assert state["active_transfer"]["via"] == "session"


@pytest.mark.parametrize("terminal", ["committed", "aborted"])
def test_terminal_begin_returns_historical_result_before_controller_cas(world, terminal):
    w = world
    ready(w)
    begin(w)
    if terminal == "committed":
        result = projects.commit("demo", TRANSFER, token_file=w.token, caller=w.b)
    else:
        result = projects.abort("demo", TRANSFER, token_file=w.token)
        projects.handoff("demo", w.a, w.b["session_id"], 1)
    # A later, pending transfer must not hide the original terminal result.
    projects.begin("demo", w.b, 2)
    before = projects.path("demo").read_bytes()
    assert begin(w) == result and result["result"] == terminal
    assert projects.path("demo").read_bytes() == before
    w.pid = 21
    with pytest.raises(sessions.Refused, match="controller"):
        begin(w, for_session=w.a["session_id"], cfg=w.cfg)
    w.proc.rows[30]["state"] = "Z"
    with pytest.raises(sessions.Refused, match="alive"):
        begin(w)


def test_replay_mismatches_and_cancelled_transfers_refused(world):
    w = world
    ready(w)
    begin(w)
    before = projects.path("demo").read_bytes()
    for caller, epoch, transfer in ((w.b, 1, TRANSFER), (w.a, 2, TRANSFER), (w.a, 1, OTHER_TRANSFER)):
        with pytest.raises(sessions.Refused):
            projects.begin("demo", caller, epoch, transfer_id=transfer, token_file=w.token)
    w.token.write_text("b" * 40)
    with pytest.raises(sessions.Refused, match="token"):
        begin(w)
    assert projects.path("demo").read_bytes() == before
    w.token.write_text(SECRET)
    projects.assign("demo", w.b["session_id"], 1, {"project_admins": ["operator"]}, "operator", "recover")
    with pytest.raises(sessions.Refused, match="pending"):
        begin(w)


@pytest.mark.parametrize("after_replace", [False, True])
def test_caller_generated_begin_recovers_lost_response(world, monkeypatch, after_replace):
    w = world
    ready(w)
    original = statefile.os.replace
    def crash(source, target):
        if after_replace:
            original(source, target)
        raise RuntimeError("simulated lost response")
    with monkeypatch.context() as m:
        m.setattr(statefile.os, "replace", crash)
        with pytest.raises(RuntimeError):
            begin(w)
    result = begin(w)
    assert result["transfer_id"] == TRANSFER and "token" not in result
    assert len([e for e in projects.show("demo")["history"] if e["kind"] == "project.transfer_begun"]) == 1


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o700, 0o4600])
@pytest.mark.parametrize("action", ["begin", "commit", "abort"])
def test_token_file_permissions_apply_to_every_operation(world, mode, action):
    w = world
    ready(w)
    if action != "begin":
        begin(w)
    w.token.chmod(mode)
    with pytest.raises(sessions.Refused, match="0600"):
        if action == "begin":
            begin(w)
        elif action == "commit":
            projects.commit("demo", TRANSFER, caller=w.b, token_file=w.token)
        else:
            projects.abort("demo", TRANSFER, token_file=w.token)


def test_token_file_owner_and_other_unsafe_inputs(world, monkeypatch, tmp_path):
    w = world
    original = projects.os.fstat
    with monkeypatch.context() as m:
        def foreign(fd):
            info = original(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
        m.setattr(projects.os, "fstat", foreign)
        with pytest.raises(sessions.Refused, match="current uid"):
            projects.read_token_file(w.token)
    link = tmp_path / "link"
    link.symlink_to(w.token)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    for path in (link, fifo, tmp_path, tmp_path / "missing"):
        with pytest.raises(sessions.Refused):
            projects.read_token_file(path)
    for value in (b"short", b"-" + b"a" * 40, b"\xff" * 40):
        w.token.write_bytes(value)
        with pytest.raises(sessions.Refused):
            projects.read_token_file(w.token)
    w.token.write_text(" \n" + SECRET + "\t")
    w.token.chmod(0o400)
    assert projects.read_token_file(w.token) == SECRET


@pytest.mark.parametrize("transfer", ["t-ABC" + "0" * 29, "t-" + "0" * 31, "../transfer", "t-" + "g" * 32])
def test_invalid_caller_transfer_ids_refused(world, transfer):
    ready(world)
    with pytest.raises(sessions.Refused, match="lowercase hexadecimal"):
        projects.begin("demo", world.a, 1, transfer_id=transfer, token_file=world.token)


def test_cli_option_combinations_are_usage_errors_before_identity(world, invoke, monkeypatch):
    w = world
    monkeypatch.setenv("YARDGOAT_STATION", "fixture")
    base = ("project", "transfer", "begin", "demo", "--expect-epoch", 1)
    invoke(*base, "--transfer", TRANSFER, rc=2)
    invoke(*base, "--token-file", w.token, rc=2)
    for action in ("commit", "abort"):
        args = ("project", "transfer", action, "demo", "--transfer", TRANSFER)
        invoke(*args, "--token", SECRET, "--token-file", w.token, rc=2)
        if action == "abort":
            for option, value in (("--token", SECRET), ("--token-file", w.token)):
                invoke(*args, option, value, "--for-session", w.a["session_id"], rc=2)


@pytest.mark.parametrize("reason", ["boot_changed", "start_ticks_mismatch", "absence_confirmed", "zombie"])
def test_reclaim_commits_epoch_and_actual_liveness_reason(world, reason):
    w = world
    ready(w, "dead")
    if reason == "boot_changed":
        w.proc.boot = "next-boot"
        w.b = sessions.identify(w.cfg, pid=41, env={})
        sessions.register(w.b)
    elif reason == "start_ticks_mismatch":
        w.proc.rows[30]["start_ticks"] = "301"
    elif reason == "absence_confirmed":
        del w.proc.rows[30]
    else:
        w.proc.rows[30]["state"] = "Z"
    assert sessions.liveness(sessions.record(w.a["session_id"])) == "dead"
    state = projects.reclaim("demo", w.b, 1)
    assert state["controller"]["session_id"] == w.b["session_id"]
    assert state["controller"]["epoch"] == 2 and state["controller"]["how"] == "reclaim"
    event = state["history"][-1]
    assert event["kind"] == "project.reclaimed" and event["old_session"] == w.a["session_id"]
    assert event["dead_reason"] == reason and event["actor"] == w.b["session_id"]


@pytest.mark.parametrize("condition,reason", [
    ("policy", "allow reclaim"), ("unknown_caller", "no configured agent"),
    ("unregistered_caller", "not registered"), ("dead_caller", "alive"), ("station", "station"),
    ("no_controller", "existing project controller"), ("alive_controller", "alive"),
    ("missing_controller_record", "unknown"), ("unknown_controller", "unknown"),
    ("hidden_controller", "unknown"), ("transferring", "token or abort"), ("cas", "epoch conflict"),
])
def test_each_reclaim_gate_refuses_without_mutation(world, condition, reason, monkeypatch):
    w = world
    ready(w, "none" if condition == "policy" else "dead")
    caller = w.b
    epoch = 0 if condition == "cas" else 1
    if condition == "transferring":
        begin(w)
    w.proc.rows[30]["state"] = "Z"
    if condition == "unknown_caller":
        caller = sessions.identify(w.cfg, pid=21, env={})
    elif condition == "unregistered_caller":
        (home() / "sessions" / (w.b["session_id"] + ".json")).unlink()
    elif condition == "dead_caller":
        w.proc.rows[40]["state"] = "Z"
    elif condition == "station":
        caller = sessions.identify(w.cfg, pid=41, env={"YARDGOAT_STATION": "fixture"})
    elif condition == "no_controller":
        with projects.locked("demo") as state:
            state["controller"]["session_id"] = None
    elif condition == "alive_controller":
        w.proc.rows[30]["state"] = "S"
    elif condition == "missing_controller_record":
        (home() / "sessions" / (w.a["session_id"] + ".json")).unlink()
    elif condition == "hidden_controller":
        del w.proc.rows[30]
        w.proc.confirm_absence = False
    elif condition == "unknown_controller":
        original = w.proc.stat
        def denied(pid):
            if pid == 30:
                raise PermissionError("fixture proc is unreadable")
            return original(pid)
        monkeypatch.setattr(w.proc, "stat", denied)
    before = projects.path("demo").read_bytes()
    with pytest.raises(sessions.Refused, match=reason):
        projects.reclaim("demo", caller, epoch)
    assert projects.path("demo").read_bytes() == before


def test_reclaim_configuration_supports_legacy_records_and_views(world, invoke):
    w = world
    ready(w)
    with projects.locked("demo") as state:
        state.pop("reclaim")
    w.proc.rows[30]["state"] = "Z"
    with pytest.raises(sessions.Refused, match="policy is none"):
        projects.reclaim("demo", w.b, 1)
    w.cfg["reclaim_dead"] = ["demo"]
    Path(os.environ["YARDGOAT_CONFIG"]).write_text(json.dumps(w.cfg))
    before = projects.path("demo").read_bytes()
    for state in (invoke("project", "show", "demo"), invoke("project", "list")[0]):
        assert state["reclaim"] == "none" and state["reclaim_effective"] == "dead"
    assert projects.path("demo").read_bytes() == before
    w.pid = 41
    state = invoke("project", "reclaim", "demo", "--expect-epoch", 1)
    assert state["controller"]["epoch"] == 2


def test_new_cli_keeps_legacy_begin_commit_abort_claim(world, invoke):
    w = world
    invoke("session", "register", "--bind-supervisor")
    w.pid = 41
    invoke("session", "register")
    w.pid = 31
    invoke("project", "add", "demo", "--reclaim", "dead")
    invoke("project", "claim", "demo", "--expect-epoch", 0)
    start = invoke("project", "transfer", "begin", "demo", "--expect-epoch", 1)
    assert "token" in start
    invoke("project", "transfer", "abort", "demo", "--transfer", start["transfer_id"])
    start = invoke("project", "transfer", "begin", "demo", "--expect-epoch", 1)
    w.pid = 41
    result = invoke("project", "transfer", "commit", "demo", "--transfer", start["transfer_id"], "--token", start["token"])
    assert result["result"] == "committed" and result["controller"]["epoch"] == 2


# A detached fixture owns these processes and private files. It never launches a
# model, accesses the real store, or weakens the production station boundary.
SUPERVISOR = r'''
import json, os, pathlib, subprocess, sys, time
if sys.argv[1] == "launch":
    if os.fork():
        sys.exit(0)
    os.setsid()
    with open(os.devnull, "r+b", buffering=0) as f:
        for n in (0, 1, 2): os.dup2(f.fileno(), n)
    os.execv(sys.executable, [sys.executable, __file__, "serve", sys.argv[2]])
root = pathlib.Path(sys.argv[2])
children, terminals = {}, []
try:
    for name in ("a", "b"):
        master, slave = os.openpty()
        terminals.append(master)
        children[name] = subprocess.Popen([str(root / "fixture-agent"), str(root / "agent.sh"), str(root / name)],
                                          stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
    (root / "ready").write_text(str(os.getpid()))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not (root / "stop").exists():
        for request in root.glob("*.request"):
            data = json.loads(request.read_text())
            if "stop_agent" in data:
                child = children[data["stop_agent"]]
                child.terminate()
                child.wait(timeout=3)
                result = {"rc": 0, "out": "", "err": ""}
            else:
                p = subprocess.run([sys.executable, "-m", "yardgoat.cli", *data["args"]],
                                   capture_output=True, text=True, timeout=10)
                result = {"rc": p.returncode, "out": p.stdout, "err": p.stderr}
            temp = request.with_suffix(".tmp")
            temp.write_text(json.dumps(result))
            temp.replace(request.with_suffix(".response"))
            request.unlink()
        time.sleep(0.01)
finally:
    for child in children.values():
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=3)
    for fd in terminals: os.close(fd)
    (root / "stopped").touch()
'''

AGENT = r'''
root=$1
for ((i=0; i<6000; i++)); do
    for cmd in "$root"/*.command; do
        [[ -f "$cmd" ]] || continue
        bash "$cmd" > "$cmd.out" 2> "$cmd.err"
        printf '%s' "$?" > "$cmd.rc.tmp"
        mv "$cmd.rc.tmp" "$cmd.rc"
        rm "$cmd"
    done
    sleep 0.01
done
'''


@pytest.fixture
def real_tree(env, tmp_path):
    root = tmp_path / "processes"
    root.mkdir()
    for name in ("a", "b"):
        (root / name).mkdir()
    executable = root / "fixture-agent"
    shutil.copyfile(shutil.which("bash"), executable)
    executable.chmod(0o700)
    (root / "agent.sh").write_text(AGENT)
    script = root / "supervisor.py"
    script.write_text(SUPERVISOR)
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps({"sessions": {"agents": [
        {"name": "fixture", "exe": "fixture-agent"}]}}))

    def wait(path):
        end = time.monotonic() + 12
        while not path.exists() and time.monotonic() < end:
            time.sleep(0.01)
        assert path.exists(), f"fixture timed out: {path.name}"

    def run(who, *args, rc=0, stop_agent=None):
        if who == "supervisor":
            base = root / uuid.uuid4().hex
            request = {"stop_agent": stop_agent} if stop_agent else {"args": list(map(str, args))}
            base.with_suffix(".tmp").write_text(json.dumps(request))
            base.with_suffix(".tmp").replace(base.with_suffix(".request"))
            wait(base.with_suffix(".response"))
            result = json.loads(base.with_suffix(".response").read_text())
        else:
            command = root / who / (uuid.uuid4().hex + ".command")
            command.with_suffix(".tmp").write_text(shlex.join([sys.executable, "-m", "yardgoat.cli", *map(str, args)]))
            command.with_suffix(".tmp").replace(command)
            wait(Path(str(command) + ".rc"))
            result = {"rc": int(Path(str(command) + ".rc").read_text()),
                      "out": Path(str(command) + ".out").read_text(), "err": Path(str(command) + ".err").read_text()}
        assert result["rc"] == rc, result
        return json.loads(result["out"]) if result["out"].strip() else result["err"]

    subprocess.run([sys.executable, str(script), "launch", str(root)], env=env, check=True, timeout=5)
    try:
        wait(root / "ready")
        yield run
    finally:
        (root / "stop").touch()
        wait(root / "stopped")


def test_real_supervisor_file_handoff_and_dead_controller_reclaim(real_tree, tmp_path):
    run = real_tree
    a = run("a", "session", "register", "--bind-supervisor")
    assert a == run("a", "session", "register", "--bind-supervisor")
    b = run("b", "session", "register", "--bind-supervisor")
    assert a["supervisor"] == b["supervisor"]
    assert run("supervisor", "whoami", rc=3)["status"] == "unknown"
    for project in ("demo", "recover"):
        run("a", "project", "add", project, "--reclaim", "dead")
        run("a", "project", "claim", project, "--expect-epoch", 0)
    token = tmp_path / "secret"
    token.write_text(SECRET)
    token.chmod(0o600)
    args = ("project", "transfer", "begin", "demo", "--expect-epoch", 1,
            "--transfer", TRANSFER, "--token-file", token, "--for-session", a["session_id"])
    first = run("supervisor", *args)
    assert first == run("supervisor", *args) and "token" not in first
    run("b", "project", "reclaim", "recover", "--expect-epoch", 1, rc=3)
    run("supervisor", stop_agent="a")
    run("supervisor", *args, rc=3)
    committed = run("b", "project", "transfer", "commit", "demo", "--transfer", TRANSFER, "--token-file", token)
    assert committed["controller"]["epoch"] == 2
    assert committed["controller"]["session_id"] == b["session_id"]
    reclaimed = run("b", "project", "reclaim", "recover", "--expect-epoch", 1)
    assert reclaimed["controller"]["how"] == "reclaim" and reclaimed["controller"]["epoch"] == 2
    assert reclaimed["history"][-1]["dead_reason"] == "absence_confirmed"


def test_real_supervisor_abort_and_agent_delegation_refusal(real_tree):
    run = real_tree
    a = run("a", "session", "register", "--bind-supervisor")
    run("a", "project", "add", "demo")
    run("a", "project", "claim", "demo", "--expect-epoch", 0)
    start = run("supervisor", "project", "transfer", "begin", "demo", "--expect-epoch", 1,
                "--for-session", a["session_id"])
    result = run("supervisor", "project", "transfer", "abort", "demo", "--transfer", start["transfer_id"],
                 "--for-session", a["session_id"])
    assert result["result"] == "aborted" and result["controller"]["epoch"] == 1
    # An agent may not invoke the delegated route through its own parent.
    run("a", "project", "transfer", "begin", "demo", "--expect-epoch", 1,
        "--for-session", a["session_id"], rc=3)
