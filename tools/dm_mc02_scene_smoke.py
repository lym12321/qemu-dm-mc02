#!/usr/bin/env python3
"""Native application gates through the public sim/v1 experiment path.

Only bounded observers live here. Simulation owns every CPU target, world step,
connection delivery and teardown. UART reports supplement actual CAN/IMU bytes;
no host PASS text is injected and no global CPU/plant barrier is claimed.
"""
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict
from types import SimpleNamespace
import os
import yaml
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crccheck.crc import Crc8MaximDow, Crc16Mcrf4Xx
from smoke.support.experiment import execute_scene
from smoke.support.native_cli import regular_file, resolve_sim
from smoke.support.presets import four_wheel_document, add_can_faults


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_origins():
    result = {}
    for name, module in sorted(sys.modules.items()):
        if name == "__main__" or name.startswith(("smoke.", "tools.", "dm_mc02_")):
            value = getattr(module, "__file__", None)
            if value is None:
                continue
            path = Path(value).resolve()
            if path.is_file() and path.is_relative_to(ROOT):
                result[name] = {"path": str(path), "sha256": sha(path)}
    return result


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, SimpleNamespace):
        return plain(vars(value))
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
    """Independent assertions over completed public MCAP events."""

    def __init__(self, report, members, profile):
        self.report, self.members, self.profile = report, tuple(members), profile
        self.total = 0
        self.buffers = {name: bytearray() for name in members}
        self.ready, self.stages, self.done = {}, {}, set()
        self.controls = {name: {"scheduled_ns": stamp} for name, stamp in report["scheduled_controls"].items()}
        self.samples = {name: {} for name in members}

    def __call__(self, event):
        self.total += 1
        self.event(event)

    def event(self, event):
        name, kind, payload = event.source_node, event.kind, event.payload
        if kind == "uart" and name in self.members and event.source_port == "uart1":
            self.uart(name, payload, event)
        elif kind == "physics.state" and name == "world":
            bounded(self.report["cut_times_ns"], event.delivery_time_ns, 31251)
            if abs(payload["native_time_s"] - event.delivery_time_ns / 1e9) > 1e-12:
                raise ValueError("completed native world time differs from its public cut")
        elif kind == "can.sent" and "_motor_" in name:
            frame = payload["frame"]
            bounded(self.report["feedback_emitted"], {"member": name.split("_motor_")[0],
                "can_id": frame.can_id, "data_hex": frame.data.hex(),
                "source_ns": event.source_time_ns}, 20000)
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

    def verify(self, result):
        report = self.report
        cuts = report["cut_times_ns"]
        if not cuts or cuts[-1] != result.end_ns:
            raise ValueError("completed native world does not reach the logical endpoint")
        for name in self.members:
            if self.profile.startswith("dji-"):
                coverage = check_dji_scenario_coverage if self.profile == "dji-scenario" else check_dji_feedback_coverage
                coverage(report["cases"][name], report["summaries"][name])
            elif self.profile.startswith("referee"):
                check_referee_coverage(report["cases"][name], report["summaries"][name], name)
            feedback = [row for row in report["feedback"] if row["member"] == name and not row["dropped"]]
            if not feedback:
                raise ValueError("same-peer actual motor feedback is missing")
            origin = result.boards[name]["native_origin_ns"]
            receipts = [row for row in report["can_receipts"] if row["member"] == name]
            if not receipts or any(row["native_ns"] != origin + row["delivery_ns"] for row in receipts):
                raise ValueError("actual native CAN receipt epoch missing")
            for row in receipts:
                delivered = sum(item["delivery_ns"] <= row["delivery_ns"] for item in feedback)
                if row["receipt"]["completed_frames"] != delivered:
                    raise ValueError("native receipt count differs from prior same-peer deliveries")
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
            for name in self.members:
                if (self.samples[name].get("1", 250_000_000) >= 250_000_000
                        or self.stages.get(name, 250_000_000) > 250_000_000
                        or self.controls[name]["delivered_ns"] != 250_000_000
                        or self.samples[name].get("2", 0) <= 250_000_000):
                    raise ValueError("DJI250ms baseline/stage/control/second-sample order differs")
            east = [row for row in report["feedback"] if row["member"] == "east"]
            if not any(row["due_ns"] - row["source_ns"] == 5_000_000 for row in east):
                raise ValueError("delayed feedback evidence absent")
            if not any(row["source_ns"] > 250_000_000 and row["due_ns"] == row["source_ns"] for row in east):
                raise ValueError("restored feedback evidence absent")
        if self.profile == "referee-scenario":
            control = self.controls.get("west", {}).get("delivered_ns")
            if (control != 300_000_000 or self.stages.get("west", control + 1) >= control
                    or self.samples["east"].get("2", control + 1) >= control
                    or any(self.samples[name].get("1", SCENARIO_EVENT_NS) >= SCENARIO_EVENT_NS for name in self.members)
                    or self.samples["west"].get("2", 0) <= control):
                raise ValueError("referee event/east sample/west control order differs")
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



