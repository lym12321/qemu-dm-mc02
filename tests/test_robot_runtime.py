"""Robot lifecycle consumers; no QEMU, solver or protocol model is mocked."""

from contextlib import contextmanager
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.robots import runtime
from sim.robots.manifest import load_manifest
from tools import dm_mc02_firmware_rtf as rtf
from tools.dm_mc02_test_harness import stop
from test_harness_lifecycle import identity
from test_lab_runtime import snapshot
from test_robot_manifest import DOCUMENT, executable, write_manifest


class Pipe:
    """An actual pipe with observable close acquisition/cleanup failures."""

    def __init__(self, environment, label):
        self.environment, self.label = environment, label
        reader, self.writer = os.pipe()
        self.file = os.fdopen(reader, "rb", buffering=0)

    def fileno(self):
        return self.file.fileno()

    def close(self):
        self.environment.events.append(self.label + " close")
        self.file.close()
        if self.label in self.environment.close_failures:
            raise OSError(self.label + " close failure")


class Environment:
    """Only replace external process/QMP/socket acquisition, with literal states."""

    def __init__(self, tmp_path, monkeypatch):
        self._monkeypatch = monkeypatch
        self._real_popen = subprocess.Popen
        self.directory = tmp_path
        self.binary = executable(tmp_path / "qemu")
        self.clock = 100.0
        self.events, self.processes, self.launches, self.waits = [], [], [], []
        self.close_failures = set()
        self.qemu_status = None
        self.states = iter((100, 2_000_100, 3_000_100))
        self.last_stamp = 100
        self.uart_chunks = []
        self.owner_delay = self.spawn_delay = 0
        self.popen_failure = None
        self.pause_callback = lambda: None
        self.stop_failure = False
        self.owner_failure = None
        self.real_upper = None
        self.qmp = SimpleNamespace(command=self.command, settimeout=lambda seconds: None,
                                   close=lambda: self.close("qmp"))
        self.uart = SimpleNamespace(recv=self.recv, send=lambda data: len(data),
                                    close=lambda: self.close("uart"))
        monkeypatch.setattr(runtime, "running_qemu", self.owner)
        monkeypatch.setattr(runtime, "QmpSession", self.connect_qmp)
        monkeypatch.setattr(runtime, "connect_unix", self.connect_uart)
        monkeypatch.setattr(runtime.subprocess, "Popen", self.popen)
        monkeypatch.setattr(runtime, "stop", self.stop)
        # Keep the existing deadline wrapper: only its clock and QMP peer are isolated.
        clock = SimpleNamespace(monotonic=lambda: self.clock)
        monkeypatch.setattr(runtime, "time", clock)
        monkeypatch.setattr(rtf, "time", clock)
        monkeypatch.setattr(runtime, "select", SimpleNamespace(select=self.select))

    @contextmanager
    def owner(self, command, *, sockets, startup_timeout):
        self.events.append("owner start")
        self.owner_command, self.sockets = command, sockets
        started = self.clock
        self.clock += self.owner_delay
        process = SimpleNamespace(poll=lambda: self.qemu_status, returncode=self.qemu_status)
        try:
            yield SimpleNamespace(start_ns=round(started * 1e9), process=process,
                                  sockets={name: self.directory / name for name in sockets})
        finally:
            self.events.append("owner cleanup")
            if self.owner_failure is not None:
                raise self.owner_failure

    def connect_qmp(self, *args, **kwargs):
        self.events.append("qmp connect")
        return self.qmp

    def connect_uart(self, path, timeout):
        self.events.append("uart connect")
        assert Path(path).name.startswith("uart")
        return self.uart

    def command(self, name, arguments=None):
        if name == "qom-get":
            self.last_stamp = next(self.states, self.last_stamp)
            return snapshot(self.last_stamp)
        self.events.append(name)
        if name == "stop":
            self.pause_callback()

    def recv(self, maximum):
        assert maximum == 4096
        if self.uart_chunks:
            return self.uart_chunks.pop(0)
        raise BlockingIOError()

    def select(self, readers, writable, exceptional, timeout):
        self.waits.append(timeout)
        self.clock += timeout
        return [], [], []

    def popen(self, argv, **kwargs):
        label = "worker" if not self.processes else "upper"
        self.launches.append((list(argv), kwargs))
        if label == self.popen_failure:
            raise OSError(label + " spawn failure")
        if label == "upper" and self.real_upper is not None:
            process = self.real_upper(argv, **kwargs)
        else:
            process = SimpleNamespace(pid=1000 + len(self.processes), returncode=None)
            process.stdout = Pipe(self, label + " stdout")
            process.stderr = Pipe(self, label + " stderr")
            process.poll = lambda: process.returncode
        self.processes.append(process)
        self.clock += self.spawn_delay
        return process

    def stop(self, process):
        self.events.append("child stop")
        process.returncode = -signal.SIGTERM
        if self.stop_failure:
            raise OSError("stop failure")

    def close(self, label):
        self.events.append(label + " close")
        if label in self.close_failures:
            raise OSError(label + " close failure")

    def manifest(self, *, ready=False, upper=False):
        text = DOCUMENT
        if ready:
            text += "ready: {endpoint: main.uart10, text: READY}\n"
        if upper:
            text += yaml.safe_dump({"upper_computers": {
                "vision": {"command": [sys.executable, "literal;$HOME", "$(touch unwanted)"]}}})
        return load_manifest(write_manifest(self.directory, text))

    def run(self, manifest=None, **options):
        return runtime.run_manifest(manifest or self.manifest(), qemu=self.binary,
                                    duration=.001, **options)

    def release(self):
        for process in self.processes:
            for name in ("stdout", "stderr"):
                pipe = getattr(process, name)
                if isinstance(pipe, Pipe):
                    pipe.file.close()
                    os.close(pipe.writer)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    value = Environment(tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.release()


@pytest.mark.parametrize("options", [
    {"duration": float("nan")}, {"duration": True}, {"duration": 0},
    {"duration": 1e-10}, {"duration": 3601}, {"timeout": float("inf")},
    {"startup_timeout": 3, "timeout": 2}, {"gdb_port": True}, {"gdb_port": 65536},
    {"emit": 1},
])
def test_invalid_runtime_configuration_has_no_owner(environment, options):
    manifest = environment.manifest()
    arguments = dict(qemu=environment.binary, duration=.001)
    arguments.update(options)
    with pytest.raises((ValueError, TypeError)):
        runtime.run_manifest(manifest, **arguments)
    assert environment.events == []
    assert environment.launches == []


@pytest.mark.parametrize("delay", ["owner_delay", "spawn_delay"])
def test_first_qemu_start_budget_covers_owner_and_child_spawn(environment, delay):
    setattr(environment, delay, 2)
    with pytest.raises(TimeoutError, match="startup deadline"):
        environment.run(startup_timeout=1, timeout=3)
    assert environment.events[-1] == "owner cleanup"
    if delay == "owner_delay":
        assert environment.launches == []
    else:
        assert "child stop" in environment.events
        assert environment.processes[0].stdout.file.closed
        assert environment.processes[0].stderr.file.closed


@pytest.mark.parametrize("failure", ["popen", "prepare"])
def test_partial_child_acquisition_releases_every_owned_resource(environment, monkeypatch, failure):
    if failure == "popen":
        environment.popen_failure = "upper"
    else:
        real_prepare = runtime._Child.prepare
        def prepare(child):
            real_prepare(child)
            if child.label.startswith("upper:"):
                raise OSError("pipe configuration failure")
        monkeypatch.setattr(runtime._Child, "prepare", prepare)
    with pytest.raises(OSError, match="spawn failure|pipe configuration failure"):
        environment.run(environment.manifest(upper=True))
    assert environment.events.count("child stop") == (1 if failure == "popen" else 2)
    assert all(p.stdout.file.closed and p.stderr.file.closed for p in environment.processes)
    assert environment.events[-3:] == ["uart close", "qmp close", "owner cleanup"]


def test_real_pipe_reads_are_bounded_keep_binary_tail_and_remove_eof(tmp_path, monkeypatch):
    pipes, writers = [], []
    for _ in range(2):
        reader, writer = os.pipe()
        pipes.append(os.fdopen(reader, "rb", buffering=0))
        writers.append(writer)
    process = SimpleNamespace(stdout=pipes[0], stderr=pipes[1])
    child = runtime._Child("fixture", process)
    real_read, reads = os.read, []
    def read(fd, size):
        reads.append((fd, size))
        return real_read(fd, size)
    monkeypatch.setattr(runtime.os, "read", read)
    try:
        child.prepare()
        for data in (b"a" * 4096, b"b" * 4096, b"c" * 4096, b"\xff" + b"d" * 4095):
            for writer in writers:
                os.write(writer, data)
            before = len(reads)
            child.pump()
            assert len(reads) - before == 2
        expected = b"b" * 3808 + b"c" * 4096 + b"\xff" + b"d" * 4095
        assert child.tails == {"stdout": expected, "stderr": expected}
        assert "\ufffd" in child.diagnostics()["stderr"]
        assert all(size == 4096 for _, size in reads)
        for writer in writers:
            os.close(writer)
        writers.clear()
        child.pump()
        assert child.readers() == []
        before = len(reads)
        child.pump()
        assert len(reads) == before
    finally:
        for writer in writers:
            os.close(writer)
        for pipe in pipes:
            pipe.close()


@pytest.mark.parametrize("label,code,during_pause", [
    ("worker", 0, False), ("upper", 7, False),
    ("worker", 7, True), ("upper", 0, True),
])
def test_early_exit_including_final_pause_cannot_return_success(
        environment, label, code, during_pause):
    if during_pause:
        environment.pause_callback = lambda: setattr(environment.processes[0 if label == "worker" else 1],
                                                       "returncode", code)
    else:
        original = environment.popen
        def popen(*args, **kwargs):
            process = original(*args, **kwargs)
            if len(environment.processes) == (1 if label == "worker" else 2):
                process.returncode = code
            return process
        environment.popen = popen
        # The monkeypatch refers to the old bound method until replaced explicitly.
        environment._monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match=f"{label}.*exited early \\({code}\\)"):
        environment.run(environment.manifest(upper=True))
    assert environment.events[-1] == "owner cleanup"
    assert environment.events.count("child stop") == 2


