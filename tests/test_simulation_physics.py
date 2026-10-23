"""One owned native world behind the generic Simulation input/sample boundary.

The two independent hinges have Izz=.02, no gravity or damping. Expectations
come from constant acceleration and MuJoCo's documented Euler integration;
no replacement solver or production integration helper supplies the oracle.
"""

import builtins
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Topology
from sim.physics.base import JointState
from sim.runtime import Simulation, SimulationState


ROOT = Path(__file__).resolve().parents[1]
WORLD_XML = """<mujoco>
  <option timestep="0.001" integrator="Euler" gravity="0 0 0"/>
  <worldbody>
    <body name="left"><joint name="j1" axis="0 0 1"/>
      <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/>
    </body>
    <body name="right" pos="1 0 0"><joint name="j2" axis="0 0 1"/>
      <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/>
    </body>
  </worldbody>
  <actuator><motor name="m1" joint="j1"/><motor name="m2" joint="j2"/></actuator>
</mujoco>"""


def _spec(tmp_path, **changes):
    (tmp_path / "world.xml").write_text(WORLD_XML)
    return NodeSpec("world", "physics.mujoco", {
        "model": "world.xml", "joints": {"j1": "m1", "j2": "m2"}, **changes},
        base_dir=tmp_path)


def _simulation(tmp_path, *, spec=None, factory=None, **kwargs):
    from sim.physics.factory import MujocoFactory

    return Simulation(Topology((spec or _spec(tmp_path),)),
                      {"physics.mujoco": factory or MujocoFactory()}, **kwargs)


def _world_events(simulation):
    return [event for event in simulation.events if event.kind == "physics.state"]


def _capture_physics(monkeypatch):
    from sim.physics.mujoco import MujocoPhysics

    created = []
    load = MujocoPhysics.from_xml_path

    def capture(path, **kwargs):
        physics = load(path, **kwargs)
        created.append(physics)
        return physics

    monkeypatch.setattr(MujocoPhysics, "from_xml_path", staticmethod(capture))
    return created


