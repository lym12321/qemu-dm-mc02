"""Contract tests for external clocks and QEMU FDCAN timestamps.

These tests intentionally keep the worker import local to this test module.
The FDCAN test describes the boundary between the QEMU clock epoch and the
worker virtual-time epoch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


WORKER = Path(__file__).resolve().parents[1] / "tools" / \
    "dm_mc02_sim_worker.py"
SPEC = importlib.util.spec_from_file_location(
    "dm_mc02_sim_worker_external_timestamp_test", WORKER)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def test_reset_reanchors_a_new_external_clock_epoch():
    mapper = worker.ExternalTimestampMapper(1_000)

    assert mapper.map(50_000, 1_100) == 1_100
    assert mapper.map(51_000, 1_200) == 2_000

    # A simulator reset can move its clock backwards.  The worker must keep
    # advancing until a forward sample from the new epoch is available.
    assert mapper.map(10, 1_300) == 2_001
    assert mapper.map(20, 1_400) == 2_002
    assert mapper.map(120, 1_500) == 2_102
    assert mapper.map(130, 1_600) == 2_112


def test_external_timestamp_rollback_never_moves_worker_time_backwards():
    mapper = worker.ExternalTimestampMapper(0)
    values = [
        mapper.map(1_000, 100),
        mapper.map(2_000, 200),
        mapper.map(1_500, 300),
        mapper.map(1_501, 400),
        mapper.map(1_600, 500),
    ]

    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_repeated_external_timestamp_uses_fallback_progress():
    mapper = worker.ExternalTimestampMapper(0)

    assert mapper.map(123, 10) == 10
    assert mapper.map(123, 20) == 20
    assert mapper.map(123, 30) == 30
    assert mapper.map(123, 40) == 40


def test_forward_external_clock_recovers_without_old_fallback_rate():
    mapper = worker.ExternalTimestampMapper(0)

    assert mapper.map(10_000, 10_000) == 10_000
    assert mapper.map(10_000, 20_000) == 20_000
    assert mapper.map(10_000, 30_000) == 30_000
    assert mapper.map(10_100, 40_000) == 40_000
    assert mapper.map(10_200, 50_000) == 40_100
    assert mapper.map(10_300, 60_000) == 40_200


class _Backend:
    def __init__(self):
        self.events = []

    def set_motor(self, index, value):
        self.events.append(("set", index, value))

    def reset_motor(self, index):
        self.events.append(("reset", index))

    def set_motor_enabled(self, index, enabled):
        self.events.append(("enabled", index, enabled))

    def set_motor_dm(self, index, command):
        self.events.append(("dm", index, command))

    def motor_feedback(self, index):
        return (0.0, 0.0, 0.0)

    def step(self, dt):
        self.events.append(("step", dt))
        return (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)


def test_qemu_fdcan_timestamp_is_submitted_in_worker_virtual_epoch():
    """A QEMU timestamp must be translated before coordinator submission.

    QEMU's timestamp is in its virtual-clock epoch.  After RESET, the worker
    owns a separate virtual-time epoch, so only the delta from the QEMU reset
    timestamp is meaningful to the worker.  Feeding the raw timestamp into
    ``StepCoordinator`` leaves a command scheduled far in the future.
    """
    adapter = worker.DmMotorBusAdapter(1, motor_protocol="float")
    backend = _Backend()
    coordinator = worker.StepCoordinator(backend, adapter)

    qemu_epoch_ns = 10_000_000_000
    worker_epoch_ns = 0
    qemu_frame_timestamp_ns = qemu_epoch_ns + 25
    expected_worker_timestamp_ns = (
        worker_epoch_ns + qemu_frame_timestamp_ns - qemu_epoch_ns)

    mapper = worker.QemuTimestampMapper(qemu_epoch_ns, worker_epoch_ns)
    mapped_timestamp_ns = mapper.map(qemu_frame_timestamp_ns,
                                     worker_epoch_ns)
    assert mapped_timestamp_ns == expected_worker_timestamp_ns

    # Simulate the worker's FDCAN receive path. The coordinator receives the
    # mapped worker timestamp, never QEMU's raw timestamp.
    coordinator.submit_can(
        0x200, 0, b"\x00\x00\xc0?", mapped_timestamp_ns,
        worker_epoch_ns)
    coordinator.advance(expected_worker_timestamp_ns, 0.001)

    assert backend.events[0] == ("set", 0, 1.5)
