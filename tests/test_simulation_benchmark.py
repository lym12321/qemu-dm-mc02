"""Tool instrumentation contracts; fake acquisition is never native evidence."""

import io
import hashlib
import errno
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from sim.api import Event, StepContext, StepResult
from tools import benchmark_simulation as benchmark


# Literal QEMU log records follow system/trace-events and translate-all.c.
# They are parser fixtures, never evidence of a native firmware execution.
MMIO_READ = (
    "memory_region_ops_read cpu 0 mr 0x7f1000 addr 0x4001101c "
    "value 0xc0 size 4 name 'dm-mc02-uart'\n"
)
MMIO_WRITE = (
    "memory_region_ops_write cpu 0 mr 0x7f1000 addr 0x40011028 "
    "value 0x41 size 1 name 'dm-mc02-uart'\n"
)
MMIO_RECOMPILE = "cpu_io_recompile: rewound execution of TB to 000000000800012c\n"


def test_native_instrumentation_forwards_the_requested_elf(monkeypatch, tmp_path):
    firmware, qemu = tmp_path / "requested.elf", tmp_path / "qemu"
    calls = []

    def run(case, **kwargs):
        calls.append((case, kwargs))
        return {"snapshots": [{"mapped_time_ns": cut, "source": "main"} for cut in (0, 1)],
                "events": 2}

    monkeypatch.setattr(benchmark, "run_case", run)
    metrics = benchmark.native("minimal-board", qemu, firmware=firmware)
    assert calls == [("minimal-board", {"qemu": qemu, "firmware": firmware})]
    assert metrics["cuts"] == 1 and metrics["observations"]["events"] == 2


@pytest.fixture
def simple_native(monkeypatch, tmp_path):
    """Build/execute stand-ins verify tool ownership, never native behavior."""
    qemu = tmp_path / "qemu"
    qemu.write_bytes(b"qemu fixture")
    qemu.chmod(0o700)
    state = SimpleNamespace(qemu=qemu, builds=[], runs=[], failure=None, after_run=None)

    def build(case, *, build_dir):
        state.builds.append((case, build_dir))
        build_dir.mkdir(parents=True, exist_ok=False)
        elf = build_dir / "firmware.elf"
        elf.write_bytes(f"{case}:{len(state.builds)}".encode())
        return {"elf": str(elf), "sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
                "build_type": "Debug", "build": []}

    def native(case, path, *, firmware):
        state.runs.append((case, path, firmware))
        if state.failure is not None:
            raise state.failure
        if state.after_run is not None:
            state.after_run(case)
        return {"observations": {"fixture": True}}

    monkeypatch.setattr(benchmark.subprocess, "check_output", lambda *_, **__: "QEMU fixture")
    monkeypatch.setattr(benchmark, "build_example", build)
    monkeypatch.setattr(benchmark, "native", native)
    monkeypatch.setattr(benchmark, "host", lambda: {"fixture": True})
    return state


@pytest.mark.parametrize("name,case", [("single", "minimal-board"), ("dual", "dual-board")])
def test_native_benchmark_reports_own_distinct_builds_and_forward_exact_elf(
        simple_native, tmp_path, name, case):
    retained = []
    for stem in ("first", "second"):
        report = tmp_path / f"{stem}.json"
        assert benchmark.main(["--case", name, "--qemu", str(simple_native.qemu),
                               "--repeat", "2", "--report", str(report)]) == 0
        item = json.loads(report.read_text())["cases"][name]
        expected = tmp_path / f"{stem}-assets" / case / "Debug" / "firmware.elf"
        assert item["status"] == "PASS" and Path(item["firmware"]["elf"]) == expected
        assert simple_native.runs[-2:] == [(case, simple_native.qemu, expected)] * 2
        retained.append((expected, expected.read_bytes()))
    assert simple_native.builds == [(case, path.parent) for path, _ in retained]
    assert all(path.read_bytes() == content for path, content in retained)


@pytest.mark.parametrize("target,kind", [(target, kind) for target in ("report", "assets")
                                        for kind in ("file", "directory", "symlink")])
def test_existing_benchmark_outputs_are_rejected_before_acquisition(
        monkeypatch, tmp_path, target, kind):
    report, assets = tmp_path / "existing.json", tmp_path / "existing-assets"
    path = report if target == "report" else assets
    if kind == "file":
        path.write_bytes(b"existing evidence")
    elif kind == "directory":
        path.mkdir()
        (path / "keep").write_bytes(b"existing evidence")
    else:
        path.symlink_to(tmp_path / "missing-target")

    def acquired(*_, **__):
        pytest.fail("existing outputs must be rejected before acquisition")

    for name in ("host", "native", "build_example", "attribution"):
        monkeypatch.setattr(benchmark, name, acquired)
    monkeypatch.setattr(benchmark.subprocess, "check_output", acquired)
    with pytest.raises(FileExistsError):
        benchmark.main(["--case", "all", "--report", str(report)])
    if kind == "file":
        assert path.read_bytes() == b"existing evidence"
    elif kind == "directory":
        assert list(path.iterdir()) == [path / "keep"]
        assert (path / "keep").read_bytes() == b"existing evidence"
    else:
        assert path.is_symlink() and path.readlink() == tmp_path / "missing-target"
    if target == "assets":
        assert not report.exists()


