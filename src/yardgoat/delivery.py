"""Recoverable owner-side delivery. Shell commands hold only the delivery mutex."""
from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import sys
import time

from . import contract, runner
from .store import TaskDir, now, read_json, write_json


@contextlib.contextmanager
def locked(td: TaskDir):
    # Take this BEFORE schedule → task; waiting for a slow delivery never holds either.
    with (td.path / ".delivery.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


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
    pending = bool(st.get("status") == "verified" and spec.get("on_pass") and st.get("verdict", {})
                   and st["verdict"]["verdict"] == "PASS" and not complete(spec["on_pass"], records))
    return {"on_pass": records, "delivery_failed": pending, "delivery_pending": pending,
            "delivery_reason": (reason or failure_reason(records) or "收尾未完成") if pending else None}


def mark_pending(st: dict, receipt: dict, reason: str = "收尾未完成") -> None:
    for target in (st, receipt):
        target.update(delivery_pending=True, delivery_failed=True, delivery_reason=reason)


def problem_locked(td: TaskDir, st: dict, reason: str) -> None:
    """Caller holds the task lock; preserve PASS and record preflight failures too."""
    path = td.run_dir(st["verdict"]["run_id"]) / "receipt.json"
    receipt = read_json(path)
    mark_pending(st, receipt, reason)
    write_json(path, receipt)


def replay_finished(td: TaskDir, run_id: str, records: list[dict]) -> None:
    """Receipt is the outbox; replay before skipping success or replacing a failed attempt."""
    events = td.events()
    for record in records:
        if not record.get("finished_at"):
            continue
        if any(e["kind"] == "task.on_pass_finished" and e.get("run_id") == run_id
               and e.get("step") == record["step"] and e.get("attempt") == record["attempt"] for e in events):
            continue
        event_id = f"{td.task_id}:{run_id}:on_pass:{record['step']}:{record['attempt']}:finished"
        events.append(td.event("task.on_pass_finished", record["by"], run_id=run_id, event_id=event_id,
                               **{k: v for k, v in record.items() if k != "by"}))


def run_locked(td: TaskDir, st: dict, spec: dict, who: str, cfg: dict) -> int:
    """Caller owns the delivery mutex and a durable write-set reservation, not the task lock."""
    actions = spec.get("on_pass", [])
    if not actions:
        return 0
    timeout = contract.positive_timeout(spec.get("on_pass_timeout", cfg.get("on_pass_timeout", 600)))
    run_id = st["verdict"]["run_id"]
    rd = td.run_dir(run_id)
    receipt_path = rd / "receipt.json"
    receipt = read_json(receipt_path)
    if receipt.get("verifier", {}).get("result") != "PASS":
        raise ValueError("on_pass requires a PASS receipt")
    records = receipt.setdefault("on_pass", [])

    def save(reason=None, release=False):
        pending = not complete(actions, records)
        fields = {"on_pass": records, "delivery_pending": pending, "delivery_failed": pending,
                  "delivery_reason": (reason or failure_reason(records) or "收尾未完成") if pending else None}
        receipt.update(fields)
        write_json(receipt_path, receipt)
        with td.locked() as box:
            # Never overwrite a takeover/archive that happened during the shell command.
            box["state"].update(fields)
            if release:
                box["state"].pop("delivery_reservation", None)
        st.update(fields)

    def tail(record):
        path = rd / f"on-pass-{record['step']}-{record['attempt']}.log"
        if path.exists():
            with path.open("rb") as log:
                log.seek(max(0, path.stat().st_size - 4000))
                record["output_tail"] = log.read().decode("utf-8", errors="replace")

    replay_finished(td, run_id, records)
    for index, step in enumerate(actions):
        previous = records[index] if index < len(records) else None
        if previous and runner.pgid_alive(previous.get("pgid")):
            deadline = previous.get("deadline", float("inf"))
            if time.time() < deadline:
                save(f"等待收尾第 {index + 1} 步 {previous['name']} 的进程组 {previous['pgid']}，截止时间 {deadline}")
                return 3  # reservation stays until the old group has actually exited
            runner.kill_group(previous["pgid"], grace=0.5)
            if runner.pgid_alive(previous["pgid"]):
                save(f"收尾进程组 {previous['pgid']} 超过截止时间 {deadline}，尚未确认退出")
                return 3
            previous.update(exit_code=124, timed_out=True, finished_at=now())
            tail(previous)
            save()
            replay_finished(td, run_id, records)
        if previous and previous.get("exit_code") == 0:
            continue
        if previous and not previous.get("finished_at"):
            previous.update(exit_code=125, finished_at=now(), interrupted=True)
            tail(previous)
            save()
            replay_finished(td, run_id, records)
        fresh = td.state()
        if fresh["owner"] != who or fresh["status"] != "verified":
            save(f"收尾暂停：当前 owner={fresh['owner']}，status={fresh['status']}", release=True)
            return 3
        record = {"step": index + 1, "name": step["name"], "command": step["command"],
                  "cwd": step.get("cwd", spec["workdir"]), "by": who, "started_at": now(),
                  "attempt": (previous or {}).get("attempt", 0) + 1,
                  "exit_code": None, "output_tail": "", "timed_out": False,
                  "pgid": None, "deadline": time.time() + timeout}
        if previous is None:
            records.append(record)
        else:
            records[index] = record
        save()
        td.event("task.on_pass_started", who, run_id=run_id, **{k: v for k, v in record.items() if k != "by"})
        log_path = rd / f"on-pass-{index + 1}-{record['attempt']}.log"
        env = {**os.environ, "YARDGOAT_TASK_ID": td.task_id, "YARDGOAT_RUN_DIR": str(rd), "YARDGOAT_OWNER": who}
        # The child cannot execute the shell until its PGID/deadline are durable. EOF on
        # controller death before the grant exits harmlessly, closing the Popen/save gap.
        gate = ("import os,sys; grant=sys.stdin.buffer.read(1); "
                "sys.stdin.close(); "
                "fd=os.open(os.devnull,os.O_RDONLY); os.dup2(fd,0); "
                "os.execv('/bin/sh',['sh','-c',sys.argv[1]]) if grant==b'1' else sys.exit(125)")
        with log_path.open("wb") as log:
            try:
                process = subprocess.Popen([sys.executable, "-c", gate, step["command"]],
                                           cwd=record["cwd"], env=env, stdin=subprocess.PIPE,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                record["pgid"] = process.pid
                try:
                    save()
                    # Serialize the execution grant with takeover/archive, without holding
                    # the task lock during the command or waiting under the scheduler lock.
                    with td.locked() as box:
                        fresh = box["state"]
                        if fresh["owner"] == who and fresh["status"] == "verified":
                            process.stdin.write(b"1")
                            process.stdin.flush()
                finally:
                    process.stdin.close()
                try:
                    record["exit_code"] = process.wait(timeout=max(0, record["deadline"] - time.time()))
                    # A shell can exit while background children still own the write set.
                    while runner.pgid_alive(process.pid) and time.time() < record["deadline"]:
                        time.sleep(0.05)
                    if runner.pgid_alive(process.pid):
                        raise subprocess.TimeoutExpired(step["command"], timeout)
                except subprocess.TimeoutExpired:
                    runner.kill_group(process.pid, grace=0.5)
                    process.wait()
                    record.update(exit_code=124, timed_out=True)
            except OSError as exc:
                record["exit_code"] = 127
                log.write(str(exc).encode("utf-8", errors="replace"))
        if runner.pgid_alive(record.get("pgid")):
            record["exit_code"] = None
            save(f"收尾进程组 {record['pgid']} 尚未确认退出，截止时间 {record['deadline']}")
            return 3
        tail(record)
        record["finished_at"] = now()
        save()
        replay_finished(td, run_id, records)
        if record["exit_code"] != 0:
            save(release=True)
            return 1
    save(release=True)
    return 0
