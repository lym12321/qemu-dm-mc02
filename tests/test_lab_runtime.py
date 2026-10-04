"""Failure/resource and scheduling consumers of the Lab runtime adapter."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.lab import runtime
from sim.lab.manifest import load_manifest


def manifest(tmp_path, body=""):
    (tmp_path / "guest.elf").write_bytes(b"fixture")
    path = tmp_path / "lab.yaml"
    path.write_text("schema: 1\nname: fixture\nboard: dm-mc02\nfirmware: guest.elf\nduration: 10ms\n" + body)
    return load_manifest(path)


def snapshot(time_ns, level=0):
    return {"schema": 1, "timestamp_ns": time_ns, "board": "dm-mc02", "vin_mv": 24000,
            "gpio": {port: {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0}
                     for port in "ABCDEFGH"},
            "key": {"pin": "A15", "pressed": False, "level": 1},
            "led": {"pin": "A7", "rgb": 0xffffff if level else 0,
                    "brightness": 255 if level else 0, "source": "gpio-latch"}}


@pytest.mark.parametrize("options", [
    {"timeout": float("inf")}, {"startup_timeout": float("nan")}, {"timeout": True},
    {"startup_timeout": 3, "timeout": 2}, {"uarts": ("uart4",)},
    {"uarts": ("uart1", "uart1")}, {"cans": ("can4",)}, {"adc": 1},
    {"gdb_port": True}, {"gdb_port": 65536},
])
def test_runtime_admission_precedes_process(tmp_path, monkeypatch, options):
    elf = tmp_path / "guest.elf"
    elf.write_bytes(b"fixture")
    binary = tmp_path / "qemu"
    binary.write_text("fixture")
    binary.chmod(0o755)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid configuration created process resources")

    monkeypatch.setattr(runtime, "running_qemu", forbidden)
    with pytest.raises(ValueError):
        with runtime.open_lab(elf, qemu=binary, **options):
            pytest.fail("invalid runtime yielded")


def test_second_connection_failure_closes_first_and_qmp(tmp_path, monkeypatch):
    binary = tmp_path / "qemu"
    binary.write_text("fixture")
    binary.chmod(0o755)
    elf = tmp_path / "guest.elf"
    elf.write_bytes(b"fixture")
    closed = []

    @contextmanager
    def owner(*args, **kwargs):
        try:
            yield SimpleNamespace(start_ns=runtime.time.monotonic_ns(),
                                  sockets={name: tmp_path / name for name in kwargs["sockets"]})
        finally:
            closed.append("owner")

    qmp = SimpleNamespace(close=lambda: closed.append("qmp"))
    sock = SimpleNamespace(close=lambda: closed.append("uart1"))
    def connect(path, timeout):
        if path.endswith("uart2"):
            raise OSError("second peer failed")
        return sock

    monkeypatch.setattr(runtime, "running_qemu", owner)
    monkeypatch.setattr(runtime, "QmpSession", lambda *args, **kwargs: qmp)
    monkeypatch.setattr(runtime, "connect_unix", connect)
    # First wrapper is deliberately minimal: this test stops before channel
    # admission and isolates acquisition, rather than duplicating codecs.
    monkeypatch.setattr(runtime, "StreamTransport", lambda sock: sock)
    with pytest.raises(OSError, match="second peer failed"):
        with runtime.open_lab(elf, qemu=binary, uarts=("uart1", "uart2")):
            pytest.fail("partial connection yielded")
    assert closed == ["uart1", "qmp", "owner"]


def test_total_deadline_and_early_exit_are_explicit():
    owner = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
    lab = runtime.ConnectedLab(owner, SimpleNamespace(command=lambda *args: None), [], None,
                               runtime.time.monotonic() - 1, runtime.time.monotonic() - 1)
    with pytest.raises(TimeoutError, match="host deadline"):
        lab.remaining()
    with pytest.raises(TimeoutError, match="startup deadline"):
        lab.remaining(startup=True)
    owner.process = SimpleNamespace(poll=lambda: 7, returncode=7)
    with pytest.raises(RuntimeError, match="QEMU exited.*7"):
        lab.remaining()


@pytest.mark.parametrize("interrupt", [False, True])
def test_peer_cleanup_attempts_all_and_preserves_interrupt(tmp_path, monkeypatch, interrupt):
    binary = tmp_path / "qemu"
    binary.write_text("fixture")
    binary.chmod(0o755)
    elf = tmp_path / "guest.elf"
    elf.write_bytes(b"fixture")
    closed = []

    @contextmanager
    def owner(*args, **kwargs):
        try:
            yield SimpleNamespace(start_ns=runtime.time.monotonic_ns(),
                                  sockets={name: tmp_path / name for name in kwargs["sockets"]},
                                  process=SimpleNamespace(poll=lambda: None))
        finally:
            closed.append("owner")

    def fail_close(name):
        closed.append(name)
        raise OSError(f"{name} close failed")

    qmp = SimpleNamespace(command=lambda *args: snapshot(0), settimeout=lambda seconds: None,
                          close=lambda: fail_close("qmp"))
    sock = SimpleNamespace(close=lambda: fail_close("uart1"))
    monkeypatch.setattr(runtime, "running_qemu", owner)
    monkeypatch.setattr(runtime, "QmpSession", lambda *args, **kwargs: qmp)
    monkeypatch.setattr(runtime, "connect_unix", lambda *args: sock)
    expected = KeyboardInterrupt if interrupt else OSError
    with pytest.raises(expected) as failure:
        with runtime.open_lab(elf, qemu=binary, uarts=("uart1",)):
            if interrupt:
                raise KeyboardInterrupt("consumer interrupted")
    assert closed == ["uart1", "qmp", "owner"]
    assert "Lab cleanup uart1 failed" in "\n".join(failure.value.__notes__)
    assert "Lab cleanup QMP failed" in "\n".join(failure.value.__notes__)


def test_run_manifest_returns_after_owner_cleanup_and_retains_dispatch_time(tmp_path, monkeypatch):
    spec = manifest(tmp_path, "stimulus:\n  - at: 2ms\n    endpoint: key\n    pressed: true\nassertions:\n  - before: 8ms\n    digital_equals: {endpoint: led, value: 1}\n")
    calls = []
    states = iter((snapshot(100), snapshot(3_000_100), snapshot(7_000_100, 1),
                   snapshot(10_000_100, 1), snapshot(10_500_100, 1)))
    lab = SimpleNamespace(snapshot=lambda **kwargs: snapshot(100),
                          pump=lambda **kwargs: (next(states), []),
                          board=SimpleNamespace(resume=lambda: calls.append("cont"),
                                                pause=lambda: calls.append("stop")),
                          call=lambda callback, **kwargs: callback(),
                          inject=lambda stimulus: calls.append(stimulus.endpoint) or 3_500_100,
                          wait=lambda **kwargs: None)

    @contextmanager
    def open_lab(*args, **kwargs):
        try:
            yield lab
        finally:
            calls.append("cleanup")

    monkeypatch.setattr(runtime, "open_lab", open_lab)
    result = runtime.run_manifest(spec)
    assert calls == ["cont", "key", "stop", "cleanup"]
    assert result.passed and result.status == ("pass",)
    assert result.dispatched == ((2_000_000, 3_500_100, "key"),)
    assert result.end_ns == 10_500_100


def test_adc_waits_for_genuine_native_time_not_fabricated_increment():
    stamps = iter((10, 10, 12))
    sent = []
    lab = runtime.ConnectedLab(SimpleNamespace(), SimpleNamespace(command=lambda *args: None), [],
                               SimpleNamespace(send_adc=lambda channel, **kwargs:
                                               sent.append((channel, kwargs))), 0, 0)
    lab._last_adc_ns = 10
    lab.snapshot = lambda: {"timestamp_ns": next(stamps)}
    waits = []
    lab.wait = lambda: waits.append(True)
    from sim.lab.manifest import Stimulus
    assert lab.inject(Stimulus(0, "adc1.ch4", "adc_raw", 12345)) == 12
    assert waits == [True, True]
    assert sent == [(4, {"timestamp_ns": 12, "raw": 12345})]


def test_interruption_in_output_sink_still_releases_owner(tmp_path, monkeypatch):
    spec = manifest(tmp_path)
    released = []
    lab = SimpleNamespace(snapshot=lambda **kwargs: snapshot(0),
                          call=lambda callback, **kwargs: callback(),
                          board=SimpleNamespace(resume=lambda: None),
                          pump=lambda **kwargs: (snapshot(1), [object()]))
    @contextmanager
    def owner(*args, **kwargs):
        try:
            yield lab
        finally:
            released.append(True)
    monkeypatch.setattr(runtime, "open_lab", owner)
    def interrupted(event):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        runtime.run_manifest(spec, emit=interrupted)
    assert released == [True]
