#!/usr/bin/env python3
"""Chromium consumer of initialization, one-click launch and native cancellation."""
import argparse
import json
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
from lab_uart_console_browser import UART_TEXT, uart_text
from lab_hot_reload_browser import identity_alive
from lab_firmware_replace_browser import qmp_system_reset
from lab_web_native import identities


def main():
    parser = argparse.ArgumentParser()
    for option in ["sim", "qemu", "elf", "adc", "output"]:
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    output = args.output.resolve()
    allow = output / "allow-qemu"
    started = output / "qemu-started"
    wrapper = output / "qemu-gated.sh"
    wrapper.write_text("#!/bin/sh\nprintf '%s\\n' \"$$\" > " + shlex.quote(str(started))
        + "\nwhile [ ! -f " + shlex.quote(str(allow)) + " ]; do sleep .01; done\nexec "
        + shlex.quote(str(args.qemu.resolve())) + ' "$@"\n')
    wrapper.chmod(0o755)
    uploaded = output / "selected-board.elf"
    uploaded.write_bytes(args.adc.read_bytes())
    empty_upload = output / "empty.elf"
    empty_upload.touch()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    report = {"status": "failed", "requests": [], "reset_events": [], "native_identities": [], "browser_errors": []}
    process = None
    primary = None
    qmp_directory = tempfile.TemporaryDirectory(prefix="lab-startup-")
    qmp_path = Path(qmp_directory.name) / "qmp.sock"

    def state():
        with urlopen(url + "/api/state", timeout=1) as response:
            return json.load(response)

    def hold(response_list):
        def handler(route):
            response_list.append((route, route.fetch()))
        return handler

    try:
        with (output / "stdout").open("wb") as stdout, (output / "stderr").open("wb") as stderr:
            process = subprocess.Popen([str(args.sim.resolve()), "lab", "web", "--port", str(port),
                "--elf", str(args.elf.resolve()), "--qemu", str(wrapper), "--qmp", str(qmp_path), "--timeout", "60"],
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
                    page.add_init_script("""const NativeWebSocket = window.WebSocket;
                        window.testStateFrames = 0;
                        window.WebSocket = class extends NativeWebSocket {
                            constructor(...args) {
                                super(...args); window.testSocket = this;
                                this.addEventListener('message', event => {
                                    if (JSON.parse(event.data).type === 'state') window.testStateFrames++;
                                });
                            }
                        };""")
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                    def received(data):
                        event = json.loads(data)
                        if event.get("type") == "reset":
                            report["reset_events"].append(event)

                    def track_socket(websocket):
                        websocket.on("framesent", lambda data: report["requests"].append(json.loads(data)))
                        websocket.on("framereceived", received)

                    page.on("websocket", track_socket)

                    def click_across_state_updates(selector):
                        # A normal press can span the periodic state feed.
                        # Fast locator.click hid discarded clicks when an
                        # unchanged slotted button label was replaced.
                        control = page.locator(selector)
                        control.click(trial=True)  # Wait for any dialog's closing overlay.
                        box = control.bounding_box()
                        assert box, selector
                        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        page.mouse.down()
                        baseline = page.evaluate("window.testStateFrames")
                        try:
                            page.wait_for_function("before=>window.testStateFrames>=before+2", arg=baseline)
                        finally:
                            page.mouse.up()

                    held_project = []
                    page.route("**/api/project", hold(held_project))
                    page.goto(url)
                    page.wait_for_function("!!document.querySelector('#upload-button').shadowRoot?.querySelector('button')")
                    assert page.locator("#upload-button").evaluate("element=>element.disabled")
                    assert page.locator("#settings-button").evaluate("element=>element.disabled")
                    assert page.locator("#primary-button").evaluate("element=>element.disabled")
                    assert page.locator("#session-progress").is_visible()
                    assert not page.locator("#session-progress").evaluate("element=>element.hasAttribute('value')")
                    assert not identities(process.pid)
                    assert len(held_project) == 1
                    held_project[0][0].fulfill(response=held_project[0][1])
                    page.wait_for_function("!document.querySelector('#primary-button').disabled")
                    assert page.locator("#session-progress").is_hidden()

                    # Hold real native acquisition, rather than fabricating a
                    # slow state frame. Feedback and Stop must change in the
                    # same click task before the server publishes starting.
                    immediate = page.evaluate("""() => {
                        const primary = document.querySelector('#primary-button');
                        for (let i=0;i<4;i++) primary.shadowRoot.querySelector('button').click();
                        return {label:primary.textContent,disabled:primary.disabled,
                            progress:!document.querySelector('#session-progress').hidden,
                            stop:!document.querySelector('#stop-button').hidden&&!document.querySelector('#stop-button').disabled};
                    }""")
                    assert immediate == {"label": "启动中…", "disabled": True, "progress": True, "stop": True}, immediate
                    page.wait_for_function("document.querySelector('#session-status').textContent==='启动中'")
                    deadline = time.monotonic() + 3
                    while not started.exists():
                        assert time.monotonic() < deadline, "native launch did not reach acquisition gate"
                        page.wait_for_timeout(10)
                    page.screenshot(path=str(output / "starting.png"), animations="disabled")
                    click_across_state_updates("#stop-button")
                    page.wait_for_function("document.querySelector('#session-status').textContent==='已停止'&&!document.querySelector('#primary-button').disabled")
                    assert not identities(process.pid), "cancel completed before native cleanup"
                    assert [request["action"] for request in report["requests"]] == ["start", "stop"]
                    report["immediate_start"] = immediate

                    click_across_state_updates("#primary-button")
                    page.wait_for_function("document.querySelector('#session-status').textContent==='启动中'")
                    allow.touch()
                    page.wait_for_function(f"({UART_TEXT}).includes('SIM_READY minimal-board\\n')")
                    page.wait_for_function("document.querySelector('#primary-button').textContent==='暂停'")
                    assert state()["config"]["imu"] == "stationary"
                    original_native = identities(process.pid)
                    assert len(original_native) == 1
                    report["native_identities"].extend(original_native)
                    assert page.locator("#session-progress").is_hidden()
                    traffic_start = len(report["requests"])
                    native_session = state()["session_id"]
                    for payload in ["q", "hi", "abc"]:
                        page.locator("#uart-data").locator("input, textarea").fill(payload)
                        page.locator("#uart-send").click()
                        page.wait_for_function("bytes=>document.querySelector('#uart-tx-status').textContent===`TX ${bytes} B · 已接受`", arg=len(payload))
                    page.locator('#channel-tabs mdui-tab[value="can"]').click()
                    assert page.locator("#console-can").is_visible()
                    page.locator('#channel-tabs mdui-tab[value="uart"]').click()
                    assert page.locator("#console-uart").is_visible()
                    report["uart_tab_actions"] = [request["action"] for request in report["requests"][traffic_start:]]
                    assert report["uart_tab_actions"] == ["uart", "uart", "uart"]
                    after_tabs = state()
                    assert after_tabs["status"] == "running" and after_tabs["session_id"] == native_session
                    assert identities(process.pid) == original_native
                    page.locator("#uart-clear").click()
                    assert qmp_system_reset(qmp_path) == {}
                    page.wait_for_function(f"({UART_TEXT}).includes('SIM_READY minimal-board\\n')")
                    page.locator("#events-button").click()
                    page.wait_for_function("document.querySelector('#event-log').textContent.includes('仿真输入已重新初始化（RESET）')")
                    page.locator("#events-close").click()
                    reset_state = state()
                    assert reset_state["status"] == "running" and reset_state["session_id"] == native_session
                    assert reset_state["config"]["imu"] == "stationary"
                    assert reset_state["error"] is None
                    assert identities(process.pid) == original_native
                    assert len(report["reset_events"]) == 1
                    assert report["reset_events"][0]["endpoint"] == "cosim"
                    assert [request["action"] for request in report["requests"][traffic_start:]] == ["uart", "uart", "uart"]
                    report["reset_preserved_session"] = native_session
                    click_across_state_updates("#primary-button")
                    page.wait_for_function("document.querySelector('#primary-button').textContent==='继续'")
                    click_across_state_updates("#primary-button")
                    page.wait_for_function("document.querySelector('#primary-button').textContent==='暂停'")
                    allow.unlink()
                    immediate_reload = page.evaluate("""() => {
                        document.querySelector('#restart-button').shadowRoot.querySelector('button').click();
                        return {label:document.querySelector('#primary-button').textContent,
                            progress:!document.querySelector('#session-progress').hidden,
                            stop:!document.querySelector('#stop-button').hidden&&!document.querySelector('#stop-button').disabled};
                    }""")
                    assert immediate_reload == {"label": "重载中…", "progress": True, "stop": True}, immediate_reload
                    report["immediate_reload"] = immediate_reload
                    immediate_stop = page.evaluate("""() => {
                        document.querySelector('#stop-button').shadowRoot.querySelector('button').click();
                        return {label:document.querySelector('#stop-button').textContent,
                            progress:!document.querySelector('#session-progress').hidden};
                    }""")
                    assert immediate_stop == {"label": "停止中…", "progress": True}, immediate_stop
                    page.wait_for_function("document.querySelector('#session-status').textContent==='已停止'")
                    assert not identities(process.pid)
                    allow.touch()
                    upload_baseline = len(report["requests"])

                    held_upload = []
                    # Chromium retains file bodies that route.fetch cannot
                    # replay. Pause the original upload, then continue it intact.
                    page.route("**/api/firmware", lambda route: held_upload.append(route))

                    def upload_and_click_start(file):
                        before = len(report["requests"])
                        index = len(held_upload)
                        box = page.locator("#primary-button").bounding_box()
                        with page.expect_file_chooser() as chooser:
                            click_across_state_updates("#upload-button")
                        chooser.value.set_files(str(file))
                        assert page.locator("#primary-button").inner_text() == "加载后启动"
                        assert not page.locator("#primary-button").evaluate("element=>element.disabled")
                        # A physical click immediately after chooser dismissal
                        # must survive upload latency; locator.click would wait
                        # for the old disabled button and hide this regression.
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        assert page.locator("#primary-button").inner_text() == "加载中…"
                        assert page.locator("#primary-button").evaluate("element=>element.disabled")
                        assert page.locator("#session-progress").is_visible()
                        assert page.locator("#stop-button").is_visible()
                        assert not page.locator("#stop-button").evaluate("element=>element.disabled")
                        deadline = time.monotonic() + 3
                        while len(held_upload) == index:
                            assert time.monotonic() < deadline, "upload did not reach server"
                            page.wait_for_timeout(10)
                        assert len(report["requests"]) == before, "started the previous ELF before upload completed"
                        return held_upload[index]

                    canceled = upload_and_click_start(uploaded)
                    click_across_state_updates("#stop-button")
                    assert page.locator("#primary-button").inner_text() == "加载后启动"
                    canceled.continue_()
                    page.wait_for_function("document.querySelector('#upload-button').textContent==='打开 ELF'")
                    assert state()["status"] == "stopped" and not identities(process.pid)
                    assert len(report["requests"]) == upload_baseline
                    report["upload_start_canceled"] = True

                    rejected = upload_and_click_start(empty_upload)
                    rejected.continue_()
                    page.wait_for_function("document.querySelector('#session-error').textContent.includes('nonempty')")
                    assert not page.locator("#primary-button").evaluate("element=>element.disabled")
                    assert state()["status"] == "stopped" and not identities(process.pid)
                    assert len(report["requests"]) == upload_baseline, "failed upload launched the previous ELF"
                    report["upload_failure_clears_start"] = True

                    disconnected = upload_and_click_start(uploaded)
                    page.evaluate("window.testSocket.close()")
                    page.wait_for_function("document.querySelector('#connection-status').textContent==='重新连接…'")
                    disconnected.continue_()
                    page.wait_for_function("document.querySelector('#upload-button').textContent==='打开 ELF'&&!document.querySelector('#upload-button').disabled")
                    assert state()["status"] == "stopped" and not identities(process.pid)
                    assert len(report["requests"]) == upload_baseline, "disconnect retained the upload start intent"
                    report["upload_disconnect_clears_start"] = True

                    page.locator("#settings-button").click()
                    page.wait_for_function("document.querySelector('#settings-dialog').open")
                    page.locator("#advanced-settings").click()
                    page.locator("#project-imu").click()
                    page.locator('#project-imu mdui-menu-item[value="off"]').click()
                    click_across_state_updates("#settings-apply")
                    page.wait_for_function("!document.querySelector('#settings-dialog').open")
                    successful = upload_and_click_start(uploaded)
                    page.screenshot(path=str(output / "upload-start.png"))
                    successful.continue_()
                    page.wait_for_function(f"({UART_TEXT}).includes('LAB_READY\\n')")
                    loaded = state()
                    assert loaded["config"]["imu"] == "off"
                    assert loaded["config"]["firmware"] != str(args.elf.resolve())
                    assert page.locator("#firmware-name").inner_text() == uploaded.name
                    report["native_identities"].extend(identities(process.pid))
                    click_across_state_updates("#stop-button")
                    page.wait_for_function("document.querySelector('#session-status').textContent==='已停止'")
                    assert not identities(process.pid)
                    assert [request["action"] for request in report["requests"]] == [
                        "start", "stop", "start", "uart", "uart", "uart", "pause", "run", "restart", "stop", "start", "stop"]
                    report["single_click_across_state_updates"] = "passed"
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
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