def test_marker_across_reads_sets_post_read_native_origin(environment):
    environment.states = iter((100, 1_000_100, 1_100_100, 2_000_100, 2_100_100,
                               3_000_100, 3_100_100))
    environment.uart_chunks = [b"REA", b"DY"]
    emitted = []
    result = environment.run(environment.manifest(ready=True), emit=emitted.append)
    assert result.ready == "uart_marker"
    assert (result.origin_ns, result.end_ns) == (2_100_100, 3_100_100)
    assert [event.data for event in emitted] == [b"REA", b"DY"]
    assert environment.sockets == ("qmp", "cosim", "can1", "uart10")


def test_no_ready_is_explicit_and_result_follows_owner_cleanup(environment):
    result = environment.run(environment.manifest(upper=True))
    environment.events.append("returned")
    assert result.ready == "not_waited"
    assert (result.origin_ns, result.end_ns) == (100, 3_000_100)
    assert environment.events[-2:] == ["owner cleanup", "returned"]
    assert [child["label"] for child in result.children] == ["worker", "upper:vision"]
    worker, upper = environment.launches
    assert worker[0][worker[0].index("--protocol") + 1] == "v2"
    assert worker[0][worker[0].index("--frames") + 1] == "0"
    assert worker[0][worker[0].index("--rate") + 1] == "1000"
    assert "--realtime" in worker[0]
    assert upper[0][1:] == ["literal;$HOME", "$(touch unwanted)"]
    assert all(options["shell"] is False and options["start_new_session"] is True and
               options["cwd"] == environment.directory for _, options in environment.launches)


