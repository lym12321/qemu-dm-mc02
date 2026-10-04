"""Process/socket ownership checks without firmware or a QEMU dependency."""

from __future__ import annotations

from io import BytesIO, StringIO
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import dm_mc02_test_harness as harness


# The Unix server is only an external process consumer, not a QEMU emulator.
SERVER = """
import signal, socket, sys
if len(sys.argv) > 2:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(sys.argv[1])
server.listen()
sys.stderr.write('x' * 12500 + 'TAIL\\n')
sys.stderr.flush()
while True:
    client, _ = server.accept()
    client.sendall(b'ready')
    client.close()
"""


def command(script=SERVER, *args):
    return [sys.executable, "-u", "-c", script, "{qmp}", *args]


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    except FileNotFoundError:
        return None
    return pid, fields[19]  # Linux stat field 22, after pid/comm.


def connect(owner):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1.0)
        client.connect(str(owner.sockets["qmp"]))
        assert client.recv(5) == b"ready"


@pytest.mark.parametrize("options", [
    dict(command=[]),
    dict(sockets=["../bad"]),
    dict(wait_sockets=["other"]),
    dict(startup_timeout=float("nan")),
])
def test_invalid_admission_has_no_process_or_temporary_directory(monkeypatch, options):
    temporary = Mock()
    popen = Mock()
    monkeypatch.setattr(harness.tempfile, "TemporaryDirectory", temporary)
    monkeypatch.setattr(harness.subprocess, "Popen", popen)
    arguments = dict(command=command(), sockets=["qmp"])
    arguments.update(options)
    with pytest.raises(ValueError):
        with harness.running_qemu(**arguments):
            pytest.fail("invalid inputs were admitted")
    temporary.assert_not_called()
    popen.assert_not_called()


def test_owner_expands_private_paths_and_reaps_real_process():
    manager = harness.running_qemu(
        command(), sockets=["qmp", "uart"], wait_sockets=["qmp"])
    with manager as owner:
        connect(owner)
        original = identity(owner.process.pid)
        assert original is not None
        temporary = owner.stderr_path.parent
        assert temporary.parent == Path("/tmp")
        assert len(str(owner.sockets["qmp"])) < 108
        assert not owner.sockets["uart"].exists()
        assert owner.expand("{qmp}:{qemu_pid}:{qemu_start_ns}") == (
            f"{owner.sockets['qmp']}:{owner.process.pid}:{owner.start_ns}")
    assert owner.process.poll() is not None
    assert identity(owner.process.pid) != original
    assert not temporary.exists()


@pytest.mark.parametrize("failure", [RuntimeError("consumer failed"), KeyboardInterrupt()])
def test_consumer_primary_and_bounded_stderr_survive_cleanup(failure):
    with pytest.raises(type(failure)) as caught:
        with harness.running_qemu(command(), sockets=["qmp"]) as owner:
            connect(owner)
            original = identity(owner.process.pid)
            raise failure
    assert caught.value is failure
    note, = failure.__notes__
    assert note.endswith("TAIL\n")
    assert len(note.removeprefix("QEMU stderr tail:\n").encode()) == 12000
    assert owner.process.poll() is not None
    assert identity(owner.process.pid) != original
    assert not owner.stderr_path.parent.exists()


def test_popen_failure_preserves_exception_and_removes_empty_stderr(monkeypatch):
    real_temporary = harness.tempfile.TemporaryDirectory
    directories = []

    def temporary(**kwargs):
        result = real_temporary(**kwargs)
        directories.append(Path(result.name))
        return result

    failure = OSError("executable unavailable")
    monkeypatch.setattr(harness.tempfile, "TemporaryDirectory", temporary)
    monkeypatch.setattr(harness.subprocess, "Popen", Mock(side_effect=failure))
    stop = Mock()
    monkeypatch.setattr(harness, "stop", stop)
    with pytest.raises(OSError) as caught:
        with harness.running_qemu(command(), sockets=["qmp"]):
            pytest.fail("Popen failure reached consumer")
    assert caught.value is failure
    assert not getattr(failure, "__notes__", ())
    assert len(directories) == 1 and not directories[0].exists()
    stop.assert_not_called()


def test_popen_time_is_inside_startup_deadline(monkeypatch):
    process = Mock(pid=1234)
    process.poll.return_value = None
    popen = Mock(return_value=process)
    stop = Mock()
    monkeypatch.setattr(harness.subprocess, "Popen", popen)
    monkeypatch.setattr(harness, "stop", stop)
    clock = Mock(side_effect=[0, 1_000_000_001])
    monkeypatch.setattr(harness.time, "monotonic_ns", clock)
    with pytest.raises(RuntimeError, match="startup deadline"):
        with harness.running_qemu(command(), sockets=["qmp"],
                                  wait_sockets=[], startup_timeout=1.0):
            pytest.fail("late process creation reached consumer")
    popen.assert_called_once()
    stop.assert_called_once_with(process)


