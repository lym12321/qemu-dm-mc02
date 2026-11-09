#!/usr/bin/env python3
"""Real Chromium/CLI consumers of existing HAL and native channel fixtures.

The browser operates visible controls and reads the public board-state display.
No guest memory, private simulator state or test-only browser hook is used.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "smoke"))

from smoke.support.process import identity, stop
from smoke.support.processes import ChildProcess


def regular_file(path, label, *, executable=False):
    path = Path(path).resolve()
    if not path.is_file() or not os.access(path, os.X_OK if executable else os.R_OK):
        raise ValueError(f"{label} is not {'executable' if executable else 'readable'}: {path}")
    return path


def resolve_qemu(value):
    value = value or os.environ.get("QEMU_SYSTEM_ARM") or ROOT / "build/qemu/qemu-system-arm"
    if isinstance(value, str) and "/" not in value:
        value = shutil.which(value) or value
    return regular_file(value, "QEMU", executable=True)


class StageFailure(AssertionError):
    def __init__(self, stage, last):
        self.stage = stage
        super().__init__(f"GPIO lesson stage {stage!r} did not match; last={last!r}")

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("teaching consumer host deadline expired")
    return value

def levels(state):
    a, b = state["gpio"]["A"], state["gpio"]["B"]
    return {"timestamp_ns": state["timestamp_ns"],
            "a15": (a["sampled"] >> 15) & 1,
            "a15_mode": (a["moder"] >> 30) & 3,
            "b0": b["sampled"] & 1, "b0_odr": b["odr"] & 1,
            "b0_mode": b["moder"] & 3,
            "b0_output_mask": b["output_mask"] & 1}

def matches(state, key, output):
    value = levels(state)
    return (value["a15_mode"] == 0 and value["a15"] == key
            and value["b0_mode"] == 1 and value["b0_output_mask"] == 1
            and value["b0"] == value["b0_odr"] == output)

def ordered_sequence(snapshot, press, release, wait, deadline, record):
    """The same acceptance is used for both valid and constant-low firmware."""
    for name, key, output, action in (("initial", 1, 0, None),
                                     ("pressed", 0, 1, press),
                                     ("released", 1, 0, release)):
        if action is not None:
            action()
        stage_deadline = min(deadline, time.monotonic() + 2.0)
        last = None
        while True:
            remaining(deadline)
            state = snapshot()
            last = levels(state)
            if matches(state, key, output):
                record["stages"].append({"name": name, **last})
                break
            if time.monotonic() >= stage_deadline:
                record["failed_stage"] = {"name": name, "last": last}
                raise StageFailure(name, last)
            wait()

def cleanup(primary, resources):
    failures = []
    for label, release in resources:
        try:
            release()
        except BaseException as error:
            failures.append((label, error))
    if failures:
        error = primary if primary is not None else failures[0][1]
        for label, failure in failures:
            BaseException.add_note(error, f"Teaching cleanup {label} failed: {failure}")
        if primary is None:
            raise error

def free_gdb_port():
    # Native GDB is TCP. Bind briefly to choose an unused local port; a race
    # with another host process fails explicitly rather than changing ports.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]

def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"interrupted by signal {signum}")

def descendants(pid):
    """Read only identities and argv of this test's own server descendants."""
    result = {}
    tasks = Path(f"/proc/{pid}/task")
    if not tasks.exists():
        return result
    for task in tasks.iterdir():
        try:
            children = (task / "children").read_text().split()
        except FileNotFoundError:
            continue
        for child in children:
            current = identity(int(child))
            if current is not None:
                result[int(child)] = current
    return result


