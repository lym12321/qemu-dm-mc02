"""Small line console over the existing Lab owner and public board channels.

Input is a borrowed, exclusively consumed pipe or terminal file descriptor.
One select/read services at most 4096 bytes; command lines are bounded to 4096
bytes and processed one per channel-pump iteration. Regular input files and
Python-only text streams are rejected. Output/emit callbacks are synchronous
caller interfaces, without a guarantee that a blocking sink can be preempted.
"""

from __future__ import annotations

import json
import os
import re
import select
import shlex
import stat
import sys

from sim.boards import validate_gpio_pin
from sim.fabric import CanFrame

from .manifest import Stimulus
from .runtime import open_lab


HELP = """Commands:
  help | state | pause | cont | quit
  key press|release
  gpio A0 0|1
  vin MILLIVOLTS
  uart uart1 TEXT
  can can1 ID FLAGS DLC HEX
  adc chN raw|voltage_uv VALUE
UART text uses UTF-8; quotes group spaces. No shell expansion or reset command.
"""


class _CommandError(ValueError):
    pass


def _number(text: str, name: str, maximum: int) -> int:
    try:
        value = int(text, 0)
    except ValueError as error:
        raise _CommandError(f"{name} must be an integer") from error
    if not 0 <= value <= maximum:
        raise _CommandError(f"{name} must be in range 0..{maximum}")
    return value


