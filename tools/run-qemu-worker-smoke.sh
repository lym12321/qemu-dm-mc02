#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-qemu-worker.XXXXXX")
guest_elf="$run_dir/readback.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket cosim --socket qmp \
    --python-arg "{cosim}" --python-arg "{qmp}" --python-arg "$root_dir/tools/run-worker.sh" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import subprocess
import sys
import time

cosim, qmp, worker = sys.argv[1:]
control = QmpSession(qmp, timeout=2)

command = control.command

command("cont")

# The readback guest programs BMI088 ODR before it accepts physical samples.
# Start the worker only after that marker, otherwise a fast host can deliver
# all finite samples during sensor configuration and make this integration
# test pass without exercising the guest-visible data path.
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    memory = command("human-monitor-command", {
        "command-line": "xp /8wx 0x20000000"
    })
    guest_words = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", memory or "")]
    if len(guest_words) >= 8 and guest_words[7] == 0x434f4e46:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("guest did not finish BMI088 configuration")

process = subprocess.Popen([
    worker, "--cosim", cosim, "--protocol", "v2",
    "--wait-step-done", "--rate", "1000", "--frames", "1"
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

stdout, stderr = process.communicate(timeout=5)
if process.returncode != 0:
    raise RuntimeError("worker failed: %s" % stderr)

deadline = time.monotonic() + 2.0
guest_words = []
while time.monotonic() < deadline:
    memory = command("human-monitor-command", {
        "command-line": "xp /13wx 0x20000000"
    })
    guest_words = [int(value, 16) for value in re.findall(
        r"0x([0-9a-fA-F]{8})", memory or "")]
    # RBK1 + CONF prove that the guest reached its sensor transaction, while
    # a non-zero sensor time proves at least one worker IMU sample was
    # consumed by the BMI088 model rather than merely sent to a socket.
    if (len(guest_words) >= 13 and guest_words[0] == 0x52424b31 and
            guest_words[7] == 0x434f4e46 and guest_words[12] != 0):
        break
    time.sleep(0.01)
if not (len(guest_words) >= 13 and guest_words[0] == 0x52424b31 and
        guest_words[7] == 0x434f4e46 and guest_words[12] != 0):
    raise RuntimeError("worker IMU was not consumed by guest: %r" %
                       guest_words)
status = command("query-status")
if not isinstance(status, dict) or status.get("status") != "running":
    raise RuntimeError("QEMU did not remain running: %r" % status)
command("quit")
control.close()
print("RESULT: QEMU-native external worker integration smoke passed")
PY

# Keep the ordinary CLI/guest consumption gate above. These direct worker
# consumers isolate only host send availability on an actual QEMU stream.
for prefix in 0 2 59; do
    python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
        --socket cosim --socket qmp \
        --python-arg "{cosim}" --python-arg "{qmp}" --python-arg "$prefix" -- \
        "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
        -display none -monitor none -S \
        -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
        -serial chardev:cosim -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
from dm_mc02_firmware_rtf import deadline_qmp_call
import dm_mc02_sim_worker as worker
import json
import socket
import struct
import sys
import time
from unittest.mock import patch

cosim_path, qmp_path, prefix_text = sys.argv[1:]
prefix = int(prefix_text)
control = QmpSession(qmp_path, timeout=2)
state = {"steps": 0, "resets": 0, "closes": 0}
original_connect = worker.connect_unix
stream_class = worker.StepFramedSocket


def diagnostics():
    value = control.command("qom-get", {
        "path": "/machine", "property": "cosim-diagnostics"})
    return {key: int(number) for key, number in
            (field.split("=", 1) for field in value.split(","))}


def observe_prefix():
    deadline = time.monotonic() + 2.0
    while True:
        observed = deadline_qmp_call(
            control, deadline, "worker RX prefix", diagnostics)
        if observed["rx_buffered"] == prefix:
            control.settimeout(2.0)
            return observed
        time.sleep(0.001)  # Wait for host chardev IO, not guest time.


class BudgetSocket(socket.socket):
    def __init__(self, fd):
        super().__init__(fileno=fd)
        self.output = bytearray()
        self.old_wire = None
        self.holding = False
        self.release = False
        self.close_calls = 0

    def send(self, data):
        wire = bytes(data)
        if self.holding and not self.release:
            raise BlockingIOError
        if self.old_wire is None and struct.unpack_from("<H", wire, 10)[0] == 2:
            self.old_wire = wire
            self.holding = True
            written = super().send(data[:prefix]) if prefix else 0
            if written != prefix:
                raise RuntimeError("native prefix budget was not written exactly")
            self.output.extend(wire[:written])
            state["before"] = observe_prefix()
            control.command("system_reset")
            state["retained"] = diagnostics()
            if state["retained"]["rx_buffered"] != prefix:
                raise RuntimeError("native reset lost worker prefix")
            if written:
                return written
            raise BlockingIOError
        written = super().send(data)
        self.output.extend(wire[:written])
        return written

    def close(self):
        if self.fileno() != -1:
            self.close_calls += 1
        super().close()


class ObservedPlant(worker.NullEngine):
    def step(self, dt):
        state["steps"] += 1
        return super().step(dt)

    def reset(self):
        state["resets"] += 1
        if state["steps"] != 1:
            raise RuntimeError("old plant advanced during RESET")
        pending = [bytes(view) for view in state["stream"].tx_queue]
        if pending != [state["sock"].old_wire[prefix:]]:
            raise RuntimeError("RESET altered or copied the pending old STEP")
        state["pending_at_reset"] = [view.hex() for view in pending]
        super().reset()
        # This callback follows the actual worker's matching RESET_ACK.
        state["sock"].release = True

    def close(self):
        state["closes"] += 1


def connect(path, timeout):
    owned = original_connect(path, timeout)
    sock = BudgetSocket(owned.detach())
    state["sock"] = sock
    return sock


def capture_stream(sock):
    state["stream"] = stream_class(sock)
    return state["stream"]


engine = ObservedPlant(0)
with patch.object(sys, "argv", [
        "worker", "--cosim", cosim_path, "--protocol", "v2",
        "--backend", "test:factory", "--motor-count", "0", "--frames", "1"]):
    args = worker.parse_args()
with patch.object(worker, "connect_unix", connect), \
        patch.object(worker, "load_factory", lambda spec: lambda args: engine), \
        patch.object(worker, "StepFramedSocket", capture_stream), \
        patch.object(worker.step_protocol, "generate_session_id", lambda: 123):
    if worker.run(args) != 0:
        raise RuntimeError("pending STEP/reset worker failed")

after = diagnostics()
before = state["before"]
if (state["steps"], state["resets"], state["closes"]) != (2, 1, 1):
    raise RuntimeError("worker advanced or closed an incorrect number of times")
if (state["sock"].fileno() != -1 or state["sock"].close_calls != 1 or
        state["stream"].tx_pending):
    raise RuntimeError("worker did not drain and close its transport")
if (after["rx_frames"] != before["rx_frames"] + 1 or
        after["rx_bad_frames"] != before["rx_bad_frames"] + 1 or
        after["rx_dropped_bytes"] != before["rx_dropped_bytes"] + 104 or
        after["rx_buffered"] != 0):
    raise RuntimeError("native RX did not reject exactly one complete old STEP")
output = bytes(state["sock"].output)
initial_reset = struct.pack("<IIHHIQQQI", 40, 0x32434D44, 2, 1, 0,
                            0, 0, 0, 123)
old = state["sock"].old_wire
new = output[len(initial_reset) + len(old):]
if output != initial_reset + old + new or len(old) != 104 or len(new) != 104:
    raise RuntimeError("pending STEP/reset output order or frame size is wrong")
old_header = struct.unpack_from("<IIHHIQQQI", old)
new_header = struct.unpack_from("<IIHHIQQQI", new)
if (old_header != (100, 0x32434D44, 2, 2, 60, 1, 1000000, 1000000, 123) or
        new_header != (100, 0x32434D44, 2, 2, 60, 1, 1000000, 1000000, 124)):
    raise RuntimeError("old/new STEP headers did not use their own sessions")
if old[44:] != new[44:]:
    raise RuntimeError("stationary NullEngine payload changed across RESET")
report = {key: value for key, value in state.items() if key not in ("sock", "stream")}
report.update(prefix=prefix, after=after, output_hex=output.hex(),
              socket_close_calls=state["sock"].close_calls)
print("WORKER_PENDING_RESET_EVIDENCE: " + json.dumps(report, sort_keys=True))
control.close()
print("RESULT: actual worker pending STEP/reset native gate passed (prefix=%d)" % prefix)
PY
done
