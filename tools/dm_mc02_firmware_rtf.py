#!/usr/bin/env python3
"""Measure DM-MC02 firmware progress in one reset-observed QMP epoch."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Sequence, TextIO, TypeVar

from dm_mc02_qmp import QmpSession


UINT32_MASK = 0xFFFF_FFFF
UINT32_HALF_RANGE = 0x8000_0000
QMP_COMMAND_TIMEOUT_SECONDS = 2.0
PROCESS_CLEANUP_TIMEOUT_SECONDS = 2.0
STDERR_EXCERPT_BYTES = 16 * 1024
T = TypeVar("T")


class SampleInvalidError(RuntimeError):
    """The observed values no longer belong to one valid sampling epoch."""


class StartupTimeoutError(RuntimeError):
    """The process did not reach the requested sampling-ready boundary."""


def forward_tick_delta(previous: int, current: int) -> int:
    """Return a forward uint32 delta, accepting wrap but rejecting ambiguity."""
    if not 0 <= previous <= UINT32_MASK or not 0 <= current <= UINT32_MASK:
        raise ValueError("tick values must be uint32")
    delta = (current - previous) & UINT32_MASK
    if delta >= UINT32_HALF_RANGE:
        raise SampleInvalidError(
            f"tick did not move forward: {previous} -> {current} (delta={delta})"
        )
    return delta


def _event_name(event: Any) -> str | None:
    if isinstance(event, dict):
        value = event.get("event")
        return value if isinstance(value, str) else None
    return None


@dataclass
class TickEpochTracker:
    """Validate tick, reset-event and watchdog observations after QMP cont."""

    initial_tick: int
    initial_watchdog_timeouts: int
    previous_tick: int = 0
    total_ticks: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.initial_tick <= UINT32_MASK:
            raise ValueError("initial tick must be uint32")
        if self.initial_watchdog_timeouts < 0:
            raise ValueError("initial watchdog timeout count must be non-negative")
        self.previous_tick = self.initial_tick

    def observe(
        self,
        tick: int,
        *,
        events: Iterable[Any] = (),
        watchdog_timeouts: int | None = None,
        phase: str = "sample",
    ) -> int:
        reset_events = [event for event in events if _event_name(event) == "RESET"]
        if reset_events:
            raise SampleInvalidError(
                f"QMP RESET event during {phase}: {reset_events[-1]!r}"
            )
        if (
            watchdog_timeouts is not None
            and watchdog_timeouts != self.initial_watchdog_timeouts
        ):
            raise SampleInvalidError(
                f"IWDG timeout count changed during {phase}: "
                f"{self.initial_watchdog_timeouts} -> {watchdog_timeouts}"
            )
        delta = forward_tick_delta(self.previous_tick, tick)
        self.previous_tick = tick
        self.total_ticks += delta
        return delta


@dataclass(frozen=True)
class RunConfig:
    qemu: str
    elf: str
    tick_address: str
    warmup: float = 0.25
    duration: float = 1.0
    virtual_seconds: float | None = None
    runs: int = 1
    min_rtf: float | None = None
    ready_tick: int = 1
    poll_interval: float = 0.250
    startup_timeout: float = 10.0
    iwdg_boot_grace_ms: int | None = None
    adc_accurate_timing: bool = False
    tcg_tb_size: int | None = None
    tcg_thread: str | None = None


def read_tick(qmp: QmpSession, tick_address: str) -> int:
    result = qmp.command(
        "human-monitor-command",
        {"command-line": "xp /1wx " + tick_address},
    )
    matches = re.findall(r"0x([0-9a-fA-F]{1,8})", str(result or ""))
    if not matches:
        raise RuntimeError(f"cannot parse xTickCount from QMP response: {result!r}")
    return int(matches[-1], 16)


def read_iwdg_timeouts(qmp: QmpSession) -> tuple[int, str]:
    result = qmp.command(
        "qom-get",
        {"path": "/machine", "property": "iwdg-diagnostics"},
    )
    diagnostics = str(result or "")
    match = re.search(r"timeouts=(\d+)", diagnostics)
    if not match:
        raise RuntimeError(f"cannot read IWDG diagnostics: {diagnostics!r}")
    return int(match.group(1)), diagnostics


def process_cpu_seconds(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stream:
            fields = stream.read().split()
        ticks = int(fields[13]) + int(fields[14])
        return ticks / os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def process_rss_kib(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, IndexError, ValueError):
        return None
    return None


def _stderr_excerpt(
    process: subprocess.Popen[str], stderr_file: TextIO | None
) -> str:
    if stderr_file is not None:
        try:
            stderr_file.flush()
            position = stderr_file.tell()
            stderr_file.seek(0, os.SEEK_END)
            end = stderr_file.tell()
            stderr_file.seek(max(0, end - STDERR_EXCERPT_BYTES))
            text = stderr_file.read().strip()
            stderr_file.seek(position)
            return text
        except (OSError, ValueError):
            return ""
    if process.stderr is not None:
        return process.stderr.read(STDERR_EXCERPT_BYTES).strip()
    return ""


def _assert_process_running(
    process: subprocess.Popen[str],
    phase: str,
    stderr_file: TextIO | None = None,
) -> None:
    status = process.poll()
    if status is None:
        return
    error = _stderr_excerpt(process, stderr_file)
    raise RuntimeError(f"QEMU exited during {phase} with status {status}: {error}")


def _deadline_remaining(deadline: float, phase: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StartupTimeoutError(f"{phase} exceeded startup timeout")
    return remaining


def _set_startup_qmp_timeout(qmp: QmpSession, deadline: float, phase: str) -> None:
    qmp.settimeout(
        min(QMP_COMMAND_TIMEOUT_SECONDS, _deadline_remaining(deadline, phase))
    )


def _startup_qmp_call(
    qmp: QmpSession,
    deadline: float,
    phase: str,
    callback: Callable[[], T],
) -> T:
    _set_startup_qmp_timeout(qmp, deadline, phase)
    try:
        result = callback()
    except TimeoutError as exc:
        raise StartupTimeoutError(f"{phase} QMP command timed out") from exc
    _deadline_remaining(deadline, phase)
    return result


def require_stopped_status(
    qmp: QmpSession,
    phase: str,
    *,
    expected_status: str | None = None,
) -> dict[str, Any]:
    """Require a QMP runstate that cannot advance the sampling epoch."""
    status = qmp.command("query-status")
    if not isinstance(status, dict) or status.get("running") is not False:
        raise SampleInvalidError(f"QEMU is not stopped during {phase}: {status!r}")
    if expected_status is not None and status.get("status") != expected_status:
        raise SampleInvalidError(
            f"QEMU has unexpected stopped state during {phase}: {status!r}"
        )
    return status


def observe_runtime(
    qmp: QmpSession,
    process: subprocess.Popen[str],
    tracker: TickEpochTracker,
    tick_address: str,
    phase: str,
    *,
    stderr_file: TextIO | None = None,
    host_deadline: float | None = None,
) -> tuple[int, str]:
    """Take one ordered process/QMP/tick/watchdog observation."""
    _assert_process_running(process, phase, stderr_file)
    if host_deadline is None:
        tick = read_tick(qmp, tick_address)
        watchdog_timeouts, diagnostics = read_iwdg_timeouts(qmp)
    else:
        tick = _startup_qmp_call(
            qmp,
            host_deadline,
            phase,
            lambda: read_tick(qmp, tick_address),
        )
        watchdog_timeouts, diagnostics = _startup_qmp_call(
            qmp,
            host_deadline,
            phase,
            lambda: read_iwdg_timeouts(qmp),
        )
    events = qmp.get_events(wait=False)
    if host_deadline is not None:
        _deadline_remaining(host_deadline, phase)
    tracker.observe(
        tick,
        events=events,
        watchdog_timeouts=watchdog_timeouts,
        phase=phase,
    )
    _assert_process_running(process, phase, stderr_file)
    return tick, diagnostics


def stop_epoch(
    qmp: QmpSession,
    process: subprocess.Popen[str],
    tracker: TickEpochTracker,
    tick_address: str,
    stderr_file: TextIO | None = None,
) -> tuple[int, int, str]:
    """Close the running epoch before accepting its final observation."""
    _assert_process_running(process, "sample stop", stderr_file)
    qmp.command("stop")
    require_stopped_status(qmp, "sample stop", expected_status="paused")
    tick = read_tick(qmp, tick_address)
    watchdog_timeouts, diagnostics = read_iwdg_timeouts(qmp)
    events = qmp.get_events(wait=False)
    tracker.observe(
        tick,
        events=events,
        watchdog_timeouts=watchdog_timeouts,
        phase="sample stop",
    )
    require_stopped_status(qmp, "sample stop", expected_status="paused")
    trailing_events = qmp.get_events(wait=False)
    tracker.observe(
        tick,
        events=trailing_events,
        watchdog_timeouts=watchdog_timeouts,
        phase="sample stop",
    )
    _assert_process_running(process, "sample stop", stderr_file)
    return tick, watchdog_timeouts, diagnostics


def wait_wall_interval(
    seconds: float,
    *,
    qmp: QmpSession,
    process: subprocess.Popen[str],
    tracker: TickEpochTracker,
    tick_address: str,
    poll_interval: float,
    phase: str,
    stderr_file: TextIO | None = None,
) -> tuple[int, str]:
    """Wait in polling steps while retaining QMP reset observations."""
    deadline = time.monotonic() + seconds
    last_tick = tracker.previous_tick
    diagnostics = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
        last_tick, diagnostics = observe_runtime(
            qmp,
            process,
            tracker,
            tick_address,
            phase,
            stderr_file=stderr_file,
        )
    return last_tick, diagnostics


def cleanup_run_resources(
    process: subprocess.Popen[str] | None,
    qmp: QmpSession | None,
    stderr_file: TextIO | None,
    run_dir: str,
) -> str | None:
    """Release one run in bounded steps; return a process-cleanup error."""
    process_error: str | None = None
    process_running = False
    if process is not None:
        try:
            process_running = process.poll() is None
        except Exception as exc:
            process_running = True
            process_error = f"cannot inspect QEMU during cleanup: {exc}"
    if process is not None and process_running:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except Exception as exc:
            process_error = f"cannot terminate QEMU during cleanup: {exc}"
        try:
            process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except Exception as first_wait_error:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except Exception as exc:
                process_error = f"cannot kill QEMU during cleanup: {exc}"
            try:
                process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process_error = (
                    "QEMU remained alive after TERM and KILL deadlines"
                )
            except Exception as exc:
                process_error = f"cannot reap QEMU during cleanup: {exc}"
            if process_error is None and not isinstance(
                first_wait_error, subprocess.TimeoutExpired
            ):
                process_error = f"cannot reap QEMU after TERM: {first_wait_error}"

    if qmp is not None:
        try:
            qmp.close()
        except Exception:
            pass
    if stderr_file is not None:
        try:
            stderr_file.close()
        except Exception:
            pass
    try:
        shutil.rmtree(run_dir, ignore_errors=True)
    except Exception:
        pass
    return process_error


def build_qemu_command(config: RunConfig, qmp_path: str) -> list[str]:
    machine = "dm-mc02"
    if config.iwdg_boot_grace_ms is not None:
        machine += f",iwdg-boot-grace-ms={config.iwdg_boot_grace_ms}"
    if config.adc_accurate_timing:
        machine += ",adc-accurate-timing=on"
    command = [config.qemu]
    properties = []
    if config.tcg_thread is not None:
        properties.append(f"thread={config.tcg_thread}")
    if config.tcg_tb_size is not None:
        properties.append(f"tb-size={config.tcg_tb_size}")
    if properties:
        command += ["-accel", "tcg," + ",".join(properties)]
    command += [
        "-machine", machine,
        "-kernel", config.elf,
        "-nodefaults",
        "-display", "none",
        "-monitor", "none",
        "-serial", "none",
        "-S",
        "-qmp", f"unix:{qmp_path},server=on,wait=off",
    ]
    return command


def run_one(config: RunConfig, run_number: int) -> dict[str, Any]:
    run_dir = tempfile.mkdtemp(prefix="dm-mc02-firmware-rtf-")
    qmp_path = os.path.join(run_dir, "qmp.sock")
    process: subprocess.Popen[str] | None = None
    qmp: QmpSession | None = None
    stderr_file: TextIO | None = None
    try:
        startup_start = time.monotonic_ns()
        startup_deadline = time.monotonic() + config.startup_timeout
        stderr_file = open(
            os.path.join(run_dir, "qemu.stderr"),
            mode="w+",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            build_qemu_command(config, qmp_path),
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            text=True,
        )
        while not os.path.exists(qmp_path):
            _assert_process_running(process, "startup", stderr_file)
            remaining = _deadline_remaining(startup_deadline, "QMP socket startup")
            time.sleep(min(0.005, remaining))
        if not os.path.exists(qmp_path):
            raise RuntimeError("QEMU did not create its QMP socket")

        connect_timeout = _deadline_remaining(
            startup_deadline, "QMP connection"
        )
        try:
            qmp = QmpSession(
                qmp_path,
                timeout=min(QMP_COMMAND_TIMEOUT_SECONDS, connect_timeout),
                connect_timeout=connect_timeout,
                close_timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise StartupTimeoutError(
                "QMP connection exceeded startup timeout"
            ) from exc
        _startup_qmp_call(
            qmp,
            startup_deadline,
            "sampling epoch setup",
            lambda: require_stopped_status(
                qmp, "sampling epoch setup", expected_status="prelaunch"
            ),
        )
        initial_timeouts, _ = _startup_qmp_call(
            qmp,
            startup_deadline,
            "IWDG startup query",
            lambda: read_iwdg_timeouts(qmp),
        )
        initial_tick = _startup_qmp_call(
            qmp,
            startup_deadline,
            "tick startup query",
            lambda: read_tick(qmp, config.tick_address),
        )
        qmp.clear_events()
        tracker = TickEpochTracker(initial_tick, initial_timeouts)
        _startup_qmp_call(
            qmp,
            startup_deadline,
            "QEMU cont",
            lambda: qmp.command("cont"),
        )

        ready_tick_value = initial_tick
        startup_ready_ns = time.monotonic_ns()
        diagnostics = ""
        if config.virtual_seconds is not None:
            while ready_tick_value < config.ready_tick:
                remaining = _deadline_remaining(
                    startup_deadline, "startup-ready"
                )
                time.sleep(min(config.poll_interval, remaining))
                ready_tick_value, diagnostics = observe_runtime(
                    qmp,
                    process,
                    tracker,
                    config.tick_address,
                    "startup-ready",
                    stderr_file=stderr_file,
                    host_deadline=startup_deadline,
                )
            startup_ready_ns = time.monotonic_ns()
            qmp.settimeout(QMP_COMMAND_TIMEOUT_SECONDS)
            sample_total_ticks = tracker.total_ticks
            target_ticks = virtual_target_ticks(config.virtual_seconds)
            sample_start_ns = startup_ready_ns
            cpu_start = process_cpu_seconds(process.pid)
            rss_kib = process_rss_kib(process.pid)
            timeout = time.monotonic() + max(
                30.0, config.virtual_seconds * 4.0 + 10.0
            )
            while tracker.total_ticks - sample_total_ticks < target_ticks:
                time.sleep(config.poll_interval)
                _, diagnostics = observe_runtime(
                    qmp, process, tracker, config.tick_address,
                    "virtual-time sample",
                    stderr_file=stderr_file,
                )
                if time.monotonic() >= timeout:
                    raise RuntimeError("virtual-time sample exceeded host timeout")
        else:
            wait_wall_interval(
                config.warmup,
                qmp=qmp,
                process=process,
                tracker=tracker,
                tick_address=config.tick_address,
                poll_interval=config.poll_interval,
                phase="warmup",
                stderr_file=stderr_file,
            )
            sample_total_ticks = tracker.total_ticks
            sample_start_ns = time.monotonic_ns()
            cpu_start = process_cpu_seconds(process.pid)
            rss_kib = process_rss_kib(process.pid)
            _, diagnostics = wait_wall_interval(
                config.duration,
                qmp=qmp,
                process=process,
                tracker=tracker,
                tick_address=config.tick_address,
                poll_interval=config.poll_interval,
                phase="wall-clock sample",
                stderr_file=stderr_file,
            )

        _, final_timeouts, diagnostics = stop_epoch(
            qmp, process, tracker, config.tick_address, stderr_file
        )
        sample_end_ns = time.monotonic_ns()
        tick_delta = tracker.total_ticks - sample_total_ticks
        cpu_end = process_cpu_seconds(process.pid)
        elapsed_seconds = (sample_end_ns - sample_start_ns) / 1_000_000_000.0
        virtual_interval = tick_delta / 1000.0
        rtf = virtual_interval / elapsed_seconds if elapsed_seconds else 0.0
        cpu_utilization = None
        if cpu_start is not None and cpu_end is not None:
            cpu_utilization = (cpu_end - cpu_start) / elapsed_seconds * 100.0
        result = {
            "run": run_number,
            "startup_latency": (startup_ready_ns - startup_start) / 1_000_000_000.0,
            "elapsed": elapsed_seconds,
            "tick_delta": tick_delta,
            "virtual_seconds": virtual_interval,
            "rtf": rtf,
            "cpu": cpu_utilization,
            "rss": rss_kib,
            "initial_timeouts": initial_timeouts,
            "final_timeouts": final_timeouts,
            "diagnostics": diagnostics,
        }
        mode = "gate" if config.virtual_seconds is not None else "baseline"
        passed = final_timeouts == initial_timeouts and (
            config.min_rtf is None or rtf >= config.min_rtf
        )
        print(
            "RESULT: QEMU firmware RTF %s run %d %s"
            % (mode, run_number, "passed" if passed else "FAILED")
        )
        print("  startup latency: %.6f s" % result["startup_latency"])
        print("  host sample interval: %.6f s" % elapsed_seconds)
        print("  FreeRTOS tick delta: %d" % tick_delta)
        print("  virtual sample interval: %.6f s" % virtual_interval)
        print("  realtime factor: %.6fx" % rtf)
        print("  effective FreeRTOS ticks/s: %.3f" % (tick_delta / elapsed_seconds))
        if cpu_utilization is not None:
            print("  QEMU process CPU utilization: %.1f%%" % cpu_utilization)
        if rss_kib is not None:
            print("  QEMU process RSS at sample start: %d KiB" % rss_kib)
        print("  IWDG diagnostics: %s" % diagnostics)
        if not passed:
            raise RuntimeError("RTF gate failed")
        return result
    finally:
        cleanup_error = cleanup_run_resources(
            process, qmp, stderr_file, run_dir
        )
        if cleanup_error is not None:
            active_error = sys.exc_info()[1]
            if active_error is None:
                raise RuntimeError(cleanup_error)
            active_error.add_note(cleanup_error)


def virtual_target_ticks(seconds: float) -> int:
    scaled = seconds * 1000.0
    if not math.isfinite(scaled):
        raise ValueError("virtual seconds produce a non-finite tick target")
    ticks = int(round(scaled))
    if ticks < 1:
        raise ValueError("virtual seconds must select at least one tick")
    return ticks


def validate_config(config: RunConfig) -> None:
    finite_values = {
        "warmup": config.warmup,
        "duration": config.duration,
        "poll interval": config.poll_interval,
        "startup timeout": config.startup_timeout,
    }
    if config.virtual_seconds is not None:
        finite_values["virtual seconds"] = config.virtual_seconds
    if config.min_rtf is not None:
        finite_values["minimum RTF"] = config.min_rtf
    for name, value in finite_values.items():
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if config.warmup < 0 or config.duration <= 0:
        raise ValueError("warmup must be >= 0 and duration must be > 0")
    if config.virtual_seconds is not None and config.virtual_seconds <= 0:
        raise ValueError("virtual seconds must be > 0")
    if config.virtual_seconds is not None:
        virtual_target_ticks(config.virtual_seconds)
        virtual_timeout = config.virtual_seconds * 4.0 + 10.0
        if not math.isfinite(virtual_timeout):
            raise ValueError("virtual seconds produce a non-finite host timeout")
    if (
        config.runs < 1
        or not 0 <= config.ready_tick <= UINT32_MASK
        or config.poll_interval <= 0
    ):
        raise ValueError("runs/ready-tick/poll-interval have invalid values")
    if config.startup_timeout <= 0:
        raise ValueError("startup timeout must be positive")
    if config.min_rtf is not None and config.min_rtf < 0:
        raise ValueError("minimum RTF must be non-negative")
    if config.iwdg_boot_grace_ms is not None and config.iwdg_boot_grace_ms < 0:
        raise ValueError("iwdg boot grace must be non-negative")
    if config.tcg_tb_size is not None and config.tcg_tb_size < 1:
        raise ValueError("TCG TB size must be positive")
    if config.tcg_thread not in (None, "single", "multi"):
        raise ValueError("TCG thread must be single or multi")


def run(config: RunConfig) -> list[dict[str, Any]]:
    validate_config(config)
    results = [run_one(config, number) for number in range(1, config.runs + 1)]
    if config.virtual_seconds is not None and config.runs > 1:
        def summary(key: str) -> str:
            data = [item[key] for item in results if item[key] is not None]
            if not data:
                return "n/a"
            return "min=%.6f mean=%.6f max=%.6f" % (
                min(data), sum(data) / len(data), max(data)
            )

        print(
            "RESULT: QEMU firmware %.3f-virtual-second RTF gate passed"
            % config.virtual_seconds
        )
        print("  runs: %d" % config.runs)
        print("  startup latency: %s s" % summary("startup_latency"))
        print("  realtime factor: %s" % summary("rtf"))
        print("  CPU utilization: %s%%" % summary("cpu"))
        print("  RSS at sample start: %s KiB" % summary("rss"))
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--elf", required=True)
    parser.add_argument("--tick-address", required=True)
    parser.add_argument("--warmup", type=float, default=0.25)
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--virtual-seconds", type=float)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--min-rtf", type=float)
    parser.add_argument("--ready-tick", type=int, default=1)
    parser.add_argument("--poll-interval", type=float, default=0.250)
    parser.add_argument("--startup-timeout", type=float, default=10.0)
    parser.add_argument("--iwdg-boot-grace-ms", type=int)
    parser.add_argument("--adc-accurate-timing", action="store_true")
    parser.add_argument("--tcg-tb-size", type=int)
    parser.add_argument("--tcg-thread", choices=("single", "multi"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    config = RunConfig(**vars(_parser().parse_args(argv)))
    try:
        run(config)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
