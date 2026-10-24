"""Optional machine-local argv hook for tasks that need a decision. Never prints to stdout."""
from __future__ import annotations

import fcntl
import hashlib
import logging
import shlex
import subprocess

from .store import TaskDir, home, list_tasks, now, read_json, write_json

DEFAULT_ON = ("parked", "exhausted", "submit_unknown", "verify_error", "provider_refused")
LOG = logging.getLogger(__name__)


def _status(state, on):
    status = state.get("status")
    if status in ("archived", "verified"):
        return None  # do not revive an old run's pending status after the task has closed
    if status in on:
        return status
    runs = state.get("runs", [])
    last_status = runs[-1].get("state") if runs else None
    # Older do_close leaves task.status=exited when the run becomes verify_error.
    return last_status if last_status in on else None


def _hint(task_id, status, owner):
    task, who = shlex.quote(task_id), shlex.quote(owner or "<you>")
    if status == "parked":
        return f"yardgoat answer {task} --as {who} --text …"
    if status == "verify_error":
        return f"yardgoat close {task} --as {who}"
    if status == "exhausted":
        return f"yardgoat archive {task} --as {who} --reason …"
    return f"yardgoat status {task} --json"


def _notify_one(task_id, command, on):
    directory = home() / "notified"
    directory.mkdir(parents=True, exist_ok=True)
    # A separate per-task lock serializes concurrent reconcilers without holding schedule/task locks
    # while an external command runs. Only a successful command creates a delivered record.
    with open(directory / f".{task_id}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = TaskDir(task_id).state()
        status = _status(state, on)
        if status is None:
            return None
        question = state.get("question") or ""
        if isinstance(question, dict):
            question = question.get("text") or ""
        question = str(question)
        question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
        key = f"{status}:{question_hash}"
        path = directory / f"{task_id}.json"
        record = read_json(path) if path.exists() else {"task_id": task_id, "sent": {}}
        if key in record["sent"]:
            return None
        owner = str(state.get("owner") or "")
        values = {"task_id": task_id, "status": status, "owner": owner, "question": question[:500],
                  "hint": _hint(task_id, status, owner)}
        argv = [arg.format_map(values) for arg in command]
        # own process group: a timeout ends the hook *and* anything it spawned, so a half-finished send cannot
        # complete later and then be repeated by the next retry
        p = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace", start_new_session=True)
        try:
            p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            from .runner import kill_group
            kill_group(p.pid, grace=3)
            p.communicate()
            LOG.warning("notify timed out for %s (%s); its process group was ended; retry next reconcile", task_id, status)
            return None
        result = p
        if result.returncode:
            LOG.warning("notify failed for %s (%s): exit %s; retry next reconcile", task_id, status, result.returncode)
            return None
        record["sent"][key] = {"status": status, "question_hash": question_hash, "at": now()}
        write_json(path, record)
        return {"task_id": task_id, "status": status}


def notify_pending(cfg: dict) -> list[dict]:
    """Best effort: config, command or bookkeeping failures never change reconcile's result."""
    delivered = []
    try:
        settings = cfg.get("notify")
        if settings is None:
            return delivered
        if not isinstance(settings, dict):
            raise ValueError("notify must be an object")
        command = settings.get("command")
        on = settings.get("on", list(DEFAULT_ON))
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            raise ValueError("notify.command must be a nonempty argv list of strings")
        if not isinstance(on, list) or not all(isinstance(s, str) for s in on):
            raise ValueError("notify.on must be a list of status strings")
        for task_id in list_tasks():
            try:
                # Do not create bookkeeping files for tasks that have no pending decision.
                if _status(TaskDir(task_id).state(), on) is None:
                    continue
                item = _notify_one(task_id, command, on)
                if item:
                    delivered.append(item)
            except Exception as exc:
                LOG.warning("notify failed for %s: %s; retry next reconcile", task_id, exc)
    except Exception as exc:
        LOG.warning("notify failed: %s; retry next reconcile", exc)
    return delivered
