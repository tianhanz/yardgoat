"""Session/project CLI and explicit legacy-task binding."""
import argparse
import json
import sys

from . import control, projects, runner, sessions
from .store import TaskDir, list_tasks


def caller(args, cfg):
    return sessions.identify(cfg, explicit=getattr(args, "caller_session", None))


def describe(identity):
    rec = sessions.record(identity["session_id"]) if identity["session_id"] else None
    controlled = [p["project_id"] for p in projects.list_projects()
                  if identity["session_id"] and p["controller"]["session_id"] == identity["session_id"]]
    counts = dict.fromkeys(controlled, 0)
    for task_id in list_tasks():
        state = TaskDir(task_id).state()
        project = control.project_of(state)
        if project in counts and state["status"] not in ("verified", "archived", "exhausted"):
            counts[project] += 1
    return {**identity, "registered": rec is not None, "label": rec["label"] if rec else None,
            "projects": controlled, "unfinished_tasks": sum(counts.values()), "project_task_counts": counts}


def execute(a):
    cfg = runner.config()
    if a.cmd == "whoami":
        identity = describe(caller(a, cfg))
        print(json.dumps(identity, ensure_ascii=False, indent=1))
        return 0 if identity["session_id"] else 3
    if a.cmd == "session":
        if a.action == "list":
            result = sessions.list_sessions()
        elif a.pid is not None:
            sessions.require_admin(cfg, a.as_, a.reason)
            result = sessions.register(sessions.identify(cfg, pid=a.pid, exact=True), a.label,
                                       administrator=a.as_, reason=a.reason)
        else:
            result = sessions.register(caller(a, cfg), a.label)
    elif a.action == "bind":
        result = control.bind(TaskDir(a.task_id), a.project_id, a.as_, caller(a, cfg))
    elif a.action == "add":
        result = projects.add(a.project_id, a.label, a.root, a.claim)
    elif a.action == "list":
        result = projects.list_projects()
    elif a.action == "show":
        result = projects.show(a.project_id)
    elif a.action == "claim":
        result = projects.claim(a.project_id, caller(a, cfg), a.expect_epoch)
    elif a.action == "handoff":
        result = projects.handoff(a.project_id, caller(a, cfg), a.to, a.expect_epoch)
    elif a.action == "assign":
        result = projects.assign(a.project_id, a.session, a.expect_epoch, cfg, a.as_, a.reason)
    elif a.transfer_action == "begin":
        result = projects.begin(a.project_id, caller(a, cfg), a.expect_epoch)
    elif a.transfer_action == "commit":
        result = projects.commit(a.project_id, a.transfer, a.token, caller(a, cfg), a.to)
    else:
        result = projects.abort(a.project_id, a.transfer, a.token, caller(a, cfg))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def command(a):
    try:
        return execute(a)
    except sessions.Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError, KeyError) as exc:
        print(f"state/config error: {exc}", file=sys.stderr)
        return 2


def assertion(parser):
    parser.add_argument("--session", dest="caller_session", default=argparse.SUPPRESS,
                        help="assert mechanically identified caller; never impersonate another session")


def register(sub):
    who = sub.add_parser("whoami", help="identify the nearest local agent process")
    assertion(who)
    who.add_argument("--as", dest="as_", help="compatibility label; never used as identity")
    who.add_argument("--json", action="store_true")
    who.set_defaults(handler=command)
    session = sub.add_parser("session")
    actions = session.add_subparsers(dest="action", required=True)
    reg = actions.add_parser("register")
    reg.add_argument("--label")
    reg.add_argument("--pid", type=int)
    reg.add_argument("--as", dest="as_")
    reg.add_argument("--reason")
    ls = actions.add_parser("list")
    for p in (reg, ls):
        assertion(p)
        p.add_argument("--json", action="store_true")
        p.set_defaults(handler=command)
    project = sub.add_parser("project")
    actions = project.add_subparsers(dest="action", required=True)
    for name in ("add", "list", "show", "claim", "handoff", "assign"):
        p = actions.add_parser(name)
        if name != "list":
            p.add_argument("project_id")
        if name == "add":
            p.add_argument("--label")
            p.add_argument("--root")
            p.add_argument("--claim", choices=("open", "admin"), default="open")
        if name in ("claim", "handoff", "assign"):
            p.add_argument("--expect-epoch", required=True, type=int)
        if name == "handoff":
            p.add_argument("--to", required=True)
        if name == "assign":
            p.add_argument("--session", required=True, help="target session (caller assertion precedes project)")
            p.add_argument("--as", dest="as_", required=True)
            p.add_argument("--reason", required=True)
        else:
            assertion(p)
            p.add_argument("--as", dest="as_", help="compatibility label; never grants project control")
        p.add_argument("--json", action="store_true")
        p.set_defaults(handler=command)
    bind = actions.add_parser("bind", help="bind one legacy task to the project controller role")
    bind.add_argument("task_id")
    bind.add_argument("--project", dest="project_id", required=True)
    bind.add_argument("--as", dest="as_", required=True)
    assertion(bind)
    bind.set_defaults(handler=command)
    transfer = actions.add_parser("transfer")
    moves = transfer.add_subparsers(dest="transfer_action", required=True)
    for name in ("begin", "commit", "abort"):
        p = moves.add_parser(name)
        p.add_argument("project_id")
        assertion(p)
        if name == "begin":
            p.add_argument("--expect-epoch", required=True, type=int)
        else:
            p.add_argument("--transfer", required=True)
            p.add_argument("--token", required=name == "commit")
        if name == "commit":
            p.add_argument("--to")
        p.add_argument("--json", action="store_true")
        p.set_defaults(handler=command)
