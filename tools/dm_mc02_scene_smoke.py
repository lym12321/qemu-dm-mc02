#!/usr/bin/env python3
"""Native application gates through the public sim/v1 experiment path.

Only bounded observers live here. Simulation owns every CPU target, world step,
connection delivery and teardown. UART reports supplement actual CAN/IMU bytes;
no host PASS text is injected and no global CPU/plant barrier is claimed.
"""
from contextlib import nullcontext
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict
import argparse
import hashlib
import json
from pathlib import Path
import signal
import struct
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crccheck.crc import Crc8MaximDow, Crc16Mcrf4Xx
from sim.apps.experiment import run_experiment
from sim.apps.presets import four_wheel_document, add_can_faults
from sim.boards.launch import resolve_qemu
from sim.composition import builtin_registry
from sim.config.scene import load_scene
from sim.transports import step_codec as codec


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def starttime(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def source_origins():
    from sim.machines.qemu.qmp import qmp_client_provenance
    client_root = Path(qmp_client_provenance()["package_root"])
    result = {}
    for name, module in sorted(sys.modules.items()):
        if name == "__main__" or name.startswith(("sim.", "tools.", "dm_mc02_", "qemu.")):
            value = getattr(module, "__file__", None)
            if value is None:
                continue
            path = Path(value).resolve()
            if name == "__main__" and not path.is_file():
                continue
            owner = client_root if name.startswith("qemu.") else ROOT
            if not path.is_relative_to(owner):
                raise AssertionError(f"actual native module outside checkout: {name} {path}")
            result[name] = {"path": str(path), "sha256": sha(path)}
    return result


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if hasattr(value, "__dataclass_fields__"):
        return plain(asdict(value))
    return value


def bounded(rows, row, maximum=10000):
    if len(rows) >= maximum:
        raise BufferError("native scene observation capacity exhausted")
    rows.append(row)

REFEREE_IDS = {"east": 3, "west": 103}

SCENARIO_EVENT_NS = 200_000_000

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

DJI_SCENARIO_EVENT_NS = 250_000_000

DJI_SCENARIO_MEMBERS = ("east", "west")

DJI_SCENARIO_FIELDS = ("angle", "speed", "current", "temp", "ts", "output",
                       "enabled", "update_age", "feedback_age")

DJI_SCENARIO_REQUIRED = frozenset(("control.rx", "reset.poweron", "bsp.init", "task.stack",
    "dji.scenario.stop", "dji.can", *(f"dji.scenario.{phase}.id{identifier}.{field}"
        for phase in (1, 2) for identifier in (1, 2) for field in DJI_SCENARIO_FIELDS)))

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

DJI_FEEDBACK_LOSS_NS = 200_000_000

DJI_FEEDBACK_RECOVERY_NS = 350_000_000

DJI_FEEDBACK_REQUIRED = frozenset(("control.rx", "reset.poweron", "bsp.init", "task.stack",
    "dji.feedback.baseline.id1", "dji.feedback.baseline.id2", "dji.feedback.loss.frozen",
    "dji.feedback.loss.age", "dji.feedback.loss.app_age", "dji.feedback.loss.output",
    "dji.feedback.loss.id2", "dji.feedback.recover.id1", "dji.feedback.recover.id2",
    "dji.feedback.stop", "dji.feedback.can"))

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

REFEREE_PAYLOADS = {
    "east": ("0100000807060504030201", "03026400c80000006400500005",
             "000000000000000028000a001400", "050900070b0d00fe"),
    "west": ("0200000807060504030201", "67024600c80000006400780006",
             "000000000000000029000c001900", "08110013171d0080"),
}

SCENARIO_EAST_PAYLOADS = (
    "0100000807060504030201", "03024600c80000006400500005",
    "000000000000000028000f001b00", "08110013171d0080",
)

class NativeObservation:
    """Observe public factories, streams and events without driving component time."""

    def __init__(self, report, members, profile):
        self.report, self.members, self.profile = report, tuple(members), profile
        self.machines, self.streams, self.identities = {}, {}, {}
        self.host_identities = []
        self.buffers = {name: bytearray() for name in members}
        self.ready, self.stages, self.controls, self.done = {}, {}, {}, set()
        self.samples = {name: {} for name in members}
        self.latest_cut = 0
        self.injected_failure = False
        self.simulation = None
        self.owners = {}
        self.observation_id = 0
        self.committed = {}

    def multi_cut_event(self, kind, **values):
        evidence = self.report.get("multi_cut")
        if evidence is None:
            return
        self.observation_id += 1
        bounded(evidence["events"], {"kind": kind, "order": self.observation_id, **values}, 10000)

    def wrap_machine(self, original):
        def create(spec, context):
            machine = original(spec, context)
            self.machines[spec.name] = machine
            command, command_many = machine.command, machine.command_many
            busy = False
            def observed_command(operation, arguments=None):
                nonlocal busy
                if busy:
                    return command(operation, arguments)
                label = operation + (":" + arguments.get("property", "") if arguments else "")
                row = self.report["qmp_stats"].setdefault(label, {"calls": 0, "seconds": 0.0})
                row["calls"] += 1
                started = time.monotonic()
                busy = True
                try:
                    if operation == "x-run-to-virtual-time":
                        self.multi_cut_event("cpu-request", member=spec.name,
                                             native_target_ns=arguments["target-ns"])
                    return command(operation, arguments)
                finally:
                    busy = False
                    row["seconds"] += time.monotonic() - started
            def observed_many(commands):
                nonlocal busy
                if busy:
                    return command_many(commands)
                label = "batch:" + ",".join(operation + ":" + (arguments or {}).get("property", "")
                                             for operation, arguments in commands)
                row = self.report["qmp_stats"].setdefault(label, {"calls": 0, "seconds": 0.0})
                row["calls"] += 1
                started = time.monotonic()
                busy = True
                try:
                    return command_many(commands)
                finally:
                    busy = False
                    row["seconds"] += time.monotonic() - started
            machine.command, machine.command_many = observed_command, observed_many
            start, close = machine.start, machine.close
            def started():
                start()
                info = plain(machine.connection_info)
                self.identities[spec.name] = {**info, "starttime": starttime(info["pid"])}
                self.report["qemu"].setdefault("native_origins", {})[spec.name] = info["native_origin_ns"]
                self.report["qemu"].setdefault("versions", {})[spec.name] = plain(machine.command("query-version"))
                streams = dict(machine.streams)
                self.streams[spec.name] = streams
                if "multi_cut" in self.report and "cosim" in streams:
                    stream = streams["cosim"]
                    send_cosim, receive_cosim = stream.send, stream.receive_records
                    def observed_cosim_send(encoded):
                        frame = codec.decode_step_frame(encoded)
                        result = send_cosim(encoded)
                        if frame.header.kind == codec.STEP:
                            self.multi_cut_event("step-send", member=spec.name, header=plain(frame.header))
                        return result
                    def observed_cosim_receive():
                        for raw in receive_cosim():
                            if int.from_bytes(raw[8:10], "little") == 2:
                                frame = codec.decode_step_frame(raw)
                                if frame.header.kind == codec.STEP_ACK:
                                    self.multi_cut_event("step-ack", member=spec.name,
                                        header=plain(frame.header), payload=plain(codec.decode_step_ack_payload(frame.payload)))
                            yield raw
                    stream.send, stream.receive_records = observed_cosim_send, observed_cosim_receive
                if "can1" in streams:
                    stream, sends = streams["can1"], self.report["wire_sends"]
                    send = stream.send
                    def observed_send(can_id, flags, dlc, stamp, data):
                        row = {"member": spec.name, "can_id": can_id, "flags": flags,
                               "dlc": dlc, "native_ns": stamp, "data_hex": data.hex(), "accepted": False}
                        bounded(sends, row, 20000)
                        result = send(can_id, flags, dlc, stamp, data)
                        row["accepted"] = True
                        return result
                    stream.send = observed_send
                    receive = stream.receive_records
                    def observed_receive():
                        for raw in receive():
                            if len(raw) != 84 or raw[9:12] != bytes(3) or raw[28:] != bytes(56):
                                raise ValueError("actual CAN transport reserved bytes/length disagree")
                            bounded(self.report["wire_commands"], {"member": spec.name,
                                "native_ns": int.from_bytes(raw[12:20], "little"), "wire_hex": raw.hex()}, 20000)
                            yield raw
                    stream.receive_records = observed_receive
            def closed():
                try:
                    return close()
                finally:
                    self.report["closed_machines"].append(spec.name)
            commit = machine.commit_inputs
            def committed(cut_ns):
                result = commit(cut_ns)
                if "multi_cut" in self.report:
                    completed = self.committed.setdefault(cut_ns, set())
                    completed.add(spec.name)
                    if completed == set(self.members):
                        # Fresh public native snapshots after both ACK returns,
                        # including cuts on which CAN submitted no new frames.
                        for member in self.members:
                            state = self.machines[member].command("qom-get", {
                                "path": "/machine", "property": "can-execution-state"})
                            submitted = sum(row["member"] == member and row["accepted"]
                                            for row in self.report["wire_sends"])
                            self.multi_cut_event("can-observation", member=member, cut_ns=cut_ns,
                                                 submitted_frames=submitted, state=plain(state))
                        del self.committed[cut_ns]
                return result
            machine.start, machine.close, machine.commit_inputs = started, closed, committed
            return machine
        return create

    def capture_factory(self, original):
        def create(spec, context):
            owner = original(spec, context)
            self.owners[spec.name] = owner
            return owner
        return create

    def native(self, original):
        def acquire(*args, **kwargs):
            native = original(*args, **kwargs)
            calls = self.report["native_calls"]
            calls["instances"] += 1
            reset, step, close = native.reset, native.step, native.close
            def reset_native():
                calls["reset"] += 1
                return reset()
            def step_native(dt):
                bounded(calls["step_ns"], dt, 31250)
                result = step(dt)
                if abs(native.time_s - sum(calls["step_ns"]) / 1e9) > 1e-10:
                    raise ValueError("sole native solver time differs from submitted intervals")
                self.multi_cut_event("world-completed", target_ns=sum(calls["step_ns"]),
                                     dt_ns=dt, native_time_s=native.time_s)
                return result
            def close_native():
                calls["close"] += 1
                return close()
            native.reset, native.step, native.close = reset_native, step_native, close_native
            return native
        return acquire

    def watchdog(self, name, stage):
        from tools.dm_mc02_firmware_rtf import read_iwdg_timeouts
        count, diagnostics = read_iwdg_timeouts(self.machines[name])
        self.report["watchdogs"].setdefault(name, {})[stage] = {"timeouts": count, "diagnostics": diagnostics}
        if count or "window-violations=0" not in diagnostics:
            raise ValueError("actual firmware watchdog event: " + name)

    def event(self, event):
        name, kind, payload = event.source_node, event.kind, event.payload
        self.latest_cut = event.delivery_time_ns
        if kind == "process.started":
            pid = payload["pid"]
            identity = starttime(pid)
            if type(pid) is not int or pid <= 0 or identity is None:
                raise RuntimeError("started host process has no live native identity")
            bounded(self.host_identities, {"node": name, "name": payload["name"],
                "pid": pid, "starttime": identity}, 8)
        elif kind == "uart" and name in self.members and event.source_port == "uart1":
            self.uart(name, payload, event)
        elif kind == "can-tx":
            bounded(self.report["commands"], {"member": name, "frame": plain(payload),
                "source_ns": event.source_time_ns, "mapped_ns": event.mapped_time_ns,
                "delivery_ns": event.delivery_time_ns}, 20000)
        elif kind == "can.delivery" and payload["destination"].endswith(".fdcan1"):
            frame = payload["frame"]
            if not 0x201 <= frame.can_id <= 0x204:
                raise ValueError("unexpected motor feedback ID")
            bounded(self.report["feedback"], {"member": payload["destination"].split(".")[0],
                "can_id": frame.can_id, "data_hex": frame.data.hex(),
                "source_ns": event.source_time_ns, "due_ns": payload["due_ns"],
                "delivery_ns": event.delivery_time_ns, "dropped": payload["dropped"]}, 20000)
        elif kind == "can-input-processed":
            bounded(self.report["can_receipts"], {"member": name, "native_ns": event.source_time_ns,
                "delivery_ns": event.delivery_time_ns, "receipt": plain(payload)}, 10000)
        elif kind in ("imu-input-accepted", "imu-consumption"):
            summary = self.report["imu"][name]
            summary[kind] += 1
            summary["last_" + kind] = plain(payload)
            if kind == "imu-consumption" and payload.get("consumed_mask") == 3 and not payload.get("missing_mask"):
                summary["full_consumed"] += 1
                bounded(self.report["imu_witnesses"][name], plain(payload), 128)
        elif kind == "referee.batch.admitted":
            member = name.removesuffix("_referee")
            check_referee_wire(payload["data"], member, payload["first_sequence"],
                logical_ns=event.delivery_time_ns, scenario=self.profile == "referee-scenario")
            bounded(self.report["referee"], {"member": member, "cut_ns": event.delivery_time_ns,
                "data_hex": payload["data"].hex(), "first_sequence": payload["first_sequence"]}, 64)
        elif kind == "can.fault.applied":
            bounded(self.report["faults"], {"node": name, "cut_ns": event.delivery_time_ns,
                "profile": payload["profile"], "delay_ns": payload["delay_ns"], "drop": plain(payload["drop"])}, 32)
        elif kind == "input.delivered" and name in self.members and event.source_port == "uart1":
            if payload != b"\xa5" or name not in self.controls:
                raise ValueError("unexpected actual UART control input")
            self.controls[name]["delivered_ns"] = event.delivery_time_ns

    def uart(self, name, data, event):
        if type(data) is not bytes:
            raise TypeError("actual UART bytes required")
        text = data.decode("ascii")
        if len(self.report["uart"][name]) + len(text) > 12000:
            raise BufferError("firmware UART evidence exhausted")
        self.report["uart"][name] += text
        buffer = self.buffers[name]
        buffer.extend(data)
        while b"\n" in buffer:
            raw, _, suffix = buffer.partition(b"\n")
            buffer[:] = suffix
            line = raw.decode("ascii").strip()
            if not line.startswith("TTEST "):
                if line.startswith("WORLD_RESULT="):
                    bounded(self.report["world_results"], {"member": name, "cut_ns": event.delivery_time_ns,
                        "native_upper_ns": event.source_time_ns, "line": line}, 2)
                if line.startswith("WORLD_BAD"):
                    raise ValueError(line)
                continue
            fields = line.split()
            if len(fields) != 5:
                raise ValueError("actual firmware report requires five fields")
            if fields[1] in ("PHASE", "DONE"):
                bounded(self.report["summaries"][name], {"kind": fields[1], "values": list(map(int, fields[2:]))}, 2)
                if fields[1] == "DONE":
                    self.done.add(name)
                    self.watchdog(name, "done")
                continue
            row = {"status": fields[1], "name": fields[2], "actual": int(fields[3]), "expected": int(fields[4])}
            if row["status"] == "READY":
                if row["actual"] or row["expected"]:
                    raise ValueError("firmware READY values disagree")
                if row["name"].endswith(".ready"):
                    self.ready[name] = event.delivery_time_ns
                else:
                    self.stages[name] = event.delivery_time_ns
                continue
            checker = (check_dji_scenario_case if self.profile == "dji-scenario" else
                       check_dji_feedback_case if self.profile == "dji-feedback" else
                       lambda item: check_referee_case(item, name))
            checker(row)
            bounded(self.report["cases"][name], row, 64)
            if row["name"].startswith("dji.scenario.") and row["name"] != "dji.scenario.stop":
                phase = row["name"].split(".")[2]
                self.samples[name].setdefault(phase, event.delivery_time_ns)
            if row["name"].startswith("referee.arena."):
                phase = row["name"].split(".")[2]
                self.samples[name].setdefault(phase, event.delivery_time_ns)

    def cut(self, cut_ns, simulation):
        if self.simulation is None:
            self.simulation = simulation
            close = simulation.close
            def observe_failure():
                from sim.runtime import SimulationState
                failure = simulation.failure
                row = {"state": simulation.state.value, "committed_ns": simulation.t_sim_ns,
                       "failure_type": None if failure is None else type(failure).__name__}
                self.report["before_cleanup"] = row
                if simulation.state == SimulationState.FAILED:
                    steps = tuple(self.report["native_calls"]["step_ns"])
                    try:
                        simulation.run_until(simulation.t_sim_ns + 1)
                    except RuntimeError as error:
                        row["retry_rejected"] = str(error)
                    else:
                        raise AssertionError("failed Simulation accepted a new execution request")
                    row["effects_preserved"] = (simulation.t_sim_ns == row["committed_ns"]
                        and tuple(self.report["native_calls"]["step_ns"]) == steps
                        and simulation.failure is failure)
                    if not row["effects_preserved"]:
                        raise AssertionError("failed Simulation replay changed committed/native effects")
            def observed_close():
                try:
                    observe_failure()
                finally:
                    close()
            simulation.close = observed_close
        mode = self.report["expected_mode"]
        if mode != "normal" and cut_ns >= 20_000_000 and not self.injected_failure:
            self.injected_failure = True
            self.report["failure_injected_ns"] = cut_ns
            if mode == "peer-exit":
                self.machines[self.members[-1]].command("quit")
            elif mode == "reset":
                self.machines[self.members[0]].command("system_reset")
            elif mode == "interrupt":
                raise KeyboardInterrupt("native fixture interrupted at a completed source cut")
            elif mode == "upper-exit":
                Path(self.report["upper_exit_file"]).touch()
        if self.profile == "dji-scenario" and cut_ns >= 250_000_000 and len(self.stages) == len(self.members):
            for name in self.members:
                if "1" not in self.samples[name] or self.samples[name]["1"] >= 250_000_000:
                    raise ValueError("original reset-relative250ms baseline missed")
                if name not in self.controls:
                    simulation.schedule_input(name + ".uart1", b"\xa5", at_ns=cut_ns)
                    self.controls[name] = {"scheduled_ns": cut_ns}
        if self.profile == "referee-scenario" and "west" in self.stages and "2" in self.samples["east"]:
            if "west" not in self.controls:
                if any("1" not in self.samples[name] or self.samples[name]["1"] >= SCENARIO_EVENT_NS for name in self.members):
                    raise ValueError("original referee session-relative baseline missed")
                simulation.schedule_input("west.uart1", b"\xa5", at_ns=cut_ns)
                self.controls["west"] = {"scheduled_ns": cut_ns}
        self.report["cuts"] += 1
        bounded(self.report["cut_times_ns"], cut_ns, 31251)
        if self.profile == "bare":
            self.report["final_networks"] = {name: {"pending": owner.pending,
                "next_due_ns": owner.next_due_ns} for name, owner in self.owners.items()
                if name.endswith("_can")}
        if self.profile.startswith(("dji-", "referee")):
            for name in self.members:
                # Refresh final native diagnostics at every completed final-ready
                # cut only once; each CPU is paused at this exact boundary.
                duration = self.report["duration_ns"]
                if len(self.ready) == len(self.members) and cut_ns == max(self.ready.values()) + duration:
                    self.watchdog(name, "paused")

    def verify(self, result):
        report = self.report
        calls = report["native_calls"]
        if calls["instances"] != 1 or calls["reset"] != 1 or calls["close"] != 1:
            raise ValueError("scene must own exactly one native solver/reset/close")
        if sum(calls["step_ns"]) != result.end_ns:
            raise ValueError("native effects differ from final logical commit")
        for name in self.members:
            if self.profile.startswith("dji-"):
                coverage = check_dji_scenario_coverage if self.profile == "dji-scenario" else check_dji_feedback_coverage
                coverage(report["cases"][name], report["summaries"][name])
            elif self.profile.startswith("referee"):
                check_referee_coverage(report["cases"][name], report["summaries"][name], name)
            if self.profile.startswith(("dji-", "referee")):
                if set(report["watchdogs"].get(name, {})) != {"done", "paused"}:
                    raise ValueError("firmware done/final watchdog evidence missing")
            feedback = [row for row in report["feedback"] if row["member"] == name and not row["dropped"]]
            sends = [row for row in report["wire_sends"] if row["member"] == name]
            if not feedback or len(feedback) != len(sends) or not all(row["accepted"] for row in sends):
                raise ValueError("feedback does not match actual native transport submissions")
            origin = self.identities[name]["native_origin_ns"]
            for row, send in zip(feedback, sends, strict=True):
                if (row["can_id"], row["data_hex"], origin + row["delivery_ns"]) != (
                        send["can_id"], send["data_hex"], send["native_ns"]):
                    raise ValueError("CAN source/delivery/native epoch provenance differs")
            receipts = [row for row in report["can_receipts"] if row["member"] == name]
            if not receipts or any(row["native_ns"] != origin + row["delivery_ns"] for row in receipts):
                raise ValueError("actual native CAN receipt epoch missing")
            if self.profile == "dji-scenario":
                values = {row["name"]: row["actual"] for row in report["cases"][name]}
                for phase in (1, 2):
                    for identifier in (1, 2):
                        prefix = f"dji.scenario.{phase}.id{identifier}."
                        actual = tuple(values[prefix + field] for field in ("angle", "speed", "current", "temp"))
                        if not any(actual == (a, b & 0xffffffff, c & 0xffffffff, d)
                            for row in feedback if row["can_id"] == 0x200 + identifier
                            for a, b, c, d, _ in [struct.unpack(">HhhBB", bytes.fromhex(row["data_hex"]))]):
                            raise ValueError("actual copied guest raw has no same-peer native feedback witness")
                if name not in self.controls or "delivered_ns" not in self.controls[name]:
                    raise ValueError("firmware observation handshake was not actually delivered")
        if self.profile == "dji-scenario":
            east = [row for row in report["feedback"] if row["member"] == "east"]
            if not any(row["due_ns"] - row["source_ns"] == 5_000_000 for row in east):
                raise ValueError("delayed feedback evidence absent")
            if not any(row["source_ns"] > 250_000_000 and row["due_ns"] == row["source_ns"] for row in east):
                raise ValueError("restored feedback evidence absent")
        if self.profile == "dji-feedback":
            for name in self.members:
                dropped = [row for row in report["feedback"] if row["member"] == name and row["dropped"]]
                if not dropped or any(row["can_id"] != 0x201 or not 200_000_000 <= row["delivery_ns"] < 350_000_000 for row in dropped):
                    raise ValueError("motor1-only feedback loss scope differs")
        if self.profile == "bare":
            rows = report["world_results"]
            if len(rows) != len(self.members) or {row["member"] for row in rows} != set(self.members):
                raise ValueError("actual firmware raw CAN/IMU result missing")
            for row in rows:
                name, fields = row["member"], row["line"].split()
                count = report.get("motor_count", 4)
                if len(fields) != count + 4 or fields[0] != "WORLD_RESULT=" + str(self.members.index(name)):
                    raise ValueError("actual firmware raw result has wrong shape/peer")
                for identifier, data_hex in enumerate(fields[1:count + 1], 1):
                    if not any(item["member"] == name and item["can_id"] == 0x200 + identifier
                        and item["data_hex"] == data_hex and not item["dropped"]
                        and item["delivery_ns"] <= row["cut_ns"] for item in report["feedback"]):
                        raise ValueError("guest CAN raw lacks same-peer earlier delivery witness")
                gyro = struct.unpack("<3h", bytes.fromhex(fields[count + 1]))
                accel = struct.unpack("<3h", bytes.fromhex(fields[count + 2]))
                if any(code == -32768 or code == 32767 for code in (*gyro, *accel)):
                    raise ValueError("raw fixture IMU saturated")
                if not report["imu"][name]["full_consumed"] or not int(fields[count + 3], 16):
                    raise ValueError("raw BMI088 full-consumed or final volatile sensortime-store evidence missing")
                row.update(gyro_raw=gyro, accel_raw=accel, sensortime=int(fields[count + 3], 16))
        if report["known_deadlines"]:
            check_can_deadline_observations(report)
        if report["quantum_ns"] == 400_000:
            check_multi_cut_observations(report)
        report["controls"] = self.controls

    def cleanup(self):
        errors = []
        for name, info in self.identities.items():
            sockets = info["sockets"]
            row = {"member": name, **info,
                "alive": starttime(info["pid"]) == info["starttime"],
                "remaining_sockets": [path for path in sockets.values() if Path(path).exists()],
                "open_streams": [key for key, stream in self.streams[name].items() if stream.sock.fileno() >= 0]}
            self.report["cleanup"].append(row)
            if row["alive"] or row["remaining_sockets"] or row["open_streams"]:
                errors.append(name)
        self.report["host_cleanup"] = []
        for identity in self.host_identities:
            owner = self.owners[identity["node"]]
            child = next(child for child in owner.children if child.process.pid == identity["pid"])
            process = child.process
            row = {**identity, "alive": starttime(identity["pid"]) == identity["starttime"],
                "reaped": process.returncode is not None, "owner_closed": owner.closed,
                "pipes_closed": all(getattr(process, pipe).closed for pipe in ("stdout", "stderr"))}
            self.report["host_cleanup"].append(row)
            if row["alive"] or not all(row[key] for key in ("reaped", "owner_closed", "pipes_closed")):
                errors.append(identity["node"] + "." + identity["name"])
        if errors or set(self.report["closed_machines"]) != set(self.members):
            raise ValueError("native scene cleanup incomplete: " + repr(errors))
        self.report["cleanup_verified"] = True

    def verify_negative(self, primary):
        """An injected failure remains failed; this only checks its evidence."""
        report = self.report
        if (primary is None or not self.injected_failure
                or "failure_injected_ns" not in report):
            raise AssertionError("negative native scene did not reach its injected failure")
        row = report.get("before_cleanup", {})
        if (row.get("state") != "failed" or not row.get("failure_type")
                or not row.get("retry_rejected") or not row.get("effects_preserved")):
            raise AssertionError("negative native scene lacks frozen-state/non-replay evidence")
        if report["expected_mode"] == "interrupt" and not isinstance(primary, KeyboardInterrupt):
            raise AssertionError("native interrupt did not preserve KeyboardInterrupt")
        if not report.get("cleanup_verified"):
            raise AssertionError("negative native scene did not release its owned resources")
        report["negative_verified"] = {"mode": report["expected_mode"],
            "injected_ns": report["failure_injected_ns"], "error_type": type(primary).__name__,
            "retry_rejected": True, "effects_preserved": True}


def run_scene(document, output, *, qemu=None, profile="bare", host_timeout=20, firmware=None,
              expected_mode="normal", trace=False, use_cli=False):
    """Acquire through the builtin factories and publish only after cleanup."""
    from sim.physics.mujoco import MujocoPhysics
    from sim.machines.qemu.qmp import qmp_client_provenance
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("native scene refuses to overwrite an existing result directory")
    output.mkdir(parents=True)
    path = output / "scene.yaml"
    path.write_text(json.dumps(document, indent=2) + "\n")
    binary = resolve_qemu(qemu)
    registry = builtin_registry(qemu=binary, allow_host_processes=True)
    scene = load_scene(path, registry)
    members = [node.name for node in scene.topology.nodes if node.kind == "machine.qemu"]
    report = {"status": "running", "result": "failed", "schema": "sim/native-scene/1", "profile": profile,
        "qemu": {"path": str(binary), "sha256": sha(binary), "qmp_client": qmp_client_provenance()},
        "scene": str(path), "expected_mode": expected_mode, "duration_ns": scene.duration_ns, "quantum_ns": scene.quantum_ns,
        "commands": [], "wire_commands": [], "feedback": [], "wire_sends": [], "can_receipts": [],
        "faults": [], "referee": [], "world_results": [], "imu_witnesses": {name: [] for name in members}, "closed_machines": [], "cleanup": [], "watchdogs": {},
        "uart": {name: "" for name in members}, "cases": {name: [] for name in members},
        "summaries": {name: [] for name in members},
        "imu": {name: {"imu-input-accepted": 0, "imu-consumption": 0, "full_consumed": 0} for name in members},
        "known_deadlines": any(node.get("fault_profiles", {}).get("policy0", {}).get("delay_ns") == 250_000
                                for node in document["nodes"].values()),
        "cut_times_ns": [], "native_calls": {"instances": 0, "reset": 0, "step_ns": [], "close": 0}, "cuts": 0,
        "limitations": ["sampled outputs and transport receipts do not establish guest consumption",
            "native CPU cuts, CAN receipt and firmware/BSP timestamps remain distinct",
            "toy mechanics/physical calibration SKIP; no global CPU/World barrier or RTF claim"],
        "errors": [], "notes": [], "qmp_stats": {},
        "upper_exit_file": str(output / "exit-upper")}
    if scene.quantum_ns == 400_000:
        report["multi_cut"] = {"members": members, "events": []}
    inputs = [path, binary, Path(document["nodes"]["world"]["model"])]
    inputs += [Path(document["nodes"][name]["firmware"]) for name in members]
    report["inputs_before"] = {str(item): sha(item) for item in inputs}
    report["actual_sources_before"] = source_origins()
    if firmware is not None:
        from tools.dm_mc02_referee_test import checkout_snapshot, source_snapshot
        report["firmware_before"] = checkout_snapshot(firmware)
        report["firmware_sources_before"] = source_snapshot(inputs[-1], binary, firmware)
    observer = NativeObservation(report, members, profile)
    factory = registry["machine.qemu"]
    original_create = factory.create
    can_factory = registry["bus.can"]
    original_can = can_factory.create
    host_factory = registry["app.host-processes"]
    original_hosts = host_factory.create
    original_native = MujocoPhysics.from_xml_path
    started = time.monotonic()
    primary = result = None
    previous_signal = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"native scene interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with patch.object(factory, "create", observer.wrap_machine(original_create)), \
             patch.object(can_factory, "create", observer.capture_factory(original_can)), \
             patch.object(host_factory, "create", observer.capture_factory(original_hosts)), \
             patch.object(MujocoPhysics, "from_xml_path", observer.native(original_native)), \
             ((output / "observations.mcap").open("xb") if trace else nullcontext(None)) as trace_stream:
            if use_cli:
                from sim import cli, composition
                from sim.apps import experiment as application
                results = []
                def observed_run(*args, **options):
                    emitted = options.pop("on_event", None)
                    def observe(event):
                        if emitted is not None:
                            emitted(event)
                        observer.event(event)
                    value = run_experiment(*args, **options, on_event=observe, on_cut=observer.cut)
                    results.append(value)
                    return value
                with patch.object(composition, "builtin_registry", lambda **_options: registry), \
                     patch.object(application, "run_experiment", observed_run):
                    code = cli.main(["run", str(path), "--qemu", str(binary), "--timeout", str(host_timeout),
                        "--report", str(output / "cli-report.json"), "--allow-host-processes"])
                report["cli"] = {"exit_code": code, "report": json.loads((output / "cli-report.json").read_text())}
                if code or len(results) != 1:
                    if code == 130:
                        raise KeyboardInterrupt("canonical CLI interrupted")
                    raise RuntimeError("canonical sim run failed; inspect cli-report.json")
                result = results[0]
            else:
                result = run_experiment(scene, registry, host_timeout_s=host_timeout,
                    startup_timeout_s=min(10, host_timeout), on_event=observer.event, on_cut=observer.cut,
                    trace=trace_stream)
        report["runtime_result"] = plain(result)
        if not result.passed:
            raise ValueError("scene experiment assertion failure: " + repr(result.failures))
        if expected_mode != "normal":
            raise AssertionError("negative native scene completed without its failure")
        observer.verify(result)
    except BaseException as error:
        primary = error
    finally:
        report["host_elapsed_s"] = time.monotonic() - started
        signal.signal(signal.SIGTERM, previous_signal)
        for verify in (observer.cleanup,):
            try:
                verify()
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    primary.add_note(f"post-cleanup verification: {type(error).__name__}: {error}")
        if expected_mode != "normal":
            try:
                observer.verify_negative(primary)
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    primary.add_note(f"negative verification: {type(error).__name__}: {error}")
        try:
            report["inputs_after"] = {str(item): sha(item) for item in inputs}
            report["actual_sources_after"] = source_origins()
            if report["inputs_before"] != report["inputs_after"]:
                raise ValueError("scene/ELF/model/QEMU input changed through cleanup")
            if any(report["actual_sources_after"].get(name) != row for name, row in report["actual_sources_before"].items()):
                raise ValueError("executed sources changed through cleanup")
            if firmware is not None:
                report["firmware_after"] = checkout_snapshot(firmware)
                report["firmware_sources_after"] = source_snapshot(inputs[-1], binary, firmware)
                if report["firmware_before"] != report["firmware_after"] or report["firmware_sources_before"] != report["firmware_sources_after"]:
                    raise ValueError("firmware input sources changed through cleanup")
            report["source_preserved"] = True
        except BaseException as error:
            if primary is None:
                primary = error
            else:
                primary.add_note(f"input preservation verification: {type(error).__name__}: {error}")
        if primary is not None:
            report["errors"].append(f"{type(primary).__name__}: {primary}")
            report["error_type"] = type(primary).__name__
            report["notes"] = list(getattr(primary, "__notes__", ()))
        report["result"] = report["status"] = "passed" if primary is None else "failed"
        (output / "report.json").write_text(json.dumps(plain(report), indent=2, allow_nan=False) + "\n")
    print("RESULT: sim/v1 native " + profile + " " + report["status"], flush=True)
    if primary is not None:
        print(report["errors"][0], file=sys.stderr)
    return 0 if primary is None else 130 if isinstance(primary, KeyboardInterrupt) else 1


def dji_document(elf, *, feedback=False):
    marker = "TTEST READY dji." + ("feedback" if feedback else "scenario") + ".ready 0 0\n"
    document = four_wheel_document({"east": elf, "west": elf}, ROOT / "models/two_four_wheel.xml",
        ready={name: marker for name in ("east", "west")}, duration="400ms" if feedback else "1s")
    if feedback:
        for name in ("east", "west"):
            add_can_faults(document, name, feedback=((200_000_000, 1, False), (350_000_000, 1, True)))
    else:
        add_can_faults(document, "east", delays=((0, 5_000_000), (250_000_000, 0)))
    return document


def run_dji_scenario(elf, output, firmware, *, qemu=None):
    return run_scene(dji_document(elf), output, qemu=qemu, profile="dji-scenario", firmware=Path(firmware))


def run_dji_feedback_scenario(elf, output, firmware, *, qemu=None):
    return run_scene(dji_document(elf, feedback=True), output, qemu=qemu, profile="dji-feedback", firmware=Path(firmware))


def referee_document(elf, *, scenario=False):
    profiles = {name: referee_configuration(name) for name in ("east", "west")}
    for profile in profiles.values():
        profile.pop("uart")
    if scenario:
        profiles["east"]["events"] = [
            {"offset_ns": 200_000_000, "hit": {"damage_hp": 30}},
            {"offset_ns": 200_000_000, "shot": {"heat_17mm": 5, "heat_42mm": 7}},
            {"offset_ns": 200_000_000, "buff": {"recovery_percent": 8, "cooling_per_s": 17,
                "defence_percent": 19, "vulnerability_percent": 23, "attack_percent": 29, "remaining_energy": 0x80}}]
    return four_wheel_document({"east": elf, "west": elf}, ROOT / "models/two_four_wheel.xml",
        ready={name: "TTEST READY referee.arena.ready 0 0\n" for name in profiles},
        duration="1s", referees=profiles)


def run_referee(elf, output, firmware, *, qemu=None, scenario=False, cli=False):
    # CLI behavior is verified separately against the same serialized scene;
    # native receipts here remain obtained from real registered factory owners.
    return run_scene(referee_document(elf, scenario=scenario), output, qemu=qemu,
        profile="referee-scenario" if scenario else "referee", firmware=Path(firmware), use_cli=cli)

def check_can_deadline_observations(report):
    """Validate actual public events; literal reports in tests are not native evidence."""
    cuts = report["cut_times_ns"]
    if 1_250_000 not in cuts or len(cuts) <= 51:
        raise ValueError("CAN-deadlines lacks the actual1.25ms execution cut")
    delayed = [row for row in report["feedback"] if row["member"] == "east"]
    delivered = [row for row in delayed if not row["dropped"]]
    if not delivered or any(row["due_ns"] - row["source_ns"] != 250_000
                            or row["delivery_ns"] != row["due_ns"] for row in delivered):
        raise ValueError("CAN-deadlines source/due/delivery mismatch")
    networks = report["final_networks"]
    pending = networks["east_can"]
    end = report["runtime_result"]["end_ns"]
    if pending["pending"] != 4 or pending["next_due_ns"] != end + 250_000:
        raise ValueError("CAN-deadlines future feedback was flushed, hidden or lost")
    report["deadline_verified"] = {"first_due_ns": 1_250_000, "future_frames": 4,
        "next_due_ns": pending["next_due_ns"], "cuts": len(cuts) - 1,
        "delivered_feedback": len(report["feedback"])}


def check_multi_cut_observations(report):
    """Require actual raw STEP/ACK and fresh native receipts on every cut."""
    evidence, cuts = report["multi_cut"], report["cut_times_ns"]
    intervals = [b - a for a, b in zip(cuts, cuts[1:])]
    if (report["quantum_ns"] != 400_000 or not intervals
            or any(not 0 < dt <= 400_000 for dt in intervals)
            or report["native_calls"]["step_ns"] != intervals
            or report["runtime_result"]["end_ns"] != cuts[-1]):
        raise ValueError("multi-cut CPU/World intervals differ from the bounded400us cuts")
    events, members = evidence["events"], evidence["members"]
    origins = report["qemu"]["native_origins"]
    baselines, goals, sessions, unchanged = {}, {}, {}, 0
    for member in members:
        initial = [row for row in events if row["kind"] == "can-observation"
                   and row["member"] == member and row["cut_ns"] == 0]
        if not initial or initial[-1]["submitted_frames"]:
            raise ValueError("multi-cut lacks the original empty native CAN baseline")
        baseline = initial[-1]["state"]["fdcan-rx-state"]["controllers"]["dm-mc02.fdcan1"]["completed_frames"]
        baselines[member] = goals[member] = baseline
    for step_id, (target, dt) in enumerate(zip(cuts[1:], intervals), 1):
        world = [row for row in events if row["kind"] == "world-completed" and row["target_ns"] == target]
        if len(world) != 1 or world[0]["dt_ns"] != dt or abs(world[0]["native_time_s"] - target / 1e9) > 1e-12:
            raise ValueError("multi-cut requires one completed native World step per cut")
        acks = []
        for member in members:
            requests = [row for row in events if row["kind"] == "cpu-request" and row["member"] == member
                        and row["native_target_ns"] == origins[member] + target]
            sent = [row for row in events if row["kind"] == "step-send" and row["member"] == member
                    and row["header"]["step_id"] == step_id]
            accepted = [row for row in events if row["kind"] == "step-ack" and row["member"] == member
                        and row["header"]["step_id"] == step_id]
            if len(requests) != 1 or len(sent) != 1 or len(accepted) != 1:
                raise ValueError("multi-cut requires one real CPU request/STEP/ACK per peer and cut")
            sent, ack = sent[0], accepted[0]
            session = sessions.setdefault(member, sent["header"]["flags"])
            if type(session) is not int or not 0 < session <= 0xffffffff:
                raise ValueError("multi-cut requires a nonzero native wire session")
            for row, kind in ((sent, codec.STEP), (ack, codec.STEP_ACK)):
                header = row["header"]
                if tuple(header[key] for key in ("version", "kind", "flags", "step_id", "t_sim_ns", "dt_ns")) != (
                        2, kind, session, step_id, target, dt):
                    raise ValueError("multi-cut STEP/ACK session/cut/dt identity differs")
            if not requests[0]["order"] < world[0]["order"] < sent["order"] < ack["order"] or ack["payload"]["status"]:
                raise ValueError("multi-cut native CPU/World/STEP/ACK order or status differs")
            acks.append(ack["order"])
        for member in members:
            receipts = [row for row in events if row["kind"] == "can-observation" and row["member"] == member
                        and row["cut_ns"] == target and row["order"] > max(acks)]
            if not receipts:
                raise ValueError("multi-cut lacks a fresh native receipt after both ACKs")
            row = receipts[-1]
            state, native_target = row["state"], origins[member] + target
            execution, status = state["execution"], state["status"]
            rx_state = state["fdcan-rx-state"]
            rx = rx_state["controllers"]["dm-mc02.fdcan1"]
            goal = row["submitted_frames"] + baselines[member]
            if (execution.get("state") != "reached" or execution.get("current-ns") != native_target
                    or execution.get("target-ns") != native_target or status.get("running") is not False
                    or status.get("status") != "paused" or rx_state["timestamp_ns"] != native_target
                    or rx["completed_frames"] != goal or rx["partial_bytes"]
                    or state["fdcan-transports"]["dm-mc02.fdcan1"]["connected"] is not True
                    or goal < goals[member]):
                raise ValueError("multi-cut native target/receipt goal or connection differs")
            if target in (400_000, 800_000):
                if goal != goals[member] or row["submitted_frames"]:
                    raise ValueError("multi-cut early no-feedback receipt goal changed")
                unchanged += 1
            goals[member] = goal
    if unchanged != 2 * len(members):
        raise ValueError("multi-cut lacks both early unchanged receipt goals")
    evidence.update(verified=True, cuts=len(intervals), unchanged_receipt_goals=unchanged,
                    final_receipt_goals=goals, wire_sessions=sessions)


def bare_document(firmwares, *, two_chassis=True, arena_runtime=False, quantum="1ms", can_deadlines=False):
    members = ("east", "west")
    markers = {name: (f"ARENA_READY={index}\n" if arena_runtime else f"WORLD_READY={index}\n")
               for index, name in enumerate(members)}
    document = four_wheel_document(dict(zip(members, firmwares)), ROOT / "models/two_four_wheel.xml",
        ready=markers, quantum=quantum, duration="50ms")
    if not arena_runtime:
        document["experiment"]["inputs"] += [
            {"at": "0ns", "endpoint": name + ".key", "value": True, "origin": "session"} for name in members]
    for name, options in document["nodes"].items():
        if options["kind"] == "device.dji-m3508":
            options["feedback_period"] = "1ms"
    if can_deadlines:
        add_can_faults(document, "east", delays=((0, 250_000),))
    if not two_chassis:
        raise ValueError("use the two-chassis native scene; obsolete four-hinge manual-run fixture was removed")
    return document


def main(args):
    if args.can_deadlines and (args.multi_cut or args.cli or args.mode != "normal"):
        raise ValueError("--can-deadlines requires normal mode without --cli or --multi-cut")
    if args.multi_cut and (args.cli or args.mode != "normal"):
        raise ValueError("--multi-cut requires normal mode without --cli")
    target = Path(args.result).resolve()
    output = target.with_suffix("")
    quantum = "400us" if args.multi_cut else "1ms"
    document = bare_document(tuple(Path(path).resolve() for path in args.firmware),
                             arena_runtime=True, quantum=quantum, can_deadlines=args.can_deadlines)
    if args.mode == "upper-exit":
        exit_file = output / "exit-upper"
        document["nodes"]["upper"] = {"kind": "app.host-processes", "commands": {"observer": {
            "command": [sys.executable, "-c", "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
                        "exec('while not p.exists(): time.sleep(.001)')", str(exit_file)]}}}
    code = run_scene(document, output, qemu=args.qemu, profile="bare", host_timeout=15,
                     expected_mode=args.mode, trace=False, use_cli=args.cli)
    if target.exists():
        raise FileExistsError("native report refuses to overwrite an existing result")
    target.write_bytes((output / "report.json").read_bytes())
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu")
    parser.add_argument("--firmware", nargs=2, required=True)
    parser.add_argument("--mode", choices=("normal", "peer-exit", "reset", "upper-exit", "interrupt"), default="normal")
    parser.add_argument("--result", required=True)
    parser.add_argument("--multi-cut", action="store_true")
    parser.add_argument("--can-deadlines", action="store_true")
    parser.add_argument("--cli", action="store_true")
    raise SystemExit(main(parser.parse_args()))
