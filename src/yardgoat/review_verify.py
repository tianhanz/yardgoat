"""Standalone frozen review verifier; copied into each review's owner-side assets."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

SEVERITIES = ("blocker", "major", "minor", "info")


def manifest(root: Path) -> dict:
    """Hash every entry, including empty directories, modes and symlink destinations.

    Do not traverse symlinks while hashing. Findings must refer to ordinary files
    within the reviewed tree.
    """
    result = {}
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        key = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            value = {"kind": "link", "mode": mode, "target": os.readlink(path)}
        elif stat.S_ISDIR(info.st_mode):
            value = {"kind": "dir", "mode": mode}
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode):
            h = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            value = {"kind": "file", "mode": mode, "sha256": h.hexdigest()}
        else:
            raise ValueError(f"unsupported entry in review target: {path}")
        result[key] = value
    return result


def verify(settings: dict) -> list[dict]:
    source, target = Path(settings["source"]), Path(settings["target"])
    expected = settings["manifest"]
    if manifest(source) != expected or manifest(target) != expected:
        raise ValueError("review target was modified (original or snapshot)")
    workdir = target.parent
    findings_path = workdir / "out/findings.json"
    report_path = workdir / "out/REVIEW.md"
    for path in (findings_path, report_path):
        if path.is_symlink() or not path.resolve().is_relative_to(workdir.resolve()):
            raise ValueError("review outputs must be ordinary files inside the review workdir")
    if not report_path.read_text(encoding="utf-8").strip():
        raise ValueError("out/REVIEW.md must be nonempty")
    findings = json.loads(findings_path.read_text(encoding="utf-8"))
    if not isinstance(findings, list):
        raise ValueError("out/findings.json must be an array")
    ids = set()
    for item in findings:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip()
                for k in ("id", "severity", "file", "title", "scenario", "fix")):
            raise ValueError("finding requires id/severity/file/line/title/scenario/fix")
        if item["id"] in ids or item["severity"] not in SEVERITIES:
            raise ValueError("finding ids must be unique and severity must be recognized")
        ids.add(item["id"])
        path = Path(item["file"])
        if path.is_absolute() or ".." in path.parts or expected.get(path.as_posix(), {}).get("kind") != "file":
            raise ValueError(f"finding file is not an ordinary reviewed file: {path}")
        actual = target / path
        if not actual.resolve().is_relative_to(target.resolve()):
            raise ValueError("finding file escapes review target")
        line = item.get("line")
        if type(line) is not int or not 1 <= line <= len(actual.read_bytes().splitlines()):
            raise ValueError(f"finding line does not exist: {path}:{line}")
    return findings


if __name__ == "__main__":
    try:
        settings = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        print(json.dumps({"findings": verify(settings)}, ensure_ascii=False))
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
