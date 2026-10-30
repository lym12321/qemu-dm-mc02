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
import subprocess
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
MULTI_CUT_QUANTUM_NS = 400_000
CAN_DEADLINE_DELAY_NS = 250_000


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


def check_multi_cut_observations(report):
    """Check actual per-cut receipts; this does not produce native evidence."""
    evidence = report["multi_cut"]
    if evidence["quantum_ns"] != 400_000:
        raise ValueError("multi-cut quantum differs from the fixed400us fixture")
    steps = report["native_calls"]["step_ns"]
    if not steps or len(steps) % 3 or steps != [400_000, 400_000, 200_000] * (len(steps) // 3):
        raise ValueError("multi-cut World steps must be400/400/200us per outer1ms")
    evidence.update(verified=True, **_check_cut_observations(report, evidence, steps, "multi-cut"))


def _check_cut_observations(report, evidence, steps, label):
    """Share the actual CPU/World/STEP/ACK/CAN consumer checks across profiles."""
    events = evidence["events"]
    resets = {row["name"]: row["decoded"]["virtual_time_ns"] for row in report["responses"]
              if row["header"]["kind"] == codec.RESET_ACK}
    names = evidence["members"]
    if len(names) != 2 or set(resets) != set(names):
        raise ValueError(label + " requires both actual RESET_ACK origins")
    baselines = {}
    for event in events:
        if event["kind"] == "can-observation" and event["state"]["execution"]["state"] == "idle":
            baselines.setdefault(event["peer"], event["state"]["fdcan-rx-state"]
                                 ["controllers"]["dm-mc02.fdcan1"]["completed_frames"])
    if set(baselines) != {0, 1}:
        raise ValueError(label + " lacks actual initial native CAN baselines")
    starts = [index for index, row in enumerate(events)
              if row["kind"] == "cpu-request" and row["peer"] == 0]
    if len(starts) != len(steps):
        raise ValueError(label + " CPU request count differs from World cuts")
    target = 0
    unchanged_goals = 0
    previous_goals = dict(baselines)
    for step_id, (start, end, dt_ns) in enumerate(zip(
            starts, [*starts[1:], len(events)], steps, strict=True), 1):
        target += dt_ns
        cut = events[start:end]
        requests = [(row["peer"], row["target_ns"]) for row in cut if row["kind"] == "cpu-request"]
        if requests != [(peer, resets[name] + target) for peer, name in enumerate(names)]:
            raise ValueError(label + " CPU targets do not match independent RESET epochs")
        worlds = [(index, row) for index, row in enumerate(cut) if row["kind"] == "world-completed"]
        if (len(worlds) != 1 or worlds[0][1]["dt_ns"] != dt_ns
                or not math.isclose(worlds[0][1]["native_time_s"], target / 1e9, rel_tol=0, abs_tol=1e-12)):
            raise ValueError(label + " requires exactly one completed World step per cut")
        acknowledgements = []
        for peer, name in enumerate(names):
            sends = [(index, row["event"]) for index, row in enumerate(cut)
                     if row["kind"] == "step-send" and row["peer"] == peer]
            acks = [(index, row["event"]) for index, row in enumerate(cut)
                    if row["kind"] == "step-ack" and row["peer"] == peer]
            if len(sends) != 1 or len(acks) != 1:
                raise ValueError(label + " requires exactly one STEP and ACK per board/cut")
            send_index, sent = sends[0]
            ack_index, ack = acks[0]
            if sent["header"]["kind"] != codec.STEP or ack["header"]["kind"] != codec.STEP_ACK:
                raise ValueError(label + " STEP/ACK message kinds disagree")
            expected = (2, peer + 1, step_id, target, dt_ns)
            for event in (sent, ack):
                header = event["header"]
                if tuple(header[key] for key in ("version", "flags", "step_id", "t_sim_ns", "dt_ns")) != expected:
                    raise ValueError(label + " STEP/ACK wire identity differs from actual cut")
            if (not sent["accepted"] or sent["imu"]["sample_id"] != step_id
                    or sent["imu"]["sensor_time_ns"] != target or ack["decoded"]["status"] != 0
                    or not worlds[0][0] < send_index < ack_index):
                raise ValueError(label + " native exchange did not follow completed World state")
            acknowledgements.append(ack_index)
        # Each segment ends before the next CPU request. Require a fresh native
        # observation after both ACKs even when no1ms CAN producer was due.
        for peer, name in enumerate(names):
            complete = []
            for index, row in enumerate(cut):
                if row["kind"] != "can-observation" or row["peer"] != peer or index <= max(acknowledgements):
                    continue
                state = row["state"]
                native_target = resets[name] + target
                execution, status = state["execution"], state["status"]
                receipt = state["fdcan-rx-state"]
                rx = receipt["controllers"]["dm-mc02.fdcan1"]
                goal = baselines[peer] + row["submitted_frames"]
                if (all(execution.get(key) == value for key, value in {
                        "state": "reached", "current-ns": native_target, "target-ns": native_target}.items())
                        and status["running"] is False and status["status"] == "paused"
                        and receipt["timestamp_ns"] == native_target
                        and rx["completed_frames"] == goal and rx["partial_bytes"] == 0
                        and state["fdcan-transports"]["dm-mc02.fdcan1"]["connected"] is True):
                    complete.append(goal)
            if not complete or complete[-1] < previous_goals[peer]:
                raise ValueError(label + " lacks completed native CAN receipt before next CPU target")
            unchanged_goals += complete[-1] == previous_goals[peer]
            previous_goals[peer] = complete[-1]
    if report["runtime_result"]["steps"] != len(steps) or report["runtime_result"]["end_ns"] != target:
        raise ValueError(label + " final result differs from completed native cuts")
    return dict(cuts=len(steps), unchanged_receipt_goals=unchanged_goals,
                final_receipt_goals=previous_goals)


def check_can_deadline_observations(report):
    """Check known queued dues, without extending execution to drain the future."""
    evidence = report["can_deadlines"]
    if evidence["delay_ns"] != CAN_DEADLINE_DELAY_NS or evidence["outer_step_ns"] != 1_000_000:
        raise ValueError("CAN-deadlines requires fixed250us left delay and original1ms outer steps")
    steps = report["native_calls"]["step_ns"]
    if not steps or any(not 0 < value <= 1_000_000 for value in steps):
        raise ValueError("CAN-deadlines World cuts must remain inside original1ms outer steps")
    result = report["runtime_result"]
    if result["end_ns"] - result["origin_ns"] != 50_000_000:
        raise ValueError("CAN-deadlines changed the original50ms duration")
    checked = _check_cut_observations(report, evidence, steps, "CAN-deadlines")
    cuts, target = set(), 0
    for dt_ns in steps:
        target += dt_ns
        cuts.add(target)
    produced = {(row["peer"], row["can_id"], row["world_ns"]): row
                for row in evidence["events"] if row["kind"] == "feedback-produced"}
    delivered = {(row["peer"], row["can_id"], row["world_ns"]): row for row in report["feedback"]}
    pending = {(row["peer"], row["can_id"], row["world_ns"]): row for row in evidence["pending_feedback"]}
    if (not produced or len(delivered) != len(report["feedback"])
            or len(pending) != len(evidence["pending_feedback"])
            or set(delivered) & set(pending) or set(produced) != set(delivered) | set(pending)):
        raise ValueError("CAN-deadlines feedback producer/delivery/pending attribution differs")
    delayed_cuts = set()
    for key, source in produced.items():
        delay = CAN_DEADLINE_DELAY_NS if key[0] == 0 else 0
        due = source["world_ns"] + delay
        if source["due_ns"] != due:
            raise ValueError("CAN-deadlines source timestamp and due differ")
        if due <= target:
            row = delivered.get(key)
            if (row is None or not row["accepted"] or row["due_ns"] != due
                    or row["delivered_ns"] != due or row["data_hex"] != source["data_hex"]
                    or due not in cuts):
                raise ValueError("CAN-deadlines feedback did not deliver at its actual due cut")
            if delay:
                delayed_cuts.add(due)
        elif key not in pending or pending[key]["due_ns"] != due:
            raise ValueError("CAN-deadlines future feedback must remain explicitly pending")
    if 1_250_000 not in delayed_cuts or not pending or any(key[0] != 0 for key in pending):
        raise ValueError("CAN-deadlines lacks the1.25ms cut or final future left feedback")
    buses = evidence["final_buses"]
    if len(buses) != 2:
        raise ValueError("CAN-deadlines requires both final borrowed bus observations")
    for peer, bus in enumerate(buses):
        future = [row["due_ns"] for key, row in pending.items() if key[0] == peer]
        if (bus["time_ns"] != target or bus["pending"] != len(future)
                or bus["next_due_ns"] != (min(future) if future else None)):
            raise ValueError("CAN-deadlines final bus state differs from retained future feedback")
    evidence.update(verified=True, **checked, delayed_delivery_cuts=sorted(delayed_cuts),
                    delivered_feedback=len(delivered), future_feedback=len(pending))


def write_manifests(directory, firmwares, *, referees=False, dji_scenario=False):
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
                "TTEST READY dji.scenario.ready 0 0\n" if dji_scenario else
                "TTEST READY referee.arena.ready 0 0\n" if referees else f"ARENA_READY={index}\n")},
        }
        if index == 0 and not referees and not dji_scenario:
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
    feedback_bound = 2048
    step_bound = 512

    def __init__(self, report, manifest, mode):
        self.report, self.manifest, self.mode = report, manifest, mode
        self.owners, self.qmps, self.children = [], [], []
        self.plant = self.runtime = None
        self.can_count = self.cosim_count = 0
        self.feedback_sources = {}
        self.feedback_deliveries = {}
        self.run_result = self.run_error = None

    def multi_event(self, kind, **values):
        for profile in ("multi_cut", "can_deadlines"):
            if profile in self.report:
                append_bounded(self.report[profile], "events", {"kind": kind, **values}, 32768)

    def run(self, manifest, **options):
        """Observe the actual CLI's single call; never execute another loop."""
        return self.record_cli_run(manifest, options, expected_manifest=self.manifest,
            expected_options={"duration": .05, "qemu": str(self.binary),
                              "startup_timeout": 10., "timeout": 15.})

    def record_cli_run(self, manifest, options, *, expected_manifest, expected_options,
                       scenario=False):
        """Retain one actual module call, including admission failures."""
        try:
            evidence = self.report["cli"]
            evidence["runtime_calls"] += 1
            entrypoint = Path(sys.modules["__main__"].__file__).resolve()
            evidence.update(entrypoint=str(entrypoint), entrypoint_sha256=sha(entrypoint),
                module_argv=list(sys.argv), actual_sources=source_origins(),
                runtime_options={key: str(value) if key == "qemu" else value
                    for key, value in options.items() if key != "emit"})
            assert evidence["runtime_calls"] == 1
            assert manifest == expected_manifest and manifest is not expected_manifest
            assert entrypoint == ROOT / "tools/sim-unmigrated"
            assert evidence["runtime_options"] == expected_options
            emit = options.pop("emit")
            assert callable(emit)
            if scenario:
                assert manifest.arena == expected_manifest.arena and manifest.arena is not expected_manifest.arena
                evidence.update(manifest_equal=True, manifest_distinct=True,
                    arena_equal=True, arena_distinct=True, manifest_identity=id(manifest),
                    arena_identity=id(manifest.arena))
                self.scenario_manifest, self.manifest = manifest, manifest.arena

            def observed_emit(event):
                emit(event)
                self.emit(event)

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
            append_bounded(self.report["native_calls"], "step_ns", dt_ns, self.step_bound)
            result = step(dt_ns)
            if "multi_cut" in self.report or "can_deadlines" in self.report:
                self.multi_event("world-completed", dt_ns=dt_ns, native_time_s=native.time_s)
            return result

        def observed_close():
            self.report["native_calls"]["close"] += 1
            return close()

        native.reset, native.step, native.close = observed_reset, observed_step, observed_close
        for index, device in enumerate(plant.devices):
            member = manifest.robots[index // 4]
            motor = member.robot.devices[index % 4]
            self.feedback_sources[index] = {}
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
                if frame.timestamp_ns in self.feedback_sources[index]:
                    raise ValueError("Arena fixture duplicate feedback source timestamp")
                if len(self.feedback_sources[index]) >= 256:
                    raise BufferError("Arena fixture pending feedback source exhausted")
                if "can_deadlines" in self.report:
                    source["due_ns"] = frame.timestamp_ns + device.bus.delay_ns
                    self.multi_event("feedback-produced", peer=index // 4,
                        can_id=frame.can_id, **source)
                self.feedback_sources[index][frame.timestamp_ns] = source
                return frame

            device.feedback = observed_feedback
        buses = tuple(dict.fromkeys(device.bus for device in plant.devices))
        if "can_deadlines" in self.report:
            buses[0].delay_ns = CAN_DEADLINE_DELAY_NS
        for peer, bus in enumerate(buses):
            self.feedback_deliveries[peer] = None
            log = bus.log

            def observed_delivery(delivery, peer=peer, log=log):
                if log is not None:
                    log(delivery)
                if delivery.destination != "qemu" or delivery.dropped:
                    return
                frame = delivery.frame
                assert 0x201 <= frame.can_id <= 0x204, repr(delivery)
                sources = self.feedback_sources[peer * 4 + frame.can_id - 0x201]
                assert delivery.timestamp_ns in sources, "Arena feedback delivery has no producer source"
                source = sources[delivery.timestamp_ns]
                assert source["world_ns"] == delivery.timestamp_ns == frame.timestamp_ns, repr(delivery)
                assert source["data_hex"] == frame.data.hex(), repr(delivery)
                assert self.feedback_deliveries[peer] is None, "unconsumed Arena feedback delivery"
                # Public CanBus.log runs immediately before this destination's
                # receive. One attribution follows actual delivery order;
                # producer order may differ after a delay reduction.
                self.feedback_deliveries[peer] = delivery

            bus.log = observed_delivery
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
            command, command_many = qmp.command, qmp.command_many
            index = len(self.qmps) - 1
            execution = {"peer": index, "successful_runs": 0,
                         "first_target_ns": None, "last_target_ns": None,
                         "last_execution": None, "last_status": None}
            self.report.setdefault("cpu_execution", []).append(execution)
            groups = {"peer": index, "calls": 0, "completed": 0,
                      "initial_groups": 0, "can_groups": 0}
            self.report.setdefault("qmp_observation_groups", []).append(groups)

            def observe_value(name, arguments, value):
                first_run = name == "x-run-to-virtual-time" and not execution["successful_runs"]
                if name == "x-run-to-virtual-time":
                    target = arguments["target-ns"]
                    execution["successful_runs"] += 1
                    if first_run:
                        execution["first_target_ns"] = target
                    execution["last_target_ns"] = target
                elif name == "x-query-virtual-execution":
                    execution["last_execution"] = dict(value) if isinstance(value, dict) else value
                elif name == "query-status":
                    execution["last_status"] = dict(value) if isinstance(value, dict) else value
                elif name == "qom-get" and arguments.get("property") == "can-execution-state":
                    execution["last_execution"] = dict(value["execution"])
                    execution["last_status"] = dict(value["status"])
                if name in ("cont", "stop", "system_reset", "quit") or first_run:
                    append_bounded(self.report, "qmp_events", {
                        "peer": index, "command": name, "result": value,
                        **({"target_ns": target} if first_run else {})}, 16)
                return value

            def observed_command(name, arguments=None):
                if name == "x-run-to-virtual-time":
                    self.multi_event("cpu-request", peer=index, target_ns=arguments["target-ns"])
                try:
                    value = command(name, arguments)
                except BaseException as error:
                    append_bounded(self.report, "qmp_events", {
                        "peer": index, "command": name,
                        "error": f"{type(error).__name__}: {error}"}, 16)
                    raise
                return observe_value(name, arguments, value)

            def observed_many(requests):
                groups["calls"] += 1
                try:
                    values = command_many(requests)
                except BaseException as error:
                    append_bounded(self.report, "qmp_events", {
                        "peer": index, "command": "command_many",
                        "error": f"{type(error).__name__}: {error}"}, 16)
                    raise
                # Values are observed after the entire native group returns;
                # these are not individual reply-arrival timestamps.
                for (name, arguments), value in zip(requests, values, strict=True):
                    observe_value(name, arguments, value)
                    if (("multi_cut" in self.report or "can_deadlines" in self.report) and name == "qom-get"
                            and arguments.get("property") == "can-execution-state"):
                        self.multi_event("can-observation", peer=index,
                            state=json.loads(json.dumps(value)),
                            submitted_frames=self.runtime.bindings[index].endpoint.submitted_frames)
                groups["completed"] += 1
                initial = execution["last_execution"]["state"] == "idle"
                groups["initial_groups" if initial else "can_groups"] += 1
                return values

            qmp.command, qmp.command_many = observed_command, observed_many
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
            delivery = self.feedback_deliveries[index]
            assert delivery is not None, "Arena feedback send has no delivery attribution"
            # An attempt consumes the attribution even if validation or the
            # original send fails. Keep its source until transport admission.
            self.feedback_deliveries[index] = None
            original = delivery.frame
            assert (can_id, flags, dlc, data) == (
                original.can_id, original.flags, original.dlc, original.data), repr(delivery)
            device_index = index * 4 + can_id - 0x201
            device = self.plant.devices[device_index]
            source = self.feedback_sources[device_index][delivery.timestamp_ns]
            assert source["data_hex"] == data.hex()
            state = source["joint"]
            encoder, rpm, current, temperature, reserved = struct.unpack(">HhhBB", data)
            # DJI C620 V1.01 encoder8192/rpm and M3508 P19 V1.0 ratio3591/187.
            position = (state["position_rad"] * (3591 / 187)) % math.tau
            event = {"peer": index, "can_id": can_id, "qemu_ns": timestamp_ns,
                     "world_ns": source["world_ns"], "data_hex": data.hex(), "accepted": False,
                     "joint": state, "rotor_position_mod_tau": position,
                     "producer_current_raw": source["current_raw"],
                     "send_live_current_raw": device.current_raw,
                     "sequence": delivery.sequence, "due_ns": delivery.due_ns,
                     "delivered_ns": delivery.delivered_ns}
            append_bounded(self.report, "feedback", event, self.feedback_bound)
            # An extremely small negative angle can project to exact tau in
            # double modulo. On the documented cyclic8192 encoder this exact
            # boundary is zero; accept only that equality, not a wider bin.
            assert (encoder == 0 and position == math.tau) or (
                encoder * math.tau / 8192 <= position < (encoder + 1) * math.tau / 8192), repr(event)
            assert abs(state["velocity_rad_s"] * (3591 / 187) - rpm * math.pi / 30) <= math.pi / 60, repr(event)
            assert (flags, dlc, current, temperature, reserved) == (0, 8, source["current_raw"], 25, 0), repr(event)
            result = send(can_id, flags, dlc, timestamp_ns, data)
            # Accepted means the original bounded stream admitted this record;
            # physical suffix completion and guest consumption are separate.
            event["accepted"] = True
            del self.feedback_sources[device_index][delivery.timestamp_ns]
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
                self.command(index, can_id, flags, dlc, stamp, data)
            return frames

        stream.send, stream.receive = observed_send, observed_receive
        return stream

    def command(self, index, can_id, flags, dlc, stamp, data):
        append_bounded(self.report, "commands", {"peer": index,
            "can_id": can_id, "flags": flags, "dlc": dlc,
            "qemu_ns": stamp, "data_hex": data.hex()}, 8)
        assert (can_id, flags, dlc, data.hex()) == (0x200, 0, 8, COMMANDS[index])

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
                self.multi_event("step-send", peer=index, event=event)
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
        if "multi_cut" in self.report:
            kwargs["quantum_ns"] = MULTI_CUT_QUANTUM_NS
        runtime = self.original_shared(*args, **kwargs)
        self.runtime = runtime
        close = runtime.close

        def observed_close():
            # The runtime publishes only complete groups; a failed step may
            # have later native effects beyond this retained diagnostic cut.
            self.report["can_input_states"] = runtime.can_input_states
            if "can_deadlines" in self.report:
                evidence = self.report["can_deadlines"]
                evidence["pending_feedback"] = [dict(peer=index // 4,
                    can_id=0x201 + index % 4, **source)
                    for index, sources in self.feedback_sources.items() for source in sources.values()]
                evidence["final_buses"] = [dict(time_ns=item.endpoint.bus.t_sim_ns,
                    pending=item.endpoint.bus.pending, next_due_ns=item.endpoint.bus.next_due_ns)
                    for item in runtime.bindings]
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
        event = {"name": name, "header": asdict(frame.header),
                 "payload_hex": frame.payload.hex(), "decoded": None if decoded is None else asdict(decoded)}
        append_bounded(self.report, "responses", event)
        if frame.header.kind == codec.STEP_ACK:
            self.multi_event("step-ack", peer=[member.name for member in self.manifest.robots].index(name),
                             event=event)
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


def invoke_cli(observer, report_path, *, scenario=False):
    """Run the selected package's real tool entry within this instrumented fixture."""
    evidence = observer.report["cli"]
    if scenario:
        argv = ["sim", "scenario", "run", str(observer.scenario_manifest.path),
            "--qemu", str(observer.binary), "--startup-timeout", "10", "--timeout", "20",
            "--report", str(report_path)]
    else:
        argv = ["sim", "arena", "run", str(observer.manifest.path), "--duration", "0.05",
            "--qemu", str(observer.binary), "--startup-timeout", "10", "--timeout", "15",
            "--report", str(report_path)]
    evidence.update(argv=argv, report_path=str(report_path), runtime_calls=0)
    previous_argv = sys.argv
    try:
        sys.argv = list(argv)
        try:
            runpy.run_path(str(ROOT / "tools/sim-unmigrated"), run_name="__main__")
        except SystemExit as exit:
            evidence["exit_code"] = exit.code
        else:
            raise AssertionError("sim module entry did not exit")
    finally:
        sys.argv = previous_argv
    try:
        assert evidence["runtime_calls"] == 1
        assert evidence["module_argv"] == [str(ROOT / "tools/sim-unmigrated"), *argv[1:]]
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


def check_cli_sources(observer, current):
    """The runpy module object is temporary; its actual entry file is retained."""
    evidence = observer.report["cli"]
    assert all(current[name] == value for name, value in evidence["actual_sources"].items()
               if name != "__main__")
    assert sha(evidence["entrypoint"]) == evidence["entrypoint_sha256"]
    assert sha(evidence["report_path"]) == evidence["report_sha256"]
    evidence["source_preserved"] = True


REFEREE_IDS = {"east": 3, "west": 103}
REFEREE_PAYLOADS = {
    "east": ("0100000807060504030201", "03026400c80000006400500005",
             "000000000000000028000a001400", "050900070b0d00fe"),
    "west": ("0200000807060504030201", "67024600c80000006400780006",
             "000000000000000029000c001900", "08110013171d0080"),
}
SCENARIO_EVENT_NS = 200_000_000
SCENARIO_EAST_PAYLOADS = (
    "0100000807060504030201", "03024600c80000006400500005",
    "000000000000000028000f001b00", "08110013171d0080",
)
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


def write_scenario_manifest(directory, firmwares):
    """Load the fixed Scenario through its public producer, without execution."""
    from sim.scenario.manifest import load_manifest as load_scenario

    manifest = write_manifests(directory, firmwares, referees=True)
    document = {"schema": 1, "arena": manifest.path.name, "duration": "1s",
        "referee_events": {"east": [
            {"at": "200ms", "hit": {"damage_hp": 30}},
            {"at": "200ms", "shot": {"heat_17mm": 5, "heat_42mm": 7}},
            {"at": "200ms", "buff": {"recovery_percent": 8, "cooling_per_s": 17,
                "defence_percent": 19, "vulnerability_percent": 23,
                "attack_percent": 29, "remaining_energy": 0x80}}], "west": []}}
    path = directory / "scenario.yaml"
    path.write_text(json.dumps(document))
    return load_scenario(path)


def check_referee_wire(wire, name, sequence, *, logical_ns=None, scenario=False):
    """Independent literal payloads and pinned CRC, without production encoders."""
    if type(wire) is not bytes or len(wire) != 82:
        raise ValueError("Arena referee requires a complete82-byte snapshot")
    payloads = REFEREE_PAYLOADS[name]
    if scenario:
        if type(logical_ns) is not int or logical_ns < 0:
            raise ValueError("Scenario referee wire requires a nonnegative logical timestamp")
        if name == "east" and logical_ns >= SCENARIO_EVENT_NS:
            payloads = SCENARIO_EAST_PAYLOADS
    cursor = 0
    for offset, (command, payload_hex) in enumerate(zip(
            (0x0001, 0x0201, 0x0202, 0x0204), payloads)):
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


def check_scenario_observations(report):
    """Check causal guest observations and the retained original queue calls."""
    evidence = report["scenario_observation"]
    samples = evidence["samples"]
    if evidence["event_ns"] != SCENARIO_EVENT_NS:
        raise ValueError("Scenario referee event boundary disagrees")
    for name in REFEREE_IDS:
        rows = samples[name]
        if (set(rows) != {"1", "2", "3"}
                or not 0 <= rows["1"]["logical_ns"] < SCENARIO_EVENT_NS
                or any(rows[str(index)]["logical_ns"] > rows[str(index + 1)]["logical_ns"]
                       for index in (1, 2))):
            raise ValueError("Scenario referee complete baseline/sample observations disagree")
    if any(samples["east"][sample]["logical_ns"] < SCENARIO_EVENT_NS for sample in ("2", "3")):
        raise ValueError("Scenario referee changed observation preceded its event")
    stage, control = evidence["stage"], evidence["control"]
    if (stage is None or control is None or control.get("send_attempts") != 1
            or control.get("wire_hex") != "a5" or not control.get("accepted")
            or not control.get("completed") or control.get("flush_attempts", 0) < 1
            or control["logical_ns"] < max(stage["logical_ns"], samples["east"]["2"]["logical_ns"])
            or control["completed_logical_ns"] < control["logical_ns"]
            or samples["west"]["2"]["logical_ns"] < control["completed_logical_ns"]):
        raise ValueError("Scenario referee single borrowed UART observation control disagrees")
    start, calls = evidence["shared_start"], evidence["queue_calls"]
    if (start != {"session_id": 1, "origin_ns": 0, "accepted": True}
            or len(calls) != 2 or [row["member"] for row in calls] != list(REFEREE_IDS)
            or any(not row["accepted"] or not row["exact_batch"] or not row["before_cont"]
                   or row["session_id"] != start["session_id"] or row["logical_ns"] != 0
                   or row["offsets_ns"] != ([SCENARIO_EVENT_NS] * 3 if row["member"] == "east" else [])
                   for row in calls)):
        raise ValueError("Scenario referee original queue/session/before-cont evidence disagrees")


class FirmwareObservation(Observation):
    """Shared acquisition and watchdog receipts for actual UART firmware gates."""

    consumer_label = "Arena referee"

    def __init__(self, report, manifest):
        super().__init__(report, manifest, "normal")
        self.sockets = []

    def check_cont(self):
        pass

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
                    primary.add_note(f"{self.consumer_label} stderr capture failed: {type(error).__name__}: {error}")
                    if error is not primary:
                        for note in tuple(getattr(error, "__notes__", ())):
                            primary.add_note(note)

    def qmp(self, *args, **options):
        qmp = super().qmp(*args, **options)
        try:
            index = len(self.qmps) - 1
            name = self.manifest.robots[index].name
            command, close = qmp.command, qmp.close
            execution_started = False
            row = {"attempts": 0, "completed": False}
            self.report["qmp_cleanup"].append(row)
            def observed_command(operation, arguments=None):
                nonlocal execution_started
                if operation in ("cont", "x-run-to-virtual-time") and not execution_started:
                    self.check_cont()
                    execution_started = True
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

    def watchdog(self, index, name, stage):
        from tools.dm_mc02_firmware_rtf import deadline_qmp_call, read_iwdg_timeouts
        deadline = self.owners[0].start_ns / 1e9 + 20
        value = deadline_qmp_call(self.qmps[index], deadline, self.consumer_label + " watchdog",
                                 lambda: read_iwdg_timeouts(self.qmps[index]))
        timeouts, text = value
        self.report["watchdogs"].setdefault(name, {})[stage] = {"timeouts": timeouts, "diagnostics": text}
        if timeouts or "window-violations=0" not in text:
            raise ValueError("watchdog event during " + self.consumer_label + " consumer")


    def channels(self, *, uarts=None, **options):
        if self.scenario and len(self.controls) >= len(self.manifest.robots):
            raise ValueError("duplicate Scenario referee UART channel construction")
        channels = self.original_channels(uarts=uarts, **options)
        if self.scenario:
            name = self.manifest.robots[len(self.controls)].name
            if "uart1" not in uarts or name in self.controls:
                raise ValueError("Scenario referee requires its existing member UART1")
            self.controls[name] = uarts["uart1"]
        return channels


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
        # ACK/DONE callbacks run before Simulation commits its public time.
        # These control receipts belong to the actual received frame's cut.
        previous = getattr(self, "_response_cut_ns", None)
        self._response_cut_ns = (frame.header.t_sim_ns
            if frame.header.kind in (codec.STEP_ACK, codec.STEP_DONE) else None)
        try:
            self.service_control()
        finally:
            self._response_cut_ns = previous

    def observation_cut_ns(self):
        pending = getattr(self, "_response_cut_ns", None)
        return self.runtime.t_sim_ns if pending is None else pending

    def service_control(self):
        pass


class RefereeObservation(FirmwareObservation):
    """Borrow existing owners; observe this UART consumer with bounded evidence."""

    def __init__(self, report, manifest, *, scenario=False, scenario_manifest=None):
        super().__init__(report, manifest)
        self.scenario, self.scenario_manifest = scenario, scenario_manifest
        self.engines, self.endpoints = [], []
        self.buffers = {name: bytearray() for name in REFEREE_IDS}
        self.controls = {}

    def run(self, manifest, **options):
        """The actual Scenario CLI reload becomes the queue identity source."""
        return self.record_cli_run(manifest, options, expected_manifest=self.scenario_manifest,
            expected_options={"qemu": str(self.binary), "startup_timeout": 10., "timeout": 20.},
            scenario=True)

    def check_cont(self):
        if self.scenario:
            evidence = self.report["scenario_observation"]
            if (evidence["shared_start"] is None or not evidence["shared_start"]["accepted"]
                    or len(evidence["queue_calls"]) != 2
                    or any(not item["accepted"] or not item["exact_batch"]
                           or not item["before_cont"] for item in evidence["queue_calls"])):
                raise ValueError("Scenario referee resume preceded original batch queueing")

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
            if self.scenario:
                start, queue = runtime.start, runtime.queue_referee_events
                evidence = self.report["scenario_observation"]
                def observed_start(session, wire_sessions, *, deadline):
                    if evidence["shared_start"] is not None:
                        raise ValueError("duplicate Scenario referee shared start")
                    evidence["shared_start"] = {"session_id": session.session_id,
                        "origin_ns": session.t_sim_ns, "accepted": False}
                    result = start(session, wire_sessions, deadline=deadline)
                    evidence["shared_start"]["accepted"] = True
                    return result
                def observed_queue(name, events, *, session_id):
                    expected = next((item.events for item in self.scenario_manifest.referees
                                     if item.member == name), None)
                    row = {"member": name, "session_id": session_id,
                        "logical_ns": runtime.t_sim_ns, "batch_identity": id(events),
                        "exact_batch": events is expected,
                        "offsets_ns": [event.offset_ns for event in events],
                        "before_cont": not any(item["command"] in ("cont", "x-run-to-virtual-time")
                                               for item in self.report["qmp_events"]), "accepted": False}
                    append_bounded(evidence, "queue_calls", row, 2)
                    result = queue(name, events, session_id=session_id)
                    row["accepted"] = True
                    return result
                runtime.start, runtime.queue_referee_events = observed_start, observed_queue
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
                check_referee_wire(wire, name, sequence, logical_ns=row["logical_ns"], scenario=self.scenario)
                row["verified"] = True
                return wire
            runtime.publish_referee = observed_publish
            return runtime
        except BaseException as error:
            close_observation_failure(runtime, "shared world", error)
            raise

    def service_control(self):
        """At most one flush per callback; the existing loop owns all waiting."""
        if not self.scenario:
            return
        evidence = self.report["scenario_observation"]
        east = evidence["samples"]["east"].get("2")
        if evidence["stage"] is None or east is None:
            return
        stream = self.controls["west"]
        control = evidence["control"]
        if control is None:
            if any("1" not in rows or rows["1"]["logical_ns"] >= SCENARIO_EVENT_NS
                   for rows in evidence["samples"].values()):
                raise ValueError("Scenario referee requires both complete baselines before its event")
            if stream.tx_pending:
                raise ValueError("Scenario referee control UART has prior pending output")
            control = evidence["control"] = {"logical_ns": self.observation_cut_ns(),
                "wire_hex": "a5", "send_attempts": 1, "accepted": False,
                "flush_attempts": 1, "completed": False,
                "borrowed_stream_identity": id(stream)}
            # StreamTransport.send owns the first real flush attempt.
            stream.send(b"\xa5")
            control["accepted"] = True
        elif stream.tx_pending:
            control["flush_attempts"] += 1
            stream.flush()
        if not stream.tx_pending and not control["completed"]:
            control.update(completed=True, completed_logical_ns=self.observation_cut_ns())

    def scenario_case(self, name, row, poll_sample_qemu_ns):
        prefix = "referee.arena."
        if not self.scenario or not row["name"].startswith(prefix):
            return
        sample = row["name"].split(".")[2]
        evidence = self.report["scenario_observation"]
        now = self.runtime.t_sim_ns
        if sample == "1" and now >= SCENARIO_EVENT_NS:
            raise ValueError("Scenario referee complete baseline report missed the event boundary")
        if name == "east" and sample in ("2", "3") and now < SCENARIO_EVENT_NS:
            raise ValueError("Scenario referee changed report preceded its event boundary")
        if name == "west" and sample in ("2", "3"):
            control = evidence["control"]
            if control is None or not control["completed"]:
                raise ValueError("Scenario referee west sample preceded observation control")
        labels = {label for label in REFEREE_REQUIRED if label.startswith(prefix + sample + ".")}
        observed = [item["name"] for item in self.report["cases"][name]
                    if item["name"].startswith(prefix + sample + ".")]
        if len(observed) > len(labels) or len(observed) != len(set(observed)):
            raise ValueError("duplicate Scenario referee sample report")
        if set(observed) == labels:
            evidence["samples"][name][sample] = {"logical_ns": now,
                "poll_sample_qemu_ns": poll_sample_qemu_ns}

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
                if self.scenario and fields[2] == "referee.scenario.observe":
                    evidence = self.report["scenario_observation"]
                    if (event.name != "west" or row["actual"] != 0 or row["expected"] != 0
                            or evidence["stage"] is not None or "1" not in evidence["samples"]["west"]):
                        raise ValueError("unexpected Scenario referee observation stage marker")
                    evidence["stage"] = {"logical_ns": self.runtime.t_sim_ns,
                        "poll_sample_qemu_ns": event.event.timestamp_ns}
                    continue
                if (fields[2] != "referee.arena.ready" or row["actual"] != 0 or row["expected"] != 0
                        or event.name in self.report["ready"]):
                    raise ValueError("unexpected Arena referee ready marker")
                self.report["ready"][event.name] = {"poll_sample_qemu_ns": event.event.timestamp_ns,
                                                    "logical_ns": self.runtime.t_sim_ns}
            else:
                append_bounded(self.report["cases"], event.name, row, 37)
                check_referee_case(row, event.name)
                self.scenario_case(event.name, row, event.event.timestamp_ns)
        self.service_control()


def verify_firmware_cleanup(report, observer, *, label, referees=False, controls=False):
    if observer is None:
        raise ValueError(label + " owner acquisition unavailable")
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
                    for index, item in enumerate(getattr(observer, "engines", ()))],
        "endpoints": [check(f"endpoint{index} close", lambda item=item: item.closed)
                      for index, item in enumerate(getattr(observer, "endpoints", ()))],
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
                check(binding.name + " stream socket identity", lambda sock=sock: id(getattr(sock, "borrowed_socket", sock)) in borrowed)
        for endpoint in getattr(observer, "endpoints", ()):
            check("referee UART socket identity", lambda endpoint=endpoint:
                id(endpoint.transport.sock) in borrowed)
        if controls:
            check("Scenario existing UART1 borrow scope", lambda: set(observer.controls) == {item.name for item in observer.manifest.robots})
            for name, stream in observer.controls.items():
                check(name + " existing control socket identity", lambda stream=stream: id(stream.sock) in borrowed)
    for index, row in enumerate(report["qmp_cleanup"]):
        check(f"QMP{index} close", lambda row=row: row == {"attempts": 1, "completed": True})
    if (len(observer.owners) != 2 or len(report["cleanup"]) != 2
            or len(getattr(observer, "engines", ())) != (2 if referees else 0)
            or len(getattr(observer, "endpoints", ())) != (2 if referees else 0)
            or observer.children or len(owned["devices"]) != 8
            or len(report["qmp_cleanup"]) != 2 or len(owned["borrowed_sockets"]) != (8 if referees else 6)):
        failures.append("owner acquisition scope incomplete")
    if failures:
        raise ValueError(label + " cleanup incomplete: " + "; ".join(failures))
    report["cleanup_verified"] = True


