"""Probe the real launch chain with a version command, never a model request."""
from __future__ import annotations

import os
import math
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import quote

from . import pool, runner
from .store import home, now, read_json, write_json

PROBE_TIMEOUT = 20


def record_path(name: str, kind: str = "pool") -> Path:
    return home() / "doctor" / kind / (quote(name, safe="") + ".json")


def valid_report(report) -> bool:
    if not isinstance(report, dict) or type(report.get("ok")) is not bool:
        return False
    if "pool" not in report or (report["pool"] is not None and not isinstance(report["pool"], str)):
        return False
    if report.get("host") not in ("codex", "claude", "kimi", "shell"):
        return False
    if not all(isinstance(report.get(k), str) for k in ("lane", "account_alias", "checked_at", "reason")):
        return False
    epoch = report.get("checked_epoch")
    if type(epoch) not in (int, float) or not math.isfinite(epoch) or epoch <= 0:
        return False
    try:
        timestamp = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", report["checked_at"].replace("Z", "+00:00"))
        datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    checks = report.get("checks")
    if not isinstance(checks, list) or not all(isinstance(c, dict) and isinstance(c.get("name"), str)
            and type(c.get("ok")) is bool and isinstance(c.get("reason"), str) for c in checks):
        return False
    required = {"startup", "host_path", "host_version", "launcher"} | ({"mailbox"} if report["pool"] else set())
    names = [c["name"] for c in checks]
    return required <= set(names) and len(names) == len(set(names)) and report["ok"] == all(c["ok"] for c in checks)


def failure_message(name: str, report: dict | None) -> str | None:
    if valid_report(report) and report["pool"] == name and not report["ok"]:
        return f"池子 {name} 体检失败：{report.get('reason', 'unknown error')}"
    return None


def pool_report(name: str) -> dict | None:
    try:
        report = read_json(record_path(name))
        return report if valid_report(report) and report["pool"] == name else None
    except (OSError, ValueError):
        return None


def _report(request: dict, checks: list[dict]) -> dict:
    failed = [c["name"] + ": " + c["reason"] for c in checks if not c["ok"]]
    return {"pool": request.get("pool"), "host": request["executor"]["host"],
            "lane": request["executor"].get("lane", ""), "account_alias": request["executor"].get("acct", ""),
            "checked_at": now(), "checked_epoch": time.time(), "ok": not failed,
            "env": runner.launch_env_fingerprint(runner.config()),
            "checks": checks, "reason": "; ".join(failed)}


def _failed(request: dict, reason: str) -> dict:
    names = ["startup", "host_path", "host_version", "launcher"]
    if request.get("pool"):
        names.append("mailbox")
    return _report(request, [{"name": n, "ok": False, "reason": reason} for n in names])


def run(cfg: dict, name: str | None = None, host: str = "codex", lane: str = "", acct: str | None = None) -> dict:
    if name:
        ex = dict(pool.pool_conf(cfg, name))
        ex["acct"] = acct if acct is not None else pool.next_station(name, ex)[1]
        workdir = os.path.expanduser(ex.get("root", str(pool.pool_dir(name) / "stations")))
    else:
        ex = {"host": host, "lane": lane, "acct": acct or "", "command": "true"}
        workdir = str(Path.cwd())
    key = name or f"{host}-{lane}"
    d = home() / "doctor" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    rd = Path(tempfile.mkdtemp(prefix="probe-", dir=d))
    request = {"pool": name, "executor": ex, "workdir": workdir, "config": cfg}
    write_json(rd / "request.json", request)
    try:
        # Same detached/tmux wrapper, import path handling and pass_env as actual workers.
        launched = runner.launch_daemon([sys.executable, "-m", "yardgoat.cli", "_doctor", str(rd)],
                                        cfg, str(home()), "yg-doctor-" + rd.name)
        end = time.monotonic() + PROBE_TIMEOUT + 5
        while not (rd / "result.json").exists() and time.monotonic() < end:
            time.sleep(0.05)
        if (rd / "result.json").exists():
            report = read_json(rd / "result.json")
        else:
            # A broken daemon import/source must never leave the caller waiting forever.
            pidfile = rd / "worker.json"
            if pidfile.exists():
                runner.kill_group(read_json(pidfile)["pgid"], grace=0.5)
            if launched["mode"] == "process":
                runner.kill_group(launched["pid"], grace=0.5)
            report = _failed(request, "startup timed out before the probe returned")
    except (OSError, RuntimeError, ValueError) as e:
        report = _failed(request, str(e))
    path = record_path(key, "pool" if name else "direct")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, report)
    if name:
        write_json(pool.pool_dir(name) / "state.json", {"pool": name, "doctor": report,
                   "reason": failure_message(name, report)})
    return report


