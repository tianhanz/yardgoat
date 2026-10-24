"""Task contract: load, validate and freeze a task spec.

A spec is accepted once. Re-submitting the same task_id with identical content is a no-op;
with different content it is rejected. "Identical" includes the bytes of the card (prompt) file
and of every file the verifier depends on — they are hashed at submit time and re-checked
before every dispatch and every verification.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .store import sha256_file

HOSTS = ("codex", "claude", "kimi", "shell")
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")

# Task lifecycle. "exited" means the process ended; only a passing frozen verifier makes it "verified".
STATES = ("accepted", "running", "exited", "verified", "failed", "exhausted")


class SpecError(ValueError):
    pass


def load_spec_file(path: str | Path) -> dict:
    p = Path(path).expanduser().resolve()
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional dependency
        except ImportError as e:  # pragma: no cover
            raise SpecError("YAML spec needs PyYAML (pip install 'yardgoat[yaml]'), or use JSON") from e
        spec = yaml.safe_load(text)
    else:
        spec = json.loads(text)
    if not isinstance(spec, dict):
        raise SpecError("spec must be a mapping")
    return normalize(spec, base=p.parent)


def _abs(base: Path, v: str) -> str:
    q = Path(v).expanduser()
    return str(q if q.is_absolute() else (base / q).resolve())


def normalize(spec: dict, base: Path) -> dict:
    s = json.loads(json.dumps(spec))  # deep copy, JSON-only types
    for k in ("task_id", "project", "goal", "card", "workdir", "executor", "verifier"):
        if k not in s:
            raise SpecError(f"missing required field: {k}")
    if not TASK_ID_RE.match(str(s["task_id"])):
        raise SpecError("task_id must match " + TASK_ID_RE.pattern)
    s["card"] = _abs(base, s["card"])
    s["workdir"] = _abs(base, s["workdir"])
    ex = s["executor"]
    if ex.get("host") not in HOSTS:
        raise SpecError(f"executor.host must be one of {HOSTS}")
    if ex["host"] == "shell" and not ex.get("command"):
        raise SpecError("executor.command is required for host=shell")
    v = s["verifier"]
    if not v.get("command"):
        raise SpecError("verifier.command is required")
    v["cwd"] = _abs(base, v.get("cwd", s["workdir"]))
    v["freeze"] = [_abs(base, f) for f in v.get("freeze", [])]
    ad = s.setdefault("adapter", {"kind": "direct"})
    ad.setdefault("kind", "direct")
    if ad["kind"] == "file-pool" and not ad.get("pool"):
        raise SpecError("adapter.pool is required for kind=file-pool")
    if "allowed_lanes" in s and not isinstance(s["allowed_lanes"], list):
        raise SpecError("allowed_lanes must be a list")
    b = s.setdefault("budget", {})
    b.setdefault("max_attempts", 2)
    b.setdefault("max_wall_min", 60)
    ma, mw = b["max_attempts"], b["max_wall_min"]
    if isinstance(ma, bool) or not isinstance(ma, int) or ma < 1:
        raise SpecError("budget.max_attempts must be an integer >= 1")
    if isinstance(mw, bool) or not isinstance(mw, (int, float)) or mw <= 0:
        raise SpecError("budget.max_wall_min must be a number > 0")
    for k in ("outputs", "escalate_when", "allowed_lanes", "scope"):
        if k in s and not (isinstance(s[k], list) and all(isinstance(x, str) for x in s[k])):
            raise SpecError(f"{k} must be a list of strings")
    for p in s.get("scope", []):
        if not p or Path(p).is_absolute() or ".." in Path(p).parts or p.startswith("~"):
            raise SpecError("scope entries must be paths or globs relative to workdir")
    s.setdefault("scope", [])
    s.setdefault("risk", "l2")
    if not isinstance(s["risk"], str) or s["risk"] not in ("l1", "l2", "l3"):
        raise SpecError("risk must be l1, l2 or l3")
    if "acceptance" in s and not isinstance(s["acceptance"], str):
        raise SpecError("acceptance must be a string")
    if not isinstance(v["command"], str) or not all(isinstance(x, str) for x in v["freeze"]):
        raise SpecError("verifier.command must be a string and verifier.freeze a list of paths")
    for k in ("host", "lane", "acct", "command", "sandbox"):
        if k in ex and not isinstance(ex[k], str):
            raise SpecError(f"executor.{k} must be a string")
    if not Path(s["workdir"]).is_dir():
        raise SpecError(f"workdir does not exist: {s['workdir']}")
    s.setdefault("outputs", [])
    s.setdefault("authority", "")
    s.setdefault("escalate_when", [])
    return s


def freeze(spec: dict) -> dict:
    """Hash the card and every verifier dependency; return {path: sha256}."""
    files = [spec["card"], *spec["verifier"]["freeze"]]
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        raise SpecError(f"frozen files not found: {missing}")
    return {f: sha256_file(f) for f in files}


def content_hash(spec: dict, frozen: dict) -> str:
    blob = json.dumps({"spec": spec, "frozen": frozen}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def drift(frozen: dict) -> list[str]:
    """Files whose bytes changed (or vanished) since they were frozen."""
    out = []
    for f, h in frozen.items():
        if not Path(f).is_file() or sha256_file(f) != h:
            out.append(f)
    return out
