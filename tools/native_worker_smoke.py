#!/usr/bin/env python3
"""Test-only external five-frame consumer of a compiled native Rust worker."""
import argparse
import math
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smoke.support.process import stop
from smoke.support.processes import ChildProcess


def unavailable(stderr):
    return ('is not compiled in' in stderr or "unrecognized subcommand 'worker'" in stderr
            or 'error while loading shared libraries:' in stderr
            or ('Could not load library ' in stderr and 'cannot open shared object file' in stderr))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sim', type=Path, required=True)
    parser.add_argument('--engine', choices=('mujoco', 'ros2'), required=True)
    parser.add_argument('--model', type=Path)
    args = parser.parse_args()
    if not args.sim.is_file():
        print(f'SKIP: compiled Rust sim is unavailable: {args.sim}')
        return 77
    process = client = server = None
    child = None
    primary = None
    status = 1
    with tempfile.TemporaryDirectory(prefix=f'dm-native-{args.engine}-') as directory:
        try:
            path = str(Path(directory) / 'cosim.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            server.setblocking(False)
            command = [str(args.sim.resolve()), 'worker', '--cosim', path, '--engine', args.engine,
                       '--rate', '1000', '--frames', '5', '--timeout', '5']
            if args.engine == 'mujoco':
                if args.model is None:
                    raise ValueError('--model is required for MuJoCo')
                command += ['--mujoco-model', str(args.model.resolve())]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            child = ChildProcess('native worker', process)
            child.prepare()
            deadline = time.monotonic() + 7
            while client is None:
                child.pump()
                try:
                    client, _ = server.accept()
                except BlockingIOError:
                    if process.poll() is not None:
                        child.pump()
                        details = child.diagnostics()
                        if unavailable(details['stderr']):
                            print(f'SKIP: compiled {args.engine} backend/native library unavailable: {details}')
                            status = 77
                            break
                        raise RuntimeError(f'native worker exited before connection: {details}')
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'native worker did not connect: {child.diagnostics()}')
                    select.select([server, *child.readers()], [], [], .01)
            if client is not None:
                client.settimeout(.1)

                def exact(size):
                    data = bytearray()
                    while len(data) < size:
                        child.pump()
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f'native worker receive deadline: {child.diagnostics()}')
                        try:
                            part = client.recv(size - len(data))
                        except socket.timeout:
                            continue
                        if not part:
                            raise RuntimeError(f'native worker closed co-sim: {child.diagnostics()}')
                        data.extend(part)
                    return bytes(data)

                def frame():
                    size = struct.unpack('<I', exact(4))[0]
                    if not 28 <= size <= 156:
                        raise ValueError('native worker frame exceeds fixed co-sim bound')
                    body = exact(size)
                    header = struct.unpack_from('<IHHIQQ', body)
                    if header[:2] != (0x32434D44, 1) or header[3] != size - 28:
                        raise ValueError('native worker co-sim header mismatch')
                    return header, body[28:]

                header, payload = frame()
                if header[2:] != (1, 0, 0, 0) or payload:
                    raise ValueError('native worker did not send RESET')
                previous = 0
                for sequence in range(1, 6):
                    header, payload = frame()
                    if header[2] != 2 or header[4] != sequence or header[5] <= previous:
                        raise ValueError('native worker IMU sequence/time ordering mismatch')
                    if len(payload) != 24 or not all(math.isfinite(v) for v in struct.unpack('<6f', payload)):
                        raise ValueError('native worker IMU shape/finite mismatch')
                    previous = header[5]
                while process.poll() is None:
                    child.pump()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('native worker did not finish after five samples')
                    select.select(child.readers(), [], [], .01)
                child.pump()
                if process.returncode:
                    raise RuntimeError(f'native worker exited{process.returncode}: {child.diagnostics()}')
                status = 0
        except BaseException as error:
            primary = str(error) or type(error).__name__
        finally:
            operations = []
            if client is not None:
                operations.append(client.close)
            if server is not None:
                operations.append(server.close)
            if process is not None:
                operations += [lambda: stop(process), process.stdout.close, process.stderr.close]
            errors = []
            for operation in operations:
                try:
                    operation()
                except BaseException as error:
                    errors.append(str(error))
            if errors:
                primary = (primary + '; ' if primary else '') + 'cleanup failed: ' + '; '.join(errors)
    if primary:
        print(f'native {args.engine} worker FAILED: {primary}', file=sys.stderr)
        return 1
    if status == 0:
        print(f'RESULT: native Rust {args.engine} worker RESET + five finite ordered IMU frames and cleanup passed')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
