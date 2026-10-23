"""Turn an executor spec into a process, run it, and write the receipt.

Machine-local wiring (how to pick a model lane/account, which tmux session to use, which env
files to source) lives in a local config file, never in task specs:

  $YARDGOAT_CONFIG  (default ~/.config/yardgoat/config.json)
  {
    "launcher": ["my-lane-wrapper", "--host", "{host}", "--lane", "{lane}", "--acct", "{acct}", "--"],
    "source": ["~/.config/my-proxy-env.sh"],
    "tmux_session": "fleet",
    "host_args": {"claude": ["--permission-mode", "acceptEdits"]}
  }
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .store import TaskDir, now, read_json, sha256_file, write_json


def config() -> dict:
    p = Path(os.environ.get("YARDGOAT_CONFIG", Path.home() / ".config/yardgoat/config.json")).expanduser()
    return read_json(p) if p.exists() else {}


def build_argv(spec: dict, run_dir: Path, cfg: dict) -> list[str]:
    ex = spec["executor"]
    host = ex["host"]
    prompt = Path(spec["card"]).read_text(encoding="utf-8")
    extra = list(cfg.get("host_args", {}).get(host, [])) + list(ex.get("args", []))
    if host == "codex":
        argv = ["codex", "exec", "-s", ex.get("sandbox", "workspace-write"), "--skip-git-repo-check",
                "-C", spec["workdir"], "-o", str(run_dir / "last_message.md"), *extra, prompt]
    elif host == "claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", *extra, prompt]
    elif host == "kimi":  # experimental: flags verified only against `kimi --help`
        argv = ["kimi", "--prompt", prompt, "--output-format", "stream-json", *extra]
    else:  # shell
        argv = ["bash", "-c", ex["command"]]
    launcher = cfg.get("launcher") if host != "shell" else None
    if launcher:
        fill = {"host": host, "lane": ex.get("lane", ""), "acct": ex.get("acct", "")}
        argv = [a.format(**fill) for a in launcher] + argv
    return argv


def script_for(spec: dict, run_dir: Path, cfg: dict) -> str:
    lines = ["set -u"]
    for f in cfg.get("source", []):
        lines.append(f"source {shlex.quote(os.path.expanduser(f))}")
    lines.append(f"cd {shlex.quote(spec['workdir'])}")
    lines.append("exec " + shlex.join(build_argv(spec, run_dir, cfg)))
    return "\n".join(lines) + "\n"


def launch(td: TaskDir, run_id: str, cfg: dict) -> dict:
    """Start `yardgoat _run` detached: in a tmux window when configured, else as a background process."""
    me = [sys.executable, "-m", "yardgoat.cli", "_run", td.task_id, run_id]
    env_prefix = f"YARDGOAT_HOME={shlex.quote(str(td.path.parent.parent))} "
    if os.environ.get("YARDGOAT_CONFIG"):
        env_prefix += f"YARDGOAT_CONFIG={shlex.quote(os.environ['YARDGOAT_CONFIG'])} "
    pypath = os.pathsep.join(p for p in sys.path if p)
    env_prefix += f"PYTHONPATH={shlex.quote(pypath)} "
    session = cfg.get("tmux_session")
    if session and shutil.which("tmux") and not os.environ.get("YARDGOAT_NO_TMUX"):
        window = f"yg-{td.task_id[:24]}-{run_id}"
        cmd = env_prefix + shlex.join(me) + "; echo '[yardgoat] run finished; window closes in 180s'; sleep 180"
        r = subprocess.run(["tmux", "new-window", "-d", "-P", "-F", "#{pane_pid}", "-t", session, "-n", window, cmd],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("tmux new-window failed: " + r.stderr.strip())
        return {"mode": "tmux", "window": f"{session}:{window}", "pid": int(r.stdout.strip())}
    env = dict(os.environ, YARDGOAT_HOME=str(td.path.parent.parent), PYTHONPATH=pypath)
    p = subprocess.Popen(me, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    return {"mode": "process", "window": None, "pid": p.pid}


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    st = Path(f"/proc/{pid}/stat")
    return not (st.exists() and st.read_text().split(") ")[-1].startswith("Z"))


def detect_model(log: Path) -> tuple[str, str]:
    if not log.exists():
        return "", "none"
    with open(log, errors="replace") as f:
        for i, line in enumerate(f):
            if i > 400:
                break
            if line.startswith("model:"):
                return line.split(":", 1)[1].strip(), "codex-banner"
            if line.startswith("{") and '"init"' in line:
                try:
                    return json.loads(line).get("model", ""), "stream-json-init"
                except json.JSONDecodeError:
                    pass
    return "", "none"


def detect_cost(log: Path) -> str:
    if not log.exists():
        return "unknown"
    tail = log.read_text(errors="replace")[-20000:]
    m = re.findall(r'"total_cost_usd":\s*([0-9.]+)', tail)
    if m:
        return f"usd={m[-1]}"
    m = re.findall(r"tokens used\s*\n?\s*([0-9,]+)", tail)
    if m:
        return f"tokens={m[-1].replace(',', '')}"
    return "unknown"


def run_and_receipt(td: TaskDir, run_id: str) -> int:
    """Body of `yardgoat _run`: execute, then write the receipt. Never decides verification."""
    spec, cfg = td.spec(), config()
    rd = td.run_dir(run_id)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "script.sh").write_text(script_for(spec, rd, cfg), encoding="utf-8")
    with td.locked() as box:
        run = next(r for r in box["state"]["runs"] if r["run_id"] == run_id)
        run["worker_pid"] = os.getpid()
    td.event("run.started", "runner", run_id=run_id)
    timeout = int(spec["budget"]["max_wall_min"]) * 60
    with open(rd / "log", "wb") as log:
        try:
            rc = subprocess.run(["bash", str(rd / "script.sh")], stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    model, model_src = detect_model(rd / "log")
    ex = spec["executor"]
    artifacts = {}
    for o in spec["outputs"]:
        p = Path(spec["workdir"]) / o
        artifacts[o] = sha256_file(p) if p.is_file() else None
    receipt = {
        "task_id": td.task_id, "run_id": run_id, "project": spec["project"],
        "attempt": int(run_id.lstrip("r")), "exit_code": rc, "timed_out": rc == 124,
        "host": ex["host"], "lane": ex.get("lane", ""), "account_alias": ex.get("acct", ""),
        "actual_model": model, "model_source": model_src, "cost_basis": detect_cost(rd / "log"),
        "artifacts": artifacts, "finished_at": now(),
        "status": "exited",  # verification is a separate step (`yardgoat close`)
    }
    write_json(rd / "receipt.json", receipt)
    with td.locked() as box:
        st = box["state"]
        run = next(r for r in st["runs"] if r["run_id"] == run_id)
        run.update(state="exited", exit_code=rc, finished_at=receipt["finished_at"])
        st["status"] = "exited"
    td.event("run.exited", "runner", run_id=run_id, exit_code=rc)
    return rc
