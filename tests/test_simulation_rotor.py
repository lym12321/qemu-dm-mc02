"""Actual rigid-rotor factory over the unified runtime and existing CAN owner.

The closed forms below independently solve the declared 1 ms Euler recurrence
with J=.02, damping=.08 and kt=.02. They are approximation regression oracles,
not physical calibration. C620 literal raw4096 is5 A (DJI C620 V1.01).
"""

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, StepResult, Topology
from sim.composition import builtin_registry
from sim.devices.dji import M3508
from sim.fabric import CanFrame
from sim.runtime import Simulation, SimulationState


class CommandSource:
    def __init__(self, spec, context):
        self.connection = context.attach("can", self)
        self.feedback = []

    def prepare_step(self, context):
        pass

    def request_step(self, context):
        pass

    def finish_step(self, context):
        if context.step_id in (1, 11):
            command = bytes.fromhex("1000 0000 0000 0000") if context.step_id == 1 else bytes(8)
            self.connection.send(CanFrame(0x200, data=command,
                timestamp_ns=context.t_sim_ns - context.dt_ns + 250_000))
        return StepResult("ok", context.t_sim_ns)

    def receive(self, frame):
        self.feedback.append(frame)

    def close(self):
        pass


class SourceFactory:
    def describe(self, spec):
        return NodeDescription((PortSpec("can", "can", min_connections=1),), {"machine"})

    def create(self, spec, context):
        return CommandSource(spec, context)


@pytest.mark.parametrize("node_order", [("host", "motor", "bus"), ("motor", "bus", "host")])
def test_rigid_rotor_reuses_m3508_and_cut_delivered_commands_affect_next_interval(node_order):
    nodes = {"host": NodeSpec("host", "source"), "bus": NodeSpec("bus", "bus.can"),
             "motor": NodeSpec("motor", "device.dji-m3508", {"motor_id": 1,
                 "plant": "rigid-rotor", "position_rad": 0, "velocity_rad_s": 0})}
    topology = Topology(tuple(nodes[name] for name in node_order),
                        (LinkSpec("host.can", "bus.can"), LinkSpec("motor.can", "bus.can")))
    registry = {**builtin_registry(), "source": SourceFactory()}
    with Simulation(topology, registry) as simulation:
        motor = simulation.components["motor"]
        assert type(motor) is M3508
        simulation.run_until(1_000_000)
        assert motor.get_rotor_state() == (0, 0)
        assert motor.current_raw == 4096
        simulation.run_until(11_000_000)
        velocity = 1.25 * (1 - .996 ** 10)
        position = .001 * 1.25 * (10 - .996 * (1 - .996 ** 10) / .004)
        assert motor.get_rotor_state() == pytest.approx((position, velocity), abs=1e-14)
        assert motor.current_raw == 0
        simulation.run_until(21_000_000)
        coast_velocity = velocity * .996 ** 10
        coast_position = position + .001 * velocity * .996 * (1 - .996 ** 10) / .004
        assert motor.get_rotor_state() == pytest.approx((coast_position, coast_velocity), abs=1e-14)
        deliveries = [event for event in simulation.events if event.kind == "can.delivery"
                      and event.source_node == "host"]
        assert [(event.source_time_ns, event.delivery_time_ns) for event in deliveries] == [
            (250_000, 1_000_000), (10_250_000, 11_000_000)]
        assert all(frame.can_id == 0x201 for frame in simulation.components["host"].feedback)
        simulation.reset()
        assert motor.closed
        replacement = simulation.components["motor"]
        assert type(replacement) is M3508 and replacement is not motor
        assert replacement.get_rotor_state() == (0, 0)
        assert replacement.current_raw == 0
        simulation.run_until(2_000_000)
        assert replacement.get_rotor_state() == pytest.approx((.000005, .005))
    assert motor.closed
    assert replacement.closed


