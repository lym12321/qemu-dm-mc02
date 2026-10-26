"""Independent CAN motors drive one native world through typed bindings.

Literal C620 fields, the documented 3591/187 ideal gearbox, and Euler mechanics
for Izz=.02 supply expectations independently of the production plant helpers.
"""

import builtins
from pathlib import Path
import runpy

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Topology
from sim.devices.dji.factory import M3508Factory
from sim.fabric import CanFrame
from sim.fabric.factory import CanBusFactory
from sim.physics.base import JointState
from sim.physics.factory import MujocoFactory
from sim.runtime import Simulation, SimulationState


ROOT = Path(__file__).resolve().parents[1]


class Host:
    def __init__(self, context):
        self.frames = []
        self.connection = context.attach("can", self)

    def receive(self, frame):
        self.frames.append(frame)

    def input(self, port, frame):
        self.connection.send(frame)

    def close(self):
        pass


class HostFactory:
    def describe(self, spec):
        return NodeDescription((PortSpec("can", "can", min_connections=1,
                                         capabilities={"external_input"}),), {"input"})

    def validate_input(self, spec, port, payload, *, connected_ports=()):
        if port != "can" or not isinstance(payload, CanFrame):
            raise ValueError("host requires a CAN frame")
        return payload

    def create(self, spec, context):
        return Host(context)


def scene(*, changes=None, separate_buses=False, registry=None):
    options = {"model": ROOT / "examples/shared-world/world.xml",
               "joints": {"j1": "m1", "j2": "m2"}, "control": "bound"}
    nodes = [NodeSpec("world", "world", options), NodeSpec("bus", "can"),
             NodeSpec("host", "host")]
    links = [LinkSpec("host.can", "bus.can")]
    for index in (1, 2):
        motor = {"plant": "joint", "joint": f"j{index}", "motor_id": index}
        motor.update((changes or {}).get(index, {}))
        bus = "bus"
        if separate_buses and index == 2:
            bus = "other_bus"
            motor["motor_id"] = 1
            nodes.extend((NodeSpec(bus, "can"), NodeSpec("other_host", "host")))
            links.append(LinkSpec("other_host.can", f"{bus}.can"))
        nodes.append(NodeSpec(f"motor{index}", "motor" if index == 1 else "motor-alias", motor))
        links.extend((LinkSpec(f"motor{index}.can", f"{bus}.can"),
                      LinkSpec(f"motor{index}.joint", "world.drives")))
    factories = {"world": MujocoFactory(), "can": CanBusFactory(), "host": HostFactory(),
                 "motor": M3508Factory(), "motor-alias": M3508Factory()}
    factories.update(registry or {})
    return Topology(tuple(nodes), tuple(links)), factories


def command(data="1000 f800 0000 0000", timestamp=0):
    return CanFrame(0x200, data=bytes.fromhex(data), timestamp_ns=timestamp)


def states(simulation):
    return [event for event in simulation.events if event.kind == "physics.state"]


def capture_native(monkeypatch):
    from sim.physics.mujoco import MujocoPhysics

    created, calls = [], {"reset": [], "step": [], "close": [], "set_joint_effort": []}
    load = MujocoPhysics.from_xml_path

    def capture(*args, **kwargs):
        physics = load(*args, **kwargs)
        created.append(physics)
        for method in calls:
            original = getattr(physics, method)

            def observed(*values, method=method, original=original):
                calls[method].append((physics, *values))
                return original(*values)

            monkeypatch.setattr(physics, method, observed)
        return physics

    monkeypatch.setattr(MujocoPhysics, "from_xml_path", staticmethod(capture))
    return created, calls


