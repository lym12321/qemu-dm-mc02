#!/usr/bin/env python3
"""Actual native consumer of the admitted Arena process runtime.

The runtime owns every process, connection and shared-world operation. Bounded
test-only observers delegate once and retain actual data and cleanup evidence.
Raw samples retain their real filter history; the separate first-sample gate
owns the Bosch quantization oracle. This gate establishes no CPU time barrier.
"""

from contextlib import contextmanager
from collections import Counter
from dataclasses import asdict
import argparse
import errno
import json
import math
import os
from pathlib import Path
import runpy
import signal
import struct
import sys
import time
from unittest.mock import patch
from crccheck.crc import Crc8MaximDow, Crc16Mcrf4Xx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.arena.manifest import load_manifest
from sim.arena import runtime as arena
from sim.transports import step_codec as codec
from sim.transports.cosim import V2SessionReset
from tools.dm_mc02_shared_world_smoke import sha, source_origins, starttime
from qemu.qmp import ExecInterruptedError

COMMANDS = ("0040004000400040", "ffebff95ffebff95")
LITERAL_ARGS = ("$HOME", "$(touch forbidden)", "a b", "")


def append_bounded(report, key, event, maximum=2048):
    if len(report[key]) >= maximum:
        raise BufferError(f"Arena fixture {key} trace exhausted")
    report[key].append(event)


def close_observation_failure(owner, label, primary):
    """A factory still owns an acquired resource until it returns to Arena."""
    try:
        owner.close()
    except BaseException as error:
        primary.add_note(f"{label} observer setup cleanup failed: {type(error).__name__}: {error}")
        if error is not primary:
            for note in tuple(getattr(error, "__notes__", ())):
                primary.add_note(note)


def write_manifests(directory, firmwares, *, referees=False):
    """Distinct child cwd and reversed aliases exercise admitted composition."""
    for index, name in enumerate(("alpha", "bravo")):
        child = directory / name
        child.mkdir()
        document = {
            "schema": 1, "robot": {"name": "native-" + name},
            "boards": {"mcu": {"type": "qemu", "board": "dm-mc02",
                                  "firmware": str(firmwares[index])}},
            "buses": {"drive": {"type": "can"}},
            "links": [{"from": "mcu.fdcan1", "to": "drive"}],
            "devices": {"motor_" + corner: {"type": "dji-m3508", "bus": "drive",
                "id": identifier, "joint": "wheel_" + corner}
                for identifier, corner in enumerate(("fl", "fr", "rl", "rr"), 1)},
            "physics": {"backend": "mujoco", "model": str(ROOT / "models/four_wheel.xml")},
            "ready": {"endpoint": "mcu.uart1", "text": (
                "TTEST READY referee.arena.ready 0 0\n" if referees else f"ARENA_READY={index}\n")},
        }
        if index == 0 and not referees:
            script = ("import json,pathlib,sys,time; "
                "print(json.dumps({'cwd':str(pathlib.Path.cwd()),'argv':sys.argv[1:]}),flush=True); "
                "deadline=time.monotonic()+20\n"
                "while not pathlib.Path('exit-upper').exists():\n"
                " if time.monotonic()>deadline: raise TimeoutError('fixture upper deadline')\n"
                " time.sleep(.01)\n")
            document["upper_computers"] = {"observer": {
                "command": [sys.executable, "-u", "-c", script, *LITERAL_ARGS]}}
        (child / "robot.yaml").write_text(json.dumps(document))
    document = {"schema": 1, "arena": {"name": "native-two-chassis"},
        "world": {"backend": "mujoco", "model": str(ROOT / "models/two_four_wheel.xml")},
        "robots": {
            "west": {"manifest": "bravo/robot.yaml", "prefix": "bravo/", "physics": "shared-world"},
            "east": {"manifest": "alpha/robot.yaml", "prefix": "alpha/", "physics": "shared-world"}}}
    if referees:
        document["referees"] = {name: referee_configuration(name) for name in REFEREE_IDS}
    path = directory / "arena.yaml"
    path.write_text(json.dumps(document))
    return load_manifest(path)