def verify_firmware_sources(report, inputs, firmware, *, observer=None, cli=False,
                            label="Arena referee"):
    from tools.dm_mc02_referee_test import checkout_snapshot
    if any(report.get(key) is None for key in ("inputs_before", "checkout_before", "actual_sources_before")):
        raise ValueError(label + " pre-run input/source state unavailable")
    report["inputs_after"], report["checkout_after"] = inputs(), checkout_snapshot(firmware)
    report["actual_sources_after"] = source_origins()
    if (report.get("inputs_before") != report["inputs_after"]
            or report.get("checkout_before") != report["checkout_after"]
            or any(report["actual_sources_after"].get(name) != value
                   for name, value in report["actual_sources_before"].items())):
        raise ValueError(label + " actual input/source state changed")
    if cli:
        if observer is None or not report["cli"].get("verified"):
            raise ValueError("Scenario referee actual CLI outcome was not verified")
        check_cli_sources(observer, report["actual_sources_after"])
    report["source_preserved"] = True


def verify_firmware_watchdogs(report, *, label="Arena referee"):
    if (set(report["watchdogs"]) != set(REFEREE_IDS)
            or any(set(rows) != {"done", "paused"} for rows in report["watchdogs"].values())
            or any(row["timeouts"] != 0 or "window-violations=0" not in row["diagnostics"]
                   for rows in report["watchdogs"].values() for row in rows.values())):
        raise ValueError(label + " watchdog observations missing")


