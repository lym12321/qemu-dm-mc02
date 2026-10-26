#!/usr/bin/env python3
"""Native automatic Lab and joint ADC/IMU consumers over public experiments."""
from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
from collections.abc import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smoke.simulation_smoke import Blocked, build_example


def plain(value):
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    return value


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def receipt(event):
    return {name: plain(getattr(event, name)) for name in (
        "session_id", "sequence", "source_node", "source_port", "kind",
        "source_time_ns", "mapped_time_ns", "delivery_time_ns", "quality")}


def identity(event):
    return tuple(event.payload[name] for name in ("session_id", "step_id", "t_sim_ns", "dt_ns"))


def uart_observations(events):
    batches = [event for event in events if event.kind == "uart"
               and event.source_node == "main" and event.source_port == "uart1"]
    uart, bounds = bytearray(), []
    for event in batches:
        if type(event.payload) is not bytes:
            raise AssertionError("actual native UART observation must contain bytes")
        uart.extend(event.payload)
        bounds.append((len(uart), event))
    if b"FAIL" in uart:
        raise AssertionError(f"guest reported failure: {bytes(uart)!r}")
    return bytes(uart), bounds


def verify_combined(events, result):
    """Literal ADC/Bosch expectations, actual identities and later guest stores."""
    uart, bounds = uart_observations(events)
    if not result.passed or result.delivered_inputs != 2 or result.origin_ns <= 0:
        raise AssertionError("joint experiment did not complete its actual-ready inputs/assertions")
    adc = [event for event in events if event.kind == "adc-input-accepted"]
    imu = [event for event in events if event.kind == "imu-input-accepted"]
    done = [event for event in events if event.kind == "imu-consumption"]
    inputs = [event for event in events if event.kind == "input.delivered"]
    expected = ((result.origin_ns + 20_000_000, {"raw": 12345}, 12345),
                (result.origin_ns + 40_000_000, {"voltage_uv": 1_650_000}, 32768))
    if len(adc) != 2 or len(inputs) != 2:
        raise AssertionError("two actual ADC input deliveries and acceptance records are required")
    matches = list(re.finditer(
        rb"^ADC_IMU_READ n=([12]) adc=([0-9a-f]{4}) g=([0-9a-f]{12}) a=([0-9a-f]{12}) t=([0-9a-f]{6})\n",
        uart, re.MULTILINE))
    if ([int(match[1]) for match in matches] != [1, 2]
            or uart.find(b"ADC_IMU_READY\n") < 0
            or matches[0].start() < uart.index(b"ADC_IMU_READY\n") + len(b"ADC_IMU_READY\n")
            or uart.find(b"ADC_IMU_PASS\n", matches[-1].end()) < 0):
        raise AssertionError("two ordered actual joint raw readbacks are required")
    evidence = []
    accel_g = struct.unpack("<f", struct.pack("<f", 9.81 / 9.80665))[0]
    for accepted, delivered, match, (cut, payload, raw) in zip(adc, inputs, matches, expected):
        if (accepted.source_node != "main" or accepted.source_port != "adc1.ch4"
                or accepted.mapped_time_ns != cut or accepted.delivery_time_ns != cut
                or dict(accepted.payload["input"]) != payload or accepted.payload["status"] != 0
                or delivered.source_node != "main" or delivered.source_port != "adc1.ch4"
                or dict(delivered.payload) != payload or delivered.mapped_time_ns != cut
                or delivered.delivery_time_ns != cut):
            raise AssertionError("ADC receipt differs from its actual scheduled delivery")
        same_cut = [event for event in imu if identity(event) == identity(accepted)]
        if len(same_cut) != 1 or same_cut[0].mapped_time_ns != cut:
            raise AssertionError("ADC and IMU must share exactly one same-cut STEP identity")
        sample = same_cut[0]
        wire = sample.payload["wire"]
        if (sample.payload["status"] != 0 or wire["sensor_time_ns"] != cut
                or list(wire["gyro_dps"]) != [0.0, 0.0, 0.0]
                or list(wire["accel_g"]) != [0.0, 0.0, accel_g]):
            raise AssertionError("joint IMU admission differs from the independent rest fixture")
        terminal = [event for event in done if identity(event) == identity(accepted)]
        if len(terminal) != 1 or any(terminal[0].payload[name] != value for name, value in (
                ("status", 0), ("consumed_mask", 3), ("missing_mask", 0))):
            raise AssertionError("joint readback lacks matching actual full-consumed DONE")
        gyro = struct.unpack("<3h", bytes.fromhex(match[3].decode()))
        accel = struct.unpack("<3h", bytes.fromhex(match[4].decode()))
        ticks = int(match[5], 16)
        # RM0468 regular16bit DR; ideal midpoint round(65535/2)=32768.
        # Bosch Rev1.9 p27 sensortime39.0625us; p39/p30 ranges1000dps/6g.
        if (int(match[2], 16) != raw or gyro != (0, 0, 0) or accel != (0, 0, 5463)
                or ticks != ((cut * 25600 // 1_000_000_000) & 0xffffff)):
            raise AssertionError("actual guest ADC/raw/sensortime differs from the same-cut inputs")
        for values, codes, full_scale in ((wire["gyro_dps"], gyro, 1000.0),
                                          (wire["accel_g"], accel, 6.0)):
            lsb = full_scale / 32768
            if any(abs(value - code * lsb) > lsb / 2 for value, code in zip(values, codes)):
                raise AssertionError("actual guest IMU raw misses the float32 wire half-LSB bin")
        bound = next((event for length, event in bounds if length >= match.end()), None)
        if (bound is None or str(bound.quality) != "upper_bound" or bound.mapped_time_ns <= cut
                or bound.sequence <= terminal[0].sequence
                or terminal[0].sequence <= sample.sequence):
            raise AssertionError("final guest sensortime store needs later native UART publication")
        evidence.append({"input": payload, "adc_raw": raw, "gyro_raw": gyro, "accel_raw": accel,
            "sensor_ticks_u24": ticks, "wire_identity": identity(accepted),
            "delivery": receipt(delivered), "adc_acceptance": receipt(accepted),
            "imu_acceptance": receipt(sample), "imu_done": receipt(terminal[0]),
            "uart_publication": receipt(bound)})
    return {"readbacks": evidence, "uart": uart.decode("ascii")}


def verify_board(events, result):
    uart, _bounds = uart_observations(events)
    if (not result.passed or len(result.status) != 8 or result.delivered_inputs != 7
            or result.origin_ns <= 0 or result.ready_receipt is None):
        raise AssertionError("complete seven-input/eight-assertion board experiment is required")
    for line in (b"LAB_READY\n", b"UART=q\n", b"KEY=0\n", b"ADC=32768\n", b"ADC=12345\n"):
        if line not in uart:
            raise AssertionError(f"actual board readback is missing: {line!r}")
    adc = [event for event in events if event.kind == "adc-input-accepted"]
    if [(event.mapped_time_ns, dict(event.payload["input"])) for event in adc] != [
            (result.origin_ns + 120_000_000, {"voltage_uv": 1_650_000}),
            (result.origin_ns + 160_000_000, {"raw": 12345})]:
        raise AssertionError("board ADC inputs lost their actual-ready delivery cuts")
    if any(event.kind == "imu-consumption" for event in events):
        raise AssertionError("ADC-only input must not invent IMU consumption DONE")
    return {"uart": uart.decode("ascii"), "adc_acceptance": [receipt(event) for event in adc]}


def build_lab(directory, build_type):
    command = ["arm-none-eabi-gcc", "-mcpu=cortex-m7", "-mthumb",
        "-Og" if build_type == "Debug" else "-O2", "-g", "-Wall", "-Wextra", "-Werror",
        "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-nostdlib", "-nostartfiles",
        "-Wl,--gc-sections", "-Wl,--build-id=none",
        "-Wl,-T," + str(ROOT / "smoke/dm_mc02_uart_smoke.ld"),
        "-o", str(directory / "lab.elf"), str(ROOT / "smoke/dm_mc02_lab_smoke.c")]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"Lab ELF build failed: {result.stdout}{result.stderr}")
    elf = directory / "lab.elf"
    return {"elf": str(elf), "sha256": digest(elf), "build_type": build_type,
            "build": [{"command": command, "exit": result.returncode,
                       "output": result.stdout + result.stderr}]}


def build_combined(directory, build_type, skip_gyro):
    command = ["cmake", "-S", str(ROOT / "examples/adc-imu-readback"), "-B", str(directory),
               "-DSIM_ADC_IMU_SKIP_GYRO_TEST=" + ("ON" if skip_gyro else "OFF")]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"joint fixture configuration failed: {result.stdout}{result.stderr}")
    built = build_example("adc-imu-readback", build_dir=directory, build_type=build_type)
    built["build"].insert(0, {"command": command, "exit": result.returncode,
                              "output": result.stdout + result.stderr})
    built["skip_gyro_test"] = skip_gyro
    return built


def run_scene(case, *, qemu, firmware, directory):
    import yaml
    from sim.apps.experiment import run_experiment
    from sim.composition import builtin_registry
    from sim.config.scene import load_scene
    from sim.machines.qemu import QemuFactory

    source = ROOT / ("examples/labs/board-io.yaml" if case == "board-io"
                     else "examples/adc-imu-readback/scene.yaml")
    document = yaml.safe_load(source.read_text())
    document["nodes"]["main"]["firmware"] = str(firmware)
    if "world" in document["nodes"]:
        document["nodes"]["world"]["model"] = str((source.parent /
            document["nodes"]["world"]["model"]).resolve())
    prepared = directory / f"{case}.yaml"
    prepared.write_text(yaml.safe_dump(document, sort_keys=False))
    machines, resources, events = {}, {}, []

    class CaptureFactory(QemuFactory):
        def create(self, spec, context):
            machine = super().create(spec, context)
            machines[spec.name] = machine
            return machine

    def observe(event):
        if len(events) >= 8192:
            raise BufferError("native Lab observation budget exhausted")
        events.append(event)
        for name, machine in machines.items():
            if machine.started and name not in resources:
                info = machine.connection_info
                pid = info["pid"]
                resources[name] = {"pid": pid, "starttime": Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
                    "sockets": dict(info["sockets"]), "native_origin_ns": info["native_origin_ns"]}

    registry = builtin_registry(qemu=qemu)
    registry["machine.qemu"] = CaptureFactory(qemu=qemu)
    scene = load_scene(prepared, registry)
    primary = None
    try:
        result = run_experiment(scene, registry, on_event=observe)
    except BaseException as error:
        primary = error
        raise
    finally:
        # run_experiment owns actual Simulation/native cleanup on every path.
        failures = []
        for name, resource in resources.items():
            stat = Path(f"/proc/{resource['pid']}/stat")
            if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[19] == resource["starttime"]:
                failures.append(f"native QEMU identity remains: {name}")
            if any(Path(path).exists() for path in resource["sockets"].values()):
                failures.append(f"native QEMU socket remains: {name}")
        partial = {"events": plain(events), "resources": resources,
                   "cleanup_completed": not failures, "cleanup_failures": failures}
        if primary is not None:
            primary.native_observations = partial
            for failure in failures:
                primary.add_note(failure)
        elif failures:
            error = AssertionError("; ".join(failures))
            error.native_observations = partial
            raise error
    if set(resources) != {"main"}:
        raise AssertionError("actual native process identity was not observed")
    try:
        verified = (verify_board if case == "board-io" else verify_combined)(events, result)
    except BaseException as error:
        error.native_observations = partial
        raise
    return {**partial, "experiment": plain(result), "verified": plain(verified)}


def _interrupted(signum, _frame):
    raise KeyboardInterrupt(f"interrupted by signal {signum}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, default=Path(os.environ.get("QEMU_SYSTEM_ARM", ROOT / "build/qemu/qemu-system-arm")))
    parser.add_argument("--build-type", choices=("Debug", "Release"), default="Debug")
    parser.add_argument("--case", choices=("board-io", "adc-imu-readback", "all"), default="all")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--skip-gyro-test", action="store_true",
                        help="test-only mutant; the ordinary acceptance must reject its missing gyro consumption")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report = args.report or ROOT / "build/automatic-lab-migration" / (
        datetime.now(timezone.utc).strftime("native-%Y%m%dT%H%M%S-%f") + f"-{os.getpid()}") / "report.json"
    report = report.resolve()
    directory = report.parent / (report.stem + "-assets")
    payload = {"status": "failed", "cases": {}, "physical_calibration": "SKIP",
        "limits": "ACK is input admission; actual ADC DR/SPI reads and full IMU DONE are separate. No CPU/world barrier, physical calibration or RTF claim."}
    code = 1
    previous = signal.signal(signal.SIGTERM, _interrupted)
    try:
        if report.exists():
            raise FileExistsError(f"report already exists: {report}")
        directory.mkdir(parents=True, exist_ok=False)
        paths = [*sorted((ROOT / "sim").rglob("*.py")), Path(__file__),
            ROOT / "smoke/simulation_smoke.py", ROOT / "smoke/dm_mc02_lab_smoke.c",
            ROOT / "smoke/dm_mc02_uart_smoke.ld", ROOT / "smoke/dm_mc02_bmi088_spi_fixture.h",
            ROOT / "examples/labs/board-io.yaml", ROOT / "examples/shared-world/imu.xml",
            *sorted((ROOT / "examples/minimal-board").glob("*.*")),
            *sorted((ROOT / "examples/adc-imu-readback").glob("*.*"))]
        payload["source_hashes"] = {str(path.relative_to(ROOT)): digest(path) for path in paths if path.is_file()}
        payload["versions"] = {"python": sys.version}
        for program in ("arm-none-eabi-gcc", "cmake"):
            version = subprocess.run([program, "--version"], capture_output=True, text=True, timeout=5)
            version.check_returncode()
            payload["versions"][program] = version.stdout.splitlines()[0]
        if not args.build_only:
            if not args.qemu.is_file() or not os.access(args.qemu, os.X_OK):
                raise Blocked(f"project dm-mc02 QEMU executable is missing: {args.qemu}")
            payload["qemu"] = {"path": str(args.qemu.resolve()), "sha256": digest(args.qemu)}
            version = subprocess.run([str(args.qemu), "--version"], capture_output=True, text=True, timeout=5)
            version.check_returncode()
            payload["versions"]["qemu"] = version.stdout.splitlines()[0]
        for case in ("board-io", "adc-imu-readback") if args.case == "all" else (args.case,):
            build = directory / case
            build.mkdir()
            built = build_lab(build, args.build_type) if case == "board-io" else build_combined(
                build, args.build_type, args.skip_gyro_test)
            payload["cases"][case] = built
            if not args.build_only:
                built["observations"] = run_scene(case, qemu=args.qemu, firmware=Path(built["elf"]), directory=directory)
            if digest(built["elf"]) != built["sha256"]:
                raise AssertionError("native ELF input changed during execution")
        if any(digest(ROOT / name) != expected for name, expected in payload["source_hashes"].items()):
            raise AssertionError("native source input changed during execution/cleanup")
        if not args.build_only:
            if digest(args.qemu) != payload["qemu"]["sha256"]:
                raise AssertionError("QEMU input changed during execution/cleanup")
            from sim.machines.qemu.qmp import qmp_client_provenance
            payload["qmp_client"] = qmp_client_provenance()
            if args.case != "board-io":
                import mujoco
                payload["versions"]["mujoco"] = mujoco.__version__
        payload["status"] = "prepared" if args.build_only else "passed"
        code = 0
    except (Exception, KeyboardInterrupt) as error:
        from sim.api import BackendUnavailable
        blocked = isinstance(error, (Blocked, BackendUnavailable, FileNotFoundError)) or (
            isinstance(error, ModuleNotFoundError) and error.name == "mujoco")
        payload.update(status="blocked" if blocked else "failed", error=str(error),
            error_type=type(error).__name__, notes=list(getattr(error, "__notes__", ())))
        if hasattr(error, "native_observations"):
            payload["partial_observations"] = error.native_observations
        code = 78 if blocked else 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("x") as output:
            json.dump(payload, output, indent=2)
            output.write("\n")
    except (OSError, KeyboardInterrupt) as error:
        print(f"report publication failed: {error}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(f"Native automatic Lab: {payload['status']} ({report})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
