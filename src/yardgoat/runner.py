"""Run the agent CLI directly (adapter "direct"), and write receipts for any adapter.

Machine-local wiring (how to pick a model lane/account, which tmux session to use, which env
files to source) lives in a local config file, never in task specs:

  $YARDGOAT_CONFIG  (default ~/.config/yardgoat/config.json)
  {
    "launcher": ["my-lane-wrapper", "--host", "{host}", "--lane", "{lane}", "--acct", "{acct}", "--"],
    "source": ["~/.config/my-proxy-env.sh"],
    "tmux_session": "fleet",
    "host_args": {"claude": ["--permission-mode", "acceptEdits"]},
    "forbidden_lanes": ["personal-subscription"]
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
import time
from pathlib import Path

from .store import TaskDir, now, read_json, sha256_file, write_json


def config() -> dict:
    p = Path(os.environ.get("YARDGOAT_CONFIG", Path.home() / ".config/yardgoat/config.json")).expanduser()
    return read_json(p) if p.exists() else {}


def prompt_text(td: TaskDir, spec: dict) -> str:
    """The frozen card, plus every human answer given while the task was parked (in order)."""
    text = Path(spec["card"]).read_text(encoding="utf-8")
    answers = sorted((td.path / "answers").glob("*.md")) if (td.path / "answers").exists() else []
    if answers:
        text += "\n\n## Answers from a human (given while this task was parked)\n"
        for a in answers:
            text += f"\n### {a.stem}\n{a.read_text(encoding='utf-8').strip()}\n"
    return text


def build_argv(td: TaskDir, spec: dict, run_dir: Path, cfg: dict) -> list[str]:
    ex = spec["executor"]
    host = ex["host"]
    prompt = prompt_text(td, spec)
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


LAUNCH_FAIL = "yardgoat-launch-failed:"


def script_for(td: TaskDir, spec: dict, run_dir: Path, cfg: dict) -> str:
    lines = ["set -u"]
    for f in cfg.get("source", []):
        q = shlex.quote(os.path.expanduser(f))
        lines.append(f"source {q} || {{ echo {shlex.quote(LAUNCH_FAIL + ' source ' + f)}; exit 3; }}")
    w = shlex.quote(spec["workdir"])
    lines.append(f"cd {w} || {{ echo {shlex.quote(LAUNCH_FAIL + ' cd ' + spec['workdir'])}; exit 3; }}")
    lines.append("exec " + shlex.join(build_argv(td, spec, run_dir, cfg)))
    return "\n".join(lines) + "\n"


def launch(td: TaskDir, run_id: str, cfg: dict) -> dict:
    """Start `yardgoat _run` detached: in a tmux window when configured, else as a background process."""
    me = [sys.executable, "-m", "yardgoat.cli", "_run", td.task_id, run_id]
    # Only the directory that holds the yardgoat package — never our whole sys.path. Leaking sys.path
    # (stdlib of *this* interpreter) into the worker breaks every other Python the worker starts
    # (2026-10-23: a 3.13 sys.path made the host's 3.10 helpers crash before the agent started).
    pypath = str(Path(__file__).resolve().parents[1])
    orig = os.environ.get("PYTHONPATH", "")
    env_prefix = (f"YARDGOAT_HOME={shlex.quote(str(td.path.parent.parent))} PYTHONPATH={shlex.quote(pypath)} "
                  f"YARDGOAT_ORIG_PYTHONPATH={shlex.quote(orig)} ")
    if os.environ.get("YARDGOAT_CONFIG"):
        env_prefix += f"YARDGOAT_CONFIG={shlex.quote(os.environ['YARDGOAT_CONFIG'])} "
    # A tmux window inherits the tmux *server's* environment, not the caller's. Workers are configured by the local
    # config (source + launcher), deliberately not by whoever dispatched; pass_env is an explicit allowlist.
    for k in cfg.get("pass_env", []):
        if k in os.environ:
            env_prefix += f"{k}={shlex.quote(os.environ[k])} "
    session = cfg.get("tmux_session")
    if session and shutil.which("tmux") and not os.environ.get("YARDGOAT_NO_TMUX"):
        window = f"yg-{td.task_id[:24]}-{run_id}"
        cmd = env_prefix + shlex.join(me) + "; echo '[yardgoat] run finished; window closes in 180s'; sleep 180"
        r = subprocess.run(["tmux", "new-window", "-d", "-P", "-F", "#{pane_pid}", "-t", session, "-n", window, cmd],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("tmux new-window failed: " + r.stderr.strip())
        return {"mode": "tmux", "window": f"{session}:{window}", "pid": int(r.stdout.strip())}
    env = dict(os.environ, YARDGOAT_HOME=str(td.path.parent.parent), PYTHONPATH=pypath, YARDGOAT_ORIG_PYTHONPATH=orig)
    p = subprocess.Popen(me, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    return {"mode": "process", "window": None, "pid": p.pid}


def pgid_alive(pgid) -> bool:
    """Any live (non-zombie) process left in this process group?"""
    if not pgid:
        return False
    for st in Path("/proc").glob("[0-9]*/stat"):
        try:
            f = st.read_text().rsplit(") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if f[0] != "Z" and f[2] == str(pgid):
            return True
    return False


def kill_group(pgid, grace: float = 10.0) -> None:
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(int(pgid), sig)
        except (ProcessLookupError, PermissionError, ValueError):
            return
        end = time.time() + grace
        while time.time() < end and pgid_alive(pgid):
            time.sleep(0.2)
        if not pgid_alive(pgid):
            return


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


STATUS_OF = {"exited": "exited", "blocked": "blocked", "lost": "lost", "launch_failed": "launch_failed"}
STATE_OF = {v: k for k, v in STATUS_OF.items()}


def record_outcome(td: TaskDir, run_id: str, outcome: dict | None = None, replay: bool = False):
    """Commit a run's outcome: receipt + run/task state, together, under the task lock, exactly once.

    A run that is no longer `running` is left alone (late or duplicate results are ignored). With replay=True the
    outcome is taken from a receipt already on disk — recovery when a writer died between receipt and state."""
    spec = td.spec()
    rd = td.run_dir(run_id)
    rd.mkdir(parents=True, exist_ok=True)
    with td.locked() as box:
        st = box["state"]
        run = next(r for r in st["runs"] if r["run_id"] == run_id)
        if run["state"] != "running":
            return None
        existing = rd / "receipt.json"
        if not replay and existing.exists():
            try:  # a receipt already on disk is the first committed result: finish committing it, never overwrite it
                ex0 = read_json(existing)
                replay = ex0.get("task_id") == td.task_id and ex0.get("run_id") == run_id
            except (OSError, ValueError):
                replay = False
        if replay:
            receipt = read_json(rd / "receipt.json")
            state = STATE_OF.get(receipt.get("status"), "exited")
        else:
            ex = spec["executor"]
            artifacts = {}
            for o in spec["outputs"]:
                p = Path(spec["workdir"]) / o
                artifacts[o] = sha256_file(p) if p.is_file() else None
            state = outcome["state"]
            receipt = {
                "task_id": td.task_id, "run_id": run_id, "project": spec["project"], "attempt": int(run_id.lstrip("r")),
                "adapter": spec.get("adapter", {}).get("kind", "direct"),
                "exit_code": outcome.get("exit_code"), "timed_out": outcome.get("exit_code") == 124,
                "host": outcome.get("host", ex["host"]), "lane": outcome.get("lane", ex.get("lane", "")),
                "account_alias": ex.get("acct", ""), "worker": outcome.get("worker"),
                "actual_model": outcome.get("actual_model", ""), "model_source": outcome.get("model_source", "none"),
                "cost_basis": outcome.get("cost_basis", "unknown"), "artifacts": artifacts, "finished_at": now(),
                "status": STATUS_OF[state],
            }
            if state == "blocked":
                receipt["question"] = outcome.get("question", "")
            if state in ("lost", "launch_failed"):
                receipt["reason"] = outcome.get("reason", "")
            write_json(rd / "receipt.json", receipt)
        run.update(state=state, exit_code=receipt.get("exit_code"), finished_at=receipt["finished_at"])
        if st["status"] in ("archived", "verified"):
            pass  # a terminal task keeps its status; only the run record is completed (review 3, F7)
        elif state == "blocked":
            st["status"] = "parked"
            st["question"] = {"run_id": run_id, "text": receipt.get("question", ""), "at": receipt["finished_at"]}
        elif state in ("lost", "launch_failed"):
            st["status"] = "accepted"  # may be dispatched again within budget
        else:
            st["status"] = "exited"
    td.event({"exited": "run.exited", "blocked": "task.parked", "lost": "run.lost", "launch_failed": "run.launch_failed"}[state],
             "runner", run_id=run_id, exit_code=receipt.get("exit_code"), replay=replay)
    return receipt


def worker_env() -> dict:
    """The worker sees the caller's environment, not the one Yardgoat needed to import itself."""
    env = dict(os.environ)
    orig = env.pop("YARDGOAT_ORIG_PYTHONPATH", None)
    if orig:
        env["PYTHONPATH"] = orig
    else:
        env.pop("PYTHONPATH", None)
    return env


