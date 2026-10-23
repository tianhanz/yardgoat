"""Example verifier: exit 0 = PASS, 1 = FAIL. Frozen by hash when the task is submitted."""
import csv, json, pathlib, sys
rec = pathlib.Path(__file__).parent / "records"
out = pathlib.Path("out/table.csv")
if not out.is_file() or not pathlib.Path("out/NOTES.md").is_file():
    sys.exit(1)
rows = {r["run"]: r for r in csv.DictReader(out.open())}
for f in sorted(rec.glob("*.json")):
    d, r = json.loads(f.read_text()), rows.get(f.stem)
    if r is None or r["source"] != f.name:
        sys.exit(1)
    for k in ("wall_hours", "online_hours"):
        if (str(d[k]) if k in d else "") != r[k]:
            sys.exit(1)
sys.exit(0)
