"""Optional machine-local argv hook for tasks that need a decision. Never prints to stdout."""
from __future__ import annotations

import fcntl
import hashlib
import json
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


def _notify_one(task_id, command, on, *, name="default", recipient=None, legacy=True, errors=None):
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
        owner = str(state.get("owner") or "")
        values = {"task_id": task_id, "status": status, "owner": owner, "question": question[:500],
                  "hint": _hint(task_id, status, owner)}
        destination = recipient.format_map(values) if recipient is not None else ""
        # Preserve old single-object records byte for byte. JSON framing keeps route names
        # and destinations containing colons/braces from colliding in the new key space.
        key = (f"{status}:{question_hash}" if legacy else
               "v2:" + json.dumps([name, status, question_hash, destination], ensure_ascii=False, separators=(",", ":")))
        path = directory / f"{task_id}.json"
        record = read_json(path) if path.exists() else {"task_id": task_id, "sent": {}}
        if key in record["sent"]:
            return None
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
            LOG.warning("notify %s timed out for %s (%s); its process group was ended; retry next reconcile", name, task_id, status)
            if errors is not None:
                errors.append(f"notify {name}: {task_id} timed out")
            return None
        result = p
        if result.returncode:
            LOG.warning("notify %s failed for %s (%s): exit %s; retry next reconcile", name, task_id, status, result.returncode)
            if errors is not None:
                errors.append(f"notify {name}: {task_id} exited {result.returncode}")
            return None
        record["sent"][key] = {"status": status, "question_hash": question_hash, "at": now()}
        if not legacy:
            record["sent"][key].update(name=name, recipient=destination)
        write_json(path, record)
        return {"task_id": task_id, "status": status, **({} if legacy else {"name": name, "recipient": destination})}


def _routes(settings):
    """Validate the entire configuration before any route can send."""
    single = isinstance(settings, dict)
    if not single and not isinstance(settings, list):
        raise ValueError("notify must be an object or a list of objects")
    routes, names = [], set()
    for item in [settings] if single else settings:
        if not isinstance(item, dict):
            raise ValueError("notify entries must be objects")
        name = item.get("name", "default")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("notify.name must be a nonempty string")
        if name in names:
            raise ValueError("notify names must be unique; duplicate name: " + name)
        names.add(name)
        command, on = item.get("command"), item.get("on", list(DEFAULT_ON))
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            raise ValueError("notify.command must be a nonempty argv list of strings")
        if not isinstance(on, list) or not all(isinstance(s, str) for s in on):
            raise ValueError("notify.on must be a list of status strings")
        if "recipient" in item and not isinstance(item["recipient"], str):
            raise ValueError("notify.recipient must be a string template")
        routes.append({"name": name, "command": command, "on": on, "recipient": item.get("recipient"),
                       "legacy": single and "recipient" not in item})
    return routes


def notify_pending(cfg: dict, errors=None) -> list[dict]:
    """Best effort: config, command or bookkeeping failures never change reconcile's result."""
    delivered = []
    try:
        settings = cfg.get("notify")
        if settings is None:
            return delivered
        routes = _routes(settings)
        for task_id in list_tasks():
            for route in routes:
                try:
                    # Do not create bookkeeping files for tasks that have no pending decision.
                    if _status(TaskDir(task_id).state(), route["on"]) is None:
                        continue
                    item = _notify_one(task_id, **route, errors=errors)
                    if item:
                        delivered.append(item)
                except Exception as exc:
                    LOG.warning("notify %s failed for %s: %s; retry next reconcile", route["name"], task_id, exc)
                    if errors is not None:
                        errors.append(f"notify {route['name']}: {task_id}: {exc}")
    except Exception as exc:
        LOG.warning("notify failed: %s; retry next reconcile", exc)
        if errors is not None:
            errors.append(f"notify: {exc}")
    return delivered
