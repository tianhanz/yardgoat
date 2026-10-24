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
  yardgoat claim --pool P --station S                    (stations) take the oldest queued card of pool P, or EMPTY
  yardgoat finish <task_id> --run R --station S          (stations) report a claimed card as done (or blocked)
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
import time
from pathlib import Path

from . import adapters, contract, pool, runner
from .store import TaskDir, home, list_tasks, now, read_json, write_json

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
        (home() / "frozen").mkdir(parents=True, exist_ok=True)   # authoritative copy, outside what stations may write
        write_json(home() / "frozen" / f"{td.task_id}.json", {"content_hash": h, "frozen": frozen, "spec": spec})
        box["state"] = {"task_id": td.task_id, "content_hash": h, "frozen": frozen, "owner": a.as_,
                        "owner_history": [], "status": "accepted", "runs": [], "verdict": None,
                        "question": None, "accepted_at": now()}
    td.event("task.accepted", a.as_, content_hash=h)
    print(json.dumps({"task_id": td.task_id, "result": "accepted", "owner": a.as_}))
    return 0


def frozen_of(td: TaskDir, st: dict) -> dict:
    """Hashes to check for drift, from the authoritative copy. Any disagreement with the copies inside the task store
    (task.json hashes, spec.json) is itself reported as drift: the store is not trusted to describe the task."""
    f = td.frozen_path
    if not f.exists():
        return st["frozen"]
    auth = read_json(f)
    out = dict(auth["frozen"])
    if auth["frozen"] != st["frozen"]:
        out["<task.json frozen hashes were altered>"] = ""
    if "spec" in auth:
        try:
            if read_json(td.path / "spec.json") != auth["spec"]:
                out["<spec.json was altered>"] = ""
        except (OSError, ValueError):
            out["<spec.json is unreadable>"] = ""
    return out


DIRECT_MODES = ("tmux", "process")
IN_FLIGHT = ("queued", "running", "submit_unknown")


def _alive(run: dict) -> bool:
    """Is this run still in flight? Only direct runs are judged by local processes (their process group)."""
    mode = run.get("mode")
    if run["state"] in ("queued", "submit_unknown"):
        return True
    if run["state"] != "running":
        return False
    if mode in DIRECT_MODES:
        return runner.pgid_alive(run.get("worker_pgid")) or runner.pid_alive(run.get("worker_pid")) \
            or runner.pid_alive(run.get("pid"))
    if mode == "pool":
        rec = home() / "pools" / run.get("pool", "") / "stations" / f"{run.get('station', '')}.json"
        if not run.get("station"):
            return True
        if not rec.exists():
            return False
        r = read_json(rec)
        return r.get("state") == "running" and (runner.pid_alive(r.get("pid")) or runner.pid_alive(r.get("launcher_pid")))
    return True  # file-pool and plugin adapters: in flight until their poll() reports an outcome


def _receipt_waiting(td: TaskDir, run: dict) -> bool:
    return run["state"] == "running" and (td.run_dir(run["run_id"]) / "receipt.json").exists()


def _active_run(st: dict):
    last = st["runs"][-1] if st["runs"] else None
    return last if last and last["state"] in IN_FLIGHT and _alive(last) else None


def direct_slots_in_use() -> int:
    n = 0
    for t in list_tasks():
        st = TaskDir(t).state()
        last = st["runs"][-1] if st["runs"] else None
        if last and last["state"] == "running" and last.get("mode") in DIRECT_MODES and _alive(last):
            n += 1
    return n


def machine_load(cfg: dict) -> int:
    return direct_slots_in_use() + pool.all_live_stations(cfg)


def lane_problem(spec: dict, cfg: dict) -> str | None:
    lane = spec["executor"].get("lane", "")
    if adapter_kind(spec) == "file-pool":
        lane = cfg.get("adapters", {}).get("file-pool", {}).get("pools", {}).get(spec["adapter"]["pool"], {}).get("lane", lane)
    if adapter_kind(spec) == "pool":
        lane = pool.pool_conf(cfg, spec["adapter"]["pool"]).get("lane", lane)
    allowed = spec.get("allowed_lanes")
    if allowed is not None and lane not in allowed:
        return f"lane {lane!r} is not in this task's allowed_lanes {allowed}"
    if lane and lane in cfg.get("forbidden_lanes", []):
        return f"lane {lane!r} is forbidden on this machine (config forbidden_lanes)"
    return None


