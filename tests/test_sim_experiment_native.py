"""Opt-in real ELF consumers of the Simulation experiment application.

Set DM_MC02_EXPERIMENT_QEMU to the existing project native binary. Compilation
reuses the canonical simulation-smoke helper; no firmware or launcher copy.
"""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
from time import monotonic
from types import SimpleNamespace

import pytest

from sim.api import NodeDescription, NodeSpec, StepResult, Topology
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


def test_real_external_qmp_advance_during_snapshot_freezes_before_host_step(tmp_path):
    """A second native monitor advances the CPU; no mock supplies its receipt."""
    configured = os.environ.get("DM_MC02_EXPERIMENT_QEMU")
    if configured is None:
        pytest.skip("set DM_MC02_EXPERIMENT_QEMU for the separate actual firmware gate")
    from smoke.simulation_smoke import build_example
    from sim.machines.qemu import QemuFactory
    from sim.machines.qemu.deadline import deadline_qmp_call
    from sim.machines.qemu.qmp import qmp_client_provenance, qmp_session_class
    from sim.runtime import Simulation, SimulationState

    qemu = Path(configured).resolve(strict=True)
    digest = hashlib.sha256(qemu.read_bytes()).hexdigest()
    build = build_example("minimal-board", build_dir=tmp_path / "firmware")
    root = Path(__file__).resolve().parents[1]
    evidence = {"gate_status": "FAIL", "execution": {"status": "NOT RUN"}, "python": sys.version,
        "build": build, "qemu": {"path": str(qemu), "sha256": digest,
            "version": subprocess.check_output([str(qemu), "--version"], text=True, timeout=3)},
        "qmp_client": qmp_client_provenance(), "native_observations": [],
        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("sim/machines/qemu/machine.py", "sim/machines/qemu/profile.py",
                         "tests/test_sim_experiment_native.py")},
        "host_steps": [], "snapshots": [], "resources": [], "cleanup_errors": []}

    # This Host-only witness owns no solver or native handles. Its empty step
    # record proves machine failure stops the next Runtime execution phase.
    class HostWitness:
        close_count = 0

        def step(self, context):
            evidence["host_steps"].append(asdict(context))
            return StepResult("ok", context.t_sim_ns)

        def close(self):
            self.close_count += 1

    witness = HostWitness()
    host = SimpleNamespace(describe=lambda _spec: NodeDescription(capabilities={"step"}),
                           create=lambda _spec, _context: witness)
    independent = None
    primary = None
    verified = False
    temporary = tempfile.TemporaryDirectory(prefix="dm-qemu.foreign-monitor.", dir="/tmp")
    external = Path(temporary.name) / "qmp.sock"
    factory = QemuFactory(qemu=qemu, qmp_path=external)
    simulation = None

    def observe_native(label, expected_ns):
        deadline = monotonic() + 2
        execution = deadline_qmp_call(independent, deadline, "external monitor",
            lambda: independent.command("x-query-virtual-execution"))
        status = deadline_qmp_call(independent, deadline, "external monitor",
            lambda: independent.command("query-status"))
        row = {"phase": label, "execution": execution, "status": status}
        evidence["native_observations"].append(row)
        assert execution == {"state": "reached", "current-ns": expected_ns, "target-ns": expected_ns}
        assert status["running"] is False and status["status"] == "paused"
        return row

    def observe(event):
        nonlocal independent
        if event.kind != "snapshot":
            return
        evidence["snapshots"].append({"session_id": event.session_id,
            "source_time_ns": event.source_time_ns, "mapped_time_ns": event.mapped_time_ns,
            "delivery_time_ns": event.delivery_time_ns, "quality": str(event.quality)})
        if not evidence["resources"]:
            info = simulation.components["main"].connection_info
            pid = info["pid"]
            start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            sockets = {**dict(info["sockets"]), "external_qmp": str(external)}
            evidence["resources"].append({"pid": pid, "starttime": start,
                "session_id": event.session_id, "native_origin_ns": info["native_origin_ns"],
                "sockets": sockets,
                "socket_inodes": {name: Path(path).stat().st_ino for name, path in sockets.items()}})
        if event.mapped_time_ns == 0 or independent is not None:
            return
        independent = qmp_session_class()(str(external), timeout=1, connect_timeout=1, close_timeout=1)
        observe_native("before_external_advance", event.source_time_ns)
        target = event.source_time_ns + 1_000_000
        deadline = monotonic() + 2
        deadline_qmp_call(independent, deadline, "external advance",
            lambda: independent.command("x-run-to-virtual-time", {"target-ns": target}))
        while True:
            execution = deadline_qmp_call(independent, deadline, "external advance",
                lambda: independent.command("x-query-virtual-execution"))
            assert execution["target-ns"] == target
            assert event.source_time_ns <= execution["current-ns"] <= target
            if execution["state"] == "reached":
                break
            assert execution["state"] == "running"
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("external native target was not reached")
            select.select([], [], [], min(.001, remaining))
        observe_native("after_external_advance", target)

    simulation = Simulation(Topology((NodeSpec("main", "machine", {"firmware": build["elf"]}),
                                     NodeSpec("host", "host"))),
        {"machine": factory, "host": host}, quantum_ns=1_000_000,
        startup_timeout_s=5, host_timeout_s=10, on_event=observe)
    try:
        simulation.start()
        try:
            simulation.run_until(3_000_000)
        except BaseException as error:
            primary = error
            evidence["execution"] = {"status": "FAIL", "error_type": type(error).__name__,
                "error": str(error), "notes": list(getattr(error, "__notes__", ()))}
        else:
            evidence["execution"] = {"status": "PASS"}
        evidence["failed_state"] = {"state": str(simulation.state), "time_ns": simulation.t_sim_ns,
            "step_id": simulation.step_id, "session": asdict(simulation.session),
            "machine_time_ns": simulation.components["main"].t_sim_ns}
        assert isinstance(primary, RuntimeError), evidence["execution"]
        assert "QEMU completed cut changed outside Simulation control" in str(primary)
        assert simulation.state == SimulationState.FAILED and simulation.failure is primary
        assert simulation.t_sim_ns == simulation.step_id == 0
        assert simulation.components["main"].t_sim_ns == 0
        assert evidence["host_steps"] == []
        assert [row["mapped_time_ns"] for row in evidence["snapshots"]] == [0, 1_000_000]
        with pytest.raises(RuntimeError, match="paused, healthy"):
            simulation.run_until(3_000_000)
        with pytest.raises(RuntimeError, match="paused, healthy"):
            simulation.reset()
        observe_native("after_rejected_reexecution",
                       evidence["native_observations"][1]["execution"]["current-ns"])
        verified = True
    finally:
        active_error = sys.exception()
        failures = []
        for label, resource in (("independent QMP", independent), ("Simulation", simulation)):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    failures.append((label, error))
        try:
            for resource in evidence["resources"]:
                stat = Path(f"/proc/{resource['pid']}/stat")
                assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[19] != resource["starttime"]
                assert not any(Path(path).exists() for path in resource["sockets"].values())
            assert simulation.state == SimulationState.CLOSED and witness.close_count == 1
            assert hashlib.sha256(qemu.read_bytes()).hexdigest() == digest
            assert hashlib.sha256(Path(build["elf"]).read_bytes()).hexdigest() == build["sha256"]
            assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
                       for name, expected in evidence["source_sha256"].items())
        except BaseException as error:
            failures.append(("cleanup or input identity verification", error))
        try:
            temporary.cleanup()
        except BaseException as error:
            failures.append(("private monitor directory", error))
        error = active_error or primary
        for label, failure in failures:
            if error is None:
                error = failure
            elif error is not failure:
                error.add_note(f"{label}: {type(failure).__name__}: {failure}")
            if primary is not None and primary is not error and primary is not failure:
                primary.add_note(f"{label}: {type(failure).__name__}: {failure}")
            evidence["cleanup_errors"].append(f"{label}: {type(failure).__name__}: {failure}")
        if primary is not None:
            evidence["execution"]["notes"] = list(getattr(primary, "__notes__", ()))
        evidence["cleanup_completed"] = not failures
        evidence["expected_native_failure_verified"] = verified
        evidence["gate_status"] = "PASS" if verified and active_error is None and not failures else "FAIL"
        (tmp_path / "external-monitor-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        if failures and active_error is None:
            raise error
