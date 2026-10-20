#!/usr/bin/env python3
"""Actual ELF acceptance through the single public Simulation execution path."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES = ("minimal-board", "can-motor")
EXAMPLE_ALIASES = {"pulse": "minimal-board", "dual-board": "can-motor",
                   "digital-link": "minimal-board"}
NATIVE_CASES = (*CASES, "pulse", "digital-link")


class Blocked(RuntimeError):
    """An explicitly required external build/native dependency is absent."""


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
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
        elif event.kind == "can-tx" or (event.kind == "can.sent" and event.source_node == "motor0"):
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
                "digital": self.digital, "digital_consumption": self.digital_consumption}


def build_example(case, *, build_dir=None):
    for program in ("cmake", "arm-none-eabi-gcc"):
        if shutil.which(program) is None:
            raise Blocked(f"{program} is required to build the real example ELF")
    source = ROOT / "examples" / EXAMPLE_ALIASES.get(case, case)
    default_build = ROOT / "build" / "simulation-examples" / "pulse" if case == "pulse" else source / "build"
    build = default_build if build_dir is None else Path(build_dir)
    variant = (["-DSIM_KEY_PULSE_TEST=ON"] if case == "pulse" else
               ["-DSIM_KEY_PULSE_TEST=OFF"] if source.name == "minimal-board" else [])
    commands = (["cmake", "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Debug", *variant],
                ["cmake", "--build", str(build), "--parallel", "2"])
    logs = []
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
        logs.append({"command": command, "exit": result.returncode,
                     "output": (result.stdout + result.stderr)[-16000:]})
        if result.returncode:
            raise RuntimeError(f"example build failed: {logs[-1]}")
    elf = build / "firmware.elf"
    return {"elf": str(elf), "sha256": hashlib.sha256(elf.read_bytes()).hexdigest(), "build": logs}


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
                           "scene.yaml", registry)
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
    parser.add_argument("--case", choices=(*NATIVE_CASES, "hal-gpio", "dual-board", "all"), default="all")
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
                payload["cases"][case] = build_example(case)
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
