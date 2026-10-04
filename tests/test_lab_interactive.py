"""Borrowed real-pipe console IO with isolated, already verified Lab ownership."""

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.boards.channels import ChannelEvent
from sim.fabric import CanFrame
from sim.lab import interactive


@contextmanager
def pipe(data=b"", *, eof=True):
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    try:
        if data:
            os.write(write_fd, data)
        if eof:
            os.close(write_fd)
            write_fd = None
        yield reader, write_fd
    finally:
        reader.close()
        if write_fd is not None:
            os.close(write_fd)


class Lab:
    """Mock dependencies only; this fixture does not decode transport bytes."""

    def __init__(self, *, events=None, running=True):
        self.calls = []
        self.peers = []
        self.batches = 0
        self.events = events or []
        self.hook = None
        self.failure = None
        self.running = running
        self.board = SimpleNamespace(
            resume=lambda: self.calls.append("cont"),
            pause=lambda: self.calls.append("pause"),
            set_vin=lambda value: self.calls.append(("vin", value)))
        self.qmp = SimpleNamespace(command=self.command)

    def command(self, name):
        assert name == "query-status"
        return {"running": self.running}

    def remaining(self):
        if self.failure:
            raise self.failure
        return 1.0

    def call(self, callback, **kwargs):
        return callback()

    def snapshot(self):
        return {"schema": 1, "timestamp_ns": self.batches, "board": "DM-MC02",
                "gpio": {"A": {"sampled": 3}}, "extension": ["native", 7]}

    def pump(self):
        self.batches += 1
        if self.hook:
            self.hook(self.batches)
        events, self.events = self.events, []
        return self.snapshot(), events

    def inject(self, stimulus):
        self.calls.append(stimulus)
        return self.batches


def owner(monkeypatch, lab, *, cleanup_error=None):
    entered = []

    class Context:
        def __enter__(self):
            entered.append("enter")
            return lab

        def __exit__(self, typ, primary, traceback):
            entered.append("cleanup")
            if cleanup_error is not None:
                if primary is not None:
                    primary.add_note(f"owner cleanup failed: {cleanup_error}")
                else:
                    raise cleanup_error
            return False

    def open_lab(*args, **kwargs):
        entered.append(kwargs)
        return Context()

    monkeypatch.setattr(interactive, "open_lab", open_lab)
    return entered


def test_commands_shlex_literal_and_native_state_without_shell_effect(monkeypatch):
    lab = Lab(events=[ChannelEvent("uart1", 1, b"\x1b[31m\r\n")])
    lifecycle = owner(monkeypatch, lab)
    output = io.StringIO()
    commands = (b"state\nkey press\nkey release\ngpio A0 1\nvin 24000\n"
                b' uart uart1 "hello world $(never)"\n'
                b"can can1 0x123 0 2 aabb\nadc ch4 raw 12345\n"
                b"adc ch19 voltage_uv 1650000\npause\ncont\nquit\n")
    with pipe(commands) as (reader, _):
        result = interactive.run_interactive(
            "fw.elf", cans=("can1",), adc=True, input_stream=reader,
            output_stream=output)
        assert reader.fileno() >= 0
    assert lifecycle[-1] == "cleanup"
    assert lifecycle[0]["timeout"] == 300.0
    assert result["commands"] == 12 and result["observation_batches"] >= 13
    assert lab.calls[0] == "cont"
    values = [item for item in lab.calls if not isinstance(item, (str, tuple))]
    assert [(item.endpoint, item.kind, item.value) for item in values[:3]] == [
        ("key", "key", True), ("key", "key", False), ("gpio.A0", "gpio", 1)]
    assert values[3].value == b"hello world $(never)"
    assert values[4].value == CanFrame(0x123, 0, 2, b"\xaa\xbb")
    assert [(item.endpoint, item.kind, item.value) for item in values[5:]] == [
        ("adc1.ch4", "adc_raw", 12345), ("adc1.ch19", "adc_voltage", 1650000)]
    assert ("vin", 24000) in lab.calls and lab.calls[-2:] == ["pause", "cont"]
    text = output.getvalue()
    assert "\\x1b[31m" in text and "\x1b" not in text
    native = next(json.loads(line) for line in text.splitlines() if line.startswith("{"))
    assert native["extension"] == ["native", 7] and native["gpio"]["A"]["sampled"] == 3


def test_partial_line_and_final_unterminated_eof_are_processed_once(monkeypatch):
    lab = Lab()
    lifecycle = owner(monkeypatch, lab)
    with pipe(b"gpio A", eof=False) as (reader, writer):
        def complete(batch):
            if batch == 2:
                os.write(writer, b"0 1\nquit\n")
        lab.hook = complete
        result = interactive.run_interactive(
            "fw", input_stream=reader, output_stream=io.StringIO())
    assert result["commands"] == 2 and lifecycle[-1] == "cleanup"
    inputs = [item for item in lab.calls if not isinstance(item, str)]
    assert len(inputs) == 1 and inputs[0].endpoint == "gpio.A0"
    lab = Lab()
    owner(monkeypatch, lab)
    with pipe(b"key press") as (reader, _):
        result = interactive.run_interactive(
            "fw", input_stream=reader, output_stream=io.StringIO())
    assert result["commands"] == 1
    assert lab.calls[-1].value is True