def do_dispatch(td: TaskDir, who: str, actor_note: str = "", echo: bool = True) -> int:
    """Accept a new attempt as a queued run; start it at once if capacity allows (all under the schedule lock)."""
    with pool.schedule_lock():
        return do_dispatch_locked(td, who, actor_note, echo)


def do_dispatch_locked(td: TaskDir, who: str, actor_note: str = "", echo: bool = False) -> int:
    cfg = runner.config()
    if True:  # (kept the block shape of the pre-lock version; the schedule lock is held by the caller)
        last = (td.state()["runs"] or [None])[-1]
        if last and _receipt_waiting(td, last):      # a writer died between receipt and state: finish its commit
            runner.record_outcome(td, last["run_id"], replay=True)
        with td.locked() as box:
            st = box["state"]
            if st["owner"] != who:
                return refuse(f"owner is {st['owner']}, not {who}")
            if st["status"] in ("verified", "exhausted", "archived"):
                return refuse(f"task is {st['status']}")
            if st["status"] == "parked":
                return refuse("task is parked waiting for an answer; use `yardgoat answer`")
            if actor_note.startswith("resumed by reconcile") and st["status"] != "answered":
                return refuse(f"task is {st['status']}, no longer answered")  # re-checked under the lock (F4)
            act = _active_run(st)
            if act:
                if echo:
                    print(json.dumps({"task_id": td.task_id, "result": "already-" + act["state"], "run_id": act["run_id"]}))
                return REFUSED
            last = st["runs"][-1] if st["runs"] else None
            if last and last["state"] == "exited":
                return refuse(f"{last['run_id']} exited but is not verified yet; run `yardgoat close` first")
            if last and last["state"] == "verify_error":
                return refuse(f"{last['run_id']}'s verifier crashed; fix its environment and run `yardgoat close` again")
            if last and last["state"] == "running":  # recorded running, nothing alive, no receipt
                last["state"] = "lost"
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
            run = {"run_id": run_id, "state": "queued", "dispatched_by": who, "queued_at": now(), "mode": "queued"}
            if adapter_kind(spec) == "pool":
                run.update(mode="pool", pool=spec["adapter"]["pool"])
            st["runs"].append(run)
            st["status"] = "queued"
            st["question"] = None
        cap = cfg.get("max_concurrent")
        if adapter_kind(spec) == "pool":
            td.event("run.queued", who, run_id=run_id, pool=spec["adapter"]["pool"], note=actor_note)
            started = pool.schedule(cfg, direct_slots_in_use)
            if echo:
                print(json.dumps({"task_id": td.task_id, "result": "queued", "run_id": run_id, "pool": spec["adapter"]["pool"],
                                  "stations_started": started}))
            return 0
        if adapter_kind(spec) == "direct" and cap is not None and machine_load(cfg) >= int(cap):
            td.event("run.queued", who, run_id=run_id, cap=cap, note=actor_note)
            if echo:
                print(json.dumps({"task_id": td.task_id, "result": "queued", "run_id": run_id,
                                  "note": f"{cap} runs already in flight; reconcile starts it when a slot frees"}))
            return 0
        return start_queued(td, who, actor_note, echo)


def _launch_problem(td: TaskDir, st: dict, spec: dict, cfg: dict) -> str | None:
    d = contract.drift(frozen_of(td, st))
    if d:
        return f"frozen files changed since submit: {d}"
    if not Path(spec["workdir"]).is_dir():
        return f"workdir does not exist: {spec['workdir']}"
    if adapter_kind(spec) == "pool":
        try:
            w = pool.workdir_problem(spec, cfg)
        except RuntimeError as e:
            return str(e)
        if w:
            return w
    return lane_problem(spec, cfg)


