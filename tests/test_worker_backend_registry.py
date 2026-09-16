"""Worker integration tests for protocol-neutral backend loading."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


WORKER = Path(__file__).resolve().parents[1] / "tools" / \
    "dm_mc02_sim_worker.py"
SPEC = importlib.util.spec_from_file_location(
    "dm_mc02_sim_worker_backend_registry_test", WORKER)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def direct_factory(config):
    return ("direct", config)


def register_custom(registry):
    registry.register("custom", direct_factory)


def config(**overrides):
    values = {
        "backend": None,
        "backend_registry": None,
        "engine": "null",
        "motor_count": 1,
        "dm_t_max": 10.0,
        "rate": 1000.0,
        "mujoco_model": None,
        "ros_motor_topic": "/motor",
        "ros_imu_topic": "/imu",
        "ros_joint_state_topic": "/joint_states",
        "ros_node_name": "test_worker",
        "ros_wait_imu": False,
        "ros_reset_service": "",
        "ros_reset_timeout": 2.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_builtin_registry_is_isolated_and_has_stable_names():
    first = worker.create_backend_registry()
    second = worker.create_backend_registry()

    assert first.names() == ("null", "mujoco", "ros2")
    first.register("custom", direct_factory)
    assert second.names() == ("null", "mujoco", "ros2")


def test_direct_factory_receives_original_namespace():
    received = config(backend=f"{__name__}:direct_factory")

    result = worker.create_engine(received)

    assert result == ("direct", received)


def test_registry_initializer_selects_custom_engine():
    received = config(backend_registry=f"{__name__}:register_custom",
                      engine="custom")

    result = worker.create_engine(received)

    assert result == ("direct", received)


def test_custom_backend_can_opt_into_external_timestamp_mapping():
    timed = argparse.Namespace(imu_timestamp_ns=None)
    plain = argparse.Namespace()

    assert worker.engine_uses_external_time(timed)
    assert not worker.engine_uses_external_time(plain)