def test_long_partial_line_discarded_and_following_command_recovers(monkeypatch):
    lab = Lab()
    owner(monkeypatch, lab)
    output = io.StringIO()
    with pipe(b"x" * 4096, eof=False) as (reader, writer):
        def continue_line(batch):
            if batch == 2:
                os.write(writer, b"y" * 50)
            elif batch == 3:
                os.write(writer, b"bad suffix\nkey press\nquit\n")
        lab.hook = continue_line
        result = interactive.run_interactive("fw", input_stream=reader, output_stream=output)
    assert result["commands"] == 2
    assert output.getvalue().count("error:") == 1
    assert "exceeds 4096 bytes" in output.getvalue()
    assert lab.calls[-1].endpoint == "key"


@pytest.mark.parametrize("command", [
    b"reset", b"key maybe", b"gpio I0 1", b"gpio A0 2", b"vin -1",
    b"uart uart2 abc", b"can can2 1 0 0 ''", b"can can1 1 0 9 aa",
    b"can can1 1 0 1 zz", b"adc ch32 raw 1", b"adc ch1 raw 65536",
    b"adc ch1 voltage_uv 3300001", b"state extra", b"'unclosed", b"\xff",
])
def test_bad_commands_report_without_control_effects_and_continue(monkeypatch, command):
    lab = Lab()
    owner(monkeypatch, lab)
    output = io.StringIO()
    with pipe(command + b"\nquit\n") as (reader, _):
        result = interactive.run_interactive(
            "fw", cans=("can1",), adc=True, input_stream=reader, output_stream=output)
    assert lab.calls == ["cont"]
    assert result["commands"] == 1 and "error:" in output.getvalue()


def test_paused_native_gdb_and_adc_admission_without_shadow_state(monkeypatch):
    lab = Lab(running=False)
    lifecycle = owner(monkeypatch, lab)
    output = io.StringIO()
    with pipe(b"adc ch1 raw 3\nstate\nquit\n") as (reader, _):
        result = interactive.run_interactive(
            "fw", adc=True, paused=True, gdb_port=3333,
            input_stream=reader, output_stream=output)
    assert lab.calls == [] and lifecycle[-1] == "cleanup"
    assert result["commands"] == 2
    assert "requires running QEMU" in output.getvalue()


def test_adc_requires_explicitly_configured_cosim(monkeypatch):
    lab = Lab()
    owner(monkeypatch, lab)
    output = io.StringIO()
    with pipe(b"adc ch0 raw 1\nquit\n") as (reader, _):
        interactive.run_interactive("fw", input_stream=reader, output_stream=output)
    assert lab.calls == ["cont"]
    assert "ADC co-sim is not configured" in output.getvalue()


def test_input_and_paused_admission_happen_before_owner(monkeypatch, tmp_path):
    lab = Lab()
    lifecycle = owner(monkeypatch, lab)
    with pytest.raises(ValueError, match="descriptor"):
        interactive.run_interactive("fw", input_stream=io.StringIO("quit\n"))
    path = tmp_path / "commands"
    path.write_text("quit\n")
    with path.open() as stream, pytest.raises(ValueError, match="regular files"):
        interactive.run_interactive("fw", input_stream=stream)
    with pipe(b"quit\n") as (reader, _), pytest.raises(ValueError, match="GDB"):
        interactive.run_interactive("fw", input_stream=reader, paused=True)
    assert lifecycle == []


@pytest.mark.parametrize("failure", [TimeoutError("deadline"),
                                      ConnectionError("transport"), KeyboardInterrupt()])
def test_runtime_fault_or_interrupt_propagates_and_cleanup_preserves_primary(monkeypatch, failure):
    lab = Lab()
    lab.failure = failure
    lifecycle = owner(monkeypatch, lab, cleanup_error=OSError("cleanup"))
    with pipe(b"quit\n") as (reader, _):
        with pytest.raises(type(failure)) as caught:
            interactive.run_interactive("fw", input_stream=reader, output_stream=io.StringIO())
    assert caught.value is failure and lifecycle[-1] == "cleanup"
    assert any("cleanup" in note for note in failure.__notes__)


def test_valid_command_qmp_valueerror_is_not_misreported_as_bad_input(monkeypatch):
    lab = Lab()
    failure = ValueError("QMP producer fault")
    def broken(stimulus):
        raise failure
    lab.inject = broken
    lifecycle = owner(monkeypatch, lab)
    with pipe(b"key press\nquit\n") as (reader, _), pytest.raises(ValueError) as caught:
        interactive.run_interactive("fw", input_stream=reader, output_stream=io.StringIO())
    assert caught.value is failure and lifecycle[-1] == "cleanup"


def test_cleanup_failure_prevents_success_return_and_emit_uses_caller(monkeypatch):
    event = ChannelEvent("uart1", 1, b"ready")
    lab = Lab(events=[event])
    failure = OSError("cannot clean owner")
    lifecycle = owner(monkeypatch, lab, cleanup_error=failure)
    emitted = []
    with pipe(b"quit\n") as (reader, _), pytest.raises(OSError) as caught:
        interactive.run_interactive("fw", input_stream=reader, output_stream=io.StringIO(),
                                    emit=emitted.append)
    assert caught.value is failure and emitted == [event] and lifecycle[-1] == "cleanup"


def test_finite_timeout_admission_reuses_runtime_before_process(monkeypatch, tmp_path):
    firmware = tmp_path / "fw.elf"
    firmware.write_bytes(b"firmware input")
    from sim.lab import runtime
    def never_start(*args, **kwargs):
        pytest.fail("invalid host deadline started QEMU")
    monkeypatch.setattr(runtime, "running_qemu", never_start)
    with pipe(b"quit\n") as (reader, _):
        with pytest.raises(ValueError, match="finite"):
            interactive.run_interactive(firmware, qemu="/bin/true", timeout=float("inf"),
                                        input_stream=reader, output_stream=io.StringIO())