@pytest.mark.parametrize("change", [{"plant": "implicit"}, {"position_rad": True},
                                    {"position_rad": 10 ** 400},
                                    {"velocity_rad_s": float("inf")}, {"temperature_c": 256},
                                    {"command_timeout": "0ms"}, {"feedback_period": "0ms"},
                                    {"inertia": .02}, {"torque_per_amp": True},
                                    {"torque_per_amp": -.01},
                                    {"torque_per_amp": float("nan")},
                                    {"torque_per_amp": 10 ** 400},
                                    {"torque_per_amp": 1e308}])
def test_rigid_configuration_refuses_invalid_or_unexposed_options_before_create(change):
    from sim.devices.dji.factory import M3508Factory

    class NoCreate(M3508Factory):
        def create(self, *args):
            pytest.fail("invalid options reached resource creation")

    options = {"motor_id": 1, "plant": "rigid-rotor", "position_rad": 0,
               "velocity_rad_s": 0, **change}
    topology = Topology((NodeSpec("motor", "motor", options), NodeSpec("bus", "bus.can")),
                        (LinkSpec("motor.can", "bus.can"),))
    with pytest.raises(ValueError):
        Simulation(topology, {**builtin_registry(), "motor": NoCreate()})


@pytest.mark.parametrize("torque_per_amp,expected", [(0, (0, 0)), (.04, (.00001, .01))])
def test_rigid_factory_applies_configured_torque_to_actual_rotor(torque_per_amp, expected):
    topology = Topology((NodeSpec("host", "source"), NodeSpec("bus", "bus.can"),
                         NodeSpec("motor", "device.dji-m3508", {
                             "motor_id": 1, "plant": "rigid-rotor", "position_rad": 0,
                             "velocity_rad_s": 0, "torque_per_amp": torque_per_amp})),
                        (LinkSpec("host.can", "bus.can"), LinkSpec("motor.can", "bus.can")))
    with Simulation(topology, {**builtin_registry(), "source": SourceFactory()}) as simulation:
        simulation.run_until(2_000_000)
        motor = simulation.components["motor"]
        assert motor.current_raw == 4096  # C620 raw4096 =5A, including zero kt.
        assert motor.get_rotor_state() == pytest.approx(expected, abs=1e-14)


def test_rigid_feedback_failure_keeps_completed_plant_and_freezes_without_retry():
    failure = RuntimeError("feedback observation failed")

    def observe(event):
        if (event.source_node == "motor" and event.kind == "can.sent"
                and event.mapped_time_ns == 2_000_000):
            raise failure

    topology = Topology((NodeSpec("host", "source"), NodeSpec("bus", "bus.can"),
                         NodeSpec("motor", "device.dji-m3508", {"motor_id": 1,
                             "plant": "rigid-rotor", "position_rad": 0,
                             "velocity_rad_s": 0})),
                        (LinkSpec("host.can", "bus.can"), LinkSpec("motor.can", "bus.can")))
    with Simulation(topology, {**builtin_registry(), "source": SourceFactory()},
                    on_event=observe) as simulation:
        simulation.step()
        motor, bus = simulation.components["motor"], simulation.components["bus"]
        with pytest.raises(RuntimeError) as caught:
            simulation.step()
        assert caught.value is simulation.failure is failure
        assert simulation.state is SimulationState.FAILED
        # Physical work and protocol commit precede the failing observation;
        # Simulation's published cut remains the last wholly successful one.
        assert simulation.t_sim_ns == 1_000_000 and motor.t_sim_ns == 2_000_000
        assert motor.get_rotor_state() == pytest.approx((.000005, .005))
        assert bus.pending == 1
        for retry in (simulation.step, simulation.reset):
            with pytest.raises(RuntimeError, match="healthy Simulation"):
                retry()
        assert motor.get_rotor_state() == pytest.approx((.000005, .005))
        assert bus.pending == 1
        motor.close()
        assert bus.pending == 1  # Device cleanup does not close its borrowed bus.
    assert motor.closed and bus.pending == 0
