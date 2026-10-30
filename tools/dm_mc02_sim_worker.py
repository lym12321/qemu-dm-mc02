#!/usr/bin/env python3
"""Small DM-MC02 external simulation worker.

The worker deliberately keeps the QEMU-facing hot path binary and fixed
width.  It connects to the QEMU co-sim chardev for IMU/telemetry and, when
requested, to one FDCAN chardev for timestamped 84-byte CAN frames.  The
default NullEngine is useful for wiring tests; MuJoCo is an optional import.
"""

from __future__ import annotations

import argparse
from collections import deque
import importlib.util
import math
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# Direct tools execution and importlib consumers resolve the same package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.transports import (
    DatagramTransport, FixedRecordStream, LengthPrefixedStream,
    connect_unix as transport_connect_unix, send_datagram,
)
from sim.transports import step_codec as step_protocol
from sim.transports.can_stream import CanStream
from sim.transports.cosim import (
    MAGIC, VERSION, HEADER, MAX_PAYLOAD, RESET, IMU_SAMPLE, TELEMETRY,
    ADC_INPUT, ADC_VOLTAGE, ADC_VOLTAGE_MAX_UV, ADC_VOLTAGE_FLAGS_MASK,
    MAX_PENDING_FRAMES, V2_BODY_FIXED_SIZE, V2_STEP_RESPONSE_TIMEOUT_SEC,
    V2_STEP_ACK_TIMEOUT_SEC, V2_STEP_MAX_RETRIES, V2_STEP_DONE_MAX_RETRIES,
    V2_QUEUE_FULL_RETRY_SEC,
    QemuTimestampMapper, FramedSocket, StepFramedSocket, V2SessionReset,
    wait_v2_response, send_v2_step, wait_v2_reset_ack,
    protocol_frame, adc_input_frame, adc_voltage_frame,
)
from sim.api import Session, StepResult
from sim.runtime import Simulation, SimulationState
from sim.physics import MujocoPhysics, integrate_rotor
from sim.devices.bmi088 import imu_to_bmi088_units

try:
    from dm_mc02_socketcan import (CanFrame, SocketCANError, open_socketcan,
                                   pack_can_frame, pack_wire_frame,
                                   unpack_can_frame, unpack_wire_frame)
except ModuleNotFoundError as exc:
    # Some smoke tests load this file by path with importlib, so its directory
    # is not necessarily present on sys.path. Keep the worker usable in that
    # mode without requiring tools/ to become a Python package.
    if exc.name != "dm_mc02_socketcan":
        raise
    socketcan_path = Path(__file__).with_name("dm_mc02_socketcan.py")
    socketcan_spec = importlib.util.spec_from_file_location(
        "dm_mc02_socketcan", socketcan_path)
    if socketcan_spec is None or socketcan_spec.loader is None:
        raise ImportError(f"cannot load {socketcan_path}") from exc
    socketcan_module = importlib.util.module_from_spec(socketcan_spec)
    sys.modules[socketcan_spec.name] = socketcan_module
    socketcan_spec.loader.exec_module(socketcan_module)
    from dm_mc02_socketcan import (CanFrame, SocketCANError, open_socketcan,
                                   pack_can_frame, pack_wire_frame,
                                   unpack_can_frame, unpack_wire_frame)

try:
    from dm_mc02_motor_adapter import (
        DM_DEFAULT_FEEDBACK_ID, DM_DEFAULT_P_MAX, DM_DEFAULT_SLAVE_ID,
        DM_DEFAULT_T_MAX, DM_DEFAULT_V_MAX, DmMotorBusAdapter,
    )
except ModuleNotFoundError as exc:
    if exc.name != "dm_mc02_motor_adapter":
        raise
    motor_adapter_path = Path(__file__).with_name("dm_mc02_motor_adapter.py")
    motor_adapter_spec = importlib.util.spec_from_file_location(
        "dm_mc02_motor_adapter", motor_adapter_path)
    if motor_adapter_spec is None or motor_adapter_spec.loader is None:
        raise ImportError(f"cannot load {motor_adapter_path}") from exc
    motor_adapter_module = importlib.util.module_from_spec(motor_adapter_spec)
    sys.modules[motor_adapter_spec.name] = motor_adapter_module
    motor_adapter_spec.loader.exec_module(motor_adapter_module)
    from dm_mc02_motor_adapter import (
        DM_DEFAULT_FEEDBACK_ID, DM_DEFAULT_P_MAX, DM_DEFAULT_SLAVE_ID,
        DM_DEFAULT_T_MAX, DM_DEFAULT_V_MAX, DmMotorBusAdapter,
    )

try:
    from dm_mc02_step_coordinator import StepCoordinator
except ModuleNotFoundError as exc:
    if exc.name != "dm_mc02_step_coordinator":
        raise
    coordinator_path = Path(__file__).with_name("dm_mc02_step_coordinator.py")
    coordinator_spec = importlib.util.spec_from_file_location(
        "dm_mc02_step_coordinator", coordinator_path)
    if coordinator_spec is None or coordinator_spec.loader is None:
        raise ImportError(f"cannot load {coordinator_path}") from exc
    coordinator_module = importlib.util.module_from_spec(coordinator_spec)
    sys.modules[coordinator_spec.name] = coordinator_module
    coordinator_spec.loader.exec_module(coordinator_module)
    from dm_mc02_step_coordinator import StepCoordinator

try:
    from dm_mc02_backend_registry import (BackendRegistry, load_factory,
                                          load_registry)
except ModuleNotFoundError as exc:
    if exc.name != "dm_mc02_backend_registry":
        raise
    backend_registry_path = Path(__file__).with_name(
        "dm_mc02_backend_registry.py")
    backend_registry_spec = importlib.util.spec_from_file_location(
        "dm_mc02_backend_registry", backend_registry_path)
    if (backend_registry_spec is None or
            backend_registry_spec.loader is None):
        raise ImportError(f"cannot load {backend_registry_path}") from exc
    backend_registry_module = importlib.util.module_from_spec(
        backend_registry_spec)
    sys.modules[backend_registry_spec.name] = backend_registry_module
    backend_registry_spec.loader.exec_module(backend_registry_module)
    from dm_mc02_backend_registry import (BackendRegistry, load_factory,
                                          load_registry)

CAN_WIRE_SIZE = 84
ROS_RESET_TIMEOUT_SEC = 2.0
BACKEND_REQUIRED_METHODS = (
    "step", "set_motor", "reset_motor", "set_motor_enabled",
    "set_motor_dm", "motor_feedback",
)
BACKEND_OPTIONAL_METHODS = ("reset", "close")
_MISSING_BACKEND_MEMBER = object()

def realtime_target_ns(wall_origin_ns: int, virtual_time_ns: int,
                       clock_base_ns: int) -> int:
    """Map a virtual timestamp onto the wall-clock origin of this worker."""
    return wall_origin_ns + virtual_time_ns - clock_base_ns