DJI_SCENARIO_EVENT_NS = 250_000_000
DJI_SCENARIO_MEMBERS = ("east", "west")
DJI_SCENARIO_FIELDS = ("angle", "speed", "current", "temp", "ts", "output",
                       "enabled", "update_age", "feedback_age")
DJI_SCENARIO_REQUIRED = frozenset(("control.rx", "reset.poweron", "bsp.init", "task.stack",
    "dji.scenario.stop", "dji.can", *(f"dji.scenario.{phase}.id{identifier}.{field}"
        for phase in (1, 2) for identifier in (1, 2) for field in DJI_SCENARIO_FIELDS)))


def write_dji_scenario_manifest(directory, firmwares):
    from sim.scenario.manifest import load_manifest as load_scenario
    manifest = write_manifests(directory, firmwares, dji_scenario=True)
    document = {"schema": 1, "arena": manifest.path.name, "duration": "1s", "events": [
        {"at": "0ms", "action": "can_delay", "target": "east.drive", "value": "5ms"},
        {"at": "250ms", "action": "can_delay", "target": "east.drive", "value": "0ms"}]}
    path = directory / "scenario.yaml"
    path.write_text(json.dumps(document))
    return load_scenario(path)


def check_dji_scenario_case(row):
    if type(row) is not dict or set(row) != {"status", "name", "actual", "expected"}:
        raise ValueError("DJI Scenario firmware case requires exact fields")
    status, name, actual, expected = (row[key] for key in ("status", "name", "actual", "expected"))
    if any(type(value) is not int or not 0 <= value <= 0xffffffff for value in (actual, expected)):
        raise ValueError("DJI Scenario firmware report requires uint32 fields")
    if status == "SKIP" and name == "plant.physics" and actual == expected == 0:
        return
    if status != "PASS" or name not in DJI_SCENARIO_REQUIRED:
        raise ValueError("failed or unexpected DJI Scenario firmware case: " + name)
    if name == "task.stack":
        valid = actual >= 64 and expected == 64
    elif name == "reset.poweron":
        valid = actual == expected == 0x800000
    elif name.endswith(".angle"):
        valid = actual < 8192 and expected == 8192
    elif name.endswith(".speed"):
        # Sentinel expected0 does not claim zero speed; copied signed int16 is
        # sign-extended through the firmware's uint32 report field.
        valid = expected == 0 and (actual <= 0x7fff or actual >= 0xffff8000)
    elif name.endswith(".ts"):
        valid = actual != 0 and actual != expected
    elif name.endswith((".update_age", ".feedback_age")):
        valid = actual < 50 and expected == 50
    elif name.endswith((".current", ".output")):
        valid = actual == expected == (64 if ".id1." in name else 0xffffffc0)
    elif name.endswith(".temp"):
        valid = actual == expected == 25
    elif name.endswith(".enabled"):
        valid = actual == expected == 1
    else:
        valid = actual == expected == 0
    if not valid:
        raise ValueError("DJI Scenario firmware fields disagree: " + name)


