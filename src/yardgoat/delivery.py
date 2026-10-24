"""Recoverable owner-side delivery. Shell commands hold only the delivery mutex."""
from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import contract, control, runner, sessions
from .store import TaskDir, now, read_json, write_json

UNKNOWN_REASON = "旧版本留下的收尾记录，进程状态未知；确认旧进程已停止后用 deliver --assume-stopped 继续"


@contextlib.contextmanager
def locked(td: TaskDir, blocking: bool = True):
    # Take this BEFORE schedule → task; upkeep skips busy controllers, never waits
    # for this mutex while holding the global schedule lock.
    with (td.path / ".delivery.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def process_identity(pgid) -> dict | None:
    if not pgid:
        return None
    try:
        fields = Path(f"/proc/{int(pgid)}/stat").read_text().rsplit(") ", 1)[1].split()
        if fields[2] != str(pgid):
            return None
        return {"start_ticks": fields[19], "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, ValueError, IndexError):
        return None


def group_alive(record: dict) -> bool:
    identity = record.get("process_identity")
    return bool(identity and process_identity(record.get("pgid")) == identity
                and runner.pgid_alive(record["pgid"]))


def kill_group(record: dict, grace: float = 0.5) -> None:
    # Recheck the full identity before EVERY signal, including escalation. The
    # delivery worker retains its group-leader identity while descendants live.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_alive(record):
            return
        try:
            os.killpg(record["pgid"], sig)
        except ProcessLookupError:
            return
        end = time.monotonic() + grace
        while group_alive(record) and time.monotonic() < end:
            time.sleep(0.02)


def unknown(record: dict) -> bool:
    if record.get("exit_code") == 0 or record.get("finished_at") or record.get("process_identity"):
        return False
    # Only the new grant protocol proves that a missing identity means the shell
    # was never permitted to execute. Old formats cannot make this assertion.
    return not (record.get("launch_protocol") == 2 and record.get("pgid") is None)


def complete(actions: list[dict], records: list[dict]) -> bool:
    return len(records) == len(actions) and all(r.get("exit_code") == 0 for r in records)


def failure_reason(records: list[dict]) -> str | None:
    return next((f"收尾第 {r['step']} 步 {r['name']} " + ("未完成" if r.get("exit_code") is None else "失败")
                 for r in records if r.get("exit_code") != 0), None)


def view(td: TaskDir, st: dict, spec: dict) -> dict:
    """Read-only projection, including legacy and receipt-first crash recovery."""
    records = st.get("on_pass", [])
    reason = st.get("delivery_reason")
    if st.get("verdict") and spec.get("on_pass"):
        path = td.run_dir(st["verdict"]["run_id"]) / "receipt.json"
        if path.exists():
            receipt = read_json(path)
            records = receipt.get("on_pass", [])
            reason = receipt.get("delivery_reason", reason)
    pending = bool(st.get("status") in ("verified", "archived") and spec.get("on_pass")  # not while reviewing
                   and st.get("verdict", {})
                   and st["verdict"]["verdict"] == "PASS" and not complete(spec["on_pass"], records))
    uncertain = pending and any(unknown(r) for r in records)
    return {"on_pass": records, "delivery_failed": pending, "delivery_pending": pending,
            "delivery_unknown": bool(uncertain),
            "delivery_reason": (UNKNOWN_REASON if uncertain else reason or failure_reason(records) or "收尾未完成")
            if pending else None}


def mark_pending(st: dict, receipt: dict, reason: str = "收尾未完成") -> None:
    for target in (st, receipt):
        target.update(delivery_pending=True, delivery_failed=True, delivery_reason=reason)


def problem_locked(td: TaskDir, st: dict, reason: str) -> None:
    """Caller holds the task lock; preserve PASS and record preflight failures too."""
    path = td.run_dir(st["verdict"]["run_id"]) / "receipt.json"
    receipt = read_json(path)
    mark_pending(st, receipt, reason)
    write_json(path, receipt)


def reserve_locked(st: dict, spec: dict, who: str, records=(), replace=False) -> None:
    """Caller owns schedule → task. Shrinking is allowed only at a new attempt."""
    from . import scheduling
    paths = set(scheduling.write_set(spec))
    if not replace:
        paths.update(st.get("delivery_reservation", {}).get("write_set", []))
        for record in records:
            if record.get("exit_code") != 0 and not record.get("finished_at"):
                paths.update(record.get("write_set", []))
    st["delivery_reservation"] = {"write_set": sorted(paths), "by": who,
                                  "run_id": st["verdict"]["run_id"], "at": now()}


def replay_finished(td: TaskDir, run_id: str, records: list[dict], *, recovering=True) -> None:
    """Receipt is the outbox; replay before skipping success or replacing a failed attempt."""
    events = td.events()
    for record in records:
        if not record.get("finished_at"):
            continue
        if any(e["kind"] == "task.on_pass_finished" and e.get("run_id") == run_id
               and e.get("step") == record["step"] and e.get("attempt") == record["attempt"] for e in events):
            continue
        event_id = f"{td.task_id}:{run_id}:on_pass:{record['step']}:{record['attempt']}:finished"
        fields = {k: v for k, v in record.items() if k != "by"}
        origin = {k: fields.pop(k) for k in ("actor_session", "epoch", "on_behalf_of") if k in fields}
        if recovering:
            # Recovery is a system action even when deliver/archive invoked it.
            # Do not merge a new controller's ticket with the old execution grant.
            with control.system():
                audit = control.event_fields(td)
            if origin:
                fields["execution_control"] = origin
        else:
            audit = origin
            if "on_behalf_of" in origin:
                audit = {"actor": "reconciler", **origin}
        events.append(td.event("task.on_pass_finished", record["by"], control_fields=audit,
                               run_id=run_id, event_id=event_id, **fields))


class Journal:
    """The delivery mutex protects receipt progress; short commits preserve takeover."""
    def __init__(self, td, st, spec):
        self.td, self.st, self.spec = td, st, spec
        self.run_id = st["verdict"]["run_id"]
        self.rd = td.run_dir(self.run_id)
        self.path = self.rd / "receipt.json"
        self.receipt = read_json(self.path)
        if self.receipt.get("verifier", {}).get("result") != "PASS":
            raise ValueError("on_pass requires a PASS receipt")
        self.records = self.receipt.setdefault("on_pass", [])

    def save(self, reason=None, release=False, reserve=False):
        from . import pool
        pending = not complete(self.spec.get("on_pass", []), self.records)
        fields = {"on_pass": self.records, "delivery_pending": pending, "delivery_failed": pending,
                  "delivery_reason": (reason or failure_reason(self.records) or "收尾未完成") if pending else None}
        # Result fields and reservation release share one task.json commit. Receipt
        # remains first: upkeep can finish a crash between the two atomic files.
        with pool.schedule_lock(), self.td.locked() as box:
            self.receipt.update(fields)
            write_json(self.path, self.receipt)
            fresh = box["state"]
            fresh.update(fields)
            if release:
                fresh.pop("delivery_reservation", None)
            elif reserve:
                reserve_locked(fresh, self.spec, fresh["owner"], self.records)
        self.st.update(fields)

    def tail(self, record):
        path = self.rd / f"on-pass-{record['step']}-{record['attempt']}.log"
        if path.exists():
            with path.open("rb") as log:
                log.seek(max(0, path.stat().st_size - 4000))
                record["output_tail"] = log.read().decode("utf-8", errors="replace")

    def replay(self, *, recovering=True):
        replay_finished(self.td, self.run_id, self.records, recovering=recovering)

    def recover(self, who, wait=False, assume_stopped=False, release=False) -> str | None:
        self.replay()
        for record in self.records:
            # A committed success is terminal, even with stale/reused PGID fields.
            if record.get("exit_code") == 0 or record.get("finished_at"):
                continue
            if unknown(record):
                if not assume_stopped:
                    self.save(UNKNOWN_REASON, reserve=True)
                    return UNKNOWN_REASON
                # Log the human assertion BEFORE it enables any retry or release.
                with control.locked(self.td) as box:
                    owner = box["state"]["owner"]
                    permitted = control.allowed(box["state"], who)
                    if permitted:
                        self.td.event("task.delivery_assumed_stopped", who, run_id=self.run_id,
                                      step=record["step"], attempt=record["attempt"], assume_stopped=True,
                                      reason=UNKNOWN_REASON)
                if not permitted:
                    reason = f"owner is {owner}, not {who}；{UNKNOWN_REASON}"
                    self.save(reason, reserve=True)
                    return reason
                record["assumed_stopped_by"] = who
                record["assumed_stopped_at"] = now()
            else:
                deadline = record.get("deadline", float("inf"))
                while wait and group_alive(record) and time.time() < deadline:
                    time.sleep(0.05)
                if group_alive(record):
                    if time.time() < deadline:
                        reason = (f"等待收尾第 {record['step']} 步 {record['name']} 的进程组 "
                                  f"{record['pgid']}，截止时间 {deadline}")
                        self.save(reason, reserve=True)
                        return reason
                    kill_group(record)
                    if group_alive(record):
                        reason = f"收尾进程组 {record['pgid']} 超过截止时间 {deadline}，尚未确认退出"
                        self.save(reason, reserve=True)
                        return reason
                    record.update(exit_code=124, timed_out=True)
            if record.get("exit_code") is None:
                record.update(exit_code=125, interrupted=True)
            record["finished_at"] = now()
            self.tail(record)
            self.save()
            self.replay()
        self.save(release=release)
        return None


def cleanup_locked(td: TaskDir, who: str, wait=False, assume_stopped=False) -> str | None:
    """Pure recovery, including archived tasks. Caller owns the delivery mutex."""
    st, spec = td.state(), td.spec()
    if (st.get("verdict") or {}).get("verdict") != "PASS" or not spec.get("on_pass"):
        return None
    return Journal(td, st, spec).recover(who, wait=wait, assume_stopped=assume_stopped, release=True)


def run_locked(td: TaskDir, st: dict, spec: dict, who: str, cfg: dict, assume_stopped=False) -> int:
    """Caller owns the delivery mutex and a durable reservation, not the task lock."""
    from . import pool, scheduling
    actions = spec.get("on_pass", [])
    if not actions:
        return 0
    timeout = contract.positive_timeout(spec.get("on_pass_timeout", cfg.get("on_pass_timeout", 600)))
    journal = Journal(td, st, spec)
    if journal.recover(who, assume_stopped=assume_stopped):
        return 3
    records = journal.records
    for index, step in enumerate(actions):
        previous = records[index] if index < len(records) else None
        if previous and previous.get("exit_code") == 0:
            continue
        # All old attempts have now been proved stopped. Recheck and replace the
        # union only when actually preparing a fresh attempt, under the schedule lock.
        try:
            with pool.schedule_lock(), control.locked(td) as box:
                fresh = box["state"]
                if not control.allowed(fresh, who) or fresh["status"] != "verified":
                    reason = f"收尾暂停：当前 owner={fresh['owner']}，status={fresh['status']}"
                else:
                    conflicts = scheduling.scope_conflicts(td.task_id, spec)
                    reason = "；".join(f"收尾撞了 {c['task_id']} 的 {c['path']}，它完成后可重试" for c in conflicts)
                if not reason:
                    reserve_locked(fresh, spec, who, replace=True)
                    paths = fresh["delivery_reservation"]["write_set"]
        except sessions.Refused as exc:
            journal.save(str(exc), release=True)
            return 3
        if reason:
            journal.save(reason, release=True)
            return 3
        record = {"step": index + 1, "name": step["name"], "command": step["command"],
                  "cwd": step.get("cwd", spec["workdir"]), "by": who, "started_at": now(),
                  "attempt": (previous or {}).get("attempt", 0) + 1,
                  "exit_code": None, "output_tail": "", "timed_out": False,
                  "launch_protocol": 2, "process_identity": None, "write_set": paths,
                  "pgid": None, "deadline": time.time() + timeout,
                  **{k: v for k, v in control.audit(st).items() if k != "actor"}}
        if previous is None:
            records.append(record)
        else:
            records[index] = record
        journal.save()
        td.event("task.on_pass_started", who, run_id=journal.run_id,
                 **{k: v for k, v in record.items() if k != "by"})
        log_path = journal.rd / f"on-pass-{index + 1}-{record['attempt']}.log"
        env = {**os.environ, "YARDGOAT_TASK_ID": td.task_id,
               "YARDGOAT_RUN_DIR": str(journal.rd), "YARDGOAT_OWNER": who}
        grant_refused = None
        with log_path.open("wb") as log:
            try:
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("delivery_worker.py")),
                                            step["command"]],
                                           cwd=record["cwd"], env=env, stdin=subprocess.PIPE,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                record["pgid"] = process.pid
                record["process_identity"] = process_identity(process.pid)
                try:
                    if record["process_identity"] is None:
                        raise OSError("cannot identify delivery group leader; execution grant withheld")
                    journal.save()
                    # No shell runs before identity + deadline + write set are durable.
                    try:
                        with control.locked(td) as box:
                            fresh = box["state"]
                            if control.allowed(fresh, who) and fresh["status"] == "verified":
                                # The one-byte execution grant is the commit point.
                                process.stdin.write(b"1")
                                process.stdin.flush()
                            else:
                                grant_refused = "task or owner changed before delivery grant"
                    except sessions.Refused as exc:
                        grant_refused = str(exc)
                finally:
                    process.stdin.close()
                try:
                    record["exit_code"] = process.wait(timeout=max(0, record["deadline"] - time.time()))
                except subprocess.TimeoutExpired:
                    kill_group(record)
                    # Do not wait without a bound on a group that could not be stopped.
                    if not group_alive(record):
                        process.wait(timeout=1)
                    record.update(exit_code=124, timed_out=True)
            except OSError as exc:
                record["exit_code"] = 127
                log.write(str(exc).encode("utf-8", errors="replace"))
        if group_alive(record):
            record["exit_code"] = None
            journal.save(f"收尾进程组 {record['pgid']} 尚未确认退出，截止时间 {record['deadline']}")
            return 3
        journal.tail(record)
        record["finished_at"] = now()
        # The terminal result and release land together, before result-event replay.
        terminal = record["exit_code"] != 0 or index == len(actions) - 1
        journal.save(grant_refused, release=terminal)
        journal.replay(recovering=False)
        if grant_refused:
            return 3
        if record["exit_code"] != 0:
            return 1
    journal.save(release=True)
    return 0
