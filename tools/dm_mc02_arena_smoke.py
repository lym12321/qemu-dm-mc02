#!/usr/bin/env python3
"""Actual native consumer of the admitted Arena process runtime.

The runtime owns every process, connection and shared-world operation. Bounded
test-only observers delegate once and retain actual data and cleanup evidence.
Raw samples retain their real filter history; the separate first-sample gate
owns the Bosch quantization oracle. This gate establishes no CPU time barrier.
"""

from contextlib import contextmanager
from dataclasses import asdict
import argparse
import json
import math
from pathlib import Path
import signal
import struct
import sys
import time
from unittest.mock import patch

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


def write_manifests(directory, firmwares):
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
            "ready": {"endpoint": "mcu.uart1", "text": f"ARENA_READY={index}\n"},
        }
        if index == 0:
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
            frames = receive()
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
            result = send(wire)
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
        if self.mode == "upper-exit":
            (self.manifest.robots[0].robot.path.parent / "exit-upper").write_text("exit0\n")
        else:
            index = 0 if self.mode == "reset" else 1
            self.qmps[index].settimeout(2.)
            self.qmps[index].command("system_reset" if self.mode == "reset" else "quit")


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
        "commands": [], "feedback": [], "cosim_sends": [], "qmp_events": [],
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
    overrides = {"create_arena_plant": "world", "running_qemu": "qemu", "QmpSession": "qmp",
        "CanStream": "can", "StepFramedSocket": "cosim", "SharedWorldRuntime": "shared", "ChildProcess": "child"}
    for original, replacement in overrides.items():
        setattr(observer, "original_" + replacement, getattr(arena, original))
    primary = None
    try:
        with patch.multiple(arena, **{name: getattr(observer, method) for name, method in overrides.items()}):
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
            assert report.get("injected") == args.mode and report.get("freeze_verified")
            if args.mode == "reset":
                assert isinstance(primary, V2SessionReset)
            elif args.mode == "peer-exit" and isinstance(primary, ExecInterruptedError):
                # QEMU can disconnect this native QMP before its quit response
                # or the following snapshot. Require the actual exit0 as well;
                # a different transport failure is not this expected event.
                assert str(primary) == "Disconnected"
                assert report["cleanup"][1]["returncode"] == 0
                assert any(event["peer"] == 1 and event["command"] == "quit"
                           for event in report["qmp_events"])
                report["peer_exit_observation"] = "qmp_disconnect_then_exit0"
            else:
                assert isinstance(primary, RuntimeError) and "exited early (0)" in str(primary)
                assert ("QEMU west" if args.mode == "peer-exit" else "east/upper:observer") in str(primary)
            report["expected_failure_verified"] = True
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
    parser.add_argument("--mode", choices=("normal", "reset", "peer-exit", "upper-exit"), default="normal")
    parser.add_argument("--result", required=True)
    try:
        sys.exit(main(parser.parse_args()))
    except ModuleNotFoundError as error:
        if error.name != "mujoco":
            raise
        print("SKIP: optional native MuJoCo runtime is unavailable", file=sys.stderr)
        sys.exit(77)
