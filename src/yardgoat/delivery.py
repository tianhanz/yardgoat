"""Owner-side post-verification commands. Caller holds the task lock, never the schedule lock."""
from __future__ import annotations

import os
import subprocess

from . import contract, runner
from .store import TaskDir, now, read_json, write_json


def failure_reason(records: list[dict]) -> str | None:
    return next((f"收尾第 {r['step']} 步 {r['name']} 失败" for r in records if r.get("exit_code") != 0), None)


def run_locked(td: TaskDir, st: dict, spec: dict, who: str, cfg: dict) -> int:
    """Persist each completed step before starting the next; the receipt is the progress journal.

    Receipt-first writes allow recovery if the controller dies before committing task.json.
    External side effects cannot be atomic with this journal: commands should be idempotent
    for the crash window after execution but before a successful result is recorded.
    """
    actions = spec.get("on_pass", [])
    if not actions:
        return 0
    timeout = contract.positive_timeout(spec.get("on_pass_timeout", cfg.get("on_pass_timeout", 600)))
    rd = td.run_dir(st["verdict"]["run_id"])
    receipt_path = rd / "receipt.json"
    receipt = read_json(receipt_path)
    if receipt.get("verifier", {}).get("result") != "PASS":
        raise ValueError("on_pass requires a PASS receipt")
    records = receipt.setdefault("on_pass", [])

    def save():
        failed = failure_reason(records)
        receipt["delivery_failed"] = st["delivery_failed"] = failed is not None
        st["delivery_reason"] = failed
        st["on_pass"] = records
        write_json(receipt_path, receipt)
        write_json(td.path / "task.json", st)

    for index, step in enumerate(actions):
        previous = records[index] if index < len(records) else None
        if previous and previous.get("exit_code") == 0:
            continue
        record = {"step": index + 1, "name": step["name"], "command": step["command"],
                  "cwd": step.get("cwd", spec["workdir"]), "by": who, "started_at": now(),
                  "attempt": (previous or {}).get("attempt", 0) + 1,
                  "exit_code": None, "output_tail": "", "timed_out": False}
        if previous is None:
            records.append(record)
        else:
            records[index] = record
        save()  # an interrupted step remains retryable, with its attempt recorded
        td.event("task.on_pass_started", who, run_id=st["verdict"]["run_id"], **{k: v for k, v in record.items() if k != "by"})
        log_path = rd / f"on-pass-{index + 1}-{record['attempt']}.log"
        env = {**os.environ, "YARDGOAT_TASK_ID": td.task_id, "YARDGOAT_RUN_DIR": str(rd), "YARDGOAT_OWNER": who}
        with log_path.open("wb") as log:
            try:
                process = subprocess.Popen(step["command"], shell=True, cwd=record["cwd"], env=env,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                try:
                    record["exit_code"] = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    runner.kill_group(process.pid, grace=0.5)
                    process.wait()
                    record.update(exit_code=124, timed_out=True)
            except OSError as exc:
                record["exit_code"] = 127
                log.write(str(exc).encode("utf-8", errors="replace"))
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 4000))
            record["output_tail"] = log.read().decode("utf-8", errors="replace")
        record["finished_at"] = now()
        save()
        td.event("task.on_pass_finished", who, run_id=st["verdict"]["run_id"], **{k: v for k, v in record.items() if k != "by"})
        if record["exit_code"] != 0:
            return 1
    save()  # also repairs task.json after a receipt-first commit interrupted by a crash
    return 0
