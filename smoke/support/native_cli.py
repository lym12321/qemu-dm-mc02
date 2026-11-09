"""Black-box CLI driver and observations for native integration tests only."""

import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time

from smoke.support.process import identity, stop
from smoke.support.processes import ChildProcess


def regular_file(path, label, *, executable=False):
    path = Path(path).resolve()
    if not path.is_file() or not os.access(path, os.X_OK if executable else os.R_OK):
        raise ValueError(f"{label} is not {'executable' if executable else 'readable'}: {path}")
    return path


def resolve_sim(value=None):
    root = Path(__file__).resolve().parents[2]
    return regular_file(value or os.environ.get("SIM_EXECUTABLE", root / "target/release/sim"),
                        "Rust sim", executable=True)


def children(pid):
    result = set()
    try:
        tasks = tuple(Path(f"/proc/{pid}/task").iterdir())
    except FileNotFoundError:
        return result
    for task in tasks:
        try:
            result.update(int(value) for value in (task / "children").read_text().split())
        except FileNotFoundError:
            pass
    return result


class NativeCommand:
    """One test command; runtime ownership remains inside the Rust CLI."""

    def __init__(self, argv, deadline, record, *, stdin=False, measure=False):
        self.deadline, self.record = deadline, record
        self.measure = measure
        self.native = {}
        self.process = subprocess.Popen(
            [str(value) for value in argv], stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.child = ChildProcess("native CLI consumer", self.process)
        self.record.update(argv=[str(value) for value in argv], identity=identity(self.process.pid))
        self.child.prepare()
        if self.record["identity"] is None:
            self.close()
            raise AssertionError("CLI exited before its process identity was recorded")

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("native CLI consumer host deadline expired")
        return left

    def pump(self):
        self.remaining()
        self.child.pump()
        for pid in children(self.process.pid):
            if pid in self.native:
                continue
            saved = identity(pid)
            if saved is None:
                continue
            try:
                argv = [os.fsdecode(value) for value in
                        Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if value]
            except FileNotFoundError:
                continue
            if "-machine" not in argv or not any("dm-mc02" in arg for arg in argv):
                continue
            sockets = {}
            for index, arg in enumerate(argv):
                for match in re.finditer(r"(?:unix:|path=)(/tmp/dm-qemu\.[^,]+)", arg):
                    sockets[str(index)] = match[1]
            if argv.count("-kernel") != 1 or not sockets:
                raise AssertionError("native process lacks its ELF or private socket paths")
            firmware = Path(argv[argv.index("-kernel") + 1]).resolve(strict=True)
            self.native[pid] = {**saved, "argv": argv, "sockets": sockets,
                                "firmware": {"path": str(firmware),
                                  "sha256": hashlib.sha256(firmware.read_bytes()).hexdigest()}}

        if self.measure:
            samples = self.record.setdefault("process_samples", {})
            for pid in (self.process.pid, *self.native):
                try:
                    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                    resident = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
                except (FileNotFoundError, ProcessLookupError):
                    continue
                row = samples.setdefault(str(pid), {"starttime": int(fields[19]),
                    "cpu_sampled_s": 0.0, "peak_rss_bytes": 0, "samples": 0})
                if row["starttime"] != int(fields[19]):
                    raise AssertionError("sampled process identity changed")
                row["cpu_sampled_s"] = max(row["cpu_sampled_s"],
                    (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"))
                row["peak_rss_bytes"] = max(row["peak_rss_bytes"], resident * os.sysconf("SC_PAGE_SIZE"))
                row["samples"] += 1

    def wait(self):
        self.pump()
        select.select(self.child.readers(), [], [], min(.005, self.remaining()))

    def check(self):
        self.pump()
        if self.process.poll() is not None:
            raise RuntimeError(f"CLI exited early: {self.process.returncode}; {self.child.diagnostics()}")

    def send(self, text):
        self.check()
        self.process.stdin.write((text + "\n").encode())
        self.process.stdin.flush()

    def ready(self):
        while b"Lab connected;" not in self.child.tails["stdout"]:
            self.check()
            self.wait()

    def snapshot(self):
        def states():
            return [json.loads(line) for line in self.child.tails["stdout"].splitlines()
                    if line.startswith(b'{"') and line.endswith(b"}")]
        self.check()
        before = states()
        previous = before[-1]["timestamp_ns"] if before else None
        self.send("state")
        while True:
            self.check()
            current = states()
            if current and current[-1]["timestamp_ns"] != previous:
                return current[-1]
            self.wait()

    def finish(self):
        while self.process.poll() is None:
            self.wait()
        self.child.pump()
        return self.process.returncode

    def close(self, primary=None):
        failures = []
        for label, close in (("stop/reap", lambda: stop(self.process)),
                             ("stdin", self.process.stdin.close if self.process.stdin else lambda: None),
                             ("stdout", self.process.stdout.close),
                             ("stderr", self.process.stderr.close)):
            try:
                close()
            except BaseException as error:
                failures.append(f"CLI {label}: {error}")
        for pid, saved in self.native.items():
            if identity(pid) == {key: saved[key] for key in ("pid", "starttime")}:
                failures.append(f"owned native process remains: {pid}")
            if any(os.path.lexists(path) for path in saved["sockets"].values()):
                failures.append(f"owned native socket remains: {pid}")
            if any(Path(path).parent.exists() for path in saved["sockets"].values()
                   if Path(path).parent.name.startswith("dm-qemu.native.")):
                failures.append(f"owned native private directory remains: {pid}")
        if self.process.returncode is None or identity(self.process.pid) == self.record["identity"]:
            failures.append("CLI process identity remains or was not reaped")
        self.record.update(returncode=self.process.returncode,
                           identity_gone=identity(self.process.pid) != self.record["identity"],
                           resources=list(self.native.values()), cleanup_completed=not failures,
                           cleanup_failures=failures, **self.child.diagnostics())
        if failures:
            if primary is None:
                raise AssertionError("; ".join(failures))
            for failure in failures:
                primary.add_note(failure)
