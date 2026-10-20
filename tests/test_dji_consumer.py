"""Host fixture ownership and report checks; device physics has separate gates."""

import importlib.util
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import signal
import sys
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools/dm_mc02_dji_test.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_dji_consumer_test", PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

from tools import dm_mc02_arena_smoke as arena_module
from sim.boards.channels import ChannelEvent
from sim.core import Session
from sim.machines.qemu.endpoints import QemuCanEndpoint
from sim.transports.cosim import QemuTimestampMapper
from sim.transports import StreamTransport
from sim.scenario import ScenarioCanDelay, load_manifest as load_scenario, runtime as scenario_runtime
from test_arena_runtime import Environment, Peer
from test_sim_shared_world import can_wire


SCENARIO_FIELDS = ("angle", "speed", "current", "temp", "ts", "output", "enabled",
                   "update_age", "feedback_age")
SCENARIO_REQUIRED = {"control.rx", "reset.poweron", "bsp.init", "task.stack", "dji.scenario.stop", "dji.can",
    *(f"dji.scenario.{phase}.id{identifier}.{field}" for phase in (1, 2)
      for identifier in (1, 2) for field in SCENARIO_FIELDS)}


def scenario_sample_rows(phase):
    """Independent copied firmware fields; no production validator/encoder oracle."""
    rows = []
    for identifier, current in ((1, 64), (2, 0xFFFFFFC0)):
        values = {"angle": (0, 8192), "speed": (0, 0), "current": (current, current),
            "temp": (25, 25), "ts": (100 if phase == 1 else 200, 50 if phase == 1 else 150),
            "output": (current, current), "enabled": (1, 1), "update_age": (3, 50), "feedback_age": (4, 50)}
        rows += [dict(status="PASS", name=f"dji.scenario.{phase}.id{identifier}.{field}",
                      actual=values[field][0], expected=values[field][1]) for field in SCENARIO_FIELDS]
    return rows


def scenario_case_rows():
    rows = [dict(status="PASS", name=label, actual=value, expected=expected)
            for label, value, expected in (("control.rx", 0, 0), ("reset.poweron", 0x800000, 0x800000),
                ("bsp.init", 0, 0), ("task.stack", 96, 64))]
    return [*rows, *scenario_sample_rows(1), *scenario_sample_rows(2),
        dict(status="PASS", name="dji.scenario.stop", actual=0, expected=0),
        dict(status="PASS", name="dji.can", actual=0, expected=0),
        dict(status="SKIP", name="plant.physics", actual=0, expected=0)]


def scenario_summaries():
    return [{"kind": kind, "values": [42, 0, 1]} for kind in ("PHASE", "DONE")]


def scenario_empty_report():
    def command_summary():
        return dict(count=0, active_count=0, zero_count=0, transition_count=0,
                    first=None, last=None, transitions=[])
    return {"native_calls": dict(reset=0, step_ns=[], close=0), "feedback": [],
        "can_receive_errors": [], "qmp_events": [], "uart": ["", ""], "ready": {}, "watchdogs": {},
        "cases": {name: [] for name in ("east", "west")},
        "summaries": {name: [] for name in ("east", "west")},
        "responses": {name: dict(count=0, first=None, last=None, reset=None,
            step_ack_count=0, first_step_ack=None, last_step_ack=None, done_count=0, last_done=None)
            for name in ("east", "west")},
        "commands": {name: command_summary() for name in ("east", "west")},
        "command_wire": {name: command_summary() for name in ("east", "west")},
        "command_deliveries": {name: command_summary() for name in ("east", "west")},
        "scenario_observation": dict(shared_start=None, actions=[],
            samples={name: {} for name in ("east", "west")}, stages={}, controls={})}


def scenario_completed_report():
    """Independent verification receipt; never simulates firmware or a scheduler."""
    report = scenario_empty_report()
    evidence = report["scenario_observation"]
    evidence["shared_start"] = dict(session_id=1, origin_ns=0, wire_sessions=[1, 2], accepted=True)
    evidence["actions"] = [dict(logical_ns=0, member="east", delay_ns=5_000_000,
        before_cont=True, accepted=True, pending_sources=[]), dict(logical_ns=250_000_000,
        member="east", delay_ns=0, before_cont=False, accepted=True,
        pending_sources=[dict(device=0, world_ns=247_000_000, current_raw=64,
                              data_hex="0000000000401900")])]
    from test_referee_consumer import arena_response_summary
    for peer, name in enumerate(("east", "west")):
        epoch = (1000, 2000)[peer]
        report["cases"][name], report["summaries"][name] = scenario_case_rows(), scenario_summaries()
        report["responses"][name] = arena_response_summary(peer + 1, epoch)
        report["ready"][name] = dict(logical_ns=0, poll_sample_qemu_ns=epoch)
        report["watchdogs"][name] = {stage: dict(timeouts=0, diagnostics="window-violations=0")
                                     for stage in ("done", "paused")}
        for phase, source_time, delivery_time, logical_time in (("1", 10_000_000,
                15_000_000 if peer == 0 else 10_000_000, 20_000_000),
                ("2", 260_000_000, 260_000_000, 260_000_000)):
            sample = dict(logical_ns=logical_time, poll_sample_qemu_ns=epoch + logical_time, source_matches={})
            evidence["samples"][name][phase] = sample
            for identifier, raw in ((1, "0000000000401900"), (2, "00000000ffc01900")):
                row = dict(peer=peer, can_id=0x200 + identifier, accepted=True,
                    world_ns=source_time, due_ns=delivery_time, delivered_ns=delivery_time,
                    qemu_ns=epoch + delivery_time, data_hex=raw, physical_complete=True)
                row["physical_wire_hex"] = struct.pack("<IIB3xQ64s", row["can_id"], 0, 8,
                    row["qemu_ns"], bytes.fromhex(raw).ljust(64, b"\0")).hex()
                report["feedback"].append(row)
                sample["source_matches"][f"id{identifier}"] = dict(data_hex=raw,
                    source_world_ns=source_time, delivery_ns=delivery_time, qemu_ns=epoch + delivery_time)
        evidence["stages"][name] = dict(logical_ns=20_000_000, poll_sample_qemu_ns=epoch + 20_000_000)
        evidence["controls"][name] = dict(logical_ns=250_000_000, completed_logical_ns=250_000_000,
            wire_hex="a5", accepted=True, completed=True, send_attempts=1, flush_attempts=1,
            borrowed_stream_identity=peer + 1)
        for key in ("commands", "command_wire", "command_deliveries"):
            transitions = [dict(peer=peer, can_id=0x200, flags=0, dlc=8,
                qemu_ns=epoch + stamp, data_hex=raw)
                for stamp, raw in ((0, "0040ffc000000000"), (280_000_000, "0000000000000000"))]
            if key == "command_wire":
                for row in transitions:
                    row["wire_hex"] = struct.pack("<IIB3xQ64s", 0x200, 0, 8, row["qemu_ns"],
                        bytes.fromhex(row["data_hex"]).ljust(64, b"\0")).hex()
            if key == "command_deliveries":
                for row, stamp, destination in zip(transitions, (0, 280_000_000), ("m3508-1", "m3508-4")):
                    row.update(mapped_qemu_ns=row.pop("qemu_ns"), source_world_ns=stamp,
                        due_ns=stamp + (5_000_000 if peer == 0 and stamp == 0 else 0),
                        delivered_ns=stamp + (5_000_000 if peer == 0 and stamp == 0 else 0),
                        destination=destination)
            multiple = 4 if key == "command_deliveries" else 1
            report[key][name] = dict(count=2 * multiple, active_count=multiple, zero_count=multiple,
                transition_count=2, first=transitions[0], last=transitions[-1], transitions=transitions)
    # A retained delayed source follows a newer zero-delay source in actual delivery order.
    for world_time, due_time in ((251_000_000, 251_000_000), (247_000_000, 252_000_000)):
        row = dict(peer=0, can_id=0x201, accepted=True, world_ns=world_time,
            due_ns=due_time, delivered_ns=due_time, qemu_ns=1000 + due_time,
            data_hex="0000000000401900", physical_complete=True)
        row["physical_wire_hex"] = struct.pack("<IIB3xQ64s", 0x201, 0, 8, row["qemu_ns"],
            bytes.fromhex(row["data_hex"]).ljust(64, b"\0")).hex()
        report["feedback"].append(row)
    report["feedback"].sort(key=lambda row: row["delivered_ns"])
    report["native_calls"] = dict(reset=1, step_ns=[1_000_000] * 1000, close=1)
    return report


@pytest.fixture
def dji_scenario_observation(tmp_path, monkeypatch):
    """Original Scenario/core/eight-device/socket owners; only acquisition is isolated."""
    environment = Environment(tmp_path, monkeypatch)
    elf = tmp_path / "same.elf"
    elf.write_bytes(b"fixed input")
    directory = tmp_path / "scenario"
    directory.mkdir()
    scenario = arena_module.write_dji_scenario_manifest(directory, (elf, elf))
    report = scenario_empty_report()
    observer = arena_module.DjiScenarioObservation(report, scenario.arena)
    for original, method in (("create_arena_plant", "world"), ("CanStream", "can"),
                             ("SharedWorldRuntime", "shared"), ("LabChannels", "channels")):
        setattr(observer, "original_" + method, getattr(arena_module.arena, original))
        monkeypatch.setattr(arena_module.arena, original, getattr(observer, method))
    observer.watchdog = lambda index, name, stage: report["watchdogs"].setdefault(name, {}).update(
        {stage: dict(timeouts=0, diagnostics="window-violations=0")})
    def direct():
        plant = observer.world(scenario.arena)
        plant.reset(Session(1, 0))
        streams = []
        for index, bus in enumerate(dict.fromkeys(device.bus for device in plant.devices)):
            bus.reset(0)
            peer = environment.connect(tmp_path / f"peer{index}" / "can1", 1, wrapped_can=True)
            stream = observer.can(peer)
            endpoint = QemuCanEndpoint(bus, stream)
            endpoint.bind_mapper(QemuTimestampMapper((1000, 2000)[index], 0))
            streams.append(stream)
        return plant.devices[0], streams[0]
    try:
        yield SimpleNamespace(environment=environment, scenario=scenario, report=report,
                              observer=observer, direct=direct)
    finally:
        environment.release()