class Observation:
    def __init__(self, report, manifest, mode):
        self.report, self.manifest, self.mode = report, manifest, mode
        self.owners, self.qmps, self.children = [], [], []
        self.plant = self.runtime = None
        self.can_count = self.cosim_count = 0
        self.feedback_sources = {}
        self.run_result = self.run_error = None

    def run(self, manifest, **options):
        """Observe the actual CLI's single call; never execute another loop."""
        evidence = self.report["cli"]
        evidence["runtime_calls"] += 1
        assert evidence["runtime_calls"] == 1
        assert manifest == self.manifest and manifest is not self.manifest
        entrypoint = Path(sys.modules["__main__"].__file__).resolve()
        assert entrypoint == ROOT / "sim/__main__.py"
        evidence.update(entrypoint=str(entrypoint), entrypoint_sha256=sha(entrypoint),
            module_argv=list(sys.argv), actual_sources=source_origins(),
            runtime_options={key: str(value) if key == "qemu" else value
                for key, value in options.items() if key != "emit"})
        assert evidence["runtime_options"] == {
            "duration": .05, "qemu": str(self.binary), "startup_timeout": 10., "timeout": 15.}
        emit = options.pop("emit")
        assert callable(emit)

        def observed_emit(event):
            emit(event)
            self.emit(event)

        try:
            self.run_result = self.original_run(manifest, **options,
                emit=observed_emit, on_response=self.response)
        except BaseException as error:
            self.run_error = error
            raise
        return self.run_result

    def world(self, manifest):
        plant = self.original_world(manifest)
        self.plant = plant
        native = plant.physics
        self.report["world"] = {"model": str(manifest.world.model),
            "devices": [{"id": device.motor_id, "bus": index // 4}
                        for index, device in enumerate(plant.devices)],
            "distinct_buses": len({id(device.bus) for device in plant.devices})}
        reset, step, close = native.reset, native.step, native.close

        def observed_reset():
            self.report["native_calls"]["reset"] += 1
            return reset()

        def observed_step(dt_ns):
            append_bounded(self.report["native_calls"], "step_ns", dt_ns, 512)
            return step(dt_ns)

        def observed_close():
            self.report["native_calls"]["close"] += 1
            return close()

        native.reset, native.step, native.close = observed_reset, observed_step, observed_close
        for index, device in enumerate(plant.devices):
            member = manifest.robots[index // 4]
            motor = member.robot.devices[index % 4]
            self.feedback_sources[index] = []
            feedback = device.feedback

            def observed_feedback(device=device, feedback=feedback, index=index,
                                  member=member, motor=motor):
                # Capture completed producer fields before the later Fabric
                # target pass can deliver a command and change live current.
                source = {"world_ns": device.t_sim_ns,
                    "joint": asdict(native.get_joint_state(member.prefix + motor.joint)),
                    "current_raw": device.current_raw}
                frame = feedback()
                assert frame.timestamp_ns == source["world_ns"]
                source["data_hex"] = frame.data.hex()
                if len(self.feedback_sources[index]) >= 256:
                    raise BufferError("Arena fixture pending feedback source exhausted")
                self.feedback_sources[index].append(source)
                return frame

            device.feedback = observed_feedback
        return plant

    @contextmanager
    def qemu(self, command, **kwargs):
        with self.original_qemu(command, **kwargs) as owner:
            identity = {"pid": owner.process.pid, "starttime": starttime(owner.process.pid),
                "tempdir": str(owner.stderr_path.parent), "sockets": {
                    name: str(path) for name, path in owner.sockets.items()},
                "argv": [owner.expand(value) for value in command]}
            assert identity["starttime"] is not None
            self.owners.append(owner)
            self.report["cleanup"].append(identity)
            yield owner

    def qmp(self, *args, **kwargs):
        qmp = self.original_qmp(*args, **kwargs)
        try:
            self.qmps.append(qmp)
            command = qmp.command
            index = len(self.qmps) - 1

            def observed_command(name, arguments=None):
                try:
                    value = command(name, arguments)
                except BaseException as error:
                    append_bounded(self.report, "qmp_events", {
                        "peer": index, "command": name,
                        "error": f"{type(error).__name__}: {error}"}, 16)
                    raise
                if name in ("cont", "stop", "system_reset", "quit"):
                    append_bounded(self.report, "qmp_events", {
                        "peer": index, "command": name, "result": value}, 16)
                return value

            qmp.command = observed_command
            return qmp
        except BaseException as error:
            close_observation_failure(qmp, "QMP", error)
            raise

    def can(self, sock):
        stream = self.original_can(sock)
        index = self.can_count
        self.can_count += 1
        send, receive = stream.send, stream.receive
        def observed_send(can_id, flags, dlc, timestamp_ns, data):
            device = self.plant.devices[index * 4 + can_id - 0x201]
            source = self.feedback_sources[index * 4 + can_id - 0x201].pop(0)
            assert source["data_hex"] == data.hex()
            state = source["joint"]
            encoder, rpm, current, temperature, reserved = struct.unpack(">HhhBB", data)
            # DJI C620 V1.01 encoder8192/rpm and M3508 P19 V1.0 ratio3591/187.
            position = (state["position_rad"] * (3591 / 187)) % math.tau
            event = {"peer": index, "can_id": can_id, "qemu_ns": timestamp_ns,
                     "world_ns": source["world_ns"], "data_hex": data.hex(), "accepted": False,
                     "joint": state, "rotor_position_mod_tau": position,
                     "producer_current_raw": source["current_raw"],
                     "send_live_current_raw": device.current_raw}
            append_bounded(self.report, "feedback", event)
            # An extremely small negative angle can project to exact tau in
            # double modulo. On the documented cyclic8192 encoder this exact
            # boundary is zero; accept only that equality, not a wider bin.
            assert (encoder == 0 and position == math.tau) or (
                encoder * math.tau / 8192 <= position < (encoder + 1) * math.tau / 8192), repr(event)
            assert abs(state["velocity_rad_s"] * (3591 / 187) - rpm * math.pi / 30) <= math.pi / 60, repr(event)
            assert (flags, dlc, current, temperature, reserved) == (0, 8, source["current_raw"], 25, 0), repr(event)
            result = send(can_id, flags, dlc, timestamp_ns, data)
            event["accepted"] = True
            return result

        def observed_receive():
            try:
                frames = receive()
            except ConnectionError as error:
                append_bounded(self.report, "can_receive_errors", {
                    "peer": index, "error_type": type(error).__name__,
                    "error": str(error)}, 8)
                raise
            for can_id, flags, dlc, stamp, data in frames:
                append_bounded(self.report, "commands", {"peer": index,
                    "can_id": can_id, "flags": flags, "dlc": dlc,
                    "qemu_ns": stamp, "data_hex": data.hex()}, 8)
                assert (can_id, flags, dlc, data.hex()) == (0x200, 0, 8, COMMANDS[index])
            return frames

        stream.send, stream.receive = observed_send, observed_receive
        return stream

    def cosim(self, sock):
        stream = self.original_cosim(sock)
        index = self.cosim_count
        self.cosim_count += 1
        send, receive = stream.send, stream.receive

        def observed_send(wire):
            event = {"peer": index, "wire_hex": wire.hex(), "accepted": False}
            append_bounded(self.report, "cosim_sends", event)
            frame = codec.decode_step_frame(wire)
            event["header"] = asdict(frame.header)
            if frame.header.kind == codec.STEP:
                name = self.manifest.robots[index].prefix[:-1]
                source = self.plant.physics.get_imu(name)
                imu = codec.decode_imu_sample_v2(frame.payload)
                expected = struct.unpack("<6f", struct.pack("<6f",
                    *(value * 180 / math.pi for value in source.gyro_rad_s),
                    *(value / 9.80665 for value in source.accel_m_s2)))
                assert (*imu.gyro_dps, *imu.accel_g) == expected
                assert (imu.sample_id, imu.sensor_time_ns) == (frame.header.step_id, frame.header.t_sim_ns)
                event.update(imu=asdict(imu), source_name=name, source_si=asdict(source))
            try:
                result = send(wire)
            except BrokenPipeError as error:
                event["send_error"] = {"error_type": type(error).__name__,
                    "errno": error.errno, "error": str(error)}
                raise
            event["accepted"] = True
            return result

        def observed_receive():
            frames = receive()
            for frame in frames:
                if frame.header.kind == codec.STEP_DONE:
                    event = {"name": self.manifest.robots[index].name,
                        "peer": index, "header": asdict(frame.header),
                        "payload_hex": frame.payload.hex()}
                    append_bounded(self.report, "transport_done", event)
                    event["decoded"] = asdict(codec.decode_step_done_payload(frame.payload))
            return frames

        stream.send, stream.receive = observed_send, observed_receive
        return stream

    def completed_names(self):
        # The runtime callback intentionally skips some advisory replies.
        # Observe the existing receive batches (including returned suffixes),
        # and require an exact actual STEP identity without another RX consumer.
        names = set()
        for event in self.report["transport_done"]:
            done = event.get("decoded", {})
            if (done.get("status"), done.get("consumed_mask"), done.get("missing_mask")) != (0, 3, 0):
                continue
            if any(sent["peer"] == event["peer"] and sent["accepted"]
                   and sent["header"]["kind"] == codec.STEP
                   and all(sent["header"][key] == event["header"][key]
                           for key in ("flags", "step_id", "t_sim_ns"))
                   for sent in self.report["cosim_sends"]):
                names.add(event["name"])
        return names

    def shared(self, *args, **kwargs):
        runtime = self.original_shared(*args, **kwargs)
        self.runtime = runtime
        close = runtime.close

        def observed_close():
            if self.mode == "interrupt" and self.report.get("injected"):
                before = list(self.report["native_calls"]["step_ns"])
                self.report["failed_before_close"] = runtime.failed
                result = close()
                try:
                    runtime.step(1_000_000, deadline=time.monotonic() + 1)
                except RuntimeError as error:
                    self.report["retry_error"] = str(error)
                else:
                    raise AssertionError("closed Arena runtime accepted another tick")
                assert self.report["native_calls"]["step_ns"] == before
                self.report["terminal_retry_verified"] = True
                return result
            try:
                before = list(self.report["native_calls"]["step_ns"])
                self.report["failed_before_close"] = runtime.failed
                if self.mode != "normal" and self.report.get("injected"):
                    try:
                        runtime.step(1_000_000, deadline=time.monotonic() + 1)
                    except RuntimeError as error:
                        self.report["retry_error"] = str(error)
                    else:
                        raise AssertionError("failed Arena runtime accepted another tick")
                    assert self.report["native_calls"]["step_ns"] == before
                    assert runtime.failed
                    self.report["freeze_verified"] = True
            except BaseException as error:
                try:
                    close()
                except BaseException as cleanup:
                    error.add_note(f"Arena fixture runtime close failed: {cleanup}")
                raise
            return close()

        runtime.close = observed_close
        return runtime

    def child(self, label, process):
        child = self.original_child(label, process)
        self.children.append(child)
        self.report["upper_cleanup"].append({"label": label, "pid": process.pid,
            "starttime": starttime(process.pid)})
        return child

    def emit(self, event):
        index = [member.name for member in self.manifest.robots].index(event.name)
        text = event.event.data.decode("ascii")
        self.report["uart"][index] += text
        if len(self.report["uart"][index]) > 4096:
            raise BufferError("Arena guest UART trace exhausted")
        assert "WORLD_BAD_CAN" not in self.report["uart"][index]
        self.inject()

    def response(self, name, frame, decoded):
        append_bounded(self.report, "responses", {"name": name, "header": asdict(frame.header),
            "payload_hex": frame.payload.hex(), "decoded": None if decoded is None else asdict(decoded)})
        self.inject()

    def inject(self):
        if self.mode == "normal" or self.report.get("injected"):
            return
        if not all(f"ARENA_READY={index}\n" in text for index, text in enumerate(self.report["uart"])):
            return
        if self.completed_names() != {member.name for member in self.manifest.robots}:
            return
        self.report["injected"] = self.mode
        if self.mode == "interrupt":
            os.kill(os.getpid(), signal.SIGTERM)
        elif self.mode == "upper-exit":
            (self.manifest.robots[0].robot.path.parent / "exit-upper").write_text("exit0\n")
        else:
            index = 0 if self.mode == "reset" else 1
            self.qmps[index].settimeout(2.)
            self.qmps[index].command("system_reset" if self.mode == "reset" else "quit")


def invoke_cli(observer, report_path):
    """Run the selected package's real module entry within this instrumented fixture."""
    evidence = observer.report["cli"]
    argv = ["sim", "arena", "run", str(observer.manifest.path), "--duration", "0.05",
        "--qemu", str(observer.binary), "--startup-timeout", "10", "--timeout", "15",
        "--report", str(report_path)]
    evidence.update(argv=argv, report_path=str(report_path), runtime_calls=0)
    previous_argv = sys.argv
    try:
        sys.argv = list(argv)
        try:
            runpy.run_module("sim", run_name="__main__", alter_sys=True)
        except SystemExit as exit:
            evidence["exit_code"] = exit.code
        else:
            raise AssertionError("sim module entry did not exit")
    finally:
        sys.argv = previous_argv
    try:
        assert evidence["runtime_calls"] == 1
        assert evidence["module_argv"] == [str(ROOT / "sim/__main__.py"), *argv[1:]]
        payload = json.loads(report_path.read_text())
        evidence.update(report=payload, report_sha256=sha(report_path))
        error = observer.run_error
        if error is None:
            expected = {"status": "passed", "result": asdict(observer.run_result)}
            assert evidence["exit_code"] == 0
        else:
            expected = {"status": "failed", "error": str(error),
                "error_type": type(error).__name__, "notes": list(getattr(error, "__notes__", ()))}
            assert evidence["exit_code"] == (130 if isinstance(error, KeyboardInterrupt) else 1)
        assert payload == json.loads(json.dumps(expected))
        evidence["verified"] = True
    except BaseException as verification:
        evidence["verification_error"] = f"{type(verification).__name__}: {verification}"
        if observer.run_error is None:
            raise
        observer.run_error.add_note(f"Arena CLI verification failed: {verification}")
    if observer.run_error is not None:
        raise observer.run_error
    return observer.run_result


REFEREE_IDS = {"east": 3, "west": 103}
REFEREE_PAYLOADS = {
    "east": ("0100000807060504030201", "03026400c80000006400500005",
             "000000000000000028000a001400", "050900070b0d00fe"),
    "west": ("0200000807060504030201", "67024600c80000006400780006",
             "000000000000000029000c001900", "08110013171d0080"),
}
REFEREE_REQUIRED = frozenset(("control.rx", "reset.poweron", "bsp.init", "task.stack",
    "referee.executor", "referee.uart", *(f"referee.arena.{sample}.{group}"
        for sample in range(1, 4) for group in (
            "fresh", "game", "robot", "power", "buff", "id",
            "ts.game", "ts.robot", "ts.power", "ts.buff"))))


def referee_configuration(name):
    """Stable explicit profiles; no script or guest/world time relationship."""
    west = name == "west"
    return {"uart": "mcu.uart7", "durations": {"countdown_s": 1, "battle_s": 1},
        "initial": {
            "game": {"game_type": 2 if west else 1, "game_progress": 0,
                     "stage_remain_time": 0, "sync_timestamp": 0x0102030405060708},
            "robot": {"robot_id": REFEREE_IDS[name], "robot_level": 2,
                "current_hp": 70 if west else 100, "maximum_hp": 200, "cooling_per_s": 0,
                "heat_limit": 100, "chassis_power_limit": 120 if west else 80,
                "power_outputs": 6 if west else 5},
            "power_heat": {"buffer_energy_j": 41 if west else 40,
                           "heat_17mm": 12 if west else 10, "heat_42mm": 25 if west else 20},
            "buff": {"recovery_percent": 8 if west else 5, "cooling_per_s": 17 if west else 9,
                "defence_percent": 19 if west else 7, "vulnerability_percent": 23 if west else 11,
                "attack_percent": 29 if west else 13, "remaining_energy": 0x80 if west else 0xfe}}}


def check_referee_wire(wire, name, sequence):
    """Independent literal payloads and pinned CRC, without production encoders."""
    if type(wire) is not bytes or len(wire) != 82:
        raise ValueError("Arena referee requires a complete82-byte snapshot")
    cursor = 0
    for offset, (command, payload_hex) in enumerate(zip(
            (0x0001, 0x0201, 0x0202, 0x0204), REFEREE_PAYLOADS[name])):
        payload = bytes.fromhex(payload_hex)
        raw = wire[cursor:cursor + len(payload) + 9]
        if (raw[:4] != struct.pack("<BHB", 0xa5, len(payload), (sequence + offset) % 256)
                or raw[4] != Crc8MaximDow.calc(raw[:4], initvalue=0xff)
                or raw[5:7] != struct.pack("<H", command) or raw[7:-2] != payload
                or int.from_bytes(raw[-2:], "little") != Crc16Mcrf4Xx.calc(raw[:-2])):
            raise ValueError("Arena referee wire disagrees with member literal/sequence/CRC")
        cursor += len(raw)


def check_referee_case(row, name):
    status, label = row["status"], row["name"]
    actual, expected = row["actual"], row["expected"]
    if any(type(value) is not int or not 0 <= value <= 0xffffffff for value in (actual, expected)):
        raise ValueError("Arena firmware report requires uint32 fields")
    if status == "SKIP" and label == "plant.physics" and actual == expected == 0:
        return
    if status != "PASS" or label not in REFEREE_REQUIRED:
        raise ValueError("failed or unexpected Arena firmware case: " + label)
    if label == "task.stack":
        valid = expected == 64 and actual >= 64
    elif label == "reset.poweron":
        valid = actual == expected == 0x800000
    elif label.endswith(".id"):
        valid = actual == expected == REFEREE_IDS[name]
    elif ".ts." in label:
        valid = actual != 0 and actual != expected
    else:
        valid = actual == expected == (1 if label.endswith(".fresh") else 0)
    if not valid:
        raise ValueError("Arena firmware fields disagree: " + label)


def check_referee_coverage(rows, summaries, name):
    for row in rows:
        check_referee_case(row, name)
    passed = Counter(row["name"] for row in rows if row["status"] == "PASS")
    if (passed != Counter({label: 1 for label in REFEREE_REQUIRED})
            or [row["name"] for row in rows if row["status"] == "SKIP"] != ["plant.physics"]):
        raise ValueError("missing/duplicate Arena firmware coverage")
    for group in ("game", "robot", "power", "buff"):
        previous = 0
        for sample in range(1, 4):
            row = next(row for row in rows if row["name"] == f"referee.arena.{sample}.ts.{group}")
            if row["expected"] != previous:
                raise ValueError("Arena firmware timestamp history disagrees: " + group)
            previous = row["actual"]
    if summaries != [{"kind": kind, "values": [36, 0, 1]} for kind in ("PHASE", "DONE")]:
        raise ValueError("Arena firmware summaries disagree")


def check_referee_responses(summary, wire_session, steps, end_ns, reset_qemu_ns):
    """Check the retained actual RESET and first/last ACK identities."""
    reset = summary["reset"]
    if (reset is None or reset["header"]["kind"] != codec.RESET_ACK
            or reset["header"]["flags"] != wire_session
            or (reset["header"]["step_id"], reset["header"]["t_sim_ns"],
                reset["header"]["dt_ns"]) != (0, 0, 0)
            or reset["decoded"]["status"] != codec.STATUS_OK
            or reset["decoded"]["virtual_time_ns"] != reset_qemu_ns):
        raise ValueError("Arena referee scoped RESET_ACK identity disagrees")
    if summary["step_ack_count"] != steps or steps <= 0:
        raise ValueError("Arena referee STEP_ACK coverage disagrees")
    for row, step, target in ((summary["first_step_ack"], 1, 1_000_000),
                              (summary["last_step_ack"], steps, end_ns)):
        if (row is None or row["header"]["kind"] != codec.STEP_ACK
                or row["header"]["flags"] != wire_session
                or (row["header"]["step_id"], row["header"]["t_sim_ns"],
                    row["header"]["dt_ns"]) != (step, target, 1_000_000)
                or row["decoded"]["status"] != codec.STATUS_OK):
            raise ValueError("Arena referee scoped STEP_ACK identity disagrees")


def check_referee_publications(rows, referee, end_ns, ready_logical_ns):
    if (not rows or not all(row.get("accepted") and row.get("verified") for row in rows)
            or referee.publications != len(rows) or referee.period_ns != 100_000_000
            or referee.first_logical_ns != rows[0]["logical_ns"]
            or referee.last_logical_ns != rows[-1]["logical_ns"]
            or referee.first_logical_ns != ready_logical_ns
            or len(rows) != 1 + (end_ns - rows[0]["logical_ns"]) // 100_000_000
            or any(row["logical_ns"] != rows[0]["logical_ns"] + index * 100_000_000
                   for index, row in enumerate(rows))
            or referee.next_sequence != (4 * len(rows)) % 256):
        raise ValueError("Arena referee scoped publication quantities/cadence disagree")


class RefereeObservation(Observation):
    """Borrow existing owners; observe this UART consumer with bounded evidence."""

    def __init__(self, report, manifest):
        super().__init__(report, manifest, "normal")
        self.engines, self.endpoints = [], []
        self.sockets = []
        self.buffers = {name: bytearray() for name in REFEREE_IDS}

    def connect(self, *args, **options):
        sock = self.original_connect(*args, **options)
        try:
            self.sockets.append((str(args[0]), sock))
            return sock
        except BaseException as error:
            close_observation_failure(sock, "socket", error)
            raise

    @contextmanager
    def qemu(self, command, **options):
        with super().qemu(command, **options) as owner:
            primary = None
            try:
                yield owner
            except BaseException as error:
                primary = error
                raise
            finally:
                try:
                    with owner.stderr_path.open("rb") as stream:
                        stream.seek(max(0, owner.stderr_path.stat().st_size - 12000))
                        self.report["cleanup"][self.owners.index(owner)]["stderr_tail"] = (
                            stream.read(12000).decode("utf-8", errors="replace"))
                except BaseException as error:
                    if primary is None:
                        raise
                    primary.add_note(f"Arena referee stderr capture failed: {type(error).__name__}: {error}")
                    if error is not primary:
                        for note in tuple(getattr(error, "__notes__", ())):
                            primary.add_note(note)

    def qmp(self, *args, **options):
        qmp = super().qmp(*args, **options)
        try:
            index = len(self.qmps) - 1
            name = self.manifest.robots[index].name
            command, close = qmp.command, qmp.close
            row = {"attempts": 0, "completed": False}
            self.report["qmp_cleanup"].append(row)
            def observed_command(operation, arguments=None):
                result = command(operation, arguments)
                if operation == "stop":
                    self.watchdog(index, name, "paused")
                return result
            def observed_close():
                row["attempts"] += 1
                # Preserve the native atexit registration's bound method identity.
                qmp.close = close
                try:
                    result = close()
                    row["completed"] = True
                    return result
                finally:
                    qmp.close = observed_close
            qmp.command, qmp.close = observed_command, observed_close
            return qmp
        except BaseException as error:
            close_observation_failure(qmp, "QMP", error)
            raise

    def world(self, manifest):
        plant = self.original_world(manifest)
        try:
            self.plant = plant
            native = plant.physics
            self.report["world"] = {"model": str(manifest.world.model),
                "devices": len(plant.devices), "distinct_buses": len({id(item.bus) for item in plant.devices})}
            reset, step, close = native.reset, native.step, native.close
            calls = self.report["native_calls"]
            def observed_reset():
                calls["reset"] += 1
                return reset()
            def observed_step(dt_ns):
                calls["attempts"] += 1
                result = step(dt_ns)
                calls["steps"] += 1
                calls["total_ns"] += dt_ns
                calls["minimum_ns"] = dt_ns if calls["minimum_ns"] is None else min(calls["minimum_ns"], dt_ns)
                calls["maximum_ns"] = dt_ns if calls["maximum_ns"] is None else max(calls["maximum_ns"], dt_ns)
                return result
            def observed_close():
                calls["close"] += 1
                return close()
            native.reset, native.step, native.close = observed_reset, observed_step, observed_close
            return plant
        except BaseException as error:
            close_observation_failure(plant, "world", error)
            raise

    def engine(self, *args, **options):
        engine = self.original_engine(*args, **options)
        try:
            self.engines.append(engine)
            return engine
        except BaseException as error:
            close_observation_failure(engine, "referee engine", error)
            raise

    def endpoint(self, *args, **options):
        endpoint = self.original_endpoint(*args, **options)
        try:
            self.endpoints.append(endpoint)
            return endpoint
        except BaseException as error:
            close_observation_failure(endpoint, "referee endpoint", error)
            raise

    def shared(self, *args, **options):
        runtime = self.original_shared(*args, **options)
        try:
            self.runtime = runtime
            publish = runtime.publish_referee
            def observed_publish(name, *, deadline):
                if name not in self.report["ready"]:
                    raise ValueError("Arena referee publication preceded actual local ready")
                item = next(item for item in runtime.referees if item.name == name)
                rows = self.report["publications"][name]
                sequence = (4 * len(rows)) % 256
                if item.endpoint.next_sequence != sequence:
                    raise ValueError("Arena referee outgoing sequence owner disagrees")
                row = {"logical_ns": runtime.t_sim_ns, "sequence": sequence, "accepted": False}
                append_bounded(self.report["publications"], name, row, 127)
                wire = publish(name, deadline=deadline)
                row.update(wire_hex=wire.hex(), accepted=True)
                check_referee_wire(wire, name, sequence)
                row["verified"] = True
                return wire
            runtime.publish_referee = observed_publish
            return runtime
        except BaseException as error:
            close_observation_failure(runtime, "shared world", error)
            raise

    def response(self, name, frame, decoded):
        row = {"header": asdict(frame.header), "payload_hex": frame.payload.hex(),
               "decoded": None if decoded is None else asdict(decoded)}
        summary = self.report["responses"][name]
        summary["count"] += 1
        if summary["first"] is None:
            summary["first"] = row
        summary["last"] = row
        if frame.header.kind == codec.RESET_ACK:
            if summary["reset"] is not None:
                raise ValueError("duplicate Arena RESET_ACK observation")
            summary["reset"] = row
        elif frame.header.kind == codec.STEP_ACK:
            summary["step_ack_count"] += 1
            if summary["first_step_ack"] is None:
                summary["first_step_ack"] = row
            summary["last_step_ack"] = row
        elif frame.header.kind == codec.STEP_DONE:
            summary["done_count"] += 1
            summary["last_done"] = row

    def emit(self, event):
        if event.event.endpoint != "uart1":
            raise ValueError("unexpected Arena referee guest UART output")
        index = [item.name for item in self.manifest.robots].index(event.name)
        text = event.event.data.decode("ascii")
        if len(self.report["uart"][index]) + len(text) > 12000:
            raise BufferError("Arena referee UART observation exhausted")
        self.report["uart"][index] += text
        buffer = self.buffers[event.name]
        if len(buffer) + len(event.event.data) > 12000:
            raise BufferError("Arena referee line buffer exhausted")
        buffer.extend(event.event.data)
        while b"\n" in buffer:
            raw, _, suffix = buffer.partition(b"\n")
            buffer[:] = suffix
            line = raw.decode("ascii").strip()
            if not line.startswith("TTEST "):
                continue
            fields = line.split()
            if len(fields) != 5:
                raise ValueError("invalid Arena firmware report: " + line)
            if fields[1] in ("PHASE", "DONE"):
                row = {"kind": fields[1], "values": list(map(int, fields[2:]))}
                append_bounded(self.report["summaries"], event.name, row, 2)
                if fields[1] == "DONE":
                    check_referee_coverage(self.report["cases"][event.name],
                                           self.report["summaries"][event.name], event.name)
                    self.watchdog(index, event.name, "done")
                continue
            row = {"status": fields[1], "name": fields[2],
                   "actual": int(fields[3]), "expected": int(fields[4])}
            if fields[1] == "READY":
                if (fields[2] != "referee.arena.ready" or row["actual"] != 0 or row["expected"] != 0
                        or event.name in self.report["ready"]):
                    raise ValueError("unexpected Arena referee ready marker")
                self.report["ready"][event.name] = {"poll_sample_qemu_ns": event.event.timestamp_ns,
                                                    "logical_ns": self.runtime.t_sim_ns}
            else:
                append_bounded(self.report["cases"], event.name, row, 37)
                check_referee_case(row, event.name)

    def watchdog(self, index, name, stage):
        from tools.dm_mc02_firmware_rtf import deadline_qmp_call, read_iwdg_timeouts
        deadline = self.owners[0].start_ns / 1e9 + 20
        value = deadline_qmp_call(self.qmps[index], deadline, "Arena referee watchdog",
                                 lambda: read_iwdg_timeouts(self.qmps[index]))
        timeouts, text = value
        self.report["watchdogs"].setdefault(name, {})[stage] = {"timeouts": timeouts, "diagnostics": text}
        if timeouts or "window-violations=0" not in text:
            raise ValueError("watchdog event during Arena referee consumer")


def run_referee(elf, output, firmware, *, qemu=None):
    """One existing Arena execution, with passed published after cleanup checks."""
    from tools.dm_mc02_referee_test import source_snapshot, checkout_snapshot, write_json
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("refusing to overwrite existing Arena referee result directory")
    output.mkdir(parents=True)
    report = {"result": "failed", "arena": True, "errors": [], "notes": [],
        "cleanup": [], "qmp_events": [], "qmp_cleanup": [], "uart": ["", ""],
        "cases": {name: [] for name in REFEREE_IDS}, "summaries": {name: [] for name in REFEREE_IDS},
        "publications": {name: [] for name in REFEREE_IDS}, "ready": {}, "watchdogs": {},
        "responses": {name: {"count": 0, "first": None, "last": None, "reset": None,
            "step_ack_count": 0, "first_step_ack": None, "last_step_ack": None,
            "done_count": 0, "last_done": None} for name in REFEREE_IDS},
        "native_calls": {"reset": 0, "attempts": 0, "steps": 0, "total_ns": 0,
                         "minimum_ns": None, "maximum_ns": None, "close": 0},
        "limitations": ["Actual dual UART7/HAL/DMA/msg consumer of stable explicit profiles.",
            "Guest1000ms waits, independent QEMU epochs and shared logical100ms periods are not a CPU barrier.",
            "Publication counts mean transport admission; no exact host batch to firmware observation pairing.",
            "World/ACK evidence is a constant-space summary, not a full trace; response callbacks are incomplete.",
            "No official independent command rates, live adjudication, calibrated physics, pacing or RTF claim.",
            "Firmware plant.physics remains an explicit SKIP."],
        "missing": {name: sorted(REFEREE_REQUIRED) for name in REFEREE_IDS},
        "skips": {name: [] for name in REFEREE_IDS},
        "missing_skips": {name: ["plant.physics"] for name in REFEREE_IDS}}
    write_json(output / "report.json", report)
    observer = primary = binary = manifest = None
    previous_signal = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"Arena referee interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    def inputs():
        result = source_snapshot(elf, binary, firmware)
        paths = [ROOT / name for name in ("models/two_four_wheel.xml", "models/four_wheel.xml",
            "models/mujoco-LICENSE.txt", "flake.lock", "tools/dm_mc02_arena_smoke.py",
            "tools/dm_mc02_shared_world_smoke.py")]
        if manifest is not None:
            paths += [manifest.path, *(item.robot.path for item in manifest.robots)]
        result.update({str(path): sha(path) for path in paths})
        return result
    try:
        elf = arena.regular_file(elf, "ArenaRefereeTest ELF")
        binary = arena.resolve_qemu(qemu)
        firmware = Path(firmware).resolve()
        if not firmware.is_dir():
            raise ValueError("firmware source tree is not a directory")
        directory = output / "arena"
        directory.mkdir()
        manifest = write_manifests(directory, (elf, elf), referees=True)
        report["inputs_before"], report["checkout_before"] = inputs(), checkout_snapshot(firmware)
        report["actual_sources_before"] = source_origins()
        observer = RefereeObservation(report, manifest)
        overrides = {"create_arena_plant": "world", "running_qemu": "qemu", "QmpSession": "qmp",
            "connect_unix": "connect", "SharedWorldRuntime": "shared",
            "RefereeEngine": "engine", "RefereeUartEndpoint": "endpoint"}
        for original, replacement in overrides.items():
            setattr(observer, "original_" + replacement, getattr(arena, original))
        with patch.multiple(arena, **{name: getattr(observer, method) for name, method in overrides.items()}):
            result = arena.run_manifest(manifest, duration=1., qemu=binary, startup_timeout=10., timeout=20.,
                                        emit=observer.emit, on_response=observer.response)
        report["runtime_result"] = asdict(result)
        calls = report["native_calls"]
        if (result.end_ns - result.origin_ns != 1_000_000_000 or calls["reset"] != 1
                or calls["close"] != 1 or calls["steps"] != result.steps or calls["attempts"] != result.steps
                or calls["total_ns"] != result.end_ns
                or (calls["minimum_ns"], calls["maximum_ns"]) != (1_000_000, 1_000_000)):
            raise ValueError("Arena referee world execution quantities disagree")
        if len(result.boards) != 2 or len(result.referees) != 2:
            raise ValueError("Arena referee public result scope is incomplete")
        for wire_session, (board, referee) in enumerate(zip(result.boards, result.referees), 1):
            name = board.name
            rows = report["publications"][name]
            if board.ready != "uart_marker" or board.ready_qemu_ns is None or name != referee.member:
                raise ValueError("Arena referee scoped publication quantities disagree")
            check_referee_publications(rows, referee, result.end_ns, report["ready"][name]["logical_ns"])
            report["ready"][name]["post_read_qemu_ns"] = board.ready_qemu_ns
            check_referee_responses(report["responses"][name], wire_session,
                                    result.steps, result.end_ns, board.reset_qemu_ns)
    except BaseException as error:
        primary = error
    finally:
        def verify(callback):
            nonlocal primary
            try:
                callback()
            except BaseException as error:
                report["errors"].append(f"{type(error).__name__}: {error}")
                if primary is None:
                    primary = error
                elif error is not primary:
                    primary.add_note(f"Arena referee post-cleanup verification failed: {type(error).__name__}: {error}")
                    for note in tuple(getattr(error, "__notes__", ())):
                        primary.add_note(note)
        for name in REFEREE_IDS:
            report["missing"][name] = sorted(REFEREE_REQUIRED - {
                row["name"] for row in report["cases"][name] if row["status"] == "PASS"})
            report["skips"][name] = [row["name"] for row in report["cases"][name] if row["status"] == "SKIP"]
            report["missing_skips"][name] = [] if "plant.physics" in report["skips"][name] else ["plant.physics"]
            verify(lambda name=name: check_referee_coverage(report["cases"][name], report["summaries"][name], name))
        def cleanup():
            if observer is None:
                raise ValueError("Arena referee owner acquisition unavailable")
            failures = []
            def check(label, read):
                try:
                    value = read()
                except BaseException as error:
                    failures.append(f"{label}: {type(error).__name__}: {error}")
                    return False
                if not value:
                    failures.append(label)
                return value
            for identity, owner in zip(report["cleanup"], observer.owners):
                identity["identity_gone"] = check(f"QEMU{identity['pid']} identity", lambda:
                    starttime(identity["pid"]) != identity["starttime"])
                identity["reaped"] = check(f"QEMU{identity['pid']} reap", lambda: owner.process.poll() is not None)
                identity["temporary_gone"] = check(f"QEMU{identity['pid']} temporary directory", lambda:
                    not Path(identity["tempdir"]).exists())
                identity["sockets_gone"] = {name: check(f"QEMU{identity['pid']} {name} path", lambda path=path:
                    not Path(path).exists()) for name, path in identity["sockets"].items()}
            owned = report["owned_cleanup"] = {
                "engines": [check(f"engine{index} close", lambda item=item: item.closed)
                            for index, item in enumerate(observer.engines)],
                "endpoints": [check(f"endpoint{index} close", lambda item=item: item.closed)
                              for index, item in enumerate(observer.endpoints)],
                "plant": check("plant close", lambda: observer.plant is not None and observer.plant.closed),
                "devices": [] if observer.plant is None else [
                    check(f"device{index} close", lambda item=item: item.closed)
                    for index, item in enumerate(observer.plant.devices)],
                "runtime": check("runtime close", lambda: observer.runtime is not None and observer.runtime.closed),
                "borrowed_sockets": {}}
            for path, sock in observer.sockets:
                owned["borrowed_sockets"][path] = check(path + " socket", lambda sock=sock: sock.fileno() < 0)
            if observer.runtime is not None:
                borrowed = {id(sock) for _, sock in observer.sockets}
                for binding in observer.runtime.bindings:
                    for sock in (binding.cosim.sock, binding.endpoint.stream.sock):
                        check(binding.name + " stream socket identity", lambda sock=sock: id(sock) in borrowed)
                for endpoint in observer.endpoints:
                    check("referee UART socket identity", lambda endpoint=endpoint:
                        id(endpoint.transport.sock) in borrowed)
            for index, row in enumerate(report["qmp_cleanup"]):
                check(f"QMP{index} close", lambda row=row: row == {"attempts": 1, "completed": True})
            if (len(observer.owners) != 2 or len(observer.engines) != 2 or len(observer.endpoints) != 2
                    or observer.children or len(owned["devices"]) != 8
                    or len(report["qmp_cleanup"]) != 2 or len(owned["borrowed_sockets"]) != 8):
                failures.append("owner acquisition scope incomplete")
            if failures:
                raise ValueError("Arena referee cleanup incomplete: " + "; ".join(failures))
            report["cleanup_verified"] = True
        verify(cleanup)
        def sources():
            if any(report.get(key) is None for key in ("inputs_before", "checkout_before", "actual_sources_before")):
                raise ValueError("Arena referee pre-run input/source state unavailable")
            report["inputs_after"], report["checkout_after"] = inputs(), checkout_snapshot(firmware)
            report["actual_sources_after"] = source_origins()
            if (report.get("inputs_before") != report["inputs_after"]
                    or report.get("checkout_before") != report["checkout_after"]
                    or any(report["actual_sources_after"].get(name) != value
                           for name, value in report["actual_sources_before"].items())):
                raise ValueError("Arena referee actual input/source state changed")
            report["source_preserved"] = True
        verify(sources)
        def watchdogs():
            if (set(report["watchdogs"]) != set(REFEREE_IDS)
                    or any(set(rows) != {"done", "paused"} for rows in report["watchdogs"].values())
                    or any(row["timeouts"] != 0 or "window-violations=0" not in row["diagnostics"]
                           for rows in report["watchdogs"].values() for row in rows.values())):
                raise ValueError("Arena referee watchdog observations missing")
        verify(watchdogs)
        signal.signal(signal.SIGTERM, previous_signal)
        report["result"] = "passed" if primary is None else "failed"
        if primary is not None:
            report["errors"].insert(0, f"{type(primary).__name__}: {primary}")
            report["error_type"] = type(primary).__name__
            report["notes"] = list(getattr(primary, "__notes__", ()))
        write_json(output / "report.json", report)
    print("RESULT: dual-QEMU Arena referee consumer " + report["result"])
    if primary is not None:
        print(f"Arena referee consumer failed: {primary}", file=sys.stderr)
        for note in report["notes"]:
            print(note, file=sys.stderr)
    return 0 if primary is None else 130 if isinstance(primary, KeyboardInterrupt) else 1


def main(args):
    result_path = Path(args.result)
    if result_path.exists():
        raise FileExistsError("Arena fixture refuses to overwrite a result")
    directory = result_path.parent / "arena"
    directory.mkdir()
    firmwares = tuple(Path(path).resolve(strict=True) for path in args.firmware)
    manifest = write_manifests(directory, firmwares)
    binary = arena.resolve_qemu(args.qemu)
    report = {"status": "running", "mode": args.mode, "scene": "admitted-arena",
        "native_calls": {"reset": 0, "step_ns": [], "close": 0}, "responses": [],
        "transport_done": [],
        "commands": [], "can_receive_errors": [], "feedback": [], "cosim_sends": [], "qmp_events": [],
        "cleanup": [], "upper_cleanup": [], "uart": ["", ""],
        "entrypoint": str(Path(__file__).resolve()), "cwd": str(Path.cwd()),
        "limitations": ["physical calibration SKIP", "bare-MMIO lifecycle consumer, not HAL/BSP closed loop",
            "Cyclic encoder oracle admits only exact double modulo tau/zero wrap equivalence.",
            "Raw samples include actual filter history; independent fresh first-sample gate is separate.",
            "QEMU timestamps and shared-world logical time are independent; no CPU barrier or RTF claim.",
            "Runtime response callback is not complete; test-only transport observer retains received DONE, including returned suffixes."],
        "inputs": {str(path): sha(path) for path in (binary, *firmwares,
            Path(__file__).resolve(), ROOT / "smoke/dm_mc02_shared_world_smoke.c",
            ROOT / "smoke/dm_mc02_bmi088_spi_fixture.h", ROOT / "smoke/dm_mc02_cosim_link_readback_smoke.ld",
            ROOT / "tools/run-qemu-shared-world-smoke.sh", ROOT / "models/two_four_wheel.xml",
            ROOT / "models/four_wheel.xml", ROOT / "models/mujoco-LICENSE.txt",
            manifest.path, *(member.robot.path for member in manifest.robots))}}
    origins = source_origins()
    observer = Observation(report, manifest, args.mode)
    observer.binary = binary
    if args.cli:
        report["cli"] = {}
        report["limitations"].append(
            "Instrumented runpy sim module entry in the existing shell subprocess; not an unmodified external python -m sim run.")
        for path in (ROOT / "sim/__main__.py", ROOT / "sim/cli.py"):
            report["inputs"][str(path)] = sha(path)
    overrides = {"create_arena_plant": "world", "running_qemu": "qemu", "QmpSession": "qmp",
        "CanStream": "can", "StepFramedSocket": "cosim", "SharedWorldRuntime": "shared", "ChildProcess": "child"}
    for original, replacement in overrides.items():
        setattr(observer, "original_" + replacement, getattr(arena, original))
    if args.cli:
        observer.original_run = arena.run_manifest
        overrides["run_manifest"] = "run"
    primary = None
    try:
        with patch.multiple(arena, **{name: getattr(observer, method) for name, method in overrides.items()}):
            if args.cli:
                result = invoke_cli(observer, result_path.parent / "cli-result.json")
            else:
                result = arena.run_manifest(manifest, duration=.05, qemu=binary,
                    startup_timeout=10., timeout=15., emit=observer.emit, on_response=observer.response)
        report["runtime_result"] = asdict(result)
        assert args.mode == "normal", "negative fixture completed without observing its native event"
        assert result.end_ns - result.origin_ns == 50_000_000
        assert result.steps == len(report["native_calls"]["step_ns"])
        assert result.end_ns == sum(report["native_calls"]["step_ns"])
        assert [board.ready for board in result.boards] == ["uart_marker"] * 2
    except BaseException as error:
        primary = error
    verification_errors = []
    # Cleanup/source checks are independent of data assertions and still run
    # when the native consumer or its earlier verification has failed.
    for identity, owner in zip(report["cleanup"], observer.owners):
        identity.update(alive=starttime(identity["pid"]) == identity["starttime"],
            returncode=owner.process.poll(), temporary_exists=Path(identity["tempdir"]).exists())
        if identity["alive"] or identity["returncode"] is None or identity["temporary_exists"]:
            verification_errors.append(RuntimeError("Arena QEMU cleanup incomplete"))
    for identity, child in zip(report["upper_cleanup"], observer.children):
        identity.update(alive=starttime(identity["pid"]) == identity["starttime"],
            returncode=child.process.poll(), pipes_closed=child.process.stdout.closed and child.process.stderr.closed)
        if (identity["starttime"] is None or identity["alive"]
                or identity["returncode"] is None or not identity["pipes_closed"]):
            verification_errors.append(RuntimeError("Arena upper cleanup incomplete"))
    try:
        current = source_origins()
        assert all(current[name] == value for name, value in origins.items())
        assert all(sha(path) == digest for path, digest in report["inputs"].items())
        if args.cli:
            entry_sources = report["cli"]["actual_sources"]
            assert all(current[name] == value for name, value in entry_sources.items() if name != "__main__")
            assert sha(report["cli"]["entrypoint"]) == report["cli"]["entrypoint_sha256"]
            assert sha(report["cli"]["report_path"]) == report["cli"]["report_sha256"]
            report["cli"]["source_preserved"] = True
        report.update(actual_sources=current, source_preserved=True)
    except BaseException as error:
        verification_errors.append(error)
    try:
        calls = report["native_calls"]
        assert calls["reset"] == calls["close"] == 1 and calls["step_ns"]
        assert all(dt == 1_000_000 for dt in calls["step_ns"])
        assert len(observer.plant.devices) == 8 and report["world"]["distinct_buses"] == 2
        assert all(device.closed for device in observer.plant.devices)
        assert len(observer.owners) == 2 and len(observer.children) == 1
        assert all(not pending for pending in observer.feedback_sources.values())
        report["device_cleanup"] = [device.closed for device in observer.plant.devices]
        assert {event["peer"] for event in report["commands"]} == {0, 1}
        resets = {event["name"]: event["decoded"]["virtual_time_ns"]
            for event in report["responses"] if event["header"]["kind"] == codec.RESET_ACK}
        assert set(resets) == {member.name for member in manifest.robots}
        for event in report["feedback"]:
            assert event["qemu_ns"] == resets[manifest.robots[event["peer"]].name] + event["world_ns"]
        assert observer.completed_names() == {member.name for member in manifest.robots}
        for index, text in enumerate(report["uart"]):
            lines = [line for line in text.splitlines() if line.startswith("WORLD_RESULT=")]
            assert len(lines) == 1 and f"ARENA_READY={index}\n" in text
            fields = lines[0].split()
            assert len(fields) == 8 and fields[0] == f"WORLD_RESULT={index}"
            report.setdefault("raw_samples", []).append({"peer": index,
                "feedback_hex": fields[1:5], "gyro": struct.unpack("<3h", bytes.fromhex(fields[5])),
                "accel": struct.unpack("<3h", bytes.fromhex(fields[6])), "sensortime": int(fields[7], 16)})
            for identifier, data in enumerate(fields[1:5], 0x201):
                assert any(event["peer"] == index and event["can_id"] == identifier
                           and event["data_hex"] == data and event["accepted"] for event in report["feedback"])
        upper = observer.children[0]
        report["upper_output"] = upper.diagnostics()
        output = json.loads(report["upper_output"]["stdout"].splitlines()[0])
        assert output == {"cwd": str(manifest.robots[0].robot.path.parent), "argv": list(LITERAL_ARGS)}
        assert not (manifest.robots[0].robot.path.parent / "forbidden").exists()
        if args.mode != "normal":
            assert report.get("injected") == args.mode
            if args.mode == "peer-exit":
                assert report["cleanup"][1]["returncode"] == 0
                assert any(event["peer"] == 1 and event["command"] == "quit"
                           for event in report["qmp_events"])
            if args.mode == "interrupt":
                assert args.cli and isinstance(primary, KeyboardInterrupt)
                assert str(primary) == "interrupted by signal 15"
                assert report.get("terminal_retry_verified")
            elif args.mode == "reset":
                assert isinstance(primary, V2SessionReset)
            elif args.mode == "peer-exit" and isinstance(primary, ExecInterruptedError):
                # QEMU can disconnect this native QMP before its quit response
                # or the following snapshot. Require the actual exit0 as well;
                # a different transport failure is not this expected event.
                assert str(primary) == "Disconnected"
                report["peer_exit_observation"] = "qmp_disconnect_then_exit0"
            elif args.mode == "peer-exit" and type(primary) is ConnectionError:
                assert str(primary) == "QEMU FDCAN socket closed"
                assert {"peer": 1, "error_type": "ConnectionError", "error": str(primary)} in report["can_receive_errors"]
                report["peer_exit_observation"] = "fdcan_eof_then_exit0"
            elif args.mode == "peer-exit" and type(primary) is BrokenPipeError:
                assert primary.errno == errno.EPIPE
                assert any(event["peer"] == 1 and not event["accepted"]
                    and event.get("send_error") == {"error_type": "BrokenPipeError",
                        "errno": errno.EPIPE, "error": str(primary)}
                    for event in report["cosim_sends"])
                report["peer_exit_observation"] = "cosim_broken_pipe_then_exit0"
            else:
                assert isinstance(primary, RuntimeError) and "exited early (0)" in str(primary)
                assert ("QEMU west" if args.mode == "peer-exit" else "east/upper:observer") in str(primary)
            if args.mode != "interrupt":
                assert report.get("freeze_verified")
            report["expected_failure_verified"] = True
        if args.cli:
            assert report["cli"].get("verified") and report["cli"].get("source_preserved")
    except BaseException as error:
        verification_errors.append(error)
    for error in verification_errors:
        if primary is None:
            primary = error
        else:
            primary.add_note(f"Arena post-cleanup verification failed: {type(error).__name__}: {error}")
    report["status"] = "passed" if primary is None else "failed"
    if primary is not None:
        report.update(error=f"{type(primary).__name__}: {primary}", notes=getattr(primary, "__notes__", []))
    result_path.write_text(json.dumps(report, sort_keys=True) + "\n")
    if primary is not None:
        print("RESULT_JSON: " + json.dumps(report, sort_keys=True), flush=True)
        raise primary
    return 0


if __name__ == "__main__":
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--firmware", nargs=2, required=True)
    parser.add_argument("--cli", action="store_true", help="exercise the real sim module CLI entry")
    parser.add_argument("--mode", choices=("normal", "reset", "peer-exit", "upper-exit", "interrupt"), default="normal")
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    if args.mode == "interrupt" and not args.cli:
        parser.error("--mode interrupt requires --cli")
    try:
        sys.exit(main(args))
    except ModuleNotFoundError as error:
        if error.name != "mujoco":
            raise
        print("SKIP: optional native MuJoCo runtime is unavailable", file=sys.stderr)
        sys.exit(77)
