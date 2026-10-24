"""Task control tickets. Labels never authorize a project-owned task.

Identify before waiting for locks. Commit under project -> task and fence the
original epoch, including ABA. System continuation is an explicit in-process
context, never inferred from --as, environment, or a task's display owner.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import fcntl
import sys

from . import projects, sessions

_ticket = ContextVar("task_control_ticket", default=None)
_system = ContextVar("task_system_executor", default=False)


def project_of(state):
    return (state.get("owner_ref") or {}).get("project")


@contextmanager
def operation(project, caller=None):
    if not project or _system.get():
        yield
        return
    current = _ticket.get()
    if current and current[0] == project:
        yield  # nested steps retain the ORIGINAL epoch
        return
    if caller is None:
        from . import runner
        caller = sessions.identify(runner.config())
    sid, epoch = projects.authorize_control(project, caller)
    token = _ticket.set((project, sid, epoch))
    try:
        yield
    finally:
        _ticket.reset(token)


def guard(*, command=False, close=False):
    """Public control entry points; the inner locked() is the commit fence."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(first, *args, **kwargs):
            from .store import TaskDir
            td = TaskDir(first.task_id) if command else first
            try:
                state = td.state() if td.exists else {}
                with operation(project_of(state)):
                    return fn(first, *args, **kwargs)
            except sessions.Refused as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return (3, None) if close else 3
        return wrapped
    return decorate


@contextmanager
def system():
    token = _system.set(True)
    try:
        yield
    finally:
        _system.reset(token)


def as_reconciler(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with system():
            return fn(*args, **kwargs)
    return wrapped


def allowed(state, who):
    project = project_of(state)
    if not project:
        return state["owner"] == who
    ticket = _ticket.get()
    return _system.get() or bool(ticket and ticket[0] == project)


def actor(state, who):
    return "reconciler" if project_of(state) and _system.get() else who


@contextmanager
def locked(td, project=None):
    """Acquire project before task. No external work inside this short section.

    project is supplied only for submission/binding (the state has no role yet).
    Re-read owner_ref under the task lock so a concurrent bind cannot bypass us.
    """
    expected = project or (project_of(td.state()) if td.exists else None)
    if expected and not _system.get():
        ticket = _ticket.get()
        if not ticket or ticket[0] != expected:
            raise sessions.Refused("project control requires a current session ticket")
        with projects.locked(expected) as ps:
            projects.recheck_locked(ps, *ticket[1:])
            with td.locked() as box:
                actual = project_of(box["state"] or {})
                if actual and actual != expected:
                    raise sessions.Refused("task project changed during control operation")
                yield box
    else:
        with td.locked() as box:
            actual = project_of(box["state"] or {})
            if actual and not _system.get():
                raise sessions.Refused("task was bound during control operation; retry as its controller")
            yield box


def audit(state):
    project = project_of(state)
    if not project:
        return {}
    if _system.get():
        return {"actor": "reconciler", "on_behalf_of": {
            "project": project, "epoch": projects.show(project)["controller"]["epoch"]}}
    ticket = _ticket.get()
    if ticket and ticket[0] == project:
        return {"actor_session": ticket[1], "epoch": ticket[2]}
    return {}


def event_fields(td):
    return audit(td.state()) if td.exists else {}


def current_label(project):
    sid = projects.show(project)["controller"]["session_id"]
    record = sessions.record(sid)
    return record["label"] if record else ""


def owner_label(project):
    ticket = _ticket.get()
    if not ticket or ticket[0] != project:
        raise sessions.Refused("submission requires a project control ticket")
    return sessions.record(ticket[1])["label"]


@contextmanager
def verification_lock(td):
    # Serializes slow verifiers without blocking project handoff. Taken before
    # project/task locks; never used by operations that hold those locks.
    with (td.path / ".verify.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def view(state):
    project = project_of(state)
    if not project:
        return {}
    ps = projects.show(project)
    controller = dict(ps["controller"])
    rec = sessions.record(controller["session_id"])
    controller.update(label=rec["label"] if rec else "", liveness=sessions.liveness(rec) if rec else "unknown")
    return {"owner_ref": state["owner_ref"], "project": project, "controller": controller,
            "transferring": ps["status"] == "transferring"}


def describe(row):
    if not row.get("owner_ref"):
        return ""
    return (f"  project={row['project']} controller={row['controller']['label']} "
            f"epoch={row['controller']['epoch']} transferring={row['transferring']}")


def bind(td, project, who, caller):
    from . import pool
    if not td.exists:
        raise sessions.Refused(f"unknown task {td.task_id}")
    with operation(project, caller), pool.schedule_lock(), locked(td, project) as box:
        st = box["state"]
        if st["owner"] != who:
            raise sessions.Refused(f"owner is {st['owner']}, not {who}")
        if st.get("owner_ref"):
            raise sessions.Refused("task is already project-owned; use project handoff")
        st["owner_ref"] = {"project": project}
        td.event("task.project_bound", who, owner_ref=st["owner_ref"], **audit(st))
    return {"task_id": td.task_id, "result": "project-bound", "owner_ref": st["owner_ref"]}
