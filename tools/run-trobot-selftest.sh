#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
elf=${1:-"$root_dir/../trobot/build/SelfTestRelease/trobot.elf"}
report=${2:-"$root_dir/build/test-results/trobot-selftest.json"}
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
[[ -f "$elf" && -x "$qemu_bin" ]] || {
    printf 'Missing test ELF or QEMU: %s, %s\n' "$elf" "$qemu_bin" >&2
    exit 1
}
export PYTHONPATH="$script_dir${PYTHONPATH:+:$PYTHONPATH}"

args=()
for name in qmp cosim uart1 uart2 uart3 uart5 uart7 uart10 can1 can2 can3; do
    args+=(--socket "$name" --python-arg "{$name}")
done
python3 "$script_dir/dm_mc02_test_harness.py" --timeout 60 \
    "${args[@]}" --python-arg "$elf" --python-arg "$qemu_bin" --python-arg "$report" -- \
    "$qemu_bin" -machine dm-mc02,fdcan-host-ack=on,iwdg-boot-grace-ms=0 \
    -kernel "$elf" -nodefaults -display none -monitor none -S \
    -qmp 'unix:{qmp},server=on,wait=off' \
    -chardev 'socket,id=cosim,path={cosim},server=on,wait=off' -serial chardev:cosim \
    -chardev 'socket,id=uart1,path={uart1},server=on,wait=off' -serial chardev:uart1 \
    -chardev 'socket,id=uart2,path={uart2},server=on,wait=off' -serial chardev:uart2 \
    -chardev 'socket,id=uart3,path={uart3},server=on,wait=off' -serial chardev:uart3 \
    -chardev 'socket,id=uart5,path={uart5},server=on,wait=off' -serial chardev:uart5 \
    -chardev 'socket,id=uart7,path={uart7},server=on,wait=off' -serial chardev:uart7 \
    -chardev 'socket,id=uart10,path={uart10},server=on,wait=off' -serial chardev:uart10 \
    -chardev 'socket,id=can1,path={can1},server=on,wait=off' -serial chardev:can1 \
    -chardev 'socket,id=can2,path={can2},server=on,wait=off' -serial chardev:can2 \
    -chardev 'socket,id=can3,path={can3},server=on,wait=off' -serial chardev:can3 <<'PY'
from collections import Counter
import hashlib
import json
from pathlib import Path
import select
import socket
import struct
import sys
import time

from dm_mc02_qmp import QmpSession
from dm_mc02_sim_worker import (RESET, TELEMETRY, CanStream, FramedSocket, adc_voltage_frame,
                                 imu_frame, protocol_frame, QemuTimestampMapper)
from sim.fabric import CanBus, CanFrame, VirtualCanDevice
from sim.machines.qemu.endpoints import QemuCanEndpoint