def _parse(line: bytes, uarts: tuple, cans: tuple, adc: bool):
    try:
        args = shlex.split(line.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise _CommandError("command must be valid UTF-8 with balanced quotes") from error
    if not args:
        return "empty", None
    command = args[0]
    if command in ("help", "state", "pause", "cont", "quit"):
        if len(args) != 1:
            raise _CommandError(f"{command} takes no arguments")
        return command, None
    if command == "key":
        if len(args) != 2 or args[1] not in ("press", "release"):
            raise _CommandError("usage: key press|release")
        return "inject", Stimulus(0, "key", "key", args[1] == "press")
    if command == "gpio":
        if len(args) != 3:
            raise _CommandError("usage: gpio A0 0|1")
        try:
            pin = validate_gpio_pin(args[1])
        except (TypeError, ValueError) as error:
            raise _CommandError(str(error)) from error
        return "inject", Stimulus(0, f"gpio.{pin}", "gpio",
                                   _number(args[2], "GPIO level", 1))
    if command == "vin":
        if len(args) != 2:
            raise _CommandError("usage: vin MILLIVOLTS")
        return "vin", _number(args[1], "VIN", 0xffffffff)
    if command == "uart":
        if len(args) < 3:
            raise _CommandError("usage: uart uart1 TEXT")
        if args[1] not in uarts:
            raise _CommandError("UART endpoint is not configured")
        data = " ".join(args[2:]).encode("utf-8")
        if len(data) > 4096:
            raise _CommandError("UART text must be at most 4096 bytes")
        return "inject", Stimulus(0, args[1], "uart", data)
    if command == "can":
        if len(args) != 6:
            raise _CommandError("usage: can can1 ID FLAGS DLC HEX")
        if args[1] not in cans:
            raise _CommandError("CAN endpoint is not configured")
        can_id = _number(args[2], "CAN ID", 0x1fffffff)
        flags = _number(args[3], "CAN flags", 0xf)
        dlc = _number(args[4], "CAN DLC", 15)
        try:
            data = bytes.fromhex(args[5])
            frame = CanFrame(can_id, flags, dlc, data)
        except (TypeError, ValueError) as error:
            raise _CommandError(f"invalid CAN frame: {error}") from error
        return "inject", Stimulus(0, args[1], "can", frame)
    if command == "adc":
        if len(args) != 4:
            raise _CommandError("usage: adc chN raw|voltage_uv VALUE")
        if not adc:
            raise _CommandError("ADC co-sim is not configured")
        if re.fullmatch(r"ch(?:[0-9]|[12][0-9]|3[01])", args[1]) is None:
            raise _CommandError("ADC1 channel must be ch0 through ch31")
        if args[2] not in ("raw", "voltage_uv"):
            raise _CommandError("ADC input must be raw or voltage_uv")
        maximum = 65535 if args[2] == "raw" else 3300000
        value = _number(args[3], "ADC " + args[2], maximum)
        return "inject", Stimulus(0, "adc1." + args[1], "adc_" +
                                   ("raw" if args[2] == "raw" else "voltage"), value)
    raise _CommandError(f"unknown command: {command!r}; use help")


def _input_fd(stream) -> int:
    try:
        fd = stream.fileno()
        mode = os.fstat(fd).st_mode
    except (AttributeError, OSError, ValueError) as error:
        raise ValueError("interactive input requires a pipe or terminal descriptor") from error
    if not (stat.S_ISFIFO(mode) or os.isatty(fd)):
        raise ValueError("interactive input must be a pipe or terminal; regular files are unsupported")
    return fd


def run_interactive(firmware, *, uarts=("uart1",), cans=(), adc=False,
                    input_stream=sys.stdin, output_stream=sys.stdout, emit=None,
                    paused=False, **options) -> dict:
    """Run a borrowed stdin console; return only after owner cleanup succeeds."""
    fd = _input_fd(input_stream)
    if type(paused) is not bool:
        raise TypeError("paused must be a boolean")
    if paused and options.get("gdb_port") is None:
        raise ValueError("paused Lab console requires a native GDB port")
    if emit is not None and not callable(emit):
        raise TypeError("Lab output sink must be callable")
    if any(not callable(getattr(output_stream, name, None)) for name in ("write", "flush")):
        raise TypeError("interactive output requires write and flush")
    uarts, cans = tuple(uarts), tuple(cans)
    options.setdefault("timeout", 300.0)

    def write(text):
        output_stream.write(text)
        output_stream.flush()

    def output_events(events):
        for event in events:
            if emit is not None:
                emit(event)
            else:
                write(f"{event.endpoint} {event.data!r}\n")

    buffer = bytearray()
    discarding = False
    eof = False
    commands = batches = 0
    with open_lab(firmware, uarts=uarts, cans=cans, adc=adc, **options) as lab:
        write("Lab connected; enter help for commands\n")
        if not paused:
            lab.call(lab.board.resume, startup=True)
        write(HELP)
        while True:
            lab.remaining()
            snapshot, events = lab.pump()
            batches += 1
            output_events(events)
            if b"\n" not in buffer and not eof:
                readable, _, _ = select.select(
                    [fd, *lab.peers], [], [], min(.005, lab.remaining()))
                if fd in readable:
                    data = os.read(fd, 4096)
                    if not data:
                        eof = True
                    elif discarding:
                        newline = data.find(b"\n")
                        if newline >= 0:
                            discarding = False
                            buffer.extend(data[newline + 1:])
                    else:
                        buffer.extend(data)
            newline = buffer.find(b"\n")
            if newline >= 0:
                line = bytes(buffer[:newline])
                del buffer[:newline + 1]
            elif eof:
                if not buffer:
                    break
                line = bytes(buffer)
                buffer.clear()
            else:
                if len(buffer) > 4096:
                    write("error: command line exceeds 4096 bytes\n")
                    buffer.clear()
                    discarding = True
                continue
            if len(line) > 4096:
                write("error: command line exceeds 4096 bytes\n")
                continue
            lab.remaining()
            try:
                command, value = _parse(line.rstrip(b"\r"), uarts, cans, adc)
            except _CommandError as error:
                write(f"error: {error}\n")
                continue
            if command == "empty":
                continue
            if command == "quit":
                commands += 1
                break
            if command == "help":
                write(HELP)
            elif command == "state":
                write(json.dumps(lab.snapshot(), sort_keys=True) + "\n")
            elif command == "vin":
                lab.call(lambda: lab.board.set_vin(value))
            elif command == "pause":
                lab.call(lab.board.pause)
            elif command == "cont":
                lab.call(lab.board.resume)
            elif command == "inject":
                if value.kind in ("adc_raw", "adc_voltage"):
                    running = lab.call(lambda: lab.qmp.command("query-status"))["running"]
                    if not running:
                        write("error: ADC input requires running QEMU; use cont first\n")
                        continue
                lab.inject(value)
            commands += 1
        # Flush final queued CAN/UART output once, without inventing guest time.
        snapshot, events = lab.pump()
        batches += 1
        output_events(events)
        lab.remaining()
        result = {"commands": commands, "observation_batches": batches,
                  "end_ns": snapshot["timestamp_ns"]}
    return result
