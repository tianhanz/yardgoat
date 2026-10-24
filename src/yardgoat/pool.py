"""Yardgoat's own task pool: queue first, then a station claims cards one after another.

A pool is defined in the local config:

  "pools": {"codex": {"host": "codex", "lane": "codex", "acct": "a",
                      "root": "~/work/yardgoat-tasks",   # task workdirs must live under it
                      "max_stations": 3, "max_tasks_per_station": 5}}

A task opts in with `"adapter": {"kind": "pool", "pool": "codex"}`. Dispatch only queues the run.
`schedule()` (called by dispatch and by reconcile) starts stations while there are more queued cards than
live stations, within max_stations and the machine-wide max_concurrent. A station is one agent CLI session
whose prompt is a loop: `yardgoat claim` → do the card → `yardgoat finish` → claim again → EMPTY → exit.
That reuses the warm session across cards instead of paying a cold start per card. Stations never receive
typed messages; they pull. Verification stays with the task owner (`close`), or with reconcile when the
spec says `"auto_close": true`.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import runner
from .store import TaskDir, home, list_tasks, now, read_json, write_json

STATION_PROMPT = """你是 Yardgoat 任务池「{pool}」的工位，身份 {station}。按下面的循环干活，直到池子空了：

1. 领一张：运行 `{yg} claim --pool {pool} --station {station}`。输出 JSON 里 result 是 EMPTY 就结束回答。
   否则会给出 task_id、run_id、workdir、prompt（任务说明文件的路径）、blocked_file。
2. 读 prompt 文件，在 workdir 里按说明把事做完。**每张卡相互独立**：不要沿用上一张卡的结论或文件，除非卡里明确要求。
3. 卡住（缺授权、缺材料、说明自相矛盾）：把一个具体问题写进 workdir 下的 blocked_file，然后照常做第 4 步。
4. 交付：运行 `{yg} finish <task_id> --run <run_id> --station {station}`。验收不归你，持有人会用冻结的验收程序判。
5. 回到第 1 步。最多做 {max_tasks} 张就结束回答。

铁律：只做卡里写的事；不往任何 tmux 窗口发消息；卡里没授权就不 git push、不改线上、不发消息。
"""


def pools(cfg: dict) -> dict:
    return cfg.get("pools", {})


def pool_conf(cfg: dict, name: str) -> dict:
    c = pools(cfg).get(name)
    if not c:
        raise RuntimeError(f"pool {name!r} is not defined in the local config (pools.{name})")
    return c


def pool_dir(name: str) -> Path:
    d = home() / "pools" / name
    (d / "stations").mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def pool_lock(name: str):
    with open(pool_dir(name) / ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def workdir_problem(spec: dict, cfg: dict) -> str | None:
    c = pool_conf(cfg, spec["adapter"]["pool"])
    root = c.get("root")
    r, w = os.path.realpath(os.path.expanduser(root)) if root else "", os.path.realpath(spec["workdir"])
    if root and w != r and not w.startswith(r + os.sep):
        return f"pool {spec['adapter']['pool']!r} only runs tasks whose workdir is under {root}"
    return None


def queued_cards(name: str) -> list[tuple[str, str]]:
    out = []
    for t in list_tasks():
        td = TaskDir(t)
        st = td.state()
        last = st["runs"][-1] if st["runs"] else None
        if last and last["state"] == "queued" and td.spec().get("adapter", {}).get("pool") == name \
                and td.spec().get("adapter", {}).get("kind") == "pool":
            out.append((last.get("queued_at", ""), t))
    return sorted(out)


def station_file(name: str, station: str) -> Path:
    return pool_dir(name) / "stations" / f"{station}.json"


@contextmanager
def station_lock(name: str, station: str):
    with open(pool_dir(name) / "stations" / f".{station}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def update_station(pool_name: str, station_id: str, only_if_state: str | None = None, **fields) -> dict | None:
    """Re-read, merge, write — under the station's own lock, so parent and child never overwrite each other."""
    with station_lock(pool_name, station_id):
        f = station_file(pool_name, station_id)
        rec = read_json(f) if f.exists() else {}
        if only_if_state is not None and rec.get("state") != only_if_state:
            return None
        rec.update(fields)
        write_json(f, rec)
        return rec


