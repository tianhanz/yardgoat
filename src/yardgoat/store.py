"""On-disk task store. Every task is a directory; state changes happen under one file lock.

Layout (root = $YARDGOAT_HOME, default ~/.local/share/yardgoat):
  tasks/<task_id>/spec.json      the accepted spec (immutable)
  tasks/<task_id>/task.json      mutable state: owner, status, runs, verdict
  tasks/<task_id>/events.jsonl   append-only event log
  tasks/<task_id>/runs/<run_id>/ run.json, log, receipt.json, verify.json
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("YARDGOAT_HOME", Path.home() / ".local/share/yardgoat")).expanduser()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha256_file(p: str | Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(p: Path, obj) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{time.time_ns()}.tmp")  # unique: concurrent writers never share a tmp file
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


class TaskDir:
    def __init__(self, task_id: str, root: Path | None = None):
        self.task_id = task_id
        self.path = (root or home()) / "tasks" / task_id

    @property
    def exists(self) -> bool:
        return (self.path / "task.json").exists()

    def run_dir(self, run_id: str) -> Path:
        return self.path / "runs" / run_id

    @contextlib.contextmanager
    def locked(self):
        """Hold the task lock; yields the mutable state dict and writes it back on success."""
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.path / ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            state = read_json(self.path / "task.json") if self.exists else None
            box = {"state": state}
            yield box
            if box["state"] is not None:
                write_json(self.path / "task.json", box["state"])

    def state(self) -> dict:
        return read_json(self.path / "task.json")

    @property
    def frozen_path(self) -> Path:
        return self.path.parent.parent / "frozen" / f"{self.task_id}.json"

    def spec(self) -> dict:
        """Accepted spec plus any explicit owner-confirmed legacy review policy.

        Both live in the authoritative frozen record, outside station-writable state.
        The original spec and its content hash remain unchanged by confirmation.
        """
        f = self.frozen_path
        if f.exists():
            d = read_json(f)
            if "spec" in d:
                spec = d["spec"]
                confirmation = d.get("review_confirmation")
                if confirmation and spec.get("review") and not spec["review"].get("channel"):
                    spec["review"]["channel"] = confirmation["channel"]
                return spec
        return read_json(self.path / "spec.json")

    def event(self, kind: str, actor: str, **data) -> dict:
        from . import control
        audit = control.event_fields(self)
        if audit.get("actor") == "reconciler":
            # Receipt replay is a system action. Preserve the original grant as
            # provenance, rather than impersonating the session that issued it.
            origin = {k: data.pop(k) for k in ("actor_session", "epoch", "on_behalf_of") if k in data}
            if origin and origin != {k: v for k, v in audit.items() if k != "actor"}:
                data["execution_control"] = origin
        ev = {"at": now(), "task_id": self.task_id, "kind": kind, "actor": actor,
              **audit, **data}
        ev["dedupe_key"] = hashlib.sha256(json.dumps(ev, sort_keys=True).encode()).hexdigest()[:16]
        with open(self.path / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    def events(self) -> list[dict]:
        p = self.path / "events.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def list_tasks(root: Path | None = None) -> list[str]:
    d = (root or home()) / "tasks"
    return sorted(p.name for p in d.iterdir() if (p / "task.json").exists()) if d.exists() else []