def test_final_benchmark_hash_check_catches_changes_to_an_earlier_case(simple_native, tmp_path):
    def mutate(case):
        if case == "dual-board":
            first = simple_native.builds[0][1] / "firmware.elf"
            first.write_bytes(b"changed by later work")

    simple_native.after_run = mutate
    report = tmp_path / "all.json"
    assert benchmark.main(["--case", "all", "--qemu", str(simple_native.qemu),
                           "--repeat", "1", "--report", str(report)]) == 1
    payload = json.loads(report.read_text())
    assert payload["status"] == payload["cases"]["single"]["status"] == "FAIL"
    assert "firmware ELF changed after native execution" in payload["cases"]["single"]["error"]
    assert payload["cases"]["dual"]["status"] == "PASS"
    assert len(payload["cases"]["single"]["runs"]) == 1


@pytest.mark.parametrize("error_type,exit_code", [(RuntimeError, 1), (KeyboardInterrupt, 130)])
def test_native_benchmark_failure_retains_owned_firmware_and_raw_observations(
        simple_native, tmp_path, error_type, exit_code):
    error = error_type("firmware did not consume input")
    error.simulation_observations = {"uart": "PASS", "causal_reply": None}
    simple_native.failure = error
    report = tmp_path / "failed.json"
    assert benchmark.main(["--case", "single", "--qemu", str(simple_native.qemu),
                           "--report", str(report)]) == exit_code
    item = json.loads(report.read_text())["cases"]["single"]
    assert item["status"] == "FAIL" and item["partial_observations"] == error.simulation_observations
    assert Path(item["firmware"]["elf"]).is_file() and not item["runs"]


@pytest.mark.parametrize("mode", ("count", "json", "file"))
def test_output_modes_count_or_encode_once_and_measure_real_writes(monkeypatch, mode):
    from sim import cli

    clock, calls = [0.0], []
    monkeypatch.setattr(benchmark, "perf_counter", lambda: clock[0])
    dumps = json.dumps

    def convert(event):
        calls.append("convert")
        clock[0] += 2
        return {"kind": event.kind, "hex": event.payload.hex()}

    def serialize(value, **kwargs):
        calls.append("dumps")
        assert kwargs == {"ensure_ascii": True}
        clock[0] += 3
        return dumps(value, **kwargs)

    class Output(io.StringIO):
        def write(self, value):
            clock[0] += 5
            return super().write(value)

        def flush(self):
            clock[0] += 7
            return super().flush()

    monkeypatch.setattr(cli, "_json", convert)
    monkeypatch.setattr(benchmark.json, "dumps", serialize)
    row = dict(encoding_s=0, writing_s=0, output_callback_s=0, output_bytes=0,
               encoded_bytes=0, output_events=0)
    target = Output()
    observe = benchmark._attribution_output(mode, target if mode == "file" else None, row)
    event = Event(1, 0, "main", "uart1", "uart", b"hi\0\xff", 1, 1, 1, "exact")
    observe(event)
    observe(Event(1, 1, "main", "snapshot", "snapshot", {}, 1, 1, 1, "exact"))
    expected = '{"kind": "uart", "hex": "686900ff"}\n'
    assert row["output_events"] == 1
    assert calls == ([] if mode == "count" else ["convert", "dumps"])
    assert row["encoding_s"] == (0 if mode == "count" else 5)
    assert row["writing_s"] == (17 if mode == "file" else 0)
    assert target.getvalue() == (expected if mode == "file" else "")
    assert row["output_bytes"] == (len(expected) if mode == "file" else 0)
    assert row["encoded_bytes"] == (len(expected) if mode == "json" else 0)
    assert row["output_callback_s"] == {"count": 0, "json": 5, "file": 22}[mode]


