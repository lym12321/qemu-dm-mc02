"""Tool instrumentation contracts; fake acquisition is never native evidence."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sim.api import Event, StepContext, StepResult
from tools import benchmark_simulation as benchmark


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
                            run_error=None, close_error=None, diagnostic_error=False, failed=False)

    def advance(seconds):
        clock[0] += seconds

    class Machine:
        def __init__(self):
            self.pid = 100 + len(state.machines)
            self.started, self._pending = False, None
            state.machines.append(self)

        @property
        def connection_info(self):
            return {"pid": self.pid, "native_origin_ns": 0,
                    "sockets": {"qmp": str(tmp_path / str(self.pid) / "qmp.sock")}}

        @property
        def pending(self):
            if state.failed and state.diagnostic_error:
                raise OSError("diagnostic handle unavailable")
            return self._pending

        def command(self, name, arguments=None):
            advance(.001)
            return {}

        def command_many(self, requests):
            advance(.002)
            return tuple({} for _ in requests)

        def finish_step(self, context):
            self._pending = context
            self.command_many((("x-query-virtual-execution", None), ("query-status", None)))
            advance(.003)
            if state.run_error is not None and context.step_id == 3:
                state.failed = True
                raise state.run_error
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
            assert target == 200_000_000
            ready = b"SIM_READY minimal-board\n" if self.workload == "wfi" else b"LAB_READY\n"
            for index in range(1, 201):
                cut = index * 1_000_000
                context = StepContext(self.session.session_id, index, cut, 1_000_000)
                self.machine.finish_step(context)
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
