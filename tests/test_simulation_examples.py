"""Example manifest, real build and direct acceptance-verifier coverage."""

import importlib.util
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("simulation_smoke", ROOT / "smoke/simulation_smoke.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


@pytest.mark.parametrize("case", (*smoke.CASES, "pulse"))
def test_real_bare_metal_examples_build_and_link_without_vendor_or_firmware_sdk(tmp_path, case):
    if any(shutil.which(program) is None for program in ("cmake", "arm-none-eabi-gcc")):
        pytest.skip("optional ARM cross compiler/CMake are unavailable")
    result = smoke.build_example(case, build_dir=tmp_path / case)
    elf = Path(result["elf"]).read_bytes()
    assert elf[:7] == b"\x7fELF\x01\x01\x01"  # ELF32, little-endian, current version.
    assert int.from_bytes(elf[18:20], "little") == 40  # EM_ARM.
    assert int.from_bytes(elf[24:28], "little") & ~1 >= 0x08000000
    assert len(result["sha256"]) == 64
    assert all(item["exit"] == 0 for item in result["build"])
    from sim.composition import builtin_registry
    from sim.config.scene import load_scene

    scene_file = tmp_path / "scene.yaml"
    example = smoke.EXAMPLE_ALIASES.get(case, case)
    scene_file.write_text((ROOT / "examples" / example / "scene.yaml").read_text().replace(
        "./build/firmware.elf", result["elf"]))
    scene = load_scene(scene_file, builtin_registry(backend_root=ROOT))
    assert scene.mode == "stepped" and scene.quantum_ns == 1_000_000
    assert scene.duration_ns == 200_000_000
    assert len(scene.topology.nodes) == (3 if case == "can-motor" else 1)


def test_missing_build_dependency_is_explicitly_blocked(monkeypatch):
    monkeypatch.setattr(smoke.shutil, "which", lambda name: None)
    with pytest.raises(smoke.Blocked, match="cmake"):
        smoke.build_example("minimal-board")


def _minimal_observations():
    result = smoke.Observations()
    result.uart.extend(b"SIM_READY minimal-board\nKEY released B0=0\n"
                       b"KEY pressed B0=1\nKEY released B0=0\n")
    result.snapshots = [
        {"mapped_time_ns": 10_000_000, "b0_output": True, "key": 1, "b0": 0},
        {"mapped_time_ns": 50_000_000, "b0_output": True, "key": 0, "b0": 1},
        {"mapped_time_ns": 100_000_000, "b0_output": True, "key": 1, "b0": 0},
    ]
    return result


def test_acceptance_requires_actual_gpio_response_in_each_input_window():
    observations = _minimal_observations()
    assert observations.verify("minimal-board")["snapshots"] == observations.snapshots
    observations.snapshots[1]["b0"] = 0
    with pytest.raises(AssertionError, match="native GPIO"):
        observations.verify("minimal-board")


def test_acceptance_rejects_old_uart_marker_order_and_guest_failure():
    observations = _minimal_observations()
    observations.uart = bytearray(b"SIM_READY minimal-board\nKEY pressed B0=1\n"
                                 b"KEY released B0=0\n")
    with pytest.raises(AssertionError, match="ordered guest"):
        observations.verify("minimal-board")
    observations.uart.extend(b"FAIL\n")
    with pytest.raises(AssertionError, match="guest reported failure"):
        observations.verify("minimal-board")


def _pulse_observations():
    result = _minimal_observations()
    result.snapshots = [
        {"source": "main", "mapped_time_ns": cut, "delivery_time_ns": cut,
         "native_timestamp_ns": 123 + cut, "quality": "exact",
         "b0_output": cut != 0, "key": key, "b0": b0}
        for cut, key, b0 in ((0, 1, 0), (3_000_000, 1, 0),
                             (4_000_000, 0, 1), (10_000_000, 1, 0), (20_000_000, 1, 0))
    ]
    result.inputs = [
        {"source": "main", "endpoint": "key", "value": value,
         "source_time_ns": cut, "delivery_time_ns": cut}
        for value, cut in ((True, 3_000_000), (False, 4_000_000))
    ]
    return result


def test_pulse_requires_native_cuts_and_guest_response_between_input_times():
    observations = _pulse_observations()
    observations.verify("pulse")
    # Correctly timestamped inputs and later UART text cannot stand in for the
    # guest's actual GPIO response before the release input at 4ms.
    observations.snapshots[2]["b0"] = 0
    with pytest.raises(AssertionError, match="guest GPIO response.*4000000"):
        observations.verify("pulse")


def test_pulse_rejects_inputs_coalesced_at_the_outer_quantum():
    observations = _pulse_observations()
    for item in observations.inputs:
        item["delivery_time_ns"] = 10_000_000
    with pytest.raises(AssertionError, match="scheduled native cuts"):
        observations.verify("pulse")


@pytest.mark.parametrize("field,value", [
    ("mapped_time_ns", 10_000_000),
    ("native_timestamp_ns", 3_000_124),
    ("quality", "sampled"),
])
def test_pulse_rejects_missing_or_inexact_actual_native_cut(field, value):
    observations = _pulse_observations()
    observations.snapshots[1][field] = value
    with pytest.raises(AssertionError, match="actual native run cut.*3000000"):
        observations.verify("pulse")


def _digital_observations():
    result = _minimal_observations()
    result.peer_uart.extend(b"SIM_READY minimal-board\nKEY pressed B0=1\n"
                            b"KEY released B0=0\nKEY pressed B0=1\n")
    result.snapshots = []
    result.digital = []
    result.uart_batches = []
    result.inputs = [
        {"source": "main", "endpoint": "key", "value": value,
         "source_time_ns": cut, "delivery_time_ns": cut}
        for value, cut in ((True, 40_000_000), (False, 90_000_000))
    ]
    for index, (cut, level, marker) in enumerate((
            (1_000_000, 0, b"KEY pressed B0=1\n"),
            (41_000_000, 1, b"KEY released B0=0\n"),
            (91_000_000, 0, b"KEY pressed B0=1\n"))):
        sequence = 10 * index
        for node, when, key, b0, event_sequence in (
                ("main", cut, 1 - level, level, sequence),
                ("peer", cut, 1 - level, level, sequence + 1),
                ("peer", cut + 1_000_000, level, 1 - level, sequence + 4)):
            result.snapshots.append({"source": node, "mapped_time_ns": when,
                "native_timestamp_ns": (100 if node == "main" else 900) + when,
                "event_sequence": event_sequence, "b0_output": True, "key": key, "b0": b0})
        sample = {"source": "main.gpio.B0.out", "level": level, "active": True,
                  "changed": True}
        for kind, payload, event_sequence in (
                ("net.digital.sample", sample, sequence + 2),
                ("net.digital.delivery", {"source": "main.gpio.B0.out",
                 "destination": "peer.key", "level": level,
                 "sequence": index, "due_ns": cut}, sequence + 3)):
            result.digital.append({"source": "wire", "endpoint": "signal", "kind": kind,
                "payload": payload, "source_time_ns": 100 + cut, "mapped_time_ns": cut,
                "delivery_time_ns": cut, "quality": "sampled", "event_sequence": event_sequence})
        result.uart_batches.append({"source": "peer", "mapped_time_ns": cut + 5_000_000,
            "event_sequence": sequence + 5,
            "data_hex": ((b"SIM_READY minimal-board\n" if index == 0 else b"") + marker).hex()})
    return result


def test_digital_link_requires_later_real_peer_gpio_and_uart_consumption():
    observations = _digital_observations()
    assert len(observations.verify("digital-link")["digital_consumption"]) == 3
    observations.snapshots[2]["b0"] = 0
    with pytest.raises(AssertionError, match="later peer GPIO"):
        observations.verify("digital-link")


@pytest.mark.parametrize("field,value", [
    ("source_time_ns", 1_000_099),
    ("quality", "exact"),
])
def test_digital_link_retains_sampled_source_clock_provenance(field, value):
    observations = _digital_observations()
    observations.digital[1][field] = value
    with pytest.raises(AssertionError, match="digital source provenance"):
        observations.verify("digital-link")


def test_digital_link_rejects_retroactive_input_or_uart_evidence():
    observations = _digital_observations()
    observations.snapshots[1]["key"] = 0
    with pytest.raises(AssertionError, match="preceding peer interval"):
        observations.verify("digital-link")
    observations = _digital_observations()
    observations.uart_batches[0]["mapped_time_ns"] = 1_000_000
    with pytest.raises(AssertionError, match="later peer UART"):
        observations.verify("digital-link")


def test_digital_scene_reuses_two_normal_minimal_board_elfs(tmp_path, monkeypatch):
    from sim.composition import builtin_registry
    from sim.config.scene import load_scene

    firmware = tmp_path / "firmware.elf"
    firmware.write_bytes(b"admission-only ELF")
    text = (ROOT / "examples/digital-link/scene.yaml").read_text()
    scene_file = tmp_path / "scene.yaml"
    scene_file.write_text(text.replace("../minimal-board/build/firmware.elf", str(firmware)))
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *_a, **_k: pytest.fail("native acquisition"))
    scene = load_scene(scene_file, builtin_registry())
    assert [node.name for node in scene.topology.nodes] == ["main", "wire", "peer"]
    assert [node.kind for node in scene.topology.nodes] == ["machine.qemu", "net.digital", "machine.qemu"]
    assert [(str(link.source), str(link.target)) for link in scene.topology.links] == [
        ("main.gpio.B0.out", "wire.signal"), ("wire.signal", "peer.key")]


