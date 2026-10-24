"""Dependency and write-set decisions. Callers hold the machine schedule lock when starting work."""
from __future__ import annotations

import glob
import os
import stat
from pathlib import Path

from . import delivery
from .contract import SpecError
from .store import TaskDir, list_tasks

WRITE_SET_SCAN_LIMIT = 50_000


def _canonical_prefix(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        # A symlink-only cycle has no resolvable target. Keep its absolute prefix;
        # stat/scandir below will skip it, while other reachable targets still count.
        return Path(os.path.realpath(path))


def validate_dependencies(spec: dict) -> None:
    """Validate against authoritative accepted specs, including legacy dependency graphs.

    Submission holds the schedule lock through validation and acceptance. Iterative DFS avoids
    a recursion limit on long chains; the candidate replaces its own node while checking cycles.
    """
    task_id = spec["task_id"]
    if task_id in spec.get("depends_on", []):
        raise SpecError("depends_on cannot contain the task itself")
    active, done = set(), set()
    stack = [(task_id, False)]
    while stack:
        tid, leaving = stack.pop()
        if leaving:
            active.remove(tid)
            done.add(tid)
            continue
        if tid in active:
            raise SpecError(f"dependency cycle involving {tid}")
        if tid in done:
            continue
        td = TaskDir(tid)
        if tid != task_id and not td.exists:
            raise SpecError(f"dependency task does not exist: {tid}")
        node = spec if tid == task_id else td.spec()
        active.add(tid)
        stack.append((tid, True))
        stack.extend((dep, False) for dep in reversed(node.get("depends_on", [])))


def dependencies(spec: dict) -> tuple[list[str], list[str]]:
    """Return unfinished (verification or delivery) and terminal upstream ids."""
    waiting, stopped = [], []
    for tid in dict.fromkeys(spec.get("depends_on", [])):
        td = TaskDir(tid)
        st = td.state() if td.exists else {"status": "missing"}
        status = st["status"]
        if status != "verified" or delivery.view(td, st, td.spec())["delivery_pending"]:
            waiting.append(tid)
        if status in ("archived", "exhausted"):
            stopped.append(tid)
    return waiting, stopped


def wait_reason(waiting: list[str]) -> str:
    reasons = []
    for tid in waiting:
        td = TaskDir(tid)
        st = td.state() if td.exists else {"status": "missing"}
        if st["status"] == "verified" and delivery.view(td, st, td.spec())["delivery_pending"]:
            reasons.append(f"等待上游 {tid} 的收尾完成")
        else:
            reasons.append(f"等待上游 verified：{tid}")
    return "；".join(reasons)


def write_set(spec: dict) -> list[str]:
    """Canonical absolute path prefixes, including files that have not been created yet.

    Keep directory/glob prefixes for future files, and add targets of existing symlinks
    reachable below them. Directory identities break cycles; the entry budget bounds work.
    Literal paths are whole path components, so 'src' never overlaps 'src-other'.
    """
    prefixes = set()
    for scope in spec.get("scope", []):
        literal = []
        for part in Path(scope).parts:
            if glob.has_magic(part):
                break
            literal.append(part)
        prefixes.add(str(_canonical_prefix(Path(spec["workdir"]).joinpath(*literal))))
    pending = list(map(Path, sorted(prefixes)))
    seen_dirs = set()
    scanned = 0
    while pending:
        directory = pending.pop()
        try:
            info = directory.stat()
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISDIR(info.st_mode) or identity in seen_dirs:
                continue
            seen_dirs.add(identity)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if scanned >= WRITE_SET_SCAN_LIMIT:
                        if spec.get("task_id"):
                            td = TaskDir(spec["task_id"])
                            if td.exists:
                                td.event("task.scope_scan_limited", "scheduler", limit=WRITE_SET_SCAN_LIMIT,
                                         scanned=scanned, reason="Scope symlink scan limit reached; continuing with "
                                         "collected prefixes. Unvisited symlink targets may be unreserved.")
                        return sorted(prefixes)
                    scanned += 1
                    try:
                        path = Path(entry.path)
                        if entry.is_symlink():
                            path = _canonical_prefix(path)
                            prefixes.add(str(path))
                        if entry.is_dir():
                            pending.append(path)
                    except OSError:
                        continue  # broken/cyclic links or entries removed during the scan
        except OSError:
            continue  # nonexistent or unreadable directories retain their prefix
    return sorted(prefixes)


def scope_conflicts(task_id: str, spec: dict) -> list[dict]:
    mine = [Path(p) for p in write_set(spec)]
    if not mine:
        return []
    conflicts = set()
    for tid in list_tasks():
        if tid == task_id:
            continue
        td = TaskDir(tid)
        st = td.state()
        reservations = [r for r in st["runs"] if r["state"] in ("running", "submit_unknown")]
        if st.get("delivery_reservation"):
            reservations.append(st["delivery_reservation"])
        for run in reservations:
            # An unknown remote submit may already be executing; retain its reservation
            # until its outcome is resolved, just like its existing duplicate-dispatch guard.
            theirs = run.get("write_set")
            if theirs is None:  # pre-M2 running tasks
                theirs = write_set(td.spec())
            for other in map(Path, theirs):
                for own in mine:
                    if own == other or other in own.parents:
                        conflicts.add((tid, str(other)))
                    elif own in other.parents:
                        conflicts.add((tid, str(own)))
    return [{"task_id": tid, "path": path} for tid, path in sorted(conflicts)]
