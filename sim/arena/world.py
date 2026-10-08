"""Acquire the admitted two-robot profile as one native joint plant.

The caller owns the returned plant until a runtime accepts it. Its public
devices retain one CAN bus per manifest member, in the admitted member/motor
order; IMU names are the member prefixes without their trailing slash.
Neither construction nor observation advances or resets the world.
"""

from sim.devices.bmi088 import imu_to_bmi088_units
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.physics import MujocoPhysics

from .manifest import ArenaManifest


def create_arena_plant(manifest: ArenaManifest) -> M3508JointPlant:
    """Compile complete native bindings before any transport or process IO.

    Consume an already admitted Arena manifest; the existing native adapter
    and joint plant independently validate the named bindings and observations.
    Successful return transfers exclusive physics/device execution ownership.
    """
    if not isinstance(manifest, ArenaManifest):
        raise TypeError("Arena world requires an admitted ArenaManifest")
    joints = {
        member.prefix + motor.joint:
            member.prefix + "drive_" + motor.joint.removeprefix("wheel_")
        for member in manifest.robots for motor in member.robot.devices}
    imus = {member.prefix[:-1]: (member.prefix + "imu_gyro", member.prefix + "imu_accel")
            for member in manifest.robots}
    physics = MujocoPhysics.from_xml_path(manifest.world.model, joints=joints, imus=imus)
    devices, bindings = [], {}
    try:
        # Both complete finite samples precede even the first CAN attachment.
        for name in imus:
            imu_to_bmi088_units(physics.get_imu(name))
        for member in manifest.robots:
            bus = CanBus()
            for motor in member.robot.devices:
                device = M3508Device(bus, motor.motor_id)
                devices.append(device)
                bindings[device] = member.prefix + motor.joint
        return M3508JointPlant(physics, bindings)
    except BaseException as exc:
        for device in reversed(devices):
            try:
                device.close()
            except BaseException as error:
                exc.add_note(f"Arena device close failed: {error}")
        try:
            physics.close()
        except BaseException as error:
            exc.add_note(f"Arena physics close failed: {error}")
        raise
