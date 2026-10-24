"""Dependency and write-set decisions. Callers hold the machine schedule lock when starting work."""
from __future__ import annotations

import glob
from pathlib import Path

from .contract import SpecError
from .store import TaskDir, list_tasks


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
    """Return unverified and terminal upstream task ids, in declared order."""
    waiting, stopped = [], []
    for tid in dict.fromkeys(spec.get("depends_on", [])):
        td = TaskDir(tid)
        status = td.state()["status"] if td.exists else "missing"
        if status != "verified":
            waiting.append(tid)
        if status in ("archived", "exhausted"):
            stopped.append(tid)
    return waiting, stopped


def write_set(spec: dict) -> list[str]:
    """Canonical absolute path prefixes, including files that have not been created yet.

    A glob reserves its literal directory prefix before the first wildcard component. We do
    not enumerate current matches: that would leave future files unprotected. Literal paths
    are whole path components, so 'src' never overlaps 'src-other'.
    """
    prefixes = set()
    for scope in spec.get("scope", []):
        literal = []
        for part in Path(scope).parts:
            if glob.has_magic(part):
                break
            literal.append(part)
        prefixes.add(str(Path(spec["workdir"]).joinpath(*literal).resolve()))
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
        for run in td.state()["runs"]:
            # An unknown remote submit may already be executing; retain its reservation
            # until its outcome is resolved, just like its existing duplicate-dispatch guard.
            if run["state"] not in ("running", "submit_unknown"):
                continue
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