names = ['cosim', 'uart1', 'uart2', 'uart3', 'uart5', 'uart7', 'uart10', 'can1', 'can2', 'can3']
qmp_path, *values = sys.argv[1:]
paths, elf, qemu_bin, report_path = values[:10], *values[10:]
sockets = {}
qmp = None
rows = []
phases = []
error = None
last_stage = 'startup'
required = {
    'bsp.init', 'clock.sysclk', 'clock.hclk', 'clock.pclk1', 'clock.pclk2',
    'rtos.tick', 'hal.tick', 'gpio.output', 'gpio.key', 'timer.irq', 'pwm.buzzer', 'pwm.stop',
    'led.rgb', 'led.off',
    'crc.vector', 'crc.split', 'cordic.cos', 'cordic.sin', 'rng.range', 'rng.refill',
    'flash.external', 'flash.internal', 'reset.poweron', 'reset.software', 'reset.iwdg',
    'task.stack', 'control.rx', 'imu.1', 'imu.2',
    'adc.1.key', 'adc.1.vbus_mv', 'adc.2.key', 'adc.2.vbus_mv',
    *('uart.' + n for n in ('1', '2', '3', '5', '7', '10')),
    *(f'can.{n}.{length}' for n in (1, 2, 3) for length in (8, 64)),
}
allowed_skips = {'uart.5.tx', 'usb.enumeration', 'plant.physics', 'cordic.two_args'}
try:
    for name, path in zip(names, paths):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sockets[name] = sock
        sock.settimeout(2)
        sock.connect(path)
        sock.setblocking(False)
    qmp = QmpSession(qmp_path, timeout=2)
    cosim = FramedSocket(sockets['cosim'])
    cans = {name: CanStream(sockets[name]) for name in names if name.startswith('can')}
    buses = {name: CanBus() for name in cans}
    endpoints = {name: QemuCanEndpoint(buses[name], stream) for name, stream in cans.items()}
    for endpoint in endpoints.values():
        endpoint.bind_mapper(QemuTimestampMapper(0, 0))

    class Echo(VirtualCanDevice):
        def __init__(self, bus, number):
            self.number = number
            self.exchanges = 0
            super().__init__(bus, 'echo')

        def receive(self, frame):
            self.exchanges += 1
            self.emit(CanFrame(0x320 + self.number, frame.flags, frame.dlc,
                               bytes(i ^ 0x5a for i in range(frame.length)),
                               frame.timestamp_ns))

    fabric_echoes = {name: Echo(bus, int(name[-1]) - 1) for name, bus in buses.items()}
    buffers = {name: bytearray() for name in names if name.startswith('uart')}
    echoes = {}
    echo_seen = set()
    pending_ack = None
    led_expected = None
    can_expected = {}
    can_received = {}
    seq, stamp = 0, 0
    imu_profile = None
    software_requested = watchdog_requested = False
    reset_software_seen = reset_iwdg_seen = False

    def ack():
        sockets['uart1'].sendall(b'\xa5')

    def get(prop):
        return qmp.command('qom-get', {'path': '/machine', 'property': prop})

    def set_prop(prop, value):
        qmp.command('qom-set', {'path': '/machine', 'property': prop, 'value': value})

    def next_time():
        global seq, stamp
        seq += 1
        # A deterministic 100 Hz input clock crosses the configured BMI088 ODR;
        # unchanged LED telemetry is suppressed and cannot advance this clock.
        stamp += 10_000_000
        return seq, stamp

    def ready(name, actual, expected):
        global last_stage, pending_ack, led_expected, imu_profile, software_requested, watchdog_requested
        last_stage = name
        if name.startswith('gpio.key.'):
            set_prop('user-key', name.endswith('.low'))
            ack()
        elif name.startswith('led.'):
            led_expected = (actual, expected)
        elif name == 'pwm.buzzer':
            if not get('buzzer-enabled') or (int(get('buzzer-frequency-hz')), int(get('buzzer-duty-permille'))) != (actual, expected):
                raise RuntimeError('PWM public output differs from firmware configuration')
            ack()
        elif name.startswith('adc.'):
            voltages = (1650000, 825000) if name == 'adc.1' else (825000, 2475000)
            for channel, voltage in zip((4, 19), voltages):
                cosim.send(adc_voltage_frame(*next_time(), channel, voltage))
            cosim.drain()
            ack()
        elif name.startswith('imu.'):
            imu_profile = ((10, -20, 30), (.25, -.5, 1)) if name == 'imu.1' else ((-40, 50, -60), (-.75, .125, .5))
            set_prop('imu-temperature-c', '25' if name == 'imu.1' else '-5')
            cosim.send(imu_frame(*next_time(), *imu_profile))
            cosim.drain()
            ack()
        elif name.startswith('uart.'):
            number = name.split('.')[1]
            port = 'uart' + number
            if name.endswith('.tx'):
                if port in echo_seen:
                    ack()
                else:
                    pending_ack = port
            else:
                device = ('1', '2', '3', '5', '7', '10').index(number)
                payload = bytes((0, 127, 128, 255, 85, 170, device, 49))
                if number != '5':
                    echoes[port] = payload
                sockets[port].sendall(payload)
        elif name.startswith('can.'):
            _, number, length = name.split('.')
            can_expected['can' + number] = int(length)
        elif name == 'flash.internal':
            ack()
        elif name == 'reset.software':
            if software_requested:
                raise RuntimeError('unexpected repeated software reset stage')
            software_requested = True
            imu_profile = None
            ack()
        elif name == 'reset.watchdog':
            if not reset_software_seen or watchdog_requested:
                raise RuntimeError('watchdog stage precedes software reset evidence')
            watchdog_requested = True
            ack()
        else:
            raise RuntimeError('unknown READY stage ' + name)

    cosim.send(protocol_frame(RESET, 0, 0))
    qmp.command('cont')
    deadline = time.monotonic() + 50
    done = False
    while time.monotonic() < deadline and not done:
        readable, _, _ = select.select(list(sockets.values()), [], [], .01)
        # Parse control output first, so a simultaneous CAN frame has its admission context.
        for port in ['uart1', *[n for n in names if n != 'uart1']]:
            sock = sockets[port]
            if sock not in readable:
                continue
            if port == 'cosim':
                for kind, _, virtual_ns, payload in cosim.receive():
                    if kind == RESET:
                        seq, stamp = 0, 0
                        for bus in buses.values():
                            bus.reset()
                        cosim.send(protocol_frame(RESET, 0, 0))
                    else:
                        stamp = max(stamp, virtual_ns)
                    if kind == TELEMETRY and led_expected is not None:
                        rgb, brightness, _, _, _ = struct.unpack('<IIBBH', payload)
                        if (rgb, brightness) == led_expected:
                            led_expected = None
                            ack()
            elif port.startswith('can'):
                frames = cans[port].receive()
                if len(frames) > 1 or (frames and port in can_received):
                    raise RuntimeError(f'{port} unexpected extra CAN frame: pending={can_received.get(port)!r}, received={frames!r}')
                if frames:
                    can_received[port] = frames[0]
            else:
                chunk = sock.recv(4096)
                if not chunk:
                    raise RuntimeError(port + ' disconnected')
                buf = buffers[port]
                buf.extend(chunk)
                if len(buf) > 8192:
                    raise RuntimeError(port + ' unexpected output exceeds limit')
                while buf:
                    if port in echoes and buf[0] == 0:
                        if len(buf) < 8:
                            break
                        if bytes(buf[:8]) != echoes.pop(port):
                            raise RuntimeError(port + ' asynchronous TX byte mismatch')
                        del buf[:8]
                        echo_seen.add(port)
                        if pending_ack == port:
                            pending_ack = None
                            ack()
                        continue
                    if port != 'uart1':
                        raise RuntimeError(port + ' unexpected TX bytes')
                    if b'\n' not in buf:
                        break
                    line, _, rest = buf.partition(b'\n')
                    buf[:] = rest
                    line = line.decode('ascii').strip()
                    if not line.startswith('TTEST '):
                        continue  # Existing EasyFlash may print informational startup messages.
                    print(line, flush=True)
                    fields = line.split()
                    if fields[1] == 'PHASE':
                        if len(fields) != 5:
                            raise RuntimeError('invalid phase summary')
                        summary = dict(zip(('passed', 'failed', 'skipped'), map(int, fields[2:])))
                        phases.append(summary)
                        if min(summary.values()) < 0 or summary['failed'] != 0:
                            raise RuntimeError('firmware phase failed: ' + str(summary))
                        continue
                    if fields[1] == 'DONE':
                        if (len(fields) != 5 or len(phases) != 2 or
                                dict(zip(('passed', 'failed', 'skipped'), map(int, fields[2:]))) != phases[-1] or
                                not reset_iwdg_seen):
                            raise RuntimeError('invalid final completion or reset sequence')
                        done = True
                        break
                    if len(fields) != 5 or fields[1] not in ('PASS', 'FAIL', 'SKIP', 'READY'):
                        raise RuntimeError('invalid test report: ' + line)
                    status, name, actual, expected = fields[1], fields[2], int(fields[3]), int(fields[4])
                    rows.append({'status': status, 'name': name, 'actual': actual, 'expected': expected})
                    if status == 'READY':
                        ready(name, actual, expected)
                    elif name == 'pwm.stop' and int(get('buzzer-duty-permille')) != 0:
                        raise RuntimeError('PWM stop failed at public board output')
                    elif name == 'reset.software':
                        reset_software_seen = status == 'PASS' and software_requested
                    elif name == 'reset.iwdg':
                        reset_iwdg_seen = status == 'PASS' and watchdog_requested
        # Separate sockets may deliver CAN before the complete UART READY line.
        for port in can_received.keys() & can_expected.keys():
            ident, flags, dlc, timestamp, payload = can_received.pop(port)
            length = can_expected.pop(port)
            device = int(port[-1]) - 1
            expected_data = bytes(i ^ 0xa5 for i in range(length))
            if (ident, flags, dlc, payload) != (0x120 + device, 0 if length == 8 else 12, 8 if length == 8 else 15, expected_data):
                raise RuntimeError(f'{port} TX mismatch: {(ident, flags, dlc, payload)!r}')
            endpoints[port].ingest(CanFrame(ident, flags, dlc, payload, timestamp), timestamp)
            buses[port].step(max(buses[port].t_sim_ns, timestamp))
            cans[port].drain()
        if imu_profile is not None:
            cosim.send(imu_frame(*next_time(), *imu_profile))
    if not done:
        raise TimeoutError('firmware did not finish; last stage ' + last_stage)
    counts = Counter(r['name'] for r in rows if r['status'] == 'PASS')
    if any(echo.exchanges != 2 for echo in fabric_echoes.values()):
        raise RuntimeError('CAN Fabric did not complete classic and FD exchanges on every port')
    missing = required - counts.keys()
    failed = [r for r in rows if r['status'] == 'FAIL' or (r['status'] == 'SKIP' and r['name'] not in allowed_skips)]
    repeated = {n: c for n, c in counts.items() if c != (3 if n == 'control.rx' else 2 if n == 'task.stack' else 1)}
    skips = Counter(r['name'] for r in rows if r['status'] == 'SKIP')
    if (missing or failed or repeated or counts.keys() - required or
            skips != Counter({name: 1 for name in allowed_skips}) or
            can_received or can_expected or echoes or pending_ack or
            echo_seen != {'uart1', 'uart2', 'uart3', 'uart7', 'uart10'}):
        raise RuntimeError(f'coverage failed: missing={sorted(missing)}, failed={failed}, repeated={repeated}, echoes={sorted(echo_seen)}')
    diagnostics = dict(item.split('=', 1) for item in get('iwdg-diagnostics').split(','))
    if diagnostics.get('timeouts') != '1' or diagnostics.get('window-violations') != '0':
        raise RuntimeError('unexpected watchdog events: ' + str(diagnostics))
except BaseException as exc:
    error = str(exc) or type(exc).__name__
    raise
finally:
    cleanup_error = None
    if qmp is not None:
        try:
            qmp.close()
        except Exception as exc:
            cleanup_error = exc
    for sock in sockets.values():
        try:
            sock.close()
        except OSError as exc:
            if cleanup_error is None:
                cleanup_error = exc
    cleanup_failed = error is None and cleanup_error is not None
    if cleanup_failed:
        error = 'cleanup failed: ' + str(cleanup_error)
    result = {'result': 'passed' if error is None else 'failed', 'error': error,
              'last_stage': last_stage, 'cases': rows, 'phases': phases,
              'elf': str(Path(elf).resolve()), 'elf_sha256': hashlib.sha256(Path(elf).read_bytes()).hexdigest(),
              'qemu': str(Path(qemu_bin).resolve()), 'qemu_sha256': hashlib.sha256(Path(qemu_bin).read_bytes()).hexdigest()}
    target = Path(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + '\n')
    if cleanup_failed:
        raise RuntimeError(error) from cleanup_error
print('RESULT: trobot HAL/BSP firmware integration passed; physical hardware unverified')
PY
