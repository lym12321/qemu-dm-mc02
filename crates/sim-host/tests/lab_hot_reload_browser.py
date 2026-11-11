#!/usr/bin/env python3
"""Chromium consumer: local ELF changes replace the native Lab session."""
import argparse
import json
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
from lab_uart_console_browser import UART_TEXT, uart_text
from lab_web_native import identities


def identity_alive(identity):
    pid, started = identity
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19] == started
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser()
    for option in ["sim", "qemu", "elf", "adc", "output"]:
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    report = {"status": "failed", "sessions": [], "native_identities": [], "browser_errors": []}
    process = None
    primary = None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    project_path = args.output.resolve() / "sim.lab.json"
    firmware = args.output.resolve() / "build.elf"
    firmware.write_bytes(args.elf.read_bytes())

    def state():
        with urlopen(url + "/api/state", timeout=1) as response:
            return json.load(response)

    try:
        with (args.output / "stdout").open("wb") as stdout, (args.output / "stderr").open("wb") as stderr:
            process = subprocess.Popen([str(args.sim.resolve()), "lab", "web", "--port", str(port),
                "--project", str(project_path), "--qemu", str(args.qemu.resolve()), "--timeout", "60"],
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
                    page.wait_for_function("!document.querySelector('#settings-apply').disabled")
                    page.locator("#settings-button").click()
                    page.wait_for_function("document.querySelector('#settings-dialog').open")
                    assert page.locator("#project-watch").evaluate("element=>element.disabled && !element.checked")
                    page.locator("#project-firmware").locator("input").fill(str(firmware))
                    page.wait_for_function("!document.querySelector('#project-watch').disabled")
                    page.locator("#project-watch").click()
                    page.screenshot(path=str(args.output / "settings-watch.png"), animations="disabled")
                    page.locator("#settings-apply").click()
                    page.wait_for_function("!document.querySelector('#settings-dialog').open")
                    saved = json.loads(project_path.read_text())
                    assert saved["firmware"] == str(firmware) and saved["watch_elf"]
                    page.locator("#primary-button").click()
                    page.wait_for_function(f"({UART_TEXT}).includes('SIM_READY minimal-board\\n')")
                    original = state()
                    original_native = identities(process.pid)
                    assert original["status"] == "running" and original["config"]["watch_elf"]
                    assert len(original_native) == 1
                    report["sessions"].append(original["session_id"])
                    report["native_identities"].extend(original_native)

                    # An unfinished linker output is reported while the loaded
                    # image keeps running; a later valid image clears the error.
                    firmware.write_bytes(b"incomplete ELF")
                    page.wait_for_function("!document.querySelector('#session-error').hidden")
                    invalid = state()
                    assert invalid["reload_error"] and invalid["error"] is None
                    assert invalid["session_id"] == original["session_id"] and invalid["status"] == "running"
                    assert identities(process.pid) == original_native
                    report["invalid_input_error"] = page.locator("#session-error").inner_text()

                    # Replacing the actual build path must reach the browser as
                    # a new native session and the second guest's UART output.
                    replacement = firmware.with_suffix(".new")
                    replacement.write_bytes(args.adc.read_bytes())
                    replacement.replace(firmware)
                    page.wait_for_function(f"({UART_TEXT}).includes('LAB_READY\\n')")
                    reloaded = state()
                    reloaded_native = identities(process.pid)
                    assert reloaded["status"] == "running" and reloaded["session_id"] != original["session_id"]
                    assert len(reloaded_native) == 1 and reloaded_native != original_native
                    assert not any(map(identity_alive, original_native)), "old QEMU identity survived reload"
                    assert page.locator("#session-error").is_hidden()
                    report["sessions"].append(reloaded["session_id"])
                    report["native_identities"].extend(reloaded_native)
                    report["uart_markers"] = ["SIM_READY minimal-board\n", "LAB_READY\n"]
                    page.screenshot(path=str(args.output / "reloaded.png"), animations="disabled")

                    page.locator("#stop-button").click()
                    page.wait_for_function("document.querySelector('#session-status').textContent==='已停止'")
                    assert not identities(process.pid), "stopped state preceded native cleanup"
                    firmware.write_bytes(args.elf.read_bytes())
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        observed = state()
                        assert observed["status"] == "stopped" and observed["session_id"] == reloaded["session_id"]
                        assert not identities(process.pid), "file change restarted a stopped experiment"
                        time.sleep(.05)
                    report["stop_change_observation_ms"] = 1500

                    # Upload chooses a copy, so its original path cannot remain
                    # selected for local build watching.
                    with page.expect_file_chooser() as chooser:
                        page.locator("#upload-button").click()
                    chooser.value.set_files(str(args.adc.resolve()))
                    page.wait_for_function("name=>document.querySelector('#firmware-name').textContent===name", arg=args.adc.name)
                    page.locator("#settings-button").click()
                    page.wait_for_function("document.querySelector('#settings-dialog').open")
                    assert page.locator("#project-firmware").evaluate("element=>element.value") == ""
                    assert page.locator("#project-watch").evaluate("element=>element.disabled && !element.checked")
                    report["upload_disables_watch"] = True
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
            (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