def get_station(name: str, station: str) -> dict | None:
    f = station_file(name, station)
    return read_json(f) if f.exists() else None


def station_alive(rec: dict | None) -> bool:
    """Wrapper alive, or (wrapper gone) its agent process group still running: either way the slot is taken."""
    return bool(rec) and rec.get("state") == "running" and (
        runner.pid_alive(rec.get("pid")) or runner.pid_alive(rec.get("launcher_pid")) or runner.pgid_alive(rec.get("worker_pgid")))


def live_stations(name: str) -> list[dict]:
    res = []
    for f in sorted((pool_dir(name) / "stations").glob("*.json")):
        s = read_json(f)
        if station_alive(s):
            res.append(s)
        elif s.get("state") == "running":
            update_station(name, s["station"], only_if_state="running", state="gone", ended_at=now())
    return res


def all_live_stations(cfg: dict) -> int:
    return sum(len(live_stations(p)) for p in pools(cfg))


@contextmanager
def schedule_lock():
    """One machine-wide lock for every 'check capacity, then start' decision (direct runs and stations).
    Lock order: schedule lock first, then a pool or task lock — never the other way round."""
    home().mkdir(parents=True, exist_ok=True)
    with open(home() / ".schedule.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def resources_ok(cfg: dict) -> tuple[bool, str]:
    """Machine guard for starting one more worker: free memory and load (local config min_free_gb / max_load)."""
    mf, ml = cfg.get("min_free_gb"), cfg.get("max_load")
    if mf is not None:
        avail = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemAvailable")) / 1048576
        if avail < float(mf):
            return False, f"only {avail:.1f} GB memory available (min_free_gb={mf})"
    if ml is not None and os.getloadavg()[0] > float(ml):
        return False, f"1-minute load {os.getloadavg()[0]:.1f} > max_load={ml}"
    return True, ""


def schedule(cfg: dict, direct_in_use_fn) -> list[str]:
    """Start stations where queued cards outnumber live stations. Caller holds schedule_lock()."""
    direct_in_use = direct_in_use_fn() if callable(direct_in_use_fn) else direct_in_use_fn
    started = []
    cap = cfg.get("max_concurrent")
    for name, c in pools(cfg).items():
        with pool_lock(name):
            q = len(queued_cards(name))
            live = live_stations(name)
            # one more station per `spawn_per_queued` waiting cards: small batches stay on one warm station.
            # Only stations without a card in hand count as available — a station busy with a long card does not.
            k = max(1, int(c.get("spawn_per_queued", 1)))
            free = sum(1 for x in live if not x.get("current"))
            want = min(-(-q // k) - free, int(c.get("max_stations", 1)) - len(live))
            while want > 0:
                if cap is not None and direct_in_use + all_live_stations(cfg) >= int(cap):
                    break
                if not resources_ok(cfg)[0]:
                    break
                started.append(start_station(name, c, cfg)); want -= 1
    return started


def start_station(name: str, c: dict, cfg: dict) -> str:
    d = pool_dir(name)
    n = 1 + max([int(f.stem.rsplit("-", 1)[-1]) for f in (d / "stations").glob("*.json")] or [0])
    station = f"{name}-st-{n}"
    accts = c.get("accts") or ([c["acct"]] if c.get("acct") else [""])
    acct = accts[(n - 1) % len(accts)]          # spread stations over accounts (each account has its own rate limits)
    update_station(name, station, station=station, pool=name, state="running", started_at=now(), tasks=[], current=None, acct=acct)
    me = [sys.executable, "-m", "yardgoat.cli", "_station", name, station]
    pypath = str(Path(__file__).resolve().parents[1])
    envs = {"YARDGOAT_HOME": str(home()), "PYTHONPATH": pypath, "YARDGOAT_ORIG_PYTHONPATH": os.environ.get("PYTHONPATH", "")}
    if os.environ.get("YARDGOAT_CONFIG"):
        envs["YARDGOAT_CONFIG"] = os.environ["YARDGOAT_CONFIG"]
    session = cfg.get("tmux_session")
    if session and shutil.which("tmux") and not os.environ.get("YARDGOAT_NO_TMUX"):
        cmd = " ".join(f"{k}={shlex.quote(v)}" for k, v in envs.items()) + " " + shlex.join(me) + \
              "; echo '[yardgoat] station finished; window closes in 120s'; sleep 120"
        r = subprocess.run(["tmux", "new-window", "-d", "-P", "-F", "#{pane_pid}", "-t", session, "-n", f"yg-{station}", cmd],
                           capture_output=True, text=True)
        if r.returncode != 0:
            update_station(name, station, state="launch_failed", reason=r.stderr.strip())
        else:
            update_station(name, station, launcher_pid=int(r.stdout.strip()))
    else:
        p = subprocess.Popen(me, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, env=dict(os.environ, **envs))
        update_station(name, station, launcher_pid=p.pid)
    return station


def mailbox_dir(name: str, station: str, c: dict) -> Path:
    """Inside the pool root (the only place a sandboxed station may write); the broker reads it from outside."""
    root = Path(os.path.expanduser(c.get("root", str(pool_dir(name) / "stations"))))
    d = root / ".yardgoat-mailbox" / station
    d.mkdir(parents=True, exist_ok=True)
    return d


def station_argv(name: str, station: str, c: dict, cfg: dict) -> list[str]:
    yg = shutil.which("yardgoat") or f"{sys.executable} -m yardgoat.cli"
    prompt = STATION_PROMPT.format(pool=name, station=station, yg=yg, max_tasks=int(c.get("max_tasks_per_station", 5)))
    pre = c.get("preamble") or (Path(os.path.expanduser(c["preamble_file"])).read_text(encoding="utf-8") if c.get("preamble_file") else "")
    if pre:  # project rules a station reads once per session, then reuses for every card
        prompt = pre.strip() + "\n\n---\n\n" + prompt
    host = c["host"]
    sd = pool_dir(name) / "stations"
    extra = list(cfg.get("host_args", {}).get(host, [])) + list(c.get("args", []))
    root = os.path.expanduser(c.get("root", str(sd)))
    if host == "codex":
        # no --add-dir: the station writes only its pool root; claim/finish go through the mailbox in that root
        argv = ["codex", "exec", "-s", c.get("sandbox", "workspace-write"), "--skip-git-repo-check", "-C", root,
                "-o", str(sd / f"{station}.last.md"), *extra, prompt]
    elif host == "claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", *extra, prompt]
    elif host == "kimi":
        argv = ["kimi", "--prompt", prompt, *extra]
    else:  # shell: a scripted station, used by the contract tests
        argv = ["bash", "-c", c["command"], station]
    launcher = cfg.get("launcher") if host != "shell" else None
    if launcher:
        acct = (get_station(name, station) or {}).get("acct") or c.get("acct", "")
        fill = {"host": host, "lane": c.get("lane", ""), "acct": acct}
        argv = [a.format(**fill) for a in launcher] + argv
    return argv


def run_station(name: str, station: str) -> int:
    """Body of `yardgoat _station`: run the agent with the loop prompt and broker its claim/finish requests.

    The agent may be sandboxed and cannot write the task store; it drops requests into its mailbox (inside the pool
    root) and this wrapper, running outside the sandbox, executes them. The wrapper also enforces each card's
    max_wall_min: past the deadline the whole agent process group is ended and the card is recorded as timed out."""
    from . import cli  # local import: cli imports pool
    cfg = runner.config()
    c = pool_conf(cfg, name)
    d = pool_dir(name) / "stations"
    mb = mailbox_dir(name, station, c)
    update_station(name, station, pid=os.getpid(), mailbox=str(mb))
    lines = ["set -u"] + [f"source {shlex.quote(os.path.expanduser(x))} || exit 3" for x in cfg.get("source", [])]
    lines += [f"cd {shlex.quote(os.path.expanduser(c.get('root', str(d))))} || exit 3",
              "export YARDGOAT_STATION=" + shlex.quote(station), "export YARDGOAT_MAILBOX=" + shlex.quote(str(mb)),
              "exec " + shlex.join(station_argv(name, station, c, cfg))]
    (d / f"{station}.sh").write_text("\n".join(lines) + "\n")
    env = runner.worker_env()
    env["PATH"] = os.path.dirname(shutil.which("yardgoat") or sys.executable) + os.pathsep + env.get("PATH", "")
    timed_out = None
    with open(d / f"{station}.log", "wb") as log:
        p = subprocess.Popen(["bash", str(d / f"{station}.sh")], stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, env=env, start_new_session=True)
        update_station(name, station, worker_pgid=p.pid)
        while p.poll() is None:
            serve_mailbox(name, station, mb, cli)
            cur = (get_station(name, station) or {}).get("current")
            if cur and time.time() > cur["deadline"]:
                runner.kill_group(p.pid)
                timed_out = cur
                break
            time.sleep(0.2)
        p.wait()
    serve_mailbox(name, station, mb, cli, respond_only=True)
    cur = (get_station(name, station) or {}).get("current")
    if cur:
        from .store import TaskDir
        if timed_out:
            runner.record_outcome(TaskDir(cur["task_id"]), cur["run_id"], {
                "state": "exited", "exit_code": 124, "worker": station, "host": c.get("host", ""), "lane": c.get("lane", ""),
                "actual_model": station_model(name, station), "model_source": "station-log", "cost_basis": "shared-station"})
        else:
            runner.record_outcome(TaskDir(cur["task_id"]), cur["run_id"], {
                "state": "lost", "reason": f"station {station} exited without finishing the card", "worker": station})
    rec = update_station(name, station, state="exited", exit_code=p.returncode, ended_at=now(), current=None,
                         model=runner.detect_model(d / f"{station}.log")[0], timed_out=bool(timed_out))
    return p.returncode or 0


def serve_mailbox(name: str, station: str, mb: Path, cli, respond_only: bool = False) -> None:
    for req in sorted(mb.glob("req-*.json")):
        try:
            r = json.loads(req.read_text())
        except (OSError, ValueError):
            continue  # still being written; next tick
        if respond_only:
            out = (3, {"result": "REFUSED", "reason": "station is shutting down"})
        elif time.time() > float(r.get("expires", 1e18)):
            out = (3, {"result": "REFUSED", "reason": "request expired before the station handled it"})
        elif r.get("op") == "claim":
            out = cli.claim_card(name, station)
        elif r.get("op") == "finish":
            cur = (get_station(name, station) or {}).get("current") or {}
            late = cur.get("task_id") == r.get("task_id") and cur.get("run_id") == r.get("run_id") and time.time() > cur.get("deadline", 1e18)
            out = cli.finish_card(r.get("task_id", ""), r.get("run_id", ""), station, late=late)
        else:
            out = (2, {"result": "ERROR", "reason": f"unknown op {r.get('op')!r}"})
        tmp = mb / f".{req.stem}.resp.tmp"
        tmp.write_text(json.dumps({"rc": out[0], "out": out[1]}, ensure_ascii=False))
        os.replace(tmp, mb / req.name.replace("req-", "resp-"))
        req.unlink(missing_ok=True)


def station_model(name: str, station: str) -> str:
    log = pool_dir(name) / "stations" / f"{station}.log"
    return runner.detect_model(log)[0]