def duration_ns(value):
    # Only smoke fixture data is read here; Rust independently admits the scene.
    for suffix, factor in (("ms", 1_000_000), ("us", 1000), ("ns", 1), ("s", 1_000_000_000)):
        if value.endswith(suffix):
            return int(value[:-len(suffix)]) * factor
    raise ValueError("unknown fixture duration")


def run_scene(document, output, *, qemu=None, profile="bare", host_timeout=20, firmware=None,
              expected_mode="normal", trace=False, use_cli=False, sim=None):
    """Run the supplied Rust CLI; publish only after its process cleanup."""
    if expected_mode != "normal":
        raise ValueError("private Python failure injection retired; use Rust machine/process/physics failure gates")
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("native scene refuses to overwrite an existing result directory")
    output.mkdir(parents=True)
    binary = regular_file(qemu or os.environ.get("QEMU_SYSTEM_ARM", ROOT / "build/qemu/qemu-system-arm"), "QEMU", executable=True)
    executable = resolve_sim(sim)
    members = [name for name, node in document["nodes"].items() if node["kind"] == "machine.qemu"]
    controls = {name: 250_000_000 for name in members} if profile == "dji-scenario" else (
        {"west": 300_000_000} if profile == "referee-scenario" else {})
    for name, stamp in controls.items():
        document["experiment"]["inputs"].append({"at": str(stamp) + "ns", "endpoint": name + ".uart1",
            "value": b"\xa5", "origin": "session"})
    path = output / "scene.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    report = {"status": "running", "result": "failed", "schema": "sim/native-scene/2", "profile": profile,
        "qemu": {"path": str(binary), "sha256": sha(binary)},
        "sim": {"path": str(executable), "sha256": sha(executable)},
        "scene": str(path), "expected_mode": expected_mode,
        "duration_ns": duration_ns(document["runtime"]["duration"]), "quantum_ns": duration_ns(document["runtime"]["quantum"]),
        "commands": [], "feedback": [], "feedback_emitted": [], "can_receipts": [],
        "faults": [], "referee": [], "world_results": [], "imu_witnesses": {name: [] for name in members},
        "scheduled_controls": controls,
        "uart": {name: "" for name in members}, "cases": {name: [] for name in members},
        "summaries": {name: [] for name in members},
        "imu": {name: {"imu-input-accepted": 0, "imu-consumption": 0, "full_consumed": 0} for name in members},
        "known_deadlines": any(node.get("fault_profiles", {}).get("policy0", {}).get("delay_ns") == 250_000
                                for node in document["nodes"].values()),
        "cut_times_ns": [],
        "retired_private_instrumentation": {
            "solver_reset_close_counts": "physics_simulation::actual_shared_mujoco_and_can_devices_split_union_expiries_once_then_feedback",
            "raw_STEP_ACK_and_cpu_requests": "machine_imu::origin_is_observation_only_then_adc_and_si_share_one_step_and_done_is_distinct",
            "failure_injection": "machine::interrupted_or_wrong_target_freezes_last_committed_time_and_cleanup_still_reaps; physics_simulation::native_solver_failure_freezes_simulation_at_last_commit_with_original_error",
            "private_stream_watchdog_probes": "firmware TTEST reset/watchdog assertions and separate native firmware runner"},
        "limitations": ["sampled outputs and transport receipts do not establish guest consumption",
            "native CPU cuts, CAN receipt and firmware/BSP timestamps remain distinct",
            "toy mechanics/physical calibration SKIP; no global CPU/World barrier or RTF claim"],
        "errors": [], "notes": []}
    inputs = [path, binary, executable, Path(document["nodes"]["world"]["model"])]
    inputs += [Path(document["nodes"][name]["firmware"]) for name in members]
    report["inputs_before"] = {str(item): sha(item) for item in inputs}
    report["actual_sources_before"] = source_origins()
    if firmware is not None:
        from tools.dm_mc02_referee_test import checkout_snapshot, source_snapshot
        report["firmware_before"] = checkout_snapshot(firmware)
        report["firmware_sources_before"] = source_snapshot(inputs[-1], binary, firmware)
    observer = NativeObservation(report, members, profile)
    started = time.monotonic()
    primary = None
    try:
        _, result, command = execute_scene(path, qemu=binary, sim=executable,
            timeout=host_timeout, allow_host_processes=True, measure=True, observations=observer)
        report["command"] = command
        report["runtime_result"] = plain(result)
        report["cleanup_verified"] = command["cleanup_completed"]
        actual = sorted(row["firmware"]["path"] for row in command["resources"])
        expected = sorted(str(Path(document["nodes"][name]["firmware"]).resolve()) for name in members)
        if actual != expected:
            raise ValueError("actual native kernel argv differs from admitted firmware")
        if not result.passed:
            raise ValueError("scene experiment assertion failure: " + repr(result.failures))
        observer.verify(result)
    except BaseException as error:
        primary = error
        report.update(getattr(error, "native_observations", {}))
    finally:
        report["host_elapsed_s"] = time.monotonic() - started
        try:
            report["inputs_after"] = {str(item): sha(item) for item in inputs}
            if report["inputs_before"] != report["inputs_after"]:
                raise ValueError("scene/ELF/model/QEMU/Rust input changed through cleanup")
            if firmware is not None:
                report["firmware_after"] = checkout_snapshot(firmware)
                report["firmware_sources_after"] = source_snapshot(inputs[-1], binary, firmware)
                # The concurrent host checkout contains unrelated, authorized
                # work. Consumed host files are checked below by exact hashes.
                firmware_before = {key: value for key, value in report["firmware_before"].items() if key != "project"}
                firmware_after = {key: value for key, value in report["firmware_after"].items() if key != "project"}
                report["unconsumed_project_metadata_changed"] = report["firmware_before"].get("project") != report["firmware_after"].get("project")
                if firmware_before != firmware_after or report["firmware_sources_before"] != report["firmware_sources_after"]:
                    raise ValueError("firmware input sources changed through cleanup")
            report["source_preserved"] = True
            report["actual_sources_after"] = source_origins()
            if any(report["actual_sources_after"].get(name) != row for name, row in report["actual_sources_before"].items()):
                raise ValueError("executed smoke source changed through cleanup")
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
    end = report["runtime_result"]["end_ns"]
    future = [row for row in report["feedback_emitted"] if row["member"] == "east" and row["source_ns"] == end]
    if (len(future) != 4 or {row["can_id"] for row in future} != set(range(0x201, 0x205))
            or any(row["source_ns"] >= end for row in delivered)):
        raise ValueError("CAN-deadlines future emission/delivery ledger differs")
    report["deadline_verified"] = {"first_due_ns": 1_250_000, "future_frames": len(future),
        "next_expected_due_ns": end + 250_000, "cuts": len(cuts) - 1,
        "delivered_feedback": len(delivered), "future_evidence": "four public emissions at final cut; no premature delivery"}


