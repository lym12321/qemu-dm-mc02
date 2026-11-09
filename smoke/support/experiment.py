"""Public Rust scene CLI plus independent MCAP observations for test drivers."""

import json
from pathlib import Path
import time
from types import SimpleNamespace

from smoke.support.native_cli import NativeCommand, resolve_sim


class Events(list):
    def __init__(self, maximum=65536):
        super().__init__()
        self.maximum = maximum

    @property
    def total(self):
        return len(self)

    def __call__(self, event):
        if len(self) >= self.maximum:
            raise BufferError("native experiment observation capacity exhausted")
        self.append(event)


def execute_scene(path, *, qemu, sim=None, timeout=30, allow_host_processes=False, measure=False, observations=None):
    # Read full events from the existing record format; stdout intentionally
    # omits snapshots. Python owns only this external test process and oracle.
    from smoke.simulation_smoke import _read_observations

    path = Path(path).resolve()
    trace, report = path.with_suffix(".mcap"), path.with_suffix(".result.json")
    command = {}
    argv = [str(resolve_sim(sim)), "run", str(path), "--qemu", str(qemu),
            "--startup-timeout", str(min(10, timeout)), "--timeout", str(timeout),
            "--trace", str(trace), "--report", str(report)]
    if allow_host_processes:
        argv.append("--allow-host-processes")
    consumer = NativeCommand(argv, time.monotonic() + timeout + 5, command, measure=measure)
    primary = None
    try:
        consumer.finish()
        payload = json.loads(report.read_text())
    except BaseException as error:
        primary = error
        raise
    finally:
        consumer.close(primary)
    if "error" in payload:
        error = RuntimeError(f"Rust scene CLI failed: {payload}")
        error.native_observations = {"command": command}
        raise error
    events = Events() if observations is None else observations
    try:
        _read_observations(trace, events)
    except BaseException as error:
        error.native_observations = {"command": command}
        raise
    result = SimpleNamespace(**payload["result"], passed=payload["status"] == "passed")
    return events, result, command