@pytest.fixture
def fake_native(monkeypatch, tmp_path):
    """Only replace acquisition/execution; exercise the actual tool observers."""
    from sim import composition
    from sim.machines.qemu import machine as qemu

    clock = [0.0]
    state = SimpleNamespace(machines=[], sessions=[], alive=set(), close_calls=0,
                            run_error=None, run_error_step=3, close_error=None,
                            diagnostic_error=False, failed=False, commands=[],
                            native_origin_ns=71, trace_available=True, trace_payload=None,
                            command_error=None, receipt_error=False, limited_pids=[],
                            failed_qmp=[], native_status="paused", enabled_trace_state="enabled",
                            log_none_response="", mutate_log_on_close=False)

    def advance(seconds):
        clock[0] += seconds

    class Machine:
        def __init__(self):
            self.pid = 100 + len(state.machines)
            self.started, self._pending = False, None
            self.cut_ns, self.trace, self.log_exec, self.log_path = 0, {}, False, None
            state.machines.append(self)

        @property
        def connection_info(self):
            return {"pid": self.pid, "native_origin_ns": state.native_origin_ns,
                    "sockets": {"qmp": str(tmp_path / str(self.pid) / "qmp.sock")}}

        @property
        def pending(self):
            if state.failed and state.diagnostic_error:
                raise OSError("diagnostic handle unavailable")
            return self._pending

        def command(self, name, arguments=None):
            advance(.001)
            state.commands.append((self.pid, self.cut_ns, name, arguments))
            if state.failed:
                state.failed_qmp.append(name)
            if state.command_error is not None:
                error = state.command_error(name, arguments)
                if error is not None:
                    raise error
            return self.response(name, arguments)

        def response(self, name, arguments):
            if name == "query-commands":
                return [{"name": command} for command in (
                    "human-monitor-command", "trace-event-get-state", "trace-event-set-state",
                    "x-query-jit", "x-query-virtual-execution", "query-status")]
            if name == "x-query-jit":
                return {"human-readable-text": "Translation buffer state:\nTB count            19\n"
                        "TB flush count      0\nTB invalidate count 0\n"}
            if name == "trace-event-get-state":
                assert arguments == {"name": "memory_region_ops_*"}
                return [{"name": event, "state": state.enabled_trace_state if self.trace.get(event)
                         else "disabled", "vcpu": False}
                        for event in ("memory_region_ops_read", "memory_region_ops_write")
                        if state.trace_available]
            if name == "trace-event-set-state":
                self.trace[arguments["name"]] = arguments["enable"]
                return {}
            if name == "human-monitor-command":
                line = arguments["command-line"]
                if line.startswith("logfile "):
                    self.log_path = Path(line[len("logfile "):].strip('"'))
                    self.log_path.touch()
                elif line == "log exec":
                    self.log_exec = True
                elif line == "log none":
                    self.log_exec = False
                    return state.log_none_response
                else:
                    raise AssertionError(f"unexpected HMP command: {line}")
                return ""
            if name == "x-query-virtual-execution":
                native = state.native_origin_ns + self.cut_ns
                return {"state": "reached", "current-ns": native + int(state.receipt_error),
                        "target-ns": native}
            if name == "query-status":
                return {"running": False, "status": state.native_status}
            return {}

        def command_many(self, requests):
            advance(.002)
            if state.failed:
                state.failed_qmp.extend(name for name, _ in requests)
            return tuple(self.response(name, arguments) for name, arguments in requests)

        def to_native(self, cut):
            return state.native_origin_ns + cut

        def finish_step(self, context):
            self._pending = context
            self.command_many((("x-query-virtual-execution", None), ("query-status", None)))
            advance(.003)
            if state.run_error is not None and context.step_id == state.run_error_step:
                state.failed = True
                raise state.run_error
            if self.log_exec and any(self.trace.values()):
                assert self.log_path is not None
                payload = state.trace_payload
                if payload is None:
                    payload = b"" if self.workload == "wfi" else (
                        MMIO_READ + MMIO_READ + MMIO_WRITE + MMIO_RECOMPILE).encode()
                with self.log_path.open("ab") as stream:
                    stream.write(payload)
            self.cut_ns = context.t_sim_ns
            self._pending = None
            return StepResult("ok", context.t_sim_ns)

    class World:
        def step(self, context):
            advance(.004)
            return StepResult("ok", context.t_sim_ns)

    class Simulation:
        def __init__(self, topology, registry, **kwargs):
            assert kwargs["quantum_ns"] == 1_000_000
            assert kwargs["startup_timeout_s"] == 10 and kwargs["host_timeout_s"] == 30
            self.workload, self.topology = topology
            self.registry, self.observe = registry, kwargs["on_event"]
            self.state, self.t_sim_ns, self.step_id, self.sequence = "new", 0, 0, 0
            self.components = {}
            self.session = SimpleNamespace(session_id=len(state.sessions) + 1)
            state.sessions.append(self)

        def emit(self, kind, payload, cut):
            self.observe(Event(self.session.session_id, self.sequence, "main",
                "snapshot" if kind == "snapshot" else "uart1", kind, payload, cut, cut, cut, "exact"))
            self.sequence += 1

        def start(self):
            self.machine = self.registry["machine.qemu"].create()
            self.machine.workload = self.workload
            self.machine.started = True
            self.components["main"] = self.machine
            state.alive.add(self.machine.pid)
            self.machine.command("query-commands")
            if self.topology == "four-wheel":
                self.components["world"] = self.registry["physics.mujoco"].create()
            advance(.1)
            self.state = "paused"
            self.emit("snapshot", {}, 0)

        def run_until(self, target):
            assert target >= self.t_sim_ns
            ready = b"SIM_READY minimal-board\n" if self.workload == "wfi" else b"LAB_READY\n"
            while self.t_sim_ns < target:
                cut = min(self.t_sim_ns + 1_000_000, target)
                index = self.step_id + 1
                context = StepContext(self.session.session_id, index, cut, cut - self.t_sim_ns)
                try:
                    self.machine.finish_step(context)
                except BaseException:
                    self.state = "failed"
                    raise
                if "world" in self.components:
                    self.components["world"].step(context)
                self.t_sim_ns, self.step_id = cut, index
                if index in (1, 2):
                    self.emit("uart", ready[:5] if index == 1 else ready[5:], cut)
                self.emit("snapshot", {}, cut)

        def close(self):
            state.close_calls += 1
            advance(.02)
            self.state = "closed"
            self.components.clear()
            if hasattr(self, "machine"):
                state.alive.discard(self.machine.pid)
                if state.mutate_log_on_close and self.machine.log_path is not None:
                    with self.machine.log_path.open("ab") as stream:
                        stream.write(MMIO_READ.encode())
            if state.close_error is not None:
                raise state.close_error

    def process(pid):
        return {"starttime": str(pid), "cpu_s": .25, "rss_bytes": 4096} if pid in state.alive else None

    def registry(**_):
        return {"machine.qemu": SimpleNamespace(create=Machine),
                "physics.mujoco": SimpleNamespace(create=World)}

    monkeypatch.setattr(qemu, "QemuMachine", Machine)
    monkeypatch.setattr(composition, "builtin_registry", registry)
    monkeypatch.setattr(benchmark, "Simulation", Simulation)
    monkeypatch.setattr(benchmark, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(benchmark, "_process_sample", process)
    monkeypatch.setattr(benchmark, "_limit_capture_file_size", state.limited_pids.append,
                        raising=False)
    monkeypatch.setattr(benchmark, "usage", lambda: {"host_cpu_s": clock[0], "children_cpu_s": clock[0] / 2,
        "host_peak_rss_bytes": 8192, "host_current_rss_bytes": 4096})
    monkeypatch.setattr(benchmark, "_attribution_scene", lambda workload, topology, *_:
                        SimpleNamespace(topology=(workload, topology)))
    inputs = []
    for name in ("qemu", "compiler", "model", "source", "wfi", "busy-mmio"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        inputs.append(benchmark._identity(path))
    assets = dict(zip(("qemu", "compiler", "model"), inputs[:3]))
    assets.update(inputs=[inputs[3]], firmware={name: {"elf": item["path"], "sha256": item["sha256"]}
                  for name, item in zip(("wfi", "busy-mmio"), inputs[4:])})

    def built(_qemu, directory, result):
        directory.mkdir()
        result.update(assets)

    monkeypatch.setattr(benchmark, "attribution_assets", built)
    state.assets = assets
    return state


def test_attribution_matrix_uses_36_independent_sessions_and_inclusive_timers(fake_native, tmp_path):
    report = tmp_path / "matrix.json"
    assert benchmark.main(["--case", "attribution", "--report", str(report)]) == 0
    item = json.loads(report.read_text())["cases"]["attribution"]
    assert item["status"] == "PASS" and len(item["cells"]) == 12
    assert len(fake_native.sessions) == fake_native.close_calls == 36
    for cell in item["cells"]:
        assert cell["status"] == "PASS" and cell["summary_valid"] and cell["passed_runs"] == 3
        expected_active = 1.8 if cell["topology"] == "four-wheel" else 1.0
        assert cell["median"]["active_s"] == pytest.approx(expected_active)
        for row in cell["runs"]:
            assert row["status"] == "PASS" and row["ready"]
            assert row["startup_s"] == pytest.approx(.101)
            assert row["cleanup_s"] == pytest.approx(.02)
            assert row["finish_calls"] == row["cuts"] == 200
            assert row["finish_step_s"] == pytest.approx(1)
            assert row["qmp_calls"] == {"command": 1, "command_many": 200}
            assert row["qmp_commands"] == {"query-commands": 1,
                "x-query-virtual-execution": 200, "query-status": 200}
            assert row["qmp_total_s"] == pytest.approx(.401)
            assert row["world_step_calls"] == (200 if cell["topology"] == "four-wheel" else 0)
            assert row["world_step_s"] == pytest.approx(.8 if cell["topology"] == "four-wheel" else 0)
            assert row["events"] == 203 and row["snapshots"] == 201 and row["output_events"] == 2
            assert row["last_committed_cut_ns"] == 200_000_000 and not row["pending"]
            assert row["input_hashes_verified"] and all(row["resource_cleanup"].values())
            assert (row["output_bytes"] > 0) == (cell["output"] == "file")
            assert (row["encoded_bytes"] > 0) == (cell["output"] == "json")
            if cell["output"] == "file":
                lines = Path(row["output"]["path"]).read_text().splitlines()
                assert [json.loads(line)["kind"] for line in lines] == ["uart", "uart"]
    assert len({row["session_id"] for cell in item["cells"] for row in cell["runs"]}) == 36
    for workload in ("wfi", "busy-mmio"):
        streams = {row["uart_hex"] for cell in item["cells"] if cell["workload"] == workload
                   for row in cell["runs"]}
        assert streams == {(b"SIM_READY minimal-board\n" if workload == "wfi" else b"LAB_READY\n").hex()}


@pytest.mark.parametrize("diagnostic_error", (False, True))
def test_partial_failure_keeps_primary_pending_and_closes_file(fake_native, tmp_path, diagnostic_error):
    fake_native.run_error = RuntimeError("native interval failed")
    fake_native.close_error = OSError("cleanup failed")
    fake_native.diagnostic_error = diagnostic_error
    row = {}
    benchmark.attribution_run("wfi", "four-wheel", "file", fake_native.assets, tmp_path / "failed", row)
    assert row["status"] == "FAIL" and row["error"] == "RuntimeError: native interval failed"
    assert any("cleanup failed" in note for note in row["notes"])
    if diagnostic_error:
        assert any("diagnostic handle unavailable" in note for note in row["notes"])
    else:
        assert next(iter(row["pending"].values()))["t_sim_ns"] == 3_000_000
    assert row["last_committed_cut_ns"] == 2_000_000
    assert row["world_step_calls"] == 2 and row["finish_calls"] == 3
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())
    assert row["input_hashes_verified"] and row["output_bytes"] > 0


def test_failed_repetitions_keep_raw_runs_and_have_no_success_only_median(fake_native, tmp_path):
    fake_native.run_error = RuntimeError("native interval failed")
    item = {}
    benchmark.attribution(item, tmp_path / "qemu", tmp_path / "assets", 3)
    assert item["status"] == "FAIL" and len(fake_native.sessions) == 36
    assert all(len(cell["runs"]) == 3 and not cell["summary_valid"] and cell["median"] == {}
               for cell in item["cells"])


def test_missing_qemu_is_blocked78_without_building(tmp_path):
    report = tmp_path / "blocked.json"
    assert benchmark.main(["--case", "attribution", "--qemu", str(tmp_path / "missing"),
                           "--report", str(report)]) == 78
    payload = json.loads(report.read_text())
    assert payload["status"] == payload["cases"]["attribution"]["status"] == "BLOCKED"
    assert not (tmp_path / "blocked-assets").exists()


def test_interrupt_preserves_partial_report_and_returns130(fake_native, tmp_path):
    fake_native.run_error = KeyboardInterrupt()
    report = tmp_path / "interrupt.json"
    assert benchmark.main(["--case", "attribution", "--report", str(report)]) == 130
    payload = json.loads(report.read_text())
    row = payload["cases"]["attribution"]["cells"][0]["runs"][0]
    assert payload["interrupted"] and row["error"] == "KeyboardInterrupt: "
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())


