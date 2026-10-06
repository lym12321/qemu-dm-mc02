"""Protocol-only M3508 consumers share one native, uncalibrated plant.

The isolated hinges have literal Izz=.02 and no damping/contact. Euler's
velocity then position update and C620 literal bytes are independent oracles;
the plant's scheduling and feedback helpers are not used to derive expectations.
"""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.core import Coordinator, Session, StepContext
from sim.devices.dji import M3508, M3508Device, M3508JointPlant
from sim.fabric import CanBus, CanFrame, VirtualCanDevice
from sim.physics import JointState, MujocoPhysics


class Host(VirtualCanDevice):
    def __init__(self, bus):
        self.frames = []
        super().__init__(bus, "host")

    def receive(self, frame):
        self.frames.append(frame)


def physics(*, initial_position=0):
    mujoco = pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    bodies = "".join(
        f'<body name="b{i}" pos="{i} 0 0">'
        f'<joint name="j{i}" type="hinge" axis="0 0 1" ref="{initial_position}"/>'
        '<inertial pos="0 0 0" mass="1" diaginertia=".015 .015 .02"/>'
        '</body>' for i in range(1, 5))
    motors = "".join(f'<motor name="m{i}" joint="j{i}"/>' for i in range(1, 5))
    model = mujoco.MjModel.from_xml_string(
        '<mujoco><compiler angle="radian"/>'
        '<option gravity="0 0 0" integrator="Euler"/>'
        f'<worldbody>{bodies}</worldbody><actuator>{motors}</actuator></mujoco>')
    return MujocoPhysics(model, joints={f"j{i}": f"m{i}" for i in range(1, 5)})


def native_stub():
    # Isolate native dependencies without implementing a solver or motor law.
    return SimpleNamespace(
        reset=lambda: None, step=lambda _: None,
        get_joint_state=lambda _: JointState(0, 0),
        set_joint_effort=lambda *_: None, validate_step=lambda _: None,
        validate_steps=lambda _: None,
        validate_joint_effort=lambda *_: None, close=lambda: None)


def setup(*, bus=None, native=None, timeouts=(100_000_000,) * 4):
    bus = bus or CanBus()
    host = Host(bus)
    devices = tuple(M3508Device(bus, i, command_timeout_ns=timeout)
                    for i, timeout in enumerate(timeouts, 1))
    native = native or physics()
    plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2", "j3", "j4"))),
                            gear_ratio=2)
    return bus, host, devices, native, plant


def track_physics(monkeypatch, native):
    calls = {"reset": 0, "step": [], "effort": []}
    reset, step, effort = native.reset, native.step, native.set_joint_effort

    def observed_reset():
        calls["reset"] += 1
        reset()

    def observed_step(dt_ns):
        calls["step"].append(dt_ns)
        step(dt_ns)

    def observed_effort(name, torque):
        calls["effort"].append((name, torque))
        effort(name, torque)

    monkeypatch.setattr(native, "reset", observed_reset)
    monkeypatch.setattr(native, "step", observed_step)
    monkeypatch.setattr(native, "set_joint_effort", observed_effort)
    return calls


def command(bus, host, literal, *, timestamp_ns=0):
    host.emit(CanFrame(0x200, data=bytes.fromhex(literal), timestamp_ns=timestamp_ns))
    bus.step(timestamp_ns)


def test_bus_local_identity_admission_precedes_native_effects(monkeypatch):
    buses, native = (CanBus(), CanBus()), native_stub()
    devices = tuple(M3508Device(bus, 1) for bus in buses)
    duplicate = M3508Device(buses[0], 1, name="duplicate")
    calls = track_physics(monkeypatch, native)
    try:
        for bindings, message in (
                ({devices[0]: "j1", devices[1]: "j1"}, "one M3508 owner"),
                ({devices[0]: "j1", duplicate: "j2"}, "unique motor IDs")):
            with pytest.raises(ValueError, match=message):
                M3508JointPlant(native, bindings)
            assert all(not device.closed for device in (*devices, duplicate))
        # Even different buses require every initial sample before ownership.
        with monkeypatch.context() as patch:
            patch.setattr(native, "get_joint_state", lambda name:
                          JointState(float("nan") if name == "j2" else 0, 0))
            with pytest.raises(ValueError, match="joint position"):
                M3508JointPlant(native, dict(zip(devices, ("j1", "j2"))))
        plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2"))))
        assert plant.devices == devices
        assert calls == {"reset": 0, "step": [], "effort": []}
        plant.close()
        assert all(device.closed for device in devices) and not duplicate.closed
    finally:
        for device in (*devices, duplicate):
            device.close()


