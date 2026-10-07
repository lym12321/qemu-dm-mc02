#!/usr/bin/env python3
"""Test-only actual dual-MCU consumer of one shared four-hinge MuJoCo world.

The analytical fixture has literal inertia .02, gear2, no damping/gravity.
Expected CAN bytes and Bosch half-LSB gyro bins are independent constants.
Native pause is a fixture ordering aid, never a CPU/plant time barrier.
"""

from contextlib import ExitStack
from dataclasses import asdict
import argparse
import hashlib
import json
import math
from pathlib import Path
import select
import signal
import socket
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dm_mc02_qmp import QmpSession
from dm_mc02_test_harness import running_qemu
from sim.boards.launch import qemu_command, regular_file, resolve_qemu
from sim.core import Session
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.fabric.endpoints import QemuCanEndpoint
from sim.physics import MujocoPhysics
from sim.robots.shared_world import BoardBinding, SharedWorldRuntime
from sim.transports.can_codec import WIRE_SIZE, unpack_wire_frame
from sim.transports.can_stream import CanStream
from sim.transports.cosim import StepFramedSocket, V2SessionReset
from sim.transports import step_codec as protocol
from sim.transports.stream import StreamTransport
from sim.transports.unix import connect_unix

COMMANDS = ("1000200000000000", "f000e00000000000")
FEEDBACK = (("0002000210001900", "0005000420001900"),
            ("1ffdfffef0001900", "1ffafffce0001900"))
