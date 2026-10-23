#!/usr/bin/env python3
"""Actual ELF acceptance through the single public Simulation execution path."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES = ("minimal-board", "can-motor")
EXAMPLE_ALIASES = {"pulse": "minimal-board", "dual-board": "can-motor",
                   "digital-link": "minimal-board", "rotor-loop": "can-motor",
                   "joint-loop": "can-motor"}
NATIVE_CASES = (*CASES, "pulse", "digital-link", "rotor-loop")


class Blocked(RuntimeError):
    """An explicitly required external build/native dependency is absent."""


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    return value


class Observations:
    """Bound actual events; keep native source clocks separate from delivery."""

    def __init__(self):
        self.total = 0
        self.uart = bytearray()
        self.peer_uart = bytearray()
        self.snapshots = []
        self.can = []
        self.deliveries = []
        self.receipts = []
        self.inputs = []
        self.uart_batches = []
        self.digital = []
        self.digital_consumption = []
        self.rotor_state = None
        self.rotor_consumption = None
        self.physics_states = []
        self.joint_consumption = None
        self.imu = []
        self.imu_readbacks = []
        self.last_sequence = -1

    def __call__(self, event):
        self.total += 1
        if self.total > 8192:
            raise BufferError("example observation event budget exhausted")
        if event.sequence <= self.last_sequence:
            raise ValueError("observation sequence did not increase")
        self.last_sequence = event.sequence
        stamp = {"event_sequence": event.sequence, "source_time_ns": event.source_time_ns,
                 "mapped_time_ns": event.mapped_time_ns,
                 "delivery_time_ns": event.delivery_time_ns,
                 "quality": str(event.quality)}
        if event.kind == "uart" and event.source_node in ("main", "peer") and event.source_port == "uart1":
            if type(event.payload) is not bytes:
                raise TypeError("native UART event payload must be bytes")
            target = self.uart if event.source_node == "main" else self.peer_uart
            if len(target) + len(event.payload) > 16384:
                raise BufferError("example UART observation budget exhausted")
            target.extend(event.payload)
            self.uart_batches.append({**stamp, "source": event.source_node,
                                      "data_hex": event.payload.hex()})
        elif event.kind == "snapshot" and event.source_node in ("main", "peer"):
            # Reuse the public hardware projection. Copying this frozen public
            # value changes neither the native observation nor its meaning.
            from sim.boards import digital_level

            snapshot = _plain(event.payload)
            self.snapshots.append({**stamp, "source": event.source_node,
                "native_timestamp_ns": snapshot["timestamp_ns"],
                "key": digital_level("gpio.A15", snapshot),
                "b0": digital_level("gpio.B0", snapshot),
                "b0_output": bool(snapshot["gpio"]["B"]["output_mask"] & 1)})
        elif event.kind in ("net.digital.sample", "net.digital.delivery"):
            self.digital.append({**stamp, "source": event.source_node,
                "endpoint": event.source_port, "kind": event.kind, "payload": _plain(event.payload)})
        elif event.kind == "input.delivered":
            self.inputs.append({**stamp, "source": event.source_node,
                                "endpoint": event.source_port, "value": _plain(event.payload)})
        elif event.kind == "can-input-processed":
            self.receipts.append({**stamp, "source": event.source_node,
                                  "endpoint": event.source_port,
                                  "payload": _plain(event.payload)})
        elif event.kind == "can.delivery":
            self.deliveries.append({**stamp, "source": event.source_node,
                "endpoint": event.source_port, "destination": event.payload["destination"],
                "can_id": event.payload["frame"].can_id,
                "data": event.payload["frame"].data.hex(), "dropped": event.payload["dropped"]})
        elif event.kind == "physics.state" and event.source_node == "world":
            state = {**stamp, "source": event.source_node,
                "endpoint": event.source_port, "native_time_s": event.payload["native_time_s"],
                "joints": {name: {"position_rad": state.position_rad,
                                   "velocity_rad_s": state.velocity_rad_s}
                           for name, state in event.payload["joints"].items()}}
            if "imus" in event.payload:
                state["imus"] = _plain(event.payload["imus"])
            self.physics_states.append(state)
        elif (event.kind.startswith("net.imu.") or event.kind.startswith("imu-")):
            self.imu.append({**stamp, "source": event.source_node,
                "endpoint": event.source_port, "kind": event.kind, "payload": _plain(event.payload)})
        elif event.kind == "can-tx" or (event.kind == "can.sent" and event.source_node in ("motor0", "motor1")):
            frame = event.payload if event.kind == "can-tx" else event.payload["frame"]
            self.can.append({**stamp, "source": event.source_node,
                "endpoint": event.source_port, "kind": event.kind,
                "can_id": frame.can_id, "flags": frame.flags, "dlc": frame.dlc,
                "data": frame.data.hex(), "wire_timestamp_ns": frame.timestamp_ns})

    def verify(self, case):
        uart = bytes(self.uart)
        if b"FAIL" in uart:
            raise AssertionError(f"guest reported failure: {uart!r}")
        ready = f"SIM_READY {EXAMPLE_ALIASES.get(case, case)}\n".encode()
        if case != "hal-gpio" and ready not in uart:
            raise AssertionError(f"actual guest UART ready is missing: {uart!r}")
        if not self.snapshots:
            raise AssertionError("no actual native board snapshot was observed")
        if case == "rotor-loop":
            self._verify_rotor(ready)
            return self.report()
        if case == "joint-loop":
            self._verify_joints(ready)
            return self.report()
        if case == "imu-readback":
            self._verify_imu(ready)
            return self.report()
        if case in ("minimal-board", "hal-gpio", "pulse", "digital-link"):
            if case in ("minimal-board", "pulse", "digital-link"):
                required = (b"KEY released B0=0\n", b"KEY pressed B0=1\n",
                            b"KEY released B0=0\n")
                start = uart.index(ready) + len(ready)
                for pattern in required:
                    found = uart.find(pattern, start)
                    if found < 0:
                        raise AssertionError(f"ordered guest UART marker missing: {pattern!r}")
                    start = found + len(pattern)
            if case == "pulse":
                self._verify_pulse()
                return self.report()
            if case == "digital-link":
                self._verify_digital(ready)
                return self.report()
            for lower, upper, key, b0 in ((0, 40_000_000, 1, 0),
                    (40_000_000, 90_000_000, 0, 1), (90_000_000, 200_000_001, 1, 0)):
                if not any(lower <= item["mapped_time_ns"] < upper
                           and item["b0_output"] and (item["key"], item["b0"]) == (key, b0)
                           for item in self.snapshots):
                    raise AssertionError(f"native GPIO observation missing in {lower}..{upper}: {(key, b0)}")
        else:
            active = b"MOTOR_ACTIVE current=64 encoder=0 rpm=0\n"
            timeout = b"MOTOR_TIMEOUT current=0\nMOTOR_PASS\n"
            if active not in uart or timeout not in uart or uart.index(active) >= uart.index(timeout):
                raise AssertionError(f"guest active/timeout/PASS evidence missing: {uart!r}")
            commands = [item for item in self.can if item["source"] == "main"
                        and item["endpoint"] == "fdcan1" and item["can_id"] == 0x200]
            if len(commands) != 1 or commands[0]["data"] != "0040000000000000":
                raise AssertionError(f"expected one actual guest current command: {commands!r}")
            if commands[0]["source_time_ns"] is None:
                raise AssertionError("guest CAN source timestamp was discarded")
            if (commands[0]["source_time_ns"] != commands[0]["wire_timestamp_ns"]
                    or commands[0]["mapped_time_ns"] > commands[0]["delivery_time_ns"]):
                raise AssertionError("guest CAN source time and completed cut disagree")
            feedback = [item for item in self.can if item["source"] == "motor0"
                        and item["endpoint"] == "can" and item["can_id"] == 0x201]
            if any(item["flags"] != 0 or item["dlc"] != 8 or item["data"] not in
                   ("0000000000401900", "0000000000001900") for item in feedback):
                raise AssertionError("feedback contains unexpected fixed-rotor fields")
            active_rows = [item for item in feedback if item["data"] == "0000000000401900"]
            deliveries = [item for item in self.deliveries if item["source"] == "main"
                          and item["destination"] == "motor0.can" and item["can_id"] == 0x200
                          and not item["dropped"]]
            if len(deliveries) != 1:
                raise AssertionError("the actual current command was not delivered once to motor0.can")
            expiry = deliveries[0]["delivery_time_ns"] + 100_000_000
            expired_rows = [item for item in feedback if item["data"] == "0000000000001900"
                            and item["mapped_time_ns"] >= expiry]
            if not active_rows or not expired_rows:
                raise AssertionError("actual feedback active/expired phases are missing")
            if any(item["mapped_time_ns"] < deliveries[0]["delivery_time_ns"]
                   or item["mapped_time_ns"] >= expiry for item in active_rows):
                raise AssertionError("feedback used a command before its source interval")
            if (not self.receipts or not any(item["payload"]["completed_frames"] > 0
                                            for item in self.receipts)):
                raise AssertionError("native CAN input completion receipt is missing")
            if case == "dual-board":
                peer_uart = bytes(self.peer_uart)
                if (ready not in peer_uart or active not in peer_uart or timeout not in peer_uart
                        or b"FAIL" in peer_uart or peer_uart.index(active) >= peer_uart.index(timeout)):
                    raise AssertionError("second real guest lacks active/timeout/PASS UART evidence")
                peer_commands = [item for item in self.can if item["source"] == "peer"
                    and item["endpoint"] == "fdcan1" and item["can_id"] == 0x200]
                if len(peer_commands) != 1 or peer_commands[0]["data"] != "0040000000000000":
                    raise AssertionError("second real machine did not emit its own CAN command")
                for source, destination in (("main", "peer.fdcan1"), ("peer", "main.fdcan1")):
                    rows = [item for item in self.deliveries if item["source"] == source
                            and item["destination"] == destination and item["can_id"] == 0x200]
                    command = commands[0] if source == "main" else peer_commands[0]
                    if (len(rows) != 1 or rows[0]["dropped"]
                            or rows[0]["delivery_time_ns"] != command["delivery_time_ns"]
                            or rows[0]["delivery_time_ns"] <= command["mapped_time_ns"]):
                        raise AssertionError("inter-machine CAN was not delivered at the next completed cut")
                for source in ("main", "peer"):
                    receipts = [item for item in self.receipts if item["source"] == source]
                    if (not receipts or receipts[-1]["payload"]["completed_frames"] != 201
                            or receipts[-1]["payload"]["rx_dropped"] != 1
                            or receipts[-1]["payload"]["partial_bytes"] != 0):
                        raise AssertionError("peer command filter rejection and complete feedback receipts disagree")
        return self.report()

    def _verify_rotor(self, ready):
        uart = bytes(self.uart)
        cursor = uart.index(ready) + len(ready)
        for marker in (b"ROTOR_MOVING rpm>=2\n",
                       b"ROTOR_COAST current=0 motion=1\nROTOR_PASS\n"):
            found = uart.find(marker, cursor)
            if found < 0:
                raise AssertionError("ordered guest rotor motion/coast/PASS evidence missing")
            cursor = found + len(marker)

        commands = [row for row in self.can if row["source"] == "main"
                    and row["endpoint"] == "fdcan1" and row["can_id"] == 0x200]
        if ([row["data"] for row in commands] != ["1000000000000000", "0000000000000000"]
                or any(row["flags"] or row["dlc"] != 8 for row in commands)):
            raise AssertionError("expected actual guest drive and zero commands")
        for row in commands:
            if (row["source_time_ns"] is None
                    or row["source_time_ns"] != row["wire_timestamp_ns"]
                    or row["mapped_time_ns"] != row["source_time_ns"]
                    or row["quality"] != "exact"):
                raise AssertionError("guest CAN source timestamp was discarded or changed")
        deliveries = [row for row in self.deliveries if row["source"] == "main"
                      and row["destination"] == "motor0.can" and row["can_id"] == 0x200]
        if ([row["data"] for row in deliveries] != [row["data"] for row in commands]
                or any(row["dropped"] for row in deliveries)):
            raise AssertionError("actual drive and zero commands were not delivered once")
        drive_ns, zero_ns = [row["delivery_time_ns"] for row in deliveries]
        if not drive_ns < zero_ns < drive_ns + 100_000_000:
            raise AssertionError("actual zero command must arrive before command expiry")
        for command, delivery in zip(commands, deliveries):
            if (delivery["source_time_ns"] != command["source_time_ns"]
                    or delivery["delivery_time_ns"] != command["delivery_time_ns"]
                    or delivery["event_sequence"] <= command["event_sequence"]
                    or command["source_time_ns"] > delivery["delivery_time_ns"]):
                raise AssertionError("guest command source and completed delivery cut disagree")

        feedback = []
        for row in self.can:
            if row["source"] != "motor0" or row["endpoint"] != "can" or row["can_id"] != 0x201:
                continue
            raw = bytes.fromhex(row["data"])
            if (row["flags"] or row["dlc"] != 8 or len(raw) != 8 or raw[6:] != b"\x19\0"
                    or row["source_time_ns"] != row["mapped_time_ns"]
                    or row["wire_timestamp_ns"] != row["mapped_time_ns"]):
                raise AssertionError("unexpected rotor feedback fields or source time")
            encoder = int.from_bytes(raw[:2], "big")
            rpm = int.from_bytes(raw[2:4], "big", signed=True)
            current = int.from_bytes(raw[4:6], "big", signed=True)
            cut = row["mapped_time_ns"]
            # Feedback is completed before that cut's commands are delivered.
            expected_current = 4096 if drive_ns < cut <= zero_ns else 0
            if current != expected_current:
                raise AssertionError("command must affect only the next plant interval")
            if not 0 <= encoder < 8192 or rpm < 0:
                raise AssertionError("invalid positive rotor feedback observation")
            feedback.append((row, encoder, rpm, current))

        def delivered(row):
            matches = [item for item in self.deliveries
                       if item["source"] == "motor0" and item["endpoint"] == "can"
                       and item["destination"] == "main.fdcan1" and item["can_id"] == 0x201
                       and item["data"] == row["data"] and not item["dropped"]
                       and item["source_time_ns"] == row["source_time_ns"]
                       and item["delivery_time_ns"] == row["mapped_time_ns"]
                       and item["event_sequence"] > row["event_sequence"]]
            if len(matches) != 1:
                raise AssertionError("actual rotor feedback delivery to guest is missing")
            delivery = matches[0]
            receipts = [item for item in self.receipts
                        if item["source"] == "main" and item["endpoint"] == "fdcan1"
                        and item["mapped_time_ns"] == delivery["delivery_time_ns"]
                        and item["event_sequence"] > delivery["event_sequence"]
                        and item["payload"]["completed_frames"] > 0
                        and item["payload"]["rx_dropped"] == 0
                        and item["payload"]["partial_bytes"] == 0]
            if len(receipts) != 1:
                raise AssertionError("matching native CAN input completion receipt is missing")
            return delivery, receipts[0]

        moving = [row for row, encoder, rpm, current in feedback
                  if encoder > 0 and rpm >= 2 and current == 4096
                  and row["mapped_time_ns"] < commands[1]["source_time_ns"]]
        if not moving:
            raise AssertionError("moving feedback lacks a later actual guest zero command")
        motion_delivery, motion_receipt = delivered(moving[0])
        if motion_receipt["event_sequence"] >= commands[1]["event_sequence"]:
            raise AssertionError("moving feedback lacks a later actual guest zero command")
        coast = [row for row, encoder, rpm, current in feedback
                 if encoder > 0 and rpm > 0 and current == 0 and row["mapped_time_ns"] > zero_ns]
        if not coast:
            raise AssertionError("actual zero-current moving feedback is missing")
        coast_delivery, coast_receipt = delivered(coast[0])
        if coast_receipt["payload"]["completed_frames"] <= motion_receipt["payload"]["completed_frames"]:
            raise AssertionError("native CAN input completion counter did not advance for coast")
        observed = 0
        coast_bound = None
        for batch in self.uart_batches:
            if batch["source"] != "main":
                continue
            observed += len(bytes.fromhex(batch["data_hex"]))
            if observed >= cursor:
                coast_bound = batch
                break
        if (coast_bound is None or coast_bound["quality"] != "upper_bound"
                or coast_bound["mapped_time_ns"] <= coast_delivery["delivery_time_ns"]
                or coast_bound["event_sequence"] <= coast_receipt["event_sequence"]):
            raise AssertionError("guest coast/PASS lacks a later UART observation upper bound")

        state = self.rotor_state
        if state is None or state["time_ns"] != 80_000_000:
            raise AssertionError("completed public rotor state at 80ms is missing")
        end_ns = state["time_ns"]
        if not 0 < drive_ns < zero_ns < end_ns or any(
                value % 1_000_000 for value in (drive_ns, zero_ns, end_ns)):
            raise AssertionError("rotor fixture requires complete 1ms integration intervals")
        # Independent closed form of the declared 1ms half-implicit Euler
        # approximation (J=.02, damping=.08, kt=.02, C620 raw4096=5A).
        # The start of torque is the delivery cut, never the source timestamp.
        n, m = (zero_ns - drive_ns) // 1_000_000, (end_ns - zero_ns) // 1_000_000
        r, v_inf = .996, 1.25
        velocity = v_inf * (1 - r ** n)
        expected_position = (.001 * v_inf * (n - r * (1 - r ** n) / (1 - r))
                             + .001 * velocity * r * (1 - r ** m) / (1 - r))
        expected_velocity = velocity * r ** m
        if (not math.isclose(state["position_rad"], expected_position, rel_tol=0, abs_tol=1e-12)
                or not math.isclose(state["velocity_rad_s"], expected_velocity, rel_tol=0, abs_tol=1e-12)):
            raise AssertionError("completed state disagrees with independent closed-form rotor")
        self.rotor_consumption = {
            "command_source_ns": [row["source_time_ns"] for row in commands],
            "command_delivery_ns": [drive_ns, zero_ns],
            "moving_feedback_delivery_ns": motion_delivery["delivery_time_ns"],
            "moving_receipt_sequence": motion_receipt["event_sequence"],
            "coast_feedback_delivery_ns": coast_delivery["delivery_time_ns"],
            "coast_receipt_sequence": coast_receipt["event_sequence"],
            "coast_uart_upper_bound_ns": coast_bound["mapped_time_ns"],
            "driven_intervals": n, "coast_intervals": m,
            "expected_position_rad": expected_position, "expected_velocity_rad_s": expected_velocity}

    def _verify_joints(self, ready):
        uart = bytes(self.uart)
        cursor = uart.index(ready) + len(ready)
        for marker in (b"JOINT_MOVING ids=1,2\n", b"JOINT_COAST ids=1,2\nJOINT_PASS\n"):
            found = uart.find(marker, cursor)
            if found < 0:
                raise AssertionError("ordered guest two-joint motion/coast/PASS evidence missing")
            cursor = found + len(marker)
        commands = [row for row in self.can if row["source"] == "main"
                    and row["endpoint"] == "fdcan1" and row["can_id"] == 0x200]
        if ([row["data"] for row in commands] != ["1000f00000000000", "0000000000000000"]
                or any(row["flags"] or row["dlc"] != 8 for row in commands)):
            raise AssertionError("expected actual guest signed drive and zero commands")
        for row in commands:
            if (row["source_time_ns"] is None or row["quality"] != "exact"
                    or row["source_time_ns"] != row["wire_timestamp_ns"]
                    or row["mapped_time_ns"] != row["source_time_ns"]):
                raise AssertionError("guest joint CAN source timestamp was discarded or changed")
        cuts = []
        for motor in ("motor0", "motor1"):
            deliveries = [row for row in self.deliveries if row["source"] == "main"
                          and row["destination"] == motor + ".can" and row["can_id"] == 0x200]
            if ([row["data"] for row in deliveries] != [row["data"] for row in commands]
                    or any(row["dropped"] for row in deliveries)):
                raise AssertionError(f"actual drive and zero commands were not delivered once to {motor}")
            for command, delivery in zip(commands, deliveries):
                if (delivery["source_time_ns"] != command["source_time_ns"]
                        or delivery["delivery_time_ns"] != command["delivery_time_ns"]
                        or delivery["event_sequence"] <= command["event_sequence"]
                        or command["source_time_ns"] > delivery["delivery_time_ns"]):
                    raise AssertionError("joint command source and completed delivery cut disagree")
            cuts.append(tuple(row["delivery_time_ns"] for row in deliveries))
        if cuts[0] != cuts[1]:
            raise AssertionError("one command batch must reach both motors at the same cut")
        drive_ns, zero_ns = cuts[0]
        end_ns = 80_000_000
        if (not 0 < drive_ns < zero_ns < end_ns
                or zero_ns >= drive_ns + 100_000_000
                or any(value % 1_000_000 for value in (drive_ns, zero_ns))):
            raise AssertionError("joint fixture requires drive/zero before expiry on complete 1ms cuts")

        # Independent literal mechanics: Izz=.02, kt=.02, raw4096=5A,
        # ideal DJI P19 ratio3591/187 and native velocity-then-position Euler.
        # Integration starts at actual command delivery, not CAN source time.
        gear = 3591 / 187

        def expected(cut):
            n = max(0, (min(cut, zero_ns) - drive_ns) // 1_000_000)
            m = max(0, (cut - zero_ns) // 1_000_000)
            return (5 * gear * .001 ** 2 * (n * (n + 1) / 2 + n * m),
                    5 * gear * .001 * n)

        if [row["mapped_time_ns"] for row in self.physics_states] != list(range(0, end_ns + 1, 1_000_000)):
            raise AssertionError("initial and every completed shared-world sample are required")
        for row in self.physics_states:
            cut = row["mapped_time_ns"]
            if (row["source"] != "world" or row["endpoint"] != "state"
                    or row["source_time_ns"] is not None or row["quality"] != "sampled"
                    or row["delivery_time_ns"] != cut
                    or not math.isclose(row["native_time_s"], cut / 1e9, rel_tol=0, abs_tol=1e-14)
                    or set(row["joints"]) != {"j1", "j2"}):
                raise AssertionError("shared-world sample lost native float/time provenance")
            position, velocity = expected(cut)
            for joint, sign in (("j1", 1), ("j2", -1)):
                state = row["joints"][joint]
                if (not math.isclose(state["position_rad"], sign * position, rel_tol=0, abs_tol=1e-12)
                        or not math.isclose(state["velocity_rad_s"], sign * velocity, rel_tol=0, abs_tol=1e-12)):
                    raise AssertionError("shared-world state disagrees with independent joint mechanics")

        native_deliveries = sorted((row for row in self.deliveries
            if row["source"] in ("motor0", "motor1") and row["endpoint"] == "can"
            and row["destination"] == "main.fdcan1" and not row["dropped"]),
            key=lambda row: row["event_sequence"])
        for motor in ("motor0", "motor1"):
            if sum(row["source"] == motor for row in native_deliveries) != 80:
                raise AssertionError(f"actual {motor} feedback delivery to guest is missing")
        native_receipts = sorted((row for row in self.receipts
            if row["source"] == "main" and row["endpoint"] == "fdcan1"),
            key=lambda row: row["event_sequence"])
        if len(native_receipts) != len(native_deliveries):
            raise AssertionError("matching native per-frame CAN input completion receipt is missing")
        receipt_by_delivery = {}
        # QemuMachine.receive waits for one additional completed frame, then
        # emits its receipt before returning to Fabric's next delivery. The
        # fresh guest starts at counter0; two frames at one cut have distinct
        # receipts and consecutive counters, never one combined receipt.
        for index, (delivery, receipt) in enumerate(zip(native_deliveries, native_receipts)):
            next_sequence = (native_deliveries[index + 1]["event_sequence"]
                             if index + 1 < len(native_deliveries) else math.inf)
            if (not delivery["event_sequence"] < receipt["event_sequence"] < next_sequence
                    or receipt["mapped_time_ns"] != delivery["delivery_time_ns"]
                    or receipt["payload"]["rx_dropped"] != 0
                    or receipt["payload"]["partial_bytes"] != 0):
                raise AssertionError("matching next native per-frame CAN input completion receipt is missing")
            if receipt["payload"]["completed_frames"] != index + 1:
                raise AssertionError("native per-frame completion counter must advance exactly once per delivery")
            receipt_by_delivery[delivery["event_sequence"]] = receipt

        def delivered(row):
            matches = [item for item in self.deliveries
                       if item["source"] == row["source"] and item["endpoint"] == "can"
                       and item["destination"] == "main.fdcan1" and item["can_id"] == row["can_id"]
                       and item["data"] == row["data"] and not item["dropped"]
                       and item["source_time_ns"] == row["source_time_ns"]
                       and item["delivery_time_ns"] == row["mapped_time_ns"]
                       and item["event_sequence"] > row["event_sequence"]]
            if len(matches) != 1:
                raise AssertionError(f"actual {row['source']} feedback delivery to guest is missing")
            delivery = matches[0]
            return delivery, receipt_by_delivery[delivery["event_sequence"]]

        evidence = {}
        for motor, can_id, sign in (("motor0", 0x201, 1), ("motor1", 0x202, -1)):
            feedback = [row for row in self.can if row["source"] == motor and row["endpoint"] == "can"]
            if [row["mapped_time_ns"] for row in feedback] != list(range(1_000_000, end_ns + 1, 1_000_000)):
                raise AssertionError(f"complete 1ms feedback from {motor} is missing")
            moving, coast = [], []
            for row in feedback:
                cut = row["mapped_time_ns"]
                position, velocity = expected(cut)
                rotor_position, rotor_velocity = sign * position * gear, sign * velocity * gear
                # DJI C620 V1.01 fields: unsigned wrapped8192 encoder,
                # signed nearest RPM/current, temperature25 and reserved0.
                encoder = math.floor((rotor_position % math.tau) / math.tau * 8192)
                rpm = round(rotor_velocity * 30 / math.pi)
                current = sign * 4096 if drive_ns < cut <= zero_ns else 0
                data = (encoder.to_bytes(2, "big") + rpm.to_bytes(2, "big", signed=True)
                        + current.to_bytes(2, "big", signed=True) + b"\x19\0")
                if (row["can_id"] != can_id or row["flags"] or row["dlc"] != 8
                        or row["data"] != data.hex() or row["quality"] != "exact"
                        or row["source_time_ns"] != cut or row["wire_timestamp_ns"] != cut):
                    raise AssertionError(f"{motor} feedback disagrees with independent signed joint wire fields")
                if encoder > 0 and sign * rpm >= 2 and sign * current == 4096 and cut < commands[1]["source_time_ns"]:
                    moving.append(row)
                if encoder > 0 and sign * rpm > 0 and current == 0 and cut > zero_ns:
                    coast.append(row)
            if not moving or not coast:
                raise AssertionError(f"{motor} lacks moving/coast feedback around the actual guest zero command")
            motion_delivery, motion_receipt = delivered(moving[0])
            coast_delivery, coast_receipt = delivered(coast[0])
            if motion_receipt["event_sequence"] >= commands[1]["event_sequence"]:
                raise AssertionError(f"{motor} moving receipt must precede the actual guest zero command")
            evidence[motor] = {"moving_delivery_ns": motion_delivery["delivery_time_ns"],
                "moving_receipt_sequence": motion_receipt["event_sequence"],
                "coast_delivery_ns": coast_delivery["delivery_time_ns"],
                "coast_receipt_sequence": coast_receipt["event_sequence"]}
        observed, coast_bound = 0, None
        for batch in self.uart_batches:
            if batch["source"] == "main":
                observed += len(bytes.fromhex(batch["data_hex"]))
                if observed >= cursor:
                    coast_bound = batch
                    break
        if (coast_bound is None or coast_bound["quality"] != "upper_bound"
                or any(coast_bound["mapped_time_ns"] <= row["coast_delivery_ns"]
                       or coast_bound["event_sequence"] <= row["coast_receipt_sequence"]
                       for row in evidence.values())):
            raise AssertionError("guest joint coast/PASS lacks a later UART observation upper bound")
        position, velocity = expected(end_ns)
        self.joint_consumption = {"command_source_ns": [row["source_time_ns"] for row in commands],
            "command_delivery_ns": [drive_ns, zero_ns], "motors": evidence,
            "coast_uart_upper_bound_ns": coast_bound["mapped_time_ns"],
            "driven_intervals": (zero_ns - drive_ns) // 1_000_000,
            "coast_intervals": (end_ns - zero_ns) // 1_000_000,
            "expected_j1_position_rad": position, "expected_j1_velocity_rad_s": velocity}

    def _verify_imu(self, ready):
        """Independent Bosch bins plus actual UART/consumption attribution."""
        cuts = list(range(0, 100_000_001, 10_000_000))
        if [row["mapped_time_ns"] for row in self.physics_states] != cuts:
            raise AssertionError("completed native IMU world cuts are missing")
        for row in self.physics_states:
            if (row["source_time_ns"] is not None or row["quality"] != "sampled"
                    or row["delivery_time_ns"] != row["mapped_time_ns"]
                    or not math.isclose(row["native_time_s"], row["mapped_time_ns"] / 1e9,
                                        rel_tol=0, abs_tol=1e-12)
                    or row["joints"] or set(row.get("imus", {})) != {"body"}):
                raise AssertionError("completed native IMU state provenance is invalid")
            sample = row["imus"]["body"]
            if (sample["gyro_rad_s"] != [0.0, 0.0, 0.0]
                    or sample["accel_m_s2"] != [0.0, 0.0, 9.81]):
                raise AssertionError("native rest SI IMU disagrees with the independent fixture")

        initial = [row for row in self.imu if row["kind"] == "imu-input-initial"]
        accepted = [row for row in self.imu if row["kind"] == "imu-input-accepted"]
        completed = [row for row in self.imu if row["kind"] == "imu-consumption"]
        if len(initial) != 1 or initial[0]["mapped_time_ns"] != 0:
            raise AssertionError("initial IMU observation must remain separate from positive STEP inputs")
        if [row["mapped_time_ns"] for row in accepted] != cuts[1:]:
            raise AssertionError("each positive completed IMU cut needs one native admission ACK")
        # Independent unit and float32 expectations, not the production
        # converter/codec. g0=9.80665 m/s² is the public SI conversion contract.
        expected_accel_g = struct.unpack("<f", struct.pack("<f", 9.81 / 9.80665))[0]
        by_identity = {}
        for row in [*initial, *accepted]:
            payload = row["payload"]
            sample = payload["input"]
            cut = row["mapped_time_ns"]
            if (row["source"] != "main" or row["endpoint"] != "imu"
                    or row["source_time_ns"] is not None or row["quality"] != "sampled"
                    or row["delivery_time_ns"] != cut or sample != {
                        "gyro_rad_s": [0.0, 0.0, 0.0], "accel_m_s2": [0.0, 0.0, 9.81],
                        "source_time_ns": None, "mapped_time_ns": cut}):
                raise AssertionError("IMU native admission lost the completed SI source provenance")
            if not cut:
                continue
            wire = payload["wire"]
            if (wire != {"gyro_dps": [0.0, 0.0, 0.0],
                         "accel_g": [0.0, 0.0, expected_accel_g],
                         "gyro_bias_dps": [0.0, 0.0, 0.0], "accel_bias_g": [0.0, 0.0, 0.0],
                         "sensor_time_ns": cut, "sample_id": 0}
                    or payload["status"] != 0 or payload["t_sim_ns"] != cut
                    or payload["dt_ns"] != 10_000_000):
                raise AssertionError("IMU actual float32 wire or native admission disagrees with the fixture")
            key = (payload["session_id"], payload["step_id"])
            if key in by_identity:
                raise AssertionError("duplicate IMU input identity")
            by_identity[key] = row

        done_by_identity = {}
        for row in completed:
            payload = row["payload"]
            key = (payload["session_id"], payload["step_id"])
            admission = by_identity.get(key)
            if (key in done_by_identity or admission is None
                    or row["event_sequence"] <= admission["event_sequence"]
                    or row["mapped_time_ns"] != admission["mapped_time_ns"]
                    or row["delivery_time_ns"] < row["mapped_time_ns"]
                    or row["source_time_ns"] is not None or row["quality"] != "sampled"
                    or any(payload[field] != admission["payload"][field]
                           for field in ("input", "wire", "t_sim_ns", "dt_ns"))):
                raise AssertionError("IMU terminal consumption lost its actual admitted identity")
            done_by_identity[key] = row

        uart = bytes(self.uart)
        matches = list(re.finditer(rb"^IMU_READ n=([12]) g=([0-9a-f]{12}) a=([0-9a-f]{12}) t=([0-9a-f]{6})\n",
                                   uart, re.MULTILINE))
        if ([int(match[1]) for match in matches] != [1, 2]
                or matches[0].start() < uart.index(ready) + len(ready)
                or uart.find(b"IMU_PASS\n", matches[-1].end()) < 0):
            raise AssertionError("two ordered actual guest IMU readbacks and PASS are required")
        bounds, length = [], 0
        for batch in self.uart_batches:
            if batch["source"] == "main":
                length += len(bytes.fromhex(batch["data_hex"]))
                bounds.append((length, batch))
        if length != len(uart):
            raise AssertionError("guest IMU UART bytes lack actual observation bounds")
        readbacks = []
        for match in matches:
            gyro = struct.unpack("<3h", bytes.fromhex(match[2].decode()))
            accel = struct.unpack("<3h", bytes.fromhex(match[3].decode()))
            sensor_ticks = int(match[4], 16)
            candidates = [row for row in accepted if
                (row["payload"]["wire"]["sensor_time_ns"] * 25600 // 1_000_000_000) & 0xffffff == sensor_ticks]
            if len(candidates) != 1:
                raise AssertionError("guest sensortime does not identify one actual admitted IMU sample")
            admission = candidates[0]
            key = (admission["payload"]["session_id"], admission["payload"]["step_id"])
            done = done_by_identity.get(key)
            if (done is None or done["payload"]["status"] != 0
                    or done["payload"]["consumed_mask"] != 3
                    or done["payload"]["missing_mask"] != 0):
                raise AssertionError("guest readback lacks matching full gyro/accel consumed DONE")
            if gyro != (0, 0, 0) or accel != (0, 0, 5463):
                raise AssertionError("guest raw IMU disagrees with independent rest values (accelZ=5463)")
            # Bosch Rev1.9 p39 section5.5.5 and p30 section5.3.11:
            # range1 gives +/-1000dps and +/-6g, signed16 half-LSB bins.
            for name, values, raw, scale in (("gyro", admission["payload"]["wire"]["gyro_dps"], gyro, 1000.0),
                                             ("accel", admission["payload"]["wire"]["accel_g"], accel, 6.0)):
                lsb = scale / 32768
                if any(abs(value - code * lsb) > lsb / 2 for value, code in zip(values, raw)):
                    raise AssertionError(f"guest {name} raw misses the actual float32 wire half-LSB bin")
            bound = next((batch for length, batch in bounds if length >= match.end()), None)
            if (bound is None or bound["quality"] != "upper_bound"
                    or bound["mapped_time_ns"] <= admission["mapped_time_ns"]
                    or bound["event_sequence"] <= done["event_sequence"]):
                raise AssertionError("guest final sensortime store lacks later actual UART publication")
            readbacks.append({"number": int(match[1]), "gyro_raw": list(gyro),
                "accel_raw": list(accel), "sensor_ticks_u24": sensor_ticks,
                "session_id": key[0], "step_id": key[1],
                "input_cut_ns": admission["mapped_time_ns"],
                "done_observed_cut_ns": done["delivery_time_ns"],
                "uart_upper_bound_ns": bound["mapped_time_ns"]})
        if readbacks[0]["sensor_ticks_u24"] >= readbacks[1]["sensor_ticks_u24"]:
            raise AssertionError("actual guest IMU sensor time must increase")
        self.imu_readbacks = readbacks

    def _verify_digital(self, ready):
        peer_uart = bytes(self.peer_uart)
        if ready not in peer_uart or b"FAIL" in peer_uart:
            raise AssertionError("peer digital firmware ready is missing or failed")
        if [(item["source"], item["endpoint"], item["value"], item["source_time_ns"],
             item["delivery_time_ns"]) for item in self.inputs] != [
                ("main", "key", True, 40_000_000, 40_000_000),
                ("main", "key", False, 90_000_000, 90_000_000)]:
            raise AssertionError("digital fixture must inject only main's scheduled key inputs")
        deliveries = [item for item in self.digital if item["kind"] == "net.digital.delivery"]
        if [item["payload"]["level"] for item in deliveries] != [0, 1, 0]:
            raise AssertionError("digital wire must deliver the three completed source phases")
        peer_batches = [item for item in self.uart_batches if item["source"] == "peer"]
        accumulated = b""
        bounds = []
        for batch in peer_batches:
            accumulated += bytes.fromhex(batch["data_hex"])
            bounds.append((len(accumulated), batch))
        if accumulated != peer_uart:
            raise AssertionError("peer UART bytes and observation bounds disagree")
        cursor = peer_uart.index(ready) + len(ready)
        self.digital_consumption = []
        for index, (delivery, marker) in enumerate(zip(deliveries, (
                b"KEY pressed B0=1\n", b"KEY released B0=0\n", b"KEY pressed B0=1\n"))):
            payload = delivery["payload"]
            cut, level = delivery["mapped_time_ns"], payload["level"]
            lower, upper = ((0, 40_000_000), (40_000_000, 90_000_000),
                            (90_000_000, 200_000_000))[index]
            source = [item for item in self.snapshots
                      if item["source"] == "main" and item["mapped_time_ns"] == cut]
            samples = [item for item in self.digital if item["kind"] == "net.digital.sample"
                       and item["mapped_time_ns"] == cut]
            if (delivery["source"] != "wire" or delivery["endpoint"] != "signal"
                    or payload["source"] != "main.gpio.B0.out" or payload["destination"] != "peer.key"
                    or payload["sequence"] != index or payload["due_ns"] != cut
                    or delivery["delivery_time_ns"] != cut or delivery["quality"] != "sampled"
                    or not lower < cut < upper or len(source) != 1 or len(samples) != 1
                    or not source[0]["b0_output"] or source[0]["b0"] != level
                    or delivery["source_time_ns"] != source[0]["native_timestamp_ns"]
                    or samples[0]["payload"] != {"source": "main.gpio.B0.out", "level": level,
                                                 "active": True, "changed": True}
                    or not source[0]["event_sequence"] < samples[0]["event_sequence"] < delivery["event_sequence"]):
                raise AssertionError("digital source provenance does not match its completed native sample")
            preceding = [item for item in self.snapshots
                         if item["source"] == "peer" and item["mapped_time_ns"] == cut]
            if (len(preceding) != 1 or preceding[0]["key"] != 1 - level
                    or preceding[0]["event_sequence"] >= delivery["event_sequence"]):
                raise AssertionError("digital delivery changed the preceding peer interval")
            responses = [item for item in self.snapshots
                         if item["source"] == "peer" and cut < item["mapped_time_ns"] < upper
                         and item["b0_output"] and (item["key"], item["b0"]) == (level, 1 - level)]
            if not responses:
                raise AssertionError("digital delivery lacks a later peer GPIO firmware response")
            found = peer_uart.find(marker, cursor)
            if found < 0:
                raise AssertionError(f"ordered peer UART marker missing: {marker!r}")
            cursor = found + len(marker)
            bound = next(batch for length, batch in bounds if length >= cursor)
            if (not cut < bound["mapped_time_ns"] < upper
                    or bound["event_sequence"] <= delivery["event_sequence"]):
                raise AssertionError("digital delivery lacks a later peer UART observation bound")
            self.digital_consumption.append({"level": level, "source_cut_ns": cut,
                "source_native_time_ns": delivery["source_time_ns"],
                "delivery_cut_ns": delivery["delivery_time_ns"],
                "peer_gpio_cut_ns": responses[0]["mapped_time_ns"],
                "peer_uart_upper_bound_ns": bound["mapped_time_ns"]})

    def _verify_pulse(self):
        inputs = [item for item in self.inputs
                  if item["source"] == "main" and item["endpoint"] == "key"]
        if [(item["value"], item["source_time_ns"], item["delivery_time_ns"])
                for item in inputs] != [(True, 3_000_000, 3_000_000),
                                       (False, 4_000_000, 4_000_000)]:
            raise AssertionError("key pulse was not delivered at its scheduled native cuts")
        # These are actual QEMU snapshots collected after completed native
        # runs, before that cut's inputs. B0 high at 4ms proves the guest acted
        # during the 3..4ms pulse, even though UART serialization finishes later.
        for cut, key, b0 in ((3_000_000, 1, 0), (4_000_000, 0, 1),
                             (10_000_000, 1, 0)):
            rows = [item for item in self.snapshots
                    if item["source"] == "main" and item["mapped_time_ns"] == cut]
            if (len(rows) != 1 or rows[0]["delivery_time_ns"] != cut
                    or rows[0]["native_timestamp_ns"] - self.snapshots[0]["native_timestamp_ns"] != cut
                    or rows[0]["quality"] != "exact"):
                raise AssertionError(f"actual native run cut is missing or inexact: {cut}")
            if not rows[0]["b0_output"] or (rows[0]["key"], rows[0]["b0"]) != (key, b0):
                raise AssertionError(f"guest GPIO response missing at pulse cut {cut}: {(key, b0)}")

    def report(self):
        return {"events": self.total,
                "uart": bytes(self.uart).decode("ascii", errors="backslashreplace"),
                "peer_uart": bytes(self.peer_uart).decode("ascii", errors="backslashreplace"),
                "snapshots": self.snapshots, "can": self.can,
                "deliveries": self.deliveries, "receipts": self.receipts,
                "inputs": self.inputs, "uart_batches": self.uart_batches,
                "digital": self.digital, "digital_consumption": self.digital_consumption,
                "rotor_state": self.rotor_state, "rotor_consumption": self.rotor_consumption,
                "physics_states": self.physics_states, "joint_consumption": self.joint_consumption,
                "imu": self.imu, "imu_readbacks": self.imu_readbacks,
                "imu_pending": self._imu_pending()}

    def _imu_pending(self):
        completed = {(row["payload"]["session_id"], row["payload"]["step_id"])
                     for row in self.imu if row["kind"] == "imu-consumption"}
        return [row for row in self.imu if row["kind"] == "imu-input-accepted"
                and (row["payload"]["session_id"], row["payload"]["step_id"]) not in completed]


def build_example(case, *, build_dir=None, build_type="Debug", imu_skip_gyro=False):
    for program in ("cmake", "arm-none-eabi-gcc"):
        if shutil.which(program) is None:
            raise Blocked(f"{program} is required to build the real example ELF")
    source = ROOT / "examples" / EXAMPLE_ALIASES.get(case, case)
    default_build = (ROOT / "build" / "simulation-examples" / "pulse" if case == "pulse"
                     else source / "build-rotor-loop" if case == "rotor-loop"
                     else source / "build-joint-loop" if case == "joint-loop" else source / "build")
    build = default_build if build_dir is None else Path(build_dir)
    variant = (["-DSIM_KEY_PULSE_TEST=ON"] if case == "pulse" else
               ["-DSIM_KEY_PULSE_TEST=OFF"] if source.name == "minimal-board" else [])
    if source.name == "can-motor":
        variant.append("-DSIM_ROTOR_LOOP_TEST=" + ("ON" if case == "rotor-loop" else "OFF"))
        variant.append("-DSIM_JOINT_LOOP_TEST=" + ("ON" if case == "joint-loop" else "OFF"))
    if source.name == "imu-readback":
        variant.append("-DSIM_IMU_SKIP_GYRO_TEST=" + ("ON" if imu_skip_gyro else "OFF"))
    commands = (["cmake", "-S", str(source), "-B", str(build), f"-DCMAKE_BUILD_TYPE={build_type}", *variant],
                ["cmake", "--build", str(build), "--parallel", "2"])
    logs = []
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
        logs.append({"command": command, "exit": result.returncode,
                     "output": (result.stdout + result.stderr)[-16000:]})
        if result.returncode:
            raise RuntimeError(f"example build failed: {logs[-1]}")
    elf = build / "firmware.elf"
    return {"elf": str(elf), "sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
            "build_type": build_type, "build": logs}


def run_case(case, *, qemu, backend_root=ROOT, firmware=None):
    from sim.composition import builtin_registry
    from sim.config.scene import load_scene
    from sim.runtime import Simulation
    from sim.machines.qemu.errors import BackendUnavailable

    observations = Observations()
    registry = builtin_registry(qemu=qemu, backend_root=backend_root)
    if case == "hal-gpio":
        from sim.api import NodeSpec, Topology
        from sim.config.scene import Scene

        scene = Scene(Topology((NodeSpec("main", "machine.qemu", {
            "model": "dm-mc02", "firmware": str(firmware), "uarts": ()}),), ()))
    else:
        example = "digital-link" if case == "digital-link" else EXAMPLE_ALIASES.get(case, case)
        scene = load_scene(ROOT / "examples" / example /
                           (case + ".yaml" if case in ("rotor-loop", "joint-loop") else "scene.yaml"), registry)
        if case == "imu-readback" and firmware is not None:
            from sim.api import NodeSpec, Topology
            from sim.config.scene import Scene

            nodes = tuple(NodeSpec(node.name, node.kind,
                {**node.options, "firmware": str(firmware)} if node.name == "main" else node.options,
                base_dir=node.base_dir) for node in scene.topology.nodes)
            scene = Scene(Topology(nodes, scene.topology.links), scene.mode,
                          scene.quantum_ns, scene.duration_ns)
        if case == "pulse":
            from sim.api import NodeSpec, Topology
            from sim.config.scene import Scene

            main = scene.topology.nodes[0]
            node = NodeSpec(main.name, main.kind, {**main.options, "firmware": str(firmware)},
                            base_dir=main.base_dir)
            scene = Scene(Topology((node,), scene.topology.links), scene.mode,
                          10_000_000, 20_000_000)
        if case == "dual-board":
            from sim.api import LinkSpec, NodeSpec, Topology
            from sim.config.scene import Scene

            main = next(node for node in scene.topology.nodes if node.name == "main")
            peer = NodeSpec("peer", main.kind, main.options, base_dir=main.base_dir)
            scene = Scene(Topology((*scene.topology.nodes, peer),
                (*scene.topology.links, LinkSpec("peer.fdcan1", "can0.can"))),
                scene.mode, scene.quantum_ns, scene.duration_ns)
    simulation = Simulation(scene.topology, registry, quantum_ns=scene.quantum_ns,
        mode=scene.mode, startup_timeout_s=10, host_timeout_s=30,
        on_event=observations)
    primary = None
    resources = []
    try:
        simulation.start()
        for name, component in simulation.components.items():
            info = getattr(component, "connection_info", None)
            if info is not None:
                pid = info["pid"]
                stat = Path(f"/proc/{pid}/stat")
                # Linux proc starttime distinguishes an unrelated reused PID.
                starttime = stat.read_text().rsplit(")", 1)[1].split()[19]
                resources.append({"name": name, "pid": pid, "starttime": starttime,
                                  "sockets": dict(info["sockets"])})
        if {item["name"] for item in resources} != (
                {"main", "peer"} if case in ("dual-board", "digital-link") else {"main"}):
            raise AssertionError("the real machine did not expose its native process identity")
        if case in ("minimal-board", "hal-gpio", "digital-link"):
            simulation.schedule_input("main.key", True, at_ns=40_000_000)
            simulation.schedule_input("main.key", False, at_ns=90_000_000)
        elif case == "pulse":
            simulation.schedule_input("main.key", True, at_ns=3_000_000)
            simulation.schedule_input("main.key", False, at_ns=4_000_000)
            # One public step must execute the interior cuts and still reach
            # its requested 10ms endpoint. Continue for the serialized UART.
            simulation.step(10_000_000)
        simulation.run_until(scene.duration_ns)
        if case == "rotor-loop":
            position, velocity = simulation.components["motor0"].get_rotor_state()
            observations.rotor_state = {"time_ns": simulation.t_sim_ns,
                "position_rad": position, "velocity_rad_s": velocity}
    except BaseException as error:
        if isinstance(error, BackendUnavailable):
            unavailable = error
            error = Blocked(str(unavailable))
            for note in getattr(unavailable, "__notes__", ()):
                error.add_note(note)
            error.simulation_observations = observations.report()
            primary = error
            raise error
        primary = error
        error.simulation_observations = observations.report()
        raise
    finally:
        try:
            simulation.close()
        except BaseException as error:
            if primary is None:
                error.simulation_observations = observations.report()
                raise
            primary.add_note(f"Simulation cleanup also failed: {error}")
    # No success or verification is published before all resource owners close.
    try:
        for resource in resources:
            stat = Path(f"/proc/{resource['pid']}/stat")
            if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[19] == resource["starttime"]:
                raise AssertionError(f"owned QEMU identity remains after cleanup: {resource['pid']}")
            if any(Path(path).exists() for path in resource["sockets"].values()):
                raise AssertionError("owned native socket path remains after cleanup")
        result = observations.verify(case)
        if str(simulation.state) != "closed":
            raise AssertionError("Simulation did not reach CLOSED after cleanup")
    except BaseException as error:
        error.simulation_observations = observations.report()
        raise
    result["cleanup_completed"] = True
    result["resources"] = resources
    result["state"] = str(simulation.state)
    return result


def _interrupted(signum, _frame):
    raise KeyboardInterrupt(f"interrupted by signal {signum}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=(*NATIVE_CASES, "hal-gpio", "dual-board", "joint-loop", "imu-readback", "all"), default="all")
    parser.add_argument("--build-type", choices=("Debug", "Release"), default="Debug")
    parser.add_argument("--hal-elf", type=Path,
                        default=ROOT / "build/teaching/Debug/gpio_key.elf",
                        help="existing HAL GPIO lesson ELF, used only by --case hal-gpio")
    parser.add_argument("--qemu", type=Path, default=Path(os.environ.get(
        "QEMU_SYSTEM_ARM", ROOT / "build/qemu/qemu-system-arm")))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report = args.report or ROOT / "build/test-results" / (
        "simulation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%f") +
        f"-{os.getpid()}") / "report.json"
    payload = {"status": "failed", "cases": {}, "hardware_calibration": "SKIP",
               "limits": "Sampled stepped execution; no global IRQ/plant barrier or physical calibration."}
    code = 1
    previous = signal.signal(signal.SIGTERM, _interrupted)
    try:
        if not args.qemu.is_file() or not os.access(args.qemu, os.X_OK):
            raise Blocked(f"project dm-mc02 QEMU executable is missing: {args.qemu}")
        payload["qemu"] = {"path": str(args.qemu.resolve()),
                           "sha256": hashlib.sha256(args.qemu.read_bytes()).hexdigest()}
        for case in NATIVE_CASES if args.case == "all" else (args.case,):
            if case == "hal-gpio":
                if not args.hal_elf.is_file():
                    raise Blocked(f"the existing HAL GPIO lesson ELF is missing: {args.hal_elf}")
                elf = args.hal_elf.resolve()
                payload["cases"][case] = {"elf": str(elf),
                    "sha256": hashlib.sha256(elf.read_bytes()).hexdigest()}
            else:
                payload["cases"][case] = build_example(case, build_type=args.build_type)
            payload["cases"][case]["observations"] = run_case(
                case, qemu=args.qemu, firmware=Path(payload["cases"][case]["elf"]))
            binary = Path(payload["cases"][case]["elf"])
            if hashlib.sha256(binary.read_bytes()).hexdigest() != payload["cases"][case]["sha256"]:
                raise AssertionError("firmware ELF changed during the native acceptance run")
        payload["status"] = "passed"
        code = 0
    except (Exception, KeyboardInterrupt) as error:
        payload.update(status="blocked" if isinstance(error, Blocked) else "failed",
                       error=str(error), error_type=type(error).__name__,
                       notes=list(getattr(error, "__notes__", ())))
        if hasattr(error, "simulation_observations"):
            payload["partial_observations"] = error.simulation_observations
        code = 78 if isinstance(error, Blocked) else 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("x", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, ensure_ascii=True)
            output.write("\n")
    except (OSError, KeyboardInterrupt) as error:
        print(f"error: report publication failed: {error}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(f"Simulation examples: {payload['status']} ({report})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