class ExternalTimestampMapper:
    """Map an external sensor clock into the worker's monotonic clock.

    External engines may repeat a message while the worker advances, or may
    publish a timestamp that moves backwards after a simulator reset.  Keep
    the useful part of a valid forward-moving clock without allowing the
    protocol timestamp to move backwards.
    """

    def __init__(self, clock_base_ns: int):
        self.clock_base_ns = clock_base_ns
        self.origin_ns: int | None = None
        self.mapped_origin_ns: int | None = None
        self.last_external_ns: int | None = None
        self.last_mapped_ns = clock_base_ns
        self.external_stalled = False
        self.external_recovered = False

    def map(self, external_ns: int | None, fallback_ns: int) -> int:
        fallback = max(fallback_ns, self.last_mapped_ns + 1)
        candidate = fallback
        if external_ns is not None and external_ns >= 0:
            if self.origin_ns is None:
                self.origin_ns = external_ns
                self.mapped_origin_ns = self.clock_base_ns
                self.last_external_ns = external_ns
            elif external_ns >= self.last_external_ns:
                if external_ns == self.last_external_ns:
                    self.external_stalled = True
                else:
                    assert self.mapped_origin_ns is not None
                    if self.external_stalled:
                        # The fallback clock may have advanced far beyond the
                        # old mapping while ROS/Gazebo was paused or reset.
                        # Anchor the first forward sample at the current
                        # monotonic floor, then let subsequent external
                        # deltas control the output again.
                        self.origin_ns = external_ns
                        self.mapped_origin_ns = fallback
                        self.external_stalled = False
                        self.external_recovered = True
                    candidate = self.mapped_origin_ns + (
                        external_ns - self.origin_ns)
                    self.last_external_ns = external_ns
            else:
                # ROS/Gazebo reset starts a new external clock epoch. Keep the
                # fallback moving until the new epoch produces a forward
                # sample; that sample is re-anchored in the branch above.
                self.origin_ns = external_ns
                self.last_external_ns = external_ns
                self.external_stalled = True
                self.external_recovered = False
        elif self.origin_ns is not None:
            self.external_stalled = True
            self.external_recovered = False

        if (external_ns is not None and external_ns >= 0 and
                not self.external_stalled and not self.external_recovered):
            # During a healthy external session the fallback is still a floor
            # for the first samples, but it must not permanently mask a
            # recovered external clock.
            candidate = max(candidate, fallback)
        candidate = max(candidate, self.last_mapped_ns + 1)
        self.last_mapped_ns = candidate
        return candidate


def imu_frame(sequence: int, virtual_time_ns: int,
              gyro: Iterable[float], accel: Iterable[float]) -> bytes:
    return protocol_frame(IMU_SAMPLE, sequence, virtual_time_ns,
                          struct.pack("<6f", *gyro, *accel))


def send_socketcan(sock: socket.socket, frame: CanFrame) -> None:
    """Send one native CAN/CAN-FD datagram without waiting.

    The worker uses :class:`DatagramQueue` for normal operation.  This small
    helper remains useful to callers that want one-shot forwarding and makes
    backpressure explicit instead of sleeping inside the simulation loop.
    """
    send_datagram(sock, pack_can_frame(frame), name="SocketCAN")


class DatagramQueue(DatagramTransport):
    """SocketCAN codec over the bounded opaque datagram transport."""

    def __init__(self, sock: socket.socket):
        super().__init__(sock, max_pending=MAX_PENDING_FRAMES,
                         name="SocketCAN", writable_name="SocketCAN interface",
                         clock=time, poller=select)

    def send(self, frame: CanFrame) -> None:
        self._check_tx_capacity()
        self._enqueue(pack_can_frame(frame))


def _report_cleanup_error(primary: BaseException, name: str,
                          error: BaseException) -> None:
    diagnostic = (f"cleanup {name}.close() failed: "
                  f"{type(error).__name__}: {error}")
    primary.add_note(diagnostic)
    try:
        print(f"dm_mc02_sim_worker: {diagnostic}", file=sys.stderr)
    except BaseException as exc:
        primary.add_note(f"cleanup diagnostic output failed: "
                         f"{type(exc).__name__}: {exc}")


def connect_unix(path: str, timeout: float) -> socket.socket:
    return transport_connect_unix(
        path, timeout, socket_module=socket, clock=time,
        report_cleanup_error=_report_cleanup_error)


@dataclass
class MotorState:
    values: list[float]
    position: list[float] = field(init=False)
    velocity: list[float] = field(init=False)
    torque: list[float] = field(init=False)
    enabled: list[bool] = field(init=False)
    dm_command: list[dict | None] = field(init=False)

    def __post_init__(self) -> None:
        count = len(self.values)
        self.position = [0.0] * count
        self.velocity = [0.0] * count
        self.torque = [0.0] * count
        self.enabled = [False] * count
        self.dm_command = [None] * count


def dm_mit_effort(command: dict, position: float, velocity: float,
                  torque_limit: float) -> float:
    """Evaluate the common MIT command law and apply the plant torque limit."""
    effort = (command["torque"] +
              command["kp"] * (command["position"] - position) +
              command["kd"] * (command["speed"] - velocity))
    return min(max(effort, -torque_limit), torque_limit)


def legacy_motor_effort(value: float, enabled: bool,
                        torque_limit: float) -> float:
    """Return the effective output for the legacy direct-effort command."""
    if not enabled:
        return 0.0
    return min(max(value, -torque_limit), torque_limit)


class NullEngine:
    def __init__(self, motor_count: int, torque_limit: float = DM_DEFAULT_T_MAX):
        self.motors = MotorState([0.0] * motor_count)
        # A deliberately small rigid-rotor approximation.  These defaults
        # make the DM MIT example useful without pretending to be a calibrated
        # actuator model; a real plant can be supplied by MuJoCo/Gazebo.
        self.inertia = 0.02
        self.damping = 0.08
        self.torque_limit = torque_limit

    def set_motor(self, index: int, value: float) -> None:
        if 0 <= index < len(self.motors.values):
            self.motors.values[index] = value
            self.motors.dm_command[index] = None
            self.motors.torque[index] = legacy_motor_effort(
                value, self.motors.enabled[index], self.torque_limit)

    def reset(self) -> None:
        """Reset plant state while retaining the engine configuration."""
        count = len(self.motors.values)
        self.motors.values = [0.0] * count
        self.motors.position = [0.0] * count
        self.motors.velocity = [0.0] * count
        self.motors.torque = [0.0] * count
        self.motors.enabled = [False] * count
        self.motors.dm_command = [None] * count

    def set_motor_enabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < len(self.motors.values):
            self.motors.enabled[index] = enabled
            if self.motors.dm_command[index] is None:
                self.motors.torque[index] = legacy_motor_effort(
                    self.motors.values[index], enabled, self.torque_limit)

    def set_motor_dm(self, index: int, command: dict) -> None:
        if 0 <= index < len(self.motors.values):
            self.motors.dm_command[index] = command

    def reset_motor(self, index: int) -> None:
        if 0 <= index < len(self.motors.values):
            self.motors.enabled[index] = False
            self.motors.dm_command[index] = None
            self.motors.values[index] = 0.0
            self.motors.position[index] = 0.0
            self.motors.velocity[index] = 0.0
            self.motors.torque[index] = 0.0

    def motor_feedback(self, index: int) -> tuple[float, float, float]:
        if 0 <= index < len(self.motors.values):
            return (self.motors.position[index], self.motors.velocity[index],
                    self.motors.torque[index])
        return (0.0, 0.0, 0.0)

    def step(self, dt: float) -> tuple[tuple[float, float, float],
                                         tuple[float, float, float]]:
        for index, command in enumerate(self.motors.dm_command):
            if command is None:
                applied = legacy_motor_effort(
                    self.motors.values[index], self.motors.enabled[index],
                    self.torque_limit)
            elif self.motors.enabled[index]:
                applied = dm_mit_effort(command, self.motors.position[index],
                                        self.motors.velocity[index],
                                        self.torque_limit)
            else:
                applied = 0.0
            self.motors.position[index], self.motors.velocity[index] = integrate_rotor(
                self.motors.position[index], self.motors.velocity[index], applied,
                dt, self.inertia, self.damping)
            self.motors.torque[index] = applied
        return (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)


