"""Read-only accounting from task state, receipts and events. No prices or model guesses."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from .store import TaskDir, home, list_tasks, read_json

DIMENSIONS = ("pool", "lane", "model", "account", "project", "host")
ABNORMAL = ("lost", "launch_failed", "submit_unknown", "verify_error", "provider_refused", "other")
# Match the dispatch budget; a queued/cancelled, blocked or refused launch is not an attempt.
ATTEMPTS = {"exited", "failed", "verified", "lost", "verify_error"}
NORMAL = {"accepted", "queued", "running", "exited", "verified", "failed", "exhausted",
          "blocked", "parked", "answered", "archived", "cancelled", "reviewing", "reviewed"}
ALIASES = {"passed": "verified", "verifier_error": "verify_error", "canceled": "cancelled"}
INTERVENTIONS = {"task.answered": "answer", "task.owner_changed": "takeover",
                 "task.archived": "archive", "run.cancelled": "cancel", "task.approved": "approve"}
METRICS = ("first_pass_count", "first_pass_rate", "avg_attempts", "avg_wall_seconds",
           "human_interventions", "abnormal", "cost")


def since_date(value: str) -> date:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("--since must be a valid YYYY-MM-DD date") from e


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        # store.now() writes +HHMM; Python 3.10's fromisoformat requires +HH:MM.
        value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _cost(basis) -> tuple[Decimal, int, bool]:
    """Sum only explicit, fully parseable fields; preserve partial unknowns at task level."""
    usd, tokens, parsed, invalid = Decimal(0), 0, False, False
    if isinstance(basis, str):
        fields = re.split(r"[\s;]+|,(?=\s*(?:usd|tokens)\s*=)", basis.strip())
        for field in fields:
            key, sep, value = field.partition("=")
            if not sep or key not in ("usd", "tokens"):
                continue
            value = value.rstrip(",")
            # Bound the text before converting: one absurd receipt must make that field "unknown", never break the report.
            if key == "usd" and len(value) <= 40 and re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?", value):
                try:
                    amount = Decimal(value)
                except (ArithmeticError, ValueError):
                    amount = None
                if amount is not None and amount.is_finite() and amount <= Decimal("1e300"):
                    usd += amount
                    parsed = True
                    continue
            elif key == "tokens" and len(value) <= 24 and re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)", value):
                try:
                    tokens += int(value.replace(",", ""))
                    parsed = True
                    continue
                except ValueError:
                    pass
            invalid = True
    return usd, tokens, invalid or not parsed


def _load(td: TaskDir):
    state, spec = td.state(), td.spec()
    receipts = {}
    for run in state.get("runs", []):
        path = td.run_dir(run["run_id"]) / "receipt.json"
        if path.exists():
            receipts[run["run_id"]] = read_json(path)
    return state, spec, receipts, td.events()


def _machine_actors(tasks) -> set[str]:
    actors = {"runner", "reconciler", "station"}
    for path in (home() / "pools").glob("*/stations/*.json"):
        actors.add(path.stem)
        actors.add(read_json(path).get("station", path.stem))
    for state, _, receipts, events in tasks:
        actors.update(r.get("station") for r in state.get("runs", []))
        actors.update(r.get("worker") for r in receipts.values())
        actors.update(e.get("actor") for e in events if e.get("kind") == "run.claimed")
    return actors


def _group_name(by, state, spec, receipts):
    runs = state.get("runs", [])
    run = runs[-1] if runs else {}
    receipt = next((receipts[r["run_id"]] for r in reversed(runs) if r["run_id"] in receipts), {})
    executor = spec.get("executor", {})
    if by == "pool":
        adapter = spec.get("adapter") or {}
        value = run.get("pool") or adapter.get("pool") or adapter.get("kind") or "direct"  # cards without a pool run through their adapter
    elif by == "project":
        value = spec.get("project") or receipt.get("project")
    elif by == "model":
        value = receipt.get("actual_model") or run.get("actual_model")
    elif by == "account":
        value = receipt.get("account_alias") or run.get("account_alias") or executor.get("acct")
    else:
        value = receipt.get(by) or run.get(by) or executor.get(by)
    return str(value) if value else "(unknown)"


def _statuses(run, receipt, events):
    values = [run.get("state"), receipt.get("status")]
    for event in events:
        kind = event.get("kind", "")
        if kind == "run.verified":
            values.append({"ERROR": "verify_error", "PASS": "verified", "FAIL": "failed"}.get(event.get("verdict")))
        elif kind in {"run." + s for s in ABNORMAL}:
            values.append(kind.removeprefix("run."))
        values.extend((event.get("status"), event.get("state")))
    return {ALIASES.get(value, value) for value in values if value}


def _abnormal_counts(statuses):
    found = {s if s in ABNORMAL else "other" for s in statuses if s not in NORMAL}
    return Counter(found)  # state, receipt and repeated events describe the same run outcome


def _facts(state, receipts, events, machine_actors):
    by_run = defaultdict(list)
    humans = Counter()
    for event in events:
        by_run[event.get("run_id")].append(event)
        actor = event.get("actor")
        action = INTERVENTIONS.get(event.get("kind"))
        if action and actor and actor not in machine_actors and not re.fullmatch(r".+-st-\d+", actor):
            humans[action] += 1
    attempts, first_pass, walls = 0, False, []
    abnormal = Counter()
    usd, tokens, unknown_cost = Decimal(0), 0, not bool(receipts)
    runs = state.get("runs", [])
    for i, run in enumerate(runs):
        receipt = receipts.get(run["run_id"], {})
        run_events = by_run[run["run_id"]]
        statuses = _statuses(run, receipt, run_events)
        if statuses & ATTEMPTS and run.get("state") not in ("reviewing", "reviewed"):
            attempts += 1
            if attempts == 1:
                verdicts = [e["verdict"] for e in run_events if e.get("kind") == "run.verified" and e.get("verdict")]
                verdict = verdicts[0] if verdicts else receipt.get("verifier", {}).get("result")
                first_pass = verdict == "PASS" if verdict else "verified" in statuses
        if i == len(runs) - 1 and state.get("status"):
            statuses.add(ALIASES.get(state["status"], state["status"]))
        abnormal.update(_abnormal_counts(statuses))
        start = _timestamp(run.get("started_at") or receipt.get("started_at"))
        if start is None:
            start = next((_timestamp(e.get("at")) for e in run_events if e.get("kind") == "run.started"), None)
        end = _timestamp(receipt.get("finished_at") or run.get("finished_at"))
        if start and end and end >= start:
            walls.append((end - start).total_seconds())
        dollars, used_tokens, unknown = _cost(receipt.get("cost_basis"))
        usd += dollars
        tokens += used_tokens
        unknown_cost |= unknown
    if not runs and state.get("status"):
        abnormal.update(_abnormal_counts({ALIASES.get(state["status"], state["status"])}))
    return {"attempts": attempts, "first_pass": first_pass, "walls": walls, "humans": humans,
            "abnormal": abnormal, "usd": usd, "tokens": tokens, "unknown_cost": unknown_cost}


def build_report(by: str = "pool", since: date | None = None) -> dict:
    tasks = [_load(TaskDir(t)) for t in list_tasks()]
    machine_actors = _machine_actors(tasks)
    groups = defaultdict(list)
    for state, spec, receipts, events in tasks:
        accepted = _timestamp(state.get("accepted_at"))
        if since and (accepted is None or accepted.date() < since):
            continue
        groups[_group_name(by, state, spec, receipts)].append(_facts(state, receipts, events, machine_actors))
    out = []
    for name, facts in sorted(groups.items()):
        count = len(facts)
        group = {"name": name, "count": count, "insufficient": count < 5}
        if count < 5:
            group.update(dict.fromkeys(METRICS))
        else:
            first_pass = sum(f["first_pass"] for f in facts)
            walls = [w for f in facts for w in f["walls"]]
            humans = {k: sum(f["humans"][k] for f in facts) for k in INTERVENTIONS.values()}
            group.update(first_pass_count=first_pass, first_pass_rate=first_pass / count,
                         avg_attempts=sum(f["attempts"] for f in facts) / count,
                         avg_wall_seconds=sum(walls) / len(walls) if walls else None,
                         human_interventions={"total": sum(humans.values()), **humans},
                         abnormal={k: sum(f["abnormal"][k] for f in facts) for k in ABNORMAL},
                         cost={"usd": float(sum((f["usd"] for f in facts), Decimal(0))),
                               "tokens": sum(f["tokens"] for f in facts),
                               "unknown_tasks": sum(f["unknown_cost"] for f in facts)})
        out.append(group)
    return {"by": by, "since": since.isoformat() if since else None, "groups": out}


def _table(data):
    columns = [data["by"], "tasks", "data", "first PASS", "avg attempts", "avg wall (s)",
               "human interventions", "abnormal outcomes", "USD", "tokens", "unknown cost tasks"]
    rows = [columns, ["---"] * len(columns)]
    for group in data["groups"]:
        name = group["name"].replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        if group["insufficient"]:
            rows.append([name, str(group["count"]), "数据不足", *([""] * 8)])
            continue
        humans = group["human_interventions"]
        rows.append([name, str(group["count"]), "", f"{group['first_pass_rate']:.1%}",
                     f"{group['avg_attempts']:.2f}",
                     f"{group['avg_wall_seconds']:.2f}" if group["avg_wall_seconds"] is not None else "unknown",
                     ", ".join(f"{k}={v}" for k, v in humans.items()),
                     ", ".join(f"{k}={v}" for k, v in group["abnormal"].items()),
                     str(group["cost"]["usd"]), str(group["cost"]["tokens"]), str(group["cost"]["unknown_tasks"])])
    return "\n".join("| " + " | ".join(row) + " |" for row in rows)


def cmd_report(args) -> int:
    data = build_report(args.by, args.since)
    print(json.dumps(data, ensure_ascii=False, indent=1) if args.json else _table(data))
    return 0


def register(subparsers):
    parser = subparsers.add_parser("report", help="summarize recorded outcomes and costs (no rankings)")
    parser.add_argument("--since", type=since_date, metavar="YYYY-MM-DD", help="inclusive acceptance date")
    parser.add_argument("--by", choices=DIMENSIONS, default="pool")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=cmd_report)
