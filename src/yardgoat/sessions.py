"""Local Linux agent identities. A label or terminal endpoint never grants authority."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .store import home, now, read_json
from .statefile import locked_json

STATION_KEYS = ("YARDGOAT_STATION", "YARDGOAT_MAILBOX", "YARDGOAT_MAILBOX_HOME")
DEFAULT_AGENTS = [{"name": name, "exe": name, "comm": name, "argv0": name}
                  for name in ("claude", "codex", "kimi")]


class Refused(ValueError):
    """A policy refusal, mapped to exit 3 by the CLI."""


def parse_stat(text: str) -> dict:
    # comm can contain spaces AND closing parentheses. Fields start after the LAST ).
    left, right = text.index("("), text.rindex(")")
    fields = text[right + 1:].split()
    return {"pid": int(text[:left].strip()), "comm": text[left + 1:right],
            "state": fields[0], "ppid": int(fields[1]), "start_ticks": fields[19]}


class ProcFS:
    """Inject a proc root in unit tests; no environment switch can bypass identity checks."""
    def __init__(self, root=Path("/proc")):
        self.root = Path(root)

    def boot_id(self):
        value = (self.root / "sys/kernel/random/boot_id").read_text().strip()
        if not value:
            raise ValueError("empty boot_id")
        return value

    def stat(self, pid):
        return parse_stat((self.root / str(pid) / "stat").read_text())

    def uid(self, pid):
        # stat/status are often readable across users, but hidepid can restrict both.
        # Compare real UIDs, matching os.getuid(), not proc directory ownership.
        try:
            status = (self.root / str(pid) / "status").read_text()
        except OSError as exc:
            # Preserve the exception type: ENOENT may also mean a foreign ancestor
            # exited between the walk and recheck, which permits one fresh walk.
            raise type(exc)(f"cannot read uid for process {pid}: {exc}; /proc may be mounted with hidepid") from exc
        for line in status.splitlines():
            if line.startswith("Uid:"):
                values = [int(value) for value in line.split()[1:]]
                if len(values) == 4 and all(value >= 0 for value in values):
                    return values[0]
                break
        raise ValueError(f"missing or invalid Uid in process {pid} status")

    def absence_confirmed(self, pid):
        # hidepid can report ENOENT for a live process. A readable proc root alone
        # does not prove death; signal 0 checks existence without sending a signal.
        self.stat(1)
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        return False

    def process(self, pid):
        before = self.stat(pid)
        path = self.root / str(pid)
        argv = [s.decode(errors="replace") for s in (path / "cmdline").read_bytes().split(b"\0") if s]
        # Only retain endpoint and station keys. Never persist arbitrary environment/argv.
        env = {}
        for raw in (path / "environ").read_bytes().split(b"\0"):
            key, _, value = raw.partition(b"=")
            key = key.decode(errors="replace")
            if key in (*STATION_KEYS, "TMUX", "TMUX_PANE"):
                env[key] = value.decode(errors="replace")
        exe = Path(os.readlink(path / "exe")).name
        after = self.stat(pid)
        if any(after[k] != before[k] for k in ("pid", "ppid", "comm", "start_ticks")):
            raise ValueError(f"process {pid} changed during inspection")
        return {**before, "exe": exe, "argv0": Path(argv[0]).name if argv else "", "argv": argv, "env": env}


def session_id(identity):
    value = [identity["boot_id"], int(identity["pid"]), str(identity["start_ticks"])]
    return "s-" + hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def rules(cfg):
    settings = cfg.get("sessions", {})
    if not isinstance(settings, dict):
        raise ValueError("sessions must be an object")
    agents = settings.get("agents", DEFAULT_AGENTS)
    if not isinstance(agents, list):
        raise ValueError("sessions.agents must be a list")
    for rule in agents:
        if (not isinstance(rule, dict) or not isinstance(rule.get("name"), str) or not rule["name"].strip()
                or not any(k in rule for k in ("exe", "comm", "argv0"))):
            raise ValueError("each agent rule needs name and exe/comm/argv0")
        for key in ("exe", "comm", "argv0"):
            if key in rule and (not isinstance(rule[key], str) or not rule[key] or "/" in rule[key]):
                raise ValueError(f"agent {key} must be an exact process name/basename")
    return agents


def matches(process, agents):
    # Selectors within a rule are alternatives, never substring/glob matches.
    found = [r for r in agents if any(k in r and process[k] == r[k] for k in ("exe", "comm", "argv0"))]
    if len(found) > 1:
        raise Refused(f"ambiguous agent rules for pid {process['pid']}")
    return found[0]["name"] if found else None


def station_marker(process):
    if any(process["env"].get(k) for k in STATION_KEYS):
        return True
    argv = process["argv"]
    # Match the actual wrapper invocation, not incidental prompt text containing _station.
    for i, arg in enumerate(argv[:-1]):
        if (arg == "yardgoat.cli" and i > 0 and argv[i - 1] == "-m") or Path(arg).name == "yardgoat":
            remaining = argv[i + 1:]
            # Global caller assertions can precede the subcommand. Support the
            # same unambiguous option abbreviations accepted by argparse.
            while remaining:
                option, equal, _ = remaining[0].partition("=")
                if not option.startswith("--") or not "--session".startswith(option):
                    break
                remaining = remaining[1 if equal else 2:]
            if remaining and remaining[0] == "_station":
                return True
    return False


def pane_pid(socket, pane):
    result = subprocess.run(["tmux", "-S", socket, "display-message", "-p", "-t", pane, "#{pane_pid}"],
                            capture_output=True, text=True, timeout=2)
    if result.returncode:
        raise ValueError("tmux could not resolve pane")
    return int(result.stdout.strip())


def endpoint(env, ancestors, lookup=None):
    value = {"socket": env.get("TMUX", "").rsplit(",", 2)[0] or None,
             "pane_id": env.get("TMUX_PANE") or None, "verified": False}
    if not value["socket"] or not value["pane_id"]:
        value["reason"] = "tmux endpoint is absent"
        return value
    try:
        pid = (lookup or pane_pid)(value["socket"], value["pane_id"])
        value.update(pane_pid=pid, verified=pid in ancestors)
        value["reason"] = "pane process is an agent ancestor" if value["verified"] else "pane process is not an agent ancestor"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        value["reason"] = f"endpoint unverified: {exc}"
    return value


def identify(cfg, *, pid=None, explicit=None, proc=None, env=None, lookup=None, exact=False):
    """Find nearest agent, then finish walking for station markers. PID 1 is the root.

    exact=True is for administrator inventory: the supplied PID itself must match.
    Only same-user processes can be agents or stations. Foreign ancestors need
    stat/status only when readable; hidepid may restrict even these. Inaccessible
    same-user or changing identity fails closed.
    """
    proc = proc or ProcFS()
    env = dict(os.environ if env is None else env)
    unknown = {"session_id": None, "status": "unknown", "reason": None,
               "endpoint": endpoint({}, [], lookup)}
    try:
        if any(env.get(k) for k in STATION_KEYS):
            raise Refused("station environment cannot identify/register a session")
        agents = rules(cfg)
        boot = proc.boot_id()
        caller_uid = os.getuid()
        # Inspect the invoker, not Yardgoat's own interpreter (which may share
        # an executable with a configured agent).
        current = int(os.getppid() if pid is None else pid)
        if current <= 1:
            raise Refused("PID must identify a running agent, not the system root")
        start = current
        previous = None
        for attempt in range(2):
            current = start
            seen, chain, candidate, agent = set(), [], None, None
            while current != 1:
                if current <= 0 or current in seen or len(seen) >= 1024:
                    raise Refused("broken or cyclic parent process chain")
                seen.add(current)
                uid = proc.uid(current)
                same_user = uid == caller_uid
                process = proc.process(current) if same_user else proc.stat(current)
                chain.append({**process, "uid": uid})
                if process["state"] in ("Z", "X", "x"):
                    raise Refused(f"process {current} is not running")
                if same_user and station_marker(process):
                    raise Refused(f"station process in ancestry: pid {current}")
                if candidate is None:
                    agent = matches(process, agents) if same_user else None
                    if agent:
                        candidate = process
                    elif exact:
                        if not same_user:
                            raise Refused(f"pid {current} belongs to a different user")
                        raise Refused(f"pid {current} does not match an agent rule")
                current = process["ppid"]
            proc.stat(1)
            # Check foreign entries first. A same-user child may already have
            # reparented when its foreign parent vanishes; inspect the new chain
            # once rather than rejecting evidence that carried no authority.
            vanished = False
            for process in sorted(chain, key=lambda row: row["uid"] == caller_uid):
                try:
                    fresh = proc.stat(process["pid"])
                    fresh_uid = proc.uid(process["pid"])
                except FileNotFoundError:
                    if process["uid"] != caller_uid and attempt == 0:
                        vanished = True
                        continue
                    raise
                if (any(fresh[k] != process[k] for k in ("pid", "comm", "start_ticks"))
                        or fresh["state"] in ("Z", "X", "x") or fresh_uid != process["uid"]):
                    raise Refused("parent process chain changed during inspection")
                if not vanished and fresh["ppid"] != process["ppid"]:
                    raise Refused("parent process chain changed during inspection")
            if previous is not None:
                # A retry cannot substitute another nearest agent or reused PID.
                if candidate is None or any(candidate[k] != previous[k] for k in ("pid", "comm", "start_ticks")):
                    raise Refused("agent changed during inspection")
            if not vanished:
                break
            previous = candidate
        if proc.boot_id() != boot:
            raise Refused("boot changed during inspection")
        if candidate is None:
            raise Refused("no configured agent found in parent process chain")
        identity = {"boot_id": boot, "pid": candidate["pid"], "start_ticks": candidate["start_ticks"]}
        sid = session_id(identity)
        if explicit is not None and explicit != sid:
            raise Refused("--session does not match the mechanically identified caller")
        index = next(i for i, p in enumerate(chain) if p["pid"] == candidate["pid"])
        ancestors = [p["pid"] for p in chain[index:]] + [1]
        return {"session_id": sid, "status": "identified", "reason": None, "identity": identity,
                "evidence": {"agent": agent, "exe": candidate["exe"], "comm": candidate["comm"],
                             "argv0": candidate["argv0"], **identity},
                "endpoint": endpoint(candidate["env"] if exact else env, ancestors, lookup)}
    except (OSError, ValueError, IndexError, KeyError) as exc:
        return {**unknown, "reason": str(exc) or type(exc).__name__}


def require(caller):
    if not isinstance(caller, dict) or caller.get("status") != "identified" or not caller.get("session_id"):
        raise Refused((caller.get("reason") if isinstance(caller, dict) else None) or "caller is not identified")
    try:
        derived = session_id(caller["identity"])
    except (ValueError, KeyError, TypeError):
        raise Refused("caller is missing mechanical identity evidence") from None
    if derived != caller["session_id"]:
        raise Refused("inconsistent caller identity")
    return caller["session_id"]


def record(sid):
    if not isinstance(sid, str) or not re.fullmatch(r"s-[0-9a-f]{64}", sid):
        return None
    try:
        return read_json(home() / "sessions" / f"{sid}.json")
    except FileNotFoundError:
        return None


def liveness(rec, proc=None):
    """Keep the original string API; recovery uses the same evidence with its reason."""
    return liveness_with_reason(rec, proc)[0]


def liveness_with_reason(rec, proc=None):
    proc = proc or ProcFS()
    try:
        identity = rec["identity"]
        if proc.boot_id() != identity["boot_id"]:
            return "dead", "boot_changed"
        try:
            stat = proc.stat(identity["pid"])
        except FileNotFoundError:
            return ("dead", "absence_confirmed") if proc.absence_confirmed(identity["pid"]) else ("unknown", None)
        if str(stat["start_ticks"]) != str(identity["start_ticks"]):
            return "dead", "start_ticks_mismatch"
        if stat["state"] in ("Z", "X", "x"):
            return "dead", "zombie"
        return "alive", None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return "unknown", None


def _check_process(proc, before, uid):
    fresh = proc.stat(before["pid"])
    if (any(fresh[k] != before[k] for k in ("pid", "ppid", "comm", "start_ticks"))
            or fresh["state"] in ("Z", "X", "x") or proc.uid(before["pid"]) != uid):
        raise Refused("process identity or parent changed during supervisor inspection")


def _agent_stat(proc, rec, supervisor_pid=None):
    identity = rec["identity"]
    current = proc.stat(identity["pid"])
    if (proc.boot_id() != identity["boot_id"] or current["pid"] != identity["pid"]
            or str(current["start_ticks"]) != str(identity["start_ticks"])
            or current["state"] in ("Z", "X", "x") or proc.uid(identity["pid"]) != os.getuid()):
        raise Refused("session agent identity changed or is no longer alive with the same uid")
    if supervisor_pid is not None and current["ppid"] != supervisor_pid:
        raise Refused("session agent no longer has its bound supervisor as direct parent")
    return current


def supervisor_identity(caller, cfg, proc=None):
    """Capture only the live agent's direct parent, never an outer launcher."""
    proc = proc or ProcFS()
    try:
        agent = _agent_stat(proc, caller)
        parent_pid = agent["ppid"]
        if parent_pid <= 1:
            raise Refused("agent has no bindable supervisor before the system root")
        uid = proc.uid(parent_pid)
        if uid != os.getuid():
            raise Refused("supervisor belongs to a different uid")
        parent = proc.process(parent_pid)
        if parent["state"] in ("Z", "X", "x"):
            raise Refused("supervisor is not alive")
        if station_marker(parent):
            raise Refused("station process cannot be a supervisor")
        if matches(parent, rules(cfg)):
            raise Refused("agent process cannot be a supervisor")
        _check_process(proc, parent, uid)
        _check_process(proc, agent, os.getuid())
        _agent_stat(proc, caller, parent_pid)
        return {"boot_id": caller["identity"]["boot_id"], "pid": parent_pid,
                "start_ticks": str(parent["start_ticks"]), "uid": uid}
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise Refused(f"cannot bind supervisor: {exc}") from None