def test_multibus_expiry_union_and_lifecycle_leave_buses_borrowed(monkeypatch):
    buses, native = (CanBus(), CanBus()), native_stub()
    hosts = tuple(Host(bus) for bus in buses)
    devices = tuple(M3508Device(bus, 1, command_timeout_ns=timeout)
                    for bus, timeout in zip(buses, (3_000_000, 7_000_000)))
    plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2"))), gear_ratio=2)
    calls = track_physics(monkeypatch, native)
    try:
        plant.reset(Session(1, 7))
        for bus, host, literal in zip(buses, hosts,
                                      ("1000 0000 0000 0000", "f000 0000 0000 0000")):
            bus.reset(7)  # Bus lifecycle/delivery belongs to this fixture.
            command(bus, host, literal, timestamp_ns=7)
        result = plant.step(StepContext(1, 1, 10_000_007, 10_000_000))
        assert result.diagnostics == {"physics_segments": 3}
        assert calls == {"reset": 1, "step": [3_000_000, 4_000_000, 3_000_000],
                         "effort": [("j1", .2), ("j2", -.2),
                                    ("j1", 0), ("j2", -.2),
                                    ("j1", 0), ("j2", 0)]}
        assert all(bus.t_sim_ns == 7 and bus.pending == 1 for bus in buses)
        assert all(host.frames == [] for host in hosts)
        assert all(device.current_raw == 0 for device in devices)
        plant.reset(Session(2, 50_000_000))
        assert calls["reset"] == 2
        assert all(device.t_sim_ns == 50_000_000 for device in devices)
        plant.close()
        plant.close()
        # Neither reset nor close consumes the already queued feedback.
        for bus, host in zip(buses, hosts):
            assert bus.t_sim_ns == 7 and bus.pending == 1
            bus.step(10_000_007)
            assert [(f.can_id, f.timestamp_ns, f.data) for f in host.frames] == [
                (0x201, 10_000_007, bytes.fromhex("0000 0000 0000 19 00"))]
            host.emit(CanFrame(0x123, data=b"borrowed", timestamp_ns=10_000_007))
            assert bus.pending == 1
    finally:
        plant.close()


def test_multibus_feedback_failure_preserves_partial_commits_and_freezes(monkeypatch):
    buses, native = (CanBus(), CanBus(max_pending=1)), native_stub()
    hosts = tuple(Host(bus) for bus in buses)
    devices = tuple(M3508Device(bus, 1) for bus in buses)
    plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2"))))
    calls = track_physics(monkeypatch, native)
    try:
        plant.reset(Session(1))
        hosts[1].emit(CanFrame(0x123, timestamp_ns=100_000_000))
        ctx = StepContext(1, 1, 1_000_000, 1_000_000)
        with pytest.raises(BufferError, match="capacity"):
            plant.step(ctx)
        assert calls["step"] == [1_000_000]
        assert all(device.t_sim_ns == 1_000_000 for device in devices)
        assert all(bus.t_sim_ns == 0 and bus.pending == 1 for bus in buses)
        assert plant.failed and plant.t_sim_ns == 0
        with pytest.raises(RuntimeError, match="successful reset"):
            plant.step(ctx)
        assert calls["step"] == [1_000_000]
        plant.reset(Session(2))
        assert not plant.failed and all(bus.pending == 1 for bus in buses)
    finally:
        plant.close()


