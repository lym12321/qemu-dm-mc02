"""Named SI joints, link poses and IMUs over the Gazebo Sim 8.15 adapter.

Owns one synchronous server, with fixed native-ns SDF ticks and one exclusive
torque owner per bound revolute joint. Getters return completed PostUpdate
observations. No ROS, sensor, motor, CAN or QEMU clock policy lives here.
"""

from collections.abc import Mapping
import math
from pathlib import Path

from .base import BodyPose, ImuSample, JointState


def _step_ns(dt_ns):
    if type(dt_ns) is not int or not 0 < dt_ns <= (1 << 63) - 1:
        raise ValueError("Gazebo dt_ns must be a positive signed-64-bit integer")
    return dt_ns


def _effort(name, value):
    if not isinstance(name, str):
        raise ValueError("joint name must be a string")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("joint effort must be finite")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError("joint effort must be finite") from exc
    if not math.isfinite(value):
        raise ValueError("joint effort must be finite")
    return value


def _binding_map(values, label, arity=2):
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError(f"Gazebo {label} bindings must be a mapping")
    bindings = {}
    for name, target in values.items():
        if not isinstance(target, tuple) or len(target) != arity:
            raise ValueError(f"Gazebo {label} binding requires {arity} entity names")
        for value in (name, *target):
            if (not isinstance(value, str) or not value or "\0" in value
                    or "::" in value):
                raise ValueError("Gazebo names must be nonempty and unscoped without NUL")
        bindings[name] = target
    if len(set(bindings.values())) != len(bindings):
        raise ValueError(f"duplicate native {label} binding")
    return bindings


class GazeboPhysics:
    def __init__(self, path, *, joints: Mapping[str, tuple[str, str]] | None = None,
                 bodies: Mapping[str, tuple[str, str]] | None = None,
                 imus: Mapping[str, tuple[str, str, str]] | None = None,
                 resource_paths: tuple[str | Path, ...] = ()):
        if not isinstance(path, (str, Path)) or not str(path) or "\0" in str(path):
            raise ValueError("SDF path must be nonempty without NUL")
        bindings = _binding_map(joints, "joint")
        body_bindings = _binding_map(bodies, "body")
        imu_bindings = _binding_map(imus, "IMU", 3)
        if not bindings and not body_bindings and not imu_bindings:
            raise ValueError("Gazebo requires at least one joint, body or IMU binding")
        if not isinstance(resource_paths, tuple):
            raise ValueError("Gazebo resource_paths must be a tuple of directories")
        directories = []
        for directory in resource_paths:
            if (not isinstance(directory, (str, Path)) or not str(directory)
                    or "\0" in str(directory) or ":" in str(directory)):
                raise ValueError("resource paths require directories without NUL/colon")
            resolved = Path(directory).resolve()
            if not resolved.is_dir() or ":" in str(resolved):
                raise ValueError("resource paths require existing directories without colon")
            directories.append(str(resolved))
        try:
            import _dm_gazebo_native as native
        except ImportError as exc:
            raise ImportError("Gazebo requires the optional native adapter built for this "
                              "Python ABI; see docs/USAGE.md") from exc
        if native.gazebo_version != "8.15.0":
            raise RuntimeError("Gazebo adapter requires gz-sim 8.15.0")
        self._world = native.World(str(Path(path).resolve()), bindings, body_bindings,
                                   imu_bindings, directories)

    @property
    def timestep_ns(self) -> int:
        return self._world.timestep_ns

    @property
    def time_ns(self) -> int:
        """Native completed plant time; independent of transport/session epoch."""
        return self._world.time_ns

    @property
    def time_s(self) -> float:
        return self.time_ns / 1_000_000_000

    def step(self, dt_ns: int) -> None:
        self._world.step(_step_ns(dt_ns))

    def validate_step(self, dt_ns: int) -> None:
        """Check exact native tick/target admission without advancing."""
        self._world.validate_step(_step_ns(dt_ns))

    def validate_steps(self, intervals_ns: tuple[int, ...]) -> None:
        """Check sequential native targets without writing controls or stepping."""
        if not isinstance(intervals_ns, tuple) or not intervals_ns:
            raise ValueError("step intervals must be a nonempty tuple")
        self._world.validate_steps(tuple(_step_ns(dt) for dt in intervals_ns))

    def reset(self) -> None:
        """Complete native reset and paused state projection, without integration."""
        self._world.reset()

    def set_joint_effort(self, name: str, value: float) -> None:
        self._world.set_joint_effort(name, _effort(name, value))

    def validate_joint_effort(self, name: str, value: float) -> None:
        """Check binding/finite/SDF limit admission without changing torque."""
        self._world.validate_joint_effort(name, _effort(name, value))

    def get_joint_state(self, name: str) -> JointState:
        if not isinstance(name, str):
            raise ValueError("joint name must be a string")
        return JointState(*self._world.get_joint_state(name))

    def get_body_pose(self, name: str) -> BodyPose:
        """Completed link frame pose in world coordinates (m, wxyz)."""
        if not isinstance(name, str):
            raise ValueError("body name must be a string")
        values = self._world.get_body_pose(name)
        return BodyPose(tuple(values[:3]), tuple(values[3:]))

    def get_imu(self, name: str) -> ImuSample:
        """Completed native sensor-local rad/s and proper acceleration m/s²."""
        if not isinstance(name, str):
            raise ValueError("IMU name must be a string")
        values = self._world.get_imu(name)
        return ImuSample(tuple(values[:3]), tuple(values[3:]))

    def close(self) -> None:
        """Stop and destroy the owned native Server; repeated close is harmless."""
        self._world.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, primary, traceback):
        try:
            self.close()
        except BaseException as exc:
            if primary is None:
                raise
            primary.add_note(f"Gazebo close failed: {exc}")