def supervised_session(cfg, sid, *, pid=None, proc=None, env=None):
    """Authorize only a transfer begin/tokenless abort, without identifying an agent.

    Walk from the CLI's parent through PID 1, retaining the station boundary even
    above the bound supervisor. An agent below the supervisor cannot delegate
    its authority back out of its own subtree. All evidence is checked again.
    This returns supervisor evidence, deliberately not an identify() caller object.
    """
    proc = proc or ProcFS()
    env = os.environ if env is None else env
    try:
        if any(env.get(k) for k in STATION_KEYS):
            raise Refused("station environment cannot act as a supervisor")
        rec = record(sid)
        if rec is None:
            raise Refused("supervised session is not registered")
        if liveness(rec, proc) != "alive":
            raise Refused("supervised session is not confirmed alive")
        supervisor = rec.get("supervisor")
        if not supervisor:
            raise Refused("session has no bound supervisor")
        boot, uid = proc.boot_id(), os.getuid()
        if supervisor["boot_id"] != boot or supervisor["uid"] != uid:
            raise Refused("supervisor boot identity or uid does not match")
        agent = _agent_stat(proc, rec, supervisor["pid"])
        agents = rules(cfg)
        current = int(os.getppid() if pid is None else pid)
        seen, chain, found = set(), [], False
        while current != 1:
            if current <= 0 or current in seen or len(seen) >= 1024:
                raise Refused("broken or cyclic supervisor process chain")
            seen.add(current)
            owner = proc.uid(current)
            process = proc.process(current) if owner == uid else proc.stat(current)
            chain.append((process, owner))
            if process["state"] in ("Z", "X", "x"):
                raise Refused("supervisor process chain is not alive")
            if owner == uid and station_marker(process):
                raise Refused(f"station process in supervisor ancestry: pid {current}")
            if not found and owner == uid and matches(process, agents):
                raise Refused("agent process between caller and bound supervisor")
            if current == supervisor["pid"]:
                if owner != uid or str(process["start_ticks"]) != str(supervisor["start_ticks"]):
                    raise Refused("bound supervisor identity changed (uid or start_ticks)")
                found = True
            current = process["ppid"]
        proc.stat(1)
        if not found:
            raise Refused("bound direct supervisor is not in caller ancestry")
        for process, owner in chain:
            _check_process(proc, process, owner)
        _check_process(proc, agent, uid)
        _agent_stat(proc, rec, supervisor["pid"])
        if proc.boot_id() != boot:
            raise Refused("boot changed during supervisor inspection")
        return dict(supervisor)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise Refused(f"supervisor refused: {exc}") from None