def test_fixed_four_wheel_runtime_rejects_multibus_before_imu_or_endpoint(monkeypatch):
    from sim.robots import four_wheel
    buses, native = (CanBus(), CanBus()), native_stub()
    devices = tuple(M3508Device(buses[i % 2], i + 1) for i in range(4))
    plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2", "j3", "j4"))))
    effects = []
    monkeypatch.setattr(native, "get_imu", lambda _: effects.append("imu"), raising=False)
    monkeypatch.setattr(four_wheel, "QemuCanEndpoint", lambda *_: effects.append("endpoint"))
    try:
        with pytest.raises(ValueError, match="one CAN bus"):
            four_wheel.FourWheelRuntime(plant, object())
        assert effects == [] and not plant.closed
        assert all(not device.closed for device in devices)
        assert all(bus.pending == 0 and bus.t_sim_ns == 0 for bus in buses)
    finally:
        plant.close()


def test_native_gear_conversion_four_devices_one_advance_and_literal_feedback(monkeypatch):
    bus, host, devices, native, plant = setup()
    calls = track_physics(monkeypatch, native)
    plant.reset(Session(1))
    command(bus, host, "1000 f000 0000 0000")
    result = plant.step(StepContext(1, 1, 10_000_000, 10_000_000))
    assert calls == {"reset": 1, "step": [10_000_000], "effort": [
        ("j1", .2), ("j2", -.2), ("j3", 0), ("j4", 0)]}
    # 5A * .02Nm/A * gear2 / Izz.02 = 10rad/s² on the output joint.
    assert native.get_joint_state("j1") == JointState(.001, .1)
    assert native.get_joint_state("j2") == JointState(-.001, -.1)
    assert devices[0].get_rotor_state() == pytest.approx((.002, .2))
    assert devices[1].get_rotor_state() == pytest.approx((-.002, -.2))
    assert native.time_s == pytest.approx(.01)
    assert result.status == "ok" and result.reached_time_ns == 10_000_000
    assert result.diagnostics == {"physics_segments": 1}
    assert plant.t_sim_ns == 10_000_000 and not plant.failed
    bus.step(10_000_000)
    assert [(frame.can_id, frame.timestamp_ns, frame.data) for frame in host.frames] == [
        (0x201, 10_000_000, bytes.fromhex("0002 0002 1000 19 00")),
        (0x202, 10_000_000, bytes.fromhex("1ffd fffe f000 19 00")),
        (0x203, 10_000_000, bytes.fromhex("0000 0000 0000 19 00")),
        (0x204, 10_000_000, bytes.fromhex("0000 0000 0000 19 00"))]


def test_native_multibus_repeated_ids_isolated_commands_and_feedback(monkeypatch):
    native = physics()
    buses = (CanBus(), CanBus())
    hosts = tuple(Host(bus) for bus in buses)
    devices = tuple(M3508Device(bus, motor_id)
                    for bus in buses for motor_id in (1, 2))
    plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2", "j3", "j4"))),
                            gear_ratio=2)
    coordinator = Coordinator({"world": plant})
    calls = track_physics(monkeypatch, native)
    try:
        coordinator.reset(Session(1, 7))
        for bus, host, literal in zip(buses, hosts,
                                      ("1000 2000 0000 0000", "f000 e000 0000 0000")):
            bus.reset(7)
            command(bus, host, literal, timestamp_ns=7)
        result = coordinator.step(10_000_000)["world"]
        assert calls == {"reset": 1, "step": [10_000_000], "effort": [
            ("j1", .2), ("j2", .4), ("j3", -.2), ("j4", -.4)]}
        # Literal Izz=.02, gear2 and +/-5/10A: Euler v=alpha*dt,
        # theta=v*dt. The second bus must drive opposite native joints.
        for name, position, velocity in zip(("j1", "j2", "j3", "j4"),
                (.001, .002, -.001, -.002), (.1, .2, -.1, -.2)):
            assert native.get_joint_state(name) == JointState(position, velocity)
        assert native.time_s == .01 and result.reached_time_ns == 10_000_007
        assert result.diagnostics == {"physics_segments": 1}
        assert all(bus.t_sim_ns == 7 and bus.pending == 2 for bus in buses)
        for bus in buses:
            bus.step(10_000_007)
        for host, payloads in zip(hosts, (
                ("0002 0002 1000 19 00", "0005 0004 2000 19 00"),
                ("1ffd fffe f000 19 00", "1ffa fffc e000 19 00"))):
            assert [(f.can_id, f.timestamp_ns, f.data) for f in host.frames] == [
                (0x201 + i, 10_000_007, bytes.fromhex(payload))
                for i, payload in enumerate(payloads)]
        coordinator.reset(Session(2, 50_000_000))
        assert calls["reset"] == 2 and native.time_s == 0
        assert all(device.get_rotor_state() == (0, 0) for device in devices)
        assert all(bus.t_sim_ns == 10_000_007 for bus in buses)
    finally:
        coordinator.close()


