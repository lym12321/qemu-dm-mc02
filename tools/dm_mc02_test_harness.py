#!/usr/bin/env python3
"""Standalone entry for the shared native QEMU lifecycle owner."""
from pathlib import Path
import signal
import sys

# Direct script execution must locate the installed/project sim package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.machines.qemu import process as _implementation
from sim.machines.qemu.process import *  # noqa: F401,F403

if __name__ == "__main__":
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(_implementation.main())
    except KeyboardInterrupt:
        sys.exit(130)
else:
    # Preserve legacy monkeypatch/module identity while keeping one owner.
    sys.modules[__name__] = _implementation
