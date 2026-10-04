#!/usr/bin/env python3
"""Test-only observations of the actual trobot/shared-chassis consumer.

Production worker/profile owners advance the plant. UART observations select
host windows; CAN timestamps and ACK/DONE do not form a CPU/plant barrier.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import dm_mc02_sim_worker as worker
from dm_mc02_firmware_rtf import deadline_qmp_call, read_iwdg_timeouts
from dm_mc02_qmp import QmpSession
from dm_mc02_test_harness import stop
from dm_mc02_trobot_business import _identity
from sim.robots.four_wheel import FourWheelRuntime, create_four_wheel_plant
from sim.transports import connect_unix

STEPS = 7500
JOINTS = ("wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr")
STAGES = (("chassis.warm", 0, 0), ("chassis.forward", 500, 500),
          ("chassis.turn", 200, 600), ("chassis.stop", 0, 0),
          ("chassis.disable", 0, 0))
REQUIRED = {"control.rx", "reset.poweron", "bsp.init", "task.stack",
            "chassis.warm.gravity", "chassis.disable", "chassis.can"}
REQUIRED.update(f"{phase}.{case}" for phase, _, _ in STAGES[:4]
                for case in ("samples", "fresh", "speed.id1", "speed.id2",
                             "speed.id3", "speed.id4", "imu", "gyro"))
LIMITS = [
    "Fixed uncalibrated toy four-wheel model; physical calibration SKIP.",
    "Production worker: 7500 x 1 ms, realtime pacing, ACK-only consumption policy.",
    "DONE is an observed advisory response; not every source is consumed.",
    "CAN tx_queued records transport admission, not guest reception or consumption.",
    "UART read/ack host windows have polling/formatting quantization; no CPU/plant barrier.",
    "This functional consumer gate makes no sustained performance claim.",
]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def initialize(output, elf=None, qemu=None):
    out = Path(output)
    if out.exists() and any(out.iterdir()):
        raise ValueError("refusing to overwrite existing chassis artifacts: " + str(out))
    out.mkdir(parents=True, exist_ok=True)
    errors = []
    if elf is not None and not Path(elf).is_file():
        errors.append("ChassisTest ELF missing: " + elf)
    if qemu is not None and not os.access(qemu, os.X_OK):
        errors.append("project QEMU missing or not executable: " + qemu)
    write_json(out / "report.json", dict(result="failed", errors=errors or ["consumer not finished"],
               missing=["firmware", "worker", "trace"], cases=[], limits=LIMITS))
    if errors:
        raise ValueError("; ".join(errors))


class Trace:
    """Bound fixed-profile observations without owning execution or codec."""

    def __init__(self, path):
        self.file = Path(path).open("w")
        self.counts = Counter()

    def record(self, event, **fields):
        group = "step" if event == "step" else "can" if event.startswith("can_") else "control"
        self.counts[group] += 1
        if self.counts[group] > {"step": STEPS, "can": 75000, "control": 20000}[group]:
            raise BufferError("chassis observation capacity exhausted: " + group)
        self.file.write(json.dumps(dict(event=event, wall_time_ns=time.monotonic_ns(),
                                        **fields), allow_nan=False) + "\n")

    def close(self):
        self.file.close()


def run_worker(cosim, can, trace_path, ready_path):
    def interrupted(sig, _frame):
        raise RuntimeError(f"chassis worker interrupted by signal {sig}")

    signal.signal(signal.SIGTERM, interrupted)
    trace = Trace(trace_path)
    plant = None
    handed_off = False
    primary = None

    class ObservedCan(worker.CanStream):
        def receive(self):
            frames = super().receive()
            for ident, flags, dlc, stamp, data in frames:
                trace.record("can_rx", can_id=ident, flags=flags, dlc=dlc,
                             timestamp_ns=stamp, data=data.hex())
            return frames

        def send(self, can_id, flags, dlc, timestamp_ns, data):
            super().send(can_id, flags, dlc, timestamp_ns, data)
            trace.record("can_tx_queued", can_id=can_id, flags=flags, dlc=dlc,
                         timestamp_ns=timestamp_ns, data=data.hex())

    class ObservedStep(worker.StepFramedSocket):
        def receive(self):
            already_decoded = bool(self.pending_frames)
            frames = super().receive()
            if not already_decoded:
                for frame in frames:
                    header = frame.header
                    fields = dict(kind=header.kind, step_id=header.step_id,
                                  timestamp_ns=header.t_sim_ns, session_id=header.session_id,
                                  payload=frame.payload.hex())
                    if header.kind == worker.step_protocol.STEP_DONE:
                        done = worker.step_protocol.decode_step_done_payload(frame.payload)
                        fields.update(status=done.status, consumed_mask=done.consumed_mask,
                                      missing_mask=done.missing_mask, queue_depth=done.queue_depth,
                                      dropped_count=done.dropped_count)
                    elif header.kind == worker.step_protocol.STEP_ACK:
                        ack = worker.step_protocol.decode_step_ack_payload(frame.payload)
                        fields.update(status=ack.status, queue_depth=ack.queue_depth,
                                      dropped_count=ack.dropped_count)
                    trace.record("control", **fields)
            return frames

    class ObservedRuntime(FourWheelRuntime):
        def reset(self, session_id, timestamp_ns, mapper):
            super().reset(session_id, timestamp_ns, mapper)
            trace.record("reset", session_id=session_id, timestamp_ns=timestamp_ns,
                         qemu_origin_ns=mapper.qemu_origin_ns,
                         worker_origin_ns=mapper.worker_origin_ns)
            Path(ready_path).write_text("runtime reset completed\n")

        def get_imu(self):
            sample = super().get_imu()
            physics = self.plant.physics
            pose = physics.get_body_pose("chassis")
            joints = [physics.get_joint_state(name) for name in JOINTS]
            trace.record("step", timestamp_ns=self.t_sim_ns, plant_time_s=physics.time_s,
                         position_m=pose.position_m, quaternion_wxyz=pose.quaternion_wxyz,
                         joint_position_rad=[joint.position_rad for joint in joints],
                         joint_velocity_rad_s=[joint.velocity_rad_s for joint in joints],
                         current_raw=[device.current_raw for device in self.plant.devices],
                         gyro_rad_s=sample.gyro_rad_s, accel_m_s2=sample.accel_m_s2)
            return sample

    try:
        plant = create_four_wheel_plant(ROOT / "models/four_wheel.xml")

        def factory(stream, socketcan):
            nonlocal handed_off
            runtime = ObservedRuntime(plant, stream, socketcan)
            handed_off = True
            return runtime

        args = worker.parse_args(["--cosim", cosim, "--fdcan", can, "--protocol", "v2",
                                  "--rate", "1000", "--frames", str(STEPS), "--realtime"])
        with patch.object(worker, "CanStream", ObservedCan), \
                patch.object(worker, "StepFramedSocket", ObservedStep):
            return worker.run(args, plant_factory=factory)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        for name, action in (("untransferred plant", lambda: plant.close()
                             if plant is not None and not handed_off else None),
                             ("trace", trace.close)):
            try:
                action()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"{name} cleanup failed: {exc}")
        if primary is not None and sys.exc_info()[1] is None:
            raise primary


def _yaw(row):
    w, x, y, z = row["quaternion_wxyz"]
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def check_case(row):
    """Check literal UART fields independently of firmware PASS labels."""
    status, name, actual, expected = (row[k] for k in ("status", "name", "actual", "expected"))
    if any(type(v) is not int or not 0 <= v <= 0xffffffff for v in (actual, expected)):
        raise ValueError("firmware fields must fit uint32")
    if status == "SKIP":
        if (name, actual, expected) != ("plant.physics", 0, 0):
            raise ValueError("unexpected firmware SKIP: " + name)
        return
    if status != "PASS" or name not in REQUIRED:
        raise ValueError("firmware failure or unexpected coverage: " + name)
    exact = {"control.rx": 0, "reset.poweron": 0x800000, "bsp.init": 0,
             "chassis.disable": 1, "chassis.can": 0}
    if name in exact:
        ok = actual == expected == exact[name]
    elif name == "task.stack":
        ok = expected == 64 and actual >= 64
    elif name.endswith(".samples"):
        ok = expected == 100 and actual >= 100
    elif name.endswith(".fresh"):
        ok = expected == 50 and actual < 50
    elif ".speed.id" in name:
        limit = 80 if name.startswith("chassis.stop.") else 150
        ok = expected == limit and actual <= limit
    elif name.endswith(".imu"):
        ok = actual == expected and actual >= 100
    elif name.endswith(".gyro"):
        signed = actual if actual < 0x80000000 else actual - 0x100000000
        # Integer truncation puts a valid >.020 rad/s float into code20 too.
        # PASS attests the firmware's strict float check; native gyro is gated
        # independently. This integer field alone gives the [.020,.021) bin.
        ok = expected == 20 and (signed >= 20 if name == "chassis.turn.gyro" else abs(signed) <= 20)
    else:
        ok = name == "chassis.warm.gravity" and expected == 100 and actual <= 100
    if not ok:
        raise ValueError("firmware report fields disagree: " + name)


def control_coverage(rows, *, expected_steps=STEPS):
    """Report observations and absent responses, including unsuccessful runs."""
    ack = [r for r in rows if r.get("event") == "control" and r.get("kind") == 3]
    done = [r for r in rows if r.get("event") == "control" and r.get("kind") == 6]
    expected = set(range(1, expected_steps + 1))
    successful_ack = {r["step_id"] for r in ack if r["status"] == 0}
    observed_done = {r["step_id"] for r in done}
    return dict(expected_steps=expected_steps, ack_observed_steps=len(successful_ack),
                ack_missing_step_ids=sorted(expected - successful_ack),
                done_observed_steps=len(observed_done),
                done_missing_step_ids=sorted(expected - observed_done),
                duplicate_ack_responses=len(ack) - len({r["step_id"] for r in ack}),
                duplicate_done_responses=len(done) - len(observed_done),
                done_status_masks={str(key): value for key, value in Counter(
                    (r["status"], r["consumed_mask"], r["missing_mask"]) for r in done).items()},
                policy="advisory DONE; absent responses do not imply consumption")


def analyze_trace(rows, stages, *, expected_steps=STEPS):
    """Independent public-state/wire gates; host windows are observation only."""
    if not rows or any(type(row.get("wall_time_ns")) is not int for row in rows):
        raise ValueError("missing or invalid host observation time")
    if any(b["wall_time_ns"] < a["wall_time_ns"] for a, b in zip(rows, rows[1:])):
        raise ValueError("host trace moved backwards")
    steps = [row for row in rows if row["event"] == "step"]
    resets = [row for row in rows if row["event"] == "reset"]
    if len(steps) != expected_steps or len(resets) != 1:
        raise ValueError("incomplete step trace or unexpected runtime reset")
    for index, row in enumerate(steps, 1):
        if (not math.isfinite(row["plant_time_s"]) or
                row["timestamp_ns"] != index * 1_000_000 or
                abs(row["plant_time_s"] - index * .001) > 1e-8):
            raise ValueError("trace does not contain each completed 1 ms target")
        for key, count in (("position_m", 3), ("quaternion_wxyz", 4),
                           ("joint_position_rad", 4), ("joint_velocity_rad_s", 4),
                           ("gyro_rad_s", 3), ("accel_m_s2", 3)):
            if len(row[key]) != count or not all(math.isfinite(v) for v in row[key]):
                raise ValueError("invalid public target state: " + key)
    if [(r["name"], r["actual"], r["expected"]) for r in stages] != list(STAGES):
        raise ValueError("missing or unexpected actual UART stages")
    if any(b["wall_time_ns"] <= a["wall_time_ns"] for a, b in zip(stages, stages[1:])):
        raise ValueError("UART stages do not advance host observation time")
    done = [r for r in rows if r["event"] == "control" and r.get("kind") == 6]
    ack = [r for r in rows if r["event"] == "control" and r.get("kind") == 3]
    for row in ack + done:
        ident = row["step_id"]
        if (type(ident) is not int or not 1 <= ident <= expected_steps or
                row["session_id"] != resets[0]["session_id"] or
                row["timestamp_ns"] != ident * 1_000_000):
            raise ValueError("control response does not identify a completed worker target")
    coverage = control_coverage(rows, expected_steps=expected_steps)
    if coverage["ack_missing_step_ids"]:
        raise ValueError("successful ACK coverage is incomplete")
    commands = [r for r in rows if r["event"] == "can_rx"]
    for row in commands:
        data = bytes.fromhex(row["data"])
        if (row["can_id"] != 0x200 or row["flags"] != 0 or row["dlc"] != 8 or
                len(data) != 8 or any(abs(v) > 512 for v in struct.unpack(">hhhh", data))):
            raise ValueError("actual four-slot C620 command violates fixed profile")
    origin = resets[0]
    targets = {origin["qemu_origin_ns"] + row["timestamp_ns"] -
               origin["worker_origin_ns"]: row for row in steps}
    feedback = [r for r in rows if r["event"] == "can_tx_queued"]
    by_id = Counter()
    for row in feedback:
        data = bytes.fromhex(row["data"])
        index = row["can_id"] - 0x201
        if (index not in range(4) or row["flags"] != 0 or row["dlc"] != 8 or
                len(data) != 8 or row["timestamp_ns"] not in targets):
            raise ValueError("invalid or unmatched target-time rotor feedback")
        target = targets[row["timestamp_ns"]]
        encoder, rpm, current = struct.unpack(">Hhh", data[:6])
        position = target["joint_position_rad"][index] * (3591 / 187)
        velocity = target["joint_velocity_rad_s"][index] * (3591 / 187)
        width = math.tau / 8192
        # Circular bins include the floating-point 8191/0 wrap boundary.
        if (not 0 <= encoder <= 8191 or
                abs(math.remainder(position - (encoder + .5) * width,
                                   math.tau)) > width / 2 + 1e-12 or
                abs(velocity - rpm * math.pi / 30) > math.pi / 60 + 1e-12 or
                current != target["current_raw"][index] or data[6:] != b"\x19\x00"):
            raise ValueError("feedback differs from independent rotor quantization bins")
        by_id[row["can_id"]] += 1
    if set(by_id) != {0x201, 0x202, 0x203, 0x204}:
        raise ValueError("four feedback IDs were not observed")
    phases = []
    for start, end in zip(stages, stages[1:]):
        low, high = end["wall_time_ns"] - 250_000_000, end["wall_time_ns"]
        tail = [r for r in steps if low <= r["wall_time_ns"] < high]
        phase_done = [r for r in done if low <= r["wall_time_ns"] < high]
        full_done = [r for r in phase_done if (r["status"], r["consumed_mask"],
                                              r["missing_mask"]) == (0, 3, 0)]
        phase_commands = [r for r in commands if start["wall_time_ns"] <=
                          r["wall_time_ns"] < high]
        phase_feedback = [r for r in feedback if low <= r["wall_time_ns"] < high]
        if low < start["wall_time_ns"] or len(tail) < 100 or not full_done or not phase_commands:
            raise ValueError("missing tail state/command/real full-consumed DONE: " + start["name"])
        if {r["can_id"] for r in phase_feedback} != {0x201, 0x202, 0x203, 0x204}:
            raise ValueError("phase lacks four queued feedback IDs: " + start["name"])
        mean = [sum(r["joint_velocity_rad_s"][i] for r in tail) / len(tail) for i in range(4)]
        gyro = sum(r["gyro_rad_s"][2] for r in tail) / len(tail)
        delta_x = tail[-1]["position_m"][0] - tail[0]["position_m"][0]
        delta_yaw = math.remainder(_yaw(tail[-1]) - _yaw(tail[0]), math.tau)
        name = start["name"]
        if name == "chassis.forward" and (delta_x <= 0 or min(mean) <= 0):
            raise ValueError("forward native +X/four-wheel trend missing")
        if name == "chassis.turn" and (delta_yaw <= 0 or gyro <= .02 or
                (mean[1] + mean[3]) <= (mean[0] + mean[2])):
            raise ValueError("turn native +yaw/gyro/right-wheel trend missing")
        if name in ("chassis.warm", "chassis.stop") and max(map(abs, mean)) > .08:
            raise ValueError("stationary tail wheel-speed gate failed")
        if name in ("chassis.forward", "chassis.turn"):
            slots = [struct.unpack(">hhhh", bytes.fromhex(r["data"])) for r in phase_commands]
            if not all(any(v[i] != 0 for v in slots) for i in range(4)):
                raise ValueError("motion phase lacks actual commands to each motor")
        phases.append(dict(name=name, samples=len(tail), host_window_ns=[low, high],
                           wheel_mean_rad_s=mean, gyro_mean_z_rad_s=gyro,
                           native_delta_x_m=delta_x, native_delta_yaw_rad=delta_yaw,
                           full_consumed_done=len(full_done),
                           done_status_masks={str(key): value for key, value in Counter(
                               (r["status"], r["consumed_mask"], r["missing_mask"])
                               for r in phase_done).items()}))
    disabled = [r for r in commands if r["wall_time_ns"] >=
                stages[-1].get("ack_wall_time_ns", stages[-1]["wall_time_ns"])]
    if not disabled or bytes.fromhex(disabled[-1]["data"]) != bytes(8):
        raise ValueError("disable lacks actual four-slot zero command")
    return dict(steps=len(steps), phases=phases, feedback_queued_by_id=dict(by_id),
                control_coverage=coverage,
                done_observed=len(done), done_full_consumed=sum(
                    (r["status"], r["consumed_mask"], r["missing_mask"]) == (0, 3, 0) for r in done),
                done_status_masks={str(key): value for key, value in Counter(
                    (r["status"], r["consumed_mask"], r["missing_mask"]) for r in done).items()})


def source_snapshot(elf, qemu, firmware):
    firmware = Path(firmware)
    paths = [Path(elf), Path(qemu), ROOT / "models/four_wheel.xml", ROOT / "uv.lock",
             ROOT / "qemu.lock", ROOT / "pyproject.toml"]
    paths += sorted((ROOT / "sim").rglob("*.py"))
    paths += sorted(p for p in (ROOT / "tools").iterdir() if p.suffix in (".py", ".sh"))
    paths += [firmware / p for p in (
        "CMakeLists.txt", "CMakePresets.json", "app/CMakeLists.txt", "bsp/CMakeLists.txt",
        "app/selftest/main.cc", "app/selftest/chassis.cc", "bsp/test/core.c",
        "bsp/test/transport.c", "bsp/test/selftest.h", "bsp/src/can.c", "bsp/src/imu.c",
        "bsp/src/uart.c", "bsp/src/spi.c", "bsp/src/time.c", "bsp/src/sys.c",
        "bsp/include/bsp/imu.h", "bsp/internal/bsp/bmi088_reg.h",
        "components/motor/src/dji.cc", "components/motor/include/motor/dji.h",
        "components/motor/internal/device_registry.h", "components/controller/src/pid.cc",
        "components/controller/include/controller/pid.h", "Core/Inc/FreeRTOSConfig.h",
        "Core/Src/freertos.c", "cmake/gcc-arm-none-eabi.cmake")]
    result = {}
    for path in paths:
        with path.open("rb") as stream:
            result[str(path.resolve())] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def run_inside(qmp_path, uart_path, cosim_path, can_path, output, elf, qemu, firmware, start_ns, qemu_pid):
    out = Path(output)
    report = dict(result="failed", errors=[], notes=[], cleanup_errors=[], cases=[],
                  stages=[], summaries=[], missing=[], limits=LIMITS)
    qmp = uart = process = None
    files = []
    primary = None
    started = int(start_ns) / 1e9
    startup_deadline, run_deadline = started + 10, started + 15

    def call(callback, deadline=run_deadline):
        return deadline_qmp_call(qmp, deadline, "chassis consumer", callback)

    def interrupted(sig, _frame):
        raise RuntimeError(f"chassis consumer interrupted by signal {sig}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        before = source_snapshot(elf, qemu, firmware)
        report["inputs_before"] = before
        report["input_checkout"] = {}
        for name, cwd in (("project", ROOT), ("firmware", Path(firmware))):
            report["input_checkout"][name] = dict(
                head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd,
                                              text=True, timeout=2).strip(),
                status=subprocess.check_output(["git", "status", "--short"], cwd=cwd,
                                                text=True, timeout=2).strip(),
                declaration="exact hashed working tree input; no fresh-checkout claim")
        remaining = startup_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("chassis startup deadline expired before QMP")
        qmp = QmpSession(qmp_path, timeout=min(2, remaining), connect_timeout=min(2, remaining))
        uart = connect_unix(uart_path, min(2, max(.001, startup_deadline - time.monotonic())))
        uart.setblocking(False)
        for name in ("worker.stdout", "worker.stderr"):
            files.append((out / name).open("wb"))
        ready = out / "worker.ready"
        ready.unlink(missing_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", cosim_path,
                   can_path, str(out / "trace.jsonl"), str(ready)]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=files[0], stderr=files[1])
        report["processes"] = dict(qemu=_identity(int(qemu_pid)), worker=_identity(process.pid))
        if any(value is None for value in report["processes"].values()):
            raise RuntimeError("process identity missing at acquisition")
        while not ready.exists():
            if process.poll() is not None:
                raise RuntimeError("worker exited before runtime reset: " + str(process.returncode))
            if time.monotonic() >= startup_deadline:
                raise TimeoutError("worker transport startup deadline expired")
            select.select([], [], [], min(.01, startup_deadline - time.monotonic()))
        call(lambda: qmp.command("cont"), startup_deadline)
        buffer = bytearray()
        uart_done = False
        while time.monotonic() < run_deadline:
            if (not report["stages"] and time.monotonic() >= startup_deadline):
                raise TimeoutError("actual chassis READY startup deadline expired")
            readable, _, _ = select.select([uart], [], [], max(0, min(.01, run_deadline - time.monotonic())))
            if readable:
                read_before_ns = time.monotonic_ns()
                chunk = uart.recv(4096)
                read_after_ns = time.monotonic_ns()
                if not chunk:
                    raise ConnectionError("firmware UART disconnected")
                buffer.extend(chunk)
                if len(buffer) > 8192:
                    raise BufferError("firmware UART output exceeds line buffer")
                while b"\n" in buffer:
                    raw, _, remaining = buffer.partition(b"\n")
                    buffer[:] = remaining
                    line = raw.decode("ascii").strip()
                    if not line.startswith("TTEST "):
                        continue
                    print(line, flush=True)
                    fields = line.split()
                    if len(fields) != 5:
                        raise ValueError("invalid firmware report: " + line)
                    if fields[1] in ("PHASE", "DONE"):
                        report["summaries"].append(dict(kind=fields[1], values=list(map(int, fields[2:]))))
                        uart_done = fields[1] == "DONE"
                        continue
                    status, name = fields[1:3]
                    actual, expected = map(int, fields[3:])
                    row = dict(status=status, name=name, actual=actual, expected=expected,
                               wall_time_ns=read_after_ns, read_before_ns=read_before_ns,
                               read_after_ns=read_after_ns)
                    if len(report["cases"]) >= 128:
                        raise BufferError("firmware report capacity exhausted")
                    report["cases"].append(row)
                    if status == "READY":
                        index = len(report["stages"])
                        if index >= len(STAGES) or (name, actual, expected) != STAGES[index]:
                            raise ValueError("unexpected actual chassis stage: " + line)
                        uart.settimeout(min(1, max(.001, run_deadline - time.monotonic())))
                        uart.sendall(b"\xa5")
                        row["ack_wall_time_ns"] = time.monotonic_ns()
                        uart.setblocking(False)
                        report["stages"].append(row)
                    else:
                        try:
                            check_case(row)
                        except ValueError as exc:
                            report["errors"].append(str(exc))
            status = process.poll()
            for name in ("worker.stdout", "worker.stderr"):
                if (out / name).stat().st_size > 128000:
                    raise BufferError("worker diagnostic file exceeds fixed-run bound: " + name)
            if status is not None:
                report["worker_status"] = status
                if status:
                    raise RuntimeError("worker exited with status " + str(status))
                if uart_done:
                    break
        else:
            raise TimeoutError("chassis run deadline expired")
        report["analysis"] = analyze_trace(
            [json.loads(line) for line in (out / "trace.jsonl").read_text().splitlines()],
            report["stages"])
        passes = Counter(r["name"] for r in report["cases"] if r["status"] == "PASS")
        skips = [r["name"] for r in report["cases"] if r["status"] == "SKIP"]
        report["missing"] = sorted(REQUIRED - passes.keys())
        if skips != ["plant.physics"] or passes != Counter({name: 1 for name in REQUIRED}):
            raise ValueError("unexpected missing/duplicate/SKIP firmware coverage")
        expected_summary = [sum(passes.values()), 0, 1]
        if report["summaries"] != [dict(kind=k, values=expected_summary) for k in ("PHASE", "DONE")]:
            raise ValueError("firmware summary disagrees with observed reports")
        timeouts, watchdog = call(lambda: read_iwdg_timeouts(qmp))
        report["watchdog"] = watchdog
        if timeouts or "window-violations=0" not in watchdog:
            raise RuntimeError("watchdog event during chassis consumer")
    except BaseException as exc:
        primary = exc
        report["errors"].append(str(exc) or type(exc).__name__)
        report["notes"].extend(getattr(exc, "__notes__", ()))
    finally:
        for name, action in (("worker", lambda: stop(process, process_group=False)),
                             ("QMP", lambda: qmp.close() if qmp is not None else None),
                             ("UART", lambda: uart.close() if uart is not None else None),
                             *(("diagnostic", f.close) for f in files)):
            try:
                action()
            except BaseException as exc:
                report["cleanup_errors"].append(f"{name}: {exc}")
                if primary is not None:
                    primary.add_note(f"{name} cleanup failed: {exc}")
        if process is not None:
            report["worker_status"] = process.returncode
            report["worker_reaped"] = process.returncode is not None
        report["missing"] = sorted(REQUIRED - {
            row["name"] for row in report["cases"] if row["status"] == "PASS"})
        try:
            report["inputs_after"] = source_snapshot(elf, qemu, firmware)
            if report.get("inputs_before") != report["inputs_after"]:
                report["errors"].append("consumer input sources changed during execution")
        except BaseException as exc:
            report["errors"].append("cannot recheck input sources: " + str(exc))
        stderr_path = out / "worker.stderr"
        if stderr_path.exists():
            with stderr_path.open("rb") as stream:
                stream.seek(max(0, stderr_path.stat().st_size - 12000))
                report["worker_stderr_tail"] = stream.read(12000).decode(errors="replace")
        if primary is not None:
            report["notes"] = list(getattr(primary, "__notes__", ()))
        try:
            trace_path = out / "trace.jsonl"
            report["control_coverage"] = control_coverage(
                [json.loads(line) for line in trace_path.read_text().splitlines()]
                if trace_path.exists() else [])
        except BaseException as exc:
            report["errors"].append("cannot summarize retained control observations: " + str(exc))
        report["result"] = "candidate" if not report["errors"] and not report["cleanup_errors"] else "failed"
        write_json(out / "report.json", report)
    return 0 if report["result"] == "candidate" else 1


def finalize(output, harness_status):
    path = Path(output) / "report.json"
    report = json.loads(path.read_text())
    report["harness_status"] = int(harness_status)
    identities = report.get("processes", {})
    report["cleanup_verified"] = (set(identities) == {"worker", "qemu"} and
        report.get("worker_reaped") is True and all(identity is not None and
        _identity(identity["pid"]) != identity for identity in identities.values()))
    if not report["cleanup_verified"]:
        report["errors"].append("process identity missing or worker/QEMU remains after cleanup")
    try:
        before = report.get("inputs_before")
        if not before:
            raise ValueError("pre-run input hashes are missing")
        final = {}
        for name in before:
            with Path(name).open("rb") as stream:
                final[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        report["inputs_final"] = final
        if final != before:
            raise ValueError("consumer input sources changed through harness cleanup")
    except BaseException as exc:
        report["errors"].append("final input verification failed: " + str(exc))
    if harness_status or report["result"] != "candidate" or report["errors"] or report.get("cleanup_errors"):
        report["result"] = "failed"
        if harness_status:
            report["errors"].append("bounded harness failed (consumer or process cleanup)")
    else:
        report["result"] = "passed"
    write_json(path, report)
    print("RESULT: trobot shared chassis consumer " + report["result"] + "; physical calibration SKIP")
    return 0 if report["result"] == "passed" else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--worker"]:
        return run_worker(*argv[1:])
    if argv[:1] == ["--initialize"]:
        initialize(*argv[1:])
        return 0
    if argv[:1] == ["--finalize"]:
        return finalize(argv[1], int(argv[2]))
    return run_inside(*argv)


if __name__ == "__main__":
    raise SystemExit(main())