def start_queued(td: TaskDir, actor: str, actor_note: str = "", echo: bool = True) -> int:
    """Launch the task's queued run. Caller holds the schedule lock (capacity was checked under it)."""
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
        ad = adapters.get(adapter_kind(spec), cfg)
        try:
            run.update(ad.submit(td, run["run_id"], spec, cfg))
        except Exception as e:
            run["reason"] = str(e)[:300]
            if adapter_kind(spec) == "direct":     # nothing left the machine: a clean launch failure
                run["state"] = "launch_failed"; st["status"] = "accepted"
                print(f"launch failed: {e}", file=sys.stderr)
                return 2
            # the remote side may already have the work: never submit a second copy until we know
            present = None
            if hasattr(ad, "probe"):
                try:
                    present = ad.probe(td, run["run_id"], spec, cfg)
                except Exception:
                    present = None
            if present:
                run.update(mode=adapter_kind(spec), pool_task_id=f"{td.task_id}-{run['run_id']}",
                           pool=spec["adapter"].get("pool"), submit_error=run.pop("reason"))
            else:
                run["state"] = "submit_unknown"; run["mode"] = adapter_kind(spec); st["status"] = "submit_unknown"
                print(f"submit outcome unknown: {e}; not resubmitting until resolved (see `yardgoat inbox`)", file=sys.stderr)
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
            "verdict": st["verdict"], "question": st.get("question"), "runs": runs, "drift": contract.drift(frozen_of(td, st))}


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
    rc, _ = do_close(td, a.as_, echo=True)
    return rc


def do_close(td: TaskDir, who: str, actor_note: str = "", echo: bool = False) -> tuple[int, str | None]:
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != who:
            return refuse(f"owner is {st['owner']}, not {who}"), None
        if st["status"] in ("archived", "verified"):
            return refuse(f"task is {st['status']}"), None
        last = st["runs"][-1] if st["runs"] else None
        if not last or last["state"] not in ("exited", "verify_error"):
            return refuse("no exited run waiting for verification"), None
        d = contract.drift(frozen_of(td, st))
        if d:
            return refuse(f"frozen files changed since submit: {d}"), None
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
                           "checked_at": checked, "checked_by": who}
        if verdict == "PASS":
            rec["status"] = "passed"; last["state"] = "verified"; st["status"] = "verified"
        elif verdict == "FAIL":
            rec["status"] = "failed"; last["state"] = "failed"
            used = sum(r["state"] in COUNTED for r in st["runs"])
            st["status"] = "failed" if used < spec["budget"]["max_attempts"] else "exhausted"
        else:  # a crashing verifier is not a failing deliverable; leave the run waiting for a human
            rec["status"] = "verifier_error"; last["state"] = "verify_error"
        write_json(rec_p, rec)
        st["verdict"] = {"run_id": last["run_id"], "verdict": verdict, "by": who, "at": checked}
    td.event("run.verified", who, run_id=last["run_id"], verdict=verdict, note=actor_note)
    if echo:
        print(json.dumps({"task_id": td.task_id, "run_id": last["run_id"], "verdict": verdict}))
    return (0 if verdict == "PASS" else (1 if verdict == "FAIL" else 2)), verdict