class Server:
    def __init__(self, deadline, record, executable):
        self.deadline, self.record = deadline, record
        self.executable = executable
        self.record["native_argv"] = {}
        self.native = {}
        self.directories = set()
        self.port = free_gdb_port()
        self.process = self.child = None

    def start(self, qemu):
        self.process = subprocess.Popen(
            [str(self.executable), "lab", "web", "--port", str(self.port),
             "--qemu", str(qemu), "--startup-timeout", "10", "--timeout", "45"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self.child = ChildProcess("Web Lab CLI", self.process)
        self.record["identity"] = identity(self.process.pid)
        self.record["entry"] = str(self.executable)
        self.record["entry_sha256"] = sha(self.executable)
        self.record["rust_process_has_no_libpython"] = "libpython" not in Path(f"/proc/{self.process.pid}/maps").read_text()
        if not self.record["rust_process_has_no_libpython"]:
            raise AssertionError("Rust Web process loaded libpython")
        self.child.prepare()
        if self.record["identity"] is None:
            raise AssertionError("Web CLI exited before its identity was recorded")

    def check(self):
        remaining(self.deadline)
        self.child.pump()
        if self.process.poll() is not None:
            raise RuntimeError(f"Web CLI exited early: {self.process.returncode}; {self.child.diagnostics()}")
        for pid, current in descendants(self.process.pid).items():
            if pid in self.native:
                continue
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            if b"-machine" in argv and any(b"dm-mc02" in item for item in argv):
                self.native[pid] = current
                self.record["native_argv"][str(pid)] = [os.fsdecode(arg) for arg in argv if arg]
                directories = set()
                for item in argv:
                    for match in re.finditer(rb"(?:unix:|path=)(/tmp/dm-qemu\.[^,]+)", item):
                        directories.add(Path(os.fsdecode(match[1])).parent)
                if not directories:
                    raise AssertionError(f"native QEMU {pid} has no recorded private socket directory")
                self.directories.update(directories)

    def wait(self):
        self.check()
        select.select(self.child.readers(), [], [], min(.01, remaining(self.deadline)))

    def ready(self):
        expected = f"Web Lab: http://127.0.0.1:{self.port}".encode()
        while expected not in self.child.tails["stdout"]:
            self.wait()
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        if self.process is None:
            return
        primary = None
        try:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                deadline = time.monotonic() + 5
                while self.process.poll() is None and time.monotonic() < deadline:
                    self.child.pump()
                    select.select(self.child.readers(), [], [], .01)
                if self.process.poll() is None:
                    raise AssertionError("Web CLI did not finish TERM cleanup within five seconds")
            if self.process.returncode != 130:
                raise AssertionError(f"Web CLI shutdown returned {self.process.returncode}")
        except BaseException as error:
            primary = error
            raise
        finally:
            cleanup(primary, [("Web CLI stop/reap", lambda: stop(self.process)),
                              ("Web stdout", self.process.stdout.close),
                              ("Web stderr", self.process.stderr.close)])
            self.record.update(returncode=self.process.returncode,
                               identity_gone=identity(self.process.pid) != self.record["identity"],
                               native_identities=list(self.native.values()),
                               native_identities_gone=all(identity(pid) != saved for pid, saved in self.native.items()),
                               private_directories=[str(path) for path in sorted(self.directories)],
                               private_directories_gone=all(not path.exists() for path in self.directories),
                               **self.child.diagnostics())
        if not all(self.record[name] for name in
                   ("identity_gone", "native_identities_gone", "private_directories_gone")):
            raise AssertionError("Web CLI or native QEMU resources survived cleanup")
        if self.native and len(self.directories) != len(self.native):
            raise AssertionError("each native QEMU requires an observed private directory")


def wait_for(page, server, predicate, label):
    deadline = min(server.deadline, time.monotonic() + 3)
    while True:
        server.check()
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"browser did not observe {label}; notice={page.locator('#notice').text_content()!r}")
        # Playwright dispatches actual browser events while yielding to the UI.
        page.wait_for_function("() => true")
        server.wait()


def status(page):
    return page.locator("#session-status").get_attribute("data-status")


def board(page):
    state = json.loads(page.locator("#raw-state").text_content())
    if state is not None:
        # The Web envelope intentionally uses decimal strings above JS's safe
        # integer limit. Restore the integer only for the existing host checker.
        state["timestamp_ns"] = int(state["timestamp_ns"])
    return state


def log_text(page, kind="uart"):
    return "".join(page.locator(f".log-row[data-kind='{kind}'] .log-data").all_text_contents())


def load(page, server, elf):
    page.locator("#firmware-input").set_input_files(str(elf))
    wait_for(page, server, lambda: page.locator("#firmware-name").text_content() == elf.name
             and page.locator("#load-button").is_enabled(), "uploaded firmware")
    page.locator("#load-button").click()
    wait_for(page, server, lambda: status(page) == "paused", "paused native session")
    assert page.locator("#adc-send").is_disabled()
    page.locator("#run-button").click()
    wait_for(page, server, lambda: status(page) == "running", "running native session")


def stop_board(page, server):
    page.locator("#stop-button").click()
    wait_for(page, server, lambda: status(page) == "stopped", "completed native cleanup")
    assert page.locator("#observation-status").get_attribute("data-stale") == "true"


def gpio_lesson(page, server, elf, record, screenshot):
    load(page, server, elf)
    button = page.locator("#key-button")

    def snapshot():
        server.check()
        value = board(page)
        if value is None:
            raise AssertionError("running GPIO lesson has no public snapshot")
        return value

    def press():
        button.scroll_into_view_if_needed()
        box = button.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()

    ordered_sequence(snapshot, press, page.mouse.up,
                     lambda: (page.wait_for_function("() => true"), server.wait()),
                     server.deadline, record)
    assert page.locator("[data-pin='B0']").get_attribute("data-level") == "0"
    assert page.locator("[data-pin='B0']").get_attribute("data-mode") == "1"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(screenshot), full_page=True)
    record["screenshot"] = str(screenshot)
    stop_board(page, server)


