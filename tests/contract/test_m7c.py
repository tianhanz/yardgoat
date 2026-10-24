"""M7c: real pytest caches, frozen ignore policy, and unchanged integrity checks."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_contract import env  # noqa: F401
from test_m4 import rig  # noqa: F401
from test_m4_review import config_change
from yardgoat import cli
from yardgoat.store import TaskDir, home


CACHES = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
          ".hypothesis", ".benchmarks")


def track(root, *names):
    for args in (("init", "-q"), ("add", "--", *names)):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def start_review(rig):
    rig.dispatch()
    rig.execute()
    claim = rig.review(1, [], finish=False)
    return claim, Path(claim["workdir"]) / "target"


def finish_review(rig, claim, expected="PASS"):
    assert cli.finish_card(claim["task_id"], claim["run_id"], "audit-st-1")[0] == 0
    rig.call("reconcile")
    child = TaskDir(claim["task_id"]).state()
    assert child["verdict"]["verdict"] == expected, child.get("verdict")
    assert (TaskDir("parent").state()["status"] == "verified") == (expected == "PASS")


def frozen_verify(claim):
    command = TaskDir(claim["task_id"]).spec()["verifier"]["command"]
    return subprocess.run(command, shell=True, cwd=claim["workdir"],
                          capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("side", ["snapshot", "original"])
def test_real_pytest_caches_pass_review(rig, side):
    source = rig.submit() / "src"
    (source / "test_example.py").write_text("def test_value():\n    assert 1 + 1 == 2\n")
    track(source, "code.py", "test_example.py")
    claim, target = start_review(rig)
    directory = target if side == "snapshot" else source
    test_env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    for name in ("PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS", "PYTHONPYCACHEPREFIX"):
        test_env.pop(name, None)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_example.py"],
                            cwd=directory, env=test_env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (directory / ".pytest_cache").is_dir()
    assert list((directory / "__pycache__").glob("*.pyc"))
    cached = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file() and ".git" not in p.parts}
    finish_review(rig, claim)
    assert all(p.read_bytes() == data for p, data in cached.items())


@pytest.mark.parametrize("cache", [*CACHES, "loose.pyc"])
@pytest.mark.parametrize("side", ["snapshot", "original"])
def test_cache_additions_rewrites_and_removals_are_ignored(rig, cache, side):
    source = rig.submit() / "src"
    (source / "nested").mkdir()
    if cache.endswith(".pyc"):
        existing = source / "nested" / cache
    else:
        (source / "nested" / cache).mkdir()
        existing = source / "nested" / cache / "state"
    existing.write_text("before\n")
    claim, target = start_review(rig)
    directory = target if side == "snapshot" else source
    existing = directory / existing.relative_to(source)
    existing.write_text("after\n")
    assert frozen_verify(claim).returncode == 0
    existing.unlink()
    assert frozen_verify(claim).returncode == 0
    if cache.endswith(".pyc"):
        added = directory / "new.pyc"
    else:
        (directory / cache).mkdir()
        added = directory / cache / "new-state"
    added.write_text("new\n")
    finish_review(rig, claim)
    assert added.read_text() == "new\n"


def test_review_ignore_is_frozen_at_child_creation(rig, env):
    source = rig.submit() / "src"
    config_change(env, lambda c: c.update(review_ignore=[".host-state/", "logs/**/*.tmp"]))
    (source / "logs/nested").mkdir(parents=True)
    claim, target = start_review(rig)
    settings_path = home() / "reviews/parent/1/target.json"
    frozen_bytes = settings_path.read_bytes()
    # A later edit neither removes old ignores nor adds new ones to this child.
    config_change(env, lambda c: c.update(review_ignore=["unexpected.txt"]))
    for directory in (source, target):
        (directory / ".host-state").mkdir()
        (directory / ".host-state/session").write_text("state\n")
        (directory / "logs/root.tmp").write_text("state\n")
        (directory / "logs/nested/current.tmp").write_text("state\n")
    assert frozen_verify(claim).returncode == 0
    (target / "unexpected.txt").write_text("must be detected\n")
    assert frozen_verify(claim).returncode == 1
    (target / "unexpected.txt").unlink()
    finish_review(rig, claim)
    assert settings_path.read_bytes() == frozen_bytes


@pytest.mark.parametrize("name", ["code.py", ".pytest_cache/tracked.py", "tracked.pyc", ".host-state/tracked.py"])
@pytest.mark.parametrize("git_parent", [False, True])
def test_tracked_paths_override_ignores_and_warn(rig, env, caplog, name, git_parent):
    source = rig.submit() / "src"
    tracked = source / name
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("value = 1\n")
    git_root = source.parent if git_parent else source
    track(git_root, str(tracked.relative_to(git_root)))
    config_change(env, lambda c: c.update(review_ignore=[".host-state/**", "code.py"]))
    claim, target = start_review(rig)
    assert any("review_ignore" in r.message and name in r.message for r in caplog.records)
    assert frozen_verify(claim).returncode == 0
    # Even changing the source index later cannot weaken the frozen tracked set.
    if git_parent:
        subprocess.run(["git", "-C", str(git_root), "rm", "--cached", "--",
                        str(tracked.relative_to(git_root))], check=True, capture_output=True)
    (target / name).write_text("value = 2\n")
    finish_review(rig, claim, "FAIL")


@pytest.mark.parametrize("mutation", ["tracked", "new_file", "delete", "mode", "symlink",
                                     "cache_symlink", "cache_mode", "cache_named_file"])
@pytest.mark.parametrize("side", ["snapshot", "original"])
def test_review_still_rejects_noncache_changes(rig, mutation, side):
    source = rig.submit() / "src"
    (source / "note.txt").write_text("keep\n")
    (source / "link").symlink_to("note.txt")
    (source / ".pytest_cache").mkdir()
    (source / ".pytest_cache/state").write_text("cache\n")
    track(source, "code.py")
    claim, target = start_review(rig)
    directory = target if side == "snapshot" else source
    if mutation == "tracked":
        (directory / "code.py").write_text("value = 2\n")
    elif mutation == "new_file":
        (directory / "unexpected.txt").write_text("new\n")
    elif mutation == "delete":
        (directory / "note.txt").unlink()
    elif mutation == "mode":
        (directory / "code.py").chmod(0o700)
    elif mutation == "symlink":
        (directory / "link").unlink()
        (directory / "link").symlink_to("code.py")
    elif mutation == "cache_symlink":
        (directory / "__pycache__").symlink_to(".pytest_cache", target_is_directory=True)
    elif mutation == "cache_mode":
        (directory / ".pytest_cache/state").chmod(0o700)
    else:
        (directory / "__pycache__").write_text("ordinary file, not a cache directory\n")
    finish_review(rig, claim, "FAIL")


@pytest.mark.parametrize("name", [".pytest_cache/state", "out/extra.pyc", ".host-state/session"])
def test_ignores_do_not_allow_writes_outside_snapshot(rig, env, name):
    rig.submit()
    config_change(env, lambda c: c.update(review_ignore=["**"]))
    claim, _ = start_review(rig)
    added = Path(claim["workdir"]) / name
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("unauthorized\n")
    finish_review(rig, claim, "FAIL")


def test_review_template_explains_running_tests(rig):
    rig.submit()
    claim, _ = start_review(rig)
    card = Path(claim["prompt"]).read_text()
    assert "temporary directory" in card.lower()
    assert "PYTHONDONTWRITEBYTECODE=1" in card
    assert "-p no:cacheprovider" in card
