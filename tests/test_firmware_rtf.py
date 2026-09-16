"""Contract tests for the firmware RTF sampling epoch."""

from __future__ import annotations

import importlib.util
import io
import math
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS))
COLLECTOR = TOOLS / "dm_mc02_firmware_rtf.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_firmware_rtf_test", COLLECTOR)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def test_forward_tick_delta_accepts_normal_progress() -> None:
    assert collector.forward_tick_delta(100, 150) == 50


def test_forward_tick_delta_accepts_one_uint32_wrap() -> None:
    assert collector.forward_tick_delta(0xFFFF_FFF0, 0x10) == 32


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (10_000, 100),
        (0, 0x8000_0000),
        (0x8000_0000, 0),
    ],
)
def test_forward_tick_delta_rejects_backward_or_half_range_ambiguity(
    previous: int, current: int
) -> None:
    with pytest.raises(collector.SampleInvalidError, match="did not move forward"):
        collector.forward_tick_delta(previous, current)


def test_tracker_accumulates_progress_across_wrap() -> None:
    tracker = collector.TickEpochTracker(0xFFFF_FFF0, 3)

    assert tracker.observe(0x10, watchdog_timeouts=3) == 32
    assert tracker.observe(0x15, watchdog_timeouts=3) == 5
    assert tracker.previous_tick == 0x15
    assert tracker.total_ticks == 37


def test_any_qmp_reset_event_invalidates_the_epoch_before_tick_admission() -> None:
    tracker = collector.TickEpochTracker(100, 0)
    events = [
        {"event": "STOP", "data": {}},
        {
            "event": "RESET",
            "data": {"guest": False, "reason": "host-qmp-system-reset"},
        },
    ]

    with pytest.raises(collector.SampleInvalidError, match="QMP RESET event"):
        tracker.observe(
            150,
            events=events,
            watchdog_timeouts=0,
            phase="wall-clock sample",
        )

    assert tracker.previous_tick == 100
    assert tracker.total_ticks == 0


def test_watchdog_change_invalidates_the_epoch_without_advancing_it() -> None:
    tracker = collector.TickEpochTracker(200, 4)

    with pytest.raises(collector.SampleInvalidError, match="IWDG timeout count changed"):
        tracker.observe(250, watchdog_timeouts=5, phase="virtual-time sample")

    assert tracker.previous_tick == 200
    assert tracker.total_ticks == 0


def test_non_reset_events_do_not_invalidate_forward_progress() -> None:
    tracker = collector.TickEpochTracker(10, 2)

    delta = tracker.observe(
        25,
        events=[{"event": "STOP"}, {"event": "RESUME"}, object()],
        watchdog_timeouts=2,
    )

    assert delta == 15
    assert tracker.total_ticks == 15