class MujocoEngine:
    """Legacy motor policy and units over the named public physics boundary."""

    def __init__(self, model_path: str, motor_count: int, timestep: float,
                 torque_limit: float = DM_DEFAULT_T_MAX):
        try:
            import mujoco
        except ImportError as exc:  # pragma: no cover - depends on host setup
            raise RuntimeError(
                "MuJoCo engine requires `uv pip install mujoco`") from exc
        if (isinstance(timestep, bool) or not isinstance(timestep, (int, float))
                or not math.isfinite(timestep) or timestep <= 0):
            raise ValueError("MuJoCo timestep must be finite and positive")
        if type(motor_count) is not int or motor_count < 0:
            raise ValueError("MuJoCo motor_count must be a nonnegative integer")
        model = mujoco.MjModel.from_xml_path(str(Path(model_path)))
        model.opt.timestep = timestep
        self.motor_count = min(motor_count, model.nactuator)
        # Native metadata is used only for legacy configuration discovery. The
        # complete model is then transferred to the sole runtime solver owner.
        self.joint_names = tuple(mujoco.mj_id2name(
            model, mujoco.mjtObj.mjOBJ_JOINT, int(model.actuator_trnid[a, 0]))
            for a in range(self.motor_count))
        actuator_names = tuple(mujoco.mj_id2name(
            model, mujoco.mjtObj.mjOBJ_ACTUATOR, a) for a in range(self.motor_count))
        if len(set(self.joint_names)) != self.motor_count:
            raise ValueError("MuJoCo legacy motors must bind distinct named hinge joints")

        def sensor_name(names):
            return next((name for name in names if mujoco.mj_name2id(
                model, mujoco.mjtObj.mjOBJ_SENSOR, name) >= 0), None)
        gyro = sensor_name(("imu_gyro", "gyro"))
        accel = sensor_name(("imu_accel", "accelerometer", "accel"))
        if gyro is None or accel is None:
            raise ValueError("MuJoCo legacy model requires named native gyro and accelerometer")
        self.physics = MujocoPhysics(
            model, joints=dict(zip(self.joint_names, actuator_names)),
            imus={"imu": (gyro, accel)})
        self.motor_values = [0.0] * self.motor_count
        self.legacy_values = [0.0] * self.motor_count
        self.torque_limit = torque_limit
        self.enabled = [False] * self.motor_count
        self.dm_commands: list[dict | None] = [None] * self.motor_count

    @property
    def time_s(self) -> float:
        return self.physics.time_s

    def set_motor(self, index: int, value: float) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = None
            self.legacy_values[index] = value
            self.motor_values[index] = legacy_motor_effort(
                value, self.enabled[index], self.torque_limit)
            self.physics.set_joint_effort(
                self.joint_names[index], self.motor_values[index])

    def reset(self) -> None:
        """Reset MuJoCo state without reloading the model or its sensors."""
        self.physics.reset()
        self.motor_values = [0.0] * self.motor_count
        self.legacy_values = [0.0] * self.motor_count
        self.enabled = [False] * self.motor_count
        self.dm_commands = [None] * self.motor_count

    def set_motor_enabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < self.motor_count:
            self.enabled[index] = enabled
            if self.dm_commands[index] is None:
                self.motor_values[index] = legacy_motor_effort(
                    self.legacy_values[index], enabled, self.torque_limit)
                self.physics.set_joint_effort(
                    self.joint_names[index], self.motor_values[index])
            elif not enabled:
                self.motor_values[index] = 0.0
                self.physics.set_joint_effort(self.joint_names[index], 0.0)

    def set_motor_dm(self, index: int, command: dict) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = command
            if not self.enabled[index]:
                self.motor_values[index] = 0.0
                self.physics.set_joint_effort(self.joint_names[index], 0.0)

    def reset_motor(self, index: int) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = None
            self.legacy_values[index] = 0.0
            self.set_motor_enabled(index, False)

    def _joint_state(self, index: int) -> tuple[float, float]:
        if not 0 <= index < self.motor_count:
            return (0.0, 0.0)
        state = self.physics.get_joint_state(self.joint_names[index])
        return state.position_rad, state.velocity_rad_s

    def motor_feedback(self, index: int) -> tuple[float, float, float]:
        if not 0 <= index < self.motor_count:
            return (0.0, 0.0, 0.0)
        position, velocity = self._joint_state(index)
        return (position, velocity, self.motor_values[index])

    def step(self, dt: float) -> tuple[tuple[float, float, float],
                                         tuple[float, float, float]]:
        if (isinstance(dt, bool) or not isinstance(dt, (int, float))
                or not math.isfinite(dt) or dt <= 0.0):
            raise ValueError("MuJoCo step dt must be finite and positive")
        for index, command in enumerate(self.dm_commands):
            if command is None:
                applied = legacy_motor_effort(
                    self.legacy_values[index], self.enabled[index],
                    self.torque_limit)
            else:
                if not self.enabled[index]:
                    applied = 0.0
                else:
                    position, velocity = self._joint_state(index)
                    applied = dm_mit_effort(command, position, velocity,
                                            self.torque_limit)
            self.motor_values[index] = applied
            self.physics.set_joint_effort(self.joint_names[index], applied)
        self.physics.step_seconds(dt)
        return imu_to_bmi088_units(self.physics.get_imu("imu"))


