"""Yardgoat's own pool: a station claims cards one after another. The station here is a shell script, no model."""
import json
import time
from pathlib import Path

from test_contract import SRC, env, make_task, wait_exit, yg  # noqa: F401

STATION = r'''
st="$0"
while true; do
  out=$(python3 -m yardgoat.cli claim --pool p --station "$st")
  case "$out" in *EMPTY*) exit 0;; esac
  read tid rid wd <<<"$(python3 -c "import json,sys;d=json.loads(sys.argv[1]);print(d['task_id'],d['run_id'],d['workdir'])" "$out")"
  sleep 0.3
  (cd "$wd" && mkdir -p out && echo done > out/result.txt)
  python3 -m yardgoat.cli finish "$tid" --run "$rid" --station "$st" >/dev/null
done
'''


def pool_cfg(env, tmp_path, **kw):
    c = {"pools": {"p": {"host": "shell", "command": STATION, "root": str(tmp_path / "root"), "max_stations": 1, **kw}}}
    Path(env["YARDGOAT_CONFIG"]).write_text(json.dumps(c))
    (tmp_path / "root").mkdir(exist_ok=True)


def pool_task(tmp_path, env, tid, **extra):
    wd = tmp_path / "root" / tid
    spec = make_task(tmp_path / f"spec-{tid}", task_id=tid)
    s = json.loads(spec.read_text())
    s.update(workdir=str(wd), adapter={"kind": "pool", "pool": "p"}, **extra)
    (wd / "out").mkdir(parents=True, exist_ok=True)
    s["verifier"]["command"] = f"test -s {wd}/out/result.txt"
    spec.write_text(json.dumps(s))
    assert yg(env, "submit", str(spec), "--as", "A")[0] == 0
    return spec


def stations(env):
    return [json.loads(f.read_text()) for f in sorted((Path(env["YARDGOAT_HOME"]) / "pools/p/stations").glob("*.json"))]


def wait_all(env, tids, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        sts = [json.loads(yg(env, "status", t, "--json")[1]) for t in tids]
        if all(s["runs"] and s["runs"][-1]["state"] not in ("queued", "running") for s in sts):
            return sts
        yg(env, "reconcile")
        time.sleep(0.5)
    raise AssertionError("pool did not drain")


def test_one_station_takes_card_after_card(tmp_path, env):
    pool_cfg(env, tmp_path)
    tids = ["t-p1", "t-p2", "t-p3"]
    for t in tids:
        pool_task(tmp_path, env, t)
    outs = [json.loads(yg(env, "dispatch", t, "--as", "A")[1]) for t in tids]
    assert all(o["result"] == "queued" for o in outs)
    assert outs[0]["stations_started"] == ["p-st-1"] and outs[1]["stations_started"] == []
    sts = wait_all(env, tids)
    assert [s["runs"][-1]["receipt"]["worker"] for s in sts] == ["p-st-1"] * 3
    assert len(stations(env)) == 1 and len(stations(env)[0]["tasks"]) == 3
    for t in tids:
        assert yg(env, "close", t, "--as", "A")[0] == 0


def test_station_card_limit_hands_over_to_a_fresh_station(tmp_path, env):
    pool_cfg(env, tmp_path, max_tasks_per_station=2)
    tids = ["t-q1", "t-q2", "t-q3"]
    for t in tids:
        pool_task(tmp_path, env, t)
        yg(env, "dispatch", t, "--as", "A")
    sts = wait_all(env, tids)
    workers = [s["runs"][-1]["receipt"]["worker"] for s in sts]
    assert workers.count("p-st-1") == 2 and workers.count("p-st-2") == 1


def test_workdir_outside_the_pool_root_is_refused(tmp_path, env):
    pool_cfg(env, tmp_path)
    spec = make_task(tmp_path, task_id="t-out")
    s = json.loads(spec.read_text()); s["adapter"] = {"kind": "pool", "pool": "p"}; spec.write_text(json.dumps(s))
    yg(env, "submit", str(spec), "--as", "A")
    rc, _, err = yg(env, "dispatch", "t-out", "--as", "A")
    assert rc == 3 and "under" in err


def test_card_of_a_dead_station_is_marked_lost(tmp_path, env):
    pool_cfg(env, tmp_path, command="st=$0; python3 -m yardgoat.cli claim --pool p --station $st >/dev/null; exit 0")
    pool_task(tmp_path, env, "t-dead")
    yg(env, "dispatch", "t-dead", "--as", "A")
    sts = wait_all(env, ["t-dead"])
    assert sts[0]["runs"][-1]["state"] == "lost"


def test_tampering_with_frozen_hashes_in_task_json_is_caught(tmp_path, env):
    spec = make_task(tmp_path)
    yg(env, "submit", str(spec), "--as", "A")
    (tmp_path / "verify.sh").write_text("exit 0\n")
    tj = Path(env["YARDGOAT_HOME"]) / "tasks/t-demo/task.json"
    st = json.loads(tj.read_text())
    import hashlib
    st["frozen"][str(tmp_path / "verify.sh")] = hashlib.sha256(b"exit 0\n").hexdigest()
    tj.write_text(json.dumps(st))
    rc, _, err = yg(env, "dispatch", "t-demo", "--as", "A")
    assert rc == 3 and "frozen" in err


def test_auto_close_lets_reconcile_verify_on_the_owners_behalf(tmp_path, env):
    pool_cfg(env, tmp_path)
    pool_task(tmp_path, env, "t-auto", auto_close=True)
    yg(env, "dispatch", "t-auto", "--as", "A")
    wait_all(env, ["t-auto"])
    yg(env, "reconcile")
    st = json.loads(yg(env, "status", "t-auto", "--json")[1])
    assert st["status"] == "verified" and st["verdict"]["by"] == "A"