class Sink:
    def __init__(self):
        self.frames = []

    def send(self, ident, flags, dlc, stamp, data):
        self.frames.append((ident, flags, dlc, stamp, data))


def command(ident, stamp, raw=0):
    return module.CanFrame(ident, data=struct.pack(">hhhh", raw, 0, 0, 0),
                           timestamp_ns=stamp)


def test_two_groups_advance_one_tick_and_vectors_use_normal_fabric():
    sink = Sink()
    fixture = module.DjiFixture(sink)
    fixture.ingress(command(0x200, 1_010_000))
    fixture.ingress(command(0x1FF, 1_020_000))
    assert fixture.steps == 1
    assert fixture.runtime.t_sim_ns == 1_000_000
    assert sink.frames == []  # startup suppresses normal feedback only
    fixture.ready("dji.vector", 0x201, 0x205)
    assert sink.frames == [
        (0x201, 0, 8, 1_000_000, bytes.fromhex("0800ff40e0002a00")),
        (0x205, 0, 8, 1_000_000, bytes.fromhex("100000c020002b00")),
    ]
    fixture.ready("dji.start", 0, 0)
    fixture.ingress(command(0x200, 2_010_000))
    fixture.ingress(command(0x1FF, 2_020_000))
    assert fixture.steps == 2
    assert [frame[0] for frame in sink.frames[-2:]] == [0x201, 0x205]
    fixture.close()


def test_missing_zero_timeout_wire_cannot_pass_on_firmware_assertion_alone():
    fixture = module.DjiFixture(Sink())
    fixture.stage = "dji.drop"
    fixture.wire_seen[fixture.stage][0x200].add(4096)
    fixture.wire_seen[fixture.stage][0x1FF].add(4096)
    with pytest.raises(RuntimeError, match="slot=0"):
        fixture._finish_stage()
    fixture.close()


def test_early_transition_zero_cannot_prove_strict_feedback_timeout():
    fixture = module.DjiFixture(Sink())
    fixture.stage = "dji.drop"
    fixture.phase_start_ns[fixture.stage] = 0
    fixture.ingress(command(0x200, 1_010_000))
    fixture.ingress(command(0x1FF, 1_020_000, 4096))
    with pytest.raises(RuntimeError, match="beyond 51ms"):
        fixture._finish_stage()
    fixture.ingress(command(0x200, 60_010_000))
    fixture._finish_stage()
    fixture.close()


def test_time_gap_and_reverse_reject_before_more_device_steps():
    fixture = module.DjiFixture(Sink())
    fixture.ingress(command(0x200, 1_500_000))
    with pytest.raises(RuntimeError, match="backwards"):
        fixture.ingress(command(0x1FF, 1_499_999))
    with pytest.raises(RuntimeError, match="catch-up"):
        fixture.ingress(command(0x200, 1_002_000_000))
    assert fixture.steps == 1
    fixture.close()


def test_non_equality_timeout_reports_and_skip_are_checked():
    module.check_case(dict(status="PASS", name="dji.drop.age", actual=86, expected=51))
    module.check_case(dict(status="PASS", name="dji.drop.app_age", actual=3, expected=50))
    module.check_case(dict(status="SKIP", name="plant.physics", actual=0, expected=0))
    with pytest.raises(RuntimeError, match="fields disagree"):
        module.check_case(dict(status="PASS", name="dji.drop.age", actual=50, expected=51))
    with pytest.raises(RuntimeError, match="SKIP"):
        module.check_case(dict(status="SKIP", name="dji.drop.output", actual=0, expected=0))