class Ros2Engine:
    """ROS 2 topic bridge suitable for a Gazebo ROS sensor/controller plugin."""

    def __init__(self, motor_count: int, motor_topic: str, imu_topic: str,
                 joint_state_topic: str, node_name: str,
                 torque_limit: float = DM_DEFAULT_T_MAX,
                 wait_for_imu: bool = False, reset_service: str = "",
                 reset_timeout: float = ROS_RESET_TIMEOUT_SEC):
        try:
            import rclpy
            from rclpy.node import Node
            from sensor_msgs.msg import Imu
            from sensor_msgs.msg import JointState
            from std_msgs.msg import Float64MultiArray
        except ImportError as exc:  # pragma: no cover - depends on ROS setup
            raise RuntimeError(
                "ROS2 engine requires a sourced ROS2 environment") from exc
        self.rclpy = rclpy
        self.motor_count = motor_count
        self.motor_values = [0.0] * motor_count
        self.legacy_values = [0.0] * motor_count
        self.enabled = [False] * motor_count
        self.dm_commands: list[dict | None] = [None] * motor_count
        self.torque_limit = torque_limit
        self.position = [0.0] * motor_count
        self.velocity = [0.0] * motor_count
        self.effort = [0.0] * motor_count
        self.effort_valid = [False] * motor_count
        self.gyro = (0.0, 0.0, 0.0)
        self.accel = (0.0, 0.0, 1.0)
        self.imu_timestamp_ns: int | None = None
        self.wait_for_imu = wait_for_imu
        self.reset_timeout = reset_timeout
        self.reset_client = None
        self.reset_service_type = None
        if not rclpy.ok():
            rclpy.init(args=None)
        self.node = Node(node_name)
        self.message_type = Float64MultiArray
        self.publisher = self.node.create_publisher(Float64MultiArray,
                                                     motor_topic, 10)
        self.subscription = self.node.create_subscription(
            Imu, imu_topic, self._imu_callback, 10)
        self.joint_subscription = self.node.create_subscription(
            JointState, joint_state_topic, self._joint_state_callback, 10)
        if reset_service:
            from std_srvs.srv import Empty
            self.reset_service_type = Empty
            self.reset_client = self.node.create_client(Empty, reset_service)

    def _imu_callback(self, message) -> None:
        self.gyro = tuple(float(value) for value in (
            message.angular_velocity.x,
            message.angular_velocity.y,
            message.angular_velocity.z))
        self.accel = tuple(float(value) / 9.80665 for value in (
            message.linear_acceleration.x,
            message.linear_acceleration.y,
            message.linear_acceleration.z))
        stamp = getattr(getattr(message, "header", None), "stamp", None)
        if stamp is not None:
            seconds = int(getattr(stamp, "sec", -1))
            nanoseconds = int(getattr(stamp, "nanosec", -1))
            if seconds >= 0 and 0 <= nanoseconds < 1_000_000_000:
                self.imu_timestamp_ns = seconds * 1_000_000_000 + nanoseconds

    def _joint_state_callback(self, message) -> None:
        """Read JointState arrays by index; names are an optional plant detail."""
        # JointState permits arrays of different lengths, including omitted
        # fields. Clear values absent from this sample instead of retaining
        # feedback from an older timestamp.
        self.position = [0.0] * self.motor_count
        self.velocity = [0.0] * self.motor_count
        self.effort = [0.0] * self.motor_count
        self.effort_valid = [False] * self.motor_count
        for index in range(min(self.motor_count, len(message.position))):
            self.position[index] = float(message.position[index])
        for index in range(min(self.motor_count, len(message.velocity))):
            self.velocity[index] = float(message.velocity[index])
        for index in range(min(self.motor_count, len(message.effort))):
            self.effort[index] = float(message.effort[index])
            self.effort_valid[index] = True

    def _publish_motors(self) -> None:
        message = self.message_type()
        message.data = list(self.motor_values)
        self.publisher.publish(message)

    def set_motor(self, index: int, value: float) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = None
            self.legacy_values[index] = value
            self.motor_values[index] = legacy_motor_effort(
                value, self.enabled[index], self.torque_limit)
            self._publish_motors()

    def reset(self) -> None:
        """Reset commands and cached sensor state while keeping ROS alive."""
        self.motor_values = [0.0] * self.motor_count
        self.legacy_values = [0.0] * self.motor_count
        self.enabled = [False] * self.motor_count
        self.dm_commands = [None] * self.motor_count
        self.position = [0.0] * self.motor_count
        self.velocity = [0.0] * self.motor_count
        self.effort = [0.0] * self.motor_count
        self.effort_valid = [False] * self.motor_count
        self.gyro = (0.0, 0.0, 0.0)
        self.accel = (0.0, 0.0, 1.0)
        self.imu_timestamp_ns = None
        self._publish_motors()
        if self.reset_client is not None:
            deadline = time.monotonic() + self.reset_timeout
            future = self.reset_client.call_async(
                self.reset_service_type.Request())
            while self.rclpy.ok() and not future.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ROS2 reset service timed out")
                self.rclpy.spin_once(self.node, timeout_sec=min(0.01, remaining))
            if not future.done():
                raise RuntimeError("ROS2 reset service stopped before completion")
            try:
                future.result()
            except Exception as exc:
                raise RuntimeError("ROS2 reset service failed") from exc

    def set_motor_enabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < self.motor_count:
            self.enabled[index] = enabled
            if self.dm_commands[index] is None:
                self.motor_values[index] = legacy_motor_effort(
                    self.legacy_values[index], enabled, self.torque_limit)
                self._publish_motors()
            elif not enabled:
                # Keep the existing ROS2 DM-MIT disable behavior: disabling
                # a DM-MIT command clears the active command and publishes 0.
                self.set_motor(index, 0.0)

    def set_motor_dm(self, index: int, command: dict) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = command
            effort = (dm_mit_effort(command, self.position[index],
                                     self.velocity[index], self.torque_limit)
                      if self.enabled[index] else 0.0)
            self.motor_values[index] = effort
            self._publish_motors()

    def reset_motor(self, index: int) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = None
            self.legacy_values[index] = 0.0
            self.set_motor_enabled(index, False)

    def motor_feedback(self, index: int) -> tuple[float, float, float]:
        if not 0 <= index < self.motor_count:
            return (0.0, 0.0, 0.0)
        return (self.position[index], self.velocity[index],
                self.effort[index] if self.effort_valid[index] else
                self.motor_values[index])

    def step(self, dt: float) -> tuple[tuple[float, float, float],
                                         tuple[float, float, float]]:
        previous_timestamp = self.imu_timestamp_ns
        if self.wait_for_imu:
            deadline = time.monotonic() + max(0.001, min(dt * 2.0, 0.1))
            while self.rclpy.ok() and time.monotonic() < deadline:
                current_timestamp = self.imu_timestamp_ns
                if (current_timestamp is not None and
                        (previous_timestamp is None or
                         current_timestamp > previous_timestamp)):
                    break
                self.rclpy.spin_once(
                    self.node,
                    timeout_sec=min(0.001, max(0.0, deadline - time.monotonic())))
        else:
            self.rclpy.spin_once(self.node, timeout_sec=0.0)
        return (tuple(value * 180.0 / math.pi for value in self.gyro),
                self.accel)

    def close(self) -> None:
        self.node.destroy_node()
        if self.rclpy.ok():
            self.rclpy.shutdown()


def _validate_worker_timing(args: argparse.Namespace) -> None:
    if not math.isfinite(args.rate) or args.rate <= 0:
        raise ValueError("rate must be finite and positive")
    if not math.isfinite(args.connect_timeout) or args.connect_timeout <= 0:
        raise ValueError("connect-timeout must be finite and positive")


