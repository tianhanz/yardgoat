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


def build_argv(td: TaskDir, spec: dict, run_dir: Path, cfg: dict, probe_argv: list[str] | None = None) -> list[str]:
    ex = spec["executor"]
    host = ex["host"]
    prompt = "" if probe_argv is not None else prompt_text(td, spec)
    extra = list(cfg.get("host_args", {}).get(host, [])) + list(ex.get("args", []))
    if host == "codex":
        argv = ["codex", "exec", "--json", "-s", ex.get("sandbox", "workspace-write"), "--skip-git-repo-check",
                "-C", spec["workdir"], "-o", str(run_dir / "last_message.md"), *extra, prompt]
    elif host == "claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", *extra, prompt]
    elif host == "kimi":  # experimental: flags verified only against `kimi --help`
        argv = ["kimi", "--prompt", prompt, "--output-format", "stream-json", *extra]
    else:  # shell
        argv = ["bash", "-c", ex["command"]]
    if probe_argv is not None:
        argv = probe_argv
    launcher = cfg.get("launcher") if host != "shell" else None
    if launcher:
        fill = {"host": host, "lane": ex.get("lane", ""), "acct": ex.get("acct", "")}
        argv = [a.format(**fill) for a in launcher] + argv
    return argv


LAUNCH_FAIL = "yardgoat-launch-failed:"


def shell_script(workdir: str, cfg: dict, argv: list[str], exports: dict | None = None) -> str:
    """The common source → cwd → launcher/host chain, also used by station probes."""
    lines = ["set -u"]
    for f in cfg.get("source", []):
        q = shlex.quote(os.path.expanduser(f))
        lines.append(f"source {q} || {{ echo {shlex.quote(LAUNCH_FAIL + ' source ' + f)}; exit 3; }}")
    w = shlex.quote(workdir)
    lines.append(f"cd {w} || {{ echo {shlex.quote(LAUNCH_FAIL + ' cd ' + workdir)}; exit 3; }}")
    lines.extend(f"export {k}={shlex.quote(v)}" for k, v in (exports or {}).items())
    lines.append("exec " + shlex.join(argv))
    return "\n".join(lines) + "\n"


def script_for(td: TaskDir, spec: dict, run_dir: Path, cfg: dict, probe_argv: list[str] | None = None) -> str:
    return shell_script(spec["workdir"], cfg, build_argv(td, spec, run_dir, cfg, probe_argv))


def launch(td: TaskDir, run_id: str, cfg: dict) -> dict:
    """Start `yardgoat _run` detached: in a tmux window when configured, else as a background process."""
    me = [sys.executable, "-m", "yardgoat.cli", "_run", td.task_id, run_id]
    return launch_daemon(me, cfg, str(td.path.parent.parent), f"yg-{td.task_id[:24]}-{run_id}", linger=180)