def register(caller, label=None, *, administrator=None, reason=None,
             bind_supervisor=False, cfg=None, proc=None):
    sid = require(caller)
    if liveness(caller, proc) != "alive":
        raise Refused("agent is no longer confirmed alive")
    path = home() / "sessions" / f"{sid}.json"
    with locked_json(path) as box:
        rec = box["state"]
        supervisor = supervisor_identity(caller, cfg or {}, proc) if bind_supervisor else None
        if rec and supervisor and rec.get("supervisor") not in (None, supervisor):
            raise Refused("session is already bound to a different supervisor; rebinding is forbidden")
        if rec is None:
            rec = {k: caller[k] for k in ("session_id", "identity", "evidence", "endpoint")}
            rec.update(label=label or "", registered_at=now(), registrations=[])
            box["state"] = rec
        if supervisor:
            rec["supervisor"] = supervisor
        if label is not None:
            rec["label"] = label
        rec["endpoint"] = caller["endpoint"]
        audit = {"by": administrator or sid, "how": "administrator" if administrator else "self",
                 "reason": reason or "", "evidence": caller["evidence"]}
        if not any(all(row.get(k) == v for k, v in audit.items()) for row in rec["registrations"]):
            rec["registrations"].append({"at": now(), **audit})
    return rec


def require_registered(caller):
    sid = require(caller)
    rec = record(sid)
    if rec is None:
        raise Refused("caller session is not registered")
    if rec["identity"] != caller["identity"] or liveness(rec) != "alive":
        raise Refused("caller session is not confirmed alive")
    return sid


def require_target(sid):
    rec = record(sid)
    if rec is None:
        raise Refused("target session is not registered")
    state = liveness(rec)
    if state != "alive":
        raise Refused(f"target session is {state}, not confirmed alive")
    return sid


def list_sessions():
    return [{**read_json(p), "liveness": liveness(read_json(p))}
            for p in sorted((home() / "sessions").glob("s-*.json"))]


def require_admin(cfg, who, reason):
    admins = cfg.get("project_admins")
    if not isinstance(admins, list) or not all(isinstance(a, str) and a.strip() for a in admins) or who not in admins:
        raise Refused("operator is not in configured project_admins")
    if not isinstance(reason, str) or not reason.strip():
        raise Refused("administrator operation requires a nonempty reason")
