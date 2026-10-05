"""Named SI boundary over the native MuJoCo solver; no board or wire policy.

The caller transfers exclusive ownership of a compiled model. Runtime callers
use these methods rather than mutating MjModel/MjData. Only bound hinge joints
with immediate, unclamped unit-gear motors and paired native site IMUs are
admitted. Other model dynamics remain MuJoCo's responsibility.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import math

from .base import BodyPose, JointState


@dataclass(frozen=True)
class ImuSample:
    gyro_rad_s: tuple[float, float, float]
    accel_m_s2: tuple[float, float, float]


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class MujocoPhysics:
    def __init__(self, model, *, joints: Mapping[str, str],
                 imus: Mapping[str, tuple[str, str]] | None = None):
        import mujoco

        if not isinstance(model, mujoco.MjModel):
            raise ValueError("model must be a compiled MuJoCo MjModel")
        if not isinstance(joints, Mapping) or (imus is not None
                                              and not isinstance(imus, Mapping)):
            raise ValueError("joint and IMU bindings must be mappings")
        self._mj = mujoco
        self._model = model
        if _finite(model.opt.timestep, "MuJoCo timestep") <= 0:
            raise ValueError("MuJoCo timestep must be positive")
        self._joints = {}
        self._imus = {}
        self._failed = False
        self._closed = False
        for name, actuator in joints.items():
            joint_id = self._id(mujoco.mjtObj.mjOBJ_JOINT, name)
            actuator_id = self._id(mujoco.mjtObj.mjOBJ_ACTUATOR, actuator)
            self._joints[name] = self._bind_joint(joint_id, actuator_id, name)
        for name, pair in (imus or {}).items():
            if not isinstance(name, str) or not name:
                raise ValueError("IMU binding names must be nonempty strings")
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError(f"IMU {name}: expected (gyro, accelerometer) names")
            gyro_id, accel_id = (self._id(mujoco.mjtObj.mjOBJ_SENSOR, sensor)
                                 for sensor in pair)
            self._imus[name] = self._bind_imu(gyro_id, accel_id, name)
        # Admission is complete before any runtime data or solver projection.
        self._data = mujoco.MjData(model)
        mujoco.mj_forward(model, self._data)
        self._check_solver()

    @classmethod
    def from_xml_path(cls, path, *, joints: Mapping[str, str],
                      imus: Mapping[str, tuple[str, str]] | None = None):
        import mujoco

        return cls(mujoco.MjModel.from_xml_path(str(path)), joints=joints, imus=imus)

    def _id(self, kind, name: str) -> int:
        if not isinstance(name, str) or not name:
            raise ValueError("MuJoCo names must be nonempty strings")
        result = self._mj.mj_name2id(self._model, kind, name)
        if result < 0:
            raise ValueError(f"unknown MuJoCo name: {name}")
        return result

    def _bind_joint(self, j: int, a: int, name: str) -> tuple[int, int, int]:
        m, mj = self._model, self._mj
        # MuJoCo 3.12 supports multi-control/output actuators. Their addresses
        # are separate from actuator IDs (XML reference: actuator/motor).
        c, o = int(m.actuator_ctrladr[a]), int(m.actuator_outadr[a])
        group = int(m.actuator_group[a])
        if not (
            m.jnt_type[j] == mj.mjtJoint.mjJNT_HINGE
            and m.actuator_trntype[a] == mj.mjtTrn.mjTRN_JOINT
            and m.actuator_trnid[a, 0] == j
            and m.actuator_ctrlnum[a] == 1 and m.actuator_outnum[a] == 1
            and 0 <= c < m.nu and 0 <= o < m.nout
            and tuple(m.actuator_gear[o]) == (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            and m.actuator_dyntype[a] == mj.mjtDyn.mjDYN_NONE
            and m.actuator_actnum[a] == 0 and m.actuator_actadr[a] == -1
            and m.actuator_gaintype[a] == mj.mjtGain.mjGAIN_FIXED
            and m.actuator_gainprm[a, 0] == 1
            and m.actuator_biastype[a] == mj.mjtBias.mjBIAS_NONE
            and not m.actuator_ctrllimited[c] and not m.actuator_forcelimited[a]
            and not m.jnt_actfrclimited[j] and not m.jnt_actgravcomp[j]
            and not any(m.actuator_history[a]) and m.actuator_delay[a] == 0
            and m.actuator_historyadr[a] == -1
            and m.actuator_plugin[a] == -1
            and not (m.opt.disableflags & mj.mjtDisableBit.mjDSBL_ACTUATION)
            and not (0 <= group <= 30 and m.opt.disableactuator & (1 << group))
        ):
            raise ValueError(f"joint {name}: requires an immediate unclamped unit-gear hinge motor")
        return int(m.jnt_qposadr[j]), int(m.jnt_dofadr[j]), c

    def _bind_imu(self, gyro: int, accel: int, name: str) -> tuple[int, int]:
        m, mj = self._model, self._mj
        for sensor, kind in ((gyro, mj.mjtSensor.mjSENS_GYRO),
                             (accel, mj.mjtSensor.mjSENS_ACCELEROMETER)):
            if not (
                m.sensor_type[sensor] == kind and m.sensor_dim[sensor] == 3
                and m.sensor_objtype[sensor] == mj.mjtObj.mjOBJ_SITE
                and m.sensor_cutoff[sensor] == 0
                and not any(m.sensor_history[sensor])
                and m.sensor_historyadr[sensor] == -1
                and m.sensor_delay[sensor] == 0
                and not any(m.sensor_interval[sensor])
                and m.sensor_noise[sensor] == 0
                and m.sensor_plugin[sensor] == -1
                and not (m.opt.disableflags & mj.mjtDisableBit.mjDSBL_SENSOR)
            ):
                raise ValueError(f"IMU {name}: requires immediate native site gyro/accelerometer")
        if m.sensor_objid[gyro] != m.sensor_objid[accel]:
            raise ValueError(f"IMU {name}: gyro and accelerometer must share a site")
        return int(m.sensor_adr[gyro]), int(m.sensor_adr[accel])

    @property
    def time_s(self) -> float:
        """Native floating-point plant time, independent of any transport epoch."""
        self._check_open()
        return float(self._data.time)

    def reset(self) -> None:
        self._check_open()
        self._failed = True
        self._mj.mj_resetData(self._model, self._data)
        self._mj.mj_forward(self._model, self._data)
        self._check_solver()
        self._failed = False

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("MuJoCo physics is closed")

    def _check_live(self) -> None:
        self._check_open()
        if self._failed:
            raise RuntimeError("MuJoCo solver failed; reset required")

    def close(self) -> None:
        """Release the exclusively owned native data and compiled model once."""
        if self._closed:
            return
        self._closed = True
        self._data = None
        self._model = None

    def _check_solver(self) -> None:
        # Native bad-number handling can replace controls or reset data. Do
        # not report that altered state as a successful physical observation.
        for name in ("mjWARN_BADCTRL", "mjWARN_BADQPOS", "mjWARN_BADQVEL", "mjWARN_BADQACC"):
            warning = getattr(self._mj.mjtWarning, name)
            if self._data.warning[warning].number:
                self._failed = True
                raise RuntimeError(f"MuJoCo solver warning {name}; reset required")
        # mj_step checks state before integration. Reuse the native bad-number
        # predicate at the target too, rather than waiting for the next step's
        # automatic reset or maintaining a separate numerical threshold.
        for name in ("qpos", "qvel", "qacc", "sensordata"):
            self._check_values(name, getattr(self._data, name))

    def _check_values(self, name: str, values) -> None:
        if any(self._mj.mju_isBad(value) for value in values):
            self._failed = True
            raise RuntimeError(f"MuJoCo invalid target {name}; reset required")

    def _step_dt_ns(self, dt_ns: int, time_s: float | None = None) -> float:
        self._check_live()
        if type(dt_ns) is not int or not 0 < dt_ns <= (1 << 63) - 1:
            raise ValueError("MuJoCo dt_ns must be a positive signed-64-bit integer")
        return self._step_dt_seconds(dt_ns / 1_000_000_000, time_s)

    def _step_dt_seconds(self, dt: float, time_s: float | None = None) -> float:
        self._check_live()
        dt = _finite(dt, "MuJoCo step dt")
        time_s = self.time_s if time_s is None else time_s
        target = time_s + dt
        if dt <= 0 or not math.isfinite(target) or target <= time_s:
            raise ValueError("MuJoCo step dt must be positive and advance native time")
        return dt

    def validate_step(self, dt_ns: int) -> None:
        """Check a native step without changing time, controls or solver state."""
        self._step_dt_ns(dt_ns)

    def validate_steps(self, intervals_ns: tuple[int, ...]) -> None:
        """Check every sequential native float target without advancing."""
        self._check_live()
        if not isinstance(intervals_ns, tuple) or not intervals_ns:
            raise ValueError("step intervals must be a nonempty tuple")
        target = self.time_s
        for dt_ns in intervals_ns:
            target += self._step_dt_ns(dt_ns, target)

    def step(self, dt_ns: int) -> None:
        self._advance(self._step_dt_ns(dt_ns))

    def step_seconds(self, dt: float) -> None:
        """Legacy float-dt admission: preserve the value without ns rounding."""
        self._advance(self._step_dt_seconds(dt))

    def _advance(self, dt: float) -> None:
        self._failed = True
        self._model.opt.timestep = dt
        self._mj.mj_step(self._model, self._data)
        self._check_solver()
        # Native sensors/derived pose precede integration in mj_step. Forward
        # refreshes target observations without another integration or tick.
        self._mj.mj_forward(self._model, self._data)
        self._check_solver()
        self._failed = False

    def _joint_effort(self, name: str, value: float) -> tuple[int, float]:
        self._check_live()
        binding = self._joints.get(name) if isinstance(name, str) else None
        if binding is None:
            raise ValueError(f"unbound joint: {name}")
        return binding[2], _finite(value, "joint effort")

    def validate_joint_effort(self, name: str, value: float) -> None:
        """Check bound finite joint torque without writing the held control."""
        self._joint_effort(name, value)

    def set_joint_effort(self, name: str, value: float) -> None:
        control, effort = self._joint_effort(name, value)
        self._data.ctrl[control] = effort

    def get_joint_state(self, name: str) -> JointState:
        self._check_live()
        binding = self._joints.get(name) if isinstance(name, str) else None
        if binding is None:
            raise ValueError(f"unbound joint: {name}")
        q, v, _ = binding
        return JointState(float(self._data.qpos[q]), float(self._data.qvel[v]))

    def get_body_pose(self, name: str) -> BodyPose:
        self._check_live()
        body = self._id(self._mj.mjtObj.mjOBJ_BODY, name)
        position = tuple(float(v) for v in self._data.xpos[body])
        quaternion = tuple(float(v) for v in self._data.xquat[body])
        self._check_values("body pose", position + quaternion)
        return BodyPose(position, quaternion)

    def get_imu(self, name: str) -> ImuSample:
        self._check_live()
        binding = self._imus.get(name) if isinstance(name, str) else None
        if binding is None:
            raise ValueError(f"unbound IMU: {name}")
        gyro, accel = binding
        return ImuSample(tuple(float(v) for v in self._data.sensordata[gyro:gyro + 3]),
                         tuple(float(v) for v in self._data.sensordata[accel:accel + 3]))