def test_native_cost_defaults_to_two_guests_without_world_or_trace(fake_native, tmp_path):
    report = tmp_path / "native-cost.json"
    assert benchmark.main(["--case", "native-cost", "--report", str(report)]) == 0
    item = json.loads(report.read_text())["cases"]["native-cost"]
    assert item["status"] == "PASS"
    assert [(cell["workload"], cell["topology"], cell["output"]) for cell in item["cells"]] == [
        ("wfi", "machine", "count"), ("busy-mmio", "machine", "count")]
    assert len(fake_native.sessions) == fake_native.close_calls == 6
    assert not fake_native.limited_pids
    assert all(name == "query-commands" for _, _, name, _ in fake_native.commands)
    for cell in item["cells"]:
        assert cell["passed_runs"] == 3 and cell["summary_valid"]
        for row in cell["runs"]:
            assert row["capture"] == {"mode": "none"}
            assert row["ready"] and row["finish_calls"] == row["cuts"] == 200
            assert row["world_step_calls"] == row["output_bytes"] == row["encoded_bytes"] == 0
            assert row["last_committed_cut_ns"] == 200_000_000
            assert all(row["resource_cleanup"].values())


@pytest.mark.parametrize("case,capture", [("host", "none"), ("attribution", "mmio")])
def test_capture_option_is_rejected_outside_native_cost_before_acquisition(
        fake_native, tmp_path, case, capture):
    report = tmp_path / "invalid.json"
    with pytest.raises(SystemExit) as raised:
        benchmark.main(["--case", case, "--capture", capture, "--report", str(report)])
    assert raised.value.code == 2
    assert not report.exists() and not fake_native.sessions


