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
from sim.boards.channels import ChannelEvent
from sim.fabric import CanFrame


def isolated(tmp_path, *arguments):
    return subprocess.run([sys.executable, "-I", str(ROOT / "tools/sim-unmigrated"), *arguments],
                          cwd=tmp_path, capture_output=True, text=True, timeout=5)


def test_isolated_entry_help_and_module_help(tmp_path):
    result = isolated(tmp_path, "lab", "interactive", "--help")
    assert result.returncode == 0, result.stderr
    assert "--startup-timeout" in result.stdout
    assert "--gdb-port" in result.stdout and "--elf" in result.stdout
    result = isolated(tmp_path, "board", "run", "--help")
    assert result.returncode == 0, result.stderr
    assert "Ctrl-a c" in result.stdout and "--elf" in result.stdout
    result = subprocess.run([sys.executable, "-m", "sim.apps.unmigrated_cli", "--help"], cwd=ROOT,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "board" in result.stdout and "lab" in result.stdout
    assert "robot" not in result.stdout and "arena" not in result.stdout and "scenario" not in result.stdout


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
