"""Direct native terminal consumer, without Simulation or a Python QMP client."""

import os
import subprocess
import sys

from sim.api import BackendUnavailable
from sim.machines.qemu.launch import (PROJECT_ROOT, qmp_socket_path, regular_file,
                                      resolve_qemu, terminal_qemu_command)
from sim.machines.qemu.profile import DmMc02Profile


def run_firmware(firmware, *, qemu=None, uart='uart1', debug=False, gdb_port=None,
                 machine='dm-mc02', qmp_path=None, cosim_path=None):
    profile = DmMc02Profile()
    if uart not in (*profile.terminal_uarts, 'none'):
        raise ValueError('unsupported terminal UART')
    if gdb_port is not None and not debug:
        raise ValueError('--gdb-port requires --debug')
    if gdb_port is not None and (type(gdb_port) is not int or not 1 <= gdb_port <= 65535):
        raise ValueError('GDB port must be in 1..65535')
    firmware = regular_file(firmware, 'firmware ELF')
    arguments = profile.terminal_machine_arguments(firmware, machine)
    qmp_path, cosim_path = qmp_socket_path(qmp_path), qmp_socket_path(cosim_path)
    if qmp_path is not None and qmp_path == cosim_path:
        raise ValueError('QMP and co-sim socket paths must differ')
    try:
        binary = resolve_qemu(qemu)
    except ValueError as error:
        advice = ('Build the project QEMU with bash tools/build-qemu.sh; '
                  if (PROJECT_ROOT / 'qemu.lock').is_file() else '')
        raise BackendUnavailable(
            f'{error}\n{advice}select a compatible binary with --qemu PATH or QEMU_SYSTEM_ARM.') from error
    try:
        probe = subprocess.run([str(binary), '-machine', 'help'],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackendUnavailable(f'cannot query QEMU machine support: {binary}: {error}') from error
    if probe.returncode:
        raise BackendUnavailable(
            f'QEMU machine query failed (exit {probe.returncode}): {binary}\n{probe.stderr.strip()}')
    if not any(line.split()[:1] == ['dm-mc02'] for line in probe.stdout.splitlines()):
        raise BackendUnavailable(
            f'{binary} does not provide the dm-mc02 machine; use the project QEMU with --qemu PATH.')
    port = (gdb_port or 1234) if debug else None
    command = terminal_qemu_command(binary, machine_arguments=arguments,
        serial_slots=profile.serial_slots, uart=None if uart == 'none' else uart,
        sockets={} if cosim_path is None else {'cosim': cosim_path},
        qmp_path=qmp_path, gdb_port=port)
    print(f'QEMU: {binary}\nELF: {firmware}\nMachine: {machine}\nUART: {uart}', file=sys.stderr)
    print('Ctrl-a c: switch UART/monitor; Ctrl-a x: quit; Ctrl-a h: help.'
          if uart != 'none' else 'QEMU monitor: type quit to exit.', file=sys.stderr)
    if cosim_path is not None:
        print(f'Co-sim socket: {cosim_path} (external worker required)', file=sys.stderr)
    if qmp_path is not None:
        print(f'QMP socket: {qmp_path}', file=sys.stderr)
    if debug:
        print(f'Paused at reset. In GDB: target remote 127.0.0.1:{port}', file=sys.stderr)
    sys.stderr.flush()
    if sys.platform == 'win32':
        # Windows CRT exec ends this process without preserving its caller's
        # wait handle. Keep the console caller waiting for native QEMU's exit.
        raise SystemExit(subprocess.call(command))
    os.execv(str(binary), command)