DT_NS = 10_000_000
ORIGIN_NS = 123_456_789


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def starttime(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def source_origins():
    """Cumulative currently loaded sources, not first-import times or coverage."""
    result = {}
    for name, module in sorted(sys.modules.items()):
        if name == "__main__" or name.startswith(("sim.", "dm_mc02_", "qemu.")):
            value = getattr(module, "__file__", None)
            if value is None:
                continue
            path = Path(value).resolve()
            if not path.is_relative_to(ROOT):
                raise AssertionError(f"actual native module outside checkout: {name} {path}")
            result[name] = {"path": str(path), "sha256": sha(path)}
    return result


def make_world(report):
    import mujoco
    bodies = "".join(
        f'<body name="b{i}" pos="{i} 0 0">'
        f'<joint name="j{i}" type="hinge" axis="0 0 1"/>'
        '<inertial pos="0 0 0" mass="1" diaginertia=".015 .015 .02"/>'
        f'<site name="s{i}" pos="0 0 0"/></body>' for i in range(1, 5))
    motors = "".join(f'<motor name="m{i}" joint="j{i}"/>' for i in range(1, 5))
    sensors = "".join(f'<gyro name="g{i}" site="s{i}"/>'
                       f'<accelerometer name="a{i}" site="s{i}"/>' for i in (1, 3))
    xml = ('<mujoco><compiler angle="radian"/>'
           '<option gravity="0 0 0" integrator="Euler"/>'
           f'<worldbody>{bodies}</worldbody><actuator>{motors}</actuator>'
           f'<sensor>{sensors}</sensor></mujoco>')
    report["world"] = {"xml": xml, "sha256": hashlib.sha256(xml.encode()).hexdigest(),
                       "mujoco_version": mujoco.__version__,
                       "mujoco_module": str(Path(mujoco.__file__).resolve())}
    native = MujocoPhysics(mujoco.MjModel.from_xml_string(xml),
        joints={f"j{i}": f"m{i}" for i in range(1, 5)},
        imus={f"imu{i}": (f"g{j}", f"a{j}") for i, j in enumerate((1, 3))})
    calls = report["native_calls"]
    original_reset, original_step, original_close = native.reset, native.step, native.close

    def reset():
        calls["reset"] += 1
        return original_reset()

    def step(dt_ns):
        calls["step_ns"].append(dt_ns)
        return original_step(dt_ns)

    def close():
        calls["close"] += 1
        return original_close()

    native.reset, native.step, native.close = reset, step, close
    return native


def main(args):
    binary = resolve_qemu(args.qemu)
    firmwares = tuple(regular_file(path, "firmware") for path in args.firmware)
    report = {"status": "running", "mode": args.mode, "stage": "admission",
              "native_calls": {"reset": 0, "step_ns": [], "close": 0},
              "responses": [], "commands": [], "cleanup": [], "uart": ["", ""],
              "run_statuses": [],
              "limitations": ["bare-MMIO fixture, not HAL/BSP or two chassis",
                "physical calibration SKIP", "CPU/global lockstep unsupported",
                "observer does not attest every advisory frame skipped by active waits"],
              "entrypoint": str(Path(__file__).resolve()), "cwd": str(Path.cwd()),
              "inputs": {str(path): sha(path) for path in (
                  binary, *firmwares, ROOT / "smoke/dm_mc02_shared_world_smoke.c",
                  ROOT / "smoke/dm_mc02_bmi088_spi_fixture.h",
                  ROOT / "smoke/dm_mc02_cosim_link_readback_smoke.ld",
                  ROOT / "tools/run-qemu-shared-world-smoke.sh")}}
    origins = source_origins()
    native = None
    devices = ()
    plant = None
    runtime = None
    owners, controls, peers, streams, uarts, identities = [], [], [], [], [], []
    deadline = time.monotonic() + 15.0  # Starts before first native Popen.
    primary = None

    def remaining():
        for index, owner in enumerate(owners):
            status = owner.process.poll()
            if status is not None:
                raise RuntimeError(f"native peer {index} exited early ({status})")
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("shared-world native host deadline expired")
        return value

    def qmp(index, command, arguments=None):
        remaining()
        controls[index].settimeout(min(2.0, remaining()))
        result = controls[index].command(command, arguments)
        remaining()
        if command == "query-status":
            report["run_statuses"].append({"peer": index, "stage": report["stage"],
                                           "value": result})
        return result

    def response(name, frame, decoded):
        report["responses"].append({"name": name, "kind": frame.header.kind,
            "session": frame.header.session_id, "step": frame.header.step_id,
            "time_ns": frame.header.t_sim_ns, "payload_hex": frame.payload.hex(),
            "decoded": None if decoded is None else asdict(decoded)})

    def uart_read(index):
        uarts[index].read_once()  # At most one existing 4096-byte stream read.
        data = bytes(uarts[index].buffer)
        uarts[index].buffer.clear()
        report["uart"][index] += data.decode("ascii", errors="strict")
        if len(report["uart"][index]) > 4096:
            raise RuntimeError("native fixture UART exceeds bounded trace")
        if "WORLD_BAD_CAN" in report["uart"][index]:
            raise AssertionError(f"guest {index} rejected actual CAN feedback")

    def wait_uart(index, marker):
        while marker not in report["uart"][index]:
            remaining()
            uart_read(index)
            if marker in report["uart"][index]:
                break
            select.select([uarts[index].sock], [], [], min(.01, remaining()))
        remaining()

    try:
        native = make_world(report)
        buses = (CanBus(), CanBus())
        devices = tuple(M3508Device(bus, identifier) for bus in buses for identifier in (1, 2))
        plant = M3508JointPlant(native, dict(zip(devices, ("j1", "j2", "j3", "j4"))),
                                gear_ratio=2)
        # Until runtime construction succeeds, caller retains world ownership.
        with ExitStack() as stack:
            try:
                for index, firmware in enumerate(firmwares):
                    report["stage"] = f"startup_peer_{index}"
                    sockets = ("qmp", "cosim", "can1", "uart1")
                    command = qemu_command(firmware, binary, sockets)
                    owner = stack.enter_context(running_qemu(
                        command, sockets=sockets, startup_timeout=min(3., remaining())))
                    owners.append(owner)
                    identity = {"peer": index, "pid": owner.process.pid,
                        "starttime": starttime(owner.process.pid),
                        "tempdir": str(owner.stderr_path.parent),
                        "argv": [owner.expand(value) for value in command]}
                    if identity["starttime"] is None:
                        raise RuntimeError("native peer identity disappeared during startup")
                    identities.append(identity)
                    control = QmpSession(str(owner.sockets["qmp"]),
                        timeout=min(2., remaining()), connect_timeout=min(2., remaining()),
                        close_timeout=2.)
                    peers.append((f"peer {index} QMP", control))
                    controls.append(control)
                    sockets_open = {}
                    for name in ("cosim", "can1", "uart1"):
                        sock = connect_unix(str(owner.sockets[name]), min(2., remaining()))
                        peers.append((f"peer {index} {name}", sock))
                        sockets_open[name] = sock
                    streams.append((StepFramedSocket(sockets_open["cosim"]),
                                    CanStream(sockets_open["can1"])))
                    uarts.append(StreamTransport(sockets_open["uart1"]))
                    assert qmp(index, "query-status")["running"] is False
                    qmp(index, "cont")
                    assert qmp(index, "query-status")["running"] is True
                    wait_uart(index, f"WORLD_READY={index}\n")
                    qmp(index, "qom-set", {"path": "/machine", "property": "user-key", "value": True})
                    # Test-only peek leaves every physical byte for the runtime.
                    while True:
                        remaining()
                        try:
                            raw = sockets_open["can1"].recv(WIRE_SIZE, socket.MSG_PEEK)
                        except BlockingIOError:
                            raw = None
                        if raw == b"":
                            raise ConnectionError("native CAN closed before command")
                        if raw is not None and len(raw) == WIRE_SIZE:
                            frame = unpack_wire_frame(raw)
                            assert (frame.can_id, frame.flags, frame.dlc, frame.data.hex()) == (
                                0x200, 0, 8, COMMANDS[index])
                            report["commands"].append({"peer": index, "wire_hex": raw.hex(),
                                "timestamp_ns": frame.timestamp_ns, "data_hex": frame.data.hex()})
                            break
                        select.select([sockets_open["can1"]], [], [], min(.01, remaining()))
                    qmp(index, "stop")
                    assert qmp(index, "query-status")["running"] is False

                report["stage"] = "start_shared_world"
                bindings = tuple(BoardBinding(f"peer{i}", QemuCanEndpoint(bus, streams[i][1]),
                    streams[i][0], f"imu{i}") for i, bus in enumerate(buses))
                runtime = SharedWorldRuntime(plant, bindings,
                    check_running=remaining, on_response=response)
                reset_acks = runtime.start(Session(17, ORIGIN_NS), (81, 82), deadline=deadline)
                report["reset_acks"] = [{"peer": i, "session": ack.header.session_id,
                    "epoch_ns": protocol.decode_reset_ack_payload(ack.payload).virtual_time_ns}
                    for i, ack in enumerate(reset_acks)]
                for command, ack in zip(report["commands"], report["reset_acks"]):
                    assert command["timestamp_ns"] <= ack["epoch_ns"]
                assert report["native_calls"]["reset"] == 1
                assert report["native_calls"]["step_ns"] == []
                assert runtime.t_sim_ns == ORIGIN_NS and runtime.step_id == 0

                report["stage"] = "one_shared_world_tick"
                acks = runtime.step(DT_NS, deadline=deadline)
                assert len(acks) == 2
                for index, ack in enumerate(acks):
                    assert ack.header.kind == protocol.STEP_ACK and ack.header.step_id == 1
                    assert ack.header.session_id == 81 + index
                    assert protocol.decode_step_ack_payload(ack.payload).status == 0
                assert report["native_calls"]["reset"] == 1
                assert report["native_calls"]["step_ns"] == [DT_NS]
                assert native.time_s == .01
                report["joints"] = {}
                for name, position, velocity in zip(("j1", "j2", "j3", "j4"),
                        (.001, .002, -.001, -.002), (.1, .2, -.1, -.2)):
                    state = native.get_joint_state(name)
                    assert abs(state.position_rad - position) < 1e-12
                    assert abs(state.velocity_rad_s - velocity) < 1e-12
                    report["joints"][name] = asdict(state)
                report["imus"] = [asdict(native.get_imu(f"imu{i}")) for i in range(2)]
                for index, sample in enumerate(report["imus"]):
                    assert all(abs(v) < 1e-12 for v in sample["accel_m_s2"])
                    assert all(abs(v) < 1e-12 for v in sample["gyro_rad_s"][:2])
                    assert abs(sample["gyro_rad_s"][2] - (.1 if index == 0 else -.1)) < 1e-12

                report["stage"] = "actual_guest_raw_consumption"
                for index in range(2):
                    qmp(index, "cont")
                    assert qmp(index, "query-status")["running"] is True
                while True:
                    runtime.service(deadline=deadline, timeout=0)
                    for index in range(2):
                        uart_read(index)
                    done = [event for event in report["responses"]
                            if event["kind"] == protocol.STEP_DONE and event["step"] == 1]
                    if all(f"WORLD_RESULT={i} " in report["uart"][i] and
                           report["uart"][i].endswith("\n") for i in range(2)) and len(done) == 2:
                        break
                    remaining()
                    select.select([stream.sock for stream, _ in streams] +
                                  [uart.sock for uart in uarts], [], [], min(.01, remaining()))
                assert {event["name"] for event in done} == {"peer0", "peer1"}
                for event in done:
                    assert (event["decoded"]["status"], event["decoded"]["consumed_mask"],
                            event["decoded"]["missing_mask"]) == (0, 3, 0)
                for index in range(2):
                    lines = [line for line in report["uart"][index].splitlines()
                             if line.startswith("WORLD_RESULT=")]
                    assert len(lines) == 1
                    fields = lines[0].split()
                    assert fields[:3] == [f"WORLD_RESULT={index}", *FEEDBACK[index]]
                    raw_gyro = struct.unpack("<3h", bytes.fromhex(fields[3]))
                    raw_accel = struct.unpack("<3h", bytes.fromhex(fields[4]))
                    assert raw_gyro == (0, 0, 188 if index == 0 else -188)
                    assert raw_accel == (0, 0, 0)
                    # Bosch 39.0625us sensortime ticks: one10ms fixture target.
                    assert int(fields[5], 16) == (ORIGIN_NS + DT_NS) * 256 // 10_000_000
                    # +/-1000dps => 32.768 LSB/dps. Native float32 input must
                    # be inside the fixed code's independent half-LSB interval.
                    degrees = struct.unpack("<f", struct.pack("<f", (.1 if index == 0 else -.1) * 180 / math.pi))[0]
                    code = raw_gyro[2]
                    assert (code - .5) / 32.768 <= degrees <= (code + .5) / 32.768
                for index in range(2):
                    qmp(index, "stop")
                    assert qmp(index, "query-status")["running"] is False

                if args.mode != "normal":
                    report["stage"] = f"negative_{args.mode}"
                    before = dict(reset=report["native_calls"]["reset"],
                                  step_ns=list(report["native_calls"]["step_ns"]))
                    if args.mode == "reset":
                        qmp(0, "system_reset")
                    else:
                        # This deliberate exit bypasses only the fixture's
                        # post-command guard so the runtime itself observes it.
                        wait_timeout = min(2., remaining())
                        controls[1].settimeout(wait_timeout)
                        controls[1].command("quit")
                        owners[1].process.wait(timeout=wait_timeout)
                    try:
                        while True:
                            runtime.service(deadline=deadline, timeout=.01)
                    except (V2SessionReset, RuntimeError) as exc:
                        if args.mode == "reset":
                            assert isinstance(exc, V2SessionReset)
                        else:
                            assert "native peer 1 exited early (0)" in str(exc)
                        assert runtime.failed
                        try:
                            runtime.step(DT_NS, deadline=deadline)
                        except RuntimeError:
                            pass
                        else:
                            raise AssertionError("frozen runtime accepted another world tick")
                        assert before == dict(reset=report["native_calls"]["reset"],
                            step_ns=report["native_calls"]["step_ns"])
                        report["freeze_verified"] = True
                        raise
                remaining()
            except BaseException as exc:
                primary = exc
                raise
            finally:
                errors = []
                world_owner = runtime if runtime is not None else plant
                try:
                    world_owner.close()
                except BaseException as exc:
                    errors.append(("world", exc))
                for name, peer in reversed(peers):
                    try:
                        peer.close()
                    except BaseException as exc:
                        errors.append((name, exc))
                for name, error in errors:
                    if primary is None:
                        primary = error
                    else:
                        primary.add_note(f"{name} cleanup failed: {error}")
                if sys.exc_info()[1] is None and primary is not None:
                    raise primary
    except BaseException as exc:
        primary = exc
        if plant is None:
            # Plant admission has not transferred either input owner yet.
            for device in devices:
                try:
                    device.close()
                except BaseException as cleanup:
                    primary.add_note(f"device {device.motor_id} cleanup failed: {cleanup}")
        if runtime is None and native is not None and report["native_calls"]["close"] == 0:
            try:
                native.close()
            except BaseException as cleanup:
                primary.add_note(f"native cleanup failed: {cleanup}")

    report["device_cleanup"] = [device.closed for device in devices]

    for identity, owner in zip(identities, owners):
        alive = starttime(identity["pid"]) == identity["starttime"]
        identity.update(alive=alive, returncode=owner.process.poll(),
                        temporary_exists=Path(identity["tempdir"]).exists())
        report["cleanup"].append(identity)
        if alive or identity["returncode"] is None or identity["temporary_exists"]:
            error = RuntimeError(f"native peer {identity['peer']} cleanup incomplete")
            if primary is None:
                primary = error
            else:
                primary.add_note(str(error))
    try:
        current = source_origins()
        assert all(current[name] == value for name, value in origins.items())
        for path, digest in report["inputs"].items():
            assert sha(path) == digest
        report["actual_sources"] = current
        report["source_preserved"] = True
    except BaseException as exc:
        if primary is None:
            primary = exc
        else:
            primary.add_note(f"source provenance failed: {exc}")
    report["status"] = "passed" if primary is None else "failed"
    if primary is not None:
        report["error"] = f"{type(primary).__name__}: {primary}"
        report["notes"] = getattr(primary, "__notes__", [])
        print("RESULT_JSON: " + json.dumps(report, sort_keys=True), flush=True)
        raise primary
    Path(args.result).write_text(json.dumps(report, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--firmware", nargs=2, required=True)
    parser.add_argument("--mode", choices=("normal", "reset", "peer-exit"), default="normal")
    parser.add_argument("--result", required=True)
    try:
        sys.exit(main(parser.parse_args()))
    except ModuleNotFoundError as exc:
        if exc.name != "mujoco":
            raise
        print("SKIP: optional native MuJoCo runtime is unavailable", file=sys.stderr)
        sys.exit(77)