def test_motor_uart_pass_alone_cannot_replace_actual_can_and_native_receipts():
    observations = smoke.Observations()
    observations.uart.extend(b"SIM_READY can-motor\nMOTOR_ACTIVE current=64 encoder=0 rpm=0\n"
                             b"MOTOR_TIMEOUT current=0\nMOTOR_PASS\n")
    observations.snapshots = [{"mapped_time_ns": 200_000_000}]
    with pytest.raises(AssertionError, match="actual guest current command"):
        observations.verify("can-motor")


def test_dual_guest_acceptance_requires_each_real_source_and_later_peer_delivery():
    observations = smoke.Observations()
    uart = (b"SIM_READY can-motor\nMOTOR_ACTIVE current=64 encoder=0 rpm=0\n"
            b"MOTOR_TIMEOUT current=0\nMOTOR_PASS\n")
    observations.uart.extend(uart)
    observations.peer_uart.extend(uart)
    observations.snapshots = [{"mapped_time_ns": 200_000_000}]
    observations.can = [
        {"source": source, "endpoint": "fdcan1", "can_id": 0x200,
         "data": "0040000000000000", "source_time_ns": 1_650_878,
         "wire_timestamp_ns": 1_650_878, "mapped_time_ns": 1_650_878,
         "delivery_time_ns": 2_000_000} for source in ("main", "peer")]
    observations.can += [
        {"source": "motor0", "endpoint": "can", "can_id": 0x201, "flags": 0,
         "dlc": 8, "data": data, "mapped_time_ns": timestamp}
        for data, timestamp in (("0000000000401900", 3_000_000),
                                ("0000000000001900", 102_000_000))]
    observations.deliveries = [
        {"source": source, "destination": target, "can_id": 0x200,
         "dropped": False, "delivery_time_ns": 2_000_000}
        for source, target in (("main", "motor0.can"), ("main", "peer.fdcan1"),
                               ("peer", "main.fdcan1"))]
    observations.receipts = [
        {"source": source, "payload": {"completed_frames": 201,
         "rx_dropped": 1, "partial_bytes": 0}} for source in ("main", "peer")]
    observations.verify("dual-board")
    observations.deliveries[-1]["delivery_time_ns"] = 1_000_000
    with pytest.raises(AssertionError, match="next completed cut"):
        observations.verify("dual-board")


def test_observation_limits_are_visible_and_sequence_regression_rejects():
    observations = smoke.Observations()
    event = SimpleNamespace(sequence=4, source_time_ns=None, mapped_time_ns=0,
                            delivery_time_ns=0, quality="sampled", kind="other", payload=None)
    observations(event)
    with pytest.raises(ValueError, match="sequence"):
        observations(event)
    observations.total = 8192
    with pytest.raises(BufferError, match="event budget"):
        observations(SimpleNamespace(sequence=5))


def test_blocked_native_gate_retains_new_report_and_returns_78(tmp_path):
    report = tmp_path / "missing-qemu.json"
    assert smoke.main(["--qemu", str(tmp_path / "not-a-qemu"), "--report", str(report)]) == 78
    assert '"status": "blocked"' in report.read_text()
    original = report.read_bytes()
    assert smoke.main(["--qemu", str(tmp_path / "not-a-qemu"), "--report", str(report)]) == 1
    assert report.read_bytes() == original
