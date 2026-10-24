"""Project controllers, epoch fencing and recoverable handoff.

Lock order for task integration: delivery -> schedule -> pool -> project -> task.
Use locked(project_id), then recheck_locked(state, sid, epoch) at the task commit point.
Never call the locking wrappers again while holding the same project lock.
No subprocess, model, tmux, or network operation executes under a project lock.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import os
import re
import secrets
import stat
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


def reclaim_policy(state, cfg=None):
    allowed = (cfg or {}).get("reclaim_dead", [])
    if not isinstance(allowed, list) or any(not isinstance(p, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", p) for p in allowed):
        raise Refused("reclaim_dead must be a list of project ids")
    return "dead" if state.get("reclaim", "none") == "dead" or state["project_id"] in allowed else "none"


def view(state, cfg=None):
    result = copy.deepcopy(state)
    result.setdefault("reclaim", "none")
    result["reclaim_effective"] = reclaim_policy(state, cfg)
    transfer = state["transfers"].get(state["transfer"])
    result["active_transfer"] = ({"transfer_id": transfer["transfer_id"], "from": transfer["from"],
                                  "via": transfer.get("via", "session"),
                                  **({"supervisor": transfer["supervisor"]} if "supervisor" in transfer else {})}
                                 if transfer else None)
    return result


def show(project_id, cfg=None):
    try:
        return view(read_json(path(project_id)), cfg)
    except FileNotFoundError:
        raise Refused(f"project does not exist: {project_id}") from None


def list_projects(cfg=None):
    return [view(read_json(p), cfg) for p in sorted((home() / "projects").glob("*/project.json"))]


def add(project_id, label=None, root=None, claim="open", reclaim="none"):
    if claim not in ("open", "admin"):
        raise Refused("claim policy must be open or admin")
    if reclaim not in ("none", "dead"):
        raise Refused("reclaim policy must be none or dead")
    with locked_json(path(project_id)) as box:
        if box["state"] is not None:
            raise Refused("project id already exists and is immutable")
        box["state"] = {"project_id": project_id, "label": label or "", "root": root,
                        "claim": claim, "reclaim": reclaim, "created_at": now(), "status": "normal",
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


def reclaim(project_id, caller, epoch, cfg=None):
    sid = sessions.require_registered(caller)
    with locked(project_id) as state:
        if reclaim_policy(state, cfg) != "dead":
            raise Refused("project does not allow reclaim (policy is none)")
        old_session = state["controller"]["session_id"]
        if old_session is None:
            raise Refused("reclaim requires an existing project controller")
        live, reason = sessions.liveness_with_reason(sessions.record(old_session))
        if live != "dead":
            raise Refused(f"current controller is {live}, not confirmed dead; reclaim refused")
        if state["status"] != "normal" or state["transfer"] is not None:
            raise Refused("project is transferring; continue with the transfer token or abort before reclaim")
        expect(state, epoch)
        old = snapshot(state)
        move(state, sid, "reclaim")
        event(state, "project.reclaimed", sid, old, old_session=old_session, dead_reason=reason)
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


def read_token_file(filename):
    """Validate the opened inode, never a path checked before a separate open.

    NOFOLLOW rejects links; NONBLOCK makes an accidental FIFO fail the regular
    file check without waiting for a writer. Never include token bytes in errors.
    """
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            def check_file():
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise Refused("token file must be a regular file")
                if info.st_uid != os.getuid():
                    raise Refused("token file must belong to the current uid")
                if stat.S_IMODE(info.st_mode) & ~0o600:
                    raise Refused("token file permissions must be no wider than 0600")
            check_file()
            token = stream.read().strip()
            check_file()
    except Refused:
        raise
    except (OSError, ValueError):
        raise Refused("cannot read token file as a private regular UTF-8 file") from None
    if len(token) < 32 or token.startswith("-"):
        raise Refused("token file must contain at least 32 characters and must not start with '-'")
    return token


def transfer_actor(caller, for_session=None, cfg=None):
    if for_session is not None:
        supervisor = sessions.supervised_session(cfg or {}, for_session)
        return for_session, {"via": "supervisor", "supervisor": supervisor}
    return sessions.require_registered(caller), {}


def begin(project_id, caller, epoch, *, transfer_id=None, token_file=None, for_session=None, cfg=None):
    sid, provenance = transfer_actor(caller, for_session, cfg)
    if (transfer_id is None) != (token_file is None):
        raise ValueError("--transfer and --token-file must be supplied together for begin")
    supplied = transfer_id is not None
    if supplied:
        if not isinstance(transfer_id, str) or not re.fullmatch(r"t-[0-9a-f]{32}", transfer_id):
            raise Refused("transfer id must be t- followed by 32 lowercase hexadecimal characters")
        token = read_token_file(token_file)
    else:
        # A random leading '-' would be parsed as an option after `--token`.
        token = "yg-" + secrets.token_urlsafe(32)
        transfer_id = "t-" + secrets.token_hex(16)
    with locked(project_id) as state:
        # A supervisor must satisfy ALL delegated checks, even for a replay.
        # Ordinary original sessions may read terminal results after losing control.
        if for_session is not None:
            controller(state, sid)
        record = state["transfers"].get(transfer_id)
        if supplied and record is not None:
            if record["from"] != sid or type(epoch) is not int or record["epoch"] != epoch:
                raise Refused("transfer replay requires the original session and epoch")
            check_token(record, token)
            if record["status"] in ("committed", "aborted"):
                return copy.deepcopy(record["result"])
            if record["status"] != "pending" or state["transfer"] != transfer_id:
                raise Refused("transfer is no longer pending or current")
            expect(state, epoch)
            return copy.deepcopy(record.get("begin_result") or {
                "project_id": project_id, "transfer_id": transfer_id,
                "controller": state["controller"], "status": state["status"]})
        if record is not None:
            raise Refused("generated transfer id already exists; retry begin")
        expect(state, epoch)
        normal(state)
        controller(state, sid)
        old = snapshot(state)
        state["transfers"][transfer_id] = {"transfer_id": transfer_id, "status": "pending", "from": sid,
            "epoch": epoch, "begun_at": now(), "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "via": "session", **provenance}
        state.update(status="transferring", transfer=transfer_id, last_transfer_id=transfer_id)
        result = {"project_id": project_id, "transfer_id": transfer_id,
                  "controller": copy.deepcopy(state["controller"]), "status": state["status"]}
        state["transfers"][transfer_id]["begin_result"] = copy.deepcopy(result)
        event(state, "project.transfer_begun", sid, old, transfer_id=transfer_id, **provenance)
    # No plaintext token ever goes into a file or event; this response is emitted once.
    return result if supplied else {**result, "token": token}


def transfer_record(state, transfer_id):
    record = state["transfers"].get(transfer_id)
    if record is None:
        raise Refused("unknown transfer_id")
    return record


def check_token(record, token):
    if not isinstance(token, str) or not hmac.compare_digest(record["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
        raise Refused("invalid transfer token")


def commit(project_id, transfer_id, token=None, caller=None, target=None, *, token_file=None):
    if token_file is not None:
        if token is not None:
            raise ValueError("--token and --token-file are mutually exclusive")
        token = read_token_file(token_file)
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


def abort(project_id, transfer_id, token=None, caller=None, *, token_file=None, for_session=None, cfg=None):
    if for_session is not None and (token is not None or token_file is not None):
        raise ValueError("--for-session is only allowed for abort without a token")
    if token_file is not None:
        if token is not None:
            raise ValueError("--token and --token-file are mutually exclusive")
        token = read_token_file(token_file)
    actor, provenance = transfer_actor(caller, for_session, cfg) if token is None else ("transfer-token", {})
    with locked(project_id) as state:
        record = transfer_record(state, transfer_id)
        if token is not None:
            check_token(record, token)
        else:
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
        event(state, "project.transfer_aborted", actor, old, transfer_id=transfer_id, **provenance)
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
