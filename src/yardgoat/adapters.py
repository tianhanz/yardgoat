"""Adapters: how a run is started and how its outcome is observed.

An adapter has two operations:
  submit(td, run_id, spec, cfg) -> dict     start the work; returns fields stored on the run
  poll(td, run, spec, cfg) -> dict | None   None while still running; otherwise an outcome:
        {"state": "exited", "exit_code": int, ...}   process ended (verification is separate)
        {"state": "blocked", "question": str, ...}   the worker needs an answer from a human
        {"state": "lost", "reason": str}             the work vanished (crash, deleted card)

Built in:
  direct     Yardgoat starts the agent CLI itself (see runner.py).
  file-pool  Submit into an existing file-queue fleet whose tasks are markdown files moving through
             <root>/<pool>/{pending,claimed,done,parked}/. Yardgoat never claims from the pool;
             the fleet's own workers do. Configure in ~/.config/yardgoat/config.json:
               "adapters": {"file-pool": {"root": "~/fleet/tasks",
                                          "add_cmd": ["python3", "~/fleet/fleet.py", "add", "{text}",
                                                      "--pool", "{pool}", "--id", "{pool_task_id}", "--verify", "{verify}"],
                                          "pools": {"research": {"lane": "relay-opus", "host": "claude"}}}}
             Without add_cmd the card is written to <root>/<pool>/pending/ directly.

More adapters can be loaded from local files listed in config "plugins"; each file defines
ADAPTERS = {"kind-name": AdapterClass}. Keep adapters that embed private hosts or paths there,
not in this repository.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

from . import runner
from .store import TaskDir


class Direct:
    kind = "direct"

    def submit(self, td: TaskDir, run_id: str, spec: dict, cfg: dict) -> dict:
        return runner.launch(td, run_id, cfg)

    def poll(self, td: TaskDir, run: dict, spec: dict, cfg: dict):
        # the runner writes the receipt itself; we only notice runs whose process died without one
        if runner.pid_alive(run.get("worker_pid")) or runner.pid_alive(run.get("pid")):
            return None
        if (td.run_dir(run["run_id"]) / "receipt.json").exists():
            return None  # already recorded by the runner
        return {"state": "lost", "reason": "worker process ended without writing a receipt"}


class FilePool:
    kind = "file-pool"
    STAGES = ("pending", "claimed", "done", "parked")

    def _conf(self, cfg: dict) -> dict:
        c = cfg.get("adapters", {}).get("file-pool", {})
        if not c.get("root"):
            raise RuntimeError("file-pool adapter needs adapters.file-pool.root in the local config")
        return c

    def submit(self, td: TaskDir, run_id: str, spec: dict, cfg: dict) -> dict:
        c = self._conf(cfg)
        pool = spec["adapter"]["pool"]
        ptid = f"{td.task_id}-{run_id}"
        text = runner.prompt_text(td, spec)
        verify = spec["adapter"].get("worker_verify", "")
        if c.get("add_cmd"):
            fill = {"text": text, "pool": pool, "pool_task_id": ptid, "verify": verify}
            tmpl = list(c["add_cmd"])
            if not verify and "{verify}" in tmpl:  # drop the "--verify {verify}" pair when nothing to verify
                i = tmpl.index("{verify}")
                del tmpl[max(i - 1, 0):i + 1]
            argv = [os.path.expanduser(a) if a.startswith("~") else a.format(**fill) for a in tmpl]
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, FLEET_DISPATCHER=f"yardgoat:{td.task_id}"))
            if r.returncode != 0:
                raise RuntimeError(f"add_cmd failed: {r.stderr.strip() or r.stdout.strip()}")
        else:
            d = Path(os.path.expanduser(c["root"])) / pool / "pending"
            d.mkdir(parents=True, exist_ok=True)
            body = f"# {ptid}\n\n<!-- pool: {pool} -->\n\n{text}\n\n<!-- dispatcher: yardgoat:{td.task_id} -->\n"
            if verify:
                body += f"\n<!-- verify: {verify} -->\n"
            (d / f"{ptid}.md").write_text(body, encoding="utf-8")
        return {"mode": "file-pool", "pool": pool, "pool_task_id": ptid, "pid": None, "window": None}

    def _find(self, cfg: dict, pool: str, ptid: str):
        root = Path(os.path.expanduser(self._conf(cfg)["root"])) / pool
        for stage in self.STAGES:
            for f in (root / stage).glob(f"{ptid}*.md"):
                if f.name == f"{ptid}.md" or f.name.startswith(f"{ptid}."):
                    return stage, f
        return None, None

    def poll(self, td: TaskDir, run: dict, spec: dict, cfg: dict):
        stage, f = self._find(cfg, run["pool"], run["pool_task_id"])
        pools = self._conf(cfg).get("pools", {}).get(run["pool"], {})
        meta = {"worker": f.name[len(run["pool_task_id"]) + 1:-3] if f and f.name != f"{run['pool_task_id']}.md" else None,
                "lane": pools.get("lane", ""), "host": pools.get("host", "pool"), "model_source": "pool-config"}
        if stage in (None,):
            return {"state": "lost", "reason": "card is gone from every stage of the pool", **meta}
        if stage in ("pending", "claimed"):
            return None
        if stage == "done":
            return {"state": "exited", "exit_code": 0, **meta}
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"##\s*待[^\n]*确认\s*\n(.*)", text, re.S) or re.search(r"##\s*Question\s*\n(.*)", text, re.S)
        return {"state": "blocked", "question": (m.group(1) if m else text[-1500:]).strip()[:2000], **meta}


BUILTIN = {"direct": Direct, "file-pool": FilePool}


def registry(cfg: dict) -> dict:
    reg = dict(BUILTIN)
    for p in cfg.get("plugins", []):
        path = os.path.expanduser(p)
        spec = importlib.util.spec_from_file_location(f"yardgoat_plugin_{abs(hash(path))}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        reg.update(getattr(mod, "ADAPTERS", {}))
    return reg


def get(kind: str, cfg: dict):
    reg = registry(cfg)
    if kind not in reg:
        raise RuntimeError(f"unknown adapter kind {kind!r}; known: {sorted(reg)}")
    return reg[kind]()
