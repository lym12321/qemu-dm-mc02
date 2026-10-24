"""Legacy tools import of the canonical pinned QEMU QMP adapter."""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.machines.qemu.qmp import qmp_session_class

def QmpSession(*args, **kwargs):
    """Construct the optional installed client only when a tool needs QMP."""
    return qmp_session_class()(*args, **kwargs)