def parse_args(argv=None, *, configure_parser=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    if configure_parser is not None:
        configure_parser(parser)
    parser.add_argument("--cosim", required=True,
                        help="QEMU co-sim Unix socket")
    parser.add_argument("--protocol", choices=("v1", "v2"), default="v1",
                        help="co-sim control protocol (default: v1)")
    parser.add_argument("--wait-step-done", action="store_true",
                        help="in v2 mode, wait for guest SPI consumption")
    parser.add_argument("--fdcan", help="optional QEMU FDCAN Unix socket")
    parser.add_argument("--socketcan", metavar="IFACE",
                        help="optional Linux SocketCAN interface (for --fdcan)")
    parser.add_argument("--engine", default="null",
                        help="registered backend name (default: null)")
    parser.add_argument("--backend", metavar="MODULE:FACTORY",
                        help="load one backend factory and use it directly")
    parser.add_argument("--backend-registry", metavar="MODULE:INITIALIZER",
                        help="load a registry initializer before selecting --engine")
    parser.add_argument("--mujoco-model", help="MuJoCo XML/MJB model path")
    parser.add_argument("--motor-protocol", choices=("auto", "float", "dm-mit"),
                        default="auto",
                        help="CAN motor command protocol (default: auto)")
    parser.add_argument("--dm-slave-id", type=lambda value: int(value, 0),
                        default=DM_DEFAULT_SLAVE_ID)
    parser.add_argument("--dm-feedback-id", type=lambda value: int(value, 0),
                        default=DM_DEFAULT_FEEDBACK_ID)
    parser.add_argument("--dm-motor", action="append", default=[],
                        metavar="INDEX:SLAVE_ID:FEEDBACK_ID",
                        help="override DM IDs for one motor (repeatable)")
    parser.add_argument("--dm-p-max", type=float, default=DM_DEFAULT_P_MAX)
    parser.add_argument("--dm-v-max", type=float, default=DM_DEFAULT_V_MAX)
    parser.add_argument("--dm-t-max", type=float, default=DM_DEFAULT_T_MAX)
    parser.add_argument("--ros-motor-topic", default="/dm_mc02/motor_cmd")
    parser.add_argument("--ros-imu-topic", default="/dm_mc02/imu")
    parser.add_argument("--ros-joint-state-topic",
                        default="/dm_mc02/joint_states",
                        help="ROS2 sensor_msgs/JointState topic for motor state")
    parser.add_argument("--ros-node-name", default="dm_mc02_sim_worker")
    parser.add_argument("--ros-wait-imu", action="store_true",
                        help="wait for a newer ROS IMU timestamp at each step")
    parser.add_argument("--ros-reset-service", default="",
                        help="optional ROS2 Empty service used by reset")
    parser.add_argument("--ros-reset-timeout", type=float,
                        default=ROS_RESET_TIMEOUT_SEC,
                        help="ROS2 reset service timeout in seconds")
    parser.add_argument("--motor-count", type=int, default=4)
    parser.add_argument("--motor-can-base", type=lambda x: int(x, 0),
                        default=0x200)
    parser.add_argument("--rate", type=float, default=1000.0)
    parser.add_argument("--adc-input", action="append", default=[],
                        metavar="CHANNEL:RAW",
                        help="inject an ADC input on every simulation step")
    parser.add_argument("--adc-voltage", action="append", default=[],
                        metavar="CHANNEL:VOLTS",
                        help="inject an ADC analog voltage on every simulation step")
    parser.add_argument("--realtime", action="store_true",
                        help="pace virtual time against wall clock")
    parser.add_argument("--frames", type=int, default=0,
                        help="stop after this many IMU frames (0 = forever)")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        _validate_worker_timing(args)
    except ValueError as exc:
        parser.error(str(exc))
    if args.motor_count < 0:
        parser.error("motor-count must be non-negative")
    if args.socketcan and not args.fdcan:
        parser.error("--socketcan requires --fdcan")
    if args.backend and args.backend_registry:
        parser.error("--backend and --backend-registry cannot be combined")
    if not args.engine.strip():
        parser.error("engine name must be non-empty")
    if args.wait_step_done and args.protocol != "v2":
        parser.error("--wait-step-done requires --protocol v2")
    if not 0 <= args.dm_slave_id <= 0x7ff or not 0 <= args.dm_feedback_id <= 0x7ff:
        parser.error("DM CAN IDs must be standard 11-bit identifiers")
    args.dm_motor_map = {}
    for spec in args.dm_motor:
        try:
            index_text, slave_text, feedback_text = spec.split(":", 2)
            index = int(index_text, 0)
            slave_id = int(slave_text, 0)
            feedback_id = int(feedback_text, 0)
        except ValueError:
            parser.error("--dm-motor must use INDEX:SLAVE_ID:FEEDBACK_ID")
        if not 0 <= index < args.motor_count:
            parser.error("--dm-motor index must be within motor-count")
        if not 0 <= slave_id <= 0x7ff or not 0 <= feedback_id <= 0x7ff:
            parser.error("--dm-motor CAN IDs must be standard 11-bit identifiers")
        if index in args.dm_motor_map:
            parser.error("--dm-motor index must not be repeated")
        args.dm_motor_map[index] = (slave_id, feedback_id)
    # Reject collisions in the complete effective map, including contiguous
    # defaults for indexes that were not explicitly overridden. A feedback ID
    # colliding with another control ID is unsafe because feedback could be
    # interpreted as a new command by this worker.
    seen_ids = {}
    motor_adapter = DmMotorBusAdapter.from_namespace(args)
    for index in range(args.motor_count):
        slave_id, feedback_id = motor_adapter.motor_ids(index)
        for role, can_id in (("slave", slave_id), ("feedback", feedback_id)):
            if not 0 <= can_id <= 0x7ff:
                parser.error(
                    "effective DM CAN ID for motor %d %s must be "
                    "a standard 11-bit identifier" % (index, role))
            previous = seen_ids.get(can_id)
            if previous is not None:
                parser.error(
                    "effective DM CAN ID 0x%x is used by motor %d %s and "
                    "motor %d %s" % (can_id, previous[0], previous[1],
                                     index, role))
            seen_ids[can_id] = (index, role)
    if (not math.isfinite(args.dm_p_max) or not math.isfinite(args.dm_v_max) or
            not math.isfinite(args.dm_t_max) or args.dm_p_max <= 0 or
            args.dm_v_max <= 0 or args.dm_t_max <= 0):
        parser.error("DM motor limits must be finite and positive")
    if args.engine == "mujoco" and not args.mujoco_model:
        parser.error("--mujoco-model is required with --engine mujoco")
    if (not math.isfinite(args.ros_reset_timeout) or
            args.ros_reset_timeout <= 0):
        parser.error("ros-reset-timeout must be finite and positive")
    args.adc_input_values = []
    for spec in args.adc_input:
        try:
            channel_text, raw_text = spec.split(":", 1)
            channel, raw = int(channel_text, 0), int(raw_text, 0)
        except ValueError:
            parser.error("--adc-input must use CHANNEL:RAW")
        if not 0 <= channel <= 31 or not 0 <= raw <= 0xffff:
            parser.error("--adc-input channel must be 0..31 and raw 0..65535")
        args.adc_input_values.append((channel, raw))
    args.adc_voltage_values = []
    for spec in args.adc_voltage:
        try:
            channel_text, volts_text = spec.split(":", 1)
            channel = int(channel_text, 0)
            volts = float(volts_text)
        except ValueError:
            parser.error("--adc-voltage must use CHANNEL:VOLTS")
        if not 0 <= channel <= 31 or not math.isfinite(volts):
            parser.error("--adc-voltage channel must be 0..31 and volts finite")
        voltage_uv = int(round(volts * 1_000_000.0))
        if not 0 <= voltage_uv <= ADC_VOLTAGE_MAX_UV:
            parser.error("--adc-voltage must be in range 0..3.3 V")
        args.adc_voltage_values.append((channel, voltage_uv))
    return args


def send_dm_feedback(can: CanStream, engine, indices: Iterable[int],
                     timestamp_ns: int,
                     adapter: DmMotorBusAdapter,
                     timestamp_mapper: QemuTimestampMapper) -> None:
    wire_timestamp_ns = timestamp_mapper.unmap(
        timestamp_ns, timestamp_mapper.qemu_origin_ns)
    for index in indices:
        state = adapter.state(engine, index, timestamp_ns)
        feedback_id, payload = adapter.encode_feedback(state)
        can.send(feedback_id, 0, 8, wire_timestamp_ns,
                 payload)


def create_backend_registry() -> BackendRegistry:
    """Create the built-in backend table without sharing global state."""
    registry = BackendRegistry()
    registry.register(
        "null", lambda config: NullEngine(config.motor_count,
                                           config.dm_t_max))
    registry.register(
        "mujoco", lambda config: MujocoEngine(
            config.mujoco_model, config.motor_count, 1.0 / config.rate,
            config.dm_t_max))
    registry.register(
        "ros2", lambda config: Ros2Engine(
            config.motor_count, config.ros_motor_topic, config.ros_imu_topic,
            config.ros_joint_state_topic, config.ros_node_name,
            config.dm_t_max, config.ros_wait_imu, config.ros_reset_service,
            config.ros_reset_timeout))
    return registry


def create_engine(args: argparse.Namespace):
    """Construct a backend from a direct factory or an isolated registry."""
    if args.backend:
        engine = load_factory(args.backend)(args)
    else:
        registry = create_backend_registry()
        if args.backend_registry:
            load_registry(registry, args.backend_registry)
        engine = registry.create(args.engine, args)

    close = _MISSING_BACKEND_MEMBER
    try:
        # Resolve cleanup before inspecting other plugin-owned attributes.
        close = getattr(engine, "close", _MISSING_BACKEND_MEMBER)
        invalid_required = [
            name for name in BACKEND_REQUIRED_METHODS
            if not callable(getattr(engine, name, None))
        ]
        invalid_optional = []
        for name in BACKEND_OPTIONAL_METHODS:
            member = (close if name == "close" else
                      getattr(engine, name, _MISSING_BACKEND_MEMBER))
            if (member is not _MISSING_BACKEND_MEMBER and
                    not callable(member)):
                invalid_optional.append(name)
        if invalid_required or invalid_optional:
            problems = []
            if invalid_required:
                problems.append(
                    "missing callable methods: " + ", ".join(invalid_required))
            if invalid_optional:
                problems.append(
                    "optional methods must be callable when present: " +
                    ", ".join(invalid_optional))
            raise TypeError("invalid backend instance: " + "; ".join(problems))
        return engine
    except BaseException as exc:
        if callable(close):
            try:
                close()
            except BaseException as error:
                _report_cleanup_error(exc, "backend", error)
        else:
            exc.add_note("backend cleanup unavailable: no callable close obtained")
        raise


def engine_uses_external_time(engine) -> bool:
    """Backends opt into external timestamp mapping by exposing this field."""
    return hasattr(engine, "imu_timestamp_ns")


def run(args: argparse.Namespace, *, simulation_factory=None, imu_reader=None,
        on_session_started=None) -> int:
    _validate_worker_timing(args)
    if simulation_factory is not None and (
            not callable(simulation_factory) or not args.fdcan
            or args.backend or args.backend_registry or args.engine != "null"):
        raise ValueError("Simulation factory requires --fdcan and the default engine")
    for hook in (imu_reader, on_session_started):
        if hook is not None and (not callable(hook) or simulation_factory is None):
            raise ValueError("Simulation hooks require a callable and a Simulation factory")
    cosim_sock = None
    can_sock = None
    socketcan_sock = None
    engine = None
    simulation = None
    simulation_close = None
    primary_error = None
    try:
        cosim_sock = connect_unix(args.cosim, args.connect_timeout)
        cosim = (StepFramedSocket(cosim_sock) if args.protocol == "v2"
                 else FramedSocket(cosim_sock))
        can_sock = (connect_unix(args.fdcan, args.connect_timeout)
                    if args.fdcan else None)
        can = CanStream(can_sock) if can_sock else None
        socketcan_sock = open_socketcan(args.socketcan) if args.socketcan else None
        socketcan_tx = DatagramQueue(socketcan_sock) if socketcan_sock else None
        coordinator = None
        if simulation_factory is None:
            engine = create_engine(args)
            motor_adapter = DmMotorBusAdapter.from_namespace(args)
            coordinator = StepCoordinator(engine, motor_adapter)

        def start_session(session, mapper):
            """Replace actual graph owners only after the native RESET_ACK."""
            nonlocal simulation, simulation_close
            if simulation is not None:
                close = simulation_close
                simulation = simulation_close = None
                close()  # Cleanup failure terminates; never acquire a new graph.
            deadline = time.monotonic() + args.connect_timeout
            simulation = simulation_factory(session, mapper, can, socketcan_tx)
            # Capture cleanup before inspecting any other returned descriptor.
            simulation_close = getattr(simulation, "close", None)
            if not isinstance(simulation, Simulation) or not callable(simulation_close):
                raise TypeError("Simulation factory must return an unstarted Simulation")
            if simulation.state != SimulationState.NEW:
                raise ValueError("Simulation factory must return an unstarted Simulation")
            descriptions = simulation.topology.validate(simulation.registry)
            bridge = descriptions.get("transport")
            if bridge is None:
                raise ValueError("worker Simulation requires transport ingress")
            for port in ("qemu_rx",) + (("socketcan_rx",) if socketcan_tx else ()):
                if "external_input" not in bridge.port(port).capabilities:
                    raise ValueError("worker Simulation requires external transport ingress")
            simulation.start(deadline_monotonic=deadline)
            if on_session_started is not None:
                on_session_started(simulation)
            simulation.check_deadline()
        cosim_sock.setblocking(False)
        # The initial telemetry timestamp is the best available common clock
        # origin for v1. v2 deliberately starts the plant clock at zero and
        # reports QEMU's current clock in RESET_ACK payload metadata.
        clock_base_ns = 0
        v2_tx_validator = None
        v2_rx_validator = None
        qemu_timestamp_mapper = None
        if args.protocol == "v1":
            # QEMU emits its initial snapshot synchronously on chardev open. Keep
            # a short grace window for scheduling, but do not impose a large
            # startup stall on endpoints that intentionally omit telemetry.
            if select.select([cosim_sock], [], [], 0.02)[0]:
                for kind, _, timestamp_ns, payload in cosim.receive():
                    if kind == TELEMETRY and len(payload) == 12:
                        clock_base_ns = max(clock_base_ns, timestamp_ns)
            # Establish the QEMU RX ordering baseline before the first IMU sample.
            cosim.send(protocol_frame(RESET, 0, clock_base_ns))
        else:
            # A non-zero session ID prevents delayed frames from an earlier
            # machine reset from being accepted by the new QEMU epoch.  The
            # legacy zero-ID mode remains available to direct callers and old
            # smoke clients.
            session_id = step_protocol.generate_session_id()
            v2_tx_validator = step_protocol.StepSessionValidator(
                step_protocol.DIRECTION_HOST_TO_QEMU)
            v2_rx_validator = step_protocol.StepSessionValidator(
                step_protocol.DIRECTION_QEMU_TO_HOST,
                require_step_done=args.wait_step_done)
            reset_frame = step_protocol.StepFrame(
                step_protocol.StepHeader(step_protocol.STEP_VERSION,
                                         step_protocol.RESET, 0, 0, 0, 0,
                                         session_id),
                b"")
            v2_tx_validator.accept(reset_frame)
            v2_rx_validator.begin_reset(reset_frame.header.step_id,
                                        reset_frame.header.t_sim_ns,
                                        session_id)
            cosim.send(step_protocol.encode_step_frame(reset_frame))
            reset_ack_frame = wait_v2_response(
                cosim, v2_rx_validator, step_protocol.RESET_ACK, 0, 0)
            reset_ack = step_protocol.decode_reset_ack_payload(
                reset_ack_frame.payload)
            qemu_timestamp_mapper = QemuTimestampMapper(
                reset_ack.virtual_time_ns, 0)
        if args.protocol == "v1":
            qemu_timestamp_mapper = QemuTimestampMapper(
                clock_base_ns, clock_base_ns)
        if simulation_factory is not None:
            start_session(Session(1 if args.protocol == "v1" else session_id,
                                  clock_base_ns), qemu_timestamp_mapper)
        dt = 1.0 / args.rate
        integer_rate = (int(args.rate) if args.rate.is_integer() else 0)
        sequence = 0
        imu_frames = 0
        virtual_time_ns = clock_base_ns
        external_timestamp_mapper = (
            ExternalTimestampMapper(clock_base_ns)
            if engine_uses_external_time(engine) else None)
        wall_origin = time.monotonic_ns()

        def reset_v2_worker_session(reset: V2SessionReset) -> None:
            """Align host-side state with QEMU's unsolicited reset epoch."""
            nonlocal clock_base_ns, external_timestamp_mapper, imu_frames
            nonlocal sequence, virtual_time_ns, wall_origin
            nonlocal qemu_timestamp_mapper

            reset_frame = step_protocol.StepFrame(
                step_protocol.StepHeader(
                    step_protocol.STEP_VERSION, step_protocol.RESET, 0,
                    reset.step_id, reset.t_sim_ns, 0, reset.session_id),
                b"")
            v2_tx_validator.reset()
            v2_tx_validator.accept(reset_frame)
            if coordinator is not None:
                coordinator.reset()
            if hasattr(engine, "reset"):
                engine.reset()
            clock_base_ns = reset.t_sim_ns
            virtual_time_ns = reset.t_sim_ns
            sequence = 0
            imu_frames = 0
            wall_origin = time.monotonic_ns()
            external_timestamp_mapper = (
                ExternalTimestampMapper(clock_base_ns)
                if engine_uses_external_time(engine) else None)
            qemu_timestamp_mapper = QemuTimestampMapper(
                reset.t_sim_ns, reset.t_sim_ns)
            if simulation_factory is not None:
                start_session(Session(reset.session_id, reset.t_sim_ns),
                              qemu_timestamp_mapper)

        def complete_v2_reset(reset: V2SessionReset) -> None:
            reset = wait_v2_reset_ack(
                cosim, v2_rx_validator, reset, service_io=service_worker_io)
            reset_v2_worker_session(reset)

        def wait_v2_step_response(expected_kind: int, step_id: int,
                                  timestamp_ns: int,
                                  allow_queue_full: bool = False,
                                  timeout: float = V2_STEP_RESPONSE_TIMEOUT_SEC):
            try:
                return wait_v2_response(
                    cosim, v2_rx_validator, expected_kind, step_id, timestamp_ns,
                    timeout=timeout, allow_queue_full=allow_queue_full,
                    service_io=service_worker_io)
            except V2SessionReset as reset:
                complete_v2_reset(reset)
                return False

        if can_sock:
            can_sock.setblocking(False)
        if socketcan_sock:
            socketcan_sock.setblocking(False)

        def service_worker_io(timeout: float) -> None:
            """Service transport without consuming v2 replies or advancing plant."""
            readable = [cosim_sock]
            writable = []
            if can_sock:
                readable.append(can_sock)
            if socketcan_sock:
                readable.append(socketcan_sock)
            if cosim.tx_pending:
                writable.append(cosim_sock)
            if can and can.tx_pending:
                writable.append(can_sock)
            if socketcan_tx and socketcan_tx.tx_pending:
                writable.append(socketcan_sock)
            ready, writable_ready, _ = select.select(readable, writable, [], timeout)
            if cosim_sock in writable_ready:
                cosim.flush()
            if can_sock and can_sock in writable_ready:
                can.flush()
            if socketcan_sock and socketcan_sock in writable_ready:
                socketcan_tx.flush()
            if can_sock in ready:
                can_frames = can.receive()
                if args.verbose and can_frames:
                    print("received CAN frames", len(can_frames), flush=True)
                for can_id, flags, dlc, timestamp_ns, data in can_frames:
                    if simulation is not None:
                        simulation.schedule_input("transport.qemu_rx",
                            CanFrame(can_id, flags, dlc, data, timestamp_ns),
                            at_ns=simulation.t_sim_ns)
                        continue
                    mapped_timestamp_ns = qemu_timestamp_mapper.map(
                        timestamp_ns, virtual_time_ns)
                    if socketcan_sock:
                        socketcan_tx.send(
                            CanFrame(can_id, flags, dlc, data,
                                     mapped_timestamp_ns))
                    command = coordinator.submit_can(
                        can_id, flags, data[:min(dlc, 8)],
                        mapped_timestamp_ns, virtual_time_ns)
                    if command is not None:
                        if args.verbose:
                            print("motor command", command.mode,
                                  "index", command.index, flush=True)
            if socketcan_sock in ready:
                try:
                    raw = socketcan_sock.recv(72)
                    frame = unpack_can_frame(raw, timestamp_ns=virtual_time_ns)
                except ValueError as exc:
                    if args.verbose:
                        print(f"socketcan: ignored frame: {exc}",
                              file=sys.stderr, flush=True)
                else:
                    if simulation is not None:
                        simulation.schedule_input("transport.socketcan_rx", frame,
                                                  at_ns=simulation.t_sim_ns)
                    elif can:
                        can.send(
                            frame.can_id, frame.flags, frame.dlc,
                            qemu_timestamp_mapper.unmap(
                                frame.timestamp_ns,
                                qemu_timestamp_mapper.qemu_origin_ns),
                            frame.data)
            if cosim_sock in ready:
                if args.protocol == "v1":
                    for kind, _, _, payload in cosim.receive():
                        if kind == TELEMETRY and args.verbose:
                            if len(payload) == 12:
                                rgb, brightness, buzzer, flags, _ = \
                                    struct.unpack("<IIBBH", payload)
                                print("telemetry rgb=%06x brightness=%d buzzer=%d flags=%02x" %
                                      (rgb, brightness, buzzer, flags), flush=True)

        while args.frames == 0 or imu_frames < args.frames:
            service_worker_io(0)
            if coordinator is not None:
                gyro, accel = coordinator.advance(virtual_time_ns, dt)
            adc_count = (len(args.adc_input_values) +
                         len(args.adc_voltage_values)
                         if args.protocol == "v1" else 0)
            # Derive each IMU timestamp from the sample index instead of
            # repeatedly adding a rounded 1/rate interval.  The latter
            # accumulates a deterministic drift at non-integral rates such as
            # 333 Hz.  ADC frames still occupy strictly increasing sub-ticks;
            # at ordinary rates they fit inside the ideal IMU interval.
            if integer_rate:
                # Exact round-half-to-even for the common integer-rate path;
                # this is equivalent to the previous float round() result
                # without a per-step floating-point division.
                numerator = (imu_frames + 1) * 1_000_000_000
                quotient, remainder = divmod(numerator, integer_rate)
                twice_remainder = remainder * 2
                if (twice_remainder > integer_rate or
                        (twice_remainder == integer_rate and quotient & 1)):
                    quotient += 1
                ideal_time_ns = clock_base_ns + quotient
            else:
                ideal_time_ns = clock_base_ns + int(round(
                    (imu_frames + 1) * 1_000_000_000.0 / args.rate))
            if external_timestamp_mapper is not None:
                ideal_time_ns = external_timestamp_mapper.map(
                    getattr(engine, "imu_timestamp_ns", None), ideal_time_ns)
            step_span_ns = max(ideal_time_ns - virtual_time_ns,
                               adc_count + 1)
            virtual_time_ns += step_span_ns
            if simulation is not None:
                result = simulation.run_until(virtual_time_ns, deadline_monotonic=(
                    time.monotonic() + V2_STEP_RESPONSE_TIMEOUT_SEC))
                if (not isinstance(result, StepResult) or result.status != "ok"
                        or type(result.reached_time_ns) is not int
                        or result.reached_time_ns != virtual_time_ns):
                    raise RuntimeError(f"Simulation did not reach target: {result!r}")
                if imu_reader is None:
                    # Explicit stationary fixture, in existing BMI088 wire units.
                    gyro, accel = (0., 0., 0.), (0., 0., 1.)
                else:
                    gyro, accel = imu_to_bmi088_units(imu_reader(simulation))
                simulation.check_deadline()
            if args.protocol == "v1":
                for input_index, (channel, raw) in enumerate(
                        args.adc_input_values):
                    sequence += 1
                    cosim.send(adc_input_frame(
                        sequence, virtual_time_ns - step_span_ns +
                        input_index + 1, channel, raw))
                for input_index, (channel, voltage_uv) in enumerate(
                        args.adc_voltage_values,
                        start=len(args.adc_input_values)):
                    sequence += 1
                    cosim.send(adc_voltage_frame(
                        sequence, virtual_time_ns - step_span_ns +
                        input_index + 1, channel, voltage_uv))
                sequence += 1
                cosim.send(imu_frame(sequence, virtual_time_ns, gyro, accel))
            else:
                step_id = imu_frames + 1
                imu_sample = step_protocol.ImuSampleV2(
                    tuple(gyro), tuple(accel), (0.0,) * 3, (0.0,) * 3,
                    virtual_time_ns, step_id)
                if args.adc_input_values or args.adc_voltage_values:
                    imu_payload = step_protocol.build_step_input(
                        imu_sample,
                        (step_protocol.AdcInputV2(channel, raw)
                         for channel, raw in args.adc_input_values),
                        (step_protocol.AdcVoltageV2(channel, voltage_uv)
                         for channel, voltage_uv in args.adc_voltage_values))
                else:
                    # Preserve the compact v2 IMU-only payload for clients
                    # that do not need extensible input sections.
                    imu_payload = step_protocol.encode_imu_sample_v2(imu_sample)
                step_frame = step_protocol.StepFrame(
                    step_protocol.StepHeader(
                        step_protocol.STEP_VERSION, step_protocol.STEP,
                        len(imu_payload), step_id, virtual_time_ns,
                        step_span_ns, v2_tx_validator.session_id),
                    imu_payload)
                v2_tx_validator.accept(step_frame)
                step_wire = step_protocol.encode_step_frame(step_frame)
                try:
                    send_v2_step(
                        cosim, v2_rx_validator, step_wire, step_id,
                        virtual_time_ns, service_io=service_worker_io)
                except V2SessionReset as reset:
                    complete_v2_reset(reset)
                    continue
                if args.wait_step_done:
                    done_retries = 0
                    while True:
                        try:
                            done_response = wait_v2_step_response(
                                step_protocol.STEP_DONE, step_id,
                                virtual_time_ns)
                        except TimeoutError:
                            if done_retries >= V2_STEP_DONE_MAX_RETRIES:
                                raise
                            done_retries += 1
                            if not cosim.tx_pending:
                                cosim.send(step_wire)
                            continue
                        break
                    if done_response is False:
                        continue
            if can and simulation is None and coordinator.active_indices:
                send_dm_feedback(can, engine,
                                 sorted(coordinator.active_indices),
                                 virtual_time_ns, motor_adapter,
                                 qemu_timestamp_mapper)
            imu_frames += 1
            if args.realtime:
                # wall_origin corresponds to clock_base_ns, not virtual time
                # zero.  Subtract the common origin so a QEMU endpoint that
                # has already advanced its virtual clock does not add an
                # artificial startup delay.
                target = realtime_target_ns(wall_origin, virtual_time_ns,
                                            clock_base_ns)
                remaining = target - time.monotonic_ns()
                if remaining > 0:
                    time.sleep(remaining / 1_000_000_000)
        cosim.drain()
        if can:
            can.drain()
        if socketcan_tx:
            socketcan_tx.drain()
    except BaseException as exc:
        primary_error = exc
        if simulation is not None and not callable(simulation_close):
            exc.add_note("Simulation cleanup unavailable: no callable close obtained")
        raise
    finally:
        cleanup_errors = []
        # A failing backend must not prevent any transport from closing.
        # Catch interrupts here as well, then propagate after every attempt.
        for name, resource in (("Simulation", simulation), ("backend", engine),
                               ("SocketCAN", socketcan_sock),
                               ("CAN", can_sock), ("co-sim", cosim_sock)):
            if resource is None:
                continue
            try:
                close = (simulation_close if name == "Simulation"
                         else getattr(resource, "close", None))
                if callable(close):
                    close()
            except BaseException as exc:
                cleanup_errors.append((name, exc))
        for name, error in cleanup_errors:
            reported_error = (primary_error if primary_error is not None
                              else cleanup_errors[0][1])
            _report_cleanup_error(reported_error, name, error)
        if cleanup_errors and primary_error is None:
            raise cleanup_errors[0][1]
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args()))
    except (BufferError, ConnectionError, OSError, TimeoutError, TypeError,
            ValueError, RuntimeError, SocketCANError) as exc:
        print(f"dm_mc02_sim_worker: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(f"dm_mc02_sim_worker: {note}", file=sys.stderr)
        raise SystemExit(1)
