"""Native QEMU machine adapter, loaded without importing the optional QMP client."""

from .errors import BackendUnavailable
from .machine import QemuFactory, QemuMachine

__all__ = ["BackendUnavailable", "QemuFactory", "QemuMachine"]