def cmd_park(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != a.as_:
            return refuse(f"owner is {st['owner']}, not {a.as_}")
        if st["status"] in ("archived", "verified", "exhausted", "parked", "answered"):
            return refuse(f"task is {st['status']}; it cannot be parked")
        if _active_run(st):
            return refuse("a run is in flight; park after it ends")
        last = st["runs"][-1] if st["runs"] else None
        if last and last["state"] in ("exited", "verify_error"):
            return refuse(f"{last['run_id']} is waiting for verification; run `yardgoat close` first")
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
        if st["status"] in ("parked", "exhausted", "submit_unknown") or (st["runs"] and st["runs"][-1]["state"] == "verify_error"):
            items.append({"task_id": t, "status": st["status"], "owner": st["owner"],
                          "question": (st.get("question") or {}).get("text")})
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
    else:
        for i in items:
            print(f"{i['task_id']} [{i['status']}] owner={i['owner']}\n  {i['question'] or '(no question recorded)'}")
    return 0


def cmd_reconcile(a) -> int:
    """Server-side upkeep, safe to run concurrently: every state change re-checks under the task lock."""
    cfg = runner.config()
    report = []
    with pool.schedule_lock():
        for t in list_tasks():
            td = TaskDir(t)
            st = td.state()
            spec = td.spec()
            last = st["runs"][-1] if st["runs"] else None
            if not last:
                continue
            if _receipt_waiting(td, last):                   # writer died between receipt and state
                if runner.record_outcome(td, last["run_id"], replay=True):
                    report.append({"task_id": t, "run_id": last["run_id"], "replayed_receipt": True})
                continue
            if last["state"] == "submit_unknown":
                ad = adapters.get(adapter_kind(spec), cfg)
                if hasattr(ad, "probe") and ad.probe(td, last["run_id"], spec, cfg):
                    with td.locked() as box:
                        r = box["state"]["runs"][-1]
                        if r["state"] == "submit_unknown":
                            r.update(state="running", pool_task_id=f"{t}-{r['run_id']}", pool=spec["adapter"].get("pool"))
                            box["state"]["status"] = "running"
                    report.append({"task_id": t, "run_id": last["run_id"], "submit_confirmed": True})
                continue
            if last["state"] == "running" and adapter_kind(spec) == "pool":
                if not _alive(last):
                    if runner.record_outcome(td, last["run_id"], {"state": "lost", "reason": f"station {last.get('station')} is gone",
                                                                  "worker": last.get("station")}):
                        report.append({"task_id": t, "run_id": last["run_id"], "observed": "lost"})
                continue
            if last["state"] == "exited" and spec.get("auto_close") and st["status"] == "exited":
                rc, verdict = do_close(td, st["owner"], actor_note="auto_close by reconcile")
                if verdict:
                    report.append({"task_id": t, "run_id": last["run_id"], "auto_closed": verdict})
                continue
            if last["state"] == "running":
                try:
                    outcome = adapters.get(adapter_kind(spec), cfg).poll(td, last, spec, cfg)
                except Exception as e:
                    report.append({"task_id": t, "error": repr(e)}); continue
                if outcome and runner.record_outcome(td, last["run_id"], outcome):
                    report.append({"task_id": t, "run_id": last["run_id"], "observed": outcome["state"]})
            elif st["status"] == "answered":
                rc = do_dispatch_locked(td, st["owner"], "resumed by reconcile after an answer")
                report.append({"task_id": t, "resumed": rc == 0})
        # queued direct runs, oldest first, while there is capacity
        cap = cfg.get("max_concurrent")
        queued = []
        for t in list_tasks():
            st = TaskDir(t).state()
            if st["runs"] and st["runs"][-1]["state"] == "queued":
                queued.append((st["runs"][-1].get("queued_at", ""), t))
        for _, t in sorted(queued):
            td = TaskDir(t)
            if adapter_kind(td.spec()) == "pool":
                continue  # stations claim pool cards themselves
            if adapter_kind(td.spec()) == "direct" and cap is not None and machine_load(cfg) >= int(cap):
                continue
            rc = start_queued(td, "reconciler", actor_note="started from the queue", echo=False)
            report.append({"task_id": t, "started_from_queue": rc == 0})
        for name in pool.schedule(cfg, direct_slots_in_use):
            report.append({"station_started": name})
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
    if TaskDir(tid).exists or card_dir.exists():
        return refuse(f"task id {tid} is already taken (quick never rewrites an existing task's files); use a new --id")
    card_dir.mkdir(parents=True, exist_ok=True)
    text = a.text if a.text != "-" else sys.stdin.read()
    yg_pool = bool(a.pool) and a.pool in pool.pools(cfg)
    pool_conf = cfg.get("adapters", {}).get("file-pool", {}).get("pools", {}).get(a.pool, {}) if a.pool and not yg_pool else {}
    if yg_pool and not a.workdir:  # a pool only runs cards whose workdir is under its root
        wd = Path(os.path.expanduser(pool.pool_conf(cfg, a.pool).get("root", str(card_dir)))) / tid
        wd.mkdir(parents=True, exist_ok=True)
        a.workdir = str(wd)
    workdir = a.workdir or pool_conf.get("workdir") or str(card_dir)
    blocked = a.blocked_file or "out/BLOCKED.md"
    (card_dir / "CARD.md").write_text(text.strip() + QUICK_FOOTER.format(blocked=blocked, verify=a.verify), encoding="utf-8")
    (card_dir / "verify.sh").write_text("set -e\n" + a.verify + "\n", encoding="utf-8")
    spec = {"task_id": tid, "project": a.project or (a.pool or "adhoc"), "goal": a.goal or text.strip().splitlines()[0][:120],
            "card": "CARD.md", "workdir": workdir, "outputs": a.output or [], "blocked_file": blocked,
            "verifier": {"command": f"bash {card_dir / 'verify.sh'}", "cwd": workdir, "freeze": ["verify.sh"]},
            "budget": {"max_attempts": a.attempts, "max_wall_min": a.wall},
            "authority": a.authority or "Only what the card says; anything else needs a human."}
    if yg_pool:
        pc = pool.pool_conf(cfg, a.pool)
        spec["adapter"] = {"kind": "pool", "pool": a.pool}
        spec["executor"] = {"host": pc.get("host", "codex"), "lane": pc.get("lane", ""), "acct": pc.get("acct", "")}
        if pc.get("lane"):
            spec["allowed_lanes"] = [pc["lane"]]
    elif a.pool:
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


def claim_card(pool_name: str, station: str) -> tuple[int, dict]:
    """Give the oldest queued card of a pool to a live station. One card at a time per station."""
    cfg = runner.config()
    c = pool.pool_conf(cfg, pool_name)
    with pool.pool_lock(pool_name):
        with pool.station_lock(pool_name, station):
            rec = pool.get_station(pool_name, station)
            if not rec or rec.get("state") != "running" or not pool.station_alive(rec):
                return 3, {"result": "REFUSED", "reason": f"{station} is not a live station of pool {pool_name}"}
            if rec.get("current"):
                return 3, {"result": "REFUSED", "reason": f"{station} still holds {rec['current']['task_id']}; finish it first"}
            if len(rec.get("tasks", [])) >= int(c.get("max_tasks_per_station", 5)):
                return 0, {"result": "EMPTY", "reason": "this station reached max_tasks_per_station"}
        for _, t in pool.queued_cards(pool_name):
            td = TaskDir(t)
            with td.locked() as box:
                st = box["state"]
                run = st["runs"][-1]
                if run["state"] != "queued":
                    continue
                spec = td.spec()
                problem = _launch_problem(td, st, spec, cfg)
                if problem:
                    run.update(state="launch_failed", reason=problem); st["status"] = "accepted"
                    continue
                rd = td.run_dir(run["run_id"]); rd.mkdir(parents=True, exist_ok=True)
                (rd / "prompt.md").write_text(runner.prompt_text(td, spec), encoding="utf-8")
                run.update(state="running", station=station, started_at=now(), claimed_epoch=time.time())
                st["status"] = "running"
                out = {"result": "CLAIMED", "task_id": t, "run_id": run["run_id"], "workdir": spec["workdir"],
                       "prompt": str(rd / "prompt.md"), "blocked_file": spec.get("blocked_file", "out/BLOCKED.md"),
                       "outputs": spec["outputs"]}
                deadline = time.time() + float(spec["budget"]["max_wall_min"]) * 60
            with pool.station_lock(pool_name, station):
                rec = pool.get_station(pool_name, station)
                rec.setdefault("tasks", []).append(f"{t}/{out['run_id']}")
                rec["current"] = {"task_id": t, "run_id": out["run_id"], "deadline": deadline}
                write_json(pool.station_file(pool_name, station), rec)
            td.event("run.claimed", station, run_id=out["run_id"], pool=pool_name)
            return 0, out
    return 0, {"result": "EMPTY"}


def finish_card(task_id: str, run_id: str, station: str) -> tuple[int, dict]:
    td = TaskDir(task_id)
    if not td.exists:
        return 3, {"result": "REFUSED", "reason": f"unknown task {task_id}"}
    st = td.state()
    run = next((r for r in st["runs"] if r["run_id"] == run_id), None)
    if not run or run.get("station") != station:
        return 3, {"result": "REFUSED", "reason": f"{run_id} was not claimed by {station}"}
    spec = td.spec()
    c = pool.pool_conf(runner.config(), run["pool"])
    out = {"state": "exited", "exit_code": 0, "host": c.get("host", ""), "lane": c.get("lane", ""), "worker": station,
           "actual_model": pool.station_model(run["pool"], station), "model_source": "station-log",
           "cost_basis": "shared-station"}
    bf = Path(spec["workdir"]) / spec.get("blocked_file", "out/BLOCKED.md")
    if bf.is_file() and bf.stat().st_mtime >= run.get("claimed_epoch", 0):
        out.update(state="blocked", question=bf.read_text(encoding="utf-8", errors="replace").strip()[:2000])
    rec = runner.record_outcome(td, run_id, out)   # checks, under the task lock, that the run is still running
    pool.update_station(run["pool"], station, current=None)
    if rec is None:
        return 3, {"result": "REFUSED", "reason": f"{run_id} is no longer running (timed out, lost or already finished)"}
    return 0, {"task_id": task_id, "run_id": run_id, "result": out["state"]}


def _via_mailbox(op: dict) -> int:
    """Inside a station: hand the request to the station wrapper (outside any sandbox) and wait for its answer."""
    import uuid
    mb = Path(os.environ["YARDGOAT_MAILBOX"])
    rid = uuid.uuid4().hex[:12]
    tmp = mb / f".req-{rid}.tmp"
    tmp.write_text(json.dumps(op, ensure_ascii=False))
    os.replace(tmp, mb / f"req-{rid}.json")
    resp = mb / f"resp-{rid}.json"
    end = time.time() + 300
    while time.time() < end:
        if resp.exists():
            r = json.loads(resp.read_text()); resp.unlink(missing_ok=True)
            print(json.dumps(r["out"], ensure_ascii=False))
            return r["rc"]
        time.sleep(0.2)
    print(json.dumps({"result": "ERROR", "reason": "station wrapper did not answer within 300 s"}))
    return 2


def cmd_claim(a) -> int:
    if os.environ.get("YARDGOAT_MAILBOX"):
        return _via_mailbox({"op": "claim"})
    rc, out = claim_card(a.pool, a.station)
    print(json.dumps(out, ensure_ascii=False))
    return rc


def cmd_finish(a) -> int:
    if os.environ.get("YARDGOAT_MAILBOX"):
        return _via_mailbox({"op": "finish", "task_id": a.task_id, "run_id": a.run})
    rc, out = finish_card(a.task_id, a.run, a.station)
    print(json.dumps(out, ensure_ascii=False))
    return rc


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
    s = sub.add_parser("claim"); s.add_argument("--pool", required=True); s.add_argument("--station", required=True)
    s = sub.add_parser("finish"); s.add_argument("task_id"); s.add_argument("--run", required=True); s.add_argument("--station", required=True)
    s = sub.add_parser("_station"); s.add_argument("pool_name"); s.add_argument("station")
    s = sub.add_parser("cancel"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s = sub.add_parser("archive"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--reason", required=True)
    s = sub.add_parser("events"); s.add_argument("task_id")
    s = sub.add_parser("_run"); s.add_argument("task_id"); s.add_argument("run_id")
    a = P.parse_args(argv)
    if a.cmd == "_run":
        return runner.run_and_receipt(TaskDir(a.task_id), a.run_id)
    if a.cmd == "_station":
        return pool.run_station(a.pool_name, a.station)
    return {"submit": cmd_submit, "dispatch": cmd_dispatch, "status": cmd_status, "takeover": cmd_takeover,
            "close": cmd_close, "park": cmd_park, "answer": cmd_answer, "inbox": cmd_inbox,
            "reconcile": cmd_reconcile, "archive": cmd_archive, "cancel": cmd_cancel, "quick": cmd_quick, "claim": cmd_claim, "finish": cmd_finish, "events": cmd_events}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