def test_world_description_and_input_validation_do_not_import_native_backend(tmp_path, monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "mujoco" or name.startswith("mujoco."):
            pytest.fail("pure admission imported the optional native backend")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    from sim.composition import builtin_registry

    spec = _spec(tmp_path)
    registry = builtin_registry()
    simulation = Simulation(Topology((spec,)), registry)
    description = simulation.validate()["world"]
    assert description.capabilities == {"step", "input", "sample"}
    assert description.port("effort").direction == "in"
    assert "external_input" in description.port("effort").capabilities
    assert description.port("state").direction == "out"
    assert registry[spec.kind].validate_input(
        spec, "effort", {"j1": .4}, connected_ports=()) == {"j1": .4}


@pytest.mark.parametrize("change", [
    {"model": ""}, {"model": "missing.xml"}, {"joints": {}},
    {"joints": {"": "m1"}}, {"joints": {"j1": ""}},
    {"joints": {"j1": "m1", "j2": "m1"}},
    {"joints": {"j1": True}}, {"joints": [("j1", "m1")]}, {"imus": {}},
])
def test_bad_world_configuration_rejects_before_acquisition(tmp_path, change):
    from sim.physics.factory import MujocoFactory

    class NoCreate(MujocoFactory):
        def create(self, *args):
            pytest.fail("invalid configuration reached backend acquisition")

    with pytest.raises((TypeError, ValueError)):
        _simulation(tmp_path, spec=_spec(tmp_path, **change), factory=NoCreate())


@pytest.mark.parametrize("port,protocol", [("effort", "joint-effort"), ("state", "joint-state")])
def test_world_binding_links_reject_before_acquisition_through_factory_alias(tmp_path, port, protocol):
    from sim.physics.factory import MujocoFactory

    class NoCreate(MujocoFactory):
        def create(self, *args):
            pytest.fail("unsupported binding reached native acquisition")

    class Peer:
        def describe(self, _spec):
            return NodeDescription((PortSpec("endpoint", protocol, "inout", "binding"),),
                                   {"connection"})

        def create(self, *args):
            pytest.fail("unsupported binding reached connection acquisition")

    spec = _spec(tmp_path)
    alias = NodeSpec(spec.name, "world-alias", spec.options, base_dir=spec.base_dir)
    factory = NoCreate()
    topology = Topology((alias, NodeSpec("peer", "peer")),
                        (LinkSpec(f"world.{port}", "peer.endpoint"),))
    with pytest.raises(ValueError, match="connections"):
        Simulation(topology, {"physics.mujoco": factory, "world-alias": factory, "peer": Peer()})


@pytest.mark.parametrize("payload", [
    {}, [], {"missing": .4}, {"j1": True}, {"j1": "0.4"},
    {"j1": .4, "j2": float("nan")}, {"j1": 10 ** 400},
])
def test_effort_admission_rejects_whole_batch_without_native_import(tmp_path, payload, monkeypatch):
    from sim.physics.factory import MujocoFactory

    spec = _spec(tmp_path)
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "mujoco" or name.startswith("mujoco."):
            pytest.fail("invalid effort imported native backend")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises((TypeError, ValueError)):
        MujocoFactory().validate_input(spec, "effort", payload, connected_ports=())


def test_partial_construction_closes_native_owner_and_preserves_primary(tmp_path, monkeypatch):
    import sim.physics.factory as factory_module
    from sim.physics.mujoco import MujocoPhysics

    primary, cleanup = RuntimeError("participant admission"), OSError("native cleanup")
    closed, paths = [], []

    class Acquired:
        def close(self):
            closed.append(self)
            raise cleanup

    physics = Acquired()

    def load(path, **kwargs):
        paths.append((Path(path), kwargs))
        return physics

    def reject(_physics):
        assert _physics is physics
        raise primary

    monkeypatch.setitem(sys.modules, "mujoco", SimpleNamespace())
    monkeypatch.setattr(MujocoPhysics, "from_xml_path", staticmethod(load))
    monkeypatch.setattr(factory_module, "PhysicsParticipant", reject)
    simulation = _simulation(tmp_path)
    with pytest.raises(RuntimeError) as caught:
        simulation.start()
    assert caught.value is simulation.failure is primary
    assert simulation.state is SimulationState.FAILED
    assert closed == [physics]
    assert paths == [(tmp_path / "world.xml", {"joints": {"j1": "m1", "j2": "m2"}})]
    assert any("native cleanup" in note for note in primary.__notes__)
    simulation.close()
    assert closed == [physics]


def test_real_shared_world_steps_once_for_both_joints_and_publishes_completed_samples(monkeypatch):
    mujoco = pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    from sim.composition import builtin_registry
    from sim.config.scene import load_scene

    calls = []
    native_step = mujoco.mj_step

    def counted(model, data):
        calls.append((id(model), id(data), float(data.time)))
        return native_step(model, data)

    monkeypatch.setattr(mujoco, "mj_step", counted)
    scene = load_scene(ROOT / "examples/shared-world/scene.yaml", builtin_registry())
    with Simulation(scene.topology, builtin_registry(), quantum_ns=scene.quantum_ns,
                    mode=scene.mode) as simulation:
        initial = _world_events(simulation)[0]
        simulation.schedule_input("world.effort", {"j1": .4, "j2": -.4}, at_ns=0)
        simulation.schedule_input("world.effort", {"j1": 0, "j2": 0}, at_ns=10_000_000)
        simulation.run_until(10_000_000)
        driven = _world_events(simulation)[-1]
        assert driven.payload["native_time_s"] == pytest.approx(.01, abs=1e-15)
        assert driven.payload["joints"]["j1"] == JointState(
            pytest.approx(.0011, abs=1e-12), pytest.approx(.2, abs=1e-12))
        assert driven.payload["joints"]["j2"] == JointState(
            pytest.approx(-.0011, abs=1e-12), pytest.approx(-.2, abs=1e-12))
        zero = [event for event in simulation.events if event.kind == "input.delivered"
                and event.delivery_time_ns == 10_000_000][0]
        assert driven.sequence < zero.sequence
        simulation.run_until(20_000_000)
        samples = _world_events(simulation)
        assert len(samples) == 21 and len(calls) == 20
        assert len({(model, data) for model, data, _ in calls}) == 1
        assert samples[-1].payload["joints"]["j1"] == JointState(
            pytest.approx(.0031, abs=1e-12), pytest.approx(.2, abs=1e-12))
        assert samples[-1].payload["joints"]["j2"] == JointState(
            pytest.approx(-.0031, abs=1e-12), pytest.approx(-.2, abs=1e-12))
        assert [(event.source_time_ns, event.mapped_time_ns, event.delivery_time_ns,
                 str(event.quality)) for event in samples] == [
                     (None, cut, cut, "sampled") for cut in range(0, 20_000_001, 1_000_000)]
        assert initial.payload["joints"]["j1"] == JointState(0, 0)
        with pytest.raises(TypeError):
            initial.payload["joints"]["j1"] = JointState(1, 2)
        with pytest.raises(FrozenInstanceError):
            initial.payload["joints"]["j1"].position_rad = 1


def test_off_grid_and_equal_time_inputs_preserve_causality_and_unmentioned_effort(tmp_path):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    with _simulation(tmp_path) as simulation:
        simulation.schedule_input("world.effort", {"j1": .4, "j2": -.4}, at_ns=0)
        simulation.schedule_input("world.effort", {"j1": 0}, at_ns=1_500_000)
        simulation.schedule_input("world.effort", {"j1": -.4}, at_ns=1_500_000)
        simulation.run_until(2_000_000)
        samples = _world_events(simulation)
        assert [event.mapped_time_ns for event in samples] == [0, 1_000_000, 1_500_000, 2_000_000]
        before = samples[-2].payload["joints"]
        assert before["j1"] == JointState(pytest.approx(.000035, abs=1e-12),
                                          pytest.approx(.03, abs=1e-12))
        final = samples[-1].payload["joints"]
        assert final["j1"] == JointState(pytest.approx(.000045, abs=1e-12),
                                         pytest.approx(.02, abs=1e-12))
        assert final["j2"] == JointState(pytest.approx(-.000055, abs=1e-12),
                                         pytest.approx(-.04, abs=1e-12))
        delivered = [event for event in simulation.events if event.kind == "input.delivered"
                     and event.delivery_time_ns == 1_500_000]
        assert [event.payload for event in delivered] == [{"j1": 0}, {"j1": -.4}]
        assert samples[-2].sequence < delivered[0].sequence < delivered[1].sequence


def test_invalid_scheduled_batch_keeps_healthy_world_and_reset_recreates_owner(tmp_path, monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    created = _capture_physics(monkeypatch)
    with _simulation(tmp_path) as simulation:
        original = simulation.components["world"]
        simulation.schedule_input("world.effort", {"j1": .4, "j2": -.4})
        before = simulation.events_total
        with pytest.raises((ValueError, TypeError)):
            simulation.schedule_input("world.effort", {"j1": 0, "j2": float("inf")})
        assert simulation.state is SimulationState.PAUSED and simulation.events_total == before
        simulation.run_until(1_000_000)
        assert _world_events(simulation)[-1].payload["joints"]["j1"].velocity_rad_s == pytest.approx(.02)
        simulation.reset()
        assert simulation.components["world"] is not original and len(created) == 2
        with pytest.raises(RuntimeError, match="closed"):
            created[0].get_joint_state("j1")
        assert _world_events(simulation)[-1].payload["native_time_s"] == 0
        simulation.run_until(1_000_000)
        assert _world_events(simulation)[-1].payload["joints"] == {
            "j1": JointState(0, 0), "j2": JointState(0, 0)}
    with pytest.raises(RuntimeError, match="closed"):
        created[1].get_joint_state("j1")


def test_native_binding_rejection_never_constructs_runtime_data(tmp_path, monkeypatch):
    mujoco = pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    monkeypatch.setattr(mujoco, "MjData", lambda *_args: pytest.fail("bad binding reached runtime data"))
    simulation = _simulation(tmp_path, spec=_spec(tmp_path, joints={"j1": "m2"}))
    with pytest.raises(ValueError, match="unit-gear hinge motor"):
        simulation.start()
    assert simulation.state is SimulationState.FAILED
    simulation.close()


def test_native_batch_validation_finishes_before_any_control_write(tmp_path, monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    created = _capture_physics(monkeypatch)
    primary = ValueError("second native joint validation")
    with _simulation(tmp_path) as simulation:
        physics = created[0]
        validate = physics.validate_joint_effort
        checked, writes = [], []

        def reject_second(name, value):
            checked.append(name)
            if name == "j2":
                raise primary
            return validate(name, value)

        monkeypatch.setattr(physics, "validate_joint_effort", reject_second)
        monkeypatch.setattr(physics, "set_joint_effort", lambda *args: writes.append(args))
        simulation.schedule_input("world.effort", {"j1": .4, "j2": -.4})
        with pytest.raises(ValueError) as caught:
            simulation.run_until(0)
        assert caught.value is primary and checked == ["j1", "j2"] and writes == []
        assert simulation.state is SimulationState.FAILED
        assert physics.time_s == 0


def test_partial_native_setter_failure_preserves_effect_and_never_replays(tmp_path, monkeypatch):
    pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    created = _capture_physics(monkeypatch)
    primary = OSError("second native control write")
    with _simulation(tmp_path) as simulation:
        physics = created[0]
        setter = physics.set_joint_effort
        writes = []

        def partial(name, value):
            writes.append((name, value))
            if name == "j2":
                raise primary
            return setter(name, value)

        monkeypatch.setattr(physics, "set_joint_effort", partial)
        simulation.schedule_input("world.effort", {"j1": .4, "j2": -.4})
        with pytest.raises(OSError) as caught:
            simulation.run_until(0)
        assert caught.value is primary and simulation.failure is primary
        assert simulation.state is SimulationState.FAILED
        assert writes == [("j1", .4), ("j2", -.4)] and physics.time_s == 0
        for retry in (simulation.step, simulation.reset):
            with pytest.raises(RuntimeError):
                retry()
        assert writes == [("j1", .4), ("j2", -.4)]  # No retry or zero-effort rollback.


@pytest.mark.parametrize("failure_stage", ["solver", "sample"])
def test_native_or_sample_failure_keeps_primary_closes_once_and_never_replays(
        tmp_path, monkeypatch, failure_stage):
    mujoco = pytest.importorskip("mujoco", reason="optional native MuJoCo runtime")
    created = _capture_physics(monkeypatch)
    primary, cleanup = RuntimeError(f"{failure_stage} failed"), OSError("close failed")
    calls, closes = [], []
    native_step = mujoco.mj_step

    def step(model, data):
        calls.append(float(data.time))
        native_step(model, data)
        if failure_stage == "solver":
            raise primary

    def observe(event):
        if failure_stage == "sample" and event.kind == "physics.state" and event.mapped_time_ns:
            raise primary

    monkeypatch.setattr(mujoco, "mj_step", step)
    simulation = _simulation(tmp_path, on_event=observe)
    with pytest.raises(RuntimeError) as caught:
        with simulation:
            physics = created[0]
            native_close = physics.close

            def close():
                closes.append(physics.time_s)
                native_close()
                raise cleanup

            monkeypatch.setattr(physics, "close", close)
            simulation.step()
    assert caught.value is primary
    assert calls == [0] and closes == pytest.approx([.001], abs=1e-15)
    assert simulation.failure is primary and simulation.state is SimulationState.CLOSED
    assert simulation.t_sim_ns == 0
    assert any("close failed" in note for note in primary.__notes__)
    with pytest.raises(RuntimeError):
        simulation.step()
    assert calls == [0] and closes == pytest.approx([.001], abs=1e-15)