def test_mmio_windows_bound_execution_restore_grid_and_use_native_receipts(fake_native, tmp_path):
    report = tmp_path / "capture.json"
    assert benchmark.main(["--case", "native-cost", "--capture", "mmio", "--repeat", "1",
                           "--report", str(report)]) == 0
    item = json.loads(report.read_text())["cases"]["native-cost"]
    assert item["status"] == "PASS"
    assert fake_native.limited_pids == [machine.pid for machine in fake_native.machines]
    starts = [1_000_000, 10_000_000, 100_000_000, 199_000_000]
    for cell, machine in zip(item["cells"], fake_native.machines):
        row = cell["runs"][0]
        assert row["cuts"] == row["finish_calls"] == 204
        assert row["last_committed_cut_ns"] == 200_000_000
        assert row["world_step_calls"] == 0 and row["ready"]
        assert row["capture"]["mode"] == "mmio"
        windows = row["capture"]["windows"]
        assert [window["start_ns"] for window in windows] == starts
        for window in windows:
            assert window["status"] == "PASS"
            assert window["end_ns"] == window["start_ns"] + 10_000
            for name, cut in (("before", window["start_ns"]), ("after", window["end_ns"])):
                receipt = window[name]
                assert receipt["mapped_time_ns"] == cut
                assert receipt["native_time_ns"] == cut + 71
                assert receipt["execution"] == {
                    "state": "reached", "current-ns": cut + 71, "target-ns": cut + 71}
                assert receipt["status"] == {"running": False, "status": "paused"}
            assert window["jit_before"]["tb_count"] == window["jit_after"]["tb_count"] == 19
            if cell["workload"] == "wfi":
                assert window["log"]["bytes"] == window["log"]["lines"] == 0
                assert window["log"]["accesses"] == window["log"]["recompiles"] == []
            else:
                assert window["log"]["lines"] == 4
                assert sum(access["count"] for access in window["log"]["accesses"]) == 3
                assert window["log"]["recompiles"] == [{"pc": "0x800012c", "count": 1}]
        commands = [(cut, name, args) for pid, cut, name, args in fake_native.commands
                    if pid == machine.pid]
        enabled = [(cut, args["name"]) for cut, name, args in commands
                   if name == "trace-event-set-state" and args["enable"]]
        assert enabled == [(cut, name) for cut in starts
                           for name in ("memory_region_ops_read", "memory_region_ops_write")]
        disabled = [(cut, args["name"]) for cut, name, args in commands
                    if name == "trace-event-set-state" and not args["enable"]]
        assert disabled == [(cut + 10_000, name) for cut in starts
                            for name in ("memory_region_ops_read", "memory_region_ops_write")]
        stopped = [cut for cut, name, args in commands
                   if name == "human-monitor-command" and args["command-line"] == "log none"]
        assert stopped == [cut + 10_000 for cut in starts]
        assert not any(machine.trace.values()) and not machine.log_exec
        assert all(row["resource_cleanup"].values())


