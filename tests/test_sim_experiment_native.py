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
import signal
import subprocess
import sys
import tempfile
from time import monotonic
from types import SimpleNamespace

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, StepResult, Topology
from sim.apps.experiment import ExperimentInput, run_experiment
from sim.config.scene import Scene


def _verify_smoke_native_identity_and_cleanup(observations, build):
    assert observations["cleanup_completed"] is True
    assert observations["cleanup_failures"] == []
    assert len(observations["resources"]) == 1
    resource = observations["resources"][0]
    assert resource["firmware"] == {"path": build["elf"], "sha256": build["sha256"]}
    argv = resource["argv"]
    assert argv.count("-kernel") == 1 and argv[argv.index("-kernel") + 1] == build["elf"]
    stat = Path(f"/proc/{resource['pid']}/stat")
    assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[19] != resource["starttime"]
    assert not any(Path(path).exists() for path in resource["sockets"].values())
    assert hashlib.sha256(Path(build["elf"]).read_bytes()).hexdigest() == build["sha256"]


def test_real_smoke_runs_requested_incompatible_elf_instead_of_valid_manifest_default(
        tmp_path, monkeypatch):
    """A valid default must not hide that the explicitly requested ELF is wrong."""
    configured = os.environ.get("DM_MC02_EXPERIMENT_QEMU")
    if configured is None:
        pytest.skip("set DM_MC02_EXPERIMENT_QEMU for the separate actual firmware gate")
    from smoke import simulation_smoke as smoke

    report = tmp_path / "exact-elf-negative-evidence.json"
    evidence = {"gate_status": "FAIL", "execution": {"status": "NOT RUN"},
                "precision_limit": "completed GPIO samples and UART read upper bounds; no byte edge timing"}
    try:
        qemu = Path(configured).resolve(strict=True)
        evidence["qemu"] = {"path": str(qemu),
            "sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(),
            "version": subprocess.check_output([str(qemu), "--version"], text=True, timeout=3)}
        default = smoke.build_example("minimal-board", build_dir=tmp_path / "default-firmware")
        requested = smoke.build_example("can-motor", build_dir=tmp_path / "requested-firmware")
        evidence["firmware"] = {"valid_default": default, "incompatible_requested": requested}
        # Keep an actually valid default in a private manifest root. The old
        # ignored-firmware path would execute it and incorrectly pass this run.
        fixtures = tmp_path / "private-manifests"
        manifest = fixtures / "examples/minimal-board/scene.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text((smoke.ROOT / "examples/minimal-board/scene.yaml").read_text().replace(
            "./build/firmware.elf", default["elf"]))
        evidence["manifest"] = {"path": str(manifest),
            "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
        monkeypatch.setattr(smoke, "ROOT", fixtures)
        evidence["positive_default"] = smoke.run_case("minimal-board", qemu=qemu,
                                                       firmware=Path(default["elf"]))
        try:
            result = smoke.run_case("minimal-board", qemu=qemu, firmware=Path(requested["elf"]))
        except BaseException as error:
            evidence["execution"] = {"status": "FAIL", "expected": True,
                "error_type": type(error).__name__, "error": str(error),
                "notes": list(getattr(error, "__notes__", ())),
                "observations": getattr(error, "simulation_observations", {})}
            if isinstance(error, smoke.Blocked):
                raise
            assert isinstance(error, AssertionError), evidence["execution"]
        else:
            evidence["execution"] = {"status": "PASS", "observations": result}
            pytest.fail("incompatible requested ELF was accepted using the manifest default")
        actual = evidence["execution"]["observations"]
        assert "SIM_READY can-motor\n" in actual["uart"]
        assert "SIM_READY minimal-board\n" not in actual["uart"]
        for observations, build in ((evidence["positive_default"], default), (actual, requested)):
            _verify_smoke_native_identity_and_cleanup(observations, build)
        assert hashlib.sha256(qemu.read_bytes()).hexdigest() == evidence["qemu"]["sha256"]
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == evidence["manifest"]["sha256"]
        evidence["gate_status"] = "PASS"
    except smoke.Blocked as error:
        evidence.update(gate_status="BLOCKED", reason=str(error))
        pytest.skip(f"BLOCKED: {error}")
    finally:
        if sys.exception() is not None:
            error = sys.exception()
            evidence["gate_error"] = {"error_type": type(error).__name__, "error": str(error)}
        report.write_text(json.dumps(evidence, indent=2) + "\n")


def test_real_uart_skip_rx_mutant_rejects_apparent_pass_and_keeps_native_evidence(tmp_path):
    configured = os.environ.get("DM_MC02_EXPERIMENT_QEMU")
    if configured is None:
        pytest.skip("set DM_MC02_EXPERIMENT_QEMU for the separate actual firmware gate")
    from smoke import simulation_smoke as smoke

    evidence = {"gate_status": "FAIL", "execution": {"status": "NOT RUN"},
                "precision_limit": "sampled GPIO proves RX-dependent response; no exact UART byte timing"}
    try:
        qemu = Path(configured).resolve(strict=True)
        evidence["qemu"] = {"path": str(qemu), "sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(),
            "version": subprocess.check_output([str(qemu), "--version"], text=True, timeout=3)}
        build = smoke.build_example("uart-link", build_dir=tmp_path / "firmware", uart_skip_rx=True)
        evidence["firmware"] = build
        try:
            result = smoke.run_case("uart-link", qemu=qemu, firmware=Path(build["elf"]))
        except BaseException as error:
            evidence["execution"] = {"status": "FAIL", "expected": True,
                "error_type": type(error).__name__, "error": str(error),
                "notes": list(getattr(error, "__notes__", ())),
                "observations": getattr(error, "simulation_observations", {})}
            if isinstance(error, smoke.Blocked):
                raise
            assert isinstance(error, AssertionError) and "RX-dependent GPIO" in str(error)
        else:
            evidence["execution"] = {"status": "PASS", "observations": result}
            pytest.fail("UART mutant's apparent PASS hid its missing receive-dependent response")
        observations = evidence["execution"]["observations"]
        assert "UART_RX sum=033c n=8\nUART_PASS\n" in observations["uart"]
        _verify_smoke_native_identity_and_cleanup(observations, build)
        assert hashlib.sha256(qemu.read_bytes()).hexdigest() == evidence["qemu"]["sha256"]
        evidence["gate_status"] = "PASS"
    except smoke.Blocked as error:
        evidence.update(gate_status="BLOCKED", reason=str(error))
        pytest.skip(f"BLOCKED: {error}")
    finally:
        if sys.exception() is not None:
            error = sys.exception()
            evidence["gate_error"] = {"error_type": type(error).__name__, "error": str(error)}
        (tmp_path / "uart-mutant-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")


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


def test_real_qemu_cuts_and_reset_do_not_retain_internal_qmp_events(tmp_path, monkeypatch):
    """Actual STOP/RESUME traffic stays unretained across fresh native sessions."""
    configured = os.environ.get("DM_MC02_EXPERIMENT_QEMU")
    if configured is None:
        pytest.skip("set DM_MC02_EXPERIMENT_QEMU for the separate actual firmware gate")
    from smoke.simulation_smoke import Observations, build_example
    from sim.machines.qemu import QemuFactory
    from sim.machines.qemu import machine as adapter
    from sim.machines.qemu.qmp import qmp_client_provenance, qmp_session_class
    from sim.runtime import Simulation, SimulationState

    qemu = Path(configured).resolve(strict=True)
    digest = hashlib.sha256(qemu.read_bytes()).hexdigest()
    build = build_example("minimal-board", build_dir=tmp_path / "firmware")
    root = Path(__file__).resolve().parents[1]
    evidence = {"gate_status": "FAIL", "execution": {"status": "NOT RUN"},
        "python": sys.version, "build": build,
        "qemu": {"path": str(qemu), "sha256": digest,
            "version": subprocess.check_output([str(qemu), "--version"], text=True, timeout=3)},
        "qmp_client": qmp_client_provenance(),
        "conditions": {"quantum_ns": 1_000_000, "cuts_per_session": 1000,
            "sessions": 2, "startup_timeout_s": 10, "host_timeout_s": 60,
            "uart_timestamp_quality": "upper_bound", "snapshot_timestamp_quality": "exact"},
        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("sim/machines/qemu/machine.py", "sim/machines/qemu/qmp.py",
                         "tests/test_sim_experiment_native.py")},
        "resources": [], "checkpoints": [], "firmware_acceptance": [], "cleanup_errors": []}
    clients, observations = [], {}
    session_class = qmp_session_class()
    # Test-only capture of the real installed client. A separate upstream
    # listener counts native events without retaining them; it neither changes
    # the default listener nor manufactures protocol or execution results.
    from qemu.qmp.events import EventListener

    class CapturedSession(session_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            row = {"client": self, "retain_events": kwargs.get("retain_events", True),
                   "native_events": {"STOP": 0, "RESUME": 0, "other": 0}}
            clients.append(row)

            def count_native_event(event):
                name = event["event"]
                counts = row["native_events"]
                counts[name if name in ("STOP", "RESUME") else "other"] += 1
                return False

            self._qmp.register_listener(EventListener(event_filter=count_native_event))

    monkeypatch.setattr(adapter, "qmp_session_class", lambda: CapturedSession)
    simulation = None
    verified = False

    def resource_released(resource):
        stat = Path(f"/proc/{resource['pid']}/stat")
        assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[19] != resource["starttime"]
        assert not any(Path(path).exists() for path in resource["sockets"].values())
        assert not any(Path(path).exists() for path in resource["private_directories"])

    def observe(event):
        if event.session_id not in observations:
            observations[event.session_id] = Observations()
        observations[event.session_id](event)
        if event.kind != "snapshot":
            return
        resource = next((row for row in evidence["resources"]
                         if row["session_id"] == event.session_id), None)
        if resource is None:
            info = simulation.components["main"].connection_info
            pid = info["pid"]
            sockets = dict(info["sockets"])
            resource = {"session_id": event.session_id, "pid": pid,
                "starttime": Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
                "argv": Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0"),
                "native_origin_ns": info["native_origin_ns"], "sockets": sockets,
                "socket_inodes": {name: Path(path).stat().st_ino for name, path in sockets.items()},
                "private_directories": sorted({str(Path(path).parent) for path in sockets.values()})}
            evidence["resources"].append(resource)
        assert event.source_time_ns == event.payload["timestamp_ns"]
        assert event.source_time_ns == resource["native_origin_ns"] + event.mapped_time_ns
        assert event.delivery_time_ns == event.mapped_time_ns and event.quality == "exact"

    def checkpoint(cuts):
        client = clients[-1]["client"]
        machine = simulation.components["main"]
        execution, status = machine.command_many(
            (("x-query-virtual-execution", None), ("query-status", None)))
        listener = client._qmp.events
        row = {"session_id": simulation.session.session_id, "cuts": cuts,
            "simulation_time_ns": simulation.t_sim_ns, "machine_time_ns": machine.t_sim_ns,
            "execution": execution, "status": status,
            "queue_size": listener._queue.qsize(), "history_size": len(listener.history),
            "native_events": dict(clients[-1]["native_events"]),
            "retain_events": clients[-1]["retain_events"]}
        evidence["checkpoints"].append(row)
        assert row["retain_events"] is False
        assert row["queue_size"] == row["history_size"] == 0
        assert simulation.state == SimulationState.PAUSED
        assert simulation.step_id == cuts
        assert simulation.t_sim_ns == machine.t_sim_ns == cuts * 1_000_000
        expected = machine.native_origin_ns + simulation.t_sim_ns
        assert execution == ({"state": "reached", "current-ns": expected, "target-ns": expected}
                             if cuts else {"state": "idle", "current-ns": expected})
        assert status["running"] is False and status["status"] in (("paused",) if cuts else ("paused", "prelaunch"))
        assert row["native_events"]["STOP"] == row["native_events"]["RESUME"] == cuts

    simulation = Simulation(Topology((NodeSpec("main", "machine", {"firmware": build["elf"]}),)),
        {"machine": QemuFactory(qemu=qemu)}, quantum_ns=1_000_000,
        startup_timeout_s=10, host_timeout_s=60, event_history=16, on_event=observe)
    try:
        simulation.start()
        for session_index in range(2):
            assert len(clients) == session_index + 1
            checkpoint(0)
            simulation.schedule_input("main.key", True, at_ns=40_000_000)
            simulation.schedule_input("main.key", False, at_ns=90_000_000)
            for cuts in (100, 250, 500, 1000):
                simulation.run_until(cuts * 1_000_000)
                checkpoint(cuts)
            actual = observations[simulation.session.session_id]
            acceptance = actual.verify("minimal-board")
            evidence["firmware_acceptance"].append({"session_id": simulation.session.session_id,
                                                    "observations": acceptance})
            assert [row["mapped_time_ns"] for row in actual.snapshots] == [
                cut * 1_000_000 for cut in range(1001)]
            assert [(row["value"], row["source_time_ns"], row["delivery_time_ns"])
                    for row in actual.inputs] == [(True, 40_000_000, 40_000_000),
                                                  (False, 90_000_000, 90_000_000)]
            origin = evidence["resources"][-1]["native_origin_ns"]
            assert actual.uart_batches
            assert all(row["source_time_ns"] == origin + row["mapped_time_ns"]
                       and row["delivery_time_ns"] == row["mapped_time_ns"]
                       and row["quality"] == "upper_bound" for row in actual.uart_batches)
            assert len(simulation.events) <= 16
            if session_index == 0:
                simulation.reset()
                resource_released(evidence["resources"][0])
                assert clients[0]["client"] is not clients[1]["client"]
                first, second = evidence["resources"]
                assert (first["pid"], first["starttime"]) != (second["pid"], second["starttime"])
                assert set(first["sockets"].values()).isdisjoint(second["sockets"].values())
        evidence["execution"] = {"status": "PASS", "completed_cuts": 2000}
        verified = True
    except BaseException as error:
        evidence["execution"] = {"status": "FAIL", "error_type": type(error).__name__,
            "error": str(error), "notes": list(getattr(error, "__notes__", ()))}
        raise
    finally:
        active_error = sys.exception()
        failures = []
        try:
            simulation.close()
            simulation.close()
        except BaseException as error:
            failures.append(("Simulation cleanup", error))
        try:
            for resource in evidence["resources"]:
                resource_released(resource)
            assert simulation.state == SimulationState.CLOSED
            assert hashlib.sha256(qemu.read_bytes()).hexdigest() == digest
            assert hashlib.sha256(Path(build["elf"]).read_bytes()).hexdigest() == build["sha256"]
            assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
                       for name, expected in evidence["source_sha256"].items())
            for row in clients:
                assert row["client"]._qmp.events.empty()
                assert row["client"]._qmp.events.history == ()
        except BaseException as error:
            failures.append(("cleanup or input identity verification", error))
        for label, failure in failures:
            if active_error is not None and failure is not active_error:
                active_error.add_note(f"{label}: {type(failure).__name__}: {failure}")
            evidence["cleanup_errors"].append(f"{label}: {type(failure).__name__}: {failure}")
        evidence["clients"] = [{"retain_events": row["retain_events"],
            "native_events": row["native_events"],
            "queue_size": row["client"]._qmp.events._queue.qsize(),
            "history_size": len(row["client"]._qmp.events.history)} for row in clients]
        evidence["partial_observations"] = {str(session): actual.report()
                                             for session, actual in observations.items()}
        evidence["cleanup_completed"] = not failures
        evidence["gate_status"] = "PASS" if verified and active_error is None and not failures else "FAIL"
        if active_error is not None:
            evidence["execution"]["notes"] = list(getattr(active_error, "__notes__", ()))
        (tmp_path / "event-retention-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        if failures and active_error is None:
            raise failures[0][1]


def test_real_paused_process_exit_immediately_freezes_independent_io(tmp_path):
    """An exited real MCU freezes borrowed Host I/O before another run call."""
    from smoke.simulation_smoke import Blocked, Observations, build_example
    from sim.api import BackendUnavailable
    from sim.composition import builtin_registry
    from sim.machines.qemu.qmp import qmp_client_provenance
    from sim.runtime import Simulation, SimulationState

    report = tmp_path / "paused-exit-evidence.json"
    evidence = {"gate_status": "NOT RUN", "python": sys.version,
        "execution": {"status": "NOT RUN"}, "resources": [], "cleanup_errors": [],
        "conditions": {"quantum_ns": 1_000_000, "completed_cut_ns": 10_000_000,
            "startup_timeout_s": 10, "host_timeout_s": 20,
            "process_exit_timeout_s": 3, "termination": "SIGTERM through pidfd",
            "uart_timestamp_quality": "upper_bound",
            "precision_limit": "UART read observation is not a guest byte-consumption timestamp"}}

    def unavailable(status, reason):
        evidence.update(gate_status=status, reason=reason)
        report.write_text(json.dumps(evidence, indent=2) + "\n")
        pytest.skip(f"{status}: {reason}")

    configured = os.environ.get("DM_MC02_EXPERIMENT_QEMU")
    if configured is None:
        unavailable("NOT RUN", "set DM_MC02_EXPERIMENT_QEMU for the actual firmware gate")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        unavailable("BLOCKED", "Linux pidfd support is required for bounded native exit evidence")
    qemu = Path(configured).resolve()
    if not qemu.is_file() or not os.access(qemu, os.X_OK):
        unavailable("BLOCKED", f"project native QEMU executable is unavailable: {qemu}")
    try:
        build = build_example("minimal-board", build_dir=tmp_path / "firmware")
        provenance = qmp_client_provenance()
    except (Blocked, BackendUnavailable) as error:
        unavailable("BLOCKED", str(error))
    except BaseException as error:
        evidence.update(gate_status="FAIL", build_error={
            "type": type(error).__name__, "error": str(error)})
        report.write_text(json.dumps(evidence, indent=2) + "\n")
        raise
    evidence.update(gate_status="FAIL", build=build,
        qemu={"path": str(qemu), "sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(),
              "version": subprocess.check_output([str(qemu), "--version"], text=True, timeout=3)},
        qmp_client=provenance)
    observations = Observations()
    # Real protocol-neutral fixtures borrow the actual UART fabric. Queued
    # traffic is left pending so no echo feedback loop needs to be executed.
    simulation = Simulation(Topology((
        NodeSpec("main", "machine.qemu", {"firmware": build["elf"]}),
        NodeSpec("left", "device.uart-echo"), NodeSpec("right", "device.uart-echo"),
        NodeSpec("wire", "net.uart")), (
        LinkSpec("left.uart", "wire.uart"), LinkSpec("right.uart", "wire.uart"))),
        builtin_registry(qemu=str(qemu)), quantum_ns=1_000_000,
        startup_timeout_s=10, host_timeout_s=20, on_event=observations)
    primary = None
    verified = False
    try:
        simulation.start()
        machine, sender, wire = (simulation.components[name] for name in ("main", "left", "wire"))
        info = machine.connection_info
        pid = info["pid"]
        sockets = dict(info["sockets"])
        evidence["resources"].append({"pid": pid,
            "starttime": Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
            "sockets": sockets, "socket_inodes": {
                name: Path(path).stat().st_ino for name, path in sockets.items()},
            "private_directories": sorted({str(Path(path).parent) for path in sockets.values()})})
        simulation.run_until(10_000_000)
        assert b"SIM_READY minimal-board\n" in observations.uart
        assert simulation.state == SimulationState.PAUSED
        assert simulation.t_sim_ns == machine.t_sim_ns == 10_000_000
        assert simulation.step_id == 10
        assert observations.uart_batches
        assert all(row["quality"] == "upper_bound"
                   and row["source_time_ns"] == info["native_origin_ns"] + row["mapped_time_ns"]
                   and row["delivery_time_ns"] == row["mapped_time_ns"]
                   for row in observations.uart_batches)
        execution, status = machine.command_many(
            (("x-query-virtual-execution", None), ("query-status", None)))
        expected_native = info["native_origin_ns"] + simulation.t_sim_ns
        assert execution == {"state": "reached", "current-ns": expected_native,
                             "target-ns": expected_native}
        assert status["running"] is False and status["status"] == "paused"
        evidence["before_exit"] = {"execution": execution, "status": status,
            "native_origin_ns": info["native_origin_ns"],
            "time_ns": simulation.t_sim_ns, "step_id": simulation.step_id,
            "uart_batches": observations.uart_batches}
        sender.receive_uart(b"already queued")
        pending = (wire.pending, wire.pending_bytes, len(simulation.events))
        assert pending[:2] == (1, len(b"already queued"))
        pidfd = os.pidfd_open(pid)
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            assert select.select([pidfd], [], [], 3)[0] == [pidfd], "native process did not exit"
        finally:
            os.close(pidfd)
        # A direct public command must expose the failure now, without the
        # Runtime entering another cut or a callback supplying a fake receipt.
        with pytest.raises(RuntimeError, match="QEMU exited") as caught:
            machine.command("query-status")
        primary = caught.value
        evidence["execution"] = {"status": "FAIL", "expected": True,
            "error_type": type(primary).__name__, "error": str(primary)}
        evidence["after_exit"] = {"state": str(simulation.state),
            "time_ns": simulation.t_sim_ns, "step_id": simulation.step_id,
            "failure_is_primary": simulation.failure is primary}
        assert simulation.state == SimulationState.FAILED and simulation.failure is primary
        with pytest.raises(RuntimeError):
            sender.receive_uart(b"must not queue")
        assert (wire.pending, wire.pending_bytes, len(simulation.events)) == pending
        assert simulation.t_sim_ns == machine.t_sim_ns == 10_000_000
        assert simulation.step_id == 10 and simulation.failure is primary
        for operation in (lambda: simulation.run_until(11_000_000), simulation.reset):
            with pytest.raises(RuntimeError, match="paused, healthy"):
                operation()
        evidence["independent_io"] = {"rejected": True, "pending_chunks": wire.pending,
            "pending_bytes": wire.pending_bytes, "events_unchanged": True}
        verified = True
    except BackendUnavailable as error:
        unavailable("BLOCKED", str(error))
    finally:
        active_error = sys.exception()
        failures = []
        try:
            simulation.close()
            simulation.close()
        except BaseException as error:
            failures.append(("Simulation cleanup", error))
        try:
            for resource in evidence["resources"]:
                stat = Path(f"/proc/{resource['pid']}/stat")
                assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[19] != resource["starttime"]
                assert not any(Path(path).exists() for path in resource["sockets"].values())
                assert not any(Path(path).exists() for path in resource["private_directories"])
            assert simulation.state == SimulationState.CLOSED
            assert hashlib.sha256(qemu.read_bytes()).hexdigest() == evidence["qemu"]["sha256"]
            assert hashlib.sha256(Path(build["elf"]).read_bytes()).hexdigest() == build["sha256"]
        except BaseException as error:
            failures.append(("cleanup or input identity verification", error))
        for label, failure in failures:
            if active_error is not None and failure is not active_error:
                active_error.add_note(f"{label}: {type(failure).__name__}: {failure}")
            if primary is not None and failure is not primary:
                primary.add_note(f"{label}: {type(failure).__name__}: {failure}")
            evidence["cleanup_errors"].append(f"{label}: {type(failure).__name__}: {failure}")
        evidence["cleanup_completed"] = not failures
        evidence["expected_native_failure_verified"] = verified
        if evidence["gate_status"] != "BLOCKED" or failures:
            evidence["gate_status"] = "PASS" if verified and active_error is None and not failures else "FAIL"
        if active_error is not None:
            evidence["gate_error"] = {"type": type(active_error).__name__, "error": str(active_error),
                "notes": list(getattr(active_error, "__notes__", ()))}
        if primary is not None:
            evidence["execution"]["notes"] = list(getattr(primary, "__notes__", ()))
        evidence["partial_observations"] = observations.report()
        report.write_text(json.dumps(evidence, indent=2) + "\n")
        if failures and (active_error is None or isinstance(active_error, pytest.skip.Exception)):
            raise failures[0][1]