def check_dji_scenario_coverage(rows, summaries):
    for row in rows:
        check_dji_scenario_case(row)
    if (Counter(row["name"] for row in rows if row["status"] == "PASS") !=
            Counter({name: 1 for name in DJI_SCENARIO_REQUIRED})
            or [row["name"] for row in rows if row["status"] == "SKIP"] != ["plant.physics"]
            or summaries != [{"kind": kind, "values": [42, 0, 1]} for kind in ("PHASE", "DONE")]):
        raise ValueError("missing/duplicate DJI Scenario firmware coverage or summaries")
    for identifier in (1, 2):
        stamps = [next(row for row in rows if row["name"] == f"dji.scenario.{phase}.id{identifier}.ts")
                  for phase in (1, 2)]
        if stamps[0]["actual"] == stamps[1]["actual"]:
            raise ValueError("DJI Scenario second timestamp repeats first observation")


def check_dji_scenario_observations(report):
    if any(row["suffix_hex"] for row in report.get("physical_can_suffixes", ())):
        raise ValueError("DJI Scenario physical CAN suffix remains incomplete")
    evidence = report["scenario_observation"]
    start = evidence["shared_start"]
    if (start is None or not start["accepted"] or start["origin_ns"] != 0
            or start["session_id"] != 1 or start["wire_sessions"] != [1, 2]):
        raise ValueError("DJI Scenario shared reset/session observation disagrees")
    actions = evidence["actions"]
    if (len(actions) != 2 or any(not row["accepted"] for row in actions)
            or [(row["logical_ns"], row["member"], row["delay_ns"]) for row in actions] !=
            [(0, "east", 5_000_000), (DJI_SCENARIO_EVENT_NS, "east", 0)]
            or not actions[0]["before_cont"] or actions[1]["before_cont"]):
        raise ValueError("DJI Scenario original CAN delay actions disagree")
    if not actions[1]["pending_sources"]:
        raise ValueError("DJI Scenario restore has no retained delayed producer sources")
    for name in DJI_SCENARIO_MEMBERS:
        samples = evidence["samples"][name]
        stage, control = evidence["stages"].get(name), evidence["controls"].get(name)
        if (set(samples) != {"1", "2"} or samples["1"]["logical_ns"] >= DJI_SCENARIO_EVENT_NS
                or stage is None or stage["logical_ns"] < samples["1"]["logical_ns"]
                or control is None or not control["accepted"] or not control["completed"]
                or control["send_attempts"] != 1 or control["wire_hex"] != "a5"
                or control["logical_ns"] < max(DJI_SCENARIO_EVENT_NS,
                    *(row["logical_ns"] for row in evidence["stages"].values()))
                or samples["2"]["logical_ns"] < control["completed_logical_ns"]):
            raise ValueError("DJI Scenario firmware observation/control order disagrees: " + name)
        peer = DJI_SCENARIO_MEMBERS.index(name)
        for phase in ("1", "2"):
            rows = {row["name"]: row["actual"] for row in report["cases"][name]}
            matches = samples[phase]["source_matches"]
            for identifier in (1, 2):
                prefix = f"dji.scenario.{phase}.id{identifier}."
                values = tuple(rows[prefix + field] for field in ("angle", "speed", "current", "temp"))
                match = matches.get("id" + str(identifier))
                if match is None:
                    raise ValueError("DJI Scenario copied raw feedback has no admitted source")
                encoder, rpm, current, temp, _ = struct.unpack(">HhhBB", bytes.fromhex(match["data_hex"]))
                if values != (encoder, rpm & 0xffffffff, current & 0xffffffff, temp):
                    raise ValueError("DJI Scenario copied raw feedback differs from admitted source")
                if not any(row["peer"] == peer and row["can_id"] == 0x200 + identifier
                           and row["accepted"] and row["data_hex"] == match["data_hex"]
                           and row["world_ns"] == match["source_world_ns"]
                           and row["delivered_ns"] == match["delivery_ns"]
                           and row["qemu_ns"] == match["qemu_ns"] for row in report["feedback"]):
                    raise ValueError("DJI Scenario copied raw source receipt was not admitted")
        for key in ("commands", "command_wire", "command_deliveries"):
            commands = report[key][name]
            if (commands["count"] <= 0 or commands["active_count"] <= 0 or commands["zero_count"] <= 0
                    or commands["count"] > 10000
                    or not 0 < commands["transition_count"] <= commands["count"]
                    or len(commands["transitions"]) != min(commands["transition_count"], 8)):
                raise ValueError("DJI Scenario active/disabled command evidence incomplete")
    east = [row for row in report["feedback"] if row["peer"] == 0 and row["accepted"]]
    west = [row for row in report["feedback"] if row["peer"] == 1 and row["accepted"]]
    if (not east or not west or not any(row["due_ns"] - row["world_ns"] == 5_000_000 for row in east)
            or not any(row["world_ns"] > DJI_SCENARIO_EVENT_NS and row["due_ns"] == row["world_ns"] for row in east)
            or not any(later["world_ns"] < earlier["world_ns"] for earlier, later in zip(east, east[1:]))
            or any(row["due_ns"] != row["world_ns"] for row in west)):
        raise ValueError("DJI Scenario delayed/pending/overtaken/zero delivery evidence incomplete")
    for row in (*east, *west):
        reset = report["responses"][DJI_SCENARIO_MEMBERS[row["peer"]]]["reset"]
        epoch = reset["decoded"]["virtual_time_ns"]
        if (row["qemu_ns"] != epoch + row["delivered_ns"]
                or not row.get("physical_complete") or not row.get("physical_wire_hex")):
            raise ValueError("DJI Scenario feedback epoch/physical wire evidence disagrees")


class _CanWrittenBytes:
    """Match observed written prefixes, retaining only one physical suffix."""

    def __init__(self, observer, peer):
        self.observer, self.peer = observer, peer
        self.suffix = bytearray()
        self.cursor = 0

    def observe(self, data):
        self.suffix.extend(data)
        while len(self.suffix) >= 84:
            raw = bytes(self.suffix[:84])
            del self.suffix[:84]
            rows = self.observer.report["feedback"]
            while self.cursor < len(rows) and rows[self.cursor]["peer"] != self.peer:
                self.cursor += 1
            if self.cursor >= len(rows):
                raise ValueError("DJI Scenario physical CAN write has no original send observation")
            row = rows[self.cursor]
            row.update(physical_wire_hex=raw.hex(), physical_complete=True)
            expected = struct.pack("<IIB3xQ", row["can_id"], 0, 8, row["qemu_ns"])
            if raw[:20] != expected or raw[20:28].hex() != row["data_hex"] or raw[28:] != bytes(56):
                raise ValueError("DJI Scenario physical CAN bytes differ from original source send")
            self.cursor += 1


def _dji_command_summary():
    return {"count": 0, "active_count": 0, "zero_count": 0,
            "first": None, "last": None, "transition_count": 0, "transitions": []}


def _dji_command_invalid_field(row):
    data = row["data_hex"]
    if row["can_id"] not in (0x200, 0x1ff):
        return "can_id"
    if row["flags"] != 0:
        return "flags"
    if row["dlc"] != 8:
        return "dlc"
    if (data not in ("0040ffc000000000", "0000000000000000") or
            row["can_id"] == 0x1ff and data != "0000000000000000"):
        return "data_hex"
    return None


def _record_dji_command(summary, row):
    if _dji_command_invalid_field(row) is not None:
        raise ValueError("DJI Scenario command differs from literal CURRENT profile: " + repr(row))
    data = row["data_hex"]
    if summary["count"] >= 10000:
        raise BufferError("DJI Scenario command observation exhausted")
    summary["count"] += 1
    if row["can_id"] == 0x200:
        summary["active_count" if data == "0040ffc000000000" else "zero_count"] += 1
    if summary["first"] is None:
        summary["first"] = row
    previous = summary["last"]
    if previous is None or (previous["can_id"], previous["data_hex"]) != (row["can_id"], data):
        summary["transition_count"] += 1
        # A delay change can interleave old and new legal command values.
        # Count every transition; retain only the first eight diagnostic rows.
        if len(summary["transitions"]) < 8:
            summary["transitions"].append(row)
    summary["last"] = row


