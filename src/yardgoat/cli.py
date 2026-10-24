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
  yardgoat approve <task_id> --as <who> [--note N]        approve the frozen card (required for risk l3)
  yardgoat deliver <task_id> --as <controller>            retry unfinished on_pass steps after PASS
  yardgoat reconcile                                     poll adapter runs, mark lost runs, resume answered tasks
  yardgoat archive <task_id> --as <controller> --reason R  close out a task that will not continue (kept, hidden from inbox)
  yardgoat quick "<card text>" --verify CMD --as <who> [--pool P | --host H] [...]   one-line submit + dispatch
  yardgoat cancel <task_id> --as <controller>            cancel a queued run
  yardgoat claim --pool P --station S                    (stations) take the oldest queued card of pool P, or EMPTY
  yardgoat finish <task_id> --run R --station S          (stations) report a claimed card as done (or blocked)
  yardgoat events <task_id>                              print the event log

Direct runs are capped by the local config "max_concurrent" (default unlimited). Beyond the cap a dispatch is
accepted as "queued" and `reconcile` starts it when a slot frees up, oldest first.

Exit codes: 0 ok · 3 refused by policy · 2 usage/spec error · 1 verifier FAIL or delivery failure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import adapters, contract, delivery, doctor, notify, pool, report, review, runner, scheduling
from .store import TaskDir, home, list_tasks, now, read_json, write_json

REFUSED = 3
COUNTED = ("exited", "failed", "verified", "lost", "verify_error")  # blocked / launch_failed / provider_refused don't count


def refuse(msg: str) -> int:
    print(f"refused: {msg}", file=sys.stderr)
    return REFUSED


def adapter_kind(spec: dict) -> str:
    return spec.get("adapter", {}).get("kind", "direct")


def cmd_submit(a) -> int:
    try:
        spec = contract.load_spec_file(a.spec)
        review.prepare(spec, runner.config())
        frozen = contract.freeze(spec)
    except (contract.SpecError, OSError, ValueError, RuntimeError) as e:
        print(f"spec error: {e}", file=sys.stderr)
        return 2
    h = contract.content_hash(spec, frozen)
    td = TaskDir(spec["task_id"])
    with pool.schedule_lock(), td.locked() as box:
        if box["state"] is not None:
            old_hash = box["state"]["content_hash"]
            # Pre-M1 specs omitted metadata defaults. Compare the original input shape only;
            # explicit scope/risk (even default values) remain content changes. Never rewrite frozen data.
            legacy_hash = contract.content_hash(contract.load_spec_file(a.spec, metadata_defaults=False), frozen)
            if old_hash in (h, legacy_hash):
                print(json.dumps({"task_id": td.task_id, "result": "already-accepted", "status": box["state"]["status"]}))
                return 0
            return refuse(f"{td.task_id} already exists with different content (task ids are immutable)")
        try:
            if "review_of" in spec or "review_followup" in spec:
                raise contract.SpecError("review_of and review_followup are reserved for generated review tasks")
            scheduling.validate_dependencies(spec)
        except contract.SpecError as e:
            print(f"spec error: {e}", file=sys.stderr)
            return 2
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
    if auth.get("content_hash") != st.get("content_hash"):
        out["<task.json content hash was altered>"] = ""
    if auth["frozen"] != st["frozen"]:
        out["<task.json frozen hashes were altered>"] = ""
    if "spec" in auth:
        try:
            if read_json(td.path / "spec.json") != auth["spec"]:
                out["<spec.json was altered>"] = ""
        except (OSError, ValueError):
            out["<spec.json is unreadable>"] = ""
    return out


def checked_drift(td: TaskDir, st: dict) -> list[str]:
    """Mutation paths call under the task lock: observed drift permanently revokes approval."""
    changed = contract.drift(frozen_of(td, st))
    approval = st.get("approval")
    if changed and approval and not approval.get("invalidated_at"):
        approval.update(invalidated_at=now(), drift=changed)
        td.event("task.approval_invalidated", "yardgoat", approval=approval, reason="frozen content drift")
    return changed


def approver_allowed(who: str, cfg: dict) -> bool:
    allowed = cfg.get("approvers")
    return "approvers" not in cfg or (isinstance(allowed, list) and
            all(isinstance(item, str) for item in allowed) and who in allowed)


def approval_problem(td: TaskDir, st: dict, spec: dict, cfg: dict, changed: list[str]) -> str | None:
    if spec.get("risk", "l2") != "l3":
        return None
    approval = st.get("approval") or {}
    if (changed or not approval.get("by") or not approval.get("at") or approval.get("invalidated_at") or
            approval.get("content_hash") != st["content_hash"] or not approver_allowed(approval.get("by"), cfg)):
        return f"需要 approve：yardgoat approve {td.task_id} --as <approver>（等审批）"
    return None


