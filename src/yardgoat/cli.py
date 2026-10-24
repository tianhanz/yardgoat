"""yardgoat CLI.

  yardgoat submit <spec.json|yaml> --as <controller>     accept a task (idempotent on identical content)
  yardgoat dispatch <task_id> --as <controller>          start a run; refuses duplicates, non-owners, drift,
                                                         lanes outside the allowlist, exhausted budgets
  yardgoat status [<task_id>] [--json]                   read-only: owner, state, runs, liveness, receipts
  yardgoat takeover <task_id> --as <new> --from <old>    hand ownership to another controller (works mid-run)
  yardgoat close <task_id> --as <controller>             run the frozen verifier against the latest exited run
  yardgoat park <task_id> --as <controller> --question Q  stop and ask a human (also automatic: worker writes out/BLOCKED.md)
  yardgoat inbox [--json]                                parked tasks and their questions
  yardgoat answer <task_id> --as <who> (--text T | --file F)   answer a parked task; reconcile resumes it
  yardgoat reconcile                                     poll adapter runs, mark lost runs, resume answered tasks
  yardgoat archive <task_id> --as <controller> --reason R  close out a task that will not continue (kept, hidden from inbox)
  yardgoat quick "<card text>" --verify CMD --as <who> [--pool P | --host H] [...]   one-line submit + dispatch
  yardgoat cancel <task_id> --as <controller>            cancel a queued run
  yardgoat events <task_id>                              print the event log

Direct runs are capped by the local config "max_concurrent" (default unlimited). Beyond the cap a dispatch is
accepted as "queued" and `reconcile` starts it when a slot frees up, oldest first.

Exit codes: 0 ok · 3 refused by policy (nothing changed) · 2 usage/spec error · 1 verifier FAIL.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import adapters, contract, runner
from .store import TaskDir, list_tasks, now, write_json

REFUSED = 3
COUNTED = ("exited", "failed", "verified", "lost", "verify_error")  # blocked / launch_failed don't use the budget


def refuse(msg: str) -> int:
    print(f"refused: {msg}", file=sys.stderr)
    return REFUSED


def adapter_kind(spec: dict) -> str:
    return spec.get("adapter", {}).get("kind", "direct")


def cmd_submit(a) -> int:
    try:
        spec = contract.load_spec_file(a.spec)
        frozen = contract.freeze(spec)
    except (contract.SpecError, OSError, ValueError) as e:
        print(f"spec error: {e}", file=sys.stderr)
        return 2
    h = contract.content_hash(spec, frozen)
    td = TaskDir(spec["task_id"])
    with td.locked() as box:
        if box["state"] is not None:
            if box["state"]["content_hash"] == h:
                print(json.dumps({"task_id": td.task_id, "result": "already-accepted", "status": box["state"]["status"]}))
                return 0
            return refuse(f"{td.task_id} already exists with different content (task ids are immutable)")
        write_json(td.path / "spec.json", spec)
        box["state"] = {"task_id": td.task_id, "content_hash": h, "frozen": frozen, "owner": a.as_,
                        "owner_history": [], "status": "accepted", "runs": [], "verdict": None,
                        "question": None, "accepted_at": now()}
    td.event("task.accepted", a.as_, content_hash=h)
    print(json.dumps({"task_id": td.task_id, "result": "accepted", "owner": a.as_}))
    return 0


def _alive(run: dict) -> bool:
    if run.get("mode") == "file-pool":
        return run["state"] == "running"
    return runner.pid_alive(run.get("worker_pid")) or runner.pid_alive(run.get("pid"))


def _active_run(st: dict):
    last = st["runs"][-1] if st["runs"] else None
    if last and last["state"] == "queued":
        return last
    return last if last and last["state"] == "running" and _alive(last) else None


def direct_slots_in_use() -> int:
    n = 0
    for t in list_tasks():
        st = TaskDir(t).state()
        last = st["runs"][-1] if st["runs"] else None
        if last and last["state"] == "running" and last.get("mode") in ("tmux", "process") and _alive(last):
            n += 1
    return n


def lane_problem(spec: dict, cfg: dict) -> str | None:
    lane = spec["executor"].get("lane", "")
    if adapter_kind(spec) == "file-pool":
        lane = cfg.get("adapters", {}).get("file-pool", {}).get("pools", {}).get(spec["adapter"]["pool"], {}).get("lane", lane)
    allowed = spec.get("allowed_lanes")
    if allowed is not None and lane not in allowed:
        return f"lane {lane!r} is not in this task's allowed_lanes {allowed}"
    if lane and lane in cfg.get("forbidden_lanes", []):
        return f"lane {lane!r} is forbidden on this machine (config forbidden_lanes)"
    return None


def do_dispatch(td: TaskDir, who: str, actor_note: str = "", echo: bool = True) -> int:
    """Accept a new attempt: start it now, or queue it when the direct-run cap is reached."""
    cfg = runner.config()
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != who:
            return refuse(f"owner is {st['owner']}, not {who}")
        if st["status"] in ("verified", "exhausted", "archived"):
            return refuse(f"task is {st['status']}")
        if st["status"] == "parked":
            return refuse("task is parked waiting for an answer; use `yardgoat answer`")
        act = _active_run(st)
        if act:
            if echo:
                print(json.dumps({"task_id": td.task_id, "result": "already-" + act["state"], "run_id": act["run_id"]}))
            return REFUSED
        if st["runs"] and st["runs"][-1]["state"] == "exited":
            return refuse(f"{st['runs'][-1]['run_id']} exited but is not verified yet; run `yardgoat close` first")
        if st["runs"] and st["runs"][-1]["state"] == "running":  # recorded running but the process is gone
            st["runs"][-1]["state"] = "lost"
        spec = td.spec()
        problem = _launch_problem(td, st, spec, cfg)
        if problem:
            return refuse(problem)
        used = sum(r["state"] in COUNTED for r in st["runs"])
        if used >= spec["budget"]["max_attempts"]:
            st["status"] = "exhausted"
            return refuse("attempt budget exhausted; escalate to a human")
        run_id = f"r{len(st['runs']) + 1}"
        td.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        st["runs"].append({"run_id": run_id, "state": "queued", "dispatched_by": who, "queued_at": now(), "mode": "queued"})
        st["status"] = "queued"
        st["question"] = None
        cap = cfg.get("max_concurrent")
        must_wait = adapter_kind(spec) == "direct" and cap is not None and direct_slots_in_use() >= int(cap)
    if must_wait:
        td.event("run.queued", who, run_id=run_id, cap=cap, note=actor_note)
        if echo:
            print(json.dumps({"task_id": td.task_id, "result": "queued", "run_id": run_id,
                              "note": f"{cap} direct runs already in flight; reconcile starts it when a slot frees"}))
        return 0
    return start_queued(td, who, actor_note, echo)


def _launch_problem(td: TaskDir, st: dict, spec: dict, cfg: dict) -> str | None:
    d = contract.drift(st["frozen"])
    if d:
        return f"frozen files changed since submit: {d}"
    return lane_problem(spec, cfg)


def start_queued(td: TaskDir, actor: str, actor_note: str = "", echo: bool = True) -> int:
    """Launch the task's queued run (re-checking drift and lane policy at launch time)."""
    cfg = runner.config()
    with td.locked() as box:
        st = box["state"]
        run = st["runs"][-1] if st["runs"] else None
        if not run or run["state"] != "queued":
            return refuse("no queued run")
        spec = td.spec()
        problem = _launch_problem(td, st, spec, cfg)
        if problem:
            run["state"] = "launch_failed"; run["reason"] = problem; st["status"] = "accepted"
            return refuse(problem)
        run.update(state="running", started_at=now())
        st["status"] = "running"
        try:
            run.update(adapters.get(adapter_kind(spec), cfg).submit(td, run["run_id"], spec, cfg))
        except Exception as e:  # a failed launch leaves no running claim behind
            run["state"] = "launch_failed"; run["reason"] = str(e)[:300]
            st["status"] = "accepted"
            print(f"launch failed: {e}", file=sys.stderr)
            return 2
        run_id = run["run_id"]
    td.event("run.dispatched", actor, run_id=run_id, mode=run["mode"], window=run.get("window"), note=actor_note)
    if echo:
        print(json.dumps({"task_id": td.task_id, "result": "dispatched", "run_id": run_id,
                          "mode": run["mode"], "window": run.get("window"), "pid": run.get("pid")}))
    return 0


