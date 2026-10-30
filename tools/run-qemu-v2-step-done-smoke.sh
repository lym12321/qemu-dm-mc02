#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir:$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
mujoco_source=""
source_qemu_args=()
if [[ $# -ne 0 ]]; then
    if [[ $# -ne 2 || $1 != --mujoco-source ||
          ( $2 != rest && $2 != turn ) ]]; then
        printf 'usage: %s [--mujoco-source rest|turn]\n' "$0" >&2
        exit 2
    fi
    mujoco_source=$2
    # Prepare the native plant before the guest's one-shot read window opens.
    source_qemu_args=(-S)
fi

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-v2-step-done.XXXXXX")
guest_elf="$run_dir/readback.elf"
trap 'rm -rf -- "$run_dir"' EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.c"

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp --socket cosim \
    --wait-socket cosim --wait-socket qmp \
    --python-arg "{cosim}" --python-arg "{qmp}" \
    --python-arg "$root_dir" --python-arg "$mujoco_source" -- \
    "$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults \
    "${source_qemu_args[@]}" \
    -display none -monitor none \
    -qmp "unix:{qmp},server=on,wait=off" \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim <<'PY'
from dm_mc02_qmp import QmpSession
import json
from pathlib import Path
import re
import socket
import struct
import sys
import time

COSIM, QMP, ROOT, SOURCE_MODE = sys.argv[1:]
MAGIC = 0x32434D44
RESET, STEP, STEP_ACK, RESET_ACK, STEP_DONE = 1, 2, 3, 5, 6
HEADER = 36
MARKER = 0x52424B31


def exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise RuntimeError("socket closed")
        result.extend(chunk)
    return bytes(result)


def read_frame(sock):
    body_len = struct.unpack("<I", exact(sock, 4))[0]
    return exact(sock, body_len)


def read_v2(sock, wanted_kind=None):
    while True:
        body = read_frame(sock)
        if (len(body) >= 8 and struct.unpack_from("<H", body, 4)[0] == 2 and
                (wanted_kind is None or
                 struct.unpack_from("<H", body, 6)[0] == wanted_kind)):
            return body


def v2_frame(kind, step_id, timestamp, payload=b"", dt=0):
    body = struct.pack("<IHHIQQQI", MAGIC, 2, kind, len(payload),
                       step_id, timestamp, dt, 0) + payload
    return struct.pack("<I", len(body)) + body


def fields(body):
    return (struct.unpack_from("<H", body, 6)[0],
            struct.unpack_from("<Q", body, 12)[0])


def payload(body):
    length = struct.unpack_from("<I", body, 8)[0]
    return body[4 + HEADER:4 + HEADER + length]


def release(owners):
    """Attempt every owned close; retain the first runtime/cleanup error."""
    primary = sys.exc_info()[1]
    failure = None
    for name, owner in owners:
        if owner is None:
            continue
        try:
            owner.close()
        except BaseException as exc:
            if primary is not None:
                primary.add_note(f"{name} close failed: {exc}")
            elif failure is None:
                failure = exc
            else:
                failure.add_note(f"{name} close failed: {exc}")
    if failure is not None:
        raise failure


def prepare_mujoco_source(mode):
    from types import SimpleNamespace
    from sim.api import NodeSpec, NodeDescription, Topology
    from sim.runtime import Simulation
    from sim.devices.bmi088 import imu_to_bmi088_units
    from sim.devices.dji import M3508Device, M3508JointPlant
    from sim.fabric import CanBus, CanFrame
    from sim.fabric.runtime import BorrowedCanDispatcher
    from sim.physics import MujocoPhysics
    from dm_mc02_step_protocol import ImuSampleV2, encode_imu_sample_v2

    joints = {f"wheel_{corner}": f"drive_{corner}"
              for corner in ("fl", "fr", "rl", "rr")}
    physics = MujocoPhysics.from_xml_path(
        Path(ROOT) / "models" / "four_wheel.xml", joints=joints,
        imus={"imu": ("imu_gyro", "imu_accel")})
    bus = CanBus()

    class Host:
        def receive(self, frame):
            pass  # The shared adapter owns rotor feedback; this fixture sends.

    host = Host()
    bus.attach(host, "fixture-host")
    devices = []
    owner = None
    try:
        for motor_id in (1, 2, 3, 4):
            devices.append(M3508Device(bus, motor_id))
        owner = M3508JointPlant(physics, dict(zip(devices, joints)))
        plant = owner
        owner = Simulation(Topology((NodeSpec("bus", "bus"), NodeSpec("plant", "plant"))), {
            "bus": SimpleNamespace(
                describe=lambda spec: NodeDescription(capabilities={"dispatch"}),
                create=lambda spec, context: BorrowedCanDispatcher(bus)),
            "plant": SimpleNamespace(
                describe=lambda spec: NodeDescription(capabilities={"step"}),
                create=lambda spec, context: plant),
        })
        owner.start()

        def advance(command):
            bus.send(host, CanFrame(0x200, dlc=8, data=command,
                                   timestamp_ns=owner.t_sim_ns))
            owner.step(1_000_000)

        for _ in range(500):
            advance(bytes.fromhex("0000 0000 0000 0000"))
        if mode == "turn":
            for _ in range(500):
                advance(bytes.fromhex("0015 006b 0015 006b"))
        sample = physics.get_imu("imu")  # Completed native site-local SI state.
        gyro, accel = imu_to_bmi088_units(sample)
        # Peer sample_id is metadata. Native link admission alone assigns the
        # retained, nonzero source identity used by each real die's raw read.
        imu = ImuSampleV2(gyro, accel, (0.0,) * 3, (0.0,) * 3,
                          owner.t_sim_ns, 1)
        encoded = encode_imu_sample_v2(imu)
        return sample, physics.time_s, owner.t_sim_ns, encoded
    finally:
        if owner is not None:
            release((("plant/coordinator", owner),))
        else:
            release(tuple((f"M3508 {device.motor_id}", device)
                          for device in reversed(devices)))


def check_source_readback(source, words):
    sample, native_time_s, target_ns, encoded = source
    wire = struct.unpack("<12fQI", encoded)
    if any(value > 0xffff for value in words[1:7]):
        raise RuntimeError("guest raw sample contains non-byte data")
    raw = tuple(struct.unpack("<h", struct.pack("<H", value))[0]
                for value in words[1:7])
    # Bosch BMI088 datasheet Rev1.9 (Jan2024), p39 §5.5.5: gyro range1 gives
    # ±1000dps / 32.768LSB/dps; p30 §5.3.11 and p27 §5.3.4: accel range1
    # gives ±6g / 32768. Fresh first sample with zero biases/noise only.
    # These are fixture range/rounding bins, not silicon timing
    # or filter validation. Check the actual float32 wire values independently
    # of the producer quantizer. At a midpoint only the two adjacent codes fit.
    for name, values, codes, full_scale in (
            ("gyro_dps", wire[:3], raw[:3], 1000.0),
            ("accel_g", wire[3:6], raw[3:], 6.0)):
        lsb = full_scale / 32768
        for axis, (value, code) in enumerate(zip(values, codes)):
            if not -full_scale < value < full_scale:
                raise RuntimeError(f"source {name}[{axis}] is saturated: {value}")
            if abs(value - code * lsb) > lsb / 2:
                raise RuntimeError(
                    f"{name}[{axis}] raw={code} misses wire={value} half-LSB bin")
    if SOURCE_MODE == "turn":
        if sample.gyro_rad_s[2] <= 0 or wire[2] <= 0 or raw[2] <= 0:
            raise RuntimeError("native turning gyroZ was not read as positive rawZ")
    elif max(abs(value) for value in sample.gyro_rad_s) >= 1e-6 or any(raw[:3]):
        raise RuntimeError("rest source did not produce near-zero native/raw gyro")
    print("PROFILE: " + json.dumps({
        "mode": SOURCE_MODE, "settle_ns": 500_000_000,
        "turn_ns": 500_000_000 if SOURCE_MODE == "turn" else 0,
        "can_id": "0x200", "motor_ids": [1, 2, 3, 4],
        "turn_current_raw": [21, 107, 21, 107] if SOURCE_MODE == "turn" else None,
        "plant_time_s": native_time_s, "plant_target_ns": target_ns,
        "sensor_time_ns": wire[12], "host_step_ns": 1, "host_dt_ns": 1,
        "peer_sample_id": wire[13], "source_identity_owner": "QEMU co-sim link",
        "clock_boundary": "plant and transport epochs; no CPU/plant barrier",
        "gyro_rad_s": sample.gyro_rad_s, "accel_m_s2": sample.accel_m_s2,
        "gyro_dps_float32": wire[:3], "accel_g_float32": wire[3:6],
        "gyro_raw": raw[:3], "accel_raw": raw[3:],
        "raw_u16": words[1:7], "sensor_ticks_u24": words[12],
        "range_contract": "fresh first sample; +/-1000 dps and +/-6 g",
        "done": "consumed=3 missing=0; identical replay",
    }, sort_keys=True))


def run_protocol(cosim, qmp, source):
    # Wait until the guest has configured both BMI088 dies and is ready to read the
    # next accepted sample. The marker is in DTCM and is observable through QMP.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        response = qmp.command("human-monitor-command", {
            "command-line": "xp /8wx 0x20000000"}) or ""
        words = [int(value, 16) for value in re.findall(
            r"0x([0-9a-fA-F]{8})", response)]
        if len(words) >= 8 and words[7] == 0x434F4E46:
            break
        time.sleep(0.01)
    else:
        raise RuntimeError("guest did not finish BMI088 configuration")

    if source is None:
        step_wire = v2_frame(
            STEP, 7, 1, struct.pack("<12f", 1.0, -2.0, 3.0,
                                      0.5, -1.0, 1.0,
                                      0.0, 0.0, 0.0, 0.0, 0.0, 0.0) +
            struct.pack("<QI", 1, 1), dt=1)
    else:
        from dm_mc02_step_protocol import StepFrame, StepHeader, encode_step_frame
        encoded_imu = source[3]
        step_wire = encode_step_frame(StepFrame(
            StepHeader(kind=STEP, payload_len=len(encoded_imu), step_id=7,
                       t_sim_ns=1, dt_ns=1), encoded_imu))
    cosim.sendall(step_wire)

    ack = read_v2(cosim, STEP_ACK)
    if fields(ack) != (STEP_ACK, 7) or struct.unpack_from("<I", payload(ack))[0] != 0:
        raise RuntimeError("STEP_ACK was not successful: %r status=%d" %
                           (fields(ack), struct.unpack_from("<I", payload(ack))[0]))

    done = read_v2(cosim, STEP_DONE)
    if fields(done) != (STEP_DONE, 7):
        raise RuntimeError("STEP_DONE step identity mismatch")
    done_values = struct.unpack("<IIIII", payload(done))
    if done_values != (0, 3, 0, 0, 0):
        raise RuntimeError("unexpected STEP_DONE payload: %r" % (done_values,))

    # Replaying an already consumed STEP is the recovery path when STEP_DONE was
    # lost. It must return both phases without another guest sensor transaction.
    cosim.sendall(step_wire)
    retry_ack = read_v2(cosim, STEP_ACK)
    retry_done = read_v2(cosim, STEP_DONE)
    if (fields(retry_ack) != (STEP_ACK, 7) or
            fields(retry_done) != (STEP_DONE, 7) or payload(retry_done) != payload(done)):
        raise RuntimeError("consumed STEP was not replayed idempotently")

    while True:
        response = qmp.command("human-monitor-command", {
            "command-line": "xp /13wx 0x20000000"}) or ""
        words = [int(value, 16) for value in re.findall(
            r"0x([0-9a-fA-F]{8})", response)]
        if source is None:
            break
        # DONE precedes the guest's stores of raw readback. Its final volatile
        # sensor-time store witnesses completion: Bosch's 39.0625us tick gives
        # these literal counts for the fixture's .5s/1s native targets. Reuse
        # the CONF deadline; QMP round trips do not open a new waiting period.
        expected_ticks = 12800 if SOURCE_MODE == "rest" else 25600
        if len(words) >= 13 and words[12] == expected_ticks:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("guest did not finish native source raw readback")
    if len(words) < 13 or words[0] != MARKER or words[10] != 0 or words[11] != 0:
        raise RuntimeError("guest did not consume both BMI088 raw bursts")
    if source is not None:
        check_source_readback(source, words)
        return  # The single native source/consumer gate is complete.
    # The guest completed its real raw reads and now sleeps. A distinct wire sample
    # with the same sensor timestamp is rejected by both configured 100 Hz dies.
    # Admission succeeds, but this input must not occupy a token forever.
    def imu_step(step_id, host_ns, sensor_ns):
        data = struct.pack("<12f", 1.0, -2.0, 3.0, 0.5, -1.0, 1.0,
                           0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return v2_frame(STEP, step_id, host_ns,
                        data + struct.pack("<QI", sensor_ns, 1), dt=1)


    def response(wanted_kind, wanted_step):
        body = read_v2(cosim)
        # Telemetry may be published between control responses.
        while fields(body)[0] not in (STEP_ACK, STEP_DONE):
            body = read_v2(cosim)
        if fields(body) != (wanted_kind, wanted_step):
            raise RuntimeError("control response order mismatch: %r" % (fields(body),))
        return body


    rejected = imu_step(8, 2, 1)
    cosim.sendall(rejected)
    if struct.unpack_from("<I", payload(response(STEP_ACK, 8)))[0] != 0:
        raise RuntimeError("ODR-rejected input was not admitted")
    failed = response(STEP_DONE, 8)
    if struct.unpack("<IIIII", payload(failed)) != (2, 0, 3, 0, 0):
        raise RuntimeError("ODR-rejected input did not terminate with missing dies")
    cosim.sendall(rejected)
    response(STEP_ACK, 8)
    if payload(response(STEP_DONE, 8)) != payload(failed):
        raise RuntimeError("failed terminal replay changed bytes")

    # An accepted raw source remains unread. A later source replaces both raw
    # registers; only the older STEP may terminate, after the newer ACK.
    cosim.sendall(imu_step(9, 3, 10_000_001))
    response(STEP_ACK, 9)
    cosim.sendall(imu_step(10, 4, 20_000_001))
    response(STEP_ACK, 10)
    if struct.unpack("<IIIII", payload(response(STEP_DONE, 9))) != (2, 0, 3, 0, 0):
        raise RuntimeError("superseded raw source remained pending")

    # Wire RESET invalidates tokens but leaves physical sensor bytes. Reusing peer
    # sample/STEP numbers cannot attribute those bytes to the new rejected input.
    cosim.sendall(v2_frame(RESET, 0, 0))
    if fields(read_v2(cosim, RESET_ACK)) != (RESET_ACK, 0):
        raise RuntimeError("restart RESET_ACK mismatch")
    cosim.sendall(imu_step(7, 1, 20_000_001))
    response(STEP_ACK, 7)
    if struct.unpack("<IIIII", payload(response(STEP_DONE, 7))) != (2, 0, 3, 0, 0):
        raise RuntimeError("retained old bytes were assigned to a reused peer ID")


def main():
    source = prepare_mujoco_source(SOURCE_MODE) if SOURCE_MODE else None
    cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    qmp = None
    try:
        cosim.settimeout(3.0)
        cosim.connect(COSIM)
        read_frame(cosim)  # initial v1 telemetry
        cosim.sendall(v2_frame(RESET, 0, 0))
        if fields(read_v2(cosim)) != (RESET_ACK, 0):
            raise RuntimeError("v2 RESET_ACK mismatch")
        qmp = QmpSession(QMP, timeout=2.0)
        if source is not None:
            qmp.command("cont")
        run_protocol(cosim, qmp, source)
    finally:
        release((("QMP", qmp), ("co-sim", cosim)))


main()
PY

# The shared harness has finished QEMU/test-process cleanup before success.
if [[ -n "$mujoco_source" ]]; then
    printf 'RESULT: QEMU v2 STEP_DONE MuJoCo %s raw-consumer smoke passed\n' "$mujoco_source"
else
    printf '%s\n' 'RESULT: QEMU v2 STEP_DONE consume/reject/overwrite/restart smoke passed'
fi