def test_missing_mmio_trace_capability_is_blocked_and_closes_session(fake_native, tmp_path):
    fake_native.trace_available = False
    row = {}
    benchmark.attribution_run("wfi", "machine", "count", fake_native.assets,
                              tmp_path / "missing-trace", row, capture="mmio")
    assert row["status"] == "BLOCKED"
    assert row["cuts"] == 0 and not row["capture"]["windows"]
    assert not any(name == "trace-event-set-state" for _, _, name, _ in fake_native.commands)
    assert [arguments for _, _, name, arguments in fake_native.commands
            if name == "trace-event-get-state"] == [{"name": "memory_region_ops_*"}]
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())


@pytest.mark.parametrize("mismatch", ["time", "status"])
def test_mmio_native_receipt_mismatch_fails_before_logging(fake_native, tmp_path, mismatch):
    if mismatch == "time":
        fake_native.receipt_error = True
    else:
        fake_native.native_status = "shutdown"
    row = {}
    benchmark.attribution_run("busy-mmio", "machine", "count", fake_native.assets,
                              tmp_path / "bad-receipt", row, capture="mmio")
    assert row["status"] == "FAIL" and row["last_committed_cut_ns"] == 1_000_000
    assert not any(name == "trace-event-set-state" and args["enable"]
                   for _, _, name, args in fake_native.commands)
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())