def cmd_dispatch(a) -> int:
    td = TaskDir(a.task_id)
    return do_dispatch(td, a.as_) if td.exists else refuse(f"unknown task {a.task_id}")


def _status_obj(td: TaskDir) -> dict:
    st = td.state()
    runs = []
    for r in st["runs"]:
        rr = dict(r)
        rr["alive"] = _alive(r) if r["state"] == "running" else False
        rec = td.run_dir(r["run_id"]) / "receipt.json"
        rr["receipt"] = json.loads(rec.read_text()) if rec.exists() else None
        runs.append(rr)
    return {"task_id": td.task_id, "status": st["status"], "owner": st["owner"], "owner_history": st["owner_history"],
            "verdict": st["verdict"], "question": st.get("question"), "runs": runs, "drift": contract.drift(st["frozen"])}


def cmd_status(a) -> int:
    ids = [a.task_id] if a.task_id else list_tasks()
    out = [_status_obj(TaskDir(t)) for t in ids if TaskDir(t).exists]
    if a.task_id and not out:
        return refuse(f"unknown task {a.task_id}")
    if a.json:
        print(json.dumps(out if not a.task_id else out[0], ensure_ascii=False, indent=1))
        return 0
    for o in out:
        print(f"{o['task_id']}: {o['status']}  owner={o['owner']}  verdict={(o['verdict'] or {}).get('verdict')}")
        if o["question"]:
            print(f"  QUESTION: {o['question']['text'][:200]}")
        for r in o["runs"]:
            rec = r["receipt"]
            tail = f" exit={rec['exit_code']} model={rec['actual_model'] or '?'}" if rec else ""
            print(f"  {r['run_id']}: {r['state']} alive={r['alive']} by={r['dispatched_by']}{tail}")
        if o["drift"]:
            print(f"  DRIFT: {o['drift']}")
    return 0