class DjiScenarioObservation(FirmwareObservation):
    """Observe the existing Scenario and release only later guest observations."""

    consumer_label = "DJI Scenario"
    feedback_bound = 10000
    # Storage covers the outer cuts plus one distinct due per admitted command
    # and feedback. Shared deadlines can reduce this count, never increase it.
    step_bound = 1250 + len(DJI_SCENARIO_MEMBERS) * 10000 + feedback_bound

    def __init__(self, report, manifest):
        super().__init__(report, manifest)
        self.scenario = True
        self.controls = {}
        self.buffers = {name: bytearray() for name in DJI_SCENARIO_MEMBERS}
        self.written = []

    def _reject_command(self, name, layer, row, field, error):
        if self.report.get("first_rejected_command") is not None:
            return
        wire = row.get("wire_hex")
        self.report["first_rejected_command"] = {
            "member": name, "layer": layer, "field": field, "row": dict(row),
            "wire_hex": wire, "error_type": type(error).__name__, "error": str(error)}

    def _record_command(self, group, name, layer, row):
        try:
            _record_dji_command(self.report[group][name], row)
        except BaseException as error:
            self._reject_command(name, layer, row,
                                 _dji_command_invalid_field(row) or "observation_capacity", error)
            raise

    def world(self, manifest):
        try:
            plant = super().world(manifest)
            for peer, bus in enumerate(dict.fromkeys(device.bus for device in plant.devices)):
                log = bus.log
                name = self.manifest.robots[peer].name
                def observed_log(delivery, log=log, peer=peer, name=name):
                    log(delivery)
                    if delivery.source != "qemu" or delivery.dropped:
                        return
                    frame = delivery.frame
                    reset = self.report["responses"][name]["reset"]
                    if reset is None:
                        raise ValueError("DJI Scenario command delivery preceded actual RESET epoch")
                    epoch = reset["decoded"]["virtual_time_ns"]
                    self._record_command("command_deliveries", name, "Fabric delivery", {
                        "peer": peer, "can_id": frame.can_id, "flags": frame.flags,
                        "dlc": frame.dlc, "data_hex": frame.data.hex(),
                        "mapped_qemu_ns": epoch + delivery.timestamp_ns,
                        "source_world_ns": delivery.timestamp_ns, "due_ns": delivery.due_ns,
                        "delivered_ns": delivery.delivered_ns, "sequence": delivery.sequence,
                        "destination": delivery.destination})
                bus.log = observed_log
            return plant
        except BaseException as error:
            if self.plant is not None:
                close_observation_failure(self.plant, "world", error)
            raise

    def can(self, sock):
        stream = super().can(sock)
        peer = self.can_count - 1
        name = self.manifest.robots[peer].name
        records = stream.receive_records
        def observed_records():
            for raw in records():
                # Retain original bytes before the original receive consumes them.
                # The production codec still owns decoding and validation.
                if len(raw) != 84:
                    error = ValueError("DJI Scenario command wire is not84 bytes")
                    self._reject_command(name, "CAN records before codec",
                        {"peer": peer, "wire_hex": raw.hex()}, "wire_length", error)
                    raise error
                row = {"peer": peer, "can_id": int.from_bytes(raw[:4], "little"),
                    "flags": int.from_bytes(raw[4:8], "little"), "dlc": raw[8],
                    "qemu_ns": int.from_bytes(raw[12:20], "little"),
                    "data_hex": raw[20:28].hex(), "wire_hex": raw.hex()}
                if raw[9:12] != bytes(3) or raw[28:] != bytes(56):
                    error = ValueError("DJI Scenario command wire reserved bytes disagree")
                    self._reject_command(name, "CAN records before codec", row,
                        "header_reserved" if raw[9:12] != bytes(3) else "payload_padding", error)
                    raise error
                self._record_command("command_wire", name, "CAN records before codec", row)
                yield raw
        stream.receive_records = observed_records
        written = _CanWrittenBytes(self, peer)
        self.written.append(written)
        flush = stream.flush

        def observe_progress(before):
            after = b"".join(bytes(view) for view in stream.tx_queue)
            if len(after) > len(before) or not before.endswith(after):
                raise ValueError("DJI Scenario CAN flush did not retain the original suffix")
            written.observe(before[:len(before) - len(after)])

        def observed_flush():
            before = b"".join(bytes(view) for view in stream.tx_queue)
            try:
                result = flush()
            except BaseException as error:
                try:
                    observe_progress(before)
                except BaseException as observation:
                    error.add_note("DJI Scenario CAN flush observation failed: " +
                                   f"{type(observation).__name__}: {observation}")
                    if observation is not error:
                        for note in tuple(getattr(observation, "__notes__", ())):
                            error.add_note(note)
                raise
            observe_progress(before)
            return result

        stream.flush = observed_flush
        return stream

    def command(self, index, can_id, flags, dlc, stamp, data):
        name = self.manifest.robots[index].name
        self._record_command("commands", name, "CAN decoded ingress", {"peer": index,
            "can_id": can_id, "flags": flags, "dlc": dlc, "qemu_ns": stamp, "data_hex": data.hex()})

    def shared(self, *args, **options):
        runtime = super().shared(*args, **options)
        try:
            start, setter = runtime.start, runtime.set_can_delay
            evidence = self.report["scenario_observation"]
            def observed_start(session, wire_sessions, *, deadline):
                if evidence["shared_start"] is not None:
                    raise ValueError("duplicate DJI Scenario shared start")
                row = evidence["shared_start"] = {"session_id": session.session_id,
                    "origin_ns": session.t_sim_ns, "wire_sessions": list(wire_sessions), "accepted": False}
                result = start(session, wire_sessions, deadline=deadline)
                row["accepted"] = True
                return result
            def observed_setter(name, delay_ns):
                peer = [item.name for item in self.manifest.robots].index(name)
                row = {"member": name, "delay_ns": delay_ns, "logical_ns": runtime.t_sim_ns,
                    "before_cont": not any(item["command"] in ("cont", "x-run-to-virtual-time")
                                           for item in self.report["qmp_events"]),
                    "accepted": False, "pending_sources": [dict(device=index, **source)
                        for index in range(peer * 4, peer * 4 + 4)
                        for source in self.feedback_sources[index].values()],
                    "bus_pending": runtime.bindings[peer].endpoint.bus.pending}
                append_bounded(evidence, "actions", row, 2)
                result = setter(name, delay_ns)
                row["accepted"] = True
                self.service_control()
                return result
            runtime.start, runtime.set_can_delay = observed_start, observed_setter
            return runtime
        except BaseException as error:
            close_observation_failure(runtime, "shared world", error)
            raise

    def check_cont(self):
        evidence = self.report["scenario_observation"]
        if (evidence["shared_start"] is None or not evidence["shared_start"]["accepted"]
                or len(evidence["actions"]) != 1 or not evidence["actions"][0]["accepted"]
                or not evidence["actions"][0]["before_cont"]):
            raise ValueError("DJI Scenario resume preceded original reset/offset0 delay")

    def service_control(self):
        evidence = self.report["scenario_observation"]
        if (len(evidence["actions"]) != 2 or not evidence["actions"][-1]["accepted"]
                or set(evidence["stages"]) != set(DJI_SCENARIO_MEMBERS)
                or any("1" not in samples for samples in evidence["samples"].values())):
            return
        for name in DJI_SCENARIO_MEMBERS:
            stream = self.controls[name]
            control = evidence["controls"].get(name)
            if control is None:
                if stream.tx_pending:
                    raise ValueError("DJI Scenario borrowed control UART has prior pending output")
                control = evidence["controls"][name] = {"logical_ns": self.observation_cut_ns(),
                    "wire_hex": "a5", "send_attempts": 1, "accepted": False,
                    "flush_attempts": 1, "completed": False, "borrowed_stream_identity": id(stream)}
                stream.send(b"\xa5")
                control["accepted"] = True
            elif stream.tx_pending:
                control["flush_attempts"] += 1
                stream.flush()
            if not stream.tx_pending and not control["completed"]:
                control.update(completed=True, completed_logical_ns=self.observation_cut_ns())

    def scenario_case(self, name, row, poll_sample_qemu_ns):
        if not row["name"].startswith("dji.scenario.") or row["name"] == "dji.scenario.stop":
            return
        phase = row["name"].split(".")[2]
        evidence, now = self.report["scenario_observation"], self.runtime.t_sim_ns
        if name not in self.report["ready"]:
            raise ValueError("DJI Scenario sample report preceded actual UART ready")
        if phase == "1" and now >= DJI_SCENARIO_EVENT_NS:
            raise ValueError("DJI Scenario first firmware report missed250ms boundary")
        if phase == "2":
            control = evidence["controls"].get(name)
            if control is None or not control["completed"]:
                raise ValueError("DJI Scenario second firmware report preceded observation control")
        prefix = f"dji.scenario.{phase}."
        required = {label for label in DJI_SCENARIO_REQUIRED if label.startswith(prefix)}
        rows = [item for item in self.report["cases"][name] if item["name"].startswith(prefix)]
        if len(rows) != len({item["name"] for item in rows}):
            raise ValueError("duplicate DJI Scenario sample report")
        if {item["name"] for item in rows} == required:
            values = {item["name"]: item["actual"] for item in rows}
            sample = {"logical_ns": now, "poll_sample_qemu_ns": poll_sample_qemu_ns, "source_matches": {}}
            evidence["samples"][name][phase] = sample
            peer = [item.name for item in self.manifest.robots].index(name)
            for identifier in (1, 2):
                observed = tuple(values[prefix + f"id{identifier}." + field]
                    for field in ("angle", "speed", "current", "temp"))
                for source in reversed(self.report["feedback"]):
                    if (source["peer"] != peer or source["can_id"] != 0x200 + identifier
                            or not source["accepted"] or not source.get("physical_complete")
                            or source["delivered_ns"] > now):
                        continue
                    angle, speed, current, temp, _ = struct.unpack(">HhhBB", bytes.fromhex(source["data_hex"]))
                    if observed == (angle, speed & 0xffffffff, current & 0xffffffff, temp):
                        sample["source_matches"][f"id{identifier}"] = {"data_hex": source["data_hex"],
                            "source_world_ns": source["world_ns"], "delivery_ns": source["delivered_ns"],
                            "qemu_ns": source["qemu_ns"]}
                        break
                else:
                    raise ValueError("DJI Scenario actual copied raw has no admitted same-peer feedback")

    def emit(self, event):
        if event.event.endpoint != "uart1":
            raise ValueError("unexpected DJI Scenario guest UART output")
        index = [item.name for item in self.manifest.robots].index(event.name)
        text = event.event.data.decode("ascii")
        if len(self.report["uart"][index]) + len(text) > 12000:
            raise BufferError("DJI Scenario UART observation exhausted")
        self.report["uart"][index] += text
        buffer = self.buffers[event.name]
        if len(buffer) + len(event.event.data) > 12000:
            raise BufferError("DJI Scenario UART line buffer exhausted")
        buffer.extend(event.event.data)
        while b"\n" in buffer:
            raw, _, suffix = buffer.partition(b"\n")
            buffer[:] = suffix
            line = raw.decode("ascii").strip()
            if not line.startswith("TTEST "):
                continue
            fields = line.split()
            if len(fields) != 5:
                raise ValueError("invalid DJI Scenario firmware report")
            if fields[1] in ("PHASE", "DONE"):
                append_bounded(self.report["summaries"], event.name,
                    {"kind": fields[1], "values": list(map(int, fields[2:]))}, 2)
                if fields[1] == "DONE":
                    check_dji_scenario_coverage(self.report["cases"][event.name], self.report["summaries"][event.name])
                    self.watchdog(index, event.name, "done")
                continue
            row = {"status": fields[1], "name": fields[2], "actual": int(fields[3]), "expected": int(fields[4])}
            if row["status"] == "READY":
                if row["actual"] != 0 or row["expected"] != 0:
                    raise ValueError("DJI Scenario ready/stage values disagree")
                marker = {"logical_ns": self.runtime.t_sim_ns, "poll_sample_qemu_ns": event.event.timestamp_ns}
                if row["name"] == "dji.scenario.ready" and event.name not in self.report["ready"]:
                    self.report["ready"][event.name] = marker
                elif (row["name"] == "dji.scenario.observe" and event.name not in self.report["scenario_observation"]["stages"]
                        and "1" in self.report["scenario_observation"]["samples"][event.name]):
                    self.report["scenario_observation"]["stages"][event.name] = marker
                else:
                    raise ValueError("unexpected DJI Scenario ready/stage marker")
            else:
                append_bounded(self.report["cases"], event.name, row, 43)
                check_dji_scenario_case(row)
                self.scenario_case(event.name, row, event.event.timestamp_ns)
        self.service_control()


