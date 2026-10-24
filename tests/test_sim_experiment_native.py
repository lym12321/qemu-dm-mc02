"""Opt-in real ELF consumers of the Simulation experiment application.

Set DM_MC02_EXPERIMENT_QEMU to the existing project native binary. Compilation
reuses the canonical simulation-smoke helper; no firmware or launcher copy.
"""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

import pytest

from sim.api import NodeSpec, Topology
from sim.apps.experiment import ExperimentInput, run_experiment
from sim.config.scene import Scene


@pytest.mark.parametrize("case", ["minimal-board", "pulse"])
@pytest.mark.parametrize("build_type", ["Debug", "Release"])
def test_real_qemu_experiment_reuses_firmware_smoke_and_cleans_before_result(tmp_path, case, build_type):
    configured_qemu = os.environ.get("DM_MC02_EXPERIMENT_QEMU")
    if configured_qemu is None:
        pytest.skip("set DM_MC02_EXPERIMENT_QEMU for the separate actual firmware gate")
    qemu = Path(configured_qemu).resolve(strict=True)
    from smoke.simulation_smoke import Observations, build_example
    from sim.composition import builtin_registry

    build = build_example(case, build_dir=tmp_path / "firmware", build_type=build_type)
    digest = hashlib.sha256(qemu.read_bytes()).hexdigest()
    registry = builtin_registry(qemu=str(qemu))
    factory = registry["machine.qemu"]
    machines, resources = [], []
    original = factory.create

    def capture(spec, context):
        machine = original(spec, context)
        machines.append(machine)
        return machine

    factory.create = capture
    observations = Observations()

    def observe(event):
        observations(event)
        if not resources:
            info = machines[0].connection_info
            pid = info["pid"]
            starttime = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            resources.append({"pid": pid, "starttime": starttime, "sockets": dict(info["sockets"])})

    pulse = case == "pulse"
    scene = Scene(Topology((NodeSpec("main", "machine.qemu", {
        "model": "dm-mc02", "firmware": build["elf"]}),), ()),
        quantum_ns=10_000_000 if pulse else 1_000_000,
        duration_ns=20_000_000 if pulse else 200_000_000)
    result = run_experiment(scene, registry, inputs=(
        ExperimentInput(3_000_000 if pulse else 40_000_000, "main.key", True),
        ExperimentInput(4_000_000 if pulse else 90_000_000, "main.key", False)), assertions=(
        {"before": "20ms" if pulse else "200ms", "uart_contains": {
            "endpoint": "main.uart1", "text": "KEY pressed B0=1\nKEY released B0=0\n"}},
        {"before": "5ms" if pulse else "50ms", "digital_equals": {
            "endpoint": "main.gpio.B0", "value": 1}}), on_event=observe)
    assert result.passed and result.delivered_inputs == 2
    if pulse:
        # One run_until20ms uses the existing3/4/14/20ms cuts. The original
        # smoke instead requests step10ms before run_until20ms, so its10ms
        # snapshot requirement belongs to that different public call sequence.
        assert b"KEY released B0=0\nKEY pressed B0=1\nKEY released B0=0\n" in observations.uart
        assert b"FAIL" not in observations.uart
        assert [(row["value"], row["delivery_time_ns"]) for row in observations.inputs] == [
            (True, 3_000_000), (False, 4_000_000)]
        for cut, key, b0 in ((3_000_000, 1, 0), (4_000_000, 0, 1), (14_000_000, 1, 0)):
            rows = [row for row in observations.snapshots if row["mapped_time_ns"] == cut]
            assert len(rows) == 1
            row = rows[0]
            assert (row["key"], row["b0"], row["b0_output"]) == (key, b0, True)
            assert row["delivery_time_ns"] == cut and row["quality"] == "exact"
            assert row["native_timestamp_ns"] - observations.snapshots[0]["native_timestamp_ns"] == cut
        independent = observations.report()
    else:
        independent = observations.verify(case)
    assert resources
    for resource in resources:
        stat = Path(f"/proc/{resource['pid']}/stat")
        assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[19] != resource["starttime"]
        assert not any(Path(path).exists() for path in resource["sockets"].values())
    assert hashlib.sha256(qemu.read_bytes()).hexdigest() == digest
    assert hashlib.sha256(Path(build["elf"]).read_bytes()).hexdigest() == build["sha256"]
    (tmp_path / "report.json").write_text(json.dumps({"experiment": asdict(result),
        "independent_firmware_acceptance": independent, "build": build,
        "qemu_sha256": digest, "resources": resources, "cleanup_completed": True}, indent=2) + "\n")
