"""Fixed toy chassis: IDs 1..4 are FL, FR, RL, RR on one shared plant.

The launcher owns the admitted plant until a runtime is successfully returned.
The runtime then owns its reset, advance and physics/device cleanup; transports remain
borrowed. Native IMU observations use SI and the completed physics state.
"""

from sim.devices.bmi088 import imu_to_bmi088_units
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.fabric.endpoints import QemuCanEndpoint, SocketCanEndpoint
from sim.fabric.runtime import CanRuntime
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


class FourWheelRuntime(CanRuntime):
    def __init__(self, plant: M3508JointPlant, can, socketcan_tx=None):
        if not isinstance(plant, M3508JointPlant) or plant.closed:
            raise TypeError("four-wheel runtime requires an open joint plant")
        if tuple(device.motor_id for device in plant.devices) != (1, 2, 3, 4):
            raise ValueError("four-wheel runtime requires IDs 1, 2, 3, 4 in order")
        imu_to_bmi088_units(plant.physics.get_imu("imu"))
        bus = plant.devices[0].bus
        endpoint = QemuCanEndpoint(bus, can)
        socket_endpoint = (SocketCanEndpoint(bus, socketcan_tx)
                           if socketcan_tx is not None else None)
        super().__init__(bus, endpoint, [plant], socket_endpoint)
        self.plant = plant

    def get_imu(self) -> ImuSample:
        if (self.coordinator.closed or self.coordinator.failed
                or self.coordinator.session is None):
            raise RuntimeError("four-wheel IMU requires a successful runtime reset/step")
        return self.plant.physics.get_imu("imu")
