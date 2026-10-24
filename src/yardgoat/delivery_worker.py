"""Keep an identifiable group leader until the delivery shell AND its children exit."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def children_alive() -> bool:
    leader = os.getpid()
    for path in Path("/proc").glob("[0-9]*/stat"):
        if path.parent.name == str(leader):
            continue
        try:
            fields = path.read_text().rsplit(") ", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] != "Z" and fields[2] == str(leader):
            return True
    return False


def main() -> int:
    # A caught (not ignored) signal resets to default in the exec'd shell. Keeping
    # this leader through TERM lets the controller verify identity again for KILL.
    signal.signal(signal.SIGTERM, lambda *_: None)
    grant = sys.stdin.buffer.read(1)
    sys.stdin.close()
    if grant != b"1":
        return 125
    process = subprocess.Popen(["/bin/sh", "-c", sys.argv[1]], stdin=subprocess.DEVNULL)
    result = process.wait()
    while children_alive():
        time.sleep(0.01)
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    sys.exit(main())