def check_multi_cut_observations(report):
    cuts = report["cut_times_ns"]
    intervals = [b - a for a, b in zip(cuts, cuts[1:])]
    if (not intervals or any(not 0 < dt <= 400_000 for dt in intervals)
            or report["runtime_result"]["end_ns"] != cuts[-1]):
        raise ValueError("completed native World cuts exceed400us or miss final endpoint")
    report["public_multi_cut"] = {"cuts": len(intervals), "maximum_interval_ns": max(intervals),
        "private_STEP_ACK_proof": "retired; see Rust machine_imu gate"}


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
    if args.can_deadlines and (args.multi_cut or args.mode != "normal"):
        raise ValueError("--can-deadlines requires normal mode without --cli or --multi-cut")
    if args.multi_cut and args.mode != "normal":
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
                     expected_mode=args.mode, use_cli=args.cli, sim=args.sim)
    if target.exists():
        raise FileExistsError("native report refuses to overwrite an existing result")
    target.write_bytes((output / "report.json").read_bytes())
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu")
    parser.add_argument("--sim", help="compiled Rust sim executable (or SIM_EXECUTABLE)")
    parser.add_argument("--firmware", nargs=2, required=True)
    parser.add_argument("--mode", choices=("normal", "peer-exit", "reset", "upper-exit", "interrupt"), default="normal")
    parser.add_argument("--result", required=True)
    parser.add_argument("--multi-cut", action="store_true")
    parser.add_argument("--can-deadlines", action="store_true")
    parser.add_argument("--cli", action="store_true")
    raise SystemExit(main(parser.parse_args()))
