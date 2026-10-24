"""Example manifest, real build and direct acceptance-verifier coverage."""

import importlib.util
from fractions import Fraction
import json
import math
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("simulation_smoke", ROOT / "smoke/simulation_smoke.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


@pytest.mark.parametrize("case", (*smoke.CASES, "pulse", "rotor-loop", "joint-loop"))
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
    if case in ("can-motor", "rotor-loop", "joint-loop"):
        configure = result["build"][0]["command"]
        assert "-DSIM_ROTOR_LOOP_TEST=" + ("ON" if case == "rotor-loop" else "OFF") in configure
        assert "-DSIM_JOINT_LOOP_TEST=" + ("ON" if case == "joint-loop" else "OFF") in configure
    from sim.composition import builtin_registry
    from sim.config.scene import load_scene

    scene_file = tmp_path / "scene.yaml"
    example = smoke.EXAMPLE_ALIASES.get(case, case)
    scene_name = case + ".yaml" if case in ("rotor-loop", "joint-loop") else "scene.yaml"
    scene_file.write_text((ROOT / "examples" / example / scene_name).read_text().replace(
        f"./build-{case}/firmware.elf" if case in ("rotor-loop", "joint-loop") else "./build/firmware.elf",
        result["elf"]).replace("../shared-world/world.xml", str(ROOT / "examples/shared-world/world.xml")))
    scene = load_scene(scene_file, builtin_registry(backend_root=ROOT))
    assert scene.mode == "stepped" and scene.quantum_ns == 1_000_000
    assert scene.duration_ns == (80_000_000 if case in ("rotor-loop", "joint-loop") else 200_000_000)
    assert len(scene.topology.nodes) == (5 if case == "joint-loop" else
                                       3 if case in ("can-motor", "rotor-loop") else 1)


def test_missing_build_dependency_is_explicitly_blocked(monkeypatch):
    monkeypatch.setattr(smoke.shutil, "which", lambda name: None)
    with pytest.raises(smoke.Blocked, match="cmake"):
        smoke.build_example("minimal-board")


def test_motor_loop_variants_are_mutually_exclusive_at_cmake_admission(tmp_path):
    if any(shutil.which(program) is None for program in ("cmake", "arm-none-eabi-gcc")):
        pytest.skip("optional ARM cross compiler/CMake are unavailable")
    result = smoke.subprocess.run(["cmake", "-S", str(ROOT / "examples/can-motor"),
        "-B", str(tmp_path / "incompatible"), "-DSIM_ROTOR_LOOP_TEST=ON", "-DSIM_JOINT_LOOP_TEST=ON"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and "mutually exclusive" in result.stdout + result.stderr


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


def _rotor_observations():
    """Closed-form independent fixture; no production integrator is called."""
    result = smoke.Observations()
    result.uart.extend(b"SIM_READY can-motor\nROTOR_MOVING rpm>=2\n"
                       b"ROTOR_COAST current=0 motion=1\nROTOR_PASS\n")
    result.snapshots = [{"mapped_time_ns": 80_000_000}]
    result.rotor_state = {"time_ns": 80_000_000}
    result.can = []
    result.deliveries = []
    for index, (data, source, cut) in enumerate((
            ("1000000000000000", 1_650_878, 2_000_000),
            ("0000000000000000", 37_100_000, 38_000_000))):
        stamp = {"event_sequence": 10 + index * 400, "source_time_ns": source,
                 "mapped_time_ns": source, "delivery_time_ns": cut, "quality": "exact"}
        result.can.append({**stamp, "source": "main", "endpoint": "fdcan1",
            "kind": "can-tx", "can_id": 0x200, "flags": 0, "dlc": 8,
            "data": data, "wire_timestamp_ns": source})
        result.deliveries.append({**stamp, "event_sequence": stamp["event_sequence"] + 2,
            "source": "main", "endpoint": "fdcan1", "destination": "motor0.can",
            "can_id": 0x200, "data": data, "dropped": False})
    # A driven feedback frame causes the later actual zero-current command.
    # The next interval reflects that command while the rotor keeps moving.
    for sequence, cut, data in ((400, 36_000_000, "0004000210001900"),
                               (420, 38_000_000, "0004000210001900"),
                               (430, 39_000_000, "0004000200001900")):
        result.can.append({"event_sequence": sequence, "source": "motor0",
            "endpoint": "can", "kind": "can.sent", "can_id": 0x201,
            "flags": 0, "dlc": 8, "data": data, "source_time_ns": cut,
            "mapped_time_ns": cut, "delivery_time_ns": cut, "quality": "exact",
            "wire_timestamp_ns": cut})
        result.deliveries.append({"event_sequence": sequence + 1,
            "source": "motor0", "endpoint": "can", "destination": "main.fdcan1",
            "can_id": 0x201, "data": data, "dropped": False,
            "source_time_ns": cut, "mapped_time_ns": cut,
            "delivery_time_ns": cut, "quality": "exact"})
    result.receipts = [{"event_sequence": sequence, "source": "main", "endpoint": "fdcan1",
        "mapped_time_ns": cut, "payload": {
            "completed_frames": cut // 1_000_000, "rx_dropped": 0, "partial_bytes": 0}}
        for sequence, cut in ((402, 36_000_000), (422, 38_000_000), (432, 39_000_000))]
    result.uart_batches = [{"source": "main", "event_sequence": 450,
        "mapped_time_ns": 45_000_000, "quality": "upper_bound",
        "data_hex": bytes(result.uart).hex()}]
    # Compute only the fixture's final constants from its analytic expression.
    r, v, n, m = 0.996, 1.25, 36, 42
    driven = v * (1 - r ** n)
    result.rotor_state["position_rad"] = (
        .001 * v * (n - r * (1 - r ** n) / (1 - r))
        + .001 * driven * r * (1 - r ** m) / (1 - r))
    result.rotor_state["velocity_rad_s"] = driven * r ** m
    return result


def test_rotor_loop_requires_feedback_guest_command_coast_and_closed_form_state():
    observations = _rotor_observations()
    report = observations.verify("rotor-loop")
    assert report["rotor_consumption"]["command_delivery_ns"] == [2_000_000, 38_000_000]
    assert math.isfinite(report["rotor_state"]["position_rad"])
    observations.rotor_state["velocity_rad_s"] = 0
    with pytest.raises(AssertionError, match="closed-form rotor"):
        observations.verify("rotor-loop")


def test_rotor_loop_rejects_uart_pass_without_two_actual_guest_commands():
    observations = _rotor_observations()
    observations.can = [item for item in observations.can
                        if item["source"] != "main" or item["data"] != "0000000000000000"]
    with pytest.raises(AssertionError, match="drive and zero commands"):
        observations.verify("rotor-loop")


def test_rotor_loop_rejects_timeout_as_zero_command_explanation():
    observations = _rotor_observations()
    observations.deliveries[1]["delivery_time_ns"] = 102_000_000
    with pytest.raises(AssertionError, match="before command expiry"):
        observations.verify("rotor-loop")


def test_rotor_loop_rejects_retroactive_current_or_missing_guest_consumption():
    observations = _rotor_observations()
    observations.can[-2]["data"] = "0004000200001900"
    with pytest.raises(AssertionError, match="next plant interval"):
        observations.verify("rotor-loop")
    observations = _rotor_observations()
    observations.receipts.clear()
    with pytest.raises(AssertionError, match="native CAN input completion"):
        observations.verify("rotor-loop")


def test_rotor_loop_preserves_can_source_time_and_later_firmware_feedback_response():
    observations = _rotor_observations()
    observations.can[0]["source_time_ns"] = None
    with pytest.raises(AssertionError, match="CAN source timestamp"):
        observations.verify("rotor-loop")
    observations = _rotor_observations()
    observations.can[1]["source_time_ns"] = 35_000_000
    observations.can[1]["mapped_time_ns"] = 35_000_000
    observations.can[1]["wire_timestamp_ns"] = 35_000_000
    observations.deliveries[1]["source_time_ns"] = 35_000_000
    with pytest.raises(AssertionError, match="later actual guest zero"):
        observations.verify("rotor-loop")


def test_rotor_loop_rejects_sent_feedback_without_actual_guest_delivery():
    observations = _rotor_observations()
    observations.deliveries = [row for row in observations.deliveries
                               if row["source"] != "motor0"]
    with pytest.raises(AssertionError, match="feedback delivery to guest"):
        observations.verify("rotor-loop")


def test_rotor_loop_rejects_unrelated_receipt_and_early_coast_uart():
    observations = _rotor_observations()
    observations.receipts[0]["mapped_time_ns"] = 37_000_000
    with pytest.raises(AssertionError, match="matching native CAN input completion"):
        observations.verify("rotor-loop")
    observations = _rotor_observations()
    observations.uart_batches[0]["mapped_time_ns"] = 38_000_000
    with pytest.raises(AssertionError, match="later UART observation"):
        observations.verify("rotor-loop")


def test_rotor_loop_rejects_stale_native_completion_counter():
    observations = _rotor_observations()
    observations.receipts[-1]["payload"]["completed_frames"] = 36
    with pytest.raises(AssertionError, match="counter did not advance"):
        observations.verify("rotor-loop")


def _joint_observations():
    """Exact rational mechanics for d=2ms/z=6ms; no production solver/helpers."""
    result = smoke.Observations()
    result.uart.extend(b"SIM_READY can-motor\nJOINT_MOVING ids=1,2\n"
                       b"JOINT_COAST ids=1,2\nJOINT_PASS\n")
    result.snapshots = [{"mapped_time_ns": 80_000_000}]
    for sequence, data, source, cut in ((190, "1000f00000000000", 1_650_878, 2_000_000),
                                        (590, "0000000000000000", 5_100_000, 6_000_000)):
        row = {"event_sequence": sequence, "source": "main", "endpoint": "fdcan1",
            "kind": "can-tx", "can_id": 0x200, "flags": 0, "dlc": 8, "data": data,
            "source_time_ns": source, "mapped_time_ns": source, "delivery_time_ns": cut,
            "quality": "exact", "wire_timestamp_ns": source}
        result.can.append(row)
        for index in (0, 1):
            result.deliveries.append({**row, "event_sequence": cut // 10_000 + 60 + index,
                "destination": f"motor{index}.can", "dropped": False})
    ratio = Fraction(3591, 187)
    for tick in range(81):
        cut = tick * 1_000_000
        n, m = max(0, min(tick, 6) - 2), max(0, tick - 6)
        q = Fraction(5, 1_000_000) * ratio * (n * (n + 1) // 2 + n * m)
        v = Fraction(5, 1000) * ratio * n
        result.physics_states.append({"event_sequence": 100 * tick + 30,
            "source": "world", "endpoint": "state", "source_time_ns": None,
            "mapped_time_ns": cut, "delivery_time_ns": cut, "quality": "sampled",
            "native_time_s": tick / 1000,
            "joints": {"j1": {"position_rad": float(q), "velocity_rad_s": float(v)},
                       "j2": {"position_rad": -float(q), "velocity_rad_s": -float(v)}}})
        if not tick:
            continue
        for index, sign in ((0, 1), (1, -1)):
            encoder = int((float(sign * q * ratio) % math.tau) * 8192 / math.tau)
            rpm = round(float(sign * v * ratio) * 30 / math.pi)
            current = sign * 4096 if 2 < tick <= 6 else 0
            data = (encoder.to_bytes(2, "big") + rpm.to_bytes(2, "big", signed=True)
                    + current.to_bytes(2, "big", signed=True) + bytes.fromhex("1900")).hex()
            row = {"event_sequence": tick * 100 + index + 1, "source": f"motor{index}",
                "endpoint": "can", "kind": "can.sent", "can_id": 0x201 + index,
                "flags": 0, "dlc": 8, "data": data, "source_time_ns": cut,
                "mapped_time_ns": cut, "delivery_time_ns": cut, "quality": "exact",
                "wire_timestamp_ns": cut}
            result.can.append(row)
            result.deliveries.append({**row, "event_sequence": tick * 100 + index * 3 + 40,
                "destination": "main.fdcan1", "dropped": False})
            # The canonical receiver processes and receipts one frame before
            # Fabric delivers the next, including two frames at the same cut.
            result.receipts.append({"event_sequence": tick * 100 + index * 3 + 41,
                "source": "main", "endpoint": "fdcan1", "mapped_time_ns": cut,
                "source_time_ns": cut, "delivery_time_ns": cut, "quality": "exact",
                "payload": {"completed_frames": 2 * tick - 1 + index,
                            "rx_dropped": 0, "partial_bytes": 0}})
    result.uart_batches = [{"source": "main", "event_sequence": 1250,
        "mapped_time_ns": 12_000_000, "quality": "upper_bound",
        "data_hex": bytes(result.uart).hex()}]
    return result


def test_joint_loop_proves_both_signed_feedback_reads_and_independent_native_state():
    observations = _joint_observations()
    report = observations.verify("joint-loop")
    assert report["joint_consumption"]["command_delivery_ns"] == [2_000_000, 6_000_000]
    assert set(report["joint_consumption"]["motors"]) == {"motor0", "motor1"}
    assert [row["data"] for row in observations.can
            if row["source"] in ("motor0", "motor1") and row["mapped_time_ns"] == 3_000_000] == [
                "0002001210001900", "1ffdffeef0001900"]
    json.dumps(report)  # Native JointState records have an explicit primitive projection.


def test_joint_loop_rejects_motor2_sent_feedback_without_actual_delivery_despite_pass_and_receipts():
    observations = _joint_observations()
    observations.deliveries = [row for row in observations.deliveries if row["source"] != "motor1"]
    with pytest.raises(AssertionError, match="motor1 feedback delivery to guest"):
        observations.verify("joint-loop")


@pytest.mark.parametrize("change,error", [
    ("sign", "independent signed joint wire"),
    ("same_joint", "independent joint mechanics"),
    ("early_zero", "motor0 lacks moving/coast"),
    ("late_motion_receipt", "next native per-frame"),
    ("stale_receipt", "counter must advance exactly once"),
    ("early_uart", "later UART observation"),
])
def test_joint_loop_rejects_false_two_motor_consumption_evidence(change, error):
    observations = _joint_observations()
    if change == "sign":
        next(row for row in observations.can if row["source"] == "motor1"
             and row["mapped_time_ns"] == 3_000_000)["data"] = "0002001210001900"
    elif change == "same_joint":
        observations.physics_states[-1]["joints"]["j2"] = observations.physics_states[-1]["joints"]["j1"]
    elif change == "early_zero":
        command = observations.can[1]
        for field in ("source_time_ns", "mapped_time_ns", "wire_timestamp_ns"):
            command[field] = 2_500_000
        for row in observations.deliveries:
            if row["source"] == "main" and row["data"] == "0000000000000000":
                row["source_time_ns"] = 2_500_000
    elif change == "late_motion_receipt":
        observations.receipts[4]["event_sequence"] = 600
    elif change == "stale_receipt":
        observations.receipts[12]["payload"]["completed_frames"] = 12
    elif change == "early_uart":
        observations.uart_batches[0]["mapped_time_ns"] = 7_000_000
    with pytest.raises(AssertionError, match=error):
        observations.verify("joint-loop")


def test_joint_loop_rejects_a_combined_receipt_in_place_of_per_frame_completion():
    observations = _joint_observations()
    observations.receipts = observations.receipts[1::2]
    with pytest.raises(AssertionError, match="per-frame CAN input completion receipt"):
        observations.verify("joint-loop")


def test_joint_loop_is_explicit_and_keeps_the_original_five_native_cases():
    assert smoke.NATIVE_CASES == ("minimal-board", "can-motor", "pulse", "digital-link", "rotor-loop")
    assert smoke.EXAMPLE_ALIASES["joint-loop"] == "can-motor"


def test_native_physics_observation_copies_frozen_joint_records_to_primitive_report():
    from sim.api import Event
    from sim.physics.base import JointState

    observations = smoke.Observations()
    observations(Event(1, 0, "world", "state", "physics.state",
        {"native_time_s": .001, "joints": {"j1": JointState(.001, .2)}},
        None, 1_000_000, 1_000_000, "sampled"))
    assert observations.physics_states[0]["joints"] == {
        "j1": {"position_rad": .001, "velocity_rad_s": .2}}
    json.dumps(observations.report())


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


def _imu_observations():
    """Independent literal SI/wire/raw fixture; no converter or codec calls."""
    import struct

    result = smoke.Observations()
    uart = (b"SIM_READY imu-readback\n"
            b"IMU_READ n=1 g=000000000000 a=000000005715 t=000300\n"
            b"IMU_READ n=2 g=000000000000 a=000000005715 t=000400\nIMU_PASS\n")
    result.uart.extend(uart)
    result.snapshots = [{"mapped_time_ns": 100_000_000}]
    f32_g = struct.unpack("<f", struct.pack("<f", 9.81 / 9.80665))[0]
    for tick in range(11):
        cut = tick * 10_000_000
        sample = {"gyro_rad_s": [0.0, 0.0, 0.0], "accel_m_s2": [0.0, 0.0, 9.81]}
        result.physics_states.append({"event_sequence": tick * 100,
            "source": "world", "endpoint": "state", "source_time_ns": None,
            "mapped_time_ns": cut, "delivery_time_ns": cut, "quality": "sampled",
            "native_time_s": tick / 100, "joints": {}, "imus": {"body": sample}})
        payload = {"input": {**sample, "source_time_ns": None, "mapped_time_ns": cut}}
        if tick:
            payload.update(session_id=1, step_id=tick, t_sim_ns=cut, dt_ns=10_000_000,
                status=0, queue_depth=0, dropped_count=0,
                wire={"gyro_dps": [0.0, 0.0, 0.0], "accel_g": [0.0, 0.0, f32_g],
                      "gyro_bias_dps": [0.0, 0.0, 0.0], "accel_bias_g": [0.0, 0.0, 0.0],
                      "sensor_time_ns": cut, "sample_id": 0})
        stamp = {"source": "main", "endpoint": "imu", "source_time_ns": None,
            "mapped_time_ns": cut, "delivery_time_ns": cut, "quality": "sampled"}
        result.imu.append({**stamp, "event_sequence": tick * 100 + 1,
            "kind": "imu-input-accepted" if tick else "imu-input-initial", "payload": payload})
        if 0 < tick < 10:
            full = tick in (3, 4)
            result.imu.append({**stamp, "event_sequence": tick * 100 + 50,
                "delivery_time_ns": cut + 10_000_000,
                "kind": "imu-consumption", "payload": {**payload,
                    "status": 0 if full else 2,
                    "consumed_mask": 3 if full else 0, "missing_mask": 0 if full else 3}})
    result.uart_batches = [{"source": "main", "event_sequence": 550,
        "mapped_time_ns": 50_000_000, "quality": "upper_bound", "data_hex": uart.hex()}]
    return result


def test_imu_readback_requires_two_actual_full_consumed_sources_and_retains_pending():
    observations = _imu_observations()
    report = observations.verify("imu-readback")
    assert [row["input_cut_ns"] for row in report["imu_readbacks"]] == [30_000_000, 40_000_000]
    assert [row["sensor_ticks_u24"] for row in report["imu_readbacks"]] == [768, 1024]
    assert [row["payload"]["t_sim_ns"] for row in report["imu_pending"]] == [100_000_000]
    assert len([row for row in report["imu"] if row["kind"] == "imu-consumption"]) == 9
    json.dumps(report)


def test_imu_readback_rejects_skipped_zero_gyro_burst_despite_identical_raw_and_pass():
    observations = _imu_observations()
    for row in observations.imu:
        if row["kind"] == "imu-consumption" and row["payload"]["consumed_mask"] == 3:
            row["payload"].update(status=2, consumed_mask=1, missing_mask=2)
    with pytest.raises(AssertionError, match="matching full gyro/accel consumed DONE"):
        observations.verify("imu-readback")


@pytest.mark.parametrize("change,error", [
    ("default_gravity", "independent rest values"),
    ("wrong_sensor_time", "identify one actual admitted"),
    ("wrong_done_identity", "actual admitted identity"),
    ("early_uart", "final sensortime store"),
    ("wrong_wire", "actual float32 wire"),
    ("source_clock", "completed SI source provenance"),
])
def test_imu_readback_rejects_unattributed_or_default_sensor_evidence(change, error):
    observations = _imu_observations()
    if change == "default_gravity":
        observations.uart = observations.uart.replace(b"5715", b"5515")
        observations.uart_batches[0]["data_hex"] = bytes(observations.uart).hex()
    elif change == "wrong_sensor_time":
        observations.uart = observations.uart.replace(b"t=000300", b"t=000301")
        observations.uart_batches[0]["data_hex"] = bytes(observations.uart).hex()
    elif change == "wrong_done_identity":
        next(row for row in observations.imu if row["kind"] == "imu-consumption")["payload"]["step_id"] = 50
    elif change == "early_uart":
        observations.uart_batches[0]["event_sequence"] = 340
    elif change == "wrong_wire":
        next(row for row in observations.imu if row["kind"] == "imu-input-accepted")["payload"]["wire"]["accel_g"][2] = 1.0
    elif change == "source_clock":
        next(row for row in observations.imu if row["kind"] == "imu-input-accepted")["source_time_ns"] = 1
    with pytest.raises(AssertionError, match=error):
        observations.verify("imu-readback")


def test_imu_readback_build_and_scene_are_explicit_and_share_existing_helpers(tmp_path):
    if any(shutil.which(program) is None for program in ("cmake", "arm-none-eabi-gcc")):
        pytest.skip("optional ARM cross compiler/CMake are unavailable")
    result = smoke.build_example("imu-readback", build_dir=tmp_path / "firmware")
    assert "-DSIM_IMU_SKIP_GYRO_TEST=OFF" in result["build"][0]["command"]
    from sim.composition import builtin_registry
    from sim.config.scene import load_scene

    scene_file = tmp_path / "scene.yaml"
    scene_file.write_text((ROOT / "examples/imu-readback/scene.yaml").read_text().replace(
        "./build/firmware.elf", result["elf"]).replace(
        "../shared-world/imu.xml", str(ROOT / "examples/shared-world/imu.xml")))
    scene = load_scene(scene_file, builtin_registry())
    assert (scene.mode, scene.quantum_ns, scene.duration_ns) == ("stepped", 10_000_000, 100_000_000)
    assert [(node.name, node.kind) for node in scene.topology.nodes] == [
        ("main", "machine.qemu"), ("world", "physics.mujoco"), ("wire", "net.imu")]
    assert [(str(link.source), str(link.target)) for link in scene.topology.links] == [
        ("world.imu.body", "wire.signal"), ("wire.signal", "main.imu")]
    assert smoke.NATIVE_CASES == ("minimal-board", "can-motor", "pulse", "digital-link", "rotor-loop")


def test_imu_observation_projects_frozen_envelopes_without_serializing_backend_objects():
    from sim.api import Event, ImuSample, ImuSignalSample

    observations = smoke.Observations()
    sample = ImuSignalSample(ImuSample((0.0, 0.0, 0.0), (0.0, 0.0, 9.81)), None, 10_000_000)
    observations(Event(1, 0, "wire", "signal", "net.imu.sample", {"sample": sample},
        None, 10_000_000, 10_000_000, "sampled"))
    assert observations.imu[0]["payload"]["sample"] == {
        "sample": {"gyro_rad_s": [0.0, 0.0, 0.0], "accel_m_s2": [0.0, 0.0, 9.81]},
        "source_time_ns": None, "mapped_time_ns": 10_000_000}
    json.dumps(observations.report())


def _uart_link_observations():
    observations = smoke.Observations()
    observations.uart.extend(bytes.fromhex('007f80ff35a61053') +
        b'SIM_READY uart-link\nUART_RX sum=033c n=8\nUART_PASS\n')
    observations.snapshots = [
        {'source': 'main', 'mapped_time_ns': cut, 'native_timestamp_ns': 71 + cut,
         'event_sequence': sequence, 'b0_output': True, 'b0': level}
        for cut, sequence, level in ((0, 0, 0), (6_000_000, 1, 0),
                                     (7_000_000, 20, 1), (40_000_000, 30, 1))]
    # Independent fragments in each direction; no packet boundary assumptions.
    for sequence, source, data, cut in (
            (0, 'main.uart1', bytes(observations.uart[:2]), 6_000_000),
            (1, 'main.uart1', bytes(observations.uart[2:8]), 6_000_000),
            (2, 'echo.uart', bytes(observations.uart[:4]), 6_000_000),
            (3, 'echo.uart', bytes(observations.uart[4:8]), 6_000_000),
            (4, 'main.uart1', bytes(observations.uart[8:]), 12_000_000),
            (5, 'echo.uart', bytes(observations.uart[8:]), 12_000_000)):
        stamp = {'source': 'wire', 'endpoint': 'uart', 'mapped_time_ns': cut,
                 'delivery_time_ns': cut, 'event_sequence': 2 + sequence,
                 'source_time_ns': 71 + cut if source == 'main.uart1' else None,
                 'quality': 'upper_bound' if source == 'main.uart1' else 'sampled'}
        payload = {'source': source, 'sequence': sequence, 'due_ns': cut, 'data_hex': data.hex()}
        observations.uart_net.append({**stamp, 'kind': 'net.uart.sent', 'payload': payload})
        observations.uart_net.append({**stamp, 'kind': 'net.uart.delivery', 'payload': {
            **payload, 'destination': 'echo.uart' if source == 'main.uart1' else 'main.uart1'}})
    return observations


def test_uart_link_acceptance_handles_binary_fragmentation_and_independent_echo():
    observations = _uart_link_observations()
    report = observations.verify('uart-link')
    assert report['uart_consumption']['computed_sum'] == 828
    assert report['uart_consumption']['gpio_response_ns'] == 7_000_000
    json.dumps(report)


def test_uart_link_rejects_skip_rx_despite_same_correct_checksum_and_pass():
    observations = _uart_link_observations()
    for row in observations.snapshots:
        row['b0'] = 0
    with pytest.raises(AssertionError, match='RX-dependent GPIO'):
        observations.verify('uart-link')


def test_uart_link_rejects_transport_only_or_rewritten_time_evidence():
    observations = _uart_link_observations()
    observations.uart_net = [row for row in observations.uart_net
                             if row['payload']['source'] != 'echo.uart']
    with pytest.raises(AssertionError, match='echo deliveries'):
        observations.verify('uart-link')
    observations = _uart_link_observations()
    observations.uart_net[1]['source_time_ns'] = 0
    with pytest.raises(AssertionError, match='provenance'):
        observations.verify('uart-link')


def test_uart_link_build_and_scene_use_real_elf_and_independent_python_echo(tmp_path):
    if any(shutil.which(program) is None for program in ('cmake', 'arm-none-eabi-gcc')):
        pytest.skip('optional ARM cross compiler/CMake are unavailable')
    result = smoke.build_example('uart-link', build_dir=tmp_path / 'firmware')
    assert '-DSIM_UART_SKIP_RX_TEST=OFF' in result['build'][0]['command']
    from sim.composition import builtin_registry
    from sim.config.scene import load_scene

    scene_file = tmp_path / 'scene.yaml'
    scene_file.write_text((ROOT / 'examples/uart-link/scene.yaml').read_text().replace(
        './build/firmware.elf', result['elf']))
    scene = load_scene(scene_file, builtin_registry())
    assert (scene.quantum_ns, scene.duration_ns) == (1_000_000, 40_000_000)
    assert [(node.name, node.kind) for node in scene.topology.nodes] == [
        ('main', 'machine.qemu'), ('echo', 'device.uart-echo'), ('wire', 'net.uart')]
    assert smoke.NATIVE_CASES == ('minimal-board', 'can-motor', 'pulse', 'digital-link', 'rotor-loop')