class _FakeQmp:
    def __init__(self, tick: int, timeouts: int, events: list[dict[str, Any]]):
        self.tick = tick
        self.timeouts = timeouts
        self.events = events
        self.commands: list[str] = []
        self.closed = False
        self.status = "prelaunch"
        self.timeout: float | None = None

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def command(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        del arguments
        self.commands.append(name)
        if name == "human-monitor-command":
            return f"0x20000000: 0x{self.tick:08x}"
        if name == "qom-get":
            return f"starts=1,reloads=1,timeouts={self.timeouts}"
        if name == "query-status":
            return {
                "status": self.status,
                "running": self.status == "running",
            }
        if name == "cont":
            self.status = "running"
        if name == "stop":
            self.status = "paused"
        return {}

    def get_events(self, wait: bool = False) -> list[dict[str, Any]]:
        assert wait is False
        events, self.events = self.events, []
        return events

    def clear_events(self) -> None:
        self.commands.append("clear-events")
        self.events.clear()

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    pid = 1234

    def __init__(self) -> None:
        self.stderr = io.StringIO("")
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.terminated = True
        return 0

    def kill(self) -> None:
        self.terminated = True


@pytest.mark.parametrize(
    ("phase", "events"),
    [
        ("wall-clock sample", [{"event": "RESET", "data": {"guest": False}}]),
        ("virtual-time sample", [{"event": "RESET", "data": {"guest": True}}]),
    ],
)
def test_observe_runtime_applies_the_same_epoch_admission_to_both_modes(
    phase: str, events: list[dict[str, Any]]
) -> None:
    qmp = _FakeQmp(tick=300, timeouts=0, events=events)
    process = _FakeProcess()
    tracker = collector.TickEpochTracker(250, 0)

    with pytest.raises(collector.SampleInvalidError, match="QMP RESET event"):
        collector.observe_runtime(qmp, process, tracker, "0x20000000", phase)

    assert qmp.commands == ["human-monitor-command", "qom-get"]
    assert tracker.previous_tick == 250
    assert tracker.total_ticks == 0


def test_stop_epoch_confirms_pause_before_final_tick_admission() -> None:
    qmp = _FakeQmp(tick=300, timeouts=2, events=[{"event": "STOP"}])
    qmp.status = "running"
    process = _FakeProcess()
    tracker = collector.TickEpochTracker(250, 2)

    tick, timeouts, diagnostics = collector.stop_epoch(
        qmp, process, tracker, "0x20000000"
    )

    assert (tick, timeouts) == (300, 2)
    assert diagnostics == "starts=1,reloads=1,timeouts=2"
    assert qmp.commands == [
        "stop",
        "query-status",
        "human-monitor-command",
        "qom-get",
        "query-status",
    ]
    assert tracker.total_ticks == 50


def test_config_rejects_non_qemu_tcg_thread_value() -> None:
    config = collector.RunConfig(
        qemu="qemu-system-arm",
        elf="firmware.elf",
        tick_address="0x20000000",
        tcg_thread="parallel",
    )

    with pytest.raises(ValueError, match="TCG thread must be single or multi"):
        collector.validate_config(config)


def test_sampling_epoch_setup_rejects_other_stopped_runstates() -> None:
    qmp = _FakeQmp(tick=0, timeouts=0, events=[])
    qmp.status = "shutdown"

    with pytest.raises(collector.SampleInvalidError, match="unexpected stopped state"):
        collector.require_stopped_status(
            qmp, "sampling epoch setup", expected_status="prelaunch"
        )


@pytest.mark.parametrize(
    ("virtual_seconds", "expected_phase"),
    [
        (None, "wall-clock sample"),
        (0.001, "virtual-time sample"),
    ],
)
def test_baseline_and_virtual_run_paths_share_observe_runtime_tracker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    virtual_seconds: float | None,
    expected_phase: str,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "qmp.sock").touch()
    process = _FakeProcess()
    qmp = _FakeQmp(tick=0, timeouts=7, events=[])
    observations: list[tuple[collector.TickEpochTracker, str]] = []

    def reject_first_observation(
        observed_qmp: _FakeQmp,
        observed_process: _FakeProcess,
        tracker: collector.TickEpochTracker,
        tick_address: str,
        phase: str,
        **_kwargs: Any,
    ) -> tuple[int, str]:
        assert observed_qmp is qmp
        assert observed_process is process
        assert tick_address == "0x20000000"
        observations.append((tracker, phase))
        raise collector.SampleInvalidError("controlled epoch rejection")

    monkeypatch.setattr(collector.tempfile, "mkdtemp", lambda **_kwargs: str(run_dir))
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(collector, "QmpSession", lambda *_args, **_kwargs: qmp)
    monkeypatch.setattr(collector, "observe_runtime", reject_first_observation)

    config = collector.RunConfig(
        qemu="qemu-system-arm",
        elf="firmware.elf",
        tick_address="0x20000000",
        warmup=0,
        duration=0.001,
        virtual_seconds=virtual_seconds,
        ready_tick=0,
        poll_interval=0.001,
    )

    with pytest.raises(collector.SampleInvalidError, match="controlled epoch rejection"):
        collector.run_one(config, 1)

    assert len(observations) == 1
    tracker, phase = observations[0]
    assert isinstance(tracker, collector.TickEpochTracker)
    assert tracker.initial_tick == 0
    assert tracker.initial_watchdog_timeouts == 7
    assert phase == expected_phase
    assert qmp.closed is True
    assert process.terminated is True
    assert qmp.commands[:5] == [
        "query-status",
        "qom-get",
        "human-monitor-command",
        "clear-events",
        "cont",
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("warmup", math.nan, "warmup must be finite"),
        ("duration", math.inf, "duration must be finite"),
        ("virtual_seconds", math.nan, "virtual seconds must be finite"),
        ("virtual_seconds", math.inf, "virtual seconds must be finite"),
        ("min_rtf", math.nan, "minimum RTF must be finite"),
        ("poll_interval", math.inf, "poll interval must be finite"),
        ("startup_timeout", math.nan, "startup timeout must be finite"),
    ],
)
def test_config_rejects_non_finite_numeric_values(
    field: str, value: float, message: str
) -> None:
    values: dict[str, Any] = {
        "qemu": "qemu-system-arm",
        "elf": "firmware.elf",
        "tick_address": "0x20000000",
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        collector.validate_config(collector.RunConfig(**values))


def test_config_rejects_virtual_window_that_rounds_to_zero_ticks() -> None:
    config = collector.RunConfig(
        qemu="qemu-system-arm",
        elf="firmware.elf",
        tick_address="0x20000000",
        virtual_seconds=0.0004,
    )

    with pytest.raises(ValueError, match="at least one tick"):
        collector.validate_config(config)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def monotonic_ns(self) -> int:
        return int(self.now * 1_000_000_000)

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_startup_ready_deadline_cleans_up_a_live_frozen_guest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "qmp.sock").touch()
    process = _FakeProcess()
    qmp = _FakeQmp(tick=0, timeouts=0, events=[])
    clock = _FakeClock()

    monkeypatch.setattr(collector.tempfile, "mkdtemp", lambda **_kwargs: str(run_dir))
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(collector, "QmpSession", lambda *_args, **_kwargs: qmp)
    monkeypatch.setattr(collector.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(collector.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(collector.time, "sleep", clock.sleep)

    config = collector.RunConfig(
        qemu="qemu-system-arm",
        elf="firmware.elf",
        tick_address="0x20000000",
        virtual_seconds=0.001,
        ready_tick=1,
        poll_interval=0.010,
        startup_timeout=0.025,
    )

    with pytest.raises(collector.StartupTimeoutError, match="startup-ready"):
        collector.run_one(config, 1)

    assert process.terminated is True
    assert qmp.closed is True
    assert not run_dir.exists()


def test_early_process_exit_reports_tempfile_stderr_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    process = _FakeProcess()
    process.terminated = True

    def start_process(*_args: Any, **kwargs: Any) -> _FakeProcess:
        kwargs["stderr"].write("controlled QEMU startup failure\n")
        kwargs["stderr"].flush()
        assert kwargs["stderr"] is not subprocess.PIPE
        return process

    monkeypatch.setattr(collector.tempfile, "mkdtemp", lambda **_kwargs: str(run_dir))
    monkeypatch.setattr(collector.subprocess, "Popen", start_process)

    config = collector.RunConfig(
        qemu="qemu-system-arm",
        elf="firmware.elf",
        tick_address="0x20000000",
    )

    with pytest.raises(RuntimeError, match="controlled QEMU startup failure"):
        collector.run_one(config, 1)

    assert not run_dir.exists()


def test_qmp_disconnect_during_setup_still_cleans_up_process_and_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "qmp.sock").touch()
    process = _FakeProcess()

    class DisconnectedQmp(_FakeQmp):
        def command(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> Any:
            del name, arguments
            raise EOFError("controlled QMP disconnect")

    qmp = DisconnectedQmp(tick=0, timeouts=0, events=[])
    monkeypatch.setattr(collector.tempfile, "mkdtemp", lambda **_kwargs: str(run_dir))
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(collector, "QmpSession", lambda *_args, **_kwargs: qmp)

    config = collector.RunConfig(
        qemu="qemu-system-arm",
        elf="firmware.elf",
        tick_address="0x20000000",
    )

    with pytest.raises(EOFError, match="controlled QMP disconnect"):
        collector.run_one(config, 1)

    assert process.terminated is True
    assert qmp.closed is True
    assert not run_dir.exists()


def test_cli_reports_qmp_disconnect_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def disconnect(_config: collector.RunConfig) -> list[dict[str, Any]]:
        raise EOFError("controlled QMP disconnect")

    monkeypatch.setattr(collector, "run", disconnect)

    status = collector.main(
        [
            "--qemu",
            "qemu-system-arm",
            "--elf",
            "firmware.elf",
            "--tick-address",
            "0x20000000",
        ]
    )

    assert status == 1
    assert capsys.readouterr().err == "error: controlled QMP disconnect\n"


def test_cleanup_uses_bounded_term_kill_and_releases_remaining_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    class StubbornProcess:
        stderr = None

        def __init__(self) -> None:
            self.dead = False
            self.killed = False

        def poll(self) -> int | None:
            return 0 if self.dead else None

        def terminate(self) -> None:
            events.append("terminate")

        def wait(self, timeout: float) -> int:
            events.append(f"wait:{timeout}")
            if not self.killed:
                raise subprocess.TimeoutExpired("qemu", timeout)
            self.dead = True
            return 0

        def kill(self) -> None:
            events.append("kill")
            self.killed = True

    class BrokenQmp:
        def close(self) -> None:
            events.append("qmp-close")
            raise TimeoutError("controlled close timeout")

    class StderrFile(io.StringIO):
        def close(self) -> None:
            events.append("stderr-close")
            super().close()

    monkeypatch.setattr(
        collector.shutil,
        "rmtree",
        lambda *_args, **_kwargs: events.append("rmtree"),
    )

    error = collector.cleanup_run_resources(
        StubbornProcess(), BrokenQmp(), StderrFile(), str(tmp_path / "run")
    )

    assert error is None
    assert events == [
        "terminate",
        "wait:2.0",
        "kill",
        "wait:2.0",
        "qmp-close",
        "stderr-close",
        "rmtree",
    ]