def test_harness_failure_replaces_prior_pass_report(tmp_path):
    # Isolate only process acquisition: no mock CAN/model/protocol state machine.
    python = tmp_path / "python"
    python.write_text("#!/usr/bin/env bash\n"
                      'if [[ "$1" == *dm_mc02_test_harness.py ]]; then exit 7; fi\n'
                      f"exec {shlex.quote(sys.executable)} \"$@\"\n")
    python.chmod(0o755)
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"fixture")
    report = tmp_path / "report.json"
    report.write_text('{"result":"passed"}\n')
    env = dict(os.environ, PYTHON=str(python), QEMU_SYSTEM_ARM="/bin/true")
    result = subprocess.run(
        ["bash", str(PATH.with_name("run-trobot-dji-test.sh")), str(elf), str(report)],
        env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 7
    actual = json.loads(report.read_text())
    assert actual["result"] == "failed"
    assert actual["harness_status"] == 7
    assert actual["cases"] == []
    assert "harness/cleanup failed" in actual["error"]


def test_dji_scenario_case_contract_has_exact_literal_coverage_and_only_physics_skip():
    assert arena_module.DJI_SCENARIO_REQUIRED == SCENARIO_REQUIRED and len(SCENARIO_REQUIRED) == 42
    rows = scenario_case_rows()
    for row in rows:
        arena_module.check_dji_scenario_case(row)
    arena_module.check_dji_scenario_coverage(rows, scenario_summaries())


@pytest.mark.parametrize("label,field,value", [
    ("dji.scenario.1.id1.current", "actual", 65),
    ("dji.scenario.1.id2.current", "expected", 64),
    ("dji.scenario.1.id1.angle", "actual", 8192),
    ("dji.scenario.1.id1.angle", "expected", 0),
    ("dji.scenario.1.id1.speed", "actual", 32768),
    ("dji.scenario.1.id1.speed", "actual", 0xFFFF7FFF),
    ("dji.scenario.1.id1.temp", "actual", 24),
    ("dji.scenario.1.id1.ts", "actual", 0),
    ("dji.scenario.1.id1.ts", "expected", 100),
    ("dji.scenario.1.id1.update_age", "actual", 50),
    ("dji.scenario.1.id1.feedback_age", "expected", 49),
    ("task.stack", "actual", 63),
    ("dji.can", "actual", 1),
    ("control.rx", "actual", True),
    ("control.rx", "expected", -1),
    ("control.rx", "actual", 1 << 32),
])
def test_dji_scenario_case_rejects_fixed_contract_or_uint32_violation(label, field, value):
    row = next(row for row in scenario_case_rows() if row["name"] == label)
    arena_module.check_dji_scenario_case(row)
    with pytest.raises((ValueError, TypeError)):
        arena_module.check_dji_scenario_case(dict(row, **{field: value}))


@pytest.mark.parametrize("corruption", ["unknown", "fail", "unexpected-skip", "missing-key", "extra-key"])
def test_dji_scenario_case_rejects_status_label_and_shape(corruption):
    row = dict(status="PASS", name="control.rx", actual=0, expected=0)
    if corruption == "unknown":
        row["name"] = "dji.other"
    elif corruption == "fail":
        row["status"] = "FAIL"
    elif corruption == "unexpected-skip":
        row["status"] = "SKIP"
    elif corruption == "missing-key":
        del row["expected"]
    else:
        row["surprise"] = 0
    with pytest.raises((ValueError, TypeError)):
        arena_module.check_dji_scenario_case(row)


@pytest.mark.parametrize("corruption", ["missing-case", "duplicate-case", "missing-skip", "duplicate-skip",
    "missing-summary", "wrong-summary", "duplicate-summary", "second-not-new"])
def test_dji_scenario_coverage_rejects_missing_duplicate_summary_and_stale_samples(corruption):
    rows, summaries = scenario_case_rows(), scenario_summaries()
    if corruption == "missing-case":
        rows.pop(0)
    elif corruption == "duplicate-case":
        rows.append(rows[0])
    elif corruption == "missing-skip":
        rows.pop()
    elif corruption == "duplicate-skip":
        rows.append(rows[-1])
    elif corruption == "missing-summary":
        summaries.pop()
    elif corruption == "wrong-summary":
        summaries[1]["values"] = [41, 0, 1]
    elif corruption == "duplicate-summary":
        summaries.append(summaries[0])
    else:
        for row in rows:
            if row["name"] == "dji.scenario.2.id1.ts":
                row.update(actual=100, expected=50)
    with pytest.raises((ValueError, TypeError)):
        arena_module.check_dji_scenario_coverage(rows, summaries)


def test_dji_scenario_manifest_loads_same_elf_fixed_actions_duration_and_motor_ids(tmp_path, monkeypatch):
    elf = tmp_path / "input.elf"
    elf.write_bytes(b"fixed firmware")
    directory = tmp_path / "scenario"
    directory.mkdir()
    def forbidden(*args, **options):
        pytest.fail("pure DJI Scenario producer reached execution/resource acquisition")
    for method in ("resolve_qemu", "create_arena_plant", "running_qemu", "connect_unix"):
        monkeypatch.setattr(arena_module.arena, method, forbidden)
    scenario = arena_module.write_dji_scenario_manifest(directory, (elf, elf))
    assert load_scenario(scenario.path) == scenario
    assert scenario.duration_ns == 1_000_000_000 and scenario.referees == ()
    assert scenario.can_delays == (ScenarioCanDelay(0, "east", 5_000_000),
                                   ScenarioCanDelay(250_000_000, "east", 0))
    assert [item.name for item in scenario.arena.robots] == ["east", "west"]
    assert all(item.robot.board.firmware == elf.resolve() and item.robot.bus.name == "drive"
               and not item.robot.upper_computers for item in scenario.arena.robots)
    assert all(item.robot.ready.endpoint == "mcu.uart1" and item.robot.ready.text ==
               b"TTEST READY dji.scenario.ready 0 0\n" for item in scenario.arena.robots)
    assert all([(device.motor_id, device.joint) for device in item.robot.devices] ==
               [(1, "wheel_fl"), (2, "wheel_fr"), (3, "wheel_rl"), (4, "wheel_rr")]
               for item in scenario.arena.robots)


@pytest.fixture
def dji_scenario_completed_owner(tmp_path, monkeypatch):
    """A completed acquisition receipt isolates verification, without a runtime."""
    monkeypatch.setattr(arena_module, "starttime", lambda pid: None)
    sockets = [(str(tmp_path / f"removed-{peer}-{name}"), SimpleNamespace(fileno=lambda: -1))
               for peer in range(2) for name in ("uart1", "can1", "cosim")]
    controls = {name: SimpleNamespace(sock=sockets[index * 3][1])
                for index, name in enumerate(("east", "west"))}
    bindings = [SimpleNamespace(name=name, cosim=SimpleNamespace(sock=sockets[index * 3 + 2][1]),
                endpoint=SimpleNamespace(stream=SimpleNamespace(sock=sockets[index * 3 + 1][1])))
                for index, name in enumerate(("east", "west"))]
    owner = SimpleNamespace(owners=[SimpleNamespace(process=SimpleNamespace(poll=lambda: 0)) for _ in range(2)],
        children=[], sockets=sockets, controls=controls,
        manifest=SimpleNamespace(robots=[SimpleNamespace(name=name) for name in ("east", "west")]),
        plant=SimpleNamespace(closed=True, devices=[SimpleNamespace(closed=True) for _ in range(8)]),
        runtime=SimpleNamespace(closed=True, bindings=bindings))
    report = {"cleanup": [dict(pid=987654 + index, starttime=1, tempdir=str(tmp_path / f"removed-{index}"),
               sockets={"qmp": str(tmp_path / f"removed-{index}.qmp")}) for index in range(2)],
              "qmp_cleanup": [dict(attempts=1, completed=True) for _ in range(2)]}
    return owner, report


@pytest.mark.parametrize("corruption", [None, "missing-owner", "qmp-close", "socket-open", "device-open",
                                        "runtime-open", "control-scope", "upper-child"])
def test_dji_scenario_shared_cleanup_gate_requires_every_owned_and_borrowed_resource(
        dji_scenario_completed_owner, corruption):
    owner, report = dji_scenario_completed_owner
    if corruption == "missing-owner":
        owner.owners.pop()
    elif corruption == "qmp-close":
        report["qmp_cleanup"][1]["completed"] = False
    elif corruption == "socket-open":
        owner.sockets[0][1].fileno = lambda: 3
    elif corruption == "device-open":
        owner.plant.devices[7].closed = False
    elif corruption == "runtime-open":
        owner.runtime.closed = False
    elif corruption == "control-scope":
        del owner.controls["west"]
    elif corruption == "upper-child":
        owner.children.append(SimpleNamespace())
    if corruption is None:
        arena_module.verify_firmware_cleanup(report, owner, label="DJI Scenario", controls=True)
        assert report["cleanup_verified"] and all(report["owned_cleanup"]["devices"])
    else:
        with pytest.raises(ValueError, match="cleanup incomplete"):
            arena_module.verify_firmware_cleanup(report, owner, label="DJI Scenario", controls=True)
        assert not report.get("cleanup_verified")
        assert len(report["owned_cleanup"]["devices"]) == 8  # Independent checks continue after the first failure.


@pytest.mark.parametrize("corruption", [None, "input", "checkout", "source", "missing-before"])
def test_dji_scenario_shared_source_gate_rejects_changed_or_missing_receipts(tmp_path, monkeypatch, corruption):
    from tools import dm_mc02_referee_test as source_module
    report = {"inputs_before": {"fixed.elf": "original"}, "checkout_before": {},
              "actual_sources_before": {"sim.core": "fixed"}}
    monkeypatch.setattr(source_module, "checkout_snapshot", lambda directory: {"changed": True}
                        if corruption == "checkout" else {})
    monkeypatch.setattr(arena_module, "source_origins", lambda: {"sim.core": "changed"}
                        if corruption == "source" else {"sim.core": "fixed"})
    if corruption == "missing-before":
        del report["inputs_before"]
    inputs = lambda: {"fixed.elf": "changed" if corruption == "input" else "original"}
    if corruption is None:
        arena_module.verify_firmware_sources(report, inputs, tmp_path, label="DJI Scenario")
        assert report["source_preserved"]
    else:
        with pytest.raises(ValueError, match="state changed|state unavailable"):
            arena_module.verify_firmware_sources(report, inputs, tmp_path, label="DJI Scenario")
        assert not report.get("source_preserved")


@pytest.mark.parametrize("corruption", [None, "missing", "timeout", "window"])
def test_dji_scenario_shared_watchdog_gate_requires_both_complete_native_observations(corruption):
    report = {"watchdogs": {name: {stage: dict(timeouts=0, diagnostics="window-violations=0")
        for stage in ("done", "paused")} for name in ("east", "west")}}
    if corruption == "missing":
        del report["watchdogs"]["west"]["paused"]
    elif corruption == "timeout":
        report["watchdogs"]["west"]["paused"]["timeouts"] = 1
    elif corruption == "window":
        report["watchdogs"]["west"]["paused"]["diagnostics"] = "window-violations=1"
    if corruption is None:
        arena_module.verify_firmware_watchdogs(report, label="DJI Scenario")
    else:
        with pytest.raises(ValueError, match="watchdog"):
            arena_module.verify_firmware_watchdogs(report, label="DJI Scenario")


def scenario_uart_rows(rows):
    return "".join(f"TTEST {row['status']} {row['name']} {row['actual']} {row['expected']}\n"
                   for row in rows).encode("ascii")


@pytest.fixture
def dji_scenario_consumed(dji_scenario_observation):
    fixture = dji_scenario_observation
    environment, observer, report = fixture.environment, fixture.observer, fixture.report
    environment.can_commands = ["0040ffc000000000"] * 2
    environment.uart_chunks = [[b"TTEST READY dji.scenario.ready 0 0\n"] for _ in range(2)]
    controls = [bytearray(), bytearray()]
    published = set()
    # Fixed 10ms command cadence belongs to the native interval producer, not
    # arbitrary QMP queries. Initial literal commands emit in interval1.
    for index, origin in enumerate((1000, 2000)):
        for now in range(10_000_000, 1_000_000_000, 10_000_000):
            raw = "0040ffc000000000" if now < 280_000_000 else "0000000000000000"
            environment.schedule_can(index, can_wire(origin + now, raw),
                                     interval=now // 1_000_000 + 1)
    def service(index, operation):
        now = sum(environment.native.steps)
        if operation == "x-run-to-virtual-time" and not environment.cpus[index].requests:
            observer.check_cont()
            report["qmp_events"].append(dict(peer=index, command=operation))
        if operation != "qom-get":
            return
        if (index, "uart1") in environment.producers:
            try:
                controls[index].extend(environment.producers[index, "uart1"].recv(4096))
            except BlockingIOError:
                pass
        if now == 20_000_000 and (index, 1) not in published:
            published.add((index, 1))
            rows = [row for row in scenario_case_rows() if row["name"] in
                    ("control.rx", "reset.poweron", "bsp.init", "task.stack")]
            environment.uart_chunks[index].append(scenario_uart_rows(rows + scenario_sample_rows(1)) +
                                                  b"TTEST READY dji.scenario.observe 0 0\n")
        if now == 260_000_000 and (index, 2) not in published:
            assert controls[index] == b"\xa5"  # Actual suffix reached the producer before phase2.
            published.add((index, 2))
            rows = [row for row in scenario_case_rows() if row["name"] in
                    ("dji.scenario.stop", "dji.can", "plant.physics")]
            environment.uart_chunks[index].append(scenario_uart_rows(scenario_sample_rows(2) + rows) +
                                                  b"TTEST PHASE 42 0 1\nTTEST DONE 42 0 1\n")
    environment.qmp_callback = service
    result = scenario_runtime.run_manifest(fixture.scenario, qemu=environment.binary,
        startup_timeout=10., timeout=20., emit=observer.emit, on_response=observer.response)
    environment.assert_clean()
    for name in ("east", "west"):
        arena_module.check_dji_scenario_coverage(report["cases"][name], report["summaries"][name])
    arena_module.check_dji_scenario_observations(report)
    # NativeCpuQmp is the only CAN input reader; this exposes its actual bytes.
    received = [environment.cpus[index].received for index in range(2)]
    return SimpleNamespace(**vars(fixture), result=result, received=received, controls=controls)


def test_dji_scenario_actual_one_second_eight_device_socket_consumer_keeps_epochs_and_pending_sources(
        dji_scenario_consumed):
    fixture = dji_scenario_consumed
    result, report, observer = fixture.result, fixture.report, fixture.observer
    assert (result.origin_ns, result.end_ns, result.steps) == (0, 1_000_000_000, 1000)
    assert [row.reset_qemu_ns for row in result.boards] == [1000, 2000]
    assert report["native_calls"] == dict(reset=1, step_ns=[1_000_000] * 1000, close=1)
    assert len(observer.plant.devices) == 8 and observer.runtime.closed and observer.plant.closed
    assert all(not sources for sources in observer.feedback_sources.values())
    assert observer.feedback_deliveries == {0: None, 1: None}
    assert all(binding.endpoint.bus.delay_ns == 0 and not binding.endpoint.stream.tx_pending
               for binding in observer.runtime.bindings)
    assert report["qmp_events"] == [dict(peer=index, command="x-run-to-virtual-time") for index in range(2)]
    assert report.get("first_rejected_command") is None
    evidence = report["scenario_observation"]
    assert [(row["logical_ns"], row["member"], row["delay_ns"], row["before_cont"])
            for row in evidence["actions"]] == [(0, "east", 5_000_000, True), (250_000_000, "east", 0, False)]
    assert len(evidence["actions"][1]["pending_sources"]) == 20
    assert all(245_000_000 < row["world_ns"] <= 250_000_000
               for row in evidence["actions"][1]["pending_sources"])
    for index, name in enumerate(("east", "west")):
        cpu = fixture.environment.cpus[index]
        binding = observer.runtime.bindings[index]
        assert binding.endpoint.stream.sock is fixture.environment.sockets[index, "can1"]
        assert type(binding.endpoint.stream.sock) is socket.socket
        assert len(cpu.requests) == 1000 and cpu.requests[0] == (1000, 2000)[index] + 1_000_000
        assert cpu.requests[-1] == (1000, 2000)[index] + 1_000_000_000
        assert [evidence["samples"][name][phase]["logical_ns"] for phase in ("1", "2")] == [20_000_000, 260_000_000]
        assert evidence["stages"][name]["logical_ns"] == 20_000_000
        control = evidence["controls"][name]
        assert (control["logical_ns"], control["completed_logical_ns"], control["send_attempts"]) == (250_000_000, 250_000_000, 1)
        assert control["borrowed_stream_identity"] == id(observer.controls[name])
        assert observer.controls[name].sock is fixture.environment.sockets[index, "uart1"]
        assert fixture.controls[index] == b"\xa5"
        assert report["commands"][name]["count"] == report["command_wire"][name]["count"]
        for group in ("commands", "command_wire"):
            assert tuple(report[group][name][key] for key in ("count", "active_count", "zero_count")) == (100, 28, 72)
        delivery = report["command_deliveries"][name]["first"]
        assert (delivery["source_world_ns"], delivery["due_ns"], delivery["delivered_ns"],
                delivery["mapped_qemu_ns"], delivery["data_hex"]) == (
            0, 5_000_000 if index == 0 else 0, 5_000_000 if index == 0 else 0,
            (1000, 2000)[index], "0040ffc000000000")
        assert report["command_deliveries"][name]["zero_count"] > 0
        for row in report["command_wire"][name]["transitions"]:
            expected = struct.pack("<IIB3xQ64s", 0x200, 0, 8, row["qemu_ns"],
                                   bytes.fromhex(row["data_hex"]).ljust(64, b"\0"))
            assert bytes.fromhex(row["wire_hex"]) == expected
        rows = [row for row in report["feedback"] if row["peer"] == index]
        expected = b"".join(struct.pack("<IIB3xQ64s", row["can_id"], 0, 8,
            (1000, 2000)[index] + row["delivered_ns"],
            bytes.fromhex(row["data_hex"]).ljust(64, b"\0")) for row in rows)
        assert bytes(fixture.received[index]) == expected
        assert cpu.completed == len(cpu.receipts) == len(rows) == 4000 and not cpu.partial
        assert b"".join(cpu.receipts) == expected
        assert all(bytes.fromhex(row["physical_wire_hex"]) == expected[offset * 84:(offset + 1) * 84]
                   for offset, row in enumerate(rows))
    east = [row for row in report["feedback"] if row["peer"] == 0 and row["can_id"] == 0x201]
    assert [(row["world_ns"], row["due_ns"], row["delivered_ns"]) for row in east if
            249_000_000 <= row["delivered_ns"] <= 253_000_000] == [
        (244_000_000, 249_000_000, 249_000_000), (245_000_000, 250_000_000, 250_000_000),
        (246_000_000, 251_000_000, 251_000_000), (251_000_000, 251_000_000, 251_000_000),
        (247_000_000, 252_000_000, 252_000_000), (252_000_000, 252_000_000, 252_000_000),
        (248_000_000, 253_000_000, 253_000_000), (253_000_000, 253_000_000, 253_000_000)]


def test_dji_scenario_actual_prefix_admission_requires_exact_original_suffix_completion(
        dji_scenario_observation, monkeypatch):
    fixture = dji_scenario_observation
    device, stream = fixture.direct()
    original_socket = fixture.environment.sockets[0, "can1"]
    assert stream.sock is original_socket
    fixture.environment.uart_send_budget[0, "can1"] = 17
    frame = device.feedback()
    device.bus.send(device, frame)
    assert device.bus.step(0) == 1
    expected = struct.pack("<IIB3xQ64s", 0x201, 0, 8, 1000,
                           bytes.fromhex("0000000000001900").ljust(64, b"\0"))
    row, = fixture.report["feedback"]
    assert row["accepted"] and not row.get("physical_complete") and stream.tx_pending
    assert fixture.environment.producers[0, "can1"].recv(4096) == expected[:17]
    assert [bytes(part) for part in stream.tx_queue] == [expected[17:]]
    assert fixture.observer.written[0].suffix == expected[:17]
    primary = OSError("original CAN suffix write failed")
    with monkeypatch.context() as failing:
        def reject(peer, data):
            raise primary
        failing.setattr(Peer, "send", reject)
        with pytest.raises(OSError) as caught:
            stream.flush()
        assert caught.value is primary
    assert stream.sock is original_socket
    assert [bytes(part) for part in stream.tx_queue] == [expected[17:]]
    assert fixture.observer.written[0].suffix == expected[:17] and not row.get("physical_complete")
    fixture.environment.uart_send_budget[0, "can1"] = 67
    assert stream.flush() and not stream.tx_pending
    assert fixture.environment.producers[0, "can1"].recv(4096) == expected[17:]
    assert row["physical_complete"] and bytes.fromhex(row["physical_wire_hex"]) == expected
    assert stream.sock is original_socket
    assert len(fixture.report["feedback"]) == 1 and not fixture.observer.written[0].suffix


@pytest.mark.parametrize("corruption", ["session", "initial-after-cont", "no-pending", "late-first",
    "early-stage", "unaccepted-control", "incomplete-control", "duplicate-control", "early-second",
    "raw-speed", "unadmitted-source", "other-peer", "no-disabled-command", "command-capacity",
    "physical-prefix", "physical-suffix", "wrong-epoch", "west-delay", "no-overtake"])
def test_dji_scenario_observation_receipt_rejects_causal_raw_wire_and_scope_failures(
        corruption):
    report = scenario_completed_report()
    arena_module.check_dji_scenario_observations(report)
    evidence = report["scenario_observation"]
    if corruption == "session":
        evidence["shared_start"]["wire_sessions"] = [1, 1]
    elif corruption == "initial-after-cont":
        evidence["actions"][0]["before_cont"] = False
    elif corruption == "no-pending":
        evidence["actions"][1]["pending_sources"] = []
    elif corruption == "late-first":
        evidence["samples"]["west"]["1"]["logical_ns"] = 250_000_000
    elif corruption == "early-stage":
        evidence["stages"]["east"]["logical_ns"] = 19_999_999
    elif corruption in ("unaccepted-control", "incomplete-control", "duplicate-control"):
        key, value = {"unaccepted-control": ("accepted", False), "incomplete-control": ("completed", False),
                      "duplicate-control": ("send_attempts", 2)}[corruption]
        evidence["controls"]["west"][key] = value
    elif corruption == "early-second":
        evidence["samples"]["west"]["2"]["logical_ns"] = 249_999_999
    elif corruption == "raw-speed":
        next(row for row in report["cases"]["west"] if row["name"] == "dji.scenario.2.id2.speed")["actual"] = 1
    elif corruption in ("unadmitted-source", "other-peer"):
        match = evidence["samples"]["west"]["2"]["source_matches"]["id2"]
        if corruption == "unadmitted-source":
            match["source_world_ns"] += 1
        else:
            match["qemu_ns"] -= 1000
    elif corruption == "no-disabled-command":
        report["command_wire"]["west"]["zero_count"] = 0
    elif corruption == "command-capacity":
        report["commands"]["west"]["count"] = 10001
    elif corruption == "physical-prefix":
        report["feedback"][0]["physical_complete"] = False
    elif corruption == "physical-suffix":
        report["physical_can_suffixes"] = [dict(peer=0, suffix_hex="0102")]
    elif corruption == "wrong-epoch":
        report["feedback"][0]["qemu_ns"] += 1
    elif corruption == "west-delay":
        next(row for row in report["feedback"] if row["peer"] == 1)["due_ns"] += 1
    else:
        report["feedback"].sort(key=lambda row: row["world_ns"])
    with pytest.raises(ValueError, match="DJI Scenario"):
        arena_module.check_dji_scenario_observations(report)


def test_dji_scenario_borrowed_control_retries_only_flush_and_blocks_second_report_until_complete(
        dji_scenario_observation):
    fixture = dji_scenario_observation
    observer, environment, evidence = fixture.observer, fixture.environment, fixture.report["scenario_observation"]
    observer.runtime = SimpleNamespace(t_sim_ns=250_000_000)
    evidence["actions"] = [dict(accepted=True), dict(accepted=True)]
    for index, name in enumerate(("east", "west")):
        peer = environment.connect(environment.directory / f"peer{index}" / "uart1", 1)
        stream = StreamTransport(peer, max_pending=1)
        channels = observer.channels(uarts={"uart1": stream})
        assert channels is not None and observer.controls[name] is stream
        observer.emit(arena_module.arena.ArenaEvent(name, ChannelEvent("uart1", 1000 + index,
            b"TTEST READY dji.scenario.ready 0 0\n")))
        evidence["samples"][name]["1"] = dict(logical_ns=20_000_000)
        evidence["stages"][name] = dict(logical_ns=20_000_000)
        environment.uart_send_budget[index, "uart1"] = 0
    observer.service_control()
    assert all(row["accepted"] and not row["completed"] and row["send_attempts"] == 1
               for row in evidence["controls"].values())
    with pytest.raises(ValueError, match="preceded observation control"):
        observer.emit(arena_module.arena.ArenaEvent("west", ChannelEvent("uart1", 777,
            scenario_uart_rows(scenario_sample_rows(2)[:1]))))
    observer.service_control()
    for index, name in enumerate(("east", "west")):
        with pytest.raises(BlockingIOError):
            environment.producers[index, "uart1"].recv(4096)
        environment.uart_send_budget[index, "uart1"] = 1
    observer.service_control()
    observer.service_control()
    for index, name in enumerate(("east", "west")):
        row = evidence["controls"][name]
        assert row["completed"] and row["send_attempts"] == 1 and row["flush_attempts"] == 3
        assert environment.producers[index, "uart1"].recv(4096) == b"\xa5"
        with pytest.raises(BlockingIOError):
            environment.producers[index, "uart1"].recv(4096)
        assert not observer.controls[name].tx_pending


def test_dji_scenario_command_samples_first_eight_changes_and_keeps_exact_count_and_last(
        dji_scenario_observation):
    fixture = dji_scenario_observation
    active, zero = "0040ffc000000000", "0000000000000000"
    rows = [dict(peer=0, can_id=0x200, flags=0, dlc=8, qemu_ns=1000 + index, data_hex=raw)
            for index, raw in enumerate((active, zero, active, zero, active, zero, active, zero, active))]
    for row in rows:
        fixture.observer.command(0, 0x200, 0, 8, row["qemu_ns"], bytes.fromhex(row["data_hex"]))
    summary = fixture.report["commands"]["east"]
    assert (summary["count"], summary["transition_count"], summary["active_count"], summary["zero_count"]) == (9, 9, 5, 4)
    assert summary["transitions"] == rows[:8] and summary["first"] == rows[0] and summary["last"] == rows[8]
    fixture.observer.command(0, 0x200, 0, 8, 2000, bytes.fromhex(active))
    assert summary["count"] == 10 and summary["transition_count"] == 9
    assert summary["transitions"] == rows[:8]
    assert summary["last"] == dict(rows[8], qemu_ns=2000)
    before = deepcopy(summary)
    with pytest.raises(ValueError, match="literal CURRENT profile"):
        fixture.observer.command(0, 0x200, 0, 8, 3000, bytes.fromhex("0041ffc000000000"))
    assert summary == before and fixture.report["first_rejected_command"]["field"] == "data_hex"


def test_dji_scenario_one_ms_fabric_commands_overtake_old_zero_after_fixed_delay_removal(
        dji_scenario_observation):
    fixture = dji_scenario_observation
    environment, observer, report = fixture.environment, fixture.observer, fixture.report
    active, zero = "0040ffc000000000", "0000000000000000"
    environment.can_commands = [zero, zero]
    environment.uart_chunks = [[b"TTEST READY dji.scenario.ready 0 0\n"] for _ in range(2)]
    for index, origin in enumerate((1000, 2000)):
        for millisecond in range(1, 260):
            raw = active if 100 <= millisecond < 120 or millisecond >= 251 else zero
            environment.schedule_can(index, can_wire(origin + millisecond * 1_000_000, raw),
                                     interval=millisecond + 1)
    def first_execution(index, name):
        if name == "x-run-to-virtual-time" and not environment.cpus[index].requests:
            observer.check_cont()
            report["qmp_events"].append(dict(peer=index, command=name))
    environment.qmp_callback = first_execution
    result = environment.run(fixture.scenario.arena, duration=.260,
        can_delays=((0, "east", 5_000_000), (250_000_000, "east", 0)),
        emit=observer.emit, on_response=observer.response)
    environment.assert_clean()
    assert result.steps == 260 and result.origin_ns == 0 and result.end_ns == 260_000_000
    assert report.get("first_rejected_command") is None
    assert [(row["logical_ns"], row["delay_ns"]) for row in report["scenario_observation"]["actions"]] == [
        (0, 5_000_000), (250_000_000, 0)]
    for group in ("commands", "command_wire"):
        summary = report[group]["east"]
        assert (summary["count"], summary["transition_count"]) == (260, 4)
        assert [(row["qemu_ns"] - 1000, row["data_hex"]) for row in summary["transitions"]] == [
            (0, zero), (100_000_000, active), (120_000_000, zero), (251_000_000, active)]
    summary = report["command_deliveries"]["east"]
    assert (summary["count"], summary["transition_count"], len(summary["transitions"])) == (1040, 10, 8)
    assert [(row["source_world_ns"], row["delivered_ns"], row["data_hex"])
            for row in summary["transitions"]] == [
        (0, 5_000_000, zero), (100_000_000, 105_000_000, active), (120_000_000, 125_000_000, zero),
        (251_000_000, 251_000_000, active), (247_000_000, 252_000_000, zero),
        (252_000_000, 252_000_000, active), (248_000_000, 253_000_000, zero),
        (253_000_000, 253_000_000, active)]
    assert (summary["last"]["source_world_ns"], summary["last"]["delivered_ns"],
            summary["last"]["data_hex"], summary["last"]["destination"]) == (
                259_000_000, 259_000_000, active, "m3508-4")
    assert report["command_deliveries"]["west"]["transition_count"] == 4
    assert all(cpu.completed == len(cpu.receipts) == 1040 and not cpu.partial
               for cpu in environment.cpus.values())


@pytest.mark.parametrize("invalid", ["flags", "dlc", "unused-slot", "id", "count"])
def test_dji_scenario_command_admission_rejects_literal_profile_and_observation_capacity(
        dji_scenario_observation, invalid):
    fixture = dji_scenario_observation
    ident, flags, dlc, raw = 0x200, 0, 8, bytes.fromhex("0040ffc000000000")
    summary = fixture.report["commands"]["east"]
    if invalid == "flags":
        flags = 1
    elif invalid == "dlc":
        dlc = 7
    elif invalid == "unused-slot":
        raw = bytes.fromhex("0040ffc000010000")
    elif invalid == "id":
        ident = 0x1ff
    elif invalid == "count":
        summary["count"] = 10000
    wire = struct.pack("<IIB3xQ64s", ident, flags, dlc, 2000, raw.ljust(64, b"\0"))
    if invalid == "count":
        with pytest.raises((ValueError, BufferError)):
            fixture.observer.command(0, ident, flags, dlc, 2000, raw)
    else:
        _, stream = fixture.direct()
        assert fixture.environment.producers[0, "can1"].send(wire) == 84
        with pytest.raises(ValueError):
            stream.receive()
    rejected = fixture.report["first_rejected_command"]
    assert rejected["member"] == "east"
    assert rejected["field"] == {"flags": "flags", "dlc": "dlc", "unused-slot": "data_hex",
        "id": "data_hex", "count": "observation_capacity"}[invalid]
    if invalid == "count":
        assert rejected["layer"] == "CAN decoded ingress" and rejected["wire_hex"] is None
    else:
        assert rejected["layer"] == "CAN records before codec"
        assert rejected["wire_hex"] == rejected["row"]["wire_hex"] == wire.hex()
        assert (rejected["row"]["can_id"], rejected["row"]["flags"], rejected["row"]["dlc"],
                rejected["row"]["qemu_ns"], rejected["row"]["data_hex"]) == (
                    ident, flags, dlc, 2000, raw.hex())
    saved = deepcopy(rejected)
    with pytest.raises(ValueError):
        fixture.observer.command(1, 0x201, 0, 8, 3000, bytes(8))
    assert fixture.report["first_rejected_command"] is rejected and rejected == saved
    assert len(summary["transitions"]) <= 8


@pytest.mark.parametrize("corruption", ["partial", "future", "peer", "raw"])
def test_dji_scenario_copied_sample_requires_completed_same_peer_source_before_observation(
        dji_scenario_observation, corruption):
    fixture = dji_scenario_observation
    observer = fixture.observer
    observer.report = scenario_completed_report()
    observer.report["cases"]["east"] = []
    observer.report["scenario_observation"]["samples"]["east"] = {}
    observer.runtime = SimpleNamespace(t_sim_ns=20_000_000)
    rows = scenario_sample_rows(1)
    for source in observer.report["feedback"]:
        if source["peer"] == 0:
            if corruption == "partial":
                source["physical_complete"] = False
            elif corruption == "future":
                source["delivered_ns"] = 20_000_001
            elif corruption == "peer":
                source["peer"] = 1
    if corruption == "raw":
        next(row for row in rows if row["name"].endswith("id1.speed"))["actual"] = 1
    with pytest.raises(ValueError, match="no admitted same-peer feedback"):
        observer.emit(arena_module.arena.ArenaEvent("east", ChannelEvent("uart1", 12345,
                                                                      scenario_uart_rows(rows))))


@pytest.mark.parametrize("mode", ["success", "missing-case", "source", "cleanup", "error", "interrupt"])
def test_dji_scenario_runner_publishes_only_after_cleanup_and_preserves_primary_and_sources(
        dji_scenario_completed_owner, tmp_path, monkeypatch, mode):
    """One actual Scenario wrapper consumes an isolated completed Arena receipt."""
    from tools import dm_mc02_dji_test as dji_sources
    from tools import dm_mc02_referee_test as referee_sources
    owner, cleanup_report = dji_scenario_completed_owner
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"fixed input")
    for relative in ("Core/Src/fdcan.c", "Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_fdcan.c",
        "Middlewares/Third_Party/FreeRTOS/Source/tasks.c",
        "Middlewares/Third_Party/FreeRTOS/Source/portable/GCC/ARM_CM4F/port.c"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixed dependency")
    inputs = lambda *args: {str(elf): hashlib.sha256(elf.read_bytes()).hexdigest()}
    monkeypatch.setattr(dji_sources, "source_snapshot", inputs)
    monkeypatch.setattr(referee_sources, "source_snapshot", inputs)
    monkeypatch.setattr(referee_sources, "checkout_snapshot", lambda firmware: {})
    monkeypatch.setattr(arena_module, "source_origins", lambda: {"sim.core": "fixed"})
    original_output = subprocess.check_output
    def checkout(command, **options):
        if Path(options.get("cwd", "/")) == tmp_path / "components/motor":
            return "fixed-motor-head\n" if command[1] == "rev-parse" else ""
        return original_output(command, **options)
    monkeypatch.setattr(subprocess, "check_output", checkout)
    calls, events = [], []
    write_json = referee_sources.write_json
    def write(path, report):
        if report["result"] == "passed":
            events.append("published passed")
        write_json(path, report)
    monkeypatch.setattr(referee_sources, "write_json", write)
    primary = KeyboardInterrupt("original DJI interruption") if mode == "interrupt" else OSError("original DJI failure")
    def completed(manifest, **options):
        calls.append((manifest, options))
        assert options["can_delays"] == ((0, "east", 5_000_000), (250_000_000, "east", 0))
        observer = options["on_response"].__self__
        for key in ("owners", "children", "sockets", "controls", "plant", "runtime"):
            setattr(observer, key, getattr(owner, key))
        observer.report.update(scenario_completed_report(), **deepcopy(cleanup_report))
        if mode == "missing-case":
            observer.report["cases"]["west"].pop(0)
        if mode in ("cleanup", "interrupt"):
            observer.report["qmp_cleanup"][1]["completed"] = False
        if mode in ("source", "interrupt"):
            elf.write_bytes(b"changed after consumer")
        events.append("runtime cleanup completed")
        if mode in ("error", "interrupt"):
            raise primary
        boards = tuple(arena_module.arena.ArenaBoardResult(name, prefix, "uart_marker", epoch + 100,
            epoch + 1_000_000_000, epoch, 1000) for name, prefix, epoch in
            (("east", "alpha/", 1000), ("west", "bravo/", 2000)))
        return arena_module.arena.ArenaResult(manifest.name, 0, 1_000_000_000, 1000, boards, ())
    monkeypatch.setattr(arena_module.arena, "run_manifest", completed)
    output = tmp_path / "result"
    saved_signal = signal.getsignal(signal.SIGTERM)
    assert arena_module.run_dji_scenario(elf, output, tmp_path, qemu=sys.executable) == (
        0 if mode == "success" else 130 if mode == "interrupt" else 1)
    assert signal.getsignal(signal.SIGTERM) == saved_signal and len(calls) == 1
    options = calls[0][1]
    assert (options["duration"], options["startup_timeout"], options["timeout"]) == (1., 10., 20.)
    report_path = output / "report.json"
    report = json.loads(report_path.read_text())
    assert report["scenario_input"]["duration_ns"] == 1_000_000_000
    assert report["physical_can_suffixes"] == []
    if mode == "success":
        assert report["result"] == "passed" and report["cleanup_verified"] and report["source_preserved"]
        assert events == ["runtime cleanup completed", "published passed"]
        assert report["missing"] == {"east": [], "west": []}
    else:
        assert report["result"] == "failed" and "published passed" not in events
        if mode == "missing-case":
            assert report["missing"]["west"] == ["control.rx"]
        if mode in ("error", "interrupt"):
            assert report["errors"][0] == f"{type(primary).__name__}: {primary}"
        if mode == "interrupt":
            assert report["error_type"] == "KeyboardInterrupt"
            assert "cleanup incomplete" in "\n".join(report["notes"])
            assert "source state changed" in "\n".join(report["notes"])
            assert report["notes"] == primary.__notes__
    saved = report_path.read_bytes()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        arena_module.run_dji_scenario(elf, output, tmp_path, qemu=sys.executable)
    assert report_path.read_bytes() == saved and len(calls) == 1


@pytest.mark.parametrize("scenario", [False, True])
def test_dji_scenario_cli_dispatches_exactly_one_literal_consumer(monkeypatch, scenario):
    calls = []
    monkeypatch.setattr(arena_module, "run_dji_scenario", lambda *args, **options: calls.append((args, options)) or 7)
    monkeypatch.setattr(module, "run", lambda *args: calls.append((args, {})) or 7)
    argv = ["dji", "--scenario", "same.elf", "output path", "firmware", "--qemu", "qemu path"] if scenario else [
        "dji", "qmp", "uart", "can", "same.elf", "qemu", "report", "firmware"]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 7
    expected = [(("same.elf", "output path", "firmware"), {"qemu": "qemu path"})] if scenario else [
        (("qmp", "uart", "can", "same.elf", "qemu", "report", "firmware"), {})]
    assert calls == expected


@pytest.mark.parametrize("extra", [["--duration", "2s"], ["--referee"], ["additional-positional"]])
def test_dji_scenario_cli_refuses_profile_overrides_before_execution(monkeypatch, extra):
    monkeypatch.setattr(arena_module, "run_dji_scenario", lambda *args, **options: pytest.fail("unadmitted execution"))
    monkeypatch.setattr(sys, "argv", ["dji", "--scenario", "same.elf", "result", "firmware", *extra])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2


def feedback_case_rows():
    """Literal copied-field receipts, independent of the host's validators."""
    values = (("control.rx", 0, 0), ("reset.poweron", 0x800000, 0x800000),
        ("bsp.init", 0, 0), ("task.stack", 96, 64),
        ("dji.feedback.baseline.id1", 110, 0), ("dji.feedback.baseline.id2", 110, 0),
        ("dji.feedback.loss.frozen", 200, 200), ("dji.feedback.loss.age", 54, 51),
        ("dji.feedback.loss.app_age", 3, 50), ("dji.feedback.loss.output", 0, 0),
        ("dji.feedback.loss.id2", 257, 254), ("dji.feedback.recover.id1", 360, 200),
        ("dji.feedback.recover.id2", 360, 0), ("dji.feedback.stop", 0, 0),
        ("dji.feedback.can", 0, 0))
    return [dict(status="PASS", name=name, actual=actual, expected=expected)
            for name, actual, expected in values] + [
        dict(status="SKIP", name="plant.physics", actual=0, expected=0)]


def feedback_empty_report():
    report = scenario_empty_report()
    report.update(feedback_dispatches=[], physical_can_suffixes=[],
        feedback_observation=dict(shared_start=None, actions=[], cases={name: {} for name in ("east", "west")}))
    for group in ("commands", "command_wire", "command_deliveries"):
        for summary in report[group].values():
            summary.update(loss_count=0, witnesses={})
    return report


def feedback_completed_report():
    """Completed verification input, without emulating firmware or scheduling."""
    from test_referee_consumer import arena_response_summary
    report = feedback_empty_report()
    evidence = report["feedback_observation"]
    evidence["shared_start"] = dict(session_id=1, origin_ns=0, wire_sessions=[1, 2], accepted=True)
    evidence["actions"] = [dict(logical_ns=stamp, member=name, motor_id=1, enabled=enabled, accepted=True)
        for stamp, enabled in ((200_000_000, False), (350_000_000, True)) for name in ("east", "west")]
    for peer, name in enumerate(("east", "west")):
        epoch = (1000, 2000)[peer]
        report["cases"][name] = feedback_case_rows()
        report["summaries"][name] = [{"kind": kind, "values": [15, 0, 1]} for kind in ("PHASE", "DONE")]
        response = report["responses"][name] = arena_response_summary(peer + 1, epoch)
        response["step_ack_count"] = 400
        response["last_step_ack"]["header"].update(step_id=400, t_sim_ns=400_000_000)
        report["ready"][name] = dict(logical_ns=0, poll_sample_qemu_ns=epoch)
        report["watchdogs"][name] = {stage: dict(timeouts=0, diagnostics="window-violations=0")
                                     for stage in ("done", "paused")}
        for case in report["cases"][name]:
            label = case["name"]
            if not label.startswith("dji.feedback."):
                continue
            stamp = 20_000_000 if ".baseline." in label else 260_000_000 if ".loss." in label else (
                360_000_000 if ".recover." in label else 390_000_000)
            evidence["cases"][name][label] = dict(logical_ns=stamp, poll_sample_qemu_ns=epoch + stamp)
        for group in ("commands", "command_wire", "command_deliveries"):
            rows = [dict(peer=peer, can_id=0x200, flags=0, dlc=8, qemu_ns=epoch + stamp, data_hex=raw)
                for stamp, raw in ((0, "0040ffc000000000"), (260_000_000, "0000ffc000000000"),
                                   (360_000_000, "0040ffc000000000"), (380_000_000, "0000000000000000"))]
            for row in rows:
                if group == "command_wire":
                    row["wire_hex"] = struct.pack("<IIB3xQ64s", 0x200, 0, 8, row["qemu_ns"],
                        bytes.fromhex(row["data_hex"]).ljust(64, b"\0")).hex()
                elif group == "command_deliveries":
                    stamp = row["qemu_ns"] - epoch
                    row.update(mapped_qemu_ns=row.pop("qemu_ns"), source_world_ns=stamp,
                               due_ns=stamp, delivered_ns=stamp, destination="m3508-1")
            report[group][name] = dict(count=4, active_count=2, loss_count=1, zero_count=1,
                transition_count=4, first=rows[0], last=rows[-1], transitions=rows,
                witnesses=dict(zip(("baseline", "late_loss", "recovery", "stop"), rows)))
        for identifier in (1, 2, 3, 4):
            raw = {1: "0000000000401900", 2: "00000000ffc01900"}.get(identifier, "0000000000001900")
            for stamp in (100_000_000, 260_000_000, 360_000_000):
                dropped = identifier == 1 and stamp == 260_000_000
                report["feedback_dispatches"].append(dict(peer=peer, motor_id=identifier,
                    source=f"m3508-{identifier}", destination="qemu", world_ns=stamp, due_ns=stamp,
                    delivered_ns=stamp, sequence=identifier, data_hex=raw, dropped=dropped))
                if not dropped:
                    wire = struct.pack("<IIB3xQ64s", 0x200 + identifier, 0, 8, epoch + stamp,
                                       bytes.fromhex(raw).ljust(64, b"\0"))
                    report["feedback"].append(dict(peer=peer, can_id=0x200 + identifier, accepted=True,
                        world_ns=stamp, due_ns=stamp, delivered_ns=stamp, qemu_ns=epoch + stamp,
                        data_hex=raw, physical_complete=True, physical_wire_hex=wire.hex()))
    report["native_calls"] = dict(reset=1, step_ns=[1_000_000] * 400, close=1)
    return report


def test_dji_feedback_fixed_profile_is_pure_same_elf_and_reset_relative(tmp_path, monkeypatch):
    elf = tmp_path / "same.elf"
    elf.write_bytes(b"fixed firmware")
    def forbidden(*args, **options):
        pytest.fail("pure feedback profile reached resource acquisition")
    for method in ("resolve_qemu", "create_arena_plant", "running_qemu", "connect_unix"):
        monkeypatch.setattr(arena_module.arena, method, forbidden)
    scenario = arena_module.write_dji_feedback_manifest(tmp_path, (elf, elf))
    assert load_scenario(scenario.path) == scenario
    assert scenario.duration_ns == 400_000_000 and scenario.can_delays == scenario.referees == ()
    assert [(row.offset_ns, row.member, row.motor_id, row.enabled) for row in scenario.motor_feedback] == [
        (200_000_000, "east", 1, False), (200_000_000, "west", 1, False),
        (350_000_000, "east", 1, True), (350_000_000, "west", 1, True)]
    assert [row.name for row in scenario.arena.robots] == ["east", "west"]
    assert all(row.robot.board.firmware == elf.resolve() and not row.robot.upper_computers and
               row.robot.ready.endpoint == "mcu.uart1" and
               row.robot.ready.text == b"TTEST READY dji.feedback.ready 0 0\n" and
               [(device.motor_id, device.joint) for device in row.robot.devices] ==
               [(1, "wheel_fl"), (2, "wheel_fr"), (3, "wheel_rl"), (4, "wheel_rr")]
               for row in scenario.arena.robots)


def test_dji_feedback_literal_case_coverage_and_new_timestamp_history():
    rows = feedback_case_rows()
    required = {row["name"] for row in rows if row["status"] == "PASS"}
    assert arena_module.DJI_FEEDBACK_REQUIRED == required and len(required) == 15
    arena_module.check_dji_feedback_coverage(rows, [{"kind": kind, "values": [15, 0, 1]}
                                                  for kind in ("PHASE", "DONE")])


@pytest.mark.parametrize("label,field,value", [
    ("dji.feedback.loss.age", "actual", 50), ("dji.feedback.loss.age", "expected", 50),
    ("dji.feedback.loss.app_age", "actual", 50), ("dji.feedback.loss.output", "actual", 64),
    ("dji.feedback.loss.frozen", "actual", 199), ("dji.feedback.loss.frozen", "actual", 0),
    ("dji.feedback.loss.id2", "actual", 254), ("dji.feedback.recover.id1", "actual", 200),
    ("dji.feedback.baseline.id1", "actual", 0), ("dji.feedback.can", "actual", 1),
    ("task.stack", "actual", 63), ("control.rx", "actual", True),
    ("control.rx", "expected", -1), ("control.rx", "actual", 1 << 32)])
def test_dji_feedback_case_rejects_stale_or_nonliteral_unsigned_fields(label, field, value):
    row = next(row for row in feedback_case_rows() if row["name"] == label)
    with pytest.raises((TypeError, ValueError)):
        arena_module.check_dji_feedback_case(dict(row, **{field: value}))


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "skip", "summary", "frozen-baseline",
                                        "recovery-origin", "stale-control"])