def run_dji_scenario(elf, output, firmware, *, qemu=None):
    """One real Scenario wrapper/Arena call; publish success after final receipts."""
    from tools.dm_mc02_dji_test import source_snapshot as dji_sources
    from tools.dm_mc02_referee_test import source_snapshot, checkout_snapshot, write_json
    from sim.scenario import runtime as scenario_runtime
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("refusing to overwrite existing DJI Scenario result directory")
    output.mkdir(parents=True)
    report = {"result": "failed", "arena": True, "scenario": True, "errors": [], "notes": [],
        "cleanup": [], "qmp_events": [], "qmp_cleanup": [], "uart": ["", ""],
        "cases": {name: [] for name in DJI_SCENARIO_MEMBERS},
        "summaries": {name: [] for name in DJI_SCENARIO_MEMBERS}, "ready": {}, "watchdogs": {},
        "responses": {name: {"count": 0, "first": None, "last": None, "reset": None,
            "step_ack_count": 0, "first_step_ack": None, "last_step_ack": None,
            "done_count": 0, "last_done": None} for name in DJI_SCENARIO_MEMBERS},
        "native_calls": {"reset": 0, "step_ns": [], "close": 0}, "feedback": [],
        "commands": {name: _dji_command_summary() for name in DJI_SCENARIO_MEMBERS},
        "command_wire": {name: _dji_command_summary() for name in DJI_SCENARIO_MEMBERS},
        "command_deliveries": {name: _dji_command_summary() for name in DJI_SCENARIO_MEMBERS},
        "can_receive_errors": [], "scenario_observation": {"shared_start": None, "actions": [],
            "samples": {name: {} for name in DJI_SCENARIO_MEMBERS}, "stages": {}, "controls": {}},
        "missing": {name: sorted(DJI_SCENARIO_REQUIRED) for name in DJI_SCENARIO_MEMBERS},
        "skips": {name: [] for name in DJI_SCENARIO_MEMBERS},
        "missing_skips": {name: ["plant.physics"] for name in DJI_SCENARIO_MEMBERS},
        "observation_bounds": {"feedback": DjiScenarioObservation.feedback_bound,
            "steps": DjiScenarioObservation.step_bound, "pending_per_device": 256,
            "commands_per_peer": 10000, "sampled_command_transitions_per_peer": 8},
        "limitations": ["Actual motor::dji/BSP/HAL/FDCAN consumer of one eight-device native world.",
            "Every command is checked and counted; transition_count is exact, transitions retains only the first8 changes and last is the actual last row.",
            "Both first complete reports must precede the reset-relative250ms event; no ready reanchor.",
            "UART1 a5 releases application observation only, without world events or CPU synchronization.",
            "Copied raw tuple has a completed admitted same-peer witness; repeated raw bytes do not identify a unique firmware source or exact host pairing.",
            "Host logical/source/due/delivery/wire epochs and BSP milliseconds are distinct.",
            "No50ms dynamic-delay timeout, calibrated physicalCAN, CPU barrier, pacing or RTF claim.",
            "Firmware plant.physics remains an explicit SKIP."]}
    write_json(output / "report.json", report)
    observer = primary = binary = manifest = scenario_manifest = None
    previous_signal = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"DJI Scenario interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    def inputs():
        result = source_snapshot(elf, binary, firmware)
        result["dji_consumer"] = dji_sources(elf, binary, firmware)
        motor = firmware / "components/motor"
        motor_state = {}
        for key, command in (("head", ["rev-parse", "--verify", "HEAD"]),
                             ("status", ["status", "--short"])):
            text = subprocess.check_output(["git", *command], cwd=motor, text=True, timeout=2)
            if len(text.encode()) > 12000:
                raise BufferError("DJI Scenario motor checkout observation exhausted")
            motor_state[key] = text.strip() if key == "head" else text
        result["actual_motor_checkout"] = motor_state
        paths = [ROOT / name for name in ("models/two_four_wheel.xml", "models/four_wheel.xml",
            "models/mujoco-LICENSE.txt", "flake.lock", "tools/dm_mc02_arena_smoke.py",
            "tools/dm_mc02_shared_world_smoke.py")]
        paths += [firmware / name for name in ("Core/Src/fdcan.c",
            "Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_fdcan.c",
            "Middlewares/Third_Party/FreeRTOS/Source/tasks.c",
            "Middlewares/Third_Party/FreeRTOS/Source/portable/GCC/ARM_CM4F/port.c")]
        if manifest is not None:
            paths += [manifest.path, *(item.robot.path for item in manifest.robots)]
        if scenario_manifest is not None:
            paths.append(scenario_manifest.path)
        result.update({str(path): sha(path) for path in paths})
        return result
    try:
        elf = arena.regular_file(elf, "DjiScenarioTest ELF")
        binary = arena.resolve_qemu(qemu)
        firmware = Path(firmware).resolve()
        if not firmware.is_dir():
            raise ValueError("firmware source tree is not a directory")
        directory = output / "arena"
        directory.mkdir()
        scenario_manifest = write_dji_scenario_manifest(directory, (elf, elf))
        manifest = scenario_manifest.arena
        report["scenario_input"] = {"path": str(scenario_manifest.path),
            "duration_ns": scenario_manifest.duration_ns,
            "can_delays": [asdict(item) for item in scenario_manifest.can_delays]}
        report["inputs_before"], report["checkout_before"] = inputs(), checkout_snapshot(firmware)
        report["actual_sources_before"] = source_origins()
        observer = DjiScenarioObservation(report, manifest)
        overrides = {"create_arena_plant": "world", "running_qemu": "qemu", "QmpSession": "qmp",
            "connect_unix": "connect", "SharedWorldRuntime": "shared", "LabChannels": "channels",
            "CanStream": "can"}
        for original, replacement in overrides.items():
            setattr(observer, "original_" + replacement, getattr(arena, original))
        with patch.multiple(arena, **{name: getattr(observer, method) for name, method in overrides.items()}):
            result = scenario_runtime.run_manifest(scenario_manifest, qemu=binary,
                startup_timeout=10., timeout=20., emit=observer.emit, on_response=observer.response)
        report["runtime_result"] = asdict(result)
        calls = report["native_calls"]
        if (result.end_ns - result.origin_ns != 1_000_000_000 or result.origin_ns >= DJI_SCENARIO_EVENT_NS
                or calls["reset"] != 1 or calls["close"] != 1 or len(calls["step_ns"]) != result.steps
                or sum(calls["step_ns"]) != result.end_ns
                or any(not 0 < dt <= 1_000_000 for dt in calls["step_ns"])
                or len(result.boards) != 2 or result.referees or result.children):
            raise ValueError("DJI Scenario world/result scope disagrees")
        for wire_session, board in enumerate(result.boards, 1):
            if board.ready != "uart_marker" or board.ready_qemu_ns is None:
                raise ValueError("DJI Scenario actual UART ready observation missing")
            report["ready"][board.name]["post_read_qemu_ns"] = board.ready_qemu_ns
            check_referee_responses(report["responses"][board.name], wire_session,
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
                    primary.add_note(f"DJI Scenario post-cleanup verification failed: {type(error).__name__}: {error}")
                    for note in tuple(getattr(error, "__notes__", ())):
                        primary.add_note(note)
        for name in DJI_SCENARIO_MEMBERS:
            rows = report["cases"][name]
            report["missing"][name] = sorted(DJI_SCENARIO_REQUIRED -
                {row["name"] for row in rows if row["status"] == "PASS"})
            report["skips"][name] = [row["name"] for row in rows if row["status"] == "SKIP"]
            report["missing_skips"][name] = [] if "plant.physics" in report["skips"][name] else ["plant.physics"]
            verify(lambda name=name: check_dji_scenario_coverage(report["cases"][name], report["summaries"][name]))
        report["physical_can_suffixes"] = [] if observer is None else [
            {"peer": item.peer, "suffix_hex": item.suffix.hex()} for item in observer.written]
        verify(lambda: check_dji_scenario_observations(report))
        verify(lambda: verify_firmware_cleanup(report, observer, label="DJI Scenario", controls=True))
        verify(lambda: verify_firmware_sources(report, inputs, firmware, observer=observer, label="DJI Scenario"))
        verify(lambda: verify_firmware_watchdogs(report, label="DJI Scenario"))
        signal.signal(signal.SIGTERM, previous_signal)
        report["result"] = "passed" if primary is None else "failed"
        if primary is not None:
            report["errors"].insert(0, f"{type(primary).__name__}: {primary}")
            report["error_type"], report["notes"] = type(primary).__name__, list(getattr(primary, "__notes__", ()))
        write_json(output / "report.json", report)
    print("RESULT: dual-QEMU DJI Scenario consumer " + report["result"])
    if primary is not None:
        print(f"DJI Scenario consumer failed: {primary}", file=sys.stderr)
        for note in report["notes"]:
            print(note, file=sys.stderr)
    return 0 if primary is None else 130 if isinstance(primary, KeyboardInterrupt) else 1


DJI_FEEDBACK_LOSS_NS = 200_000_000
DJI_FEEDBACK_RECOVERY_NS = 350_000_000
DJI_FEEDBACK_REQUIRED = frozenset(("control.rx", "reset.poweron", "bsp.init", "task.stack",
    "dji.feedback.baseline.id1", "dji.feedback.baseline.id2", "dji.feedback.loss.frozen",
    "dji.feedback.loss.age", "dji.feedback.loss.app_age", "dji.feedback.loss.output",
    "dji.feedback.loss.id2", "dji.feedback.recover.id1", "dji.feedback.recover.id2",
    "dji.feedback.stop", "dji.feedback.can"))


def write_dji_feedback_manifest(directory, firmwares):
    from sim.scenario.manifest import load_manifest as load_scenario
    manifest = write_manifests(directory, firmwares, dji_scenario=True)
    for member in manifest.robots:
        path = member.robot.path
        document = json.loads(path.read_text())
        document["ready"]["text"] = "TTEST READY dji.feedback.ready 0 0\n"
        path.write_text(json.dumps(document))
    document = {"schema": 1, "arena": manifest.path.name, "duration": "400ms", "events": [
        {"at": at, "action": "motor_feedback", "target": name + ".motor_fl", "value": enabled}
        for at, enabled in (("200ms", False), ("350ms", True))
        for name in DJI_SCENARIO_MEMBERS]}
    path = directory / "scenario.yaml"
    path.write_text(json.dumps(document))
    return load_scenario(path)


def check_dji_feedback_case(row):
    if type(row) is not dict or set(row) != {"status", "name", "actual", "expected"}:
        raise ValueError("DJI feedback firmware case requires exact fields")
    status, name, actual, expected = (row[key] for key in ("status", "name", "actual", "expected"))
    if any(type(value) is not int or not 0 <= value <= 0xffffffff for value in (actual, expected)):
        raise ValueError("DJI feedback firmware report requires uint32 fields")
    if status == "SKIP" and name == "plant.physics" and actual == expected == 0:
        return
    if status != "PASS" or name not in DJI_FEEDBACK_REQUIRED:
        raise ValueError("failed or unexpected DJI feedback firmware case: " + name)
    if name == "task.stack":
        valid = actual >= 64 and expected == 64
    elif name == "reset.poweron":
        valid = actual == expected == 0x800000
    elif name == "dji.feedback.loss.age":
        valid = actual > 50 and expected == 51
    elif name == "dji.feedback.loss.app_age":
        valid = actual < 50 and expected == 50
    elif name == "dji.feedback.loss.frozen":
        valid = actual == expected != 0
    elif name in ("dji.feedback.loss.id2", "dji.feedback.recover.id1"):
        valid = actual != 0 and expected != 0 and actual != expected
    elif name.startswith("dji.feedback.baseline.") or name == "dji.feedback.recover.id2":
        valid = actual != 0 and expected == 0
    else:
        valid = actual == expected == 0
    if not valid:
        raise ValueError("DJI feedback firmware fields disagree: " + name)


def check_dji_feedback_coverage(rows, summaries):
    for row in rows:
        check_dji_feedback_case(row)
    if (Counter(row["name"] for row in rows if row["status"] == "PASS") !=
            Counter({name: 1 for name in DJI_FEEDBACK_REQUIRED})
            or [row["name"] for row in rows if row["status"] == "SKIP"] != ["plant.physics"]
            or summaries != [{"kind": kind, "values": [15, 0, 1]} for kind in ("PHASE", "DONE")]):
        raise ValueError("missing/duplicate DJI feedback firmware coverage or summaries")
    values = {row["name"]: row for row in rows}
    baseline = values["dji.feedback.baseline.id1"]["actual"]
    frozen = values["dji.feedback.loss.frozen"]["actual"]
    if (frozen == baseline or values["dji.feedback.recover.id1"]["expected"] != frozen
            or values["dji.feedback.recover.id2"]["actual"] ==
               values["dji.feedback.baseline.id2"]["actual"]):
        raise ValueError("DJI feedback timestamp history disagrees")


class DjiFeedbackObservation(DjiScenarioObservation):
    """Observe the sole Scenario schedule and the real firmware timeout response."""

    consumer_label = "DJI feedback Scenario"

    def _record_command(self, group, name, layer, row):
        data = row["data_hex"]
        if ((row["can_id"], row["flags"], row["dlc"]) != (0x200, 0, 8)
                or data not in ("0040ffc000000000", "0000ffc000000000", "0000000000000000")):
            error = ValueError("DJI feedback command differs from literal CURRENT profile: " + repr(row))
            self._reject_command(name, layer, row, "literal_profile", error)
            raise error
        summary = self.report[group][name]
        if summary["count"] >= 10000:
            raise BufferError("DJI feedback command observation exhausted")
        summary["count"] += 1
        counter = {"0040ffc000000000": "active_count", "0000ffc000000000": "loss_count",
                   "0000000000000000": "zero_count"}[data]
        summary[counter] += 1
        if summary["first"] is None:
            summary["first"] = row
        if summary["last"] is None or summary["last"]["data_hex"] != data:
            summary["transition_count"] += 1
            if len(summary["transitions"]) < 8:
                summary["transitions"].append(row)
        summary["last"] = row
        reset = self.report["responses"][name]["reset"]
        if reset is None:
            raise ValueError("DJI feedback command preceded actual RESET epoch")
        stamp = row.get("qemu_ns", row.get("mapped_qemu_ns")) - reset["decoded"]["virtual_time_ns"]
        witnesses = summary["witnesses"]
        if data == "0040ffc000000000" and stamp < DJI_FEEDBACK_LOSS_NS:
            witnesses.setdefault("baseline", row)
        elif data == "0000ffc000000000" and 251_000_000 < stamp < DJI_FEEDBACK_RECOVERY_NS:
            witnesses.setdefault("late_loss", row)
        elif data == "0040ffc000000000" and stamp > DJI_FEEDBACK_RECOVERY_NS:
            witnesses.setdefault("recovery", row)
        elif data == "0000000000000000" and "recovery" in witnesses:
            witnesses.setdefault("stop", row)

    def world(self, manifest):
        plant = super().world(manifest)
        try:
            for peer, bus in enumerate(dict.fromkeys(device.bus for device in plant.devices)):
                log = bus.log
                def observed_dispatch(delivery, peer=peer, log=log):
                    log(delivery)
                    if delivery.destination != "qemu" or not 0x201 <= delivery.frame.can_id <= 0x204:
                        return
                    index = peer * 4 + delivery.frame.can_id - 0x201
                    source = self.feedback_sources[index].get(delivery.timestamp_ns)
                    if source is None:
                        raise ValueError("DJI feedback dispatch has no completed producer source")
                    row = {"peer": peer, "motor_id": delivery.frame.can_id - 0x200,
                        "source": delivery.source, "destination": delivery.destination,
                        "world_ns": delivery.timestamp_ns, "due_ns": delivery.due_ns,
                        "delivered_ns": delivery.delivered_ns, "sequence": delivery.sequence,
                        "data_hex": delivery.frame.data.hex(), "dropped": delivery.dropped}
                    if (plant.devices[index].motor_id != row["motor_id"]
                            or row["source"] != "m3508-" + str(row["motor_id"])
                            or row["data_hex"] != source["data_hex"]):
                        raise ValueError("DJI feedback dispatch differs from actual device source")
                    append_bounded(self.report, "feedback_dispatches", row, self.feedback_bound)
                    if delivery.dropped:
                        del self.feedback_sources[index][delivery.timestamp_ns]
                bus.log = observed_dispatch
            return plant
        except BaseException as error:
            close_observation_failure(plant, "world", error)
            raise

    def shared(self, *args, **options):
        runtime = FirmwareObservation.shared(self, *args, **options)
        try:
            start, setter = runtime.start, runtime.set_motor_feedback_enabled
            evidence = self.report["feedback_observation"]
            def observed_start(session, wire_sessions, *, deadline):
                if evidence["shared_start"] is not None:
                    raise ValueError("duplicate DJI feedback shared start")
                row = evidence["shared_start"] = {"session_id": session.session_id,
                    "origin_ns": session.t_sim_ns, "wire_sessions": list(wire_sessions), "accepted": False}
                result = start(session, wire_sessions, deadline=deadline)
                row["accepted"] = True
                return result
            def observed_setter(name, motor_id, enabled):
                row = {"logical_ns": runtime.t_sim_ns, "member": name, "motor_id": motor_id,
                       "enabled": enabled, "accepted": False}
                append_bounded(evidence, "actions", row, 4)
                result = setter(name, motor_id, enabled)
                row["accepted"] = True
                return result
            runtime.start, runtime.set_motor_feedback_enabled = observed_start, observed_setter
            return runtime
        except BaseException as error:
            close_observation_failure(runtime, "shared world", error)
            raise

    def check_cont(self):
        start = self.report["feedback_observation"]["shared_start"]
        if start is None or not start["accepted"]:
            raise ValueError("DJI feedback execution preceded actual shared reset")

    def service_control(self):
        pass

    def emit(self, event):
        if event.event.endpoint != "uart1":
            raise ValueError("unexpected DJI feedback guest UART output")
        index = [member.name for member in self.manifest.robots].index(event.name)
        text = event.event.data.decode("ascii")
        if len(self.report["uart"][index]) + len(text) > 12000:
            raise BufferError("DJI feedback UART observation exhausted")
        self.report["uart"][index] += text
        buffer = self.buffers[event.name]
        if len(buffer) + len(event.event.data) > 12000:
            raise BufferError("DJI feedback UART line buffer exhausted")
        buffer.extend(event.event.data)
        while b"\n" in buffer:
            raw, _, suffix = buffer.partition(b"\n")
            buffer[:] = suffix
            fields = raw.decode("ascii").strip().split()
            if not fields or fields[0] != "TTEST":
                continue
            if len(fields) != 5:
                raise ValueError("invalid DJI feedback firmware report")
            if fields[1] in ("PHASE", "DONE"):
                append_bounded(self.report["summaries"], event.name,
                    {"kind": fields[1], "values": list(map(int, fields[2:]))}, 2)
                if fields[1] == "DONE":
                    check_dji_feedback_coverage(self.report["cases"][event.name], self.report["summaries"][event.name])
                    self.watchdog(index, event.name, "done")
                continue
            row = {"status": fields[1], "name": fields[2], "actual": int(fields[3]), "expected": int(fields[4])}
            marker = {"logical_ns": self.runtime.t_sim_ns, "poll_sample_qemu_ns": event.event.timestamp_ns}
            if row["status"] == "READY":
                if (row["name"] != "dji.feedback.ready" or row["actual"] != 0 or row["expected"] != 0
                        or event.name in self.report["ready"]):
                    raise ValueError("unexpected DJI feedback ready marker")
                self.report["ready"][event.name] = marker
            else:
                append_bounded(self.report["cases"], event.name, row, 16)
                check_dji_feedback_case(row)
                if row["name"].startswith("dji.feedback."):
                    if event.name not in self.report["ready"]:
                        raise ValueError("DJI feedback sample preceded actual UART ready")
                    self.report["feedback_observation"]["cases"][event.name][row["name"]] = marker


def check_dji_feedback_observations(report):
    evidence = report["feedback_observation"]
    if evidence["shared_start"] != {"session_id": 1, "origin_ns": 0,
                                     "wire_sessions": [1, 2], "accepted": True}:
        raise ValueError("DJI feedback shared reset observation disagrees")
    expected = [(stamp, name, 1, enabled) for stamp, enabled in
                ((DJI_FEEDBACK_LOSS_NS, False), (DJI_FEEDBACK_RECOVERY_NS, True))
                for name in DJI_SCENARIO_MEMBERS]
    actions = evidence["actions"]
    if ([(row["logical_ns"], row["member"], row["motor_id"], row["enabled"]) for row in actions] != expected
            or any(not row["accepted"] for row in actions)):
        raise ValueError("DJI feedback original reset-relative actions disagree")
    if any(row["suffix_hex"] for row in report["physical_can_suffixes"]):
        raise ValueError("DJI feedback physical CAN suffix remains incomplete")
    for peer, name in enumerate(DJI_SCENARIO_MEMBERS):
        cases = evidence["cases"][name]
        baseline = [cases["dji.feedback.baseline.id" + str(i)]["logical_ns"] for i in (1, 2)]
        loss = [value["logical_ns"] for label, value in cases.items() if label.startswith("dji.feedback.loss.")]
        recovery = [cases["dji.feedback.recover.id" + str(i)]["logical_ns"] for i in (1, 2)]
        if (max(baseline) >= DJI_FEEDBACK_LOSS_NS or min(loss) <= 251_000_000
                or max(loss) >= DJI_FEEDBACK_RECOVERY_NS or min(recovery) <= DJI_FEEDBACK_RECOVERY_NS
                or cases["dji.feedback.stop"]["logical_ns"] < max(recovery)):
            raise ValueError("DJI feedback firmware baseline/loss/recovery order disagrees")
        for group in ("commands", "command_wire", "command_deliveries"):
            summary = report[group][name]
            if (set(summary["witnesses"]) != {"baseline", "late_loss", "recovery", "stop"}
                    or any(summary[key] <= 0 for key in ("active_count", "loss_count", "zero_count"))
                    or not 0 < summary["transition_count"] <= summary["count"] <= 10000
                    or len(summary["transitions"]) != min(summary["transition_count"], 8)):
                raise ValueError("DJI feedback actual command witnesses incomplete: " + group)
            epoch = report["responses"][name]["reset"]["decoded"]["virtual_time_ns"]
            witnesses = summary["witnesses"]
            stamps = {}
            for phase, data in (("baseline", "0040ffc000000000"), ("late_loss", "0000ffc000000000"),
                                ("recovery", "0040ffc000000000"), ("stop", "0000000000000000")):
                row = witnesses[phase]
                if (row["peer"], row["can_id"], row["flags"], row["dlc"], row["data_hex"]) != (peer, 0x200, 0, 8, data):
                    raise ValueError("DJI feedback command witness differs from literal: " + phase)
                stamps[phase] = row.get("qemu_ns", row.get("mapped_qemu_ns")) - epoch
            if not (0 <= stamps["baseline"] < DJI_FEEDBACK_LOSS_NS
                    and 251_000_000 < stamps["late_loss"] < DJI_FEEDBACK_RECOVERY_NS
                    < stamps["recovery"] <= stamps["stop"]):
                raise ValueError("DJI feedback command witness time disagrees")
        dispatches = [row for row in report["feedback_dispatches"] if row["peer"] == peer]
        for row in dispatches:
            denied = row["motor_id"] == 1 and DJI_FEEDBACK_LOSS_NS < row["delivered_ns"] <= DJI_FEEDBACK_RECOVERY_NS
            if row["dropped"] != denied or row["due_ns"] != row["world_ns"]:
                raise ValueError("DJI feedback dispatch scope/original due disagrees")
        for identifier in (1, 2, 3, 4):
            if not any(row["motor_id"] == identifier and DJI_FEEDBACK_LOSS_NS < row["delivered_ns"] <
                       DJI_FEEDBACK_RECOVERY_NS for row in dispatches):
                raise ValueError("DJI feedback loss interval lacks motor dispatch evidence")
        accepted = [row for row in report["feedback"] if row["peer"] == peer and row["accepted"]]
        if not any(row["can_id"] == 0x201 and row["delivered_ns"] > DJI_FEEDBACK_RECOVERY_NS for row in accepted):
            raise ValueError("DJI feedback fresh recovery has no admitted source")
        epoch = report["responses"][name]["reset"]["decoded"]["virtual_time_ns"]
        for row in accepted:
            if (row["qemu_ns"] != epoch + row["delivered_ns"] or not row.get("physical_complete")
                    or not row.get("physical_wire_hex")):
                raise ValueError("DJI feedback epoch/physical receipt incomplete")


def run_dji_feedback_scenario(elf, output, firmware, *, qemu=None):
    """Use the production Scenario once; retain every failed consumer/cleanup gate."""
    from tools.dm_mc02_dji_test import source_snapshot as dji_sources
    from tools.dm_mc02_referee_test import source_snapshot, checkout_snapshot, write_json
    from sim.scenario import runtime as scenario_runtime
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("refusing to overwrite existing DJI feedback result directory")
    output.mkdir(parents=True)
    report = {"result": "failed", "arena": True, "scenario": True, "feedback_scenario": True,
        "errors": [], "notes": [], "cleanup": [], "qmp_events": [], "qmp_cleanup": [], "uart": ["", ""],
        "cases": {name: [] for name in DJI_SCENARIO_MEMBERS},
        "summaries": {name: [] for name in DJI_SCENARIO_MEMBERS}, "ready": {}, "watchdogs": {},
        "responses": {name: {"count": 0, "first": None, "last": None, "reset": None,
            "step_ack_count": 0, "first_step_ack": None, "last_step_ack": None,
            "done_count": 0, "last_done": None} for name in DJI_SCENARIO_MEMBERS},
        "native_calls": {"reset": 0, "step_ns": [], "close": 0}, "feedback": [], "feedback_dispatches": [],
        **{group: {name: dict(_dji_command_summary(), loss_count=0, witnesses={})
                   for name in DJI_SCENARIO_MEMBERS}
           for group in ("commands", "command_wire", "command_deliveries")},
        "can_receive_errors": [], "feedback_observation": {"shared_start": None, "actions": [],
            "cases": {name: {} for name in DJI_SCENARIO_MEMBERS}},
        "missing": {name: sorted(DJI_FEEDBACK_REQUIRED) for name in DJI_SCENARIO_MEMBERS},
        "skips": {name: [] for name in DJI_SCENARIO_MEMBERS},
        "missing_skips": {name: ["plant.physics"] for name in DJI_SCENARIO_MEMBERS},
        "observation_bounds": {"feedback": DjiFeedbackObservation.feedback_bound,
            "steps": DjiFeedbackObservation.step_bound, "pending_per_device": 256,
            "commands_per_peer": 10000, "sampled_command_transitions_per_peer": 8},
        "limitations": ["Fixed two-board IDs1/2 CURRENT64/-64; both ID1 feedback paths denied200..350ms; ID2 is the live control.",
            "Actual commands are checked/counted; first8 transitions and four witnesses are bounded observations, not a full trace.",
            "BSP sample time, source/due/dispatch, QEMU wire and UART poll times are separate observations; repeated bytes cannot identify a unique callback.",
            "Feedback unreachability is not motor poweroff; no physical calibration, silicon timing, CPU/global barrier, RTF, replay or mixed fidelity claim.",
            "Original fixed DJI Scenario Release20s failure remains unresolved; this separately named gate does not clear it.",
            "Firmware plant.physics remains an explicit SKIP."]}
    write_json(output / "report.json", report)
    observer = primary = binary = manifest = scenario_manifest = None
    previous_signal = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"DJI feedback Scenario interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    def inputs():
        result = source_snapshot(elf, binary, firmware)
        result["dji_consumer"] = dji_sources(elf, binary, firmware)
        result["feedback_firmware"] = sha(firmware / "app/selftest/dji_feedback.cc")
        paths = [ROOT / name for name in ("models/two_four_wheel.xml", "models/four_wheel.xml",
            "models/mujoco-LICENSE.txt", "flake.lock", "tools/dm_mc02_arena_smoke.py",
            "tools/dm_mc02_shared_world_smoke.py")]
        paths += [firmware / name for name in ("Core/Src/fdcan.c",
            "Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_fdcan.c",
            "Middlewares/Third_Party/FreeRTOS/Source/tasks.c",
            "Middlewares/Third_Party/FreeRTOS/Source/portable/GCC/ARM_CM4F/port.c")]
        if manifest is not None:
            paths += [manifest.path, *(item.robot.path for item in manifest.robots)]
        if scenario_manifest is not None:
            paths.append(scenario_manifest.path)
        result.update({str(path): sha(path) for path in paths})
        return result
    try:
        elf = arena.regular_file(elf, "DjiFeedbackTest ELF")
        binary = arena.resolve_qemu(qemu)
        firmware = Path(firmware).resolve()
        if not firmware.is_dir():
            raise ValueError("firmware source tree is not a directory")
        directory = output / "arena"
        directory.mkdir()
        scenario_manifest = write_dji_feedback_manifest(directory, (elf, elf))
        manifest = scenario_manifest.arena
        report["scenario_input"] = {"path": str(scenario_manifest.path),
            "duration_ns": scenario_manifest.duration_ns,
            "can_delays": [asdict(item) for item in scenario_manifest.can_delays],
            "motor_feedback": [asdict(item) for item in scenario_manifest.motor_feedback]}
        report["inputs_before"], report["checkout_before"] = inputs(), checkout_snapshot(firmware)
        report["actual_sources_before"] = source_origins()
        observer = DjiFeedbackObservation(report, manifest)
        overrides = {"create_arena_plant": "world", "running_qemu": "qemu", "QmpSession": "qmp",
            "connect_unix": "connect", "SharedWorldRuntime": "shared", "LabChannels": "channels", "CanStream": "can"}
        for original, replacement in overrides.items():
            setattr(observer, "original_" + replacement, getattr(arena, original))
        with patch.multiple(arena, **{name: getattr(observer, method) for name, method in overrides.items()}):
            result = scenario_runtime.run_manifest(scenario_manifest, qemu=binary,
                startup_timeout=10., timeout=20., emit=observer.emit, on_response=observer.response)
        report["runtime_result"] = asdict(result)
        calls = report["native_calls"]
        if (result.end_ns - result.origin_ns != 400_000_000 or result.origin_ns >= DJI_FEEDBACK_LOSS_NS
                or calls["reset"] != 1 or calls["close"] != 1 or len(calls["step_ns"]) != result.steps
                or sum(calls["step_ns"]) != result.end_ns
                or any(not 0 < dt <= 1_000_000 for dt in calls["step_ns"])
                or len(result.boards) != 2 or result.referees or result.children):
            raise ValueError("DJI feedback world/result scope disagrees")
        for wire_session, board in enumerate(result.boards, 1):
            if board.ready != "uart_marker" or board.ready_qemu_ns is None:
                raise ValueError("DJI feedback actual UART ready missing")
            report["ready"][board.name]["post_read_qemu_ns"] = board.ready_qemu_ns
            check_referee_responses(report["responses"][board.name], wire_session,
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
                    primary.add_note(f"DJI feedback post-cleanup verification failed: {type(error).__name__}: {error}")
                    for note in tuple(getattr(error, "__notes__", ())):
                        primary.add_note(note)
        for name in DJI_SCENARIO_MEMBERS:
            rows = report["cases"][name]
            report["missing"][name] = sorted(DJI_FEEDBACK_REQUIRED -
                {row["name"] for row in rows if row["status"] == "PASS"})
            report["skips"][name] = [row["name"] for row in rows if row["status"] == "SKIP"]
            report["missing_skips"][name] = [] if "plant.physics" in report["skips"][name] else ["plant.physics"]
            verify(lambda name=name: check_dji_feedback_coverage(report["cases"][name], report["summaries"][name]))
        report["physical_can_suffixes"] = [] if observer is None else [
            {"peer": item.peer, "suffix_hex": item.suffix.hex()} for item in observer.written]
        verify(lambda: check_dji_feedback_observations(report))
        verify(lambda: verify_firmware_cleanup(report, observer, label="DJI feedback Scenario", controls=True))
        verify(lambda: verify_firmware_sources(report, inputs, firmware, observer=observer, label="DJI feedback Scenario"))
        verify(lambda: verify_firmware_watchdogs(report, label="DJI feedback Scenario"))
        signal.signal(signal.SIGTERM, previous_signal)
        report["result"] = "passed" if primary is None else "failed"
        if primary is not None:
            report["errors"].insert(0, f"{type(primary).__name__}: {primary}")
            report["error_type"], report["notes"] = type(primary).__name__, list(getattr(primary, "__notes__", ()))
        write_json(output / "report.json", report)
    print("RESULT: dual-QEMU DJI feedback Scenario consumer " + report["result"])
    if primary is not None:
        print(f"DJI feedback Scenario failed: {primary}", file=sys.stderr)
    return 0 if primary is None else 130 if isinstance(primary, KeyboardInterrupt) else 1


def run_referee(elf, output, firmware, *, qemu=None, scenario=False, cli=False):
    """One existing Arena execution, with passed published after cleanup checks."""
    if cli and not scenario:
        raise ValueError("referee --cli requires --scenario")
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
    if scenario:
        report["scenario"] = True
        report["scenario_observation"] = {"event_ns": SCENARIO_EVENT_NS,
            "samples": {name: {} for name in REFEREE_IDS}, "stage": None, "control": None,
            "shared_start": None, "queue_calls": []}
        report["limitations"][0] = "Actual dual UART7/HAL/DMA/msg consumer of explicit Scenario events."
        report["limitations"].append(
            "West UART1 a5 only releases application observation after east changed report; it does not change the world or synchronize CPUs.")
        report["limitations"].append(
            "West sample2 requires all four fresh public timestamps after the firmware's post-a5 snapshot; this does not identify an exact host publication batch.")
    if cli:
        report["cli"] = {}
        report["limitations"].append(
            "Instrumented runpy unmigrated application tool entry in the existing fixture subprocess; not an unmodified external python -m sim.apps.unmigrated_cli run.")
    write_json(output / "report.json", report)
    observer = primary = binary = manifest = scenario_manifest = None
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
        if scenario_manifest is not None:
            paths.append(scenario_manifest.path)
        result.update({str(path): sha(path) for path in paths})
        return result
    try:
        elf = arena.regular_file(elf, "ScenarioRefereeTest ELF" if scenario else "ArenaRefereeTest ELF")
        binary = arena.resolve_qemu(qemu)
        firmware = Path(firmware).resolve()
        if not firmware.is_dir():
            raise ValueError("firmware source tree is not a directory")
        directory = output / "arena"
        directory.mkdir()
        if scenario:
            from sim.scenario import runtime as scenario_runtime

            run_scenario = scenario_runtime.run_manifest

            scenario_manifest = write_scenario_manifest(directory, (elf, elf))
            manifest = scenario_manifest.arena
            report["scenario_input"] = {"path": str(scenario_manifest.path),
                "duration_ns": scenario_manifest.duration_ns,
                "referees": [asdict(item) for item in scenario_manifest.referees]}
        else:
            manifest = write_manifests(directory, (elf, elf), referees=True)
        report["inputs_before"], report["checkout_before"] = inputs(), checkout_snapshot(firmware)
        report["actual_sources_before"] = source_origins()
        observer = RefereeObservation(report, manifest, scenario=scenario, scenario_manifest=scenario_manifest)
        observer.binary = binary
        overrides = {"create_arena_plant": "world", "running_qemu": "qemu", "QmpSession": "qmp",
            "connect_unix": "connect", "SharedWorldRuntime": "shared",
            "RefereeEngine": "engine", "RefereeUartEndpoint": "endpoint"}
        if scenario:
            overrides["LabChannels"] = "channels"
        for original, replacement in overrides.items():
            setattr(observer, "original_" + replacement, getattr(arena, original))
        with patch.multiple(arena, **{name: getattr(observer, method) for name, method in overrides.items()}):
            options = dict(qemu=binary, startup_timeout=10., timeout=20.,
                           emit=observer.emit, on_response=observer.response)
            if cli:
                observer.original_run = run_scenario
                with patch.object(scenario_runtime, "run_manifest", observer.run):
                    result = invoke_cli(observer, output / "cli-result.json", scenario=True)
            elif scenario:
                result = run_scenario(scenario_manifest, **options)
            else:
                result = arena.run_manifest(manifest, duration=1., **options)
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
        if scenario:
            verify(lambda: check_scenario_observations(report))
        verify(lambda: verify_firmware_cleanup(report, observer, label="Arena referee",
            referees=True, controls=scenario))
        verify(lambda: verify_firmware_sources(report, inputs, firmware,
            observer=observer, cli=cli))
        verify(lambda: verify_firmware_watchdogs(report))
        signal.signal(signal.SIGTERM, previous_signal)
        report["result"] = "passed" if primary is None else "failed"
        if primary is not None:
            report["errors"].insert(0, f"{type(primary).__name__}: {primary}")
            report["error_type"] = type(primary).__name__
            report["notes"] = list(getattr(primary, "__notes__", ()))
        write_json(output / "report.json", report)
    label = "Scenario" if scenario else "Arena"
    print("RESULT: dual-QEMU " + label + " referee consumer " + report["result"])
    if primary is not None:
        print(f"Arena referee consumer failed: {primary}", file=sys.stderr)
        for note in report["notes"]:
            print(note, file=sys.stderr)
    return 0 if primary is None else 130 if isinstance(primary, KeyboardInterrupt) else 1


def main(args):
    if args.multi_cut and (args.cli or args.mode != "normal"):
        raise ValueError("--multi-cut requires the normal Arena runtime consumer")
    if args.can_deadlines and (args.multi_cut or args.cli or args.mode != "normal"):
        raise ValueError("--can-deadlines requires normal mode without --cli or --multi-cut")
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
    if args.multi_cut:
        report["multi_cut"] = {"quantum_ns": MULTI_CUT_QUANTUM_NS,
                               "members": [member.name for member in manifest.robots], "events": []}
        report["limitations"].append(
            "400/400/200us World cuts retain1ms CAN feedback phase; unchanged receipt goals and missing IMU consumption are permitted.")
    if args.can_deadlines:
        report["can_deadlines"] = {"delay_ns": CAN_DEADLINE_DELAY_NS, "outer_step_ns": 1_000_000,
            "members": [member.name for member in manifest.robots], "events": []}
        report["limitations"].append(
            "Known queued CAN dues add actual CPU/World/STEP cuts; interval outputs remain end-sampled. Future feedback stays pending at the original50ms duration without an extra drain step.")
    observer = Observation(report, manifest, args.mode)
    observer.binary = binary
    if args.cli:
        report["cli"] = {}
        report["limitations"].append(
            "Instrumented runpy unmigrated application tool entry in the existing shell subprocess; not an unmodified external python -m sim.apps.unmigrated_cli run.")
        for path in (ROOT / "tools/sim-unmigrated", ROOT / "sim/apps/unmigrated_cli.py"):
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
            check_cli_sources(observer, current)
        report.update(actual_sources=current, source_preserved=True)
    except BaseException as error:
        verification_errors.append(error)
    try:
        calls = report["native_calls"]
        assert calls["reset"] == calls["close"] == 1 and calls["step_ns"]
        if args.multi_cut:
            check_multi_cut_observations(report)
        elif args.can_deadlines:
            check_can_deadline_observations(report)
        else:
            assert all(dt == 1_000_000 for dt in calls["step_ns"])
        assert len(observer.plant.devices) == 8 and report["world"]["distinct_buses"] == 2
        assert all(device.closed for device in observer.plant.devices)
        assert len(observer.owners) == 2 and len(observer.children) == 1
        if not args.can_deadlines:
            assert all(not pending for pending in observer.feedback_sources.values())
        assert all(delivery is None for delivery in observer.feedback_deliveries.values())
        assert all(not item.endpoint.stream.tx_pending for item in observer.runtime.bindings)
        report["device_cleanup"] = [device.closed for device in observer.plant.devices]
        assert {event["peer"] for event in report["commands"]} == {0, 1}
        resets = {event["name"]: event["decoded"]["virtual_time_ns"]
            for event in report["responses"] if event["header"]["kind"] == codec.RESET_ACK}
        assert set(resets) == {member.name for member in manifest.robots}
        for event in report["feedback"]:
            assert event["qemu_ns"] == resets[manifest.robots[event["peer"]].name] + event["delivered_ns"]
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
                assert any(event["peer"] == 0 and event["command"] == "system_reset"
                           for event in report["qmp_events"])
                if isinstance(primary, V2SessionReset):
                    report["reset_observation"] = "cosim_session_reset"
                else:
                    assert type(primary) is RuntimeError
                    assert str(primary) == "CAN input execution target is not reached"
                    execution = report["cpu_execution"][0]
                    assert execution["last_execution"]["state"] == "cancelled"
                    assert execution["last_execution"]["target-ns"] == execution["last_target_ns"]
                    assert execution["last_status"]["running"] is False
                    # QEMU 8.2.2 system/runstate.c sets a stopped machine to
                    # PRELAUNCH after system_reset. The batch observes that
                    # fresh status; the old scalar fence failed before reading it.
                    assert execution["last_status"]["status"] == "prelaunch"
                    report["reset_observation"] = "native_cancelled_before_next_cpu"
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
    parser.add_argument("--multi-cut", action="store_true",
                        help="verify400/400/200us cuts inside each normal Arena runtime1ms step")
    parser.add_argument("--can-deadlines", action="store_true",
                        help="verify known250us delayed CAN dues with original1ms outer steps and50ms duration")
    parser.add_argument("--mode", choices=("normal", "reset", "peer-exit", "upper-exit", "interrupt"), default="normal")
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    if args.mode == "interrupt" and not args.cli:
        parser.error("--mode interrupt requires --cli")
    if args.multi_cut and (args.cli or args.mode != "normal"):
        parser.error("--multi-cut requires normal mode without --cli")
    if args.can_deadlines and (args.multi_cut or args.cli or args.mode != "normal"):
        parser.error("--can-deadlines requires normal mode without --cli or --multi-cut")
    try:
        sys.exit(main(args))
    except ModuleNotFoundError as error:
        if error.name != "mujoco":
            raise
        print("SKIP: optional native MuJoCo runtime is unavailable", file=sys.stderr)
        sys.exit(77)