def test_native_capture_failure_stops_qmp_and_preserves_close_error(fake_native, tmp_path):
    primary = fake_native.run_error = RuntimeError("native interval failed")
    fake_native.run_error_step = 2
    fake_native.close_error = OSError("simulation cleanup failed")

    row = {}
    benchmark.attribution_run("busy-mmio", "machine", "count", fake_native.assets,
                              tmp_path / "capture-failed", row, capture="mmio")
    assert row["status"] == "FAIL" and row["error"] == "RuntimeError: native interval failed"
    assert row["last_committed_cut_ns"] == 1_000_000 and row["finish_calls"] == 2
    assert len(row["capture"]["windows"]) == 1
    assert row["capture"]["windows"][0]["status"] == "FAIL"
    assert any("simulation cleanup failed" in note for note in row["notes"])
    assert list(primary.__notes__) == row["notes"]
    disable_attempts = [arguments["name"] for _, _, name, arguments in fake_native.commands
                        if name == "trace-event-set-state" and not arguments["enable"]]
    assert disable_attempts == []
    assert not fake_native.failed_qmp
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())


def test_paused_diagnostic_failure_keeps_primary_and_attempts_each_capture_cleanup(fake_native, tmp_path):
    fake_native.enabled_trace_state = "disabled"

    def command_error(name, arguments):
        if name == "trace-event-set-state" and not arguments["enable"]:
            return OSError("trace disable failed")

    fake_native.command_error = command_error
    row = {}
    benchmark.attribution_run("busy-mmio", "machine", "count", fake_native.assets,
                              tmp_path / "diagnostic-failed", row, capture="mmio")
    assert row["status"] == "FAIL"
    assert row["error"].startswith("RuntimeError: unexpected QEMU trace state:")
    assert len(row["notes"]) == 2
    assert all("trace disable failed" in note for note in row["notes"])
    assert row["last_committed_cut_ns"] == 1_000_000 and row["finish_calls"] == 1
    assert len(row["capture"]["windows"]) == 1
    disabled = [arguments["name"] for _, _, name, arguments in fake_native.commands
                if name == "trace-event-set-state" and not arguments["enable"]]
    assert disabled == ["memory_region_ops_read", "memory_region_ops_write"]
    assert not fake_native.machines[0].log_exec
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())


def test_hmp_log_disable_error_is_not_reported_as_success(fake_native, tmp_path):
    fake_native.log_none_response = "Error: logging unavailable\r\n"
    row = {}
    benchmark.attribution_run("busy-mmio", "machine", "count", fake_native.assets,
                              tmp_path / "log-disable-failed", row, capture="mmio")
    assert row["status"] == "FAIL" and "log none" in row["error"]
    assert row["last_committed_cut_ns"] == 1_010_000 and row["finish_calls"] == 2
    assert len(row["capture"]["windows"]) == 1
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())


def test_capture_log_changed_during_cleanup_invalidates_completed_windows(fake_native, tmp_path):
    fake_native.mutate_log_on_close = True
    row = {}
    benchmark.attribution_run("busy-mmio", "machine", "count", fake_native.assets,
                              tmp_path / "changed-log", row, capture="mmio")
    assert row["status"] == "FAIL" and "MMIO log changed" in row["error"]
    assert row["last_committed_cut_ns"] == 200_000_000 and row["cuts"] == 204
    assert all(window["status"] == "PASS" for window in row["capture"]["windows"])
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())


def test_mmio_parser_keeps_addresses_sizes_and_pc_counts_with_wall_prefix(tmp_path):
    path = tmp_path / "window.log"
    data = (MMIO_READ + "123@1780000000.123456:" + MMIO_READ + MMIO_WRITE +
            MMIO_RECOMPILE + MMIO_RECOMPILE +
            "Trace 0: 0x7f1200 [00000000/0800012c/01000020/ff000200]\n"
            "Linking TBs 0x7f1200 index 0 -> 0x7f1400\n"
            "Stopped execution of TB chain before 0x7f1400 [08000130]\n").encode()
    path.write_bytes(data)
    result = benchmark._parse_mmio_log(path)
    assert result["bytes"] == len(data) and result["lines"] == 8
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["accesses"] == [
        {"operation": "read", "address": "0x4001101c", "size": 4,
         "region": "dm-mc02-uart", "count": 2},
        {"operation": "write", "address": "0x40011028", "size": 1,
         "region": "dm-mc02-uart", "count": 1}]
    assert result["recompiles"] == [{"pc": "0x800012c", "count": 2}]
    assert result["exec_lines"] == {"trace": 1, "link": 1, "stop": 1}