def test_dji_feedback_coverage_requires_complete_reports_and_distinct_history(corruption):
    rows = feedback_case_rows()
    summaries = [{"kind": kind, "values": [15, 0, 1]} for kind in ("PHASE", "DONE")]
    values = {row["name"]: row for row in rows}
    if corruption == "missing":
        rows.pop(0)
    elif corruption == "duplicate":
        rows.append(rows[0])
    elif corruption == "skip":
        rows[-1]["name"] = "hardware.calibration"
    elif corruption == "summary":
        summaries[-1]["values"] = [14, 0, 1]
    elif corruption == "frozen-baseline":
        values["dji.feedback.baseline.id1"]["actual"] = 200
    elif corruption == "recovery-origin":
        values["dji.feedback.recover.id1"]["expected"] = 199
    else:
        values["dji.feedback.recover.id2"]["actual"] = 110
    with pytest.raises((TypeError, ValueError)):
        arena_module.check_dji_feedback_coverage(rows, summaries)


@pytest.fixture
def dji_feedback_observation(tmp_path, monkeypatch):
    """Borrow the original core/plant/socket consumer; isolate native acquisition."""
    environment = Environment(tmp_path, monkeypatch)
    elf = tmp_path / "same.elf"
    elf.write_bytes(b"fixed input")
    scenario = arena_module.write_dji_feedback_manifest(tmp_path, (elf, elf))
    report = feedback_empty_report()
    observer = arena_module.DjiFeedbackObservation(report, scenario.arena)
    for original, method in (("create_arena_plant", "world"), ("CanStream", "can"),
                             ("SharedWorldRuntime", "shared"), ("LabChannels", "channels")):
        setattr(observer, "original_" + method, getattr(arena_module.arena, original))
        monkeypatch.setattr(arena_module.arena, original, getattr(observer, method))
    observer.watchdog = lambda index, name, stage: report["watchdogs"].setdefault(name, {}).update(
        {stage: dict(timeouts=0, diagnostics="window-violations=0")})
    try:
        yield SimpleNamespace(environment=environment, scenario=scenario, report=report, observer=observer)
    finally:
        environment.release()


