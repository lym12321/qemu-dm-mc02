"""Worker integration tests for protocol-neutral backend loading."""

from __future__ import annotations

import argparse
import importlib.util
import io
from pathlib import Path
import sys
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

import pytest


WORKER = Path(__file__).resolve().parents[1] / "tools" / \
    "dm_mc02_sim_worker.py"
SPEC = importlib.util.spec_from_file_location(
    "dm_mc02_sim_worker_backend_registry_test", WORKER)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


class CompleteBackend:
    def __init__(self, config):
        self.config = config
        self.close_calls = 0

    def step(self, dt):
        return ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])

    def set_motor(self, index, value):
        pass

    def reset_motor(self, index):
        pass

    def set_motor_enabled(self, index, enabled):
        pass

    def set_motor_dm(self, index, command):
        pass

    def motor_feedback(self, index):
        return (0.0, 0.0, 0.0)

    def close(self):
        self.close_calls += 1


class IncompleteBackend:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def direct_factory(config):
    return CompleteBackend(config)


def incomplete_factory(config):
    return IncompleteBackend()


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
        "cosim": "/tmp/cosim.sock",
        "fdcan": None,
        "socketcan": None,
        "protocol": "v1",
        "wait_step_done": False,
        "connect_timeout": 10.0,
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

    assert isinstance(result, CompleteBackend)
    assert result.config is received
    assert callable(result.step)


def test_registry_initializer_selects_custom_engine():
    received = config(backend_registry=f"{__name__}:register_custom",
                      engine="custom")

    result = worker.create_engine(received)

    assert isinstance(result, CompleteBackend)
    assert result.config is received


def test_custom_backend_can_opt_into_external_timestamp_mapping():
    timed = argparse.Namespace(imu_timestamp_ns=None)
    plain = argparse.Namespace()

    assert worker.engine_uses_external_time(timed)
    assert not worker.engine_uses_external_time(plain)


@pytest.mark.parametrize("rate", ["nan", "inf", "-inf", "0", "-1"])
def test_argument_parser_rejects_invalid_rate(rate):
    argv = ["dm_mc02_sim_worker.py", "--cosim", "/tmp/cosim.sock",
            f"--rate={rate}"]
    stderr = io.StringIO()
    with patch.object(sys, "argv", argv), redirect_stderr(stderr):
        with pytest.raises(SystemExit):
            worker.parse_args()
    assert "rate must be finite and positive" in stderr.getvalue()


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf", "0", "-1"])
def test_argument_parser_rejects_invalid_connect_timeout(timeout):
    argv = ["dm_mc02_sim_worker.py", "--cosim", "/tmp/cosim.sock",
            f"--connect-timeout={timeout}"]
    stderr = io.StringIO()
    with patch.object(sys, "argv", argv), redirect_stderr(stderr):
        with pytest.raises(SystemExit):
            worker.parse_args()
    assert "connect-timeout must be finite and positive" in stderr.getvalue()


def test_runtime_timing_validation_precedes_socket_connection(monkeypatch):
    monkeypatch.setattr(
        worker, "connect_unix",
        lambda *args: pytest.fail("invalid timing must fail before connect"))

    with pytest.raises(ValueError, match="rate must be finite and positive"):
        worker.run(config(rate=float("nan")))


def test_direct_factory_result_is_validated_and_closed(monkeypatch):
    backend = IncompleteBackend()
    monkeypatch.setattr(worker, "load_factory", lambda spec: lambda args: backend)

    with pytest.raises(TypeError, match="missing callable methods: step"):
        worker.create_engine(config(backend="tests:factory"))

    assert backend.close_calls == 1


def test_registry_factory_result_is_validated_and_closed(monkeypatch):
    backend = IncompleteBackend()
    registry = worker.BackendRegistry({
        "incomplete": lambda args: backend,
    })
    monkeypatch.setattr(worker, "create_backend_registry", lambda: registry)

    with pytest.raises(TypeError, match="missing callable methods: step"):
        worker.create_engine(config(engine="incomplete"))

    assert backend.close_calls == 1


def test_present_non_callable_optional_method_is_rejected_and_closed(monkeypatch):
    class InvalidOptionalBackend(CompleteBackend):
        reset = None

    backend = InvalidOptionalBackend(config())
    monkeypatch.setattr(worker, "load_factory",
                        lambda spec: lambda args: backend)

    with pytest.raises(TypeError, match="optional methods must be callable.*reset"):
        worker.create_engine(config(backend="tests:factory"))

    assert backend.close_calls == 1


def test_present_non_callable_close_is_rejected(monkeypatch):
    class InvalidCloseBackend(CompleteBackend):
        close = 1

    backend = InvalidCloseBackend(config())
    monkeypatch.setattr(worker, "load_factory",
                        lambda spec: lambda args: backend)

    with pytest.raises(TypeError,
                       match="optional methods must be callable.*close"):
        worker.create_engine(config(backend="tests:factory"))


def test_worker_closes_connected_socket_before_any_reset_on_invalid_backend(
        monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    backend = IncompleteBackend()
    sock = FakeSocket()
    sent_frames = []
    monkeypatch.setattr(worker, "connect_unix", lambda *args: sock)
    monkeypatch.setattr(
        worker, "FramedSocket",
        lambda unused_sock: SimpleNamespace(send=sent_frames.append))
    monkeypatch.setattr(worker, "load_factory",
                        lambda spec: lambda args: backend)

    with pytest.raises(TypeError, match="invalid backend instance"):
        worker.run(config(backend="tests:factory"))

    assert backend.close_calls == 1
    assert sock.closed
    assert sent_frames == []