def cmd_approve(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    if not a.as_.strip() or not approver_allowed(a.as_, runner.config()):
        return refuse(f"{a.as_!r} is not in the configured approvers")
    with td.locked() as box:
        st = box["state"]
        changed = checked_drift(td, st)
        if changed:
            return refuse(f"frozen files changed since submit: {changed}; approval requires the frozen card")
        if st["status"] in ("archived", "exhausted"):
            return refuse(f"task is {st['status']}")
        st["approval"] = {"by": a.as_, "at": now(), "note": a.note or "", "content_hash": st["content_hash"]}
        td.event("task.approved", a.as_, approval=st["approval"])
    print(json.dumps({"task_id": td.task_id, "result": "approved", "approval": st["approval"]}, ensure_ascii=False))
    return 0


DIRECT_MODES = ("tmux", "process")
IN_FLIGHT = ("waiting_deps", "queued", "running", "submit_unknown")


def _alive(run: dict) -> bool:
    """Is this run still in flight? Only direct runs are judged by local processes (their process group)."""
    mode = run.get("mode")
    if run["state"] in ("waiting_deps", "queued", "submit_unknown"):
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
        return pool.station_alive(read_json(rec))
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
        elif last and last["state"] == "running" and not _alive(last):
            spec = td.spec()
            if adapter_kind(spec) == "pool":
                outcome = lambda fresh: pool.unfinished_outcome(
                    fresh["pool"], fresh.get("station", ""), cfg, fresh, task_id=td.task_id)
            else:
                outcome = adapters.get(adapter_kind(spec), cfg).poll(td, last, spec, cfg)
            if outcome:
                runner.record_outcome(td, last["run_id"], outcome)
        with td.locked() as box:
            st = box["state"]
            if st["owner"] != who:
                return refuse(f"owner is {st['owner']}, not {who}")
            if st["status"] in ("verified", "exhausted", "archived"):
                return refuse(f"task is {st['status']}")
            if st["status"] == "reviewing" or st.get("review", {}).get("complete"):
                return refuse("review is pending or its round budget is complete")
            if any(r["status"] == "pending" for r in st.get("review", {}).get("rounds", [])):
                return refuse("a review result is still pending; reconcile it before starting a repair")
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
            if spec.get("review") and len(st.get("review", {}).get("rounds", [])) >= spec["review"]["rounds"]:
                review.park(td, st, "review round budget exhausted; submit a new task or archive")
                return refuse("review round budget exhausted; submit a new task or archive")
            problem = _launch_problem(td, st, spec, cfg)
            if problem:
                if spec.get("review"):
                    review.park(td, st, problem)
                return refuse(problem)
            used = sum(r["state"] in COUNTED for r in st["runs"])
            if used >= spec["budget"]["max_attempts"]:
                st["status"] = "exhausted"
                return refuse("attempt budget exhausted; escalate to a human")
            run_id = f"r{len(st['runs']) + 1}"
            td.run_dir(run_id).mkdir(parents=True, exist_ok=True)
            run = {"run_id": run_id, "state": "queued", "dispatched_by": who, "queued_at": now(), "mode": "queued"}
            if spec.get("review"):
                run["review_round"] = len(st.get("review", {}).get("rounds", [])) + 1
            if st.get("approval"):
                run["approval"] = dict(st["approval"])
            if adapter_kind(spec) == "pool":
                run.update(mode="pool", pool=spec["adapter"]["pool"])
            st["runs"].append(run)
            st["status"] = "queued"
            st["question"] = None
            _queue_gate(td, st, run, spec, who)
        if run["state"] in ("waiting_deps", "blocked_deps"):
            if echo:
                print(json.dumps({"task_id": td.task_id, "result": st["status"], "run_id": run_id,
                                  "waiting_for": run["waiting_for"], "reason": run["reason"]}, ensure_ascii=False))
            return 0
        cap = cfg.get("max_concurrent")
        if adapter_kind(spec) == "pool":
            td.event("run.queued", who, run_id=run_id, pool=spec["adapter"]["pool"], note=actor_note)
            started = pool.schedule(cfg, direct_slots_in_use)
            if echo:
                print(json.dumps({"task_id": td.task_id, "result": "queued", "run_id": run_id, "pool": spec["adapter"]["pool"],
                                  "stations_started": started}))
            return 0
        if adapter_kind(spec) == "direct" and ((cap is not None and machine_load(cfg) >= int(cap)) or not pool.resources_ok(cfg)[0]):
            td.event("run.queued", who, run_id=run_id, cap=cap, note=actor_note, reason=pool.resources_ok(cfg)[1])
            if echo:
                print(json.dumps({"task_id": td.task_id, "result": "queued", "run_id": run_id,
                                  "note": f"{cap} runs already in flight; reconcile starts it when a slot frees"}))
            return 0
        return start_queued(td, who, actor_note, echo)


def _launch_problem(td: TaskDir, st: dict, spec: dict, cfg: dict, station: dict | None = None) -> str | None:
    d = checked_drift(td, st)
    if d:
        return f"frozen files changed since submit: {d}"
    problem = approval_problem(td, st, spec, cfg, d)
    if problem:
        return problem
    if not Path(spec["workdir"]).is_dir():
        return f"workdir does not exist: {spec['workdir']}"
    if adapter_kind(spec) == "pool":
        try:
            w = pool.workdir_problem(spec, cfg)
        except RuntimeError as e:
            return str(e)
        if w:
            return w
    if station is not None and spec.get("review") and not station.get("channel"):
        return "execution station has no recorded launch channel; restart the station"
    problem = review.channel_problem(spec, cfg, station.get("channel") if station else None)
    if spec.get("review_of"):
        problem = review.child_problem(spec, cfg, station)
    return problem or lane_problem(spec, cfg)


def _wait_for_dependencies(td: TaskDir, st: dict, run: dict, waiting: list[str], actor: str) -> None:
    changed = run["state"] != "waiting_deps" or run.get("waiting_for") != waiting
    run.update(state="waiting_deps", waiting_for=waiting, reason=scheduling.wait_reason(waiting))
    run.pop("scope_conflicts", None)
    st["status"] = "waiting_deps"
    if changed:
        td.event("run.waiting_deps", actor, run_id=run["run_id"], waiting_for=waiting)


def _queue_gate(td: TaskDir, st: dict, run: dict, spec: dict, actor: str) -> bool:
    """Refresh dependencies and exclusions under schedule → task locks; never spend an attempt."""
    waiting, stopped = scheduling.dependencies(spec)
    if stopped:
        reason = "；".join(f"上游 {tid} 已终止" for tid in stopped)
        run.update(state="blocked_deps", waiting_for=waiting, reason=reason)
        run.pop("scope_conflicts", None)
        st["status"] = "parked"
        st["question"] = {"run_id": run["run_id"], "text": reason, "at": now(), "reason": "upstream_terminated"}
        td.event("task.parked", actor, run_id=run["run_id"], reason=reason)
        return False
    if waiting:
        _wait_for_dependencies(td, st, run, waiting, actor)
        return False
    if run["state"] == "waiting_deps":
        run.update(state="queued", queued_at=now())
        st["status"] = "queued"
        td.event("run.queued", actor, run_id=run["run_id"], reason="dependencies verified")
    if run.pop("waiting_for", None) is not None:
        run.pop("reason", None)
    conflicts = scheduling.scope_conflicts(td.task_id, spec)
    if conflicts:
        reason = "；".join(f"撞了 {c['task_id']} 的 {c['path']}，它完成后放行" for c in conflicts)
        if run.get("scope_conflicts") != conflicts:
            td.event("run.scope_blocked", actor, run_id=run["run_id"], conflicts=conflicts, reason=reason)
        run.update(scope_conflicts=conflicts, reason=reason)
        return False
    if run.pop("scope_conflicts", None) is not None:
        run.pop("reason", None)
    return True


def start_queued(td: TaskDir, actor: str, actor_note: str = "", echo: bool = True) -> int:
    """Launch the task's queued run. Caller holds the schedule lock (capacity was checked under it)."""
    cfg = runner.config()
    with td.locked() as box:
        st = box["state"]
        run = st["runs"][-1] if st["runs"] else None
        if not run or run["state"] != "queued":
            return refuse("no queued run")
        spec = td.spec()
        if not _queue_gate(td, st, run, spec, actor):
            if echo:
                print(json.dumps({"task_id": td.task_id, "result": st["status"], "run_id": run["run_id"],
                                  "reason": run["reason"]}, ensure_ascii=False))
            return 0
        problem = _launch_problem(td, st, spec, cfg)
        if problem:
            run["state"] = "launch_failed"; run["reason"] = problem; st["status"] = "accepted"
            if spec.get("review"):
                review.park(td, st, problem, run["run_id"])
            return refuse(problem)
        run.update(state="running", started_at=now(), write_set=scheduling.write_set(spec))
        run["channel"] = review.execution_channel(spec, cfg)
        (td.run_dir(run["run_id"]) / "prompt.md").write_text(runner.prompt_text(td, spec), encoding="utf-8")
        review.reserve(st, run, spec)
        if st.get("approval"):
            run["approval"] = dict(st["approval"])
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
    changed = contract.drift(frozen_of(td, st))
    needs_approval = approval_problem(td, st, td.spec(), runner.config(), changed)
    runs = []
    for r in st["runs"]:
        rr = dict(r)
        if r["state"] == "waiting_deps":
            rr["reason"] = scheduling.wait_reason(r.get("waiting_for", []))
        rr["alive"] = _alive(r) if r["state"] == "running" else False
        rec = td.run_dir(r["run_id"]) / "receipt.json"
        rr["receipt"] = json.loads(rec.read_text()) if rec.exists() else None
        runs.append(rr)
    ad = td.spec().get("adapter", {})
    name = ad.get("pool") if ad.get("kind") == "pool" else None
    cfg = runner.config()
    missing_pool = bool(name and not pool.pools(cfg).get(name))
    health = doctor.pool_report(name) if name and not missing_pool else None
    backoff = pool.provider_backoff(name, cfg) if name and not missing_pool else None
    problem = f"池子已不在配置中：{name}" if missing_pool else doctor.failure_message(name or "", health)
    if backoff:
        backoff = {**backoff, "pool": name}
    return {"task_id": td.task_id, "status": st["status"], "owner": st["owner"], "owner_history": st["owner_history"],
            "approval": st.get("approval"), "approval_required": bool(needs_approval),
            "review": st.get("review"),
            "review_reservation": st.get("review_reservation") if st["status"] not in ("verified", "archived") else None,
            **delivery.view(td, st, td.spec()),
            "pool_backoff": backoff,
            "pool_doctor": health, "pool_problem": problem,
            "verdict": st["verdict"], "question": st.get("question"), "runs": runs, "drift": changed}


def _backoff_text(record: dict) -> str:
    return (f"pool={record['pool']} {record['status']} account={record.get('account_alias') or '-'} "
            f"until={record['until']}: {record['reason']}")


def cmd_status(a) -> int:
    ids = [a.task_id] if a.task_id else list_tasks()
    out = [_status_obj(TaskDir(t)) for t in ids if TaskDir(t).exists]
    if a.task_id and not out:
        return refuse(f"unknown task {a.task_id}")
    unlisted_pools = []
    if not a.task_id:
        represented = {o["pool_doctor"]["pool"] for o in out if o["pool_doctor"]}
        for name in pool.pools(runner.config()):
            backoff = pool.provider_backoff(name, runner.config())
            if backoff:
                unlisted_pools.append({**backoff, "pool": name})
            message = doctor.failure_message(name, doctor.pool_report(name))
            if message and name not in represented:
                unlisted_pools.append({"pool": name, "status": "doctor_failed", "reason": message})
    if a.json:
        print(json.dumps(out + unlisted_pools if not a.task_id else out[0], ensure_ascii=False, indent=1))
        return 0
    for o in out:
        print(f"{o['task_id']}: {o['status']}  owner={o['owner']}  verdict={(o['verdict'] or {}).get('verdict')}")
        if o["approval_required"]:
            print("  等审批：需要 approve")
        if o["delivery_reason"]:
            print(f"  {o['delivery_reason']}")
        if o["pool_problem"]:
            print(f"  {o['pool_problem']}")
        if o["pool_backoff"]:
            print(f"  {_backoff_text(o['pool_backoff'])}")
        if o["question"]:
            print(f"  QUESTION: {o['question']['text'][:200]}")
        if o["review_reservation"]:
            print(f"  Review write set reserved: {', '.join(o['review_reservation']['write_set'])}")
        for r in o["runs"]:
            rec = r["receipt"]
            tail = f" exit={rec['exit_code']} model={rec['actual_model'] or '?'}" if rec else ""
            print(f"  {r['run_id']}: {r['state']} alive={r['alive']} by={r['dispatched_by']}{tail}")
            if r.get("reason"):
                print(f"    {r['reason']}")
        if o["drift"]:
            print(f"  DRIFT: {o['drift']}")
    for item in unlisted_pools:
        print(_backoff_text(item) if "until" in item else item["reason"])
    return 0


def cmd_takeover(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    if td.spec().get("review_of"):
        return refuse(f"take over the review parent {td.spec()['review_of']['task_id']} instead")
    with pool.schedule_lock(), td.locked() as box:
        st = box["state"]
        if st["owner"] != a.from_:
            return refuse(f"current owner is {st['owner']}, not {a.from_}")
        st["owner_history"].append({"from": a.from_, "to": a.as_, "at": now(),
                                    "run_in_flight": (_active_run(st) or {}).get("run_id")})
        st["owner"] = a.as_
        review.sync_owners(td, st, a.from_)
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
    result = _do_close(td, who, actor_note, echo)
    if td.spec().get("review_of") and td.state()["owner"] == who and result[1] != "PASS":
        review.advance(TaskDir(td.spec()["review_of"]["task_id"]))
    return result


def _do_close(td: TaskDir, who: str, actor_note: str = "", echo: bool = False) -> tuple[int, str | None]:
    # Repeated close can recover an interrupted child submit without rerunning verification.
    before = td.state()
    if before["status"] == "reviewing":
        if before["owner"] != who:
            return refuse(f"owner is {before['owner']}, not {who}"), None
        rc = review.advance(td, who)
        if echo:
            print(json.dumps({"task_id": td.task_id, "status": td.state()["status"], "verdict": "PASS"}))
        return rc, "PASS"
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != who:
            return refuse(f"owner is {st['owner']}, not {who}"), None
        if st["status"] == "verified" and td.spec().get("on_pass"):
            # Verification is already durable. Retrying failed delivery is explicit via deliver.
            progress = delivery.view(td, st, td.spec())
            st.update(progress)
            if echo:
                print(json.dumps({"task_id": td.task_id, "run_id": st["verdict"]["run_id"], "verdict": "PASS",
                                  **progress}, ensure_ascii=False))
            return (1 if progress["delivery_pending"] else 0), "PASS"
        if st["status"] in ("archived", "verified"):
            return refuse(f"task is {st['status']}"), None
        last = st["runs"][-1] if st["runs"] else None
        if not last or last["state"] not in ("exited", "verify_error"):
            return refuse("no exited run waiting for verification"), None
        d = checked_drift(td, st)
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
        except OSError as exc:
            rc = 127
            (rd / "verify.out").write_text(f"Verifier could not start: {exc}\n")
        verdict = {0: "PASS", 1: "FAIL"}.get(rc, "ERROR")
        review_findings = None
        if verdict == "PASS" and spec.get("review_of"):
            try:
                review_findings = json.loads(p.stdout)["findings"]
                if not isinstance(review_findings, list):
                    raise ValueError("review verifier did not return an array")
            except (ValueError, KeyError, TypeError):
                verdict, rc = "ERROR", 2
        checked = now()
        rec_p = rd / "receipt.json"
        rec = json.loads(rec_p.read_text())
        rec["verifier"] = {"command": spec["verifier"]["command"], "exit_code": rc, "result": verdict,
                           "checked_at": checked, "checked_by": who}
        if verdict == "PASS":
            rec["status"] = "passed"; last["state"] = "verified"; st["status"] = "verified"
            if review_findings is not None:
                rec.update(review_findings=review_findings, findings_count=len(review_findings),
                           review_round=spec["review_of"]["round"])
            if spec.get("review"):
                review.begin(st, last, rec, spec)
            elif spec.get("on_pass"):
                delivery.mark_pending(st, rec)
        elif verdict == "FAIL":
            rec["status"] = "failed"; last["state"] = "failed"
            used = sum(r["state"] in COUNTED for r in st["runs"])
            st["status"] = "failed" if used < spec["budget"]["max_attempts"] else "exhausted"
        else:  # a crashing verifier is not a failing deliverable; leave the run waiting for a human
            rec["status"] = "verifier_error"; last["state"] = "verify_error"
        write_json(rec_p, rec)
        st["verdict"] = {"run_id": last["run_id"], "verdict": verdict, "by": who, "at": checked}
    td.event("run.verified", who, run_id=last["run_id"], verdict=verdict, note=actor_note)
    delivery_rc = 0
    if verdict == "PASS":
        if spec.get("review"):
            delivery_rc = review.advance(td, who)
        elif spec.get("review_of"):
            delivery_rc = review.advance(TaskDir(spec["review_of"]["task_id"]))
        elif spec.get("on_pass"):
            delivery_rc = do_deliver(td, who)
    if echo:
        result = {"task_id": td.task_id, "run_id": last["run_id"], "verdict": verdict}
        if spec.get("on_pass") and verdict == "PASS" and td.state()["status"] == "verified":
            result.update(delivery.view(td, td.state(), spec))
            result["delivery_exit_code"] = delivery_rc
        print(json.dumps(result))
    return (delivery_rc if verdict == "PASS" else (1 if verdict == "FAIL" else 2)), verdict


def do_deliver(td: TaskDir, who: str, echo: bool = False, assume_stopped: bool = False) -> int:
    # Delivery contenders wait outside schedule/task locks. Only the reservation needs
    # schedule → task; the long shell command holds neither, so unrelated work can start.
    with delivery.locked(td):
        current = td.state()
        if current["owner"] != who:
            return refuse(f"owner is {current['owner']}, not {who}")
        if current["status"] == "archived":
            # Old archives can contain orphan reservations. This path only cleans
            # up; it never grants permission to execute another delivery step.
            reason = delivery.cleanup_locked(td, who, assume_stopped=assume_stopped)
            if reason:
                return refuse(reason)
            if echo:
                print(json.dumps({"task_id": td.task_id, "result": "delivery-cleaned"}))
            return 0
        spec = td.spec()
        if current["status"] == "verified" and (current.get("verdict") or {}).get("verdict") == "PASS":
            progress = delivery.view(td, current, spec)
            if any(r.get("exit_code") != 0 and not r.get("finished_at") for r in progress["on_pass"]):
                # Recover old execution before admission checks for a NEW one.
                # Even a conflicting/revoked preflight must protect legacy writers
                # that predate delivery_reservation, and may already be running.
                reason = delivery.Journal(td, current, spec).recover(who, assume_stopped=assume_stopped)
                if reason:
                    if echo:
                        print(json.dumps({"task_id": td.task_id, "result": "delivery_failed",
                                          **delivery.view(td, td.state(), spec)}, ensure_ascii=False))
                    return refuse(reason)
        with pool.schedule_lock(), td.locked() as box:
            st = box["state"]
            if st["owner"] != who:
                return refuse(f"owner is {st['owner']}, not {who}")
            if st["status"] != "verified" or (st.get("verdict") or {}).get("verdict") != "PASS":
                return refuse("deliver requires a verified task with a PASS verdict")
            spec, cfg = td.spec(), runner.config()
            changed = checked_drift(td, st)
            problem = (f"frozen files changed since submit: {changed}" if changed else
                       approval_problem(td, st, spec, cfg, changed))
            if problem:
                delivery.problem_locked(td, st, problem)
                return refuse(problem)
            try:
                if spec.get("on_pass"):
                    contract.positive_timeout(spec.get("on_pass_timeout", cfg.get("on_pass_timeout", 600)))
            except (contract.SpecError, ValueError) as exc:
                delivery.problem_locked(td, st, str(exc))
                print(f"delivery error: {exc}", file=sys.stderr)
                return 2
            conflicts = scheduling.scope_conflicts(td.task_id, spec) if spec.get("on_pass") else []
            if conflicts:
                reason = "；".join(f"收尾撞了 {c['task_id']} 的 {c['path']}，它完成后可重试" for c in conflicts)
                delivery.problem_locked(td, st, reason)
                return refuse(reason)
            if spec.get("on_pass"):
                delivery.reserve_locked(st, spec, who)
        try:
            rc = delivery.run_locked(td, st, spec, who, cfg, assume_stopped=assume_stopped)
        except (contract.SpecError, ValueError, OSError) as exc:
            with td.locked() as box:
                delivery.problem_locked(td, box["state"], str(exc))
            print(f"delivery error: {exc}", file=sys.stderr)
            return 2
    if echo:
        print(json.dumps({"task_id": td.task_id, "result": "delivery_failed" if rc else "delivered",
                          **delivery.view(td, td.state(), spec)}, ensure_ascii=False))
    return rc


def cmd_deliver(a) -> int:
    td = TaskDir(a.task_id)
    return do_deliver(td, a.as_, echo=True, assume_stopped=getattr(a, "assume_stopped", False)) \
        if td.exists else refuse(f"unknown task {a.task_id}")


def cmd_park(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    with td.locked() as box:
        st = box["state"]
        if st["owner"] != a.as_:
            return refuse(f"owner is {st['owner']}, not {a.as_}")
        if st["status"] in ("archived", "verified", "exhausted", "parked", "answered", "reviewing"):
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


def append_answer(td: TaskDir, actor: str, text: str, key: str | None = None) -> str:
    """Caller holds the task lock. Stable keys make review replies replayable."""
    d = td.path / "answers"
    d.mkdir(exist_ok=True)
    ordinal = len(list(d.glob("*.md"))) + 1
    if key:
        existing = sorted(d.glob(f"*-{key}.md"))
        key = existing[0].stem if existing else f"{ordinal:03d}-{key}"
    else:
        key = f"{ordinal:03d}"
    path = d / f"{key}.md"
    if not path.exists():
        temporary = d / f".{key}.{os.getpid()}.tmp"
        temporary.write_text(f"(answered by {actor} at {now()})\n\n{text.strip()}\n", encoding="utf-8")
        os.replace(temporary, path)
    if not any(e["kind"] == "task.answered" and e.get("answer") == key for e in td.events()):
        td.event("task.answered", actor, answer=key)
    return key


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
        progress = st.get("review", {})
        pending = any(r["status"] == "pending" for r in progress.get("rounds", []))
        config = td.spec().get("review")
        if progress.get("complete") or (config and not pending and len(progress.get("rounds", [])) >= config["rounds"]):
            return refuse("review round budget exhausted; submit a new task or archive this task")
        append_answer(td, a.as_, text)
        st["status"] = "answered"
        if pending:
            st["status"] = "reviewing"
            st["review_resume_requested"] = True
        st["question"]["answered_by"] = a.as_
    print(json.dumps({"task_id": td.task_id, "result": "answered", "note": "reconcile resumes it on behalf of the owner"}))
    return 0


def cmd_inbox(a) -> int:
    items = []
    cfg = runner.config()
    missing_pools = set()
    for t in list_tasks():
        td = TaskDir(t)
        st = td.state()
        ad = td.spec().get("adapter", {})
        if ad.get("kind") == "pool" and not pool.pools(cfg).get(ad["pool"]):
            missing_pools.add(ad["pool"])
        progress = delivery.view(td, st, td.spec())
        if st["status"] == "accepted" and td.spec().get("review_followup"):
            items.append({"task_id": t, "status": "accepted", "owner": st["owner"],
                          "question": "Review follow-up awaiting a dispatch decision",
                          "review_followup": td.spec()["review_followup"]})
            continue
        if progress["delivery_pending"] and (st["status"] == "verified" or progress["delivery_unknown"]):
            reason = progress["delivery_reason"]
            items.append({"task_id": t, "status": st["status"], "owner": st["owner"],
                          "delivery_failed": True, "reason": reason, "question": reason})
            continue
        if st["status"] not in ("archived", "verified", "exhausted") and td.spec().get("risk") == "l3":
            problem = approval_problem(td, st, td.spec(), cfg, contract.drift(frozen_of(td, st)))
            if problem:
                items.append({"task_id": t, "status": st["status"], "owner": st["owner"], "reason": "等审批",
                              "question": (st.get("question") or {}).get("text"), "hint": problem})
                continue
        if st["status"] in ("parked", "exhausted", "submit_unknown", "provider_refused") or (st["runs"] and st["runs"][-1]["state"] == "verify_error"):
            items.append({"task_id": t, "status": st["status"], "owner": st["owner"],
                          "question": (st.get("question") or {}).get("text")})
    for name in sorted(missing_pools):
        items.append({"pool": name, "task_id": None, "owner": None, "status": "pool_unconfigured",
                      "question": f"池子已不在配置中：{name}"})
    for name in pool.pools(cfg):
        backoff = pool.provider_backoff(name, cfg)
        if backoff:
            items.append({**backoff, "pool": name, "task_id": None, "owner": None, "question": backoff["reason"]})
        message = doctor.failure_message(name, doctor.pool_report(name))
        if message:
            items.append({"pool": name, "task_id": None, "owner": None, "status": "doctor_failed", "question": message})
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
    else:
        for i in items:
            if "until" in i:
                print(_backoff_text(i))
            else:
                print(f"{i['task_id'] or i.get('pool')} [{i['status']}] owner={i['owner']}\n  "
                      f"{i.get('reason') or i['question'] or '(no question recorded)'}")
    return 0


def cmd_reconcile(a) -> int:
    """Server-side upkeep, safe to run concurrently: every state change re-checks under the task lock."""
    cfg = runner.config()
    report = []
    to_close = []
    # Pure delivery recovery runs BEFORE acquiring schedule, and never waits for
    # an active controller. Include archived tasks and receipt-first crash states.
    for t in list_tasks():
        td = TaskDir(t)
        st, spec = td.state(), td.spec()
        if not st.get("delivery_reservation") and not delivery.view(td, st, spec)["delivery_pending"]:
            continue
        with delivery.locked(td, blocking=False) as acquired:
            if not acquired:
                continue
            reserved = bool(td.state().get("delivery_reservation"))
            reason = delivery.cleanup_locked(td, "reconciler")
            if reserved and not td.state().get("delivery_reservation"):
                report.append({"task_id": t, "delivery_reservation_released": True})
            elif reason:
                report.append({"task_id": t, "delivery_reason": reason})
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
                    receipt = runner.record_outcome(td, last["run_id"], lambda fresh: pool.unfinished_outcome(
                        fresh["pool"], fresh.get("station", ""), cfg, fresh, task_id=t))
                    if receipt:
                        report.append({"task_id": t, "run_id": last["run_id"], "observed": receipt["status"]})
                continue
            if last["state"] == "exited" and (spec.get("auto_close") or spec.get("review")) and st["status"] == "exited":
                to_close.append((td, st["owner"]))  # verified after the schedule lock is released (review 3, F1)
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
            elif st["status"] == "failed" and spec.get("review"):
                rc = do_dispatch_locked(td, st["owner"], "automatic retry within review failure budget")
                report.append({"task_id": t, "retried": rc == 0})
        # Poll all workers before releasing dependency waits or scope reservations. A verifier
        # runs outside this lock; its downstream tasks become eligible on the following tick.
        # Release dependency waits before sorting the queue, so their new queued_at participates
        # in the same FIFO ordering as cards that were already eligible.
        for t in list_tasks():
            td = TaskDir(t)
            st = td.state()
            if not st["runs"] or st["runs"][-1]["state"] != "waiting_deps":
                continue
            with td.locked() as box:
                st = box["state"]
                run = st["runs"][-1]
                if run["state"] != "waiting_deps":
                    continue
                _queue_gate(td, st, run, td.spec(), "reconciler")
                if run["state"] == "queued":
                    report.append({"task_id": t, "dependencies_ready": True})
                else:
                    report.append({"task_id": t, "state": st["status"], "reason": run["reason"]})
        cap = cfg.get("max_concurrent")
        queued = []
        for t in list_tasks():
            st = TaskDir(t).state()
            if st["runs"] and st["runs"][-1]["state"] == "queued":
                queued.append((st["runs"][-1].get("queued_at", ""), t))
        for _, t in sorted(queued):
            td = TaskDir(t)
            with td.locked() as box:
                st = box["state"]
                run = st["runs"][-1]
                if run["state"] != "queued":
                    continue  # a concurrent cancel won
                ready = _queue_gate(td, st, run, td.spec(), "reconciler")
                if not ready:
                    report.append({"task_id": t, "state": st["status"], "reason": run["reason"]})
                    continue
            if adapter_kind(td.spec()) == "pool":
                continue  # stations claim pool cards themselves
            if adapter_kind(td.spec()) == "direct" and ((cap is not None and machine_load(cfg) >= int(cap)) or not pool.resources_ok(cfg)[0]):
                continue
            rc = start_queued(td, "reconciler", actor_note="started from the queue", echo=False)
            run = td.state()["runs"][-1]
            action = {"task_id": t, "started_from_queue": rc == 0 and bool(run.get("started_at"))}
            if run["state"] in ("queued", "waiting_deps", "blocked_deps") and run.get("reason"):
                action["reason"] = run["reason"]
            report.append(action)
        for name in pool.schedule(cfg, direct_slots_in_use):
            report.append({"station_started": name})
    for td, owner in to_close:   # do_close re-checks state and run under the task lock
        rc, verdict = do_close(td, owner, actor_note="auto_close by reconcile")
        if verdict:
            action = {"task_id": td.task_id, "run_id": td.state()["verdict"]["run_id"], "auto_closed": verdict}
            if verdict == "PASS" and td.spec().get("on_pass") and td.state()["status"] == "verified":
                action.update(delivery.view(td, td.state(), td.spec()), delivery_exit_code=rc)
            report.append(action)
    for t in list_tasks():
        td = TaskDir(t)
        if td.state()["status"] == "reviewing":
            rc = review.advance(td)
            report.append({"task_id": t, "review_status": td.state()["status"], "review_exit_code": rc})
    notified = notify.notify_pending(cfg)
    print(json.dumps({"reconciled_at": now(), "actions": report, "notified": notified}, ensure_ascii=False))
    return 0


def cmd_archive(a) -> int:
    td = TaskDir(a.task_id)
    if not td.exists:
        return refuse(f"unknown task {a.task_id}")
    # Wait for delivery without holding schedule/task, including orphan groups
    # until their deadline. A legacy unknown process must first be acknowledged.
    with delivery.locked(td):
        st = td.state()
        if st["owner"] != a.as_:
            return refuse(f"owner is {st['owner']}, not {a.as_}")
        if _active_run(st):
            return refuse("a run is in flight")
        reason = delivery.cleanup_locked(td, a.as_, wait=True)
        if reason:
            return refuse(reason)
        # Archiving revokes verified dependencies; serialize with gate → start.
        with pool.schedule_lock(), td.locked() as box:
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
    metadata = f"\nScope: {', '.join(a.scope or []) or '(unspecified)'}\nRisk: {a.risk}\n"
    if a.acceptance is not None:
        metadata += f"Acceptance: {a.acceptance}\n"
    (card_dir / "CARD.md").write_text(text.strip() + QUICK_FOOTER.format(blocked=blocked, verify=a.verify) + metadata, encoding="utf-8")
    (card_dir / "verify.sh").write_text("set -e\n" + a.verify + "\n", encoding="utf-8")
    spec = {"task_id": tid, "project": a.project or (a.pool or "adhoc"), "goal": a.goal or text.strip().splitlines()[0][:120],
            "card": "CARD.md", "workdir": workdir, "outputs": a.output or [], "blocked_file": blocked,
            "verifier": {"command": f"bash {card_dir / 'verify.sh'}", "cwd": workdir, "freeze": ["verify.sh"]},
            "budget": {"max_attempts": a.attempts, "max_wall_min": a.wall}, "scope": a.scope or [], "risk": a.risk,
            "authority": a.authority or "Only what the card says; anything else needs a human."}
    if a.depends_on:
        spec["depends_on"] = list(a.depends_on)
    if a.auto_close:
        spec["auto_close"] = True
    if a.acceptance is not None:
        spec["acceptance"] = a.acceptance
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
    with pool.schedule_lock(), pool.pool_lock(pool_name):
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
                if not _queue_gate(td, st, run, spec, station):
                    continue
                problem = _launch_problem(td, st, spec, cfg, rec)
                if problem:
                    run.update(state="launch_failed", reason=problem); st["status"] = "accepted"
                    if spec.get("review"):
                        review.park(td, st, problem, run["run_id"])
                    continue
                rd = td.run_dir(run["run_id"]); rd.mkdir(parents=True, exist_ok=True)
                (rd / "prompt.md").write_text(runner.prompt_text(td, spec), encoding="utf-8")
                run.update(state="running", station=station, task_id=t, started_at=now(), claimed_epoch=time.time(),
                           channel=rec.get("channel", pool.channel(c)),
                           log_start=pool.log_position(pool_name, station),
                           blocked_before=runner.file_fingerprint(Path(spec["workdir"]) / spec.get("blocked_file", "out/BLOCKED.md")),
                           write_set=scheduling.write_set(spec))
                review.reserve(st, run, spec)
                st["status"] = "running"
                if st.get("approval"):
                    run["approval"] = dict(st["approval"])
                out = {"result": "CLAIMED", "task_id": t, "run_id": run["run_id"], "workdir": spec["workdir"],
                       "prompt": str(rd / "prompt.md"), "blocked_file": spec.get("blocked_file", "out/BLOCKED.md"),
                       "outputs": spec["outputs"]}
                deadline = time.time() + float(spec["budget"]["max_wall_min"]) * 60
            with pool.station_lock(pool_name, station):
                rec = pool.get_station(pool_name, station)
                rec.setdefault("tasks", []).append(f"{t}/{out['run_id']}")
                rec["current"] = {"task_id": t, "run_id": out["run_id"], "deadline": deadline}
                rec.pop("execution", None)
                rec.pop("execution_ref", None)
                write_json(pool.station_file(pool_name, station), rec)
            td.event("run.claimed", station, run_id=out["run_id"], pool=pool_name)
            return 0, out
    return 0, {"result": "EMPTY"}


def finish_card(task_id: str, run_id: str, station: str, late: bool = False) -> tuple[int, dict]:
    td = TaskDir(task_id)
    if not td.exists:
        return 3, {"result": "REFUSED", "reason": f"unknown task {task_id}"}
    st = td.state()
    run = next((r for r in st["runs"] if r["run_id"] == run_id), None)
    if not run or run.get("station") != station:
        return 3, {"result": "REFUSED", "reason": f"{run_id} was not claimed by {station}"}
    if run["state"] != "running":
        return 3, {"result": "REFUSED", "reason": f"{run_id} is no longer running"}
    spec = td.spec()
    srec = pool.get_station(run["pool"], station) or {}
    if late:
        # A late finish invokes the actual timeout handler; 124 alone is never timeout evidence.
        pool.record_execution(run["pool"], station, {"task_id": task_id, "run_id": run_id}, exit_code=124, timed_out=True)
        if srec.get("worker_pgid"):
            runner.kill_group(srec["worker_pgid"])
    bf = Path(spec["workdir"]) / spec.get("blocked_file", "out/BLOCKED.md")

    def classify(fresh_run):
        nonlocal run
        run = fresh_run  # record_outcome re-reads execution and commits while holding the task lock
        run.setdefault("log_end", pool.log_position(run["pool"], station))
        if "blocked_before" not in run and bf.is_file() and bf.stat().st_mtime < run.get("claimed_epoch", 0):
            run["blocked_before"] = runner.file_fingerprint(bf)  # conservative legacy fallback
        return pool.unfinished_outcome(run["pool"], station, runner.config(), run,
                                       default="exited", blocked_path=bf, task_id=task_id)

    rec = runner.record_outcome(td, run_id, classify)
    with pool.station_lock(run["pool"], station):  # clear the station's hold only if it is *this* card (review 3, F2)
        srec = pool.get_station(run["pool"], station) or {}
        cur = srec.get("current") or {}
        if cur.get("task_id") == task_id and cur.get("run_id") == run_id:
            srec["current"] = None
            srec["idle_log_offset"] = run.get("log_end", pool.log_position(run["pool"], station))
            write_json(pool.station_file(run["pool"], station), srec)
    if rec is None:
        return 3, {"result": "REFUSED", "reason": f"{run_id} is no longer running (timed out, lost or already finished)"}
    return 0, {"task_id": task_id, "run_id": run_id, "result": runner.STATE_OF.get(rec["status"], "exited")}


def _station_mailbox() -> bool:
    """True when this command runs inside a station AND targets the store that station serves.

    A worker may run nested commands (a test suite, a demo) with their own YARDGOAT_HOME; those must
    use their own store directly instead of handing requests to the real station wrapper.
    Stations started before YARDGOAT_MAILBOX_HOME existed only set YARDGOAT_MAILBOX (legacy: mailbox)."""
    if not os.environ.get("YARDGOAT_MAILBOX"):
        return False
    served = os.environ.get("YARDGOAT_MAILBOX_HOME")
    return served is None or Path(served).resolve() == home().resolve()


def _via_mailbox(op: dict) -> int:
    """Inside a station: hand the request to the station wrapper (outside any sandbox) and wait for its answer."""
    import uuid
    mb = Path(os.environ["YARDGOAT_MAILBOX"])
    rid = uuid.uuid4().hex[:12]
    tmp = mb / f".req-{rid}.tmp"
    end = time.time() + 300
    tmp.write_text(json.dumps({**op, "home": str(home().resolve()), "expires": end - 5}, ensure_ascii=False))
    os.replace(tmp, mb / f"req-{rid}.json")
    resp = mb / f"resp-{rid}.json"
    while time.time() < end:
        if resp.exists():
            r = json.loads(resp.read_text()); resp.unlink(missing_ok=True)
            print(json.dumps(r["out"], ensure_ascii=False))
            return r["rc"]
        time.sleep(0.2)
    (mb / f"req-{rid}.json").unlink(missing_ok=True)  # withdraw it; the broker also refuses expired requests
    print(json.dumps({"result": "ERROR", "reason": "station wrapper did not answer within 300 s"}))
    return 2


def cmd_claim(a) -> int:
    if _station_mailbox():
        return _via_mailbox({"op": "claim"})
    rc, out = claim_card(a.pool, a.station)
    print(json.dumps(out, ensure_ascii=False))
    return rc


def cmd_finish(a) -> int:
    if _station_mailbox():
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
        if not run or run["state"] not in ("queued", "waiting_deps"):
            return refuse("only a queued or waiting_deps run can be cancelled (stopping a running worker is not supported yet)")
        run["state"] = "cancelled"; st["status"] = "accepted"
    td.event("run.cancelled", a.as_, run_id=run["run_id"])
    print(json.dumps({"task_id": td.task_id, "result": "cancelled", "run_id": run["run_id"]}))
    return 0


def cmd_events(a) -> int:
    for ev in TaskDir(a.task_id).events():
        print(json.dumps(ev, ensure_ascii=False))
    return 0


def cmd_doctor(a) -> int:
    try:
        report = doctor.run(runner.config(), name=a.pool, host=a.host or "codex", lane=a.lane or "", acct=a.acct)
    except (RuntimeError, OSError, ValueError) as e:
        print(f"doctor error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for c in report["checks"]:
            print(f"{'PASS' if c['ok'] else 'FAIL'} {c['name']}: {c['reason']}")
    return 0 if report["ok"] else 1


def main(argv=None) -> int:
    P = argparse.ArgumentParser(prog="yardgoat", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = P.add_subparsers(dest="cmd", required=True)
    report.register(sub)
    s = sub.add_parser("doctor")
    g = s.add_mutually_exclusive_group(); g.add_argument("--pool"); g.add_argument("--host", choices=contract.HOSTS)
    s.add_argument("--lane"); s.add_argument("--acct"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("_doctor"); s.add_argument("run_dir")
    s = sub.add_parser("submit"); s.add_argument("spec"); s.add_argument("--as", dest="as_", required=True)
    s = sub.add_parser("dispatch"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s = sub.add_parser("status"); s.add_argument("task_id", nargs="?"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("takeover"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--from", dest="from_", required=True)
    s = sub.add_parser("close"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s = sub.add_parser("approve"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--note")
    s = sub.add_parser("deliver"); s.add_argument("task_id"); s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--assume-stopped", action="store_true",
                   help="record owner confirmation that unidentified legacy delivery processes have stopped")
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
    s.add_argument("--scope", action="append"); s.add_argument("--risk", choices=("l1", "l2", "l3"), default="l2")
    s.add_argument("--acceptance")
    s.add_argument("--attempts", type=int, default=2); s.add_argument("--wall", type=int, default=60)
    s.add_argument("--depends-on", action="append", help="task id that must be verified first (repeatable)")
    s.add_argument("--auto-close", action="store_true", help="reconcile verifies on the owner's behalf when the run exits")
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
    if hasattr(a, "handler"):
        return a.handler(a)
    if a.cmd == "_run":
        return runner.run_and_receipt(TaskDir(a.task_id), a.run_id)
    if a.cmd == "_station":
        return pool.run_station(a.pool_name, a.station)
    if a.cmd == "_doctor":
        return doctor.run_probe(Path(a.run_dir))
    return {"submit": cmd_submit, "dispatch": cmd_dispatch, "status": cmd_status, "takeover": cmd_takeover,
            "close": cmd_close, "approve": cmd_approve, "deliver": cmd_deliver, "park": cmd_park, "answer": cmd_answer, "inbox": cmd_inbox,
            "reconcile": cmd_reconcile, "archive": cmd_archive, "cancel": cmd_cancel, "quick": cmd_quick, "claim": cmd_claim, "finish": cmd_finish, "events": cmd_events, "doctor": cmd_doctor}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
