#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# -eq 0 ]] || { printf 'Usage: %s\n' "$0" >&2; exit 2; }

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir:$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1;
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2; exit 1;
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1;
}

run_dir=$(mktemp -d "/tmp/dm-qemu.lab-fixture.XXXXXX")
guest_elf="$run_dir/lab.elf"
trap 'rm -rf -- "$run_dir"' EXIT
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -O2 -Wall -Wextra -Werror \
    -ffreestanding -fno-builtin -fno-stack-protector -nostdlib -nostartfiles \
    -Wl,--gc-sections -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_uart_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_lab_smoke.c"

# The direct Rust LabChannels native gate owns lower API evidence. This entry
# exercises the actual compiled CLI, retained board fixture and public manifest.
cd -- "$root_dir"
python3 - "$root_dir/examples/labs/board-io.yaml" \
    "$guest_elf" "$run_dir/lab.yaml" <<'PY'
from pathlib import Path
import sys
import yaml

example, firmware, destination = sys.argv[1:]
manifest = yaml.safe_load(Path(example).read_text())
manifest["nodes"]["main"]["firmware"] = firmware
Path(destination).write_text(yaml.safe_dump(manifest, sort_keys=False))
PY
"$script_dir/sim" run "$run_dir/lab.yaml" \
    --qemu "$qemu_bin" --report "$run_dir/lab-report.json"
python3 - "$run_dir/lab-report.json" <<'PY'
from pathlib import Path
import json
import sys

report = json.loads(Path(sys.argv[1]).read_text())
assert report["status"] == "passed", report
result = report["result"]
assert result["status"] == ["pass"] * 8 and not result["failures"], result
assert result["delivered_inputs"] == 7, result
assert result["end_ns"] - result["origin_ns"] == 400_000_000, result
assert result["ready_receipts"]["main"]["kind"] == "uart", result
assert result["ready_receipts"]["main"]["source_node"] == "main", result
assert result["ready_receipts"]["main"]["source_port"] == "uart1", result
print("AUTOMATIC_LAB_REPORT: " + json.dumps(report, sort_keys=True))
print("Manifest consumer: eight assertions passed after owner cleanup")
PY
# A real UART marker that never occurs must exhaust the shared startup bound,
# return failed, and unwind owners; it cannot be inferred from socket readiness.
python3 - "$run_dir/lab.yaml" "$run_dir/not-ready.yaml" <<'PY'
from pathlib import Path
import sys
import yaml

source, destination = sys.argv[1:]
manifest = yaml.safe_load(Path(source).read_text())
manifest["experiment"]["ready"][0]["text"] = "THIS_APPLICATION_EVENT_NEVER_OCCURS"
Path(destination).write_text(yaml.safe_dump(manifest, sort_keys=False))
PY
if "$script_dir/sim" run "$run_dir/not-ready.yaml" \
    --qemu "$qemu_bin" --startup-timeout 0.3 --timeout 2 \
    --report "$run_dir/not-ready-report.json" > "$run_dir/not-ready.log" 2>&1; then
    printf '%s\n' 'missing application ready unexpectedly passed' >&2
    exit 1
fi
python3 - "$run_dir/not-ready-report.json" <<'PY'
from pathlib import Path
import json
import sys

report = json.loads(Path(sys.argv[1]).read_text())
assert report["status"] == "failed", report
assert report["error_type"] in ("TimeoutError", "StartupTimeoutError"), report
print("MISSING_READY_REPORT: " + json.dumps(report, sort_keys=True))
print("Missing application ready: bounded failure confirmed")
PY
# The panel is another direct consumer of the same runtime, with native GDB
# and a separate user QMP monitor. Its controller reuses the bounded stop helper.
lab_gdb_bin=${GDB_ARM:-}
if [[ -z "$lab_gdb_bin" ]]; then
    if command -v arm-none-eabi-gdb >/dev/null 2>&1; then
        lab_gdb_bin=arm-none-eabi-gdb
    else
        lab_gdb_bin=gdb-multiarch
    fi
fi
command -v "$lab_gdb_bin" >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gdb or gdb-multiarch is required (override GDB_ARM)' >&2; exit 1;
}
python3 - "$guest_elf" "$qemu_bin" "$run_dir" "$lab_gdb_bin" "$script_dir/sim" <<'PY'
import ast
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import time

from tools.dm_mc02_qmp import QmpSession
from tools.dm_mc02_test_harness import stop

firmware, qemu_bin, directory, gdb_bin, sim_bin = sys.argv[1:]
qmp_path = str(Path(directory) / "panel-qmp.sock")
with socket.socket() as reserve:
    reserve.bind(("127.0.0.1", 0))
    gdb_port = reserve.getsockname()[1]
deadline = time.monotonic() + 20
process = None
qmp = None
primary = None
lines = []
serial = bytearray()
tail = bytearray()
log = (Path(directory) / "panel.stderr").open("w+b")

def remaining():
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError(f"interactive consumer deadline; lines={lines!r}")
    return value