def test_distinct_expiries_split_shared_time_once_and_zero_every_expired_joint(monkeypatch):
    bus, host, devices, native, plant = setup(
        timeouts=(3_000_000, 7_000_000, 10_000_000, 3_000_000))
    calls = track_physics(monkeypatch, native)
    plant.reset(Session(1))
    command(bus, host, "1000 f000 2000 e000")
    result = plant.step(StepContext(1, 1, 10_000_000, 10_000_000))
    assert calls["step"] == [3_000_000, 4_000_000, 3_000_000]
    assert calls["effort"] == [
        ("j1", .2), ("j2", -.2), ("j3", .4), ("j4", -.4),
        ("j1", 0), ("j2", -.2), ("j3", .4), ("j4", 0),
        ("j1", 0), ("j2", 0), ("j3", .4), ("j4", 0)]
    assert [device.get_rotor_state()[1] for device in devices] == pytest.approx(
        [.06, -.14, .4, -.12])
    assert all(device.current_raw == 0 for device in devices)
    assert native.time_s == pytest.approx(.01)
    assert result.diagnostics == {"physics_segments": 3}


def test_reset_once_restores_native_origin_and_seeds_rotor_without_resetting_bus(monkeypatch):
    bus, host, devices, native, plant = setup(native=physics(initial_position=.25))
    native.set_joint_effort("j1", .2)
    native.step(10_000_000)
    calls = track_physics(monkeypatch, native)
    host.emit(CanFrame(0x123, data=b"borrowed", timestamp_ns=100_000_000))
    plant.reset(Session(2, 50_000_000))
    assert calls == {"reset": 1, "step": [], "effort": []}
    assert native.time_s == 0 and plant.t_sim_ns == 50_000_000
    assert all(device.t_sim_ns == 50_000_000 for device in devices)
    assert all(device.get_rotor_state() == (.5, 0) for device in devices)
    assert bus.t_sim_ns == 0 and bus.pending == 1
    plant.step(StepContext(2, 1, 51_000_000, 1_000_000))
    assert native.time_s == pytest.approx(.001)
    assert plant.t_sim_ns == 51_000_000


def test_all_target_samples_are_admitted_before_any_device_completion(monkeypatch):
    bus, _, devices, native, plant = setup()
    plant.reset(Session(1))
    getter = native.get_joint_state
    monkeypatch.setattr(native, "get_joint_state", lambda name:
                        JointState(float("nan"), 0) if name == "j2" else getter(name))
    ctx = StepContext(1, 1, 1_000_000, 1_000_000)
    with pytest.raises(ValueError, match="joint position"):
        plant.step(ctx)
    assert native.time_s == pytest.approx(.001)
    assert all(device.t_sim_ns == 0 and device.get_rotor_state() == (0, 0)
               for device in devices)
    assert bus.pending == 0 and plant.failed and plant.t_sim_ns == 0
    with pytest.raises(RuntimeError, match="successful reset"):
        plant.step(ctx)
    assert native.time_s == pytest.approx(.001)
    monkeypatch.setattr(native, "get_joint_state", getter)
    plant.reset(Session(2))
    plant.step(StepContext(2, 1, 1_000_000, 1_000_000))
    assert not plant.failed and all(device.t_sim_ns == 1_000_000 for device in devices)