def run_and_receipt(td: TaskDir, run_id: str) -> int:
    """Body of `yardgoat _run` for the direct adapter: execute, then write the receipt."""
    spec, cfg = td.spec(), config()
    rd = td.run_dir(run_id)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "script.sh").write_text(script_for(td, spec, rd, cfg), encoding="utf-8")
    with td.locked() as box:
        run = next(r for r in box["state"]["runs"] if r["run_id"] == run_id)
        run["worker_pid"] = os.getpid()
    td.event("run.started", "runner", run_id=run_id)
    started = time.time()
    timeout = float(spec["budget"]["max_wall_min"]) * 60
    with open(rd / "log", "wb") as log:
        # own process group: a timeout ends the worker *and* everything it spawned, and liveness is judged by the
        # group, so a worker that outlives this runner still counts as running (no parallel retry)
        p = subprocess.Popen(["bash", str(rd / "script.sh")], stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, env=worker_env(), start_new_session=True)
        with td.locked() as box:
            run = next(r for r in box["state"]["runs"] if r["run_id"] == run_id)
            run["worker_pgid"] = p.pid
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(p.pid)
            p.wait()
            rc = 124
    model, model_src = detect_model(rd / "log")
    out = {"state": "exited", "exit_code": rc, "actual_model": model, "model_source": model_src,
           "cost_basis": detect_cost(rd / "log")}
    codes = cfg.get("launcher_failure_codes", [])
    head = (rd / "log").read_text(errors="replace")[:2000]
    if any(LAUNCH_FAIL in l for l in head.splitlines()[:5]):
        out.update(state="launch_failed", reason=next(l for l in head.splitlines() if LAUNCH_FAIL in l)[:500])
    elif rc in codes and model_src == "none" and time.time() - started < 120:
        # the launcher refused before any agent started (bad lane, missing tool): an infrastructure
        # failure, not an attempt at the task — it must not consume the task's attempt budget
        first = (rd / "log").read_text(errors="replace").strip().splitlines()[:1]
        out.update(state="launch_failed", reason=(first[0] if first else f"launcher exit {rc}")[:500])
    bf = Path(spec["workdir"]) / spec.get("blocked_file", "out/BLOCKED.md")
    if out["state"] == "exited" and bf.is_file() and bf.stat().st_mtime >= started:
        out.update(state="blocked", question=bf.read_text(encoding="utf-8", errors="replace").strip()[:2000])
    record_outcome(td, run_id, out)
    return rc
