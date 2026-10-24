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
            if argv[i + 1] == "_station":
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
    Inaccessible or changing ancestry fails closed; no fallback to an outer agent.
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
        # Inspect the invoker, not Yardgoat's own interpreter (which may share
        # an executable with a configured agent).
        current = int(os.getppid() if pid is None else pid)
        if current <= 1:
            raise Refused("PID must identify a running agent, not the system root")
        seen, chain, candidate, agent = set(), [], None, None
        while current != 1:
            if current <= 0 or current in seen or len(seen) >= 1024:
                raise Refused("broken or cyclic parent process chain")
            seen.add(current)
            process = proc.process(current)
            chain.append(process)
            if process["state"] in ("Z", "X", "x"):
                raise Refused(f"process {current} is not running")
            if station_marker(process):
                raise Refused(f"station process in ancestry: pid {current}")
            if candidate is None:
                agent = matches(process, agents)
                if agent:
                    candidate = process
                elif exact:
                    raise Refused(f"pid {current} does not match an agent rule")
            current = process["ppid"]
        # Check that the chain did not disappear, reparent or reuse a PID as we walked.
        proc.stat(1)
        for process in chain:
            # Scheduling state can change between R and S without changing identity.
            fresh = proc.stat(process["pid"])
            if any(fresh[k] != process[k] for k in ("pid", "comm", "ppid", "start_ticks")) or fresh["state"] in ("Z", "X", "x"):
                raise Refused("parent process chain changed during inspection")
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
    proc = proc or ProcFS()
    try:
        identity = rec["identity"]
        if proc.boot_id() != identity["boot_id"]:
            return "dead"
        try:
            stat = proc.stat(identity["pid"])
        except FileNotFoundError:
            # An absent proc mount is not evidence of death.
            proc.stat(1)
            return "dead"
        if str(stat["start_ticks"]) != str(identity["start_ticks"]) or stat["state"] in ("Z", "X", "x"):
            return "dead"
        return "alive"
    except (OSError, ValueError, KeyError, IndexError):
        return "unknown"


def register(caller, label=None, *, administrator=None, reason=None):
    sid = require(caller)
    if liveness(caller) != "alive":
        raise Refused("agent is no longer confirmed alive")
    path = home() / "sessions" / f"{sid}.json"
    with locked_json(path) as box:
        rec = box["state"]
        if rec is None:
            rec = {k: caller[k] for k in ("session_id", "identity", "evidence", "endpoint")}
            rec.update(label=label or "", registered_at=now(), registrations=[])
            box["state"] = rec
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
