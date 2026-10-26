"""Fixed toy chassis: IDs 1..4 are FL, FR, RL, RR on one shared plant.

The worker recipe preflights and closes native resources before connection.
Each acknowledged session acquires a new Simulation-owned world and devices;
transports remain borrowed. Native observations use completed SI state.
"""

from sim.devices.bmi088 import imu_to_bmi088_units
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.physics import ImuSample, MujocoPhysics


def create_four_wheel_plant(model_path, *, delay_ns=0,
                            command_timeout_ns=100_000_000,
                            torque_per_amp=.02) -> M3508JointPlant:
    """Admit the entire fixed profile before acquiring any transport."""
    corners = ("fl", "fr", "rl", "rr")
    physics = MujocoPhysics.from_xml_path(
        model_path, joints={f"wheel_{c}": f"drive_{c}" for c in corners},
        imus={"imu": ("imu_gyro", "imu_accel")})
    devices = []
    try:
        imu_to_bmi088_units(physics.get_imu("imu"))
        bus = CanBus(delay_ns=delay_ns)
        for motor_id in range(1, 5):
            devices.append(M3508Device(
                bus, motor_id, command_timeout_ns=command_timeout_ns,
                torque_per_amp=torque_per_amp))
        return M3508JointPlant(physics, dict(zip(
            devices, (f"wheel_{c}" for c in corners))))
    except BaseException as exc:
        for device in reversed(devices):
            try:
                device.close()
            except BaseException as error:
                exc.add_note(f"profile device close failed: {error}")
        try:
            physics.close()
        except BaseException as error:
            exc.add_note(f"profile physics close failed: {error}")
        raise


def create_four_wheel_simulation(session, mapper, stream, datagrams=None, *,
                                 model_path, delay_ns=0,
                                 command_timeout_ns=100_000_000,
                                 torque_per_amp=.02, quantum_ns=1_000_000,
                                 on_event=None):
    """Build a fresh graph; the returned Simulation acquires its sole solver."""
    from sim.api import NodeSpec
    from sim.apps.can_worker import motor_nodes, can_topology, create_can_simulation

    joints = tuple(f"wheel_{corner}" for corner in ("fl", "fr", "rl", "rr"))
    motors = motor_nodes(range(1, 5), command_timeout_ns=command_timeout_ns,
                         torque_per_amp=torque_per_amp, joints=joints)
    world = NodeSpec("world", "physics.mujoco", dict(
        model=str(model_path), control="bound",
        joints={joint: joint.replace("wheel_", "drive_") for joint in joints},
        imus={"imu": ("imu_gyro", "imu_accel")}))
    topology = can_topology(motors, delay_ns=delay_ns,
                            socketcan=datagrams is not None, world=world)
    return create_can_simulation(session, mapper, stream, datagrams,
                                 topology=topology, quantum_ns=quantum_ns,
                                 on_event=on_event)


def four_wheel_simulation_factory(model_path, **options):
    """Native admission completes and releases its solver before connection.

    The fixed profile is one self-contained MJCF file. Reject includes/assets:
    a single-file digest cannot attest mutable external model dependencies.
    """
    import hashlib
    from pathlib import Path
    import xml.etree.ElementTree as ET

    path = Path(model_path).resolve()
    from sim.apps.can_worker import motor_nodes, can_topology
    motors = motor_nodes(range(1, 5),
        command_timeout_ns=options.get("command_timeout_ns", 100_000_000),
        torque_per_amp=options.get("torque_per_amp", .02),
        joints=tuple(f"wheel_{corner}" for corner in ("fl", "fr", "rl", "rr")))
    can_topology(motors, delay_ns=options.get("delay_ns", 0))
    data = path.read_bytes()
    model = ET.fromstring(data)
    if model.find(".//include") is not None or model.find(".//asset") is not None:
        raise ValueError("four-wheel worker requires self-contained MJCF without includes/assets")
    admitted_hash = hashlib.sha256(data).digest()
    plant = create_four_wheel_plant(path, **options)
    bus = plant.devices[0].bus
    try:
        plant.close()
    except BaseException as primary:
        try:
            bus.close()
        except BaseException as error:
            primary.add_note(f"preflight CAN cleanup failed: {error}")
        raise
    bus.close()

    def acquire(session, mapper, stream, datagrams):
        if hashlib.sha256(path.read_bytes()).digest() != admitted_hash:
            raise ValueError("four-wheel model changed after native preflight")
        return create_four_wheel_simulation(session, mapper, stream, datagrams,
                                             model_path=path, **options)
    return acquire


def four_wheel_imu(simulation) -> ImuSample:
    return simulation.components["world"].get_imu("imu")
