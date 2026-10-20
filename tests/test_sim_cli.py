"""CLI admission and formatting; the runtime has its own native consumer gate."""

import argparse
import builtins
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.apps import unmigrated_cli as cli
from sim.arena import manifest as arena_manifest_module
from sim.arena import runtime as arena_runtime
from sim.boards.channels import ChannelEvent
from sim.fabric import CanFrame
from sim.lab import manifest as manifest_module
from sim.lab import runtime
from sim.robots import manifest as robot_manifest_module
from sim.robots import runtime as robot_runtime
from sim.scenario import manifest as scenario_manifest_module
from sim.scenario import runtime as scenario_runtime


def manifest_runtime(group):
    return {"lab": (manifest_module, runtime),
            "robot": (robot_manifest_module, robot_runtime),
            "arena": (arena_manifest_module, arena_runtime),
            "scenario": (scenario_manifest_module, scenario_runtime)}[group]


def isolated(tmp_path, *arguments):
    return subprocess.run([sys.executable, "-I", str(ROOT / "tools/sim-unmigrated"), *arguments],
                          cwd=tmp_path, capture_output=True, text=True, timeout=5)


def test_isolated_entry_help_and_module_help(tmp_path):
    result = isolated(tmp_path, "lab", "run", "--help")
    assert result.returncode == 0, result.stderr
    assert "--startup-timeout" in result.stdout
    assert "--gdb-port" in result.stdout and "--report" in result.stdout
    result = isolated(tmp_path, "board", "run", "--help")
    assert result.returncode == 0, result.stderr
    assert "Ctrl-a c" in result.stdout and "--elf" in result.stdout
    result = subprocess.run([sys.executable, "-m", "sim.apps.unmigrated_cli", "--help"], cwd=ROOT,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert all(group in result.stdout for group in ("board", "lab", "robot", "arena", "scenario"))


@pytest.mark.parametrize("group", ["lab", "robot", "arena", "scenario"])
def test_invalid_manifest_rejected_before_qemu_effect(tmp_path, group):
    manifest = tmp_path / "invalid.yaml"
    manifest.write_text("schema: 99\n", encoding="utf-8")
    qemu = tmp_path / "unexpected-qemu"
    marker = tmp_path / "started"
    qemu.write_text("#!/bin/sh\n: > '" + str(marker) + "'\n", encoding="utf-8")
    qemu.chmod(0o755)
    report = tmp_path / "report.json"
    result = isolated(tmp_path, group, "run", str(manifest), "--qemu", str(qemu),
                      "--report", str(report))
    assert result.returncode != 0
    assert "error:" in result.stderr
    assert not marker.exists()
    assert json.loads(report.read_text())["status"] == "failed"


@pytest.mark.parametrize("options", [
    ["--unknown"], ["--timeout", "nan"], ["--timeout", "inf"],
    ["--startup-timeout", "0"], ["--gdb-port", "0"],
    ["--gdb-port", "65536"], ["--paused", "false"],
])
def test_cli_option_admission_precedes_loading(monkeypatch, options):
    def forbidden(*_args, **_kwargs):
        pytest.fail("manifest was loaded before CLI option admission")

    monkeypatch.setattr(manifest_module, "load_manifest", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main(["lab", "run", "unused.yaml", *options])
    assert caught.value.code == 2


def test_numeric_option_helpers_reject_boolean_values():
    with pytest.raises(argparse.ArgumentTypeError):
        cli._seconds(True)
    with pytest.raises(argparse.ArgumentTypeError):
        cli._port(True)


def test_board_run_forwards_native_options_without_reinterpreting(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: calls.append((path, argv)))
    options = ["--elf", "firmware path.elf", "--debug", "--gdb-port", "1235",
               "--qmp", "native socket", "--uart", "USART3"]
    assert cli.main(["board", "run", *options]) == 0
    launcher = str(ROOT / "tools/run-qemu.sh")
    assert calls == [(launcher, [launcher, *options])]


def test_lab_runtime_options_safe_stream_output_and_success_report(monkeypatch, tmp_path, capsys):
    admitted = object()
    monkeypatch.setattr(manifest_module, "load_manifest", lambda path: admitted)
    calls = []

    def run(manifest, **options):
        calls.append((manifest, options))
        options["emit"](ChannelEvent("uart1", 8, b"hello\n\x1b[2J\xff"))
        options["emit"](ChannelEvent("can1", 9, CanFrame(0x321, data=b"A", timestamp_ns=9)))
        return runtime.LabResult("fixture", 1, 10, ("pass",), (), ((2, 3, "key"),), 4)

    monkeypatch.setattr(runtime, "run_manifest", run)
    report = tmp_path / "result.json"
    assert cli.main(["lab", "run", "fixture.yaml", "--qemu", "native-qemu",
                     "--timeout", "20", "--startup-timeout", "2", "--qmp", "native.sock",
                     "--gdb-port", "1235", "--paused", "--report", str(report)]) == 0
    manifest, options = calls[0]
    assert manifest is admitted
    assert {name: value for name, value in options.items() if name != "emit"} == {
        "paused": True, "qemu": "native-qemu", "startup_timeout": 2.0,
        "timeout": 20.0, "qmp_path": "native.sock", "gdb_port": 1235,
    }
    output = capsys.readouterr().out
    assert "poll_sample_ns=8" in output and "\\x1b[2J" in output
    assert "\x1b" not in output and "native_timestamp_ns=9" in output and "data=41" in output
    result = json.loads(report.read_text())
    assert result["status"] == "passed" and result["result"]["status"] == ["pass"]


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("group", ["lab", "robot", "arena", "scenario"])
def test_runtime_failure_and_cleanup_notes_remain_failed(monkeypatch, tmp_path, capsys, interrupt, group):
    loader, runner = manifest_runtime(group)
    monkeypatch.setattr(loader, "load_manifest", lambda path: object())
    error = KeyboardInterrupt("interrupted") if interrupt else RuntimeError("producer failed")
    error.add_note("cleanup CAN failed: exact diagnostic")
    error.add_note("QEMU stderr tail:\nfirst failure\nsecond failure")

    def run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(runner, "run_manifest", run)
    report = tmp_path / "result.json"
    assert cli.main([group, "run", "fixture.yaml", "--report", str(report)]) == (130 if interrupt else 1)
    result = json.loads(report.read_text())
    assert result["status"] == "failed" and result["notes"] == error.__notes__
    assert result["error"] == str(error) and result["error_type"] == type(error).__name__
    output = capsys.readouterr()
    assert str(error) in output.err and all(note in output.err for note in error.__notes__)
    assert "passed" not in output.out


def test_assertion_failure_is_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(manifest_module, "load_manifest", lambda path: object())
    monkeypatch.setattr(runtime, "run_manifest", lambda *_args, **_kwargs:
                        runtime.LabResult("fixture", 1, 10, ("fail",), ("KEY missing",), (), 1))
    report = tmp_path / "failed.json"
    assert cli.main(["lab", "run", "fixture.yaml", "--report", str(report)]) == 1
    assert json.loads(report.read_text())["status"] == "failed"
    assert "KEY missing" in capsys.readouterr().err


@pytest.mark.parametrize("group", ["lab", "robot", "arena", "scenario"])
def test_sigterm_unwinds_runtime_and_restores_callers_handler(monkeypatch, tmp_path, group):
    loader, runner = manifest_runtime(group)
    monkeypatch.setattr(loader, "load_manifest", lambda path: object())
    cleaned = []

    def run(*_args, **_kwargs):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        finally:
            cleaned.append(True)

    monkeypatch.setattr(runner, "run_manifest", run)
    previous = signal.getsignal(signal.SIGTERM)
    report = tmp_path / "interrupted.json"
    assert cli.main([group, "run", "fixture.yaml", "--report", str(report)]) == 130
    assert cleaned == [True]
    assert signal.getsignal(signal.SIGTERM) is previous
    result = json.loads(report.read_text())
    assert result["status"] == "failed" and result["error_type"] == "KeyboardInterrupt"


def test_interactive_cli_forwards_configured_channels_and_deadlines(monkeypatch, tmp_path, capsys):
    from sim.lab import interactive
    calls = []
    monkeypatch.setattr(interactive, "run_interactive", lambda firmware, **options:
                        calls.append((firmware, options)) or
                        {"commands": 3, "observation_batches": 9, "end_ns": 10})
    assert cli.main(["lab", "interactive", "--elf", "fixture.elf", "--uart", "uart2",
                     "--uart", "uart10", "--can", "can3", "--adc", "--timeout", "20",
                     "--startup-timeout", "2", "--gdb-port", "1235", "--paused"]) == 0
    firmware, options = calls[0]
    assert firmware == Path("fixture.elf")
    assert {name: value for name, value in options.items() if name != "emit"} == {
        "uarts": ("uart2", "uart10"), "cans": ("can3",), "adc": True, "paused": True,
        "qemu": None, "startup_timeout": 2., "timeout": 20., "qmp_path": None, "gdb_port": 1235,
    }
    assert "closed: 3 commands, 9 observations" in capsys.readouterr().out
    result = isolated(tmp_path, "lab", "interactive", "--help")
    assert result.returncode == 0 and "--can" in result.stdout and "--adc" in result.stdout


def test_interactive_cli_preserves_cleanup_failure(monkeypatch, capsys):
    from sim.lab import interactive
    def failed(*args, **kwargs):
        error = RuntimeError("interactive consumer failed")
        error.add_note("Lab cleanup QMP failed: connection release")
        raise error
    monkeypatch.setattr(interactive, "run_interactive", failed)
    assert cli.main(["lab", "interactive", "--elf", "fixture.elf"]) == 1
    output = capsys.readouterr()
    assert "closed" not in output.out
    assert "Lab cleanup QMP failed" in output.err


@pytest.mark.parametrize("group,duration_help", [
    ("robot", "QEMU virtual duration"), ("arena", "shared logical world duration"),
    ("scenario", None),
])
def test_manifest_run_help_isolated_cwd_and_lazy_imports(tmp_path, monkeypatch, group, duration_help):
    result = isolated(tmp_path, group, "run", "--help")
    assert result.returncode == 0, result.stderr
    assert ("--duration" in result.stdout) == (group != "scenario")
    if duration_help is not None:
        assert duration_help in result.stdout
    else:
        assert "YAML Scenario manifest" in result.stdout
    assert "--startup-timeout" in result.stdout and "--report" in result.stdout
    assert ("--gdb-port" in result.stdout) == (group == "robot")
    assert ("--qmp" in result.stdout) == (group == "robot")
    assert "--paused" not in result.stdout
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "yaml" or name == "mujoco" or name.startswith("sim."):
            pytest.fail(f"help loaded optional manifest/runtime dependency: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as caught:
        cli.main([group, "run", "--help"])
    assert caught.value.code == 0


@pytest.mark.parametrize("arguments,expected", [
    ([], {"duration": 5.0, "qemu": None, "startup_timeout": 10.0,
          "timeout": 30.0, "qmp_path": None, "gdb_port": None}),
    (["--duration", "0.25", "--qemu", "native-qemu", "--startup-timeout", "2",
      "--timeout", "20", "--qmp", "native.sock", "--gdb-port", "1235"],
     {"duration": 0.25, "qemu": "native-qemu", "startup_timeout": 2.0,
      "timeout": 20.0, "qmp_path": "native.sock", "gdb_port": 1235}),
])
def test_robot_runtime_options_and_dataclass_report(monkeypatch, tmp_path, capsys, arguments, expected):
    admitted = object()
    loaded = []
    calls = []

    def load(path):
        loaded.append(path)
        return admitted

    def run(manifest, **options):
        calls.append((manifest, options))
        options["emit"](ChannelEvent("uart1", 8, b"hello\n\x1b[2J"))
        return robot_runtime.RobotResult("four-wheel", "not requested", 10, 250_000_010, 3,
                                         ({"name": "chassis worker", "returncode": 0},))

    monkeypatch.setattr(robot_manifest_module, "load_manifest", load)
    monkeypatch.setattr(robot_runtime, "run_manifest", run)
    report = tmp_path / "robot.json"
    assert cli.main(["robot", "run", "robot.yaml", *arguments, "--report", str(report)]) == 0
    assert loaded == [Path("robot.yaml")]
    manifest, options = calls[0]
    assert manifest is admitted
    assert {name: value for name, value in options.items() if name != "emit"} == expected
    output = capsys.readouterr()
    assert "Robot 'four-wheel': passed (application ready: not requested)" in output.out
    assert "poll_sample_ns=8" in output.out and "\\x1b[2J" in output.out
    assert "\x1b" not in output.out
    payload = json.loads(report.read_text())
    assert payload["status"] == "passed"
    assert (payload["result"]["name"], payload["result"]["ready"], payload["result"]["origin_ns"],
            payload["result"]["end_ns"], payload["result"]["observation_batches"]) == (
        "four-wheel", "not requested", 10, 250_000_010, 3)
    assert payload["result"]["children"] == [{"name": "chassis worker", "returncode": 0}]
    assert "no global CPU/plant barrier" in " ".join(payload["result"]["limits"])


@pytest.mark.parametrize("group,arguments", [
    ("robot", ["--duration", "nan"]), ("robot", ["--paused"]),
    ("arena", ["--duration", "nan"]), ("arena", ["--duration", "0"]),
    ("arena", ["--timeout", "inf"]), ("arena", ["--startup-timeout", "0"]),
    ("arena", ["--qmp", "native.sock"]), ("arena", ["--gdb-port", "1235"]),
    ("arena", ["--paused"]),
    ("scenario", ["--duration", "0.25"]), ("scenario", ["--qmp", "native.sock"]),
    ("scenario", ["--gdb-port", "1235"]), ("scenario", ["--paused"]),
    ("scenario", ["--debug"]), ("scenario", ["--timeout", "inf"]),
    ("scenario", ["--startup-timeout", "0"]),
])
def test_manifest_run_option_admission_precedes_manifest_loading(monkeypatch, group, arguments):
    def forbidden(*args, **kwargs):
        pytest.fail(f"{group} manifest loaded before CLI option admission")

    loader, _runner = manifest_runtime(group)
    monkeypatch.setattr(loader, "load_manifest", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main([group, "run", "fixture.yaml", *arguments])
    assert caught.value.code == 2


@pytest.mark.parametrize("group", ["lab", "robot", "arena", "scenario"])
def test_report_write_failure_cannot_publish_passed(monkeypatch, tmp_path, capsys, group):
    loader, runner = manifest_runtime(group)
    monkeypatch.setattr(loader, "load_manifest", lambda path: object())
    result = {"lab": runtime.LabResult("fixture", 1, 10, ("pass",), (), (), 1),
              "robot": robot_runtime.RobotResult("four-wheel", "not requested", 1, 2, 1, ()),
              "arena": arena_runtime.ArenaResult("two-wheel", 1, 2, 1, (), ()),
              "scenario": arena_runtime.ArenaResult("two-wheel", 1, 2, 1, (), ())}[group]
    monkeypatch.setattr(runner, "run_manifest", lambda *_args, **_kwargs: result)
    assert cli.main([group, "run", "fixture.yaml", "--report", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "error:" in output.err and "passed" not in output.out


@pytest.mark.parametrize("group,arguments,expected", [
    ("arena", [], {"duration": 5.0, "qemu": None, "startup_timeout": 10.0, "timeout": 30.0}),
    ("arena", ["--duration", "0.25", "--qemu", "native-qemu", "--startup-timeout", "2", "--timeout", "20"],
     {"duration": 0.25, "qemu": "native-qemu", "startup_timeout": 2.0, "timeout": 20.0}),
    ("scenario", [], {"qemu": None, "startup_timeout": 10.0, "timeout": 30.0}),
    ("scenario", ["--qemu", "native-qemu", "--startup-timeout", "2", "--timeout", "20"],
     {"qemu": "native-qemu", "startup_timeout": 2.0, "timeout": 20.0}),
])
def test_world_runtime_options_named_uart_and_complete_report(monkeypatch, tmp_path, capsys, group, arguments, expected):
    admitted = object()
    loaded, calls = [], []
    loader, runner = manifest_runtime(group)
    runtime_imports = []
    original_import = builtins.__import__

    def ordered_import(name, *args, **kwargs):
        if name == runner.__name__:
            assert loaded == [Path(f"{group}.yaml")]
            runtime_imports.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", ordered_import)

    def load(path):
        loaded.append(path)
        return admitted

    def run(manifest, **options):
        calls.append((manifest, options))
        # The same UART and unrelated local epochs must remain distinct.
        options["emit"](arena_runtime.ArenaEvent("east", ChannelEvent("uart1", 8, b"hello\n\x1b[2J\x00\xff")))
        options["emit"](arena_runtime.ArenaEvent("west", ChannelEvent("uart1", 200, b"bravo\n")))
        return arena_runtime.ArenaResult("two-chassis", 7_000_000, 257_000_000, 257,
            (arena_runtime.ArenaBoardResult("east", "alpha/", "uart_marker", 25_000_100, 280_500_400, 101, 3),
             arena_runtime.ArenaBoardResult("west", "bravo/", "not_waited", None, 410_900_700, 202, 4)),
            ({"label": "east/upper:observer", "pid": 123, "stdout_tail": "literal $(pwd)"},),
            ("Duration is shared logical time; QEMU epochs remain independent.",
             "ACK/DONE do not establish CPU lockstep or wall pacing."))

    monkeypatch.setattr(loader, "load_manifest", load)
    monkeypatch.setattr(runner, "run_manifest", run)
    report = tmp_path / f"{group}.json"
    assert cli.main([group, "run", f"{group}.yaml", *arguments, "--report", str(report)]) == 0
    assert loaded == [Path(f"{group}.yaml")]
    assert runtime_imports == [runner.__name__]
    assert len(calls) == 1
    manifest, options = calls[0]
    assert manifest is admitted
    assert options["emit"] is cli._emit_arena
    assert {name: value for name, value in options.items() if name != "emit"} == expected
    output = capsys.readouterr()
    assert f"{group.title()} 'two-chassis': passed" in output.out and "2 boards" in output.out
    assert "UART east.uart1 poll_sample_ns=8" in output.out
    assert "UART west.uart1 poll_sample_ns=200" in output.out
    assert "\\x1b[2J\\x00\\xff" in output.out and "\x1b" not in output.out and "\x00" not in output.out
    assert json.loads(report.read_text()) == {"status": "passed", "result": {
        "name": "two-chassis", "origin_ns": 7_000_000, "end_ns": 257_000_000, "steps": 257,
        "boards": [
            {"name": "east", "prefix": "alpha/", "ready": "uart_marker", "ready_qemu_ns": 25_000_100,
             "end_qemu_ns": 280_500_400, "reset_qemu_ns": 101, "observation_batches": 3},
            {"name": "west", "prefix": "bravo/", "ready": "not_waited", "ready_qemu_ns": None,
             "end_qemu_ns": 410_900_700, "reset_qemu_ns": 202, "observation_batches": 4},
        ],
        "children": [{"label": "east/upper:observer", "pid": 123, "stdout_tail": "literal $(pwd)"}],
        "referees": [],
        "limits": ["Duration is shared logical time; QEMU epochs remain independent.",
                   "ACK/DONE do not establish CPU lockstep or wall pacing."],
    }}


def replay_cli_inputs(tmp_path):
    """Actual original YAML plus the existing independently encoded MCAP fixture."""
    import yaml
    from test_arena_manifest import write_arena
    from test_arena_trace import CLOCKS, offline_replay_bytes

    arena = write_arena(tmp_path)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(yaml.safe_dump({"schema": 1, "arena": arena.name, "duration": "100ns",
        "assertions": [
            {"member": "west", "before": "20ns", "uart_contains": {"endpoint": "uart1", "text": "hello"}},
            {"member": "east", "before": "20ns", "uart_contains": {"endpoint": "uart1", "text": "hello"}},
        ]}), encoding="utf-8")
    trace = tmp_path / "capture.mcap"
    trace.write_bytes(offline_replay_bytes())
    arguments = ["scenario", "replay", str(scenario), str(trace)]
    for member, epoch in CLOCKS.items():
        arguments += ["--clock", f"{member}={epoch}"]
    return scenario, trace, arguments


def test_replay_help_isolated_and_lazy(tmp_path, monkeypatch):
    result = isolated(tmp_path, "scenario", "replay", "--help")
    assert result.returncode == 0, result.stderr
    assert "--clock" in result.stdout and "--report" in result.stdout
    assert all(option not in result.stdout for option in ("--qemu", "--timeout", "--duration", "--gdb-port"))
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == "yaml" or name == "mujoco" or name.startswith("sim."):
            pytest.fail(f"replay help loaded an optional dependency: {name}")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(SystemExit) as caught:
        cli.main(["scenario", "replay", "--help"])
    assert caught.value.code == 0


@pytest.mark.parametrize("options", [[], ["--clock", "east=qemu:east", "--qemu", "native"],
    ["--clock", "east=qemu:east", "--timeout", "1"]])
def test_replay_argparse_rejects_before_loading(monkeypatch, options):
    monkeypatch.setattr(scenario_manifest_module, "load_manifest",
                        lambda *_: pytest.fail("replay loaded before argparse admission"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["scenario", "replay", "unused.yaml", "unused.mcap", *options])
    assert caught.value.code == 2


@pytest.mark.parametrize("values", [["east"], ["=epoch"], ["east="],
    ["east=one", "east=two"], ["east=same", "west=same"], ["east=" + "x" * 256]])
def test_replay_clock_admission_precedes_all_input_reads(monkeypatch, capsys, values):
    from sim.boards import launch
    monkeypatch.setattr(scenario_manifest_module, "load_manifest",
                        lambda *_: pytest.fail("invalid clock reached manifest"))
    monkeypatch.setattr(launch, "regular_file", lambda *_: pytest.fail("invalid clock reached trace"))
    options = [item for value in values for item in ("--clock", value)]
    assert cli.main(["scenario", "replay", "unused.yaml", "unused.mcap", *options]) == 1
    output = capsys.readouterr()
    assert "error:" in output.err and "passed" not in output.out


def replay_cli_stream(monkeypatch, tmp_path, *, primary=None, close_error=None):
    from io import BytesIO
    from sim.scenario import replay
    from sim.arena.trace import ArenaTraceCompletion

    calls = []
    admitted = object()
    trace = tmp_path / "source.mcap"
    trace.write_bytes(b"borrowed input")
    class Source(BytesIO):
        def close(self):
            calls.append("close")
            super().close()
            if close_error is not None:
                raise close_error
    source = Source(trace.read_bytes())
    original_open = Path.open
    def open_file(path, *args, **kwargs):
        if path == trace:
            assert args == ("rb",) and kwargs == {}
            calls.append("open")
            return source
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)
    def load(path):
        calls.append(("load", path))
        return admitted
    def evaluate(actual, manifest, *, clocks):
        calls.append(("evaluate", clocks))
        assert actual is source and manifest is admitted and not actual.closed
        assert actual.read() == b"borrowed input"
        if primary is not None:
            raise primary
        return replay.ScenarioReplayResult(("pass",), (),
            (ArenaTraceCompletion("east", clocks["east"], 1, 2, 1),))
    monkeypatch.setattr(scenario_manifest_module, "load_manifest", load)
    monkeypatch.setattr(replay, "replay_assertions", evaluate)
    return trace, source, calls


def test_replay_forwards_once_and_closes_before_exclusive_publication(monkeypatch, tmp_path, capsys):
    trace, source, calls = replay_cli_stream(monkeypatch, tmp_path)
    write = cli._write_report
    def report(path, payload, *, exclusive=False):
        assert source.closed and calls[-1] == "close" and exclusive is True
        calls.append("report")
        write(path, payload, exclusive=exclusive)
    monkeypatch.setattr(cli, "_write_report", report)
    output = tmp_path / "new.json"
    assert cli.main(["scenario", "replay", "original.yaml", str(trace),
                     "--clock", "east=qemu:east:run=15", "--report", str(output)]) == 0
    assert calls == [("load", Path("original.yaml")), "open",
                     ("evaluate", {"east": "qemu:east:run=15"}), "close", "report"]
    payload = json.loads(output.read_text())
    assert payload["status"] == "passed" and payload["result"]["status"] == ["pass"]
    assert "passed" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["close", "primary-and-close", "interrupt-and-close", "sigterm"])
def test_replay_source_lifecycle_preserves_primary_and_signal(monkeypatch, tmp_path, capsys, failure):
    primary = (KeyboardInterrupt("replay interrupted") if failure == "interrupt-and-close"
               else OSError("first replay read failed") if failure == "primary-and-close" else None)
    closing = OSError("source close failed") if failure != "sigterm" else None
    trace, source, calls = replay_cli_stream(monkeypatch, tmp_path, primary=primary, close_error=closing)
    if failure == "sigterm":
        from sim.scenario import replay
        def interrupted(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
        monkeypatch.setattr(replay, "replay_assertions", interrupted)
    previous = signal.getsignal(signal.SIGTERM)
    output = tmp_path / "failed.json"
    assert cli.main(["scenario", "replay", "original.yaml", str(trace), "--clock", "east=epoch",
                     "--report", str(output)]) == (130 if failure in ("interrupt-and-close", "sigterm") else 1)
    assert source.closed and calls.count("close") == 1
    assert signal.getsignal(signal.SIGTERM) is previous
    payload = json.loads(output.read_text())
    assert payload["status"] == "failed" and "result" not in payload
    if primary is not None:
        assert payload["error"] == str(primary) and payload["notes"] == primary.__notes__
        assert "Scenario replay source close failed: source close failed" in payload["notes"]
    elif failure == "close":
        assert payload["error"] == str(closing)
    assert "passed" not in capsys.readouterr().out


@pytest.mark.parametrize("entry", ["isolated", "module"])
@pytest.mark.parametrize("outcome", ["passed", "assertion-failed", "late-trailer"])
def test_replay_actual_mcap_original_manifest_entrypoints(tmp_path, entry, outcome):
    import yaml
    scenario, trace, arguments = replay_cli_inputs(tmp_path)
    if outcome == "assertion-failed":
        document = yaml.safe_load(scenario.read_text())
        document["assertions"][0]["uart_contains"]["text"] = "missing"
        scenario.write_text(yaml.safe_dump(document), encoding="utf-8")
    elif outcome == "late-trailer":
        trace.write_bytes(trace.read_bytes()[:-1] + b"!")
    before = scenario.read_bytes(), trace.read_bytes()
    report = tmp_path / "result.json"
    arguments += ["--report", str(report)]
    result = (isolated(tmp_path, *arguments) if entry == "isolated" else
              subprocess.run([sys.executable, "-m", "sim.apps.unmigrated_cli", *arguments], cwd=ROOT,
                             capture_output=True, text=True, timeout=5))
    assert result.returncode == (0 if outcome == "passed" else 1), result.stderr
    payload = json.loads(report.read_text())
    assert payload["status"] == ("passed" if outcome == "passed" else "failed")
    if outcome == "late-trailer":
        assert "result" not in payload and "error:" in result.stderr
    else:
        assert payload["result"]["status"] == (["pass", "pass"] if outcome == "passed" else ["fail", "pass"])
        assert [item["name"] for item in payload["result"]["completions"]] == ["east", "west"]
        if outcome == "assertion-failed":
            assert "west" in result.stderr and "assertion failed:" in result.stderr
    assert (scenario.read_bytes(), trace.read_bytes()) == before
    assert ("passed" in result.stdout) == (outcome == "passed")


@pytest.mark.parametrize("alias", ["trace", "manifest", "symlink", "hardlink"])
def test_replay_exclusive_report_preserves_existing_input_aliases(tmp_path, capsys, alias):
    scenario, trace, arguments = replay_cli_inputs(tmp_path)
    report = trace if alias == "trace" else scenario if alias == "manifest" else tmp_path / "alias.json"
    if alias == "symlink":
        report.symlink_to(trace)
    elif alias == "hardlink":
        os.link(trace, report)
    before = scenario.read_bytes(), trace.read_bytes(), report.read_bytes()
    assert cli.main([*arguments, "--report", str(report)]) == 1
    assert (scenario.read_bytes(), trace.read_bytes(), report.read_bytes()) == before
    output = capsys.readouterr()
    assert "error:" in output.err and "passed" not in output.out


def test_replay_actual_evaluation_forbids_runtime_acquisition_and_fifo_open(monkeypatch, tmp_path, capsys):
    import socket
    scenario, trace, arguments = replay_cli_inputs(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("offline replay acquired native/runtime resources")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(arena_runtime, "create_arena_plant", forbidden)
    monkeypatch.setattr(arena_runtime, "running_qemu", forbidden)
    assert cli.main(arguments) == 0
    assert "passed" in capsys.readouterr().out
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        if path == fifo:
            pytest.fail("replay tried to open a FIFO")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    arguments[3] = str(fifo)
    assert cli.main(arguments) == 1
    output = capsys.readouterr()
    assert "error:" in output.err and "passed" not in output.out


def test_replay_interrupt_survives_secondary_report_failure(monkeypatch, tmp_path, capsys):
    trace, source, calls = replay_cli_stream(monkeypatch, tmp_path,
                                           primary=KeyboardInterrupt("primary interrupt"))
    assert cli.main(["scenario", "replay", "original.yaml", str(trace), "--clock", "east=epoch",
                     "--report", str(tmp_path)]) == 130
    assert source.closed and calls.count("close") == 1
    output = capsys.readouterr()
    assert "primary interrupt" in output.err and "error:" in output.err
    assert "passed" not in output.out
