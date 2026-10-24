"""Optional machine-local argv hook for tasks that need a decision. Never prints to stdout."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import shlex
import subprocess
from string import Formatter

from . import projects, sessions
from .statefile import locked_json

from .store import TaskDir, home, list_tasks, now, read_json, write_json

DEFAULT_ON = ("parked", "exhausted", "submit_unknown", "verify_error", "provider_refused")
LOG = logging.getLogger(__name__)
CONTROLLER_FIELDS = {"controller", "controller_session", "endpoint"}
FIELDS = {"task_id", "status", "owner", "question", "hint"} | CONTROLLER_FIELDS


def template_fields(template):
    fields = set()
    for _, field, format_spec, _ in Formatter().parse(template):
        if field is not None:
            if field not in FIELDS:
                raise ValueError(f"unknown notification placeholder: {field!r}")
            fields.add(field)
            fields.update(template_fields(format_spec))
    template.format_map(dict.fromkeys(FIELDS, ""))
    return fields


def controller_values(state, *, require_endpoint=False):
    values = dict.fromkeys(CONTROLLER_FIELDS, "")
    project = (state.get("owner_ref") or {}).get("project")
    if not project:
        return values
    ps = projects.show(project)
    sid = ps["controller"]["session_id"]
    rec = sessions.record(sid)
    if not rec or sessions.liveness(rec) != "alive":
        return None
    values.update(controller=rec["label"], controller_session=sid)
    endpoint = rec.get("endpoint") or {}
    verified = endpoint.get("verified") and endpoint.get("socket") and endpoint.get("pane_id")
    if require_endpoint and not verified:
        return None
    if verified:
        values["endpoint"] = json.dumps({k: endpoint[k] for k in ("socket", "pane_id")},
                                       ensure_ascii=False, separators=(",", ":"))
    return values



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


def _notify_one(task_id, command, on, *, name="default", recipient=None, legacy=True, errors=None,
                controller_route=False, require_endpoint=False, config_error=None):
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
        if controller_route:
            current = controller_values(state, require_endpoint=require_endpoint)
            if current is None:
                return None  # no guessed endpoint and no sent key; try next reconcile
            values.update(current)
        else:
            values.update(dict.fromkeys(CONTROLLER_FIELDS, ""))
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
        fields, problem = set(), None
        try:
            for template in [*command, item.get("recipient", "")]:
                fields.update(template_fields(template))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            problem = str(exc)
        routes.append({"controller_route": bool(fields & CONTROLLER_FIELDS),
                       "require_endpoint": "endpoint" in fields, "config_error": problem,
                       "name": name, "command": command, "on": on, "recipient": item.get("recipient"),
                       "legacy": single and "recipient" not in item})
    return routes


def configuration_errors(cfg):
    """Read-only diagnostics; fixed templates take effect without a reset command."""
    if cfg.get("notify") is None:
        return []
    try:
        return [{"route": r["name"], "error": r["config_error"], "disabled": True}
                for r in _routes(cfg["notify"]) if r["config_error"]]
    except ValueError as exc:
        return [{"route": None, "error": str(exc), "disabled": True}]


def record_config_errors(cfg, problems):
    # One warning per unchanged invalid configuration, across CLI invocations.
    signature = hashlib.sha256(json.dumps(cfg.get("notify"), sort_keys=True).encode()).hexdigest()
    with locked_json(home() / "notify-config-errors.json") as box:
        if box["state"] == {"signature": signature, "errors": problems}:
            return
        box["state"] = {"signature": signature, "errors": problems}
        for problem in problems:
            LOG.warning("notify %s configuration error (route disabled): %s", problem["route"], problem["error"])


def notify_pending(cfg: dict, errors=None) -> list[dict]:
    """Best effort: config, command or bookkeeping failures never change reconcile's result."""
    delivered = []
    try:
        settings = cfg.get("notify")
        if settings is None:
            return delivered
        routes = _routes(settings)
        problems = configuration_errors(cfg)
        if problems or (home() / "notify-config-errors.json").exists():
            record_config_errors(cfg, problems)
        if errors is not None:
            errors.extend(f"notify {p['route']}: configuration error: {p['error']}" for p in problems)
        routes = [r for r in routes if not r["config_error"]]
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
