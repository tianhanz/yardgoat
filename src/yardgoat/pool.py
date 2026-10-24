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
    if root and not os.path.realpath(spec["workdir"]).startswith(os.path.realpath(os.path.expanduser(root)) + os.sep):
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


def live_stations(name: str) -> list[dict]:
    res = []
    for f in sorted((pool_dir(name) / "stations").glob("*.json")):
        s = read_json(f)
        if s.get("state") == "running" and (runner.pid_alive(s.get("pid")) or runner.pid_alive(s.get("launcher_pid"))):
            res.append(s)
        elif s.get("state") == "running":
            s["state"] = "gone"; s["ended_at"] = now(); write_json(f, s)
    return res


def all_live_stations(cfg: dict) -> int:
    return sum(len(live_stations(p)) for p in pools(cfg))


def schedule(cfg: dict, direct_in_use: int) -> list[str]:
    """Start stations where queued cards outnumber live stations. Returns started station names."""
    started = []
    cap = cfg.get("max_concurrent")
    for name, c in pools(cfg).items():
        with pool_lock(name):
            q = len(queued_cards(name))
            live = live_stations(name)
            want = min(q, int(c.get("max_stations", 1))) - len(live)
            while want > 0:
                if cap is not None and direct_in_use + all_live_stations(cfg) >= int(cap):
                    break
                started.append(start_station(name, c, cfg)); want -= 1
    return started


def start_station(name: str, c: dict, cfg: dict) -> str:
    d = pool_dir(name)
    n = 1 + max([int(f.stem.rsplit("-", 1)[-1]) for f in (d / "stations").glob("*.json")] or [0])
    station = f"{name}-st-{n}"
    rec = {"station": station, "pool": name, "state": "running", "started_at": now(), "tasks": []}
    write_json(d / "stations" / f"{station}.json", rec)
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
            rec["state"] = "launch_failed"; rec["reason"] = r.stderr.strip()
        else:
            rec["launcher_pid"] = int(r.stdout.strip())
    else:
        p = subprocess.Popen(me, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, env=dict(os.environ, **envs))
        rec["launcher_pid"] = p.pid
    write_json(d / "stations" / f"{station}.json", rec)
    return station


def station_argv(name: str, station: str, c: dict, cfg: dict) -> list[str]:
    yg = shutil.which("yardgoat") or f"{sys.executable} -m yardgoat.cli"
    prompt = STATION_PROMPT.format(pool=name, station=station, yg=yg, max_tasks=int(c.get("max_tasks_per_station", 5)))
    host = c["host"]
    sd = pool_dir(name) / "stations"
    extra = list(cfg.get("host_args", {}).get(host, [])) + list(c.get("args", []))
    root = os.path.expanduser(c.get("root", str(sd)))
    if host == "codex":
        argv = ["codex", "exec", "-s", c.get("sandbox", "workspace-write"), "--skip-git-repo-check", "-C", root,
                "--add-dir", str(home() / "tasks"), "--add-dir", str(home() / "pools"),
                "-o", str(sd / f"{station}.last.md"), *extra, prompt]
    elif host == "claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", *extra, prompt]
    elif host == "kimi":
        argv = ["kimi", "--prompt", prompt, *extra]
    else:  # shell: a scripted station, used by the contract tests
        argv = ["bash", "-c", c["command"], station]
    launcher = cfg.get("launcher") if host != "shell" else None
    if launcher:
        fill = {"host": host, "lane": c.get("lane", ""), "acct": c.get("acct", "")}
        argv = [a.format(**fill) for a in launcher] + argv
    return argv


def run_station(name: str, station: str) -> int:
    """Body of `yardgoat _station`: run the agent CLI with the loop prompt, then mark the station done."""
    cfg = runner.config()
    c = pool_conf(cfg, name)
    d = pool_dir(name) / "stations"
    f = d / f"{station}.json"
    rec = read_json(f); rec["pid"] = os.getpid(); write_json(f, rec)
    lines = ["set -u"] + [f"source {shlex.quote(os.path.expanduser(s))}" for s in cfg.get("source", [])]
    lines += [f"cd {shlex.quote(os.path.expanduser(c.get('root', str(d))))}",
              "export YARDGOAT_STATION=" + shlex.quote(station), "exec " + shlex.join(station_argv(name, station, c, cfg))]
    (d / f"{station}.sh").write_text("\n".join(lines) + "\n")
    env = runner.worker_env()
    env["PATH"] = os.path.dirname(shutil.which("yardgoat") or sys.executable) + os.pathsep + env.get("PATH", "")
    with open(d / f"{station}.log", "wb") as log:
        rc = subprocess.run(["bash", str(d / f"{station}.sh")], stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, env=env).returncode
    rec = read_json(f)
    rec.update(state="exited", exit_code=rc, ended_at=now())
    rec["model"], _ = runner.detect_model(d / f"{station}.log")
    write_json(f, rec)
    return rc


def station_model(name: str, station: str) -> str:
    log = pool_dir(name) / "stations" / f"{station}.log"
    return runner.detect_model(log)[0]
