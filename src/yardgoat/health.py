"""Reconciler observability, never a controller lease or authorization input."""
import math
import time

from .report import _timestamp
from .statefile import locked_json
from .store import home, now, read_json


def read():
    try:
        return read_json(home() / "reconcile-health.json")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return {"read_error": str(exc)}


def start():
    with locked_json(home() / "reconcile-health.json") as box:
        if box["state"] is None:
            box["state"] = {"last_started_at": None, "last_finished_at": None,
                            "last_success_at": None, "errors": {"count": 0, "first": []}}
        box["state"]["last_started_at"] = now()


def finish(errors):
    with locked_json(home() / "reconcile-health.json") as box:
        state = box["state"]
        state["last_finished_at"] = now()
        if not errors:
            state["last_success_at"] = state["last_finished_at"]
        state["errors"] = {"count": len(errors), "first": [str(e)[:1000] for e in errors[:5]]}
    return state


def check(cfg):
    state = read()
    threshold = cfg.get("reconcile_stale_min", 10)
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("reconcile_stale_min must be finite and positive")
    # A new installation has no completed round to age. Keep launch doctor backwards compatible.
    if state is None:
        return {"name": "reconcile_health", "ok": True, "reason": "no reconcile round recorded yet"}
    ended = _timestamp(state.get("last_finished_at"))
    ok = ended is not None and 0 <= time.time() - ended.timestamp() <= threshold * 60
    return {"name": "reconcile_health", "ok": ok,
            "reason": (f"last reconcile finished at {state['last_finished_at']}" if ok else
                       f"reconcile is stale or has never finished (threshold {threshold} minutes)")}