def test_actual_fabric_emission_failure_freezes_plant_and_coordinator_until_reset(monkeypatch):
    bus, host, devices, native, plant = setup(bus=CanBus(max_pending=1))
    calls = track_physics(monkeypatch, native)
    coordinator = Coordinator({"joint-plant": plant})
    coordinator.reset(Session(1))
    command(bus, host, "1000 0000 0000 0000")
    host.emit(CanFrame(0x123, data=b"occupied", timestamp_ns=100_000_000))
    with pytest.raises(BufferError, match="capacity"):
        coordinator.step(1_000_000)
    assert devices[0].t_sim_ns == 1_000_000  # Commit precedes feedback admission.
    assert all(device.t_sim_ns == 0 for device in devices[1:])
    assert native.time_s == pytest.approx(.001) and bus.pending == 1
    assert plant.t_sim_ns == coordinator.t_sim_ns == 0
    assert plant.failed and coordinator.failed
    with pytest.raises(RuntimeError, match="successful reset"):
        coordinator.step(1_000_000)
    assert calls["step"] == [1_000_000]
    coordinator.reset(Session(2))
    assert bus.pending == 1  # The queue belongs to the caller, even after failure.
    assert all(device.get_rotor_state() == (0, 0) for device in devices)
    coordinator.step(500_000)  # No feedback is due before its reset phase's first1ms.
    assert not coordinator.failed and not plant.failed
    assert calls["step"] == [1_000_000, 500_000]


def test_invalid_bindings_have_no_reset_control_or_advance_effects(monkeypatch):
    bus, native = CanBus(), physics()
    devices = tuple(M3508Device(bus, i) for i in range(1, 5))
    calls = track_physics(monkeypatch, native)
    duplicate = M3508Device(bus, 1, name="duplicate-id")
    for bindings, message in (
            ({}, "nonempty"),
            ({devices[0]: "j1", devices[1]: "j1"}, "one M3508 owner"),
            ({devices[0]: "j1", duplicate: "j2"}, "unique motor IDs"),
            ({devices[0]: "missing"}, "unbound joint")):
        with pytest.raises(ValueError, match=message):
            M3508JointPlant(native, bindings)
    assert calls == {"reset": 0, "step": [], "effort": []}
    assert all(not device.closed for device in (*devices, duplicate))


def test_gear_native_methods_and_samples_are_validated_before_runtime_effects(monkeypatch):
    native = physics()
    calls = track_physics(monkeypatch, native)
    device = M3508Device(CanBus(), torque_per_amp=1)
    for gear, message in ((0, "positive"), (float("nan"), "finite"),
                          (1e308, "maximum joint effort")):
        with pytest.raises(ValueError, match=message):
            M3508JointPlant(native, {device: "j1"}, gear_ratio=gear)
    with monkeypatch.context() as patch:
        patch.setattr(native, "step", None)
        with pytest.raises(TypeError, match="requires step"):
            M3508JointPlant(native, {device: "j1"})
    with monkeypatch.context() as patch:
        patch.setattr(native, "get_joint_state", lambda _: JointState(0, float("inf")))
        with pytest.raises(ValueError, match="joint velocity"):
            M3508JointPlant(native, {device: "j1"})
    assert calls == {"reset": 0, "step": [], "effort": []}
    assert not device.closed


def test_legacy_rotor_device_is_rejected_before_native_runtime_effects(monkeypatch):
    native = physics()
    calls = track_physics(monkeypatch, native)
    legacy = M3508(CanBus())
    with pytest.raises(TypeError, match="protocol-only M3508Device"):
        M3508JointPlant(native, {legacy: "j1"})
    assert calls == {"reset": 0, "step": [], "effort": []}
    assert not legacy.closed