def test_virtual_time_stall_ends_at_host_deadline_with_bounded_wait(environment):
    environment.states = iter((100,))
    with pytest.raises(TimeoutError, match="host deadline"):
        environment.run(startup_timeout=.005, timeout=.01)
    assert environment.waits and all(0 < value <= .005 for value in environment.waits)
    assert environment.last_stamp == 100
    assert environment.events[-1] == "owner cleanup"


@pytest.mark.parametrize("primary", [None, KeyboardInterrupt("sink interrupted")])
def test_cleanup_attempts_every_resource_and_keeps_primary(environment, primary):
    environment.stop_failure = True
    environment.close_failures = {"worker stdout", "upper stderr", "uart", "qmp"}
    environment.uart_chunks = [b"observation"]
    def emit(event):
        if primary is not None:
            raise primary
    with pytest.raises(KeyboardInterrupt if primary is not None else OSError) as caught:
        environment.run(environment.manifest(upper=True), emit=emit)
    if primary is not None:
        assert caught.value is primary
    notes = "\n".join(caught.value.__notes__)
    for label in ("worker stop", "upper:vision stop", "worker stdout", "upper:vision stderr", "uart1", "QMP"):
        assert f"Robot cleanup {label} failed" in notes
    assert environment.events.count("child stop") == 2
    assert environment.events[-3:] == ["uart close", "qmp close", "owner cleanup"]


def test_harness_cleanup_failure_prevents_success(environment):
    error = OSError("owner cleanup failure")
    environment.owner_failure = error
    with pytest.raises(OSError) as caught:
        environment.run()
    assert caught.value is error
    assert environment.events[-1] == "owner cleanup"


def test_runtime_reaps_real_term_resistant_owned_group(environment, monkeypatch):
    # Capture the original constructor, since the environment isolates acquisition.
    real_popen = environment._real_popen
    acquired = []
    script = ("import os,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
              "os.write(1,b'ready');signal.pause()")
    def upper(argv, **options):
        process = real_popen([sys.executable, "-u", "-c", script], **options)
        acquired.append((process, identity(process.pid)))
        assert select.select([process.stdout], [], [], 2)[0]
        assert os.read(process.stdout.fileno(), 5) == b"ready"
        return process
    environment.real_upper = upper
    fake_stop = environment.stop
    def stop_child(process):
        if isinstance(process, environment._real_popen):
            environment.events.append("real group stop")
            stop(process)
        else:
            fake_stop(process)
    monkeypatch.setattr(runtime, "stop", stop_child)
    try:
        result = environment.run(environment.manifest(upper=True))
        assert result.ready == "not_waited"
        process, original = acquired[0]
        assert original is not None and identity(process.pid) != original
        assert process.returncode == -signal.SIGKILL
        assert process.stdout.closed and process.stderr.closed
        assert environment.events.index("real group stop") < environment.events.index("owner cleanup")
    finally:
        for process, _ in acquired:
            if process.poll() is None:
                stop(process)
