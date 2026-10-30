#!/usr/bin/env python3
"""Actual trobot DJI driver consumer of the public CAN Fabric/M3508 boundary.

One Simulation owns the devices and time, with a 1 ms maximum interval and cuts
at known CAN delivery deadlines. Monotonic QEMU CAN timestamps bound catch-up;
transport timestamps do not synchronize the QEMU CPU to the plant.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import select
import signal
import socket
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from dm_mc02_qmp import QmpSession
from dm_mc02_sim_worker import CanStream, QemuTimestampMapper
from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Session, Topology
from sim.apps.can_worker import can_topology, create_can_simulation, motor_nodes
from sim.devices.dji.protocol import encode_feedback
from sim.fabric import CanFrame

STEP_NS = 1_000_000
MAX_CATCHUP_STEPS = 1000
MAX_RECORDS = 10_000
MOTOR_CONSUMER_REVISION = "30a4489d1fabf560f87d4740ea6e07b25faefb9f"
MOTOR_REFERENCE_HASHES = {
    "src/dji.cc": "f58dd3a12fcb37317d85dcccf7eb495795060b2310c81188eb48f3b3aea40d0f",
    "include/motor/dji.h": "54a0ba9a5dca29368c770b659e316abbc737cfc17d8d60dc85c8a4c0d1e0aa82",
}
STAGES = (
    ("dji.vector", 0x201, 0x205),
    ("dji.start", 0, 0),
    ("dji.positive", 4096, 0xFFFFF000),
    ("dji.negative", 0xFFFFF000, 4096),
    ("dji.disable", 0, 4096),
    ("dji.enable", 4096, 4096),
    ("dji.drop", 0x201, 50),
    ("dji.recover", 0x201, 0),
    ("dji.stop", 0, 0),
)
WIRE_TARGETS = {
    "dji.positive": (4096, -4096),
    "dji.negative": (-4096, 4096),
    "dji.disable": (0, 4096),
    "dji.enable": (4096, 4096),
    "dji.drop": (0, 4096),
    "dji.recover": (4096, 4096),
    "dji.stop": (0, 0),
}
REQUIRED = {
    "control.rx", "reset.poweron", "bsp.init", "task.stack",
    "dji.vector.id1", "dji.vector.id5", "dji.units.id1", "dji.units.id5",
    "dji.vector.disabled", "dji.online", "dji.positive.id1",
    "dji.positive.id5", "dji.positive.feedback", "dji.negative.id1",
    "dji.negative.id5", "dji.negative.feedback", "dji.disable.ignored",
    "dji.disable.id5", "dji.enable.id1", "dji.enable.id5", "dji.drop.frozen",
    "dji.drop.age", "dji.drop.app_age", "dji.drop.output", "dji.drop.id5",
    "dji.recover.feedback", "dji.recover.id1", "dji.recover.id5",
    "dji.stop", "dji.can",
}
EXACT_EXPECTED = {
    "control.rx": 0, "reset.poweron": 0x800000, "bsp.init": 0,
    "dji.vector.id1": 2048, "dji.vector.id5": 4096,
    "dji.units.id1": 0, "dji.units.id5": 0, "dji.vector.disabled": 0,
    "dji.online": 1, "dji.positive.id1": 4096,
    "dji.positive.id5": 0xFFFFF000, "dji.positive.feedback": 4096,
    "dji.negative.id1": 0xFFFFF000, "dji.negative.id5": 4096,
    "dji.negative.feedback": 0xFFFFF000, "dji.disable.ignored": 0,
    "dji.disable.id5": 4096, "dji.enable.id1": 4096, "dji.enable.id5": 4096,
    "dji.drop.output": 0, "dji.drop.id5": 4096,
    "dji.recover.feedback": 4096, "dji.recover.id1": 4096,
    "dji.recover.id5": 4096, "dji.stop": 0, "dji.can": 0,
}


def append_bounded(records: list, item: dict) -> None:
    if len(records) >= MAX_RECORDS:
        raise BufferError("DJI fixture observation capacity exhausted")
    records.append(item)


class VectorSource:
    """Test-only literal feedback producer, borrowing the normal bus route."""

    def __init__(self, context):
        self.connection = context.attach("can", self)

    def receive(self, frame: CanFrame) -> None:
        pass

    def input(self, port, frame):
        self.connection.send(frame)

    def close(self):
        pass


class VectorSourceFactory:
    @staticmethod
    def describe(spec):
        if spec.options:
            raise ValueError("literal vector source has no options")
        return NodeDescription((PortSpec("can", "can", min_connections=1),
            PortSpec("tx", "can-frame", "in", capabilities={"external_input"})),
            frozenset({"input"}))

    @staticmethod
    def validate_input(spec, port, payload, *, connected_ports):
        if port != "tx" or port in connected_ports:
            raise ValueError("literal vectors require an unconnected tx input")
        if not isinstance(payload, CanFrame):
            raise TypeError("literal vector requires a CanFrame")
        return payload

    @staticmethod
    def create(spec, context):
        return VectorSource(context)


class DjiFixture:
    def __init__(self, can: CanStream):
        self.stage = "startup"
        self.stage_index = 0
        self.phase_start_ns: dict[str, int] = {}
        self.previous_wire_ns = -1
        self.steps = 0
        self.trace: list[dict] = []
        self.deliveries: list[dict] = []
        self.wire_seen: dict[str, dict[int, set[int]]] = defaultdict(
            lambda: defaultdict(set))
        topology = can_topology(motor_nodes((1, 5)))
        topology = Topology((*topology.nodes, NodeSpec("literal-vector", "fixture.vector")),
                            (*topology.links, LinkSpec("literal-vector.can", "can.can")))
        # Standalone harness starts the QEMU epoch at zero. Production workers
        # instead bind the actual co-sim RESET origin to this same endpoint API.
        self.runtime = create_can_simulation(Session(1, 0), QemuTimestampMapper(0, 0),
            can, None, topology=topology, drop=self._drop, on_delivery=self._log,
            extra_factories={"fixture.vector": VectorSourceFactory()})
        self.deadline = time.monotonic() + 25
        self.runtime.start(deadline_monotonic=min(self.deadline, time.monotonic() + 2))
        self.endpoint = self.runtime.components["transport"].endpoint
        self.motors = tuple(self.runtime.components[f"motor{ident}"] for ident in (1, 5))
        self.vector = self.runtime.components["literal-vector"]

    def _drop(self, source, destination, frame: CanFrame) -> bool:
        if destination is not self.endpoint or frame.can_id not in (0x201, 0x205):
            return False
        if source is self.vector:
            return False
        return self.stage in ("startup", "dji.vector") or (
            self.stage == "dji.drop" and frame.can_id == 0x201)

    def _log(self, event) -> None:
        # The report proves firmware-facing feedback delivery. Real incoming
        # command bytes are already retained once in trace; broadcasts to the
        # two other virtual receivers add no independent boundary evidence.
        if event.destination != "transport.qemu":
            return
        append_bounded(self.deliveries, dict(
            phase=self.stage, source=event.source, destination=event.destination,
            can_id=event.frame.can_id, flags=event.frame.flags,
            dlc=event.frame.dlc, data=event.frame.data.hex(),
            original_time_ns=event.timestamp_ns, due_ns=event.due_ns,
            delivered_time_ns=event.delivered_ns, sequence=event.sequence,
            late=event.late, dropped=event.dropped))

    def _finish_stage(self) -> None:
        if self.stage in WIRE_TARGETS:
            for ident, value in zip((0x200, 0x1FF), WIRE_TARGETS[self.stage]):
                if value not in self.wire_seen[self.stage][ident]:
                    raise RuntimeError(
                        f"missing actual {self.stage} command {ident:#x} slot={value}")
        if self.stage == "dji.drop":
            # A transition may contain an earlier zero. Require another actual
            # TX after the firmware's strict >50 ms feedback timeout interval.
            deadline = self.phase_start_ns[self.stage] + 51 * STEP_NS
            if not any(row["phase"] == self.stage and row["can_id"] == 0x200
                       and row["slots"][0] == 0 and row["can_time_ns"] > deadline
                       for row in self.trace):
                raise RuntimeError("missing actual timeout zero beyond 51ms drop interval")
            feedback = [row for row in self.deliveries
                        if row["phase"] == self.stage and row["destination"] == "transport.qemu"]
            if (not any(row["can_id"] == 0x201 and row["dropped"] for row in feedback)
                    or not any(row["can_id"] == 0x205 and not row["dropped"]
                               for row in feedback)):
                raise RuntimeError("targeted feedback drop lacks independent ID5 control")

    def ready(self, name: str, actual: int, expected: int) -> None:
        if self.stage_index >= len(STAGES) or (
                name, actual, expected) != STAGES[self.stage_index]:
            raise RuntimeError(f"unexpected DJI admission stage: {name} {actual} {expected}")
        self._finish_stage()
        self.stage = name
        self.phase_start_ns[name] = max(self.previous_wire_ns, self.runtime.t_sim_ns)
        self.stage_index += 1
        if name == "dji.vector":
            # Literal bytes are the independent consumer oracle; the public
            # producer must match them before they traverse the normal Fabric.
            for ident, fields, literal in (
                    (1, (2048, -192, -8192, 42), "0800ff40e0002a00"),
                    (5, (4096, 192, 8192, 43), "100000c020002b00")):
                frame = encode_feedback(
                    ident, encoder=fields[0], speed_rpm=fields[1],
                    current_raw=fields[2], temperature_c=fields[3],
                    timestamp_ns=self.runtime.t_sim_ns)
                if frame.data != bytes.fromhex(literal):
                    raise RuntimeError("DJI feedback producer differs from literal vector")
                self.runtime.schedule_input("literal-vector.tx", frame)
            self.runtime.run_until(self.runtime.t_sim_ns, deadline_monotonic=self.deadline)

    def ingress(self, frame: CanFrame, *,
                max_catchup_steps: int = MAX_CATCHUP_STEPS) -> None:
        if frame.timestamp_ns < self.previous_wire_ns:
            raise RuntimeError("QEMU CAN timestamp moved backwards")
        # C620 manual: standard DATA, DLC8, four signed big-endian slots.
        # Only IDs1 and5 exist in this fixture; the unused slots must be zero.
        if (frame.can_id not in (0x200, 0x1FF) or frame.flags != 0
                or frame.dlc != 8 or len(frame.data) != 8):
            raise RuntimeError("unexpected real DJI command shape")
        slots = struct.unpack(">hhhh", frame.data)
        if slots[0] not in (-4096, 0, 4096) or slots[1:] != (0, 0, 0):
            raise RuntimeError(f"unexpected real DJI command slots: {slots}")
        target = frame.timestamp_ns // STEP_NS * STEP_NS
        catchup = (target - self.runtime.t_sim_ns) // STEP_NS
        if catchup > max_catchup_steps:
            raise RuntimeError("QEMU CAN gap exceeds bounded fixture catch-up")
        for _ in range(catchup):
            self.runtime.step(STEP_NS, deadline_monotonic=self.deadline)
            self.steps += 1
        self.runtime.schedule_input("transport.qemu_rx", frame)
        self.previous_wire_ns = frame.timestamp_ns
        self.wire_seen[self.stage][frame.can_id].add(slots[0])
        append_bounded(self.trace, dict(
            phase=self.stage, can_id=frame.can_id, flags=frame.flags, dlc=frame.dlc,
            can_time_ns=frame.timestamp_ns, plant_time_ns=self.runtime.t_sim_ns,
            slots=list(slots), data=frame.data.hex()))

    def finish(self) -> None:
        self._finish_stage()
        if self.stage_index != len(STAGES):
            raise RuntimeError("missing DJI admission stages")
        if any(motor.rejected_commands for motor in self.motors):
            raise RuntimeError("virtual M3508 rejected a real driver command")

    def close(self) -> None:
        self.runtime.close()


def source_snapshot(elf: Path, qemu: Path, firmware: Path) -> dict:
    paths = {"elf": elf, "qemu": qemu, "host_runner": Path(__file__).resolve(),
             "launcher": ROOT / "tools/run-trobot-dji-test.sh",
             "harness": ROOT / "tools/dm_mc02_test_harness.py",
             "qmp": ROOT / "tools/dm_mc02_qmp.py",
             "worker": ROOT / "tools/dm_mc02_sim_worker.py"}
    for path in sorted((ROOT / "sim").rglob("*.py")):
        paths[str(path.relative_to(ROOT))] = path
    for relative in (
            "app/selftest/dji.cc", "app/selftest/main.cc", "app/CMakeLists.txt",
            "CMakeLists.txt", "CMakePresets.json", "bsp/test/core.c",
            "bsp/test/transport.c", "bsp/CMakeLists.txt", "bsp/src/can.c",
            "components/motor/src/dji.cc", "components/motor/include/motor/dji.h"):
        paths["firmware/" + relative] = firmware / relative
    return {name: dict(path=str(path.resolve()),
                       sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            for name, path in paths.items()}


def check_case(row: dict) -> None:
    name, actual, expected = row["name"], row["actual"], row["expected"]
    if not 0 <= actual <= 0xFFFFFFFF or not 0 <= expected <= 0xFFFFFFFF:
        raise RuntimeError("firmware report fields exceed uint32")
    if row["status"] == "FAIL":
        raise RuntimeError("firmware failed: " + name)
    if row["status"] == "SKIP":
        if (name, actual, expected) != ("plant.physics", 0, 0):
            raise RuntimeError("unexpected firmware SKIP")
        return
    if row["status"] != "PASS" or name not in REQUIRED:
        raise RuntimeError("unexpected firmware coverage: " + name)
    if name == "dji.drop.age":
        ok = actual > 50 and expected == 51
    elif name == "dji.drop.app_age":
        ok = actual < 50 and expected == 50
    elif name == "task.stack":
        ok = actual >= 64 and expected == 64
    elif name == "dji.drop.frozen":
        ok = actual == expected and actual != 0
    else:
        ok = actual == expected == EXACT_EXPECTED[name]
    if not ok:
        raise RuntimeError("firmware report fields disagree: " + name)


def run(qmp_path: str, uart_path: str, can_path: str, elf: str, qemu: str,
        report_path: str, firmware: str) -> int:
    qmp = fixture = None
    sockets = []
    rows, summaries = [], []
    inputs = {}
    error = None
    stage = "startup"
    watchdog = None

    def interrupted(signum, _frame):
        raise RuntimeError(f"interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        inputs = source_snapshot(Path(elf), Path(qemu), Path(firmware))
        qmp = QmpSession(qmp_path, timeout=2)
        for path in (uart_path, can_path):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sockets.append(sock)
            sock.settimeout(2)
            sock.connect(path)
            sock.setblocking(False)
        uart, can_sock = sockets
        can = CanStream(can_sock)
        fixture = DjiFixture(can)
        qmp.command("cont")
        buffer = bytearray()
        deadline = time.monotonic() + 25
        done = False
        while not done and time.monotonic() < deadline:
            readable, _, _ = select.select(sockets, [], [], .01)
            # Retain transition commands before changing the UART phase policy.
            if can_sock in readable:
                start_steps = fixture.steps
                for ident, flags, dlc, stamp, data in can.receive():
                    fixture.ingress(CanFrame(ident, flags, dlc, data, stamp),
                                    max_catchup_steps=MAX_CATCHUP_STEPS -
                                    (fixture.steps - start_steps))
            if uart in readable:
                chunk = uart.recv(4096)
                if not chunk:
                    raise RuntimeError("firmware UART disconnected")
                buffer.extend(chunk)
                if len(buffer) > 8192:
                    raise BufferError("firmware UART output exceeded bound")
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer[:] = remainder
                    line = raw.decode("ascii").strip()
                    if not line.startswith("TTEST "):
                        continue
                    print(line, flush=True)
                    fields = line.split()
                    if len(fields) != 5:
                        raise RuntimeError("invalid firmware report")
                    if fields[1] in ("PHASE", "DONE"):
                        summary = list(map(int, fields[2:]))
                        if summary != [len(REQUIRED), 0, 1]:
                            raise RuntimeError("firmware summary differs from required coverage")
                        summaries.append(fields[1])
                        done = fields[1] == "DONE"
                        continue
                    status, name = fields[1:3]
                    actual, expected = map(int, fields[3:])
                    row = dict(status=status, name=name, actual=actual, expected=expected)
                    append_bounded(rows, row)
                    stage = name
                    if status == "READY":
                        fixture.ready(name, actual, expected)
                        # Handshake admits the selected policy before guest work.
                        uart.settimeout(1)
                        uart.sendall(b"\xa5")
                        uart.setblocking(False)
                    else:
                        check_case(row)
            can.flush()
        if not done:
            raise TimeoutError("firmware did not complete: " + stage)
        fixture.finish()
        counts = Counter(row["name"] for row in rows if row["status"] == "PASS")
        skips = [row["name"] for row in rows if row["status"] == "SKIP"]
        if (counts != Counter({name: 1 for name in REQUIRED})
                or skips != ["plant.physics"] or summaries != ["PHASE", "DONE"]):
            raise RuntimeError("missing, duplicated or unexpected firmware coverage")
        watchdog = qmp.command("qom-get", {
            "path": "/machine", "property": "iwdg-diagnostics"})
        values = dict(item.split("=", 1) for item in watchdog.split(","))
        if values.get("timeouts") != "0" or values.get("window-violations") != "0":
            raise RuntimeError("unexpected watchdog event")
        if source_snapshot(Path(elf), Path(qemu), Path(firmware)) != inputs:
            raise RuntimeError("consumer inputs changed during execution")
    except BaseException as exc:
        error = str(exc) or type(exc).__name__
    finally:
        cleanup_errors = []
        for resource in (fixture, qmp, *reversed(sockets)):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as exc:
                    cleanup_errors.append(f"{type(resource).__name__}: {exc}")
        if cleanup_errors:
            error = (error + "; " if error else "") + "cleanup failed: " + "; ".join(cleanup_errors)
        counts = Counter(row["name"] for row in rows if row["status"] == "PASS")
        report = dict(
            result="failed" if error else "passed", error=error, last_stage=stage,
            cases=rows, summaries=summaries, missing=sorted(REQUIRED - counts.keys()),
            trace=fixture.trace if fixture else [],
            deliveries=fixture.deliveries if fixture else [],
            steps=fixture.steps if fixture else 0,
            plant_time_ns=fixture.runtime.t_sim_ns if fixture else 0,
            host_quantum_ns=STEP_NS, known_can_due_cuts=True,
            intervals_completed=fixture.runtime.step_id if fixture else 0,
            phase_start_ns=fixture.phase_start_ns if fixture else {},
            max_catchup_steps=MAX_CATCHUP_STEPS, observation_bound=MAX_RECORDS,
            delivery_scope="feedback to QEMU transport; incoming wire commands in trace",
            source_inputs=inputs,
            motor_consumer_declared_reference=MOTOR_CONSUMER_REVISION,
            motor_reference_hashes=MOTOR_REFERENCE_HASHES,
            watchdog=watchdog, cleanup_errors=cleanup_errors,
            limits=["standalone QEMU epoch=0", "fixed two-rotor fixture",
                    "1ms host sample clock", "no global lockstep",
                    "no physical calibration", "no realtime gate"])
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n")
    if error:
        print("DJI consumer failed: " + error, file=sys.stderr)
        return 1
    print("RESULT: trobot motor::dji/CAN Fabric/M3508 boundary passed; physical calibration SKIP")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--feedback-scenario":
        import argparse
        from tools.dm_mc02_scene_smoke import run_dji_feedback_scenario
        parser = argparse.ArgumentParser(description="Actual dual-QEMU DJI feedback timeout consumer")
        parser.add_argument("--feedback-scenario", action="store_true")
        parser.add_argument("elf")
        parser.add_argument("output")
        parser.add_argument("firmware")
        parser.add_argument("--qemu")
        args = parser.parse_args()
        return run_dji_feedback_scenario(args.elf, args.output, args.firmware, qemu=args.qemu)
    if len(sys.argv) > 1 and sys.argv[1] == "--scenario":
        import argparse
        from tools.dm_mc02_scene_smoke import run_dji_scenario
        parser = argparse.ArgumentParser(description="Actual dual-QEMU DJI Scenario consumer")
        parser.add_argument("--scenario", action="store_true")
        parser.add_argument("elf")
        parser.add_argument("output")
        parser.add_argument("firmware")
        parser.add_argument("--qemu")
        args = parser.parse_args()
        return run_dji_scenario(args.elf, args.output, args.firmware, qemu=args.qemu)
    if len(sys.argv) != 8:
        raise SystemExit("expected QMP UART CAN ELF QEMU REPORT FIRMWARE_SOURCE")
    return run(*sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
