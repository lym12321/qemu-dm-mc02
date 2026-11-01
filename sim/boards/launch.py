"""DM-MC02 launch composition; generic process admission lives in QEMU."""

from sim.machines.qemu.launch import native_qemu_command

UARTS = ("uart1", "uart2", "uart3", "uart5", "uart7", "uart10")
CANS = ("can1", "can2", "can3")


def qemu_command(firmware, qemu, sockets, *, qmp_path=None, gdb_port=None):
    """The shared DM-MC02 launcher, using its profile's native slot map."""
    from sim.machines.qemu.profile import DmMc02Profile

    return native_qemu_command(qemu, sockets,
        machine_arguments=DmMc02Profile.machine_arguments(firmware),
        serial_slots=DmMc02Profile.serial_slots, qmp_path=qmp_path, gdb_port=gdb_port)