def test_dji_feedback_actual_four_hundred_ms_socket_consumer_has_scoped_drop_and_fresh_recovery(
        dji_feedback_observation):
    fixture = dji_feedback_observation
    environment, observer, report = fixture.environment, fixture.observer, fixture.report
    environment.can_commands = ["0040ffc000000000"] * 2
    environment.uart_chunks = [[b"TTEST READY dji.feedback.ready 0 0\n"] for _ in range(2)]
    for index, epoch in enumerate((1000, 2000)):
        for stamp, raw in ((260_000_000, "0000ffc000000000"), (360_000_000, "0040ffc000000000"),
                           (380_000_000, "0000000000000000")):
            environment.schedule_can(index, can_wire(epoch + stamp, raw), interval=stamp // 1_000_000 + 1)
    published = set()
    def service(index, operation):
        if operation != "qom-get":
            return
        now = sum(environment.native.steps)
        if now not in (20_000_000, 260_000_000, 360_000_000, 390_000_000) or (index, now) in published:
            return
        published.add((index, now))
        rows = [row for row in feedback_case_rows() if (
            now == 20_000_000 and (not row["name"].startswith("dji.feedback.") and row["status"] == "PASS"
                                  or ".baseline." in row["name"]) or
            now == 260_000_000 and ".loss." in row["name"] or
            now == 360_000_000 and ".recover." in row["name"] or
            now == 390_000_000 and row["name"] in ("dji.feedback.stop", "dji.feedback.can", "plant.physics"))]
        suffix = b"TTEST PHASE 15 0 1\nTTEST DONE 15 0 1\n" if now == 390_000_000 else b""
        environment.uart_chunks[index].append(scenario_uart_rows(rows) + suffix)
    environment.qmp_callback = service
    result = scenario_runtime.run_manifest(fixture.scenario, qemu=environment.binary,
        startup_timeout=10., timeout=20., emit=observer.emit, on_response=observer.response)
    environment.assert_clean()
    report["physical_can_suffixes"] = [dict(peer=row.peer, suffix_hex=row.suffix.hex()) for row in observer.written]
    for name in ("east", "west"):
        arena_module.check_dji_feedback_coverage(report["cases"][name], report["summaries"][name])
    arena_module.check_dji_feedback_observations(report)
    assert (result.origin_ns, result.end_ns, result.steps) == (0, 400_000_000, 400)
    assert report["native_calls"] == dict(reset=1, step_ns=[1_000_000] * 400, close=1)
    assert observer.runtime.closed and observer.plant.closed and len(observer.plant.devices) == 8
    assert all(not sources for sources in observer.feedback_sources.values())
    for peer, name in enumerate(("east", "west")):
        epoch = (1000, 2000)[peer]
        dispatches = [row for row in report["feedback_dispatches"] if row["peer"] == peer]
        assert len(dispatches) == 1600
        dropped = [row for row in dispatches if row["dropped"]]
        assert [(row["motor_id"], row["delivered_ns"]) for row in dropped] == [
            (1, stamp) for stamp in range(201_000_000, 351_000_000, 1_000_000)]
        accepted = [row for row in report["feedback"] if row["peer"] == peer]
        expected = b"".join(struct.pack("<IIB3xQ64s", row["can_id"], 0, 8, epoch + row["delivered_ns"],
            bytes.fromhex(row["data_hex"]).ljust(64, b"\0")) for row in accepted)
        cpu = environment.cpus[peer]
        assert len(accepted) == cpu.completed == len(cpu.receipts) == 1450 and not cpu.partial
        assert bytes(cpu.received) == b"".join(cpu.receipts) == expected
        assert all(bytes.fromhex(row["physical_wire_hex"]) == expected[offset * 84:(offset + 1) * 84]
                   for offset, row in enumerate(accepted))
        assert all(row["world_ns"] == row["due_ns"] == row["delivered_ns"] and row["qemu_ns"] == epoch + row["world_ns"]
                   for row in accepted)
        for group in ("commands", "command_wire", "command_deliveries"):
            summary = report[group][name]
            multiple = 4 if group == "command_deliveries" else 1
            assert [summary[key] for key in ("count", "active_count", "loss_count", "zero_count")] == [
                4 * multiple, 2 * multiple, multiple, multiple]
            assert [row["data_hex"] for row in summary["transitions"]] == [
                "0040ffc000000000", "0000ffc000000000", "0040ffc000000000", "0000000000000000"]


@pytest.mark.parametrize("corruption", ["action", "late-baseline", "early-loss", "early-recovery",
    "early-zero", "wrong-command", "other-motor-drop", "missing-other-motor", "no-recovery-source",
    "wrong-epoch", "physical-prefix", "physical-suffix"])
def test_dji_feedback_observation_rejects_scope_command_time_and_physical_failures(corruption):
    report = feedback_completed_report()
    arena_module.check_dji_feedback_observations(report)
    evidence = report["feedback_observation"]
    if corruption == "action":
        evidence["actions"][0]["motor_id"] = 2
    elif corruption in ("late-baseline", "early-loss", "early-recovery"):
        label, stamp = {"late-baseline": ("baseline.id1", 200_000_000),
            "early-loss": ("loss.age", 251_000_000), "early-recovery": ("recover.id1", 350_000_000)}[corruption]
        evidence["cases"]["west"]["dji.feedback." + label]["logical_ns"] = stamp
    elif corruption == "early-zero":
        report["command_wire"]["west"]["witnesses"]["late_loss"]["qemu_ns"] = 2000 + 251_000_000
    elif corruption == "wrong-command":
        report["command_deliveries"]["west"]["witnesses"]["recovery"]["data_hex"] = "0040000000000000"
    elif corruption == "other-motor-drop":
        next(row for row in report["feedback_dispatches"] if row["motor_id"] == 2)["dropped"] = True
    elif corruption == "missing-other-motor":
        report["feedback_dispatches"] = [row for row in report["feedback_dispatches"] if row["motor_id"] != 4]
    elif corruption == "no-recovery-source":
        report["feedback"] = [row for row in report["feedback"] if not (
            row["can_id"] == 0x201 and row["delivered_ns"] > 350_000_000)]
    elif corruption == "wrong-epoch":
        report["feedback"][0]["qemu_ns"] += 1
    elif corruption == "physical-prefix":
        report["feedback"][0]["physical_complete"] = False
    else:
        report["physical_can_suffixes"] = [dict(peer=0, suffix_hex="0102")]
    with pytest.raises(ValueError, match="DJI feedback"):
        arena_module.check_dji_feedback_observations(report)


@pytest.mark.parametrize("mode", ["success", "missing-case", "cleanup", "interrupt"])
def test_dji_feedback_runner_verifies_cleanup_and_preserves_failed_primary(
        dji_scenario_completed_owner, tmp_path, monkeypatch, mode):
    from tools import dm_mc02_dji_test as dji_sources
    from tools import dm_mc02_referee_test as referee_sources
    owner, cleanup_report = dji_scenario_completed_owner
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"fixed input")
    for relative in ("app/selftest/dji_feedback.cc", "Core/Src/fdcan.c",
        "Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_fdcan.c",
        "Middlewares/Third_Party/FreeRTOS/Source/tasks.c",
        "Middlewares/Third_Party/FreeRTOS/Source/portable/GCC/ARM_CM4F/port.c"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixed dependency")
    inputs = lambda *args: {str(elf): hashlib.sha256(elf.read_bytes()).hexdigest()}
    monkeypatch.setattr(dji_sources, "source_snapshot", inputs)
    monkeypatch.setattr(referee_sources, "source_snapshot", inputs)
    monkeypatch.setattr(referee_sources, "checkout_snapshot", lambda firmware: {})
    monkeypatch.setattr(arena_module, "source_origins", lambda: {"sim.core": "fixed"})
    calls, events = [], []
    write_json = referee_sources.write_json
    def write(path, report):
        if report["result"] == "passed":
            events.append("published passed")
        write_json(path, report)
    monkeypatch.setattr(referee_sources, "write_json", write)
    primary = KeyboardInterrupt("original feedback interruption")
    def completed(manifest, **options):
        calls.append((manifest, options))
        assert options["motor_feedback"] == ((200_000_000, "east", 1, False),
            (200_000_000, "west", 1, False), (350_000_000, "east", 1, True), (350_000_000, "west", 1, True))
        assert "can_delays" not in options
        observer = options["on_response"].__self__
        for key in ("owners", "children", "sockets", "controls", "plant", "runtime"):
            setattr(observer, key, getattr(owner, key))
        observer.report.update(feedback_completed_report(), **deepcopy(cleanup_report))
        if mode == "missing-case":
            observer.report["cases"]["west"].pop(0)
        if mode in ("cleanup", "interrupt"):
            observer.report["qmp_cleanup"][1]["completed"] = False
        if mode == "interrupt":
            elf.write_bytes(b"changed after consumer")
        events.append("runtime cleanup completed")
        if mode == "interrupt":
            raise primary
        boards = tuple(arena_module.arena.ArenaBoardResult(name, prefix, "uart_marker", epoch + 100,
            epoch + 400_000_000, epoch, 400) for name, prefix, epoch in
            (("east", "alpha/", 1000), ("west", "bravo/", 2000)))
        return arena_module.arena.ArenaResult(manifest.name, 0, 400_000_000, 400, boards, ())
    monkeypatch.setattr(arena_module.arena, "run_manifest", completed)
    output = tmp_path / "result"
    saved_signal = signal.getsignal(signal.SIGTERM)
    assert arena_module.run_dji_feedback_scenario(elf, output, tmp_path, qemu=sys.executable) == (
        0 if mode == "success" else 130 if mode == "interrupt" else 1)
    assert signal.getsignal(signal.SIGTERM) == saved_signal and len(calls) == 1
    assert [calls[0][1][key] for key in ("duration", "startup_timeout", "timeout")] == [.4, 10., 20.]
    report = json.loads((output / "report.json").read_text())
    assert report["feedback_scenario"] and report["scenario_input"]["duration_ns"] == 400_000_000
    if mode == "success":
        assert report["result"] == "passed" and report["cleanup_verified"] and report["source_preserved"]
        assert report["missing"] == {"east": [], "west": []}
        assert events == ["runtime cleanup completed", "published passed"]
    else:
        assert report["result"] == "failed" and "published passed" not in events
        if mode == "missing-case":
            assert report["missing"]["west"] == ["control.rx"]
        if mode == "interrupt":
            assert report["errors"][0] == "KeyboardInterrupt: original feedback interruption"
            assert report["notes"] == primary.__notes__
            assert "cleanup incomplete" in "\n".join(report["notes"])
            assert "source state changed" in "\n".join(report["notes"])
    saved = (output / "report.json").read_bytes()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        arena_module.run_dji_feedback_scenario(elf, output, tmp_path, qemu=sys.executable)
    assert (output / "report.json").read_bytes() == saved and len(calls) == 1


def test_dji_feedback_cli_dispatches_only_dedicated_literal_consumer(monkeypatch):
    calls = []
    monkeypatch.setattr(arena_module, "run_dji_feedback_scenario", lambda *args, **options: calls.append((args, options)) or 7)
    monkeypatch.setattr(sys, "argv", ["dji", "--feedback-scenario", "same.elf", "output path", "firmware", "--qemu", "qemu path"])
    assert module.main() == 7
    assert calls == [(("same.elf", "output path", "firmware"), {"qemu": "qemu path"})]


@pytest.mark.parametrize("extra", [["--duration", "1s"], ["--scenario"], ["--timeout", "30"]])
def test_dji_feedback_cli_refuses_profile_and_deadline_overrides(monkeypatch, extra):
    monkeypatch.setattr(arena_module, "run_dji_feedback_scenario", lambda *args, **options: pytest.fail("unadmitted execution"))
    monkeypatch.setattr(sys, "argv", ["dji", "--feedback-scenario", "same.elf", "result", "firmware", *extra])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2
