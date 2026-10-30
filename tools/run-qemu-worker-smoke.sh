#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
worker_entry="$script_dir/run-worker.sh"
fdcan_arg=""
chassis_harness=()
chassis_qemu=()
if [[ $# -ne 0 ]]; then
    [[ $# -eq 1 && $1 == --chassis ]] || {
        printf 'usage: %s [--chassis]\n' "$0" >&2
        exit 2
    }
    worker_entry="$script_dir/run-chassis-worker.py"
    fdcan_arg="{fdcan}"
    chassis_harness=(--socket fdcan)
    chassis_qemu=(-chardev "socket,id=fdcan,path={fdcan},server=on,wait=off"
                  -serial null -serial null -serial null -serial null -serial null -serial null
                  -serial chardev:fdcan)
fi
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

python3 - "$guest_elf" "$qemu_bin" <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import sys
elf, qemu = (Path(value).resolve() for value in sys.argv[1:])
print("WORKER_NATIVE_INPUTS: " + json.dumps({
    "elf_sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
    "qemu_path": str(qemu),
    "qemu_sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(),
    "qemu_version": subprocess.run([str(qemu), "--version"], check=True,
        capture_output=True, text=True, timeout=2).stdout.splitlines()[0],
    "python": sys.version,
}, sort_keys=True))
PY

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket cosim --socket qmp \
    "${chassis_harness[@]}" \
    --python-arg "{cosim}" --python-arg "{qmp}" --python-arg "$worker_entry" \
    --python-arg "$fdcan_arg" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim "${chassis_qemu[@]}" -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
from dm_mc02_test_harness import stop
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

cosim, qmp, worker, fdcan = sys.argv[1:]
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

worker_command = [sys.executable, worker] if fdcan else [worker]
if fdcan:
    worker_command += ["--fdcan", fdcan]
process = subprocess.Popen(worker_command + [
    "--cosim", cosim, "--protocol", "v2",
    "--wait-step-done", "--rate", "1000", "--frames", "1"
], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

primary = None
try:
    stdout, stderr = process.communicate(timeout=5)
except BaseException as error:
    primary = error
    raise
finally:
    try:
        stop(process, process_group=False)
    except BaseException as error:
        if primary is None:
            raise
        primary.add_note(f"worker cleanup failed: {error}")
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
if fdcan:
    import mujoco
    # Independent native observation, with no production profile/getter/unit
    # helper. The initial contact transient is not assumed to be static 1g.
    model = mujoco.MjModel.from_xml_path(str(
        Path(worker).resolve().parents[1] / "models/four_wheel.xml"))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    mujoco.mj_step(model, data)
    mujoco.mj_forward(model, data)
    gyro = data.sensordata[model.sensor("imu_gyro").adr[0]:][:3]
    accel = data.sensordata[model.sensor("imu_accel").adr[0]:][:3]
    # Check the actual float32 wire units against Bosch Rev1.9 p39/p27 half-LSB
    # bins (+/-1000dps and +/-6g); sensortime tick is 39.0625us on p27.
    import struct
    units = tuple(math.degrees(float(v)) for v in gyro) + tuple(
        float(v) / 9.80665 for v in accel)
    units = struct.unpack("<6f", struct.pack("<6f", *units))
    raw = [v if v < 32768 else v - 65536 for v in guest_words[1:7]]
    for expected, code, scale in zip(units, raw, [1000] * 3 + [6] * 3):
        if (not math.isfinite(expected) or abs(expected) >= scale
                or abs(expected - code * scale / 32768) > scale / 65536):
            raise RuntimeError("shared chassis target misses official half-LSB bin")
    if guest_words[10:13] != [0, 0, 25]:
        raise RuntimeError("shared chassis first target raw/time mismatch: %r" % guest_words)
    print("CHASSIS_WORKER_EVIDENCE: " + json.dumps({
        "dt_ns": 1_000_000, "sensor_ticks": 25, "native_time_s": float(data.time),
        "gyro_dps_float32": units[:3], "accel_g_float32": units[3:],
        "gyro_raw": raw[:3], "accel_raw": raw[3:], "DONE": "3/0"}))
status = command("query-status")
if not isinstance(status, dict) or status.get("status") != "running":
    raise RuntimeError("QEMU did not remain running: %r" % status)
command("quit")
control.close()
if not fdcan:
    print("RESULT: QEMU-native external worker integration smoke passed")
PY

if [[ -n "$fdcan_arg" ]]; then
    printf '%s\n' 'RESULT: shared chassis worker native IMU consumer and cleanup passed'
fi

# Keep the ordinary CLI/guest consumption gate above. These direct worker
# consumers isolate only host send availability on an actual QEMU stream.
for prefix in 0 2 59; do
    python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
        --socket cosim --socket qmp \
        "${chassis_harness[@]}" \
        --python-arg "{cosim}" --python-arg "{qmp}" --python-arg "$prefix" \
        --python-arg "$fdcan_arg" -- \
        "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
        -display none -monitor none -S \
        -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
        -serial chardev:cosim "${chassis_qemu[@]}" \
        -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
from dm_mc02_firmware_rtf import deadline_qmp_call
import dm_mc02_sim_worker as worker
import json
from pathlib import Path
import socket
import struct
import sys
import time
from unittest.mock import patch

cosim_path, qmp_path, prefix_text, fdcan_path = sys.argv[1:]
prefix = int(prefix_text)
control = QmpSession(qmp_path, timeout=2)
state = {"steps": 0, "resets": 0, "closes": 0}
state["resets_requested"] = 0
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
            state["resets_requested"] += 1
            if fdcan_path and prefix == 59:
                control.command("system_reset")
                state["resets_requested"] += 1
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
    if path != cosim_path:
        return owned
    sock = BudgetSocket(owned.detach())
    state["sock"] = sock
    return sock


def capture_stream(sock):
    state["stream"] = stream_class(sock)
    return state["stream"]


run_options = {}
arguments = ["worker", "--cosim", cosim_path, "--protocol", "v2", "--frames", "1"]
patches = []
if fdcan_path:
    from sim.fabric import CanFrame
    from sim.apps.four_wheel import four_wheel_simulation_factory, four_wheel_imu

    arguments += ["--fdcan", fdcan_path]
    model = Path(worker.__file__).resolve().parents[1] / "models/four_wheel.xml"
    factory = four_wheel_simulation_factory(model)
    sessions, worlds, devices = [], [], []
    state.update(samples=0, sessions=sessions, lifecycle=[])
    original_ack = worker.wait_v2_reset_ack

    def acknowledged(*args, **kwargs):
        reset = original_ack(*args, **kwargs)
        if (state["steps"], state["samples"], state["closes"]) != (
                len(sessions), len(sessions), len(sessions) - 1):
            raise RuntimeError("RESET_ACK wait advanced, sampled or closed the active graph")
        state.setdefault("ack_observations", []).append({
            "session_id": reset.session_id, "steps": state["steps"],
            "samples": state["samples"], "closes": state["closes"]})
        state["ack_session"] = reset.session_id
        state["lifecycle"].append(["matched_reset_ack", reset.session_id])
        return reset

    def pending_suffix():
        pending = [bytes(view) for view in state["stream"].tx_queue]
        old = state["sock"].old_wire
        expected = [old[prefix:]]
        # Sequential native RESET/ACK pairs may admit an intermediate graph.
        # Its still-blocked STEP must follow the original physical suffix.
        for record in sessions[1:]:
            expected.append(old[:40] + struct.pack("<I", record["session_id"]) + old[44:])
        if pending != expected:
            raise RuntimeError("graph replacement changed the exact pending STEP suffix")
        return [view.hex() for view in pending]

    def acquire(session, mapper, stream, datagrams):
        if sessions:
            if (state["steps"], state["samples"], state["closes"]) != (
                    len(sessions), len(sessions), len(sessions)):
                raise RuntimeError("old graph advanced, sampled or closed during RESET wait: "
                                   + json.dumps({key: value for key, value in state.items()
                                                 if key not in ("sock", "stream")}))
            if state.get("ack_session") != session.session_id:
                raise RuntimeError("new graph acquired before latest matching native RESET_ACK")
            state.setdefault("pending_at_reset", []).append(pending_suffix())
            state["resets"] += 1
            try:
                worlds[-1].get_imu("imu")
            except RuntimeError:
                pass
            else:
                raise RuntimeError("old closed World remained readable")
        state["lifecycle"].append(["acquire", session.session_id])
        return factory(session, mapper, stream, datagrams)

    def started(simulation):
        world = simulation.components["world"]
        motors = tuple(simulation.components[f"motor{ident}"].device for ident in range(1, 5))
        if any(world is other for other in worlds) or any(
                motor is old for motor in motors for old in devices):
            raise RuntimeError("RESET reused an owned native World or device")
        if world.time_s != 0 or any(motor.current_raw for motor in motors):
            raise RuntimeError("fresh graph retained time or control")
        if any(world.get_joint_state(f"wheel_{corner}").position_rad != 0
               for corner in ("fl", "fr", "rl", "rr")):
            raise RuntimeError("fresh graph retained joint state")
        record = {"session_id": simulation.session.session_id,
                  "origin_ns": simulation.session.t_sim_ns,
                  "initial_native_time_s": world.time_s,
                  "initial_currents": [motor.current_raw for motor in motors]}
        sessions.append(record)
        worlds.append(world)
        devices.extend(motors)
        state["lifecycle"].append(["started", simulation.session.session_id])
        original_step, original_close = world.step, world.close

        def step(context):
            result = original_step(context)
            state["steps"] += 1
            record["native_time_s"] = world.time_s
            record["currents"] = [motor.current_raw for motor in motors]
            record["wheel_velocities_rad_s"] = [world.get_joint_state(
                f"wheel_{corner}").velocity_rad_s for corner in ("fl", "fr", "rl", "rr")]
            if world.time_s != .001 or record["currents"] != [4096] * 4 or not all(
                    velocity > 0 for velocity in record["wheel_velocities_rad_s"]):
                raise RuntimeError("host current fixture failed to advance the actual shared World once")
            state["lifecycle"].append(["step", context.session_id])
            return result

        def close():
            original_close()
            state["closes"] += 1
            state["lifecycle"].append(["close", simulation.session.session_id])

        world.step, world.close = step, close
        # Explicit host fixture, not evidence of guest-origin CAN commands.
        simulation.schedule_input("transport.qemu_rx", CanFrame(0x200,
            data=struct.pack(">hhhh", 4096, 4096, 4096, 4096),
            timestamp_ns=simulation.session.t_sim_ns))
        if len(sessions) == state["resets_requested"] + 1 and len(sessions) > 1:
            # This graph has not produced its first STEP yet.
            current = sessions.pop()
            try:
                pending_suffix()
            finally:
                sessions.append(current)
            state["sock"].release = True

    def sample(simulation):
        value = four_wheel_imu(simulation)
        state["samples"] += 1
        state["lifecycle"].append(["sample", simulation.session.session_id])
        return value

    run_options = dict(simulation_factory=acquire, imu_reader=sample, on_session_started=started)
    patches.append(patch.object(worker, "wait_v2_reset_ack", acknowledged))
else:
    engine = ObservedPlant(0)
    arguments += ["--backend", "test:factory", "--motor-count", "0"]
    patches.append(patch.object(worker, "load_factory", lambda spec: lambda args: engine))

with patch.object(sys, "argv", arguments):
    args = worker.parse_args()
from contextlib import ExitStack
with ExitStack() as stack:
    for context in patches + [patch.object(worker, "connect_unix", connect),
            patch.object(worker, "StepFramedSocket", capture_stream),
            patch.object(worker.step_protocol, "generate_session_id", lambda: 123)]:
        stack.enter_context(context)
    if worker.run(args, **run_options) != 0:
        raise RuntimeError("pending STEP/reset worker failed")

after = diagnostics()
before = state["before"]
expected_graphs = 1 + state["resets_requested"] if fdcan_path else 2
if (state["steps"], state["resets"], state["closes"]) != (
        expected_graphs, state["resets_requested"], expected_graphs if fdcan_path else 1):
    raise RuntimeError("worker advanced or closed an incorrect number of times")
if fdcan_path and (state["samples"] != expected_graphs or len(sessions) != expected_graphs):
    raise RuntimeError("RESET or ACK waits repeated native IMU sampling")
if (state["sock"].fileno() != -1 or state["sock"].close_calls != 1 or
        state["stream"].tx_pending):
    raise RuntimeError("worker did not drain and close its transport")
if (after["rx_frames"] != before["rx_frames"] + 1 or
        after["rx_bad_frames"] != before["rx_bad_frames"] + state["resets_requested"] or
        after["rx_dropped_bytes"] != before["rx_dropped_bytes"] + 104 * state["resets_requested"] or
        after["rx_buffered"] != 0):
    raise RuntimeError("native RX did not reject every complete stale STEP")
output = bytes(state["sock"].output)
initial_reset = struct.pack("<IIHHIQQQI", 40, 0x32434D44, 2, 1, 0,
                            0, 0, 0, 123)
old = state["sock"].old_wire
step_output = output[len(initial_reset):]
steps = [step_output[index:index + 104] for index in range(0, len(step_output), 104)]
if (output[:len(initial_reset)] != initial_reset or len(old) != 104
        or len(steps) != expected_graphs or any(len(item) != 104 for item in steps)):
    raise RuntimeError("pending STEP/reset output order or frame size is wrong")
for index, item in enumerate(steps):
    if struct.unpack_from("<IIHHIQQQI", item) != (
            100, 0x32434D44, 2, 2, 60, 1, 1000000, 1000000, 123 + index):
        raise RuntimeError("old/new STEP headers did not use their own sessions")
    if item[44:] != old[44:]:
        raise RuntimeError("repeatable first-target payload changed across RESET")
report = {key: value for key, value in state.items() if key not in ("sock", "stream")}
report.update(prefix=prefix, after=after, output_hex=output.hex(),
              socket_close_calls=state["sock"].close_calls,
              backend="shared-mujoco-world" if fdcan_path else "null-fixture",
              can_stimulus="explicit host current fixture" if fdcan_path else None)
print("WORKER_PENDING_RESET_EVIDENCE: " + json.dumps(report, sort_keys=True))
control.close()
print("RESULT: actual worker pending STEP/reset native gate passed (prefix=%d)" % prefix)
PY
done