def launch_daemon(me: list[str], cfg: dict, root: str, window: str, linger: int = 0) -> dict:
    """Shared outer launch: direct runner, station wrapper and doctor get the same environment."""
    # Only the directory that holds the yardgoat package — never our whole sys.path. Leaking sys.path
    # (stdlib of *this* interpreter) into the worker breaks every other Python the worker starts
    # (2026-10-23: a 3.13 sys.path made the host's 3.10 helpers crash before the agent started).
    pypath = str(Path(__file__).resolve().parents[1])
    orig = os.environ.get("PYTHONPATH", "")
    envs = {"YARDGOAT_HOME": root, "PYTHONPATH": pypath, "YARDGOAT_ORIG_PYTHONPATH": orig}
    if os.environ.get("YARDGOAT_CONFIG"):
        envs["YARDGOAT_CONFIG"] = os.environ["YARDGOAT_CONFIG"]
    # A tmux window inherits the tmux *server's* environment, not the caller's. Workers are configured by the local
    # config (source + launcher), deliberately not by whoever dispatched; pass_env is an explicit allowlist.
    for k in cfg.get("pass_env", []):
        if k in os.environ:
            envs[k] = os.environ[k]
    session = cfg.get("tmux_session")
    if session and shutil.which("tmux") and not os.environ.get("YARDGOAT_NO_TMUX"):
        cmd = " ".join(f"{k}={shlex.quote(v)}" for k, v in envs.items()) + " " + shlex.join(me)
        if linger:
            cmd += f"; echo '[yardgoat] finished; window closes in {linger}s'; sleep {linger}"
        r = subprocess.run(["tmux", "new-window", "-d", "-P", "-F", "#{pane_pid}", "-t", session, "-n", window, cmd],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("tmux new-window failed: " + r.stderr.strip())
        return {"mode": "tmux", "window": f"{session}:{window}", "pid": int(r.stdout.strip())}
    env = dict(os.environ, **envs)
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


PROVIDER_REFUSAL_PATTERNS = [r"flagged for possible", r"usage limit", r"You've reached your", r"rate limit exceeded"]
PROVIDER_REFUSAL_HINT = "换措辞或换通道后重发"


def log_segment(log: Path, start: int = 0, end: int | None = None, tail: bool = True) -> str:
    """Read complete lines within one run's byte interval, bounded to 20 KB."""
    try:
        with log.open("rb") as f:
            end = min(os.fstat(f.fileno()).st_size, end) if end is not None else os.fstat(f.fileno()).st_size
            offset = max(start, end - 20000) if tail else start
            if offset >= end:
                return ""
            if offset:
                f.seek(offset - 1)
                if f.read(1) != b"\n":
                    f.readline(end - offset)  # never parse a fragment crossing the claim boundary
            else:
                f.seek(0)
            return f.read(min(20000, max(0, end - f.tell()))).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def provider_refusal(log: Path, cfg: dict, start: int = 0, end: int | None = None) -> str | None:
    """Only host error envelopes are evidence; prose and nested tool output are not.

    A later successful terminal event clears an intermediate refusal, including hosts that exit 0.
    Plain logs have no reliable provenance, so keyword matching is limited to error payloads.
    """
    patterns = cfg.get("provider_refusal_patterns", PROVIDER_REFUSAL_PATTERNS)
    codes = {"rate_limit_exceeded", "rate_limit_error", "usage_limit_reached", "quota_exceeded",
             "insufficient_quota", "policy_violation", "content_policy_violation"}
    refusal = None
    for line in log_segment(log, start, end).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.completed" or (kind == "result" and event.get("is_error") is False):
            refusal = None
            continue
        if kind in ("error", "turn.failed"):
            error = event.get("error", event.get("message", ""))
        elif kind == "result" and event.get("is_error") is True:
            error = event.get("errors", event.get("error", event.get("result", "")))
        else:
            continue
        message = error.get("message", "") if isinstance(error, dict) else error
        text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        code = error.get("code", error.get("type")) if isinstance(error, dict) else None
        matched = isinstance(code, str) and code in codes
        matched |= any(re.search(p, text, re.I) for p in patterns)
        refusal = (text or str(code))[:2000] if matched else None
    return refusal


def classify_outcome(log: Path, cfg: dict, *, exit_code: int | None = None, timed_out: bool = False,
                     start: int = 0, end: int | None = None, default: str = "lost",
                     elapsed: float | None = None, blocked_path: Path | None = None,
                     blocked_before: list[int] | None = None) -> dict:
    """One precedence for finish and recovery: launch failure > actual timeout > refusal > other."""
    out = {"state": default, "exit_code": exit_code, "timed_out": timed_out}
    marker = next((s for s in log_segment(log, start, end, tail=False).splitlines()
                   if s.startswith(LAUNCH_FAIL)), None)
    if marker:
        return {**out, "state": "launch_failed", "reason": marker[:500]}
    if timed_out:
        return {**out, "state": "exited", "exit_code": 124}
    refusal = provider_refusal(log, cfg, start, end)
    if refusal:
        return {**out, "state": "provider_refused", "reason": refusal}
    if exit_code in cfg.get("launcher_failure_codes", []) and elapsed is not None and elapsed < 120 \
            and not detect_model(log)[0]:
        lines = log_segment(log, start, end, tail=False).strip().splitlines()
        return {**out, "state": "launch_failed", "reason": (lines[0] if lines else f"launcher exit {exit_code}")[:500]}
    if default == "exited" and blocked_path is not None and blocked_path.is_file() \
            and file_fingerprint(blocked_path) != blocked_before:
        return {**out, "state": "blocked", "question": blocked_path.read_text(encoding="utf-8", errors="replace").strip()[:2000]}
    if default == "lost":
        out["reason"] = "worker process ended without finishing the card"
    return out


def execution_evidence(rd: Path) -> dict:
    path = rd / "execution.json"
    return read_json(path) if path.exists() else {}


def file_fingerprint(path: Path) -> list[int] | None:
    """Observe file changes without comparing filesystem timestamps to a finer-resolution clock."""
    try:
        s = path.stat()
        return [s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns] if path.is_file() else None
    except FileNotFoundError:
        return None


STATUS_OF = {"exited": "exited", "blocked": "blocked", "lost": "lost", "launch_failed": "launch_failed",
             "provider_refused": "provider_refused"}
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
                "exit_code": outcome.get("exit_code"), "timed_out": outcome.get("timed_out") is True,
                "host": outcome.get("host", ex["host"]), "lane": outcome.get("lane", ex.get("lane", "")),
                "account_alias": outcome.get("account_alias") or ex.get("acct", ""), "worker": outcome.get("worker"),
                "actual_model": outcome.get("actual_model", ""), "model_source": outcome.get("model_source", "none"),
                "cost_basis": outcome.get("cost_basis", "unknown"), "artifacts": artifacts, "finished_at": now(),
                "status": STATUS_OF[state],
            }
            if state == "blocked":
                receipt["question"] = outcome.get("question", "")
            if state in ("lost", "launch_failed", "provider_refused"):
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
        elif state == "provider_refused":
            st["status"] = state
            st["question"] = {"run_id": run_id, "text": receipt.get("reason", "") + "\n" + PROVIDER_REFUSAL_HINT,
                              "at": receipt["finished_at"]}
        else:
            st["status"] = "exited"
    td.event({"exited": "run.exited", "blocked": "task.parked", "lost": "run.lost", "launch_failed": "run.launch_failed",
              "provider_refused": "run.provider_refused"}[state],
             "runner", run_id=run_id, exit_code=receipt.get("exit_code"), replay=replay,
             **({"reason": receipt["reason"]} if state == "provider_refused" else {}))
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
    bf = Path(spec["workdir"]) / spec.get("blocked_file", "out/BLOCKED.md")
    blocked_before = file_fingerprint(bf)
    with td.locked() as box:
        run = next(r for r in box["state"]["runs"] if r["run_id"] == run_id)
        run["worker_pid"] = os.getpid()
        run["blocked_before"] = blocked_before
    td.event("run.started", "runner", run_id=run_id)
    started = time.time()
    timeout = float(spec["budget"]["max_wall_min"]) * 60
    timed_out = False
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
            timed_out = True
            # Persist the timeout handler decision before killing, so recovery preserves its priority.
            write_json(rd / "execution.json", {"exit_code": 124, "timed_out": True, "elapsed": time.time() - started})
            kill_group(p.pid)
            p.wait()
            rc = 124
    write_json(rd / "execution.json", {"exit_code": rc, "timed_out": timed_out, "elapsed": time.time() - started})
    model, model_src = detect_model(rd / "log")
    out = classify_outcome(rd / "log", cfg, default="exited", blocked_path=bf, blocked_before=blocked_before,
                           **execution_evidence(rd))
    out.update(actual_model=model, model_source=model_src, cost_basis=detect_cost(rd / "log"))
    record_outcome(td, run_id, out)
    return rc