@pytest.mark.parametrize("script, message", [
    ("import sys; print('startup failure', file=sys.stderr); sys.exit(7)",
     "exited during startup \\(7\\)"),
    ("import pathlib, signal, sys; pathlib.Path(sys.argv[1]).touch(); signal.pause()",
     "startup deadline"),
])
def test_startup_failure_reaps_and_removes_temporary_directory(monkeypatch, script, message):
    real_popen = harness.subprocess.Popen
    acquired = []

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        acquired.append((process, Path(kwargs["stderr"].name).parent))
        return process

    monkeypatch.setattr(harness.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match=message):
        with harness.running_qemu(command(script), sockets=["qmp"],
                                  startup_timeout=0.3):
            pytest.fail("startup failure reached consumer")
    process, temporary = acquired[0]
    assert process.poll() is not None
    assert not temporary.exists()


def test_ignored_term_uses_kill_and_reaps_real_process():
    with harness.running_qemu(command(SERVER, "ignore-term"), sockets=["qmp"]) as owner:
        connect(owner)
        original = identity(owner.process.pid)
    assert owner.process.returncode == -signal.SIGKILL
    assert identity(owner.process.pid) != original
    assert not owner.stderr_path.parent.exists()


@pytest.mark.parametrize("failure", [None, RuntimeError("consumer failed"), KeyboardInterrupt()])
def test_cleanup_failure_is_reported_without_replacing_primary(monkeypatch, failure):
    real_stop = harness.stop
    real_temporary = harness.tempfile.TemporaryDirectory
    attempts = []

    def stop(process):
        real_stop(process)
        attempts.append("stop")
        raise OSError("stop diagnostic failure")

    def temporary(**kwargs):
        result = real_temporary(**kwargs)
        cleanup = result.cleanup

        def failed_cleanup():
            cleanup()
            attempts.append("temporary")
            raise OSError("directory diagnostic failure")

        result.cleanup = failed_cleanup
        return result

    monkeypatch.setattr(harness, "stop", stop)
    monkeypatch.setattr(harness.tempfile, "TemporaryDirectory", temporary)
    expected = type(failure) if failure is not None else RuntimeError
    with pytest.raises(expected) as caught:
        with harness.running_qemu(command(), sockets=["qmp"]) as owner:
            connect(owner)
            if failure is not None:
                raise failure
    if failure is not None:
        assert caught.value is failure
        message = "\n".join(failure.__notes__)
    else:
        message = str(caught.value)
    assert "stop diagnostic failure" in message
    assert "directory diagnostic failure" in message
    assert attempts == ["stop", "temporary"]
    assert owner.process.poll() is not None
    assert not owner.stderr_path.parent.exists()


def test_cleanup_interrupt_retains_original_object_and_remaining_cleanup(monkeypatch):
    real_stop = harness.stop
    failure = KeyboardInterrupt()

    def stop(process):
        real_stop(process)
        raise failure

    monkeypatch.setattr(harness, "stop", stop)
    with pytest.raises(KeyboardInterrupt) as caught:
        with harness.running_qemu(command(), sockets=["qmp"]) as owner:
            connect(owner)
    assert caught.value is failure
    assert any("process stop" in note for note in failure.__notes__)
    assert not owner.stderr_path.parent.exists()


def test_repeated_context_exit_does_not_stop_again(monkeypatch):
    process = Mock(pid=1234)
    process.poll.return_value = None
    monkeypatch.setattr(harness.subprocess, "Popen", Mock(return_value=process))
    stop = Mock()
    monkeypatch.setattr(harness, "stop", stop)
    manager = harness.running_qemu(command(), sockets=["qmp"], wait_sockets=[])
    manager.__enter__()
    manager.__exit__(None, None, None)
    manager.__exit__(None, None, None)
    stop.assert_called_once_with(process)


def test_main_uses_owner_and_cleans_test_group_before_qemu(monkeypatch):
    # This is the existing CLI's direct consumer of the reusable process owner.
    from contextlib import contextmanager

    order = []
    process = Mock(pid=1234)
    owner = harness.RunningQemu(process, 123, {"qmp": Path("/tmp/qmp")}, Path("/tmp/err"))

    @contextmanager
    def running(*args, **kwargs):
        order.append("qemu start")
        try:
            yield owner
        finally:
            order.append("qemu stop")

    child = Mock(returncode=0)
    monkeypatch.setattr(harness, "running_qemu", running)
    monkeypatch.setattr(harness.subprocess, "Popen", Mock(return_value=child))
    monkeypatch.setattr(harness, "stop", lambda process: order.append("test stop"))
    monkeypatch.setattr(harness.sys, "argv", ["harness", "--socket", "qmp", "--", "qemu"])
    monkeypatch.setattr(harness.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"pass\n")))
    monkeypatch.setattr(harness.sys, "stderr", StringIO())
    assert harness.main() == 0
    assert order == ["qemu start", "test stop", "qemu stop"]
    child.communicate.assert_called_once_with(input=b"pass\n", timeout=20.0)