def read_output():
    remaining()
    readable, _, _ = select.select([process.stdout], [], [], min(.01, remaining()))
    if not readable:
        return
    data = os.read(process.stdout.fileno(), 4096)
    if not data:
        raise RuntimeError(f"interactive CLI exited early: {process.poll()}; lines={lines!r}")
    tail.extend(data)
    if len(tail) > 8192:
        raise BufferError("interactive output line exceeded bound")
    while b"\n" in tail:
        line, _, suffix = tail.partition(b"\n")
        tail[:] = suffix
        line = line.decode("utf-8")
        lines.append(line)
        if len(lines) > 128:
            raise BufferError("interactive output exceeded 128 lines")
        match = re.fullmatch(r"UART uart1 poll_sample_ns=[0-9]+ (.+)", line)
        if match:
            data = ast.literal_eval(match[1])  # Test-only inert bytes representation.
            assert isinstance(data, bytes)
            serial.extend(data)
            if len(serial) > 4096:
                raise BufferError("interactive UART exceeded bound")

def wait_for(predicate):
    while not predicate():
        read_output()

def send(command):
    process.stdin.write((command + "\n").encode())
    process.stdin.flush()

def snapshot():
    qmp.settimeout(min(2., remaining()))
    return qmp.command("qom-get", {"path": "/machine", "property": "board-state"})

try:
    process = subprocess.Popen(
        [sim_bin, "lab", "interactive", "--elf", firmware,
         "--qemu", qemu_bin, "--uart", "uart1", "--can", "can1", "--adc",
         "--qmp", qmp_path, "--gdb-port", str(gdb_port), "--paused",
         "--startup-timeout", "5", "--timeout", "15"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, start_new_session=True)
    wait_for(lambda: "Lab connected; enter help for commands" in lines)
    qmp = QmpSession(qmp_path, timeout=min(2., remaining()), connect_timeout=min(2., remaining()))
    assert qmp.command("query-status")["running"] is False
    debug = subprocess.run(
        [gdb_bin, "-q", "--batch", firmware,
         "-ex", f"target remote 127.0.0.1:{gdb_port}", "-ex", "info registers pc",
         "-ex", "detach"], capture_output=True, text=True, timeout=min(5., remaining()))
    assert debug.returncode == 0 and re.search(r"^pc\s+0x[0-9a-f]+", debug.stdout, re.M), debug
    print("Native GDB:", re.search(r"^pc\s+0x[0-9a-f]+.*", debug.stdout, re.M)[0])
    wait_for(lambda: b"LAB_READY\n" in serial and b"ADC=43329\n" in serial)
    send("uart uart1 q")
    wait_for(lambda: b"UART=q\n" in serial)
    cursor = len(serial)
    send("key press")
    wait_for(lambda: b"KEY=0\n" in serial[cursor:])
    state = snapshot()
    assert state["key"]["pressed"] and state["led"]["rgb"] == 0xffffff, state
    send("gpio B0 1")
    wait_for(lambda: bool(snapshot()["gpio"]["B"]["sampled"] & 1))
    send("pause")
    wait_for(lambda: qmp.command("query-status")["running"] is False)
    send("adc ch4 raw 12345")
    wait_for(lambda: "error: ADC input requires running QEMU; use cont first" in lines)
    send("cont")
    send("adc ch4 voltage_uv 1650000")
    wait_for(lambda: b"ADC=32768\n" in serial)
    send("adc ch4 raw 12345")
    wait_for(lambda: b"ADC=12345\n" in serial)
    send("can can1 0x321 0 8 4142434445464748")
    wait_for(lambda: any("id=0x322 flags=0x0 dlc=8 data=4142434445464748" in line for line in lines))
    cursor = len(serial)
    send("key release")
    wait_for(lambda: b"KEY=1\n" in serial[cursor:])
    assert not snapshot()["key"]["pressed"] and snapshot()["led"]["rgb"] == 0
    qmp.close()
    qmp = None
    send("quit")
    process.wait(timeout=min(5., remaining()))
    assert process.returncode == 0, process.returncode
except BaseException as error:
    primary = error
    raise
finally:
    failures = []
    for label, resource, release in [
        ("QMP", qmp, lambda item: item.close()),
        ("CLI process", process, stop),
        ("CLI stdin", None if process is None else process.stdin, lambda item: item.close()),
        ("CLI stdout", None if process is None else process.stdout, lambda item: item.close()),
    ]:
        if resource is not None:
            try:
                release(resource)
            except BaseException as error:
                failures.append((label, error))
    diagnostic = ""
    try:
        log.seek(0, os.SEEK_END)
        log.seek(max(0, log.tell() - 12000))
        diagnostic = log.read(12000).decode(errors="replace")
    except BaseException as error:
        failures.append(("CLI stderr read", error))
    try:
        log.close()
    except BaseException as error:
        failures.append(("CLI stderr close", error))
    if primary is not None and diagnostic:
        primary.add_note("interactive CLI stderr:\n" + diagnostic)
    if failures:
        error = primary if primary is not None else failures[0][1]
        for label, failure in failures:
            error.add_note(f"interactive consumer cleanup {label} failed: {failure}")
        if primary is None:
            raise error
print("Interactive consumer: native GDB/QMP, UART, KEY/LED, GPIO, ADC and CAN passed")
PY
printf '%s\n' 'RESULT: Lab channels, YAML and interactive consumers passed'
