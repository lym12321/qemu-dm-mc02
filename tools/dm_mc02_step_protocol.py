"""Compatibility reexports for the canonical co-sim v2 wire codec."""

import sys
from pathlib import Path

# Direct tools execution and importlib consumers resolve the same package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.transports import step_codec as _step_codec

# Preserve the historical public wildcard surface and direct private imports.
# Every class and function is the canonical object, regardless of import mode.
__all__ = [name for name in vars(_step_codec) if not name.startswith("_")]
globals().update({name: value for name, value in vars(_step_codec).items()
                  if not name.startswith("__")})
