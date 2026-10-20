"""Legacy tools import of the canonical pinned QEMU QMP adapter."""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.machines.qemu.qmp import qmp_session_class

QmpSession = qmp_session_class(Path(__file__).resolve().parents[1])