def test_joint_topology_admission_is_pure_and_nodes_have_one_execution_owner(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "mujoco" or name.startswith("mujoco."):
            pytest.fail("topology admission loaded MuJoCo")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    topology, registry = scene()
    simulation = Simulation(topology, registry)
    descriptions = simulation.validate()
    assert descriptions["world"].capabilities == {"binding_owner", "step", "sample"}
    with pytest.raises(ValueError, match="unknown port"):
        descriptions["world"].port("effort")
    assert descriptions["motor1"].capabilities == set()
    assert descriptions["motor1"].port("joint").min_connections == 1
    assert registry["motor"].describe_joint_binding(topology.nodes[-2], "joint") == "j1"
    assert registry["motor-alias"].describe_joint_binding(topology.nodes[-1], "joint") == "j2"


@pytest.mark.parametrize("changes,error", [
    ({2: {"joint": "j1"}}, "one driver"),
    ({2: {"joint": "missing"}}, "invalid world joint"),
    ({2: {"motor_id": 1}}, "duplicate M3508 motor_id"),
    ({1: {"position_rad": 0}}, "initial state"),
    ({1: {"torque_per_amp": .1, "gear_ratio": 1e308}}, "maximum joint effort"),
])
def test_invalid_joint_graph_rejects_before_any_create(changes, error):
    class NoWorld(MujocoFactory):
        def create(self, *args):
            pytest.fail("bad topology acquired native world")

    topology, registry = scene(changes=changes, registry={"world": NoWorld()})
    with pytest.raises(ValueError, match=error):
        Simulation(topology, registry)


def test_missing_joint_link_rejects_before_acquisition():
    topology, registry = scene()
    topology = Topology(topology.nodes, tuple(link for link in topology.links
                                            if str(link.source) != "motor2.joint"))
    with pytest.raises(ValueError, match="requires at least 1"):
        Simulation(topology, registry)


def mixed_scene():
    example = runpy.run_path(str(ROOT / "examples/shared-world/mixed_drivers.py"))
    return example["mixed_scene"]()


def test_mixed_driver_topology_admits_without_loading_native_and_rejects_joint_conflict(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "mujoco" or name.startswith("mujoco."):
            pytest.fail("mixed topology admission loaded MuJoCo")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    topology, registry = mixed_scene()
    Simulation(topology, registry).validate()
    nodes = tuple(NodeSpec(node.name, node.kind, {**node.options, "joint": "j1"})
                  if node.name == "ideal" else node for node in topology.nodes)
    with pytest.raises(ValueError, match="one driver"):
        Simulation(Topology(nodes, topology.links), registry)


def test_native_mixed_drivers_share_union_expiry_reset_failure_and_cleanup(monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    created, calls = capture_native(monkeypatch)
    topology, registry = mixed_scene()
    simulation = Simulation(topology, registry, quantum_ns=10_000_000)
    with simulation:
        motor, ideal = simulation.components["motor"], simulation.components["ideal"]
        simulation.schedule_input("ideal.effort", -.4)
        result = simulation.step()
        # Izz=.02. JointA: +.2Nm for3ms. JointB: -.4Nm for7ms.
        # Native semi-implicit Euler over3/4/3ms gives these literal targets.
        final = states(simulation)[-1].payload
        assert final["joints"] == {
            "j1": JointState(pytest.approx(.00030), pytest.approx(.03)),
            "j2": JointState(pytest.approx(-.00116), pytest.approx(-.14))}
        assert final["native_time_s"] == pytest.approx(.01)
        assert motor.device.get_rotor_state() == pytest.approx((.00060, .06))
        assert ideal.driver.observation == final["joints"]["j2"]
        assert [entry[1] for entry in calls["step"]] == [3_000_000, 4_000_000, 3_000_000]
        assert [entry[1:] for entry in calls["set_joint_effort"]] == [
            ("j1", .2), ("j2", -.4), ("j1", 0), ("j2", -.4),
            ("j1", 0), ("j2", 0)]
        assert result.diagnostics["components"]["world"].diagnostics == {"physics_segments": 3}
        assert [(frame.can_id, frame.timestamp_ns, frame.data)
                for frame in simulation.components["command"].frames] == [
                    (0x201, 10_000_000, bytes.fromhex("0000 0001 0000 1900"))]
        simulation.step()
        assert states(simulation)[-1].payload["joints"] == {
            "j1": JointState(pytest.approx(.00060), pytest.approx(.03)),
            "j2": JointState(pytest.approx(-.00256), pytest.approx(-.14))}
        simulation.reset()
        assert motor.device.closed and ideal.closed
        assert len(created) == len(calls["reset"]) == 2 and len(calls["close"]) == 1
        assert states(simulation)[-1].payload["native_time_s"] == 0
        current_motor, current_ideal = simulation.components["motor"], simulation.components["ideal"]
        assert current_ideal.driver.observation == JointState(0, 0)
        assert current_motor.device.get_rotor_state() == (0, 0)
        primary = RuntimeError("ideal feedback failed after native target")

        def fail(*_):
            raise primary

        monkeypatch.setattr(current_ideal.driver, "complete_step", fail)
        simulation.schedule_input("ideal.effort", -.4)
        with pytest.raises(RuntimeError) as caught:
            simulation.step()
        assert caught.value is primary and simulation.failure is primary
        assert simulation.state is SimulationState.FAILED and simulation.t_sim_ns == 0
        assert created[-1].time_s == pytest.approx(.01)
        assert current_motor.device.t_sim_ns == 10_000_000
        assert current_ideal.driver.t_sim_ns == 0
        assert len(states(simulation)) == 4  # Initial,10ms,20ms,reset; no failed target.
        assert simulation.components["command"].frames == []
        with pytest.raises(RuntimeError):
            simulation.step()
        assert len(calls["step"]) == 7  #3segments + coast +3failed-target segments.
    assert len(calls["close"]) == 2 and current_motor.device.closed and current_ideal.closed


def test_motor_address_admission_includes_independent_rotor_modes_and_aliases():
    topology, registry = scene()
    nodes = tuple(NodeSpec(node.name, node.kind, {
        "plant": "rigid-rotor", "motor_id": 1, "position_rad": 0,
        "velocity_rad_s": 0}) if node.name == "motor2" else node for node in topology.nodes)
    links = tuple(link for link in topology.links if str(link.source) != "motor2.joint")
    with pytest.raises(ValueError, match="duplicate M3508 motor_id 1 on bus bus"):
        Simulation(Topology(nodes, links), registry)


def test_native_two_motor_world_reuses_one_executor_and_native_resource(monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    import sim.physics.factory as world_factory

    monkeypatch.setattr(world_factory, "PhysicsParticipant", lambda *_:
                        pytest.fail("bound world constructed a second execution owner"))
    created, calls = capture_native(monkeypatch)
    topology, registry = scene()
    with Simulation(topology, registry, event_history=256) as simulation:
        devices = [simulation.components[f"motor{i}"].device for i in (1, 2)]
        simulation.schedule_input("host.can", command())
        simulation.run_until(1_000_000)
        feedback = simulation.components["host"].frames
        assert [(frame.can_id, frame.data) for frame in feedback] == [
            (0x201, bytes.fromhex("0002 0012 1000 1900")),
            (0x202, bytes.fromhex("1ffe fff7 f800 1900"))]
        simulation.run_until(10_000_000)
        ratio = 3591 / 187  # DJI M3508 P19 V1.0, independent literal.
        acceleration = .1 * ratio / .02
        final = states(simulation)[-1].payload
        assert final["joints"]["j1"] == JointState(
            pytest.approx(acceleration * .001 ** 2 * 55, abs=1e-12),
            pytest.approx(acceleration * .01, abs=1e-12))
        assert final["joints"]["j2"] == JointState(
            pytest.approx(-acceleration * .001 ** 2 * 55 / 2, abs=1e-12),
            pytest.approx(-acceleration * .01 / 2, abs=1e-12))
        assert len(calls["reset"]) == 1 and len(calls["step"]) == 10
        assert len(created) == 1 and all(item[0] is created[0] for item in calls["step"])
        assert [(event.source_time_ns, event.mapped_time_ns, str(event.quality))
                for event in states(simulation)] == [
                    (None, cut, "sampled") for cut in range(0, 10_000_001, 1_000_000)]
        assert all(not device.closed for device in devices)
    assert len(calls["close"]) == 1 and all(device.closed for device in devices)


def test_native_joint_factory_admits_and_applies_actual_torque_coefficient(monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    from sim.physics.mujoco import MujocoPhysics

    checks = []
    validate = MujocoPhysics.validate_joint_effort

    def observed(physics, name, value):
        checks.append((name, value))
        return validate(physics, name, value)

    monkeypatch.setattr(MujocoPhysics, "validate_joint_effort", observed)
    topology, registry = scene(changes={1: {"torque_per_amp": .04, "gear_ratio": 2},
                                       2: {"torque_per_amp": 0}})
    with Simulation(topology, registry) as simulation:
        # Full +/-20A admission uses kt=.04 and gear2, while kt0 admits zero.
        assert checks[:6] == [("j1", 1.6), ("j1", -1.6), ("j1", 0),
                              ("j2", 0), ("j2", 0), ("j2", 0)]
        simulation.schedule_input("host.can", command())
        simulation.step()
        # raw4096=5A, .04Nm/A, gear2, Izz=.02 -> alpha20rad/s².
        assert states(simulation)[-1].payload["joints"] == {
            "j1": JointState(pytest.approx(.00002), pytest.approx(.02)),
            "j2": JointState(0, 0)}
        assert [frame.data[4:6] for frame in simulation.components["host"].frames] == [
            bytes.fromhex("1000"), bytes.fromhex("f800")]


def test_differing_command_expiries_split_one_world_at_union_only(monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    _, calls = capture_native(monkeypatch)
    topology, registry = scene(changes={1: {"command_timeout": "3ms"},
                                       2: {"command_timeout": "7ms"}})
    with Simulation(topology, registry, quantum_ns=10_000_000) as simulation:
        simulation.schedule_input("host.can", command())
        result = simulation.step()
        assert [item[1] for item in calls["step"]] == [3_000_000, 4_000_000, 3_000_000]
        assert result.diagnostics["components"]["world"].diagnostics == {"physics_segments": 3}
        acceleration = .1 * (3591 / 187) / .02
        final = states(simulation)[-1].payload["joints"]
        assert final["j1"] == JointState(pytest.approx(acceleration * .000030, abs=1e-12),
                                         pytest.approx(acceleration * .003, abs=1e-12))
        assert final["j2"] == JointState(pytest.approx(-acceleration * .000058 / 2, abs=1e-12),
                                         pytest.approx(-acceleration * .007 / 2, abs=1e-12))
        assert [frame.data[4:6] for frame in simulation.components["host"].frames] == [b"\0\0"] * 2


def test_literal_native_joint_name_and_passive_joint_need_no_extra_ports(tmp_path):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    model = tmp_path / "world.xml"
    model.write_text((ROOT / "examples/shared-world/world.xml").read_text().replace(
        '"j1"', '"left shaft"'))
    topology, registry = scene(changes={1: {"joint": "left shaft"}})
    nodes = tuple(NodeSpec(node.name, node.kind, {
        **node.options, "model": model, "joints": {"left shaft": "m1", "j2": "m2"}},
        base_dir=node.base_dir) if node.name == "world" else node
        for node in topology.nodes if node.name != "motor2")
    links = tuple(link for link in topology.links
                  if link.source.node != "motor2" and link.target.node != "motor2")
    with Simulation(Topology(nodes, links), registry) as simulation:
        with pytest.raises(ValueError, match="unknown port"):
            simulation.schedule_input("world.effort", {"j2": .4})
        assert simulation.state is SimulationState.PAUSED
        simulation.schedule_input("host.can", command())
        simulation.step()
        observed = states(simulation)[-1].payload["joints"]
        assert observed["left shaft"].velocity_rad_s > 0
        assert observed["j2"] == JointState(0, 0)


def test_two_buses_allow_same_motor_id_and_reset_recreates_all_owners(monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    created, calls = capture_native(monkeypatch)
    topology, registry = scene(separate_buses=True)
    with Simulation(topology, registry) as simulation:
        old_devices = [simulation.components[f"motor{i}"].device for i in (1, 2)]
        simulation.schedule_input("host.can", command("1000 0000 0000 0000"))
        simulation.schedule_input("other_host.can", command("f800 0000 0000 0000"))
        simulation.step()
        assert states(simulation)[-1].payload["joints"]["j1"].velocity_rad_s > 0
        assert states(simulation)[-1].payload["joints"]["j2"].velocity_rad_s < 0
        simulation.reset()
        assert len(created) == 2 and len(calls["reset"]) == 2 and len(calls["close"]) == 1
        assert all(device.closed for device in old_devices)
        assert states(simulation)[-1].payload["native_time_s"] == 0
        simulation.step()
        assert states(simulation)[-1].payload["joints"] == {
            "j1": JointState(0, 0), "j2": JointState(0, 0)}
    assert len(calls["close"]) == 2


def test_partial_native_failure_freezes_session_without_feedback_or_replay(monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    created, calls = capture_native(monkeypatch)
    topology, registry = scene()
    simulation = Simulation(topology, registry)
    with simulation:
        physics = created[0]
        original = physics.step
        primary = RuntimeError("native completed then failed")

        def partial(dt):
            original(dt)
            raise primary

        monkeypatch.setattr(physics, "step", partial)
        simulation.schedule_input("host.can", command())
        with pytest.raises(RuntimeError) as caught:
            simulation.step()
        assert caught.value is simulation.failure is primary
        assert simulation.state is SimulationState.FAILED and simulation.t_sim_ns == 0
        assert physics.time_s == pytest.approx(.001)
        assert simulation.components["host"].frames == [] and len(states(simulation)) == 1
        for retry in (simulation.step, simulation.reset):
            with pytest.raises(RuntimeError):
                retry()
        assert len(calls["step"]) == 1
    assert len(calls["close"]) == 1


@pytest.mark.parametrize("failure_stage", ["attach", "executor"])
def test_partial_motor_or_executor_construction_cleans_each_resource_once(monkeypatch, failure_stage):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    from sim.devices.dji import M3508Device
    from sim.runtime.simulation import ComponentContext

    _, calls = capture_native(monkeypatch)
    topology, registry = scene()
    primary, closed = RuntimeError(f"{failure_stage} construction"), []
    close = M3508Device.close

    def observed_close(device):
        closed.append(device.motor_id)
        close(device)

    monkeypatch.setattr(M3508Device, "close", observed_close)
    if failure_stage == "attach":
        attach = ComponentContext.attach

        def fail(context, port, endpoint):
            if port == "joint":
                raise primary
            return attach(context, port, endpoint)

        monkeypatch.setattr(ComponentContext, "attach", fail)
    else:
        import sim.physics.factory as world_factory

        def fail(*args):
            raise primary

        monkeypatch.setattr(world_factory, "JointExecution", fail)
    simulation = Simulation(topology, registry)
    with pytest.raises(RuntimeError) as caught:
        simulation.start()
    assert caught.value is simulation.failure is primary
    assert calls["reset"] == calls["step"] == [] and len(calls["close"]) == 1
    assert closed == ([1] if failure_stage == "attach" else [2, 1])
    assert simulation.state is SimulationState.FAILED
    simulation.close()
    assert len(calls["close"]) == 1
