"""Bounded diagnostics for borrowed child process output pipes.

Callers own launching, deadlines, stopping and closing the process and pipes.
"""

from dataclasses import dataclass, field
import os
import subprocess


@dataclass
class ChildProcess:
    label: str
    process: subprocess.Popen
    tails: dict = field(default_factory=lambda: {"stdout": b"", "stderr": b""})
    eof: set = field(default_factory=set)

    def prepare(self):
        for name in self.tails:
            os.set_blocking(getattr(self.process, name).fileno(), False)

    def pump(self):
        # One bounded read per pipe, including children with continuous output.
        for name in self.tails:
            if name in self.eof:
                continue
            try:
                data = os.read(getattr(self.process, name).fileno(), 4096)
            except BlockingIOError:
                continue
            if not data:
                self.eof.add(name)
            else:
                self.tails[name] = (self.tails[name] + data)[-12000:]

    def readers(self):
        return [getattr(self.process, name) for name in self.tails if name not in self.eof]

    def diagnostics(self):
        return {name: data.decode("utf-8", errors="replace") for name, data in self.tails.items()}
