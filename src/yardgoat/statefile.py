"""Small atomic documents: the state and its append-only history commit together."""
import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path

from .store import read_json


def durable_write(path: Path, value):
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def locked_json(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            initial = read_json(path)
        except FileNotFoundError:
            initial = None
        before = json.dumps(initial, sort_keys=True)
        box = {"state": initial}
        yield box
        if json.dumps(box["state"], sort_keys=True) != before:
            durable_write(path, box["state"])
