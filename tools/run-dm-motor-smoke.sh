#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
[[ -x "$qemu_bin" ]] || qemu_bin="$root_dir/build/qemu/qemu-system-arm"

# An explicit MotorTest ELF selects the actual HAL/BSP/component consumer.
if (( $# )); then
    [[ $# -le 2 && -f "$1" && -x "$qemu_bin" ]] || {
        printf '%s\n' 'Usage: run-dm-motor-smoke.sh [MotorTest.elf [report.json]]' >&2
        exit 1
    }
    motor_python="$root_dir/.venv/bin/python"
    [[ -x "$motor_python" ]] && "$motor_python" -c 'import mujoco' || {
        printf '%s\n' 'MuJoCo environment required: uv sync --locked --group dev --extra mujoco' >&2
        exit 1
    }
    "$motor_python" "$script_dir/dm_mc02_test_harness.py" --timeout 35 \
        --socket qmp --socket uart --socket can \
        --python-arg '{qmp}' --python-arg '{uart}' --python-arg '{can}' \
        --python-arg "$1" --python-arg "$qemu_bin" \
        --python-arg "${2:-$root_dir/build/test-results/trobot-motor.json}" \
        --python-arg "$root_dir/smoke/dm_mc02_mujoco_smoke.xml" -- \
        "$qemu_bin" -machine dm-mc02,fdcan-host-ack=on,iwdg-boot-grace-ms=0 \
        -kernel "$1" -nodefaults -display none -monitor none -S \
        -qmp 'unix:{qmp},server=on,wait=off' -serial none \
        -chardev 'socket,id=uart,path={uart},server=on,wait=off' -serial chardev:uart \
        -serial none -serial none -serial none -serial none -serial none \
        -chardev 'socket,id=can,path={can},server=on,wait=off' -serial chardev:can <<'PY'
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import sys
import time

import mujoco
from dm_mc02_motor_adapter import DmMotorBusAdapter
from dm_mc02_qmp import QmpSession
from dm_mc02_sim_worker import CanStream, MujocoEngine

qmp_path, uart_path, can_path, elf, qemu_bin, report_path, model = sys.argv[1:]
# Vendor H7 dm_motor_drv.c, fa8f511c, lines171–177/315–336; P/V/T12.5/30/10.
positive = bytes.fromhex('7fff7ff000000801')
negative = bytes.fromhex('7fff7ff0000007fd')
words = lambda code: b'\xff' * 7 + bytes([code])
expected_packets = ([words(0xfb)] + [positive] * 10 + [words(0xfc)] +
                    [positive] * 100 + [words(0xfd)] + [positive] * 20 +
                    [words(0xfc)] + [negative] * 60 + [words(0xfd)])
goldens = {'disabled': (32768, 2048, 2048), 'drive': (33349, 2348, 2049),
           'coast': (33580, 2348, 2048), 'brake': (33920, 2048, 2045)}
required = {'control.rx', 'reset.poweron', 'bsp.init', 'task.stack',
            'motor.feedback', 'motor.units', 'motor.can',
            *(f'motor.{phase}.{field}' for phase in goldens
              for field in ('pos', 'vel', 'effort'))}
sockets = []
qmp = None
rows, trace, summaries = [], [], []
error = None
stage = 'startup'
drop_feedback = os.environ.get('DM_MOTOR_DROP_FEEDBACK', '0')

def interrupted(signum, frame):
    raise RuntimeError(f'interrupted by signal {signum}')

signal.signal(signal.SIGTERM, interrupted)
try:
    if drop_feedback not in ('0', '1'):
        raise ValueError('DM_MOTOR_DROP_FEEDBACK must be0 or1')
    qmp = QmpSession(qmp_path, timeout=2)
    for path in (uart_path, can_path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sockets.append(sock)
        sock.settimeout(2)
        sock.connect(path)
        sock.setblocking(False)
    uart, can_sock = sockets
    can = CanStream(can_sock)
    engine = MujocoEngine(model, 1, .001)
    adapter = DmMotorBusAdapter(1, motor_protocol='dm-mit')
    qmp.command('cont')
    buffer = bytearray()
    deadline = time.monotonic() + 25
    done = started = False
    steps = 0
    previous_time = -1
    while not done and time.monotonic() < deadline:
        readable, _, _ = select.select(sockets, [], [], .01)
        if uart in readable:
            chunk = uart.recv(4096)
            if not chunk:
                raise RuntimeError('firmware UART disconnected')
            buffer.extend(chunk)
            if len(buffer) > 8192:
                raise RuntimeError('firmware output exceeded bound')
            while b'\n' in buffer:
                line, _, remainder = buffer.partition(b'\n')
                buffer[:] = remainder
                line = line.decode('ascii').strip()
                if not line.startswith('TTEST '):
                    continue
                print(line, flush=True)
                fields = line.split()
                if len(fields) != 5:
                    raise RuntimeError('invalid firmware report')
                if fields[1] in ('PHASE', 'DONE'):
                    summary = list(map(int, fields[2:]))
                    if summary != [len(required), 0, 1]:
                        raise RuntimeError('firmware summary differs from required coverage')
                    summaries.append(fields[1])
                    if fields[1] == 'DONE':
                        done = True
                    continue
                status, name = fields[1:3]
                actual, expected = map(int, fields[3:])
                rows.append(dict(status=status, name=name, actual=actual, expected=expected))
                stage = name
                if status == 'READY':
                    if name != 'motor.start' or started or (actual, expected) != (190, 1000):
                        raise RuntimeError('unexpected motor admission stage')
                    started = True
                    uart.sendall(b'\xa5')
                elif status == 'FAIL':
                    raise RuntimeError('firmware failed: ' + name)
                elif status not in ('PASS', 'SKIP'):
                    raise RuntimeError('unknown firmware test status')
                elif status == 'PASS' and name in ('motor.feedback', 'motor.units', 'motor.can'):
                    golden = {'motor.feedback': 195, 'motor.units': 4, 'motor.can': 0}[name]
                    if (actual, expected) != (golden, golden):
                        raise RuntimeError('firmware motor summary mismatch')
                elif name.startswith('motor.') and name.rsplit('.', 1)[0].split('.')[-1] in goldens:
                    _, phase, field = name.split('.')
                    golden = goldens[phase][('pos', 'vel', 'effort').index(field)]
                    if field == 'vel':
                        rows[-1]['expected_range'] = [golden - 1, golden]
                    if expected != golden or (actual != golden and
                            not (field == 'vel' and actual == golden - 1)):
                        raise RuntimeError('actual firmware feedback differs from analytic golden')
        if can_sock in readable:
            for ident, flags, dlc, stamp, data in can.receive():
                ordinal = len(trace)
                if not started or ordinal >= len(expected_packets):
                    raise RuntimeError('unsolicited or extra motor command')
                if (ident, flags, dlc, data) != (1, 0, 8, expected_packets[ordinal]):
                    raise RuntimeError(f'motor command mismatch at {ordinal}: {data.hex()}')
                if stamp <= previous_time:
                    raise RuntimeError('CAN command time did not increase')
                previous_time = stamp
                command = adapter.decode(ident, flags, data, stamp)
                if command is None:
                    raise RuntimeError('adapter rejected admitted command')
                adapter.apply(engine, command)
                if command.mode == 'dm-control':
                    # This fixture owns a1ms sample clock, independent of CPU time.
                    engine.step(.001)
                    steps += 1
                position, velocity, effort = engine.motor_feedback(0)
                # Analytic box rotor; semi-implicit Euler uses new velocity.
                if steps <= 10:
                    p_expected = v_expected = effort_expected = 0.0
                elif steps <= 110:
                    n = steps - 10
                    acceleration = (30 / 4095) * 6000
                    v_expected = acceleration * .001 * n
                    p_expected = acceleration * .000001 * n * (n + 1) / 2
                    effort_expected = 30 / 4095
                elif steps <= 130:
                    v_expected = 18000 / 4095
                    p_expected = (909 / 4095) + v_expected * .001 * (steps - 110)
                    effort_expected = 0.0
                else:
                    n = steps - 130
                    acceleration = (-50 / 4095) * 6000
                    v_expected = 18000 / 4095 + acceleration * .001 * n
                    p_expected = (1269 / 4095) + (18000 / 4095) * .001 * n + acceleration * .000001 * n * (n + 1) / 2
                    effort_expected = -50 / 4095
                # Control words change the gate without advancing the rotor.
                if command.mode == 'dm-disable':
                    effort_expected = 0.0
                for actual, expected in zip((position, velocity, effort),
                                            (p_expected, v_expected, effort_expected)):
                    if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9):
                        raise RuntimeError(f'rotor state differs from analytic oracle at {ordinal}: {(position, velocity, effort)}')
                if not math.isclose(engine.data.time, steps * .001, rel_tol=0, abs_tol=1e-12):
                    raise RuntimeError('plant step clock drifted')
                state = adapter.state(engine, 0, steps * 1_000_000)
                feedback_id, feedback = adapter.encode_feedback(state)
                if drop_feedback != '1' or ordinal != 0:
                    can.send(feedback_id, 0, 8, stamp, feedback)
                    can.drain()
                trace.append(dict(can_time_ns=stamp, plant_time_ns=steps * 1_000_000,
                                  command=data.hex(), feedback=feedback.hex(),
                                  position=position, velocity=velocity, effort=effort))
    if not done:
        raise TimeoutError('firmware did not complete: ' + stage)
    counts = Counter(row['name'] for row in rows if row['status'] == 'PASS')
    skips = [row['name'] for row in rows if row['status'] == 'SKIP']
    if (counts != Counter({name: 1 for name in required}) or skips != ['plant.physics'] or
            summaries != ['PHASE', 'DONE'] or len(trace) != 195 or steps != 190):
        raise RuntimeError('missing, duplicated or unexpected coverage/command')
    watchdog = qmp.command('qom-get', {'path': '/machine', 'property': 'iwdg-diagnostics'})
    values = dict(item.split('=', 1) for item in watchdog.split(','))
    if values.get('timeouts') != '0' or values.get('window-violations') != '0':
        raise RuntimeError('unexpected watchdog event')
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
    for sock in sockets:
        try:
            sock.close()
        except OSError as exc:
            if cleanup_error is None:
                cleanup_error = exc
    cleanup_failed = error is None and cleanup_error is not None
    if cleanup_failed:
        error = 'cleanup failed: ' + str(cleanup_error)
    report = dict(result='passed' if error is None else 'failed', error=error,
                  last_stage=stage, cases=rows, trace=trace, mujoco=mujoco.__version__,
                  dropped_first_feedback=drop_feedback == '1',
                  plant_step_ns=1_000_000, limits=['fixed rotor fixture', 'no global lockstep',
                                               'no physical calibration', 'no realtime gate'])
    for name, path in (('elf', elf), ('qemu', qemu_bin), ('model', model)):
        report[name] = str(Path(path).resolve())
        report[name + '_sha256'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    target = Path(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + '\n')
    if cleanup_failed:
        raise RuntimeError(error) from cleanup_error
print('RESULT: trobot motor/MuJoCo rotor boundary passed; physical calibration unverified')
PY
    exit
fi

command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-motor.XXXXXX")
guest_elf="$run_dir/motor.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_motor_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_motor_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket cosim --socket can --socket qmp \
    --python-arg "$root_dir" --python-arg "{qmp}" --python-arg "{cosim}" --python-arg "{can}" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim \
    -serial none -serial none -serial none -serial none -serial none -serial none \
    -chardev "socket,id=can1,path={can},server=on,wait=off" \
    -serial chardev:can1 -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import os
import re
import subprocess
import sys
import time

root, qmp_path, cosim_path, can_path = sys.argv[1:]
worker = os.path.join(root, "tools", "run-worker.sh")
process = None

qmp = QmpSession(qmp_path, timeout=2.0)

command = qmp.command

def result_words():
    text = command("human-monitor-command", {
        "command-line": "xp /5wx 0x20000000",
    }) or ""
    return [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", text)]

try:
    command("cont")
    # Deliberately let the guest submit its first CAN frames before the
    # worker connects. This verifies QEMU's pre-open FDCAN TX queue rather
    # than relying on a lucky process scheduling order.
    time.sleep(0.05)
    process = subprocess.Popen([
        worker, "--cosim", cosim_path, "--fdcan", can_path,
        "--protocol", "v2",
        "--engine", "null", "--motor-protocol", "dm-mit", "--rate", "1000",
        "--realtime", "--frames", "5000", "--verbose",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 7.0
    words = []
    while time.monotonic() < deadline:
        words = result_words()
        if words and words[0] == 0x444d4d31:
            if ((words[1] >> 18) & 0x7ff) != 0x11 or ((words[2] >> 16) & 0xf) != 8:
                raise RuntimeError("invalid DM feedback header: %r" % words)
            print("RESULT: DM MIT motor command/feedback closed-loop smoke passed")
            break
        if process is not None and process.poll() is not None and process.returncode != 0:
            break
        time.sleep(0.01)
    else:
        process.terminate()
        stdout, stderr = process.communicate(timeout=3)
        raise RuntimeError("DM motor feedback was not observed: %r; worker stdout=%r stderr=%r" %
                           (words, stdout, stderr))
    if process is not None and process.poll() is None:
        process.terminate()
    stdout, stderr = process.communicate(timeout=3) if process is not None else ("", "")
    # Direct Python termination reports -15; the uv/bash launcher may expose
    # the equivalent shell status 143. Both are expected after feedback was
    # observed and the smoke deliberately stops the still-running worker.
    if process is not None and process.returncode not in (0, -15, 143):
        raise RuntimeError("worker failed: %s" % stderr)
finally:
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=2)
    command("quit")
    qmp.close()
PY

printf '%s\n' 'RESULT: DM MIT worker integration smoke passed'
