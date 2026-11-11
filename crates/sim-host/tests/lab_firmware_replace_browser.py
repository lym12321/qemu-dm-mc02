#!/usr/bin/env python3
"""Web consumer: rebuilding a local ELF leaves the loaded firmware intact."""
import argparse
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
from lab_uart_console_browser import UART_TEXT, uart_text
from lab_hot_reload_browser import identity_alive
from lab_web_native import identities


def qmp_system_reset(path):
    # Playwright's sync API already drives this thread's asyncio loop.
    # Run the pinned synchronous QMP client in its own bounded process.
    completed = subprocess.run([sys.executable, "-c", """
import json, sys
from smoke.support.qmp import qmp_session_class
qmp = qmp_session_class()(sys.argv[1], timeout=2, retain_events=False)
try:
    print(json.dumps(qmp.command("system_reset")))
finally:
    qmp.close()
""", str(path)], cwd=Path(__file__).resolve().parents[3],
        capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser()
    for option in ["sim", "qemu", "elf", "adc", "output"]:
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    report = {"status": "failed", "sessions": [], "native_identities": [], "browser_errors": []}
    process = None
    primary = None
    qmp_directory = tempfile.TemporaryDirectory(prefix="lab-replace-")
    qmp_path = Path(qmp_directory.name) / "qmp.sock"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    firmware = args.output.resolve() / "build.elf"
    firmware.write_bytes(args.elf.read_bytes())

    def state():
        with urlopen(url + "/api/state", timeout=1) as response:
            return json.load(response)

    try:
        with (args.output / "stdout").open("wb") as stdout, (args.output / "stderr").open("wb") as stderr:
            process = subprocess.Popen([str(args.sim.resolve()), "lab", "web", "--port", str(port),
                "--elf", str(firmware), "--qemu", str(args.qemu.resolve()), "--qmp", str(qmp_path),
                "--uart", "uart1", "--adc=false", "--imu", "off", "--timeout", "60"],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    raise AssertionError(f"server exited: {process.returncode}")
                try:
                    assert state()["status"] == "idle"
                    break
                except OSError:
                    assert time.monotonic() < deadline, "server startup deadline"
                    time.sleep(.02)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page(viewport={"width": 1366, "height": 768})
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                    page.goto(url)
                    page.wait_for_function("!document.querySelector('#primary-button').disabled")
                    page.locator("#primary-button").click()
                    page.wait_for_function(f"({UART_TEXT}).includes('SIM_READY minimal-board\\n')")
                    original = state()
                    original_native = identities(process.pid)
                    assert original["status"] == "running" and not original["config"]["watch_elf"]
                    assert len(original_native) == 1
                    report["sessions"].append(original["session_id"])
                    report["native_identities"].extend(original_native)

                    # QEMU's native reset reloads its retained ROM data. Both
                    # linker truncation and partial output must leave that data
                    # independent of the original, mutable build path.
                    report["reset_with_incomplete_source"] = []
                    for contents in [b"", b"partial ELF"]:
                        page.locator("#uart-clear").click()
                        firmware.write_bytes(contents)
                        assert qmp_system_reset(qmp_path) == {}
                        page.wait_for_function(f"({UART_TEXT}).includes('SIM_READY minimal-board\\n')")
                        observed = state()
                        assert observed["session_id"] == original["session_id"] and observed["status"] == "running"
                        assert identities(process.pid) == original_native
                        report["reset_with_incomplete_source"].append(len(contents))
                    # Explicit reload must reject a linker output that is not
                    # yet an ELF before stopping the healthy native owner.
                    page.locator("#restart-button").click()
                    page.wait_for_function("!document.querySelector('#session-error').hidden && !document.querySelector('#restart-button').disabled")
                    refused = state()
                    assert refused["session_id"] == original["session_id"] and refused["status"] == "running"
                    assert identities(process.pid) == original_native
                    report["incomplete_reload_error"] = page.locator("#session-error").inner_text()

                    firmware.write_bytes(args.adc.read_bytes())
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        observed = state()
                        assert observed["session_id"] == original["session_id"] and observed["status"] == "running"
                        assert identities(process.pid) == original_native
                        time.sleep(.05)
                    assert "LAB_READY\n" not in uart_text(page)
                    assert page.locator("#session-error").inner_text() == report["incomplete_reload_error"]
                    report["no_watch_observation_ms"] = 1500

                    page.locator("#restart-button").click()
                    page.wait_for_function(f"({UART_TEXT}).includes('LAB_READY\\n')")
                    reloaded = state()
                    reloaded_native = identities(process.pid)
                    assert reloaded["status"] == "running" and reloaded["session_id"] != original["session_id"]
                    assert not reloaded["config"]["watch_elf"]
                    assert len(reloaded_native) == 1 and reloaded_native != original_native
                    assert not any(map(identity_alive, original_native)), "old QEMU identity survived reload"
                    assert page.locator("#session-error").is_hidden()
                    report["sessions"].append(reloaded["session_id"])
                    report["native_identities"].extend(reloaded_native)
                    report["uart_markers"] = ["SIM_READY minimal-board\n", "LAB_READY\n"]
                    page.locator("#stop-button").click()
                    page.wait_for_function("document.querySelector('#session-status').textContent==='已停止'")
                    assert not identities(process.pid), "stopped state preceded native cleanup"
                    assert not report["browser_errors"], report["browser_errors"]
                finally:
                    browser.close()
    except BaseException as error:
        primary = error
        report["error"] = repr(error)
        raise
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        raise AssertionError("server failed bounded TERM cleanup")
                report["server_exit"] = process.returncode
                assert not any(map(identity_alive, report["native_identities"])), "QEMU identity survived cleanup"
                if primary is None:
                    assert process.returncode == 130, process.returncode
                    report["status"] = "passed"
        except BaseException as cleanup:
            report["cleanup_error"] = repr(cleanup)
            if primary is None:
                raise
        finally:
            qmp_directory.cleanup()
            (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