def cmd_takeover(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != a.from_:
            return refuse(f"current owner is {st['owner']}, not {a.from_}")
        st["owner_history"].append({"from": a.from_, "to": a.as_, "at": now(),
                                    "run_in_flight": (_active_run(st) or {}).get("run_id")})
        st["owner"] = a.as_
    td.event("task.owner_changed", a.as_, previous=a.from_)
    print(json.dumps({"task_id": td.task_id, "result": "owner-changed", "from": a.from_, "to": a.as_}))
    return 0


def cmd_close(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != a.as_:
            return refuse(f"owner is {st['owner']}, not {a.as_}")
        last = st["runs"][-1] if st["runs"] else None
        if not last or last["state"] != "exited":
            return refuse("no exited run waiting for verification")
        d = contract.drift(st["frozen"])
        if d:
            return refuse(f"frozen files changed since submit: {d}")
        spec = td.spec()
        rd = td.run_dir(last["run_id"])
        env = {**os.environ, "YARDGOAT_TASK_ID": td.task_id, "YARDGOAT_RUN_DIR": str(rd)}
        try:
            p = subprocess.run(spec["verifier"]["command"], shell=True, cwd=spec["verifier"]["cwd"],
                               capture_output=True, text=True, timeout=1800, env=env)
            rc = p.returncode
            (rd / "verify.out").write_text(p.stdout + ("\n--- stderr ---\n" + p.stderr if p.stderr else ""))
        except subprocess.TimeoutExpired:
            rc = 124
        verdict = {0: "PASS", 1: "FAIL"}.get(rc, "ERROR")
        checked = now()
        rec_p = rd / "receipt.json"
        rec = json.loads(rec_p.read_text())
        rec["verifier"] = {"command": spec["verifier"]["command"], "exit_code": rc, "result": verdict,
                           "checked_at": checked, "checked_by": a.as_}
        if verdict == "PASS":
            rec["status"] = "passed"; last["state"] = "verified"; st["status"] = "verified"
        elif verdict == "FAIL":
            rec["status"] = "failed"; last["state"] = "failed"
            used = sum(r["state"] in COUNTED for r in st["runs"])
            st["status"] = "failed" if used < spec["budget"]["max_attempts"] else "exhausted"
        else:  # a crashing verifier is not a failing deliverable; leave the run waiting for a human
            rec["status"] = "verifier_error"
        write_json(rec_p, rec)
        st["verdict"] = {"run_id": last["run_id"], "verdict": verdict, "by": a.as_, "at": checked}
    td.event("run.verified", a.as_, run_id=last["run_id"], verdict=verdict)
    print(json.dumps({"task_id": td.task_id, "run_id": last["run_id"], "verdict": verdict}))
    return 0 if verdict == "PASS" else (1 if verdict == "FAIL" else 2)


def cmd_park(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != a.as_:
            return refuse(f"owner is {st['owner']}, not {a.as_}")
        if _active_run(st):
            return refuse("a run is in flight; park after it ends")
        st["status"] = "parked"
        st["question"] = {"run_id": None, "text": a.question, "at": now(), "by": a.as_}
    td.event("task.parked", a.as_)
    print(json.dumps({"task_id": td.task_id, "result": "parked"}))
    return 0


def cmd_answer(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    text = a.text if a.text is not None else Path(a.file).read_text(encoding="utf-8")
    if not text.strip():
        return refuse("empty answer")
    with td.locked() as box:
        st = box["state"]
        if st["status"] != "parked":
            return refuse(f"task is {st['status']}, not parked")
        d = td.path / "answers"
        d.mkdir(exist_ok=True)
        n = len(list(d.glob("*.md"))) + 1
        (d / f"{n:03d}.md").write_text(f"(answered by {a.as_} at {now()})\n\n{text.strip()}\n", encoding="utf-8")
        st["status"] = "answered"
        st["question"]["answered_by"] = a.as_
    td.event("task.answered", a.as_, answer=f"{n:03d}")
    print(json.dumps({"task_id": td.task_id, "result": "answered", "note": "reconcile resumes it on behalf of the owner"}))
    return 0


def cmd_inbox(a) -> int:
    items = []
    for t in list_tasks():
        st = TaskDir(t).state()
        if st["status"] in ("parked", "exhausted") or (st["runs"] and st["runs"][-1]["state"] == "verify_error"):
            items.append({"task_id": t, "status": st["status"], "owner": st["owner"],
                          "question": (st.get("question") or {}).get("text")})
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
    else:
        for i in items:
            print(f"{i['task_id']} [{i['status']}] owner={i['owner']}\n  {i['question'] or '(no question recorded)'}")
    return 0


def cmd_reconcile(a) -> int:
    cfg = runner.config()
    report = []
    for t in list_tasks():
        td = TaskDir(t)
        st = td.state()
        spec = td.spec()
        last = st["runs"][-1] if st["runs"] else None
        if last and last["state"] == "running":
            try:
                outcome = adapters.get(adapter_kind(spec), cfg).poll(td, last, spec, cfg)
            except Exception as e:
                report.append({"task_id": t, "error": repr(e)}); continue
            if outcome:
                runner.record_outcome(td, last["run_id"], outcome)
                report.append({"task_id": t, "run_id": last["run_id"], "observed": outcome["state"]})
        elif st["status"] == "answered":
            st2 = td.state()
            with td.locked() as box:
                box["state"]["status"] = "accepted"
            rc = do_dispatch(td, st2["owner"], actor_note="resumed by reconcile after an answer", echo=False)
            report.append({"task_id": t, "resumed": rc == 0})
    # queued runs, oldest first, while direct slots are free
    cap = cfg.get("max_concurrent")
    queued = []
    for t in list_tasks():
        st = TaskDir(t).state()
        if st["runs"] and st["runs"][-1]["state"] == "queued":
            queued.append((st["runs"][-1].get("queued_at", ""), t))
    for _, t in sorted(queued):
        td = TaskDir(t)
        spec = td.spec()
        if adapter_kind(spec) == "direct" and cap is not None and direct_slots_in_use() >= int(cap):
            continue
        rc = start_queued(td, "reconciler", actor_note="started from the queue", echo=False)
        report.append({"task_id": t, "started_from_queue": rc == 0})
    print(json.dumps({"reconciled_at": now(), "actions": report}, ensure_ascii=False))
    return 0


def cmd_archive(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != a.as_:
            return refuse(f"owner is {st['owner']}, not {a.as_}")
        if _active_run(st):
            return refuse("a run is in flight")
        st["archived"] = {"from_status": st["status"], "reason": a.reason, "by": a.as_, "at": now()}
        st["status"] = "archived"
    td.event("task.archived", a.as_, reason=a.reason)
    print(json.dumps({"task_id": td.task_id, "result": "archived"}))
    return 0


QUICK_FOOTER = """

---
Deliverables go under the working directory. If you are blocked (missing input, missing permission, the
task contradicts itself), write one concrete question to {blocked} and stop. The task is verified afterwards by:
    {verify}
"""


def cmd_quick(a) -> int:
    """One-line submit (+ dispatch): writes CARD.md, verify.sh and spec.json under $YARDGOAT_HOME/cards/<id>/."""
    import random
    import time as _t
    from .store import home
    cfg = runner.config()
    tid = a.id or f"q-{_t.strftime('%Y%m%d-%H%M%S')}-{random.randint(0, 0xffff):04x}"
    if not contract.TASK_ID_RE.match(tid):
        print(f"spec error: bad id {tid}", file=sys.stderr); return 2
    card_dir = home() / "cards" / tid
    if card_dir.exists() and not TaskDir(tid).exists:
        print(f"spec error: {card_dir} exists but the task does not", file=sys.stderr); return 2
    card_dir.mkdir(parents=True, exist_ok=True)
    text = a.text if a.text != "-" else sys.stdin.read()
    pool_conf = cfg.get("adapters", {}).get("file-pool", {}).get("pools", {}).get(a.pool, {}) if a.pool else {}
    workdir = a.workdir or pool_conf.get("workdir") or str(card_dir)
    blocked = a.blocked_file or "out/BLOCKED.md"
    (card_dir / "CARD.md").write_text(text.strip() + QUICK_FOOTER.format(blocked=blocked, verify=a.verify), encoding="utf-8")
    (card_dir / "verify.sh").write_text("set -e\n" + a.verify + "\n", encoding="utf-8")
    spec = {"task_id": tid, "project": a.project or (a.pool or "adhoc"), "goal": a.goal or text.strip().splitlines()[0][:120],
            "card": "CARD.md", "workdir": workdir, "outputs": a.output or [], "blocked_file": blocked,
            "verifier": {"command": f"bash {card_dir / 'verify.sh'}", "cwd": workdir, "freeze": ["verify.sh"]},
            "budget": {"max_attempts": a.attempts, "max_wall_min": a.wall},
            "authority": a.authority or "Only what the card says; anything else needs a human."}
    if a.pool:
        lane = pool_conf.get("lane", "")
        spec["adapter"] = {"kind": "file-pool", "pool": a.pool, "worker_verify": a.verify}
        spec["executor"] = {"host": pool_conf.get("host", "claude"), "lane": lane}
        spec["allowed_lanes"] = [lane] if lane else []
    else:
        spec["executor"] = {"host": a.host, "lane": a.lane or "", "acct": a.acct or ""}
        if a.lane:
            spec["allowed_lanes"] = [a.lane]
    (card_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    class A: pass
    sa = A(); sa.spec = str(card_dir / "spec.json"); sa.as_ = a.as_
    rc = cmd_submit(sa)
    if rc != 0 or a.no_dispatch:
        return rc
    return do_dispatch(TaskDir(tid), a.as_)


def cmd_cancel(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != a.as_:
            return refuse(f"owner is {st['owner']}, not {a.as_}")
        run = st["runs"][-1] if st["runs"] else None
        if not run or run["state"] != "queued":
            return refuse("only a queued run can be cancelled (stopping a running worker is not supported yet)")
        run["state"] = "cancelled"; st["status"] = "accepted"
    td.event("run.cancelled", a.as_, run_id=run["run_id"])
    print(json.dumps({"task_id": td.task_id, "result": "cancelled", "run_id": run["run_id"]}))
    return 0


def cmd_events(a) -> int:
    for ev in TaskDir(a.task_id).events():
        print(json.dumps(ev, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    P = argparse.ArgumentParser(prog="yardgoat", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = P.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit"); s.add_argument("spec"); s.add_argument("--as", dest="as_", required=True)
    s = sub.add_parser("dispatch"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s = sub.add_parser("status"); s.add_argument("task_id", nargs="?"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("takeover"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--from", dest="from_", required=True)
    s = sub.add_parser("close"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s = sub.add_parser("park"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--question", required=True)
    s = sub.add_parser("answer"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    g = s.add_mutually_exclusive_group(required=True); g.add_argument("--text"); g.add_argument("--file")
    s = sub.add_parser("inbox"); s.add_argument("--json", action="store_true")
    sub.add_parser("reconcile")
    s = sub.add_parser("quick"); s.add_argument("text", help="card text, or - to read stdin")
    s.add_argument("--verify", required=True, help="shell command: exit 0 = pass, 1 = fail")
    s.add_argument("--as", dest="as_", required=True); s.add_argument("--id")
    g = s.add_mutually_exclusive_group(); g.add_argument("--pool"); g.add_argument("--host", default="codex", choices=contract.HOSTS)
    s.add_argument("--lane"); s.add_argument("--acct"); s.add_argument("--workdir"); s.add_argument("--project"); s.add_argument("--goal")
    s.add_argument("--output", action="append"); s.add_argument("--blocked-file"); s.add_argument("--authority")
    s.add_argument("--attempts", type=int, default=2); s.add_argument("--wall", type=int, default=60)
    s.add_argument("--no-dispatch", action="store_true")
    s = sub.add_parser("cancel"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s = sub.add_parser("archive"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--reason", required=True)
    s = sub.add_parser("events"); s.add_argument("task_id")
    s = sub.add_parser("_run"); s.add_argument("task_id"); s.add_argument("run_id")
    a = P.parse_args(argv)
    if a.cmd == "_run":
        return runner.run_and_receipt(TaskDir(a.task_id), a.run_id)
    return {"submit": cmd_submit, "dispatch": cmd_dispatch, "status": cmd_status, "takeover": cmd_takeover,
            "close": cmd_close, "park": cmd_park, "answer": cmd_answer, "inbox": cmd_inbox,
            "reconcile": cmd_reconcile, "archive": cmd_archive, "cancel": cmd_cancel, "quick": cmd_quick, "events": cmd_events}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