@pytest.mark.parametrize("data", [MMIO_READ.rstrip("\n").encode(), b"unrecognized diagnostic\n",
                                  b"memory_region_ops_read cpu 0 broken\n", b"\xff\n",
                                  b"Trace 0: 0x7f1200 [////]\n",
                                  b"Linking TBs 0x7f1200 index 2 -> 0x7f1400\n"])
def test_mmio_parser_rejects_truncated_malformed_and_unknown_records(tmp_path, data):
    path = tmp_path / "bad.log"
    path.write_bytes(data)
    with pytest.raises((ValueError, RuntimeError, UnicodeError)):
        benchmark._parse_mmio_log(path)


@pytest.mark.parametrize("limit", ["bytes", "lines", "line-bytes"])
def test_mmio_capture_reports_log_capacity_failure_without_continuing(
        fake_native, tmp_path, monkeypatch, limit):
    if limit == "bytes":
        monkeypatch.setattr(benchmark, "MMIO_MAX_BYTES", len(MMIO_READ.encode()) - 1)
        fake_native.trace_payload = MMIO_READ.encode()
    elif limit == "lines":
        monkeypatch.setattr(benchmark, "MMIO_MAX_LINES", 1)
        fake_native.trace_payload = (MMIO_READ * 2).encode()
    else:
        fake_native.trace_payload = b"x" * 4096 + b"\n"
    row = {}
    benchmark.attribution_run("busy-mmio", "machine", "count", fake_native.assets,
                              tmp_path / "capacity", row, capture="mmio")
    assert row["status"] == "FAIL"
    assert row["last_committed_cut_ns"] == 1_010_000 and row["finish_calls"] == 2
    assert len(row["capture"]["windows"]) == 1
    assert not any(fake_native.machines[0].trace.values())
    assert not fake_native.machines[0].log_exec
    assert fake_native.close_calls == 1 and all(row["resource_cleanup"].values())


@pytest.mark.parametrize("soft", [-1, 16 * 1024 * 1024])
def test_capture_file_limit_caps_owned_child_without_changing_hard_limit(monkeypatch, soft):
    current, calls = [soft, 32 * 1024 * 1024], []

    def prlimit(pid, resource, limits=None):
        calls.append((pid, resource, limits))
        previous = tuple(current)
        if limits is not None:
            current[:] = limits
        return previous

    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        prlimit=prlimit, RLIMIT_FSIZE=1, RLIM_INFINITY=-1))
    benchmark._limit_capture_file_size(123)
    assert current == [8 * 1024 * 1024, 32 * 1024 * 1024]
    assert calls == [(123, 1, None), (123, 1, (8 * 1024 * 1024, 32 * 1024 * 1024)),
                     (123, 1, None)]


def test_capture_file_limit_does_not_relax_lower_inherited_bound(monkeypatch):
    calls = []

    def prlimit(*args):
        calls.append(args)
        return 4096, 32 * 1024 * 1024

    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        prlimit=prlimit, RLIMIT_FSIZE=1, RLIM_INFINITY=-1))
    with pytest.raises(benchmark.Blocked):
        benchmark._limit_capture_file_size(123)
    assert calls == [(123, 1)]


@pytest.mark.parametrize("code,fail_call,expected", [
    (errno.EPERM, 1, benchmark.Blocked), (errno.ENOSYS, 2, benchmark.Blocked),
    (errno.ESRCH, 1, ProcessLookupError)])
def test_capture_file_limit_distinguishes_unavailable_facility_from_dead_process(
        monkeypatch, code, fail_call, expected):
    calls = []

    def prlimit(*args):
        calls.append(args)
        if len(calls) == fail_call:
            raise OSError(code, "injected prlimit error")
        return -1, -1

    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        prlimit=prlimit, RLIMIT_FSIZE=1, RLIM_INFINITY=-1))
    with pytest.raises(expected):
        benchmark._limit_capture_file_size(123)
    assert len(calls) == fail_call


def test_capture_file_limit_verifies_limit_after_setting(monkeypatch):
    calls = []

    def prlimit(*args):
        calls.append(args)
        return -1, -1

    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        prlimit=prlimit, RLIMIT_FSIZE=1, RLIM_INFINITY=-1))
    with pytest.raises(RuntimeError, match="not applied"):
        benchmark._limit_capture_file_size(123)
    assert calls == [(123, 1), (123, 1, (8 * 1024 * 1024, -1)), (123, 1)]
