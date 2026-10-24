"""Project controllers, epoch fencing and recoverable handoff. No task permissions yet.

Lock order for future task integration: delivery -> schedule -> pool -> project -> task.
Use locked(project_id), then recheck_locked(state, sid, epoch) at the task commit point.
Never call the locking wrappers again while holding the same project lock.
No subprocess, model, tmux, or network operation executes under a project lock.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import re
import secrets
from contextlib import contextmanager

from . import sessions
from .sessions import Refused
from .statefile import locked_json
from .store import home, now, read_json


def path(project_id):
    if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", project_id):
        raise Refused("invalid project id")
    return home() / "projects" / project_id / "project.json"


@contextmanager
def locked(project_id):
    with locked_json(path(project_id)) as box:
        if box["state"] is None:
            raise Refused(f"project does not exist: {project_id}")
        yield box["state"]


def show(project_id):
    try:
        return read_json(path(project_id))
    except FileNotFoundError:
        raise Refused(f"project does not exist: {project_id}") from None


def list_projects():
    return [read_json(p) for p in sorted((home() / "projects").glob("*/project.json"))]


def add(project_id, label=None, root=None, claim="open"):
    if claim not in ("open", "admin"):
        raise Refused("claim policy must be open or admin")
    with locked_json(path(project_id)) as box:
        if box["state"] is not None:
            raise Refused("project id already exists and is immutable")
        box["state"] = {"project_id": project_id, "label": label or "", "root": root,
                        "claim": claim, "created_at": now(), "status": "normal",
                        "controller": {"session_id": None, "epoch": 0, "since": None, "how": None},
                        "transfer": None, "last_transfer_id": None, "transfers": {}, "history": []}
        result = box["state"]
    return result


def expect(state, epoch):
    if type(epoch) is not int or epoch != state["controller"]["epoch"]:
        raise Refused(f"epoch conflict: expected {epoch}, current {state['controller']['epoch']}")


def normal(state):
    if state["transfer"] is not None:
        raise Refused("project is transferring (交接中)")


def controller(state, sid):
    if state["controller"]["session_id"] != sid:
        raise Refused("caller session is not the project controller")


def event(state, kind, actor, old, reason="", **extra):
    # The history is append-only within the one atomically replaced document.
    state["history"].append({"sequence": len(state["history"]) + 1, "at": now(), "kind": kind,
                             "actor": actor, "old_controller": old["controller"],
                             "new_controller": copy.deepcopy(state["controller"]),
                             "old_status": old["status"], "new_status": state["status"],
                             "old_transfer": old["transfer"], "reason": reason, **extra})


def snapshot(state):
    return copy.deepcopy({k: state[k] for k in ("controller", "status", "transfer")})


def move(state, sid, how):
    state["controller"] = {"session_id": sid, "epoch": state["controller"]["epoch"] + 1,
                           "since": now(), "how": how}


def claim(project_id, caller, epoch):
    sid = sessions.require_registered(caller)
    with locked(project_id) as state:
        expect(state, epoch)
        normal(state)
        if state["claim"] != "open" or epoch != 0 or state["controller"]["session_id"] is not None:
            raise Refused("claim requires open policy and a project that has never had a controller")
        old = snapshot(state)
        move(state, sid, "claim")
        event(state, "project.claimed", sid, old)
    return state


def handoff(project_id, caller, target, epoch):
    sid = sessions.require_registered(caller)
    with locked(project_id) as state:
        expect(state, epoch)
        normal(state)
        controller(state, sid)
        sessions.require_target(target)
        old = snapshot(state)
        move(state, target, "handoff")
        event(state, "project.handed_off", sid, old)
    return state


def begin(project_id, caller, epoch):
    sid = sessions.require_registered(caller)
    token = secrets.token_urlsafe(32)
    transfer_id = "t-" + secrets.token_hex(16)
    with locked(project_id) as state:
        expect(state, epoch)
        normal(state)
        controller(state, sid)
        old = snapshot(state)
        state["transfers"][transfer_id] = {"transfer_id": transfer_id, "status": "pending", "from": sid,
            "epoch": epoch, "begun_at": now(), "token_hash": hashlib.sha256(token.encode()).hexdigest()}
        state.update(status="transferring", transfer=transfer_id, last_transfer_id=transfer_id)
        event(state, "project.transfer_begun", sid, old, transfer_id=transfer_id)
    # No plaintext token ever goes into a file or event; this response is emitted once.
    return {"project_id": project_id, "transfer_id": transfer_id, "token": token,
            "controller": state["controller"], "status": state["status"]}


def transfer_record(state, transfer_id):
    record = state["transfers"].get(transfer_id)
    if record is None:
        raise Refused("unknown transfer_id")
    return record


def check_token(record, token):
    if not isinstance(token, str) or not hmac.compare_digest(record["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
        raise Refused("invalid transfer token")


def commit(project_id, transfer_id, token, caller=None, target=None):
    # An explicit target uses the bearer token; otherwise the caller must be recognized.
    # Both paths require a live registered target. The CLI separately enforces assertions.
    if target is None:
        target = sessions.require_registered(caller)
    with locked(project_id) as state:
        record = transfer_record(state, transfer_id)
        check_token(record, token)
        if record["status"] == "committed":
            if (state["last_transfer_id"] != transfer_id or state["transfer"] is not None
                    or record["result"]["controller"] != state["controller"]):
                raise Refused("stale transfer: a later handoff has occurred")
            if record["result"]["controller"]["session_id"] != target:
                raise Refused("transfer already committed to a different session")
            # A repeated result does not perform another assignment (even if target later died).
            return record["result"]
        if record["status"] != "pending" or state["transfer"] != transfer_id:
            raise Refused("transfer is no longer pending")
        expect(state, record["epoch"])
        sessions.require_target(target)
        old = snapshot(state)
        move(state, target, "transfer")
        state.update(status="normal", transfer=None)
        result = {"project_id": project_id, "transfer_id": transfer_id, "result": "committed",
                  "controller": copy.deepcopy(state["controller"])}
        record.update(status="committed", completed_at=now(), result=result)
        actor = caller.get("session_id") if caller else None
        event(state, "project.transfer_committed", actor or "transfer-token", old, transfer_id=transfer_id)
    return result


def abort(project_id, transfer_id, token=None, caller=None):
    with locked(project_id) as state:
        record = transfer_record(state, transfer_id)
        if token is not None:
            check_token(record, token)
            actor = "transfer-token"
        else:
            actor = sessions.require_registered(caller)
            controller(state, actor)
        if (record["status"] == "aborted" and state["last_transfer_id"] == transfer_id
                and state["controller"]["epoch"] == record["epoch"]):
            return record["result"]
        if record["status"] != "pending" or state["transfer"] != transfer_id:
            raise Refused("transfer is no longer pending")
        old = snapshot(state)
        state.update(status="normal", transfer=None)
        result = {"project_id": project_id, "transfer_id": transfer_id, "result": "aborted",
                  "controller": copy.deepcopy(state["controller"])}
        record.update(status="aborted", completed_at=now(), result=result)
        event(state, "project.transfer_aborted", actor, old, transfer_id=transfer_id)
    return result


def assign(project_id, target, epoch, cfg, administrator, reason):
    sessions.require_admin(cfg, administrator, reason)
    with locked(project_id) as state:
        expect(state, epoch)
        sessions.require_target(target)
        old = snapshot(state)
        if state["transfer"]:
            state["transfers"][state["transfer"]].update(status="cancelled", completed_at=now())
        state.update(status="normal", transfer=None)
        move(state, target, "assign")
        event(state, "project.assigned", administrator, old, reason)
    return state


def authorize_control_locked(state, caller):
    sid = sessions.require_registered(caller)
    normal(state)
    controller(state, sid)
    return sid, state["controller"]["epoch"]


def authorize_control(project_id, caller):
    """caller is an identify() result, not an arbitrary label or session-id string."""
    with locked(project_id) as state:
        return authorize_control_locked(state, caller)


def recheck_locked(state, session_id, epoch):
    """Keep this project lock held through the subsequent task commit."""
    normal(state)
    expect(state, epoch)
    controller(state, session_id)
    sessions.require_target(session_id)
    return session_id, epoch


def recheck(project_id, session_id, epoch):
    """Point-in-time check. For an atomic task commit use locked + recheck_locked."""
    with locked(project_id) as state:
        return recheck_locked(state, session_id, epoch)