def test_all_device_plans_are_prepared_before_control_or_shared_advance(monkeypatch):
    bus, _, devices, native, plant = setup()
    plant.reset(Session(1))
    calls = track_physics(monkeypatch, native)
    devices[1].reset(Session(2))
    with pytest.raises(ValueError, match="session and exact target"):
        plant.step(StepContext(1, 1, 1_000_000, 1_000_000))
    assert calls == {"reset": 0, "step": [], "effort": []}
    assert bus.pending == 0 and native.time_s == 0 and plant.failed
    assert all(device.t_sim_ns == 0 for device in devices)
    plant.reset(Session(3))
    plant.step(StepContext(3, 1, 1_000_000, 1_000_000))
    assert calls["step"] == [1_000_000] and not plant.failed


def test_close_attempts_each_owned_resource_once_and_preserves_borrowed_bus(monkeypatch):
    native = native_stub()
    bus, host, devices, native, plant = setup(native=native)
    calls = track_physics(monkeypatch, native)
    plant.reset(Session(1))
    host.emit(CanFrame(0x123, data=b"borrowed", timestamp_ns=100_000_000))
    closed = []
    primary = KeyboardInterrupt("fourth close")
    errors = {4: primary, 2: OSError("second close")}
    for device in devices:
        original = device.close

        def observed_close(device=device, original=original):
            closed.append(device.motor_id)
            original()
            if device.motor_id in errors:
                raise errors[device.motor_id]

        monkeypatch.setattr(device, "close", observed_close)
    def close_physics():
        closed.append("physics")
        raise OSError("physics close")
    monkeypatch.setattr(native, "close", close_physics)
    with pytest.raises(KeyboardInterrupt) as caught:
        plant.close()
    assert caught.value is primary
    assert any("M3508 2 close failed" in note for note in primary.__notes__)
    assert any("joint physics close failed" in note for note in primary.__notes__)
    plant.close()
    assert closed == [4, 3, 2, 1, "physics"] and all(device.closed for device in devices)
    assert plant.closed and plant.failed
    assert calls == {"reset": 1, "step": [], "effort": []}
    assert bus.pending == 1 and bus.t_sim_ns == 0
    host.emit(CanFrame(0x124, data=b"live"))
    assert bus.pending == 2


def test_missing_public_physics_methods_reject_without_transferring_inputs():
    device = M3508Device(CanBus())
    for method in ("reset", "step", "set_joint_effort", "get_joint_state",
                   "validate_step", "validate_steps", "validate_joint_effort", "close"):
        native = SimpleNamespace(**{name: (None if name == method else lambda *_: None)
            for name in ("reset", "step", "set_joint_effort", "get_joint_state",
                         "validate_step", "validate_steps", "validate_joint_effort", "close")})
        with pytest.raises(TypeError, match=f"requires {method}"):
            M3508JointPlant(native, {device: "joint"})
        assert not device.closed


def test_future_native_float_precision_rejection_precedes_all_control(monkeypatch):
    bus, host, devices, native, plant = setup(timeouts=((1 << 63) - 2,) * 4)
    calls = track_physics(monkeypatch, native)
    try:
        plant.reset(Session(1))
        command(bus, host, "1000 0000 0000 0000")
        with pytest.raises(ValueError, match="advance native time"):
            plant.step(StepContext(1, 1, (1 << 63) - 1, (1 << 63) - 1))
        # Both durations were admissible at time0; the final1ns cannot advance
        # the float time after the long first interval. No torque/step may occur.
        assert calls == {"reset": 1, "step": [], "effort": []}
        assert native.time_s == 0 and bus.pending == 0 and host.frames == []
        assert plant.failed and plant.t_sim_ns == 0
        assert all(device.t_sim_ns == 0 and device.get_rotor_state() == (0, 0)
                   for device in devices)
        plant.reset(Session(2))
        plant.step(StepContext(2, 1, 1_000_000, 1_000_000))
        assert not plant.failed and native.time_s == .001
    finally:
        plant.close()
