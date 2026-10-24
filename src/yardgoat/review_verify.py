"""Standalone frozen review verifier; copied into each review's owner-side assets."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

SEVERITIES = ("blocker", "major", "minor", "info")
CACHE_DIRS = frozenset(("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                        ".hypothesis", ".benchmarks"))


def glob_match(name: str, pattern: str) -> bool:
    """Root-relative glob: * matches one component, ** matches zero or more."""
    parts, globs = Path(name).parts, Path(pattern).parts

    @lru_cache(None)
    def match(i: int, j: int) -> bool:
        if j == len(globs):
            return i == len(parts)
        if globs[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i], globs[j]) and match(i + 1, j + 1)

    return match(0, 0)


def matches_ignore(name: str, entries: dict, patterns: list[str]) -> bool:
    """Match an entry or a containing directory; never treat the root as ignored."""
    if name == ".":
        return False
    path = Path(name)
    candidates = [path, *(p for p in path.parents if p != Path("."))]
    for candidate in candidates:
        key = candidate.as_posix()
        directory = entries.get(key, {}).get("kind") == "dir"
        if candidate != path and not directory:
            continue
        if directory and candidate.name in CACHE_DIRS:
            return True
        if candidate == path and entries.get(key, {}).get("kind") == "file" and key.endswith(".pyc"):
            return True
        if any((not pattern.endswith("/") or directory) and glob_match(key, pattern)
               for pattern in patterns):
            return True
    return False


def comparison_manifest(entries: dict, settings: dict, prefix: str = "") -> dict:
    """Filter only at comparison time, using the child's frozen tracked paths."""
    if "review_ignore" not in settings:  # retain strict behavior for legacy settings
        return entries
    protected = {"."}
    for name in settings["tracked_paths"]:
        path = Path(name)
        protected.update(p.as_posix() for p in (path, *path.parents))
    relative = {name[len(prefix):]: value for name, value in entries.items() if name.startswith(prefix)}
    result = {name: value for name, value in entries.items() if not name.startswith(prefix)}
    for name, value in relative.items():
        if (name in protected or value["kind"] == "link"
                or not matches_ignore(name, relative, settings["review_ignore"])):
            result[prefix + name] = value
    return result


def unchanged(actual: dict, expected: dict, settings: dict, prefix: str = "") -> bool:
    # Cache bytes/additions/removals are harmless; an existing entry's type or
    # permissions must still not change. Symlinks are never ignored.
    if any((actual[name]["kind"], actual[name]["mode"]) != (expected[name]["kind"], expected[name]["mode"])
           for name in actual.keys() & expected.keys()):
        return False
    return comparison_manifest(actual, settings, prefix) == comparison_manifest(expected, settings, prefix)


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


def workspace_manifest(root: Path, outputs: list[str]) -> dict:
    entries = manifest(root)
    for name in outputs:
        if name in entries and entries[name]["kind"] != "file":
            raise ValueError(f"review output must be an ordinary file: {name}")
    return {name: value for name, value in entries.items() if name not in outputs}


def verify(settings: dict) -> list[dict]:
    source, target = Path(settings["source"]), Path(settings["target"])
    expected = settings["manifest"]
    if not unchanged(manifest(source), expected, settings) or not unchanged(manifest(target), expected, settings):
        raise ValueError("review target was modified (original or snapshot)")
    workdir = target.parent
    if not unchanged(workspace_manifest(workdir, settings["allowed_outputs"]),
                     settings["workspace_manifest"], settings, prefix="target/"):
        raise ValueError("review workspace was modified outside the allowed outputs")
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
