#!/usr/bin/env python3
"""Actual trobot referee UART/DMA/RTOS consumer of the shared host endpoint.

This fixed fixture controls socket short writes, not OS backpressure or UART
arrival time. Optional multi-board presets run through the public Simulation;
neither profile establishes a global CPU/plant barrier.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
LIMITS = ['Fixed 2026 V1.1.0 four-command fixture; unsupported 0206 is not transmitted.', 'Actual UART7/HAL RX DMA/BSP callback/high executor/current basic.cc/public state.', 'Socket budget injects a real 19-byte write then the retained 63-byte suffix; no OS-backpressure claim.', 'READY timestamps are post-read QMP upper observations, not byte-arrival times.', 'Engine advances one explicit second independently of CPU time; no global lockstep or official command-rate claim.', 'BSP initialization exercises register/ID setup, not physical IMU samples; plant.physics SKIP.', 'Functional consumer gate; physical calibration and sustained performance unverified.']

def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")

def source_snapshot(elf, qemu, firmware):
    """Hash actual inputs, including the canonical current firmware parser."""
    paths = [Path(elf), Path(qemu), ROOT / 'uv.lock', ROOT / 'qemu.lock', ROOT / 'pyproject.toml', Path(__file__).resolve(), ROOT / 'tools/run-trobot-referee-test.sh']
    paths += [ROOT / 'tools' / name for name in ('dm_mc02_qmp.py', 'dm_mc02_firmware_rtf.py', 'dm_mc02_test_harness.py', 'dm_mc02_trobot_business.py')]
    paths += [ROOT / 'Cargo.lock', ROOT / 'crates/sim-host/tests/referee_native.rs']
    paths += sorted((ROOT / 'crates/sim-host/src/devices/referee').glob('*.rs'))
    paths += [firmware / name for name in ('CMakeLists.txt', 'CMakePresets.json', 'app/CMakeLists.txt', 'app/selftest/main.cc', 'app/selftest/referee.cc', 'bsp/CMakeLists.txt', 'bsp/test/core.c', 'bsp/test/transport.c', 'bsp/test/selftest.h', 'bsp/src/bsp.c', 'bsp/src/uart.c', 'bsp/src/time.c', 'bsp/src/sys.c', 'bsp/src/imu.c', 'bsp/include/bsp/uart.h', 'components/robomaster/src/basic.cc', 'components/robomaster/include/robomaster/robomaster.h', 'components/robomaster/include/robomaster/def.h', 'components/msg/src/msg.cc', 'components/msg/include/msg/msg.h', 'components/utils/src/crc.cc', 'components/utils/include/utils/crc.h', 'components/utils/include/utils/os.h', 'Core/Inc/FreeRTOSConfig.h', 'Core/Src/freertos.c', 'Core/Src/usart.c', 'Core/Src/dma.c', 'Core/Src/stm32h7xx_it.c', 'Core/Src/stm32h7xx_hal_msp.c', 'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart.c', 'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_dma.c', 'cmake/gcc-arm-none-eabi.cmake')]
    result = {}
    for path in paths:
        with path.open('rb') as stream:
            result[str(path.resolve())] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result

def checkout_snapshot(firmware):
    result = {}
    for name, path in (('project', ROOT), ('qemu', ROOT / 'qemu/upstream'), ('firmware', firmware), ('firmware_robomaster', firmware / 'components/robomaster'), ('firmware_msg', firmware / 'components/msg'), ('firmware_utils', firmware / 'components/utils')):
        values = {}
        head = subprocess.run(['git', 'rev-parse', '--verify', '--quiet', 'HEAD'], cwd=path, capture_output=True, text=True, timeout=2)
        if head.returncode not in (0, 1):
            raise RuntimeError('cannot observe checkout HEAD: ' + name)
        values['head'] = head.stdout.strip() if head.returncode == 0 else None
        status = subprocess.check_output(['git', 'status', '--short'], cwd=path, text=True, timeout=2)
        if len(status.encode()) > 12000:
            raise BufferError('checkout observation exceeds bound: ' + name)
        values['status'] = status.strip()
        if values['head'] is None:
            values['head_unavailable'] = 'repository has no commit; input files are hashed'
        values['declaration'] = 'actual hashed working tree; no clean-checkout claim'
        result[name] = values
    return result

def run(elf, output, firmware, *, qemu=None):
    """Build and run the sole compiled fixture; Python records provenance only."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = dict(result='failed', errors=[], cleanup_errors=[], limits=list(LIMITS))
    try:
        elf = Path(elf).resolve(strict=True)
        qemu = Path(qemu or os.environ.get('QEMU_SYSTEM_ARM', ROOT / 'build/qemu/qemu-system-arm')).resolve(strict=True)
        firmware = Path(firmware).resolve(strict=True)
        report['inputs_before'] = source_snapshot(elf, qemu, firmware)
        report['input_checkout'] = checkout_snapshot(firmware)
        executable = os.environ.get('SIM_REFEREE_TEST_RUNNER')
        if executable is None:
            command = ['cargo', 'test', '--locked', '-p', 'sim-host',
                       '--features', 'scene,machine,referee', '--test', 'referee_native',
                       '--no-run', '--message-format=json']
            built = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180)
            (output / 'build.log').write_text(built.stderr + built.stdout)
            if built.returncode:
                raise RuntimeError('Rust referee fixture build failed; see build.log')
            artifacts = [json.loads(line) for line in built.stdout.splitlines() if line.startswith('{')]
            executable, = [item['executable'] for item in artifacts if item.get('reason') == 'compiler-artifact'
                           and item.get('target', {}).get('name') == 'referee_native' and item.get('executable')]
        executable = Path(executable).resolve(strict=True)
        report['consumer_executable'] = str(executable)
        report['consumer_sha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
        environment = dict(os.environ, SIM_NATIVE_QEMU=str(qemu), SIM_NATIVE_REFEREE_ELF=str(elf))
        command = [str(executable), '--ignored', '--exact',
                   'actual_uart7_dma_parser_consumes_initial_and_retained_partial_snapshot', '--nocapture']
        # The compiled fixture owns QEMU. Forward cancellation to let that owner
        # close its peers and process before the test runner itself is reaped.
        with (output / 'consumer.log').open('w') as log:
            consumer = subprocess.Popen(command, cwd=output, env=environment,
                                        stdout=log, stderr=subprocess.STDOUT)
            try:
                consumer.wait(timeout=30)
            finally:
                if consumer.poll() is None:
                    consumer.terminate()
                    try:
                        consumer.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        consumer.kill()
                        consumer.wait(timeout=2)
                        report['cleanup_errors'].append('compiled referee consumer exceeded cancellation cleanup deadline')
        transcript = (output / 'consumer.log').read_text()
        print(transcript, end='')
        report['consumer_exit_code'] = consumer.returncode
        if consumer.returncode:
            raise RuntimeError('Rust referee consumer failed; see consumer.log')
        rows = [json.loads(line.removeprefix('NATIVE_REPORT ')) for line in transcript.splitlines()
                if line.startswith('NATIVE_REPORT ')]
        native, = rows
        report.update(native)
        if not report['cleanup_verified'] or Path(native['directory']).exists() or Path('/proc', str(native['qemu_pid'])).exists():
            raise RuntimeError('native referee consumer left an owned process or directory')
        if hashlib.sha256(executable.read_bytes()).hexdigest() != report['consumer_sha256']:
            raise RuntimeError('compiled referee consumer changed during execution')
        report['inputs_after'] = source_snapshot(elf, qemu, firmware)
        if report['inputs_before'] != report['inputs_after']:
            raise RuntimeError('referee inputs changed through cleanup')
    except BaseException as exc:
        report['errors'].append(str(exc) or type(exc).__name__)
    report['result'] = 'failed' if report['errors'] or report['cleanup_errors'] else 'passed'
    write_json(output / 'report.json', report)
    print('RESULT: trobot referee UART consumer ' + report['result'] + '; plant.physics SKIP')
    return 0 if report['result'] == 'passed' else 1

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('elf')
    parser.add_argument('output', nargs='?', default=ROOT / 'build/test-results/trobot-referee')
    parser.add_argument('firmware_source', nargs='?', default=ROOT.parent / 'trobot')
    parser.add_argument('--qemu')
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument('--arena', action='store_true', help='run ArenaRefereeTest on a public two-board sim/v1 scene')
    profile.add_argument('--scenario', action='store_true', help='run ScenarioRefereeTest with session-relative referee script inputs')
    parser.add_argument('--cli', action='store_true', help='exercise the actual sim module entry (requires --scenario)')
    args = parser.parse_args(argv)
    if args.cli and (not args.scenario):
        parser.error('--cli requires --scenario')
    try:
        if args.arena or args.scenario:
            from tools.dm_mc02_scene_smoke import run_referee
            options = {'scenario': True} if args.scenario else {}
            if args.cli:
                options['cli'] = True
            return run_referee(args.elf, args.output, args.firmware_source, qemu=args.qemu, **options)
        return run(args.elf, args.output, args.firmware_source, qemu=args.qemu)
    except (OSError, ValueError) as exc:
        print('referee consumer failed: ' + str(exc), file=sys.stderr)
        return 1
if __name__ == '__main__':
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('referee consumer interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
