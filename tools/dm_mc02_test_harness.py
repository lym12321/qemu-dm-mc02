#!/usr/bin/env python3
"""Standalone entry for the test-only native QEMU lifecycle owner."""
from pathlib import Path
import signal
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smoke.support import process as _implementation
from smoke.support.process import *  # noqa: F401,F403

if __name__ == "__main__":
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(_implementation.main())
    except KeyboardInterrupt:
        sys.exit(130)
else:
    sys.modules[__name__] = _implementation