def ensure(name: str, cfg: dict, acct: str) -> dict:
    report = pool_report(name)
    ex = pool.pool_conf(cfg, name)
    try:
        fresh = report and report["ok"] and report["pool"] == name and \
            report["host"] == ex["host"] and report["lane"] == ex.get("lane", "") and report["account_alias"] == acct and \
            report.get("env") == runner.launch_env_fingerprint(cfg) and \
            0 <= time.time() - float(report["checked_epoch"]) < float(cfg.get("doctor_ttl_h", 12)) * 3600
    except (KeyError, TypeError, ValueError):
        fresh = False
    return report if fresh else run(cfg, name=name, acct=acct)


def _probe_script(rd: Path, host: str, mailbox: Path | None) -> str:
    command = "bash" if host == "shell" else host
    q = shlex.quote
    lines = ["set +e", f"command -v {q(command)} > {q(str(rd / 'host_path.out'))} 2>&1",
             f"printf '%s' \"$?\" > {q(str(rd / 'host_path.rc'))}",
             f"{q(command)} --version > {q(str(rd / 'host_version.out'))} 2>&1",
             f"printf '%s' \"$?\" > {q(str(rd / 'host_version.rc'))}"]
    if mailbox:
        probe = q(str(mailbox / (rd.name + ".probe")))
        lines += [f"( : > {probe} && rm -- {probe} ) > {q(str(rd / 'mailbox.out'))} 2>&1",
                  f"printf '%s' \"$?\" > {q(str(rd / 'mailbox.rc'))}"]
    # Collect failures without making the launcher appear broken when only --version failed.
    lines += [f"printf done > {q(str(rd / 'complete'))}", "exit 0"]
    return "\n".join(lines) + "\n"


def run_probe(rd: Path) -> int:
    """The daemon side of doctor, reached through the same outer launch as _run/_station."""
    request = read_json(rd / "request.json")
    cfg, ex, name = request["config"], request["executor"], request.get("pool")
    try:
        mb = pool.mailbox_dir(name, rd.name, ex) if name else None
        (rd / "probe.sh").write_text(_probe_script(rd, ex["host"], mb))
        probe_argv = ["bash", str(rd / "probe.sh")]
        if name:
            script = pool.station_script(name, rd.name, ex, cfg, mb, probe_argv)
            env = pool.station_env()
        else:
            spec = {"executor": ex, "workdir": request["workdir"]}
            script = runner.script_for(None, spec, rd, cfg, probe_argv)
            env = runner.worker_env()
        (rd / "script.sh").write_text(script)
        with (rd / "log").open("wb") as log:
            p = subprocess.Popen(["bash", str(rd / "script.sh")], stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, env=env, start_new_session=True)
            write_json(rd / "worker.json", {"pgid": p.pid})
            try:
                rc = p.wait(timeout=PROBE_TIMEOUT)
            except subprocess.TimeoutExpired:
                runner.kill_group(p.pid, grace=0.5)
                p.wait()
                rc = 124
        log = (rd / "log").read_text(errors="replace")[-4000:].strip()
        completed = (rd / "complete").exists()
        reason = log or f"exit {rc}; probe {'completed' if completed else 'was not reached'}"
        checks = [{"name": "startup", "ok": completed, "reason": "source and working directory ready" if completed else reason},
                  {"name": "launcher", "ok": rc == 0 and completed,
                   "reason": "exit 0" if rc == 0 and completed else reason}]
        for key in ["host_path", "host_version"] + (["mailbox"] if name else []):
            result = rd / f"{key}.rc"
            ok = result.exists() and result.read_text() == "0"
            detail = (rd / f"{key}.out").read_text(errors="replace").strip() if result.exists() else ""
            checks.append({"name": key, "ok": ok, "reason": detail or
                           ("file created and removed" if ok and key == "mailbox" else
                            f"{ex['host']}: {key} {'passed' if ok else 'failed or was not reached'}; {reason}")})
        report = _report(request, checks)
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        report = _failed(request, str(e))
    write_json(rd / "result.json", report)
    return 0 if report["ok"] else 1