def channels(page, server, elf, record, screenshot):
    page.locator("#clear-log").click()
    load(page, server, elf)
    wait_for(page, server, lambda: all(text in log_text(page) for text in
             ("LAB_READY\\n", "KEY=1\\n", "ADC=43329\\n")), "actual fixture UART readiness")
    page.locator("#uart-data").fill("q")
    page.locator("#uart-send").click()
    wait_for(page, server, lambda: "UART=q\\n" in log_text(page), "UART echo")
    page.locator("#key-button").focus()
    page.keyboard.down("Space")
    wait_for(page, server, lambda: board(page)["key"]["pressed"] and
             board(page)["led"]["rgb"] == 0xffffff, "key-driven A7 output")
    page.keyboard.up("Space")
    wait_for(page, server, lambda: not board(page)["key"]["pressed"] and
             board(page)["led"]["rgb"] == 0, "key release")
    page.locator("#tab-adc").click()
    page.locator("#adc-channel").select_option("4")
    page.locator("#adc-mode").select_option("voltage_uv")
    page.locator("#adc-value").fill("1650000")
    page.locator("#adc-send").click()
    wait_for(page, server, lambda: "ADC=32768\\n" in log_text(page), "ADC midpoint")
    page.locator("#adc-mode").select_option("raw")
    page.locator("#adc-value").fill("12345")
    page.locator("#adc-send").click()
    wait_for(page, server, lambda: "ADC=12345\\n" in log_text(page), "ADC raw input")
    page.locator("#tab-can").click()
    page.locator("#can-send").click()  # Existing fixture example defaults.
    wait_for(page, server, lambda: "ID 0x322  flags=0  DLC=8  41 42 43 44 45 46 47 48"
             in log_text(page, "can"), "actual CAN echo")
    assert "CAN_BAD" not in log_text(page)
    record.update(uart=log_text(page), can=log_text(page, "can"), board=board(page))
    page.locator("#pause-button").click()
    wait_for(page, server, lambda: status(page) == "paused", "native pause")
    page.locator("#tab-adc").click()
    assert page.locator("#adc-send").is_disabled()
    assert "先运行" in page.locator("#adc-hint").text_content()
    page.screenshot(path=str(screenshot), full_page=True)
    record["screenshot"] = str(screenshot)
    # Leave one actual MCU active for the server SIGTERM cleanup consumer.
    record["shutdown_state"] = status(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug-elf", required=True, type=Path)
    parser.add_argument("--release-elf", required=True, type=Path)
    parser.add_argument("--lab-elf", required=True, type=Path,
                        help="existing smoke/dm_mc02_lab_smoke.c fixture ELF")
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--sim", required=True, type=Path, help="compiled Rust sim executable with web enabled")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    elfs = {name: regular_file(path, name + " ELF") for name, path in
            (("Debug", args.debug_elf), ("Release", args.release_elf), ("channels", args.lab_elf))}
    qemu = resolve_qemu(args.qemu)
    executable = regular_file(args.sim, "Rust sim", executable=True)
    args.output.mkdir(parents=True, exist_ok=False)
    protected = {str(path): sha(path) for path in (*elfs.values(), qemu, executable, Path(__file__).resolve(),
                 ROOT / "smoke/support/process.py", ROOT / "smoke/support/processes.py")}
    report = {"status": "failed", "inputs": protected, "cases": [], "server": {},
              "limits": ["Digital native firmware/browser consumer; no physical calibration.",
                         "HAL GPIO and bare MMIO channel fixtures are distinct evidence.",
                         "Observed timestamps do not establish a global CPU/peripheral barrier."]}
    code = 1
    server = browser = playwright = None
    primary = None
    try:
        from playwright.sync_api import sync_playwright
        server = Server(time.monotonic() + 90, report["server"], executable)
        server.start(qemu)
        url = server.ready()
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        report["browser"] = browser.version
        for profile, width in (("Debug", 1440), ("Release", 390), ("channels", 1440)):
            record = {"profile": profile, "status": "failed", "stages": []}
            report["cases"].append(record)
            page = browser.new_page(viewport={"width": width, "height": 960})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_default_timeout(3000)
            try:
                page.goto(url, wait_until="networkidle")
                wait_for(page, server, lambda: page.locator("#connection-status").text_content() == "已连接", "WebSocket controller")
                screenshot = args.output / (profile + ".png")
                if profile == "channels":
                    channels(page, server, elfs[profile], record, screenshot)
                else:
                    gpio_lesson(page, server, elfs[profile], record, screenshot)
                if errors:
                    raise AssertionError(f"browser JavaScript errors: {errors}")
            finally:
                record["page_errors"] = errors
                page.close()
            record["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        primary = error
        report.update(error=str(error), error_type=type(error).__name__,
                      notes=list(getattr(error, "__notes__", ())))
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        try:
            resources = []
            if browser is not None:
                resources.append(("Chromium", browser.close))
            if playwright is not None:
                resources.append(("Playwright", playwright.stop))
            if server is not None:
                resources.append(("Web server", server.close))
            cleanup(primary, resources)
            if primary is not None:
                report["notes"] = list(getattr(primary, "__notes__", ()))
            if {path: sha(path) for path in protected} != protected:
                raise AssertionError("Web smoke firmware/QEMU inputs changed")
            if primary is None:
                if len(report["server"].get("native_identities", ())) != 3:
                    raise AssertionError("browser gate did not record all three native QEMU identities")
                report["status"] = "passed"
                code = 0
        except BaseException as error:
            report.update(error=str(error), error_type=type(error).__name__,
                          notes=list(getattr(error, "__notes__", ())))
            code = 130 if isinstance(error, KeyboardInterrupt) else 1
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Web browser consumer: {report['status']} ({args.output / 'report.json'})", flush=True)
    return code


if __name__ == "__main__":
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(main())
    finally:
        signal.signal(signal.SIGTERM, previous)
