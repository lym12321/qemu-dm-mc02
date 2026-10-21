"""Explicit built-in factories. Concrete imports stop at this composition root."""


def builtin_registry(*, qemu=None, backend_root=None, gdb_port=None, qmp_path=None):
    from sim.fabric import CanBusFactory
    from sim.fabric.digital import DigitalNetFactory
    from sim.devices.dji.factory import M3508Factory
    from sim.machines.qemu import QemuFactory
    return {'bus.can': CanBusFactory(),
            'net.digital': DigitalNetFactory(),
            'machine.qemu': QemuFactory(qemu=qemu, backend_root=backend_root,
                                        gdb_port=gdb_port, qmp_path=qmp_path),
            'device.dji-m3508': M3508Factory()}
