#!/usr/bin/env python3
"""Chromium consumer of the native Lab controls and UART/CAN assistant."""
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


def main():
    parser = argparse.ArgumentParser()
    for option in ["sim", "qemu", "elf", "output"]:
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir()
    report = {"status": "failed", "native_identities": [], "browser_errors": []}
    process = None
    primary = None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    project_path = args.output.resolve() / "sim.lab.json"
    firmware = args.output.resolve() / "firmware.elf"
    firmware.write_bytes(args.elf.read_bytes())
    try:
        with (args.output / "stdout").open("wb") as stdout, (args.output / "stderr").open("wb") as stderr:
            process = subprocess.Popen([str(args.sim.resolve()), "lab", "web", "--port", str(port),
                "--project", str(project_path), "--elf", str(firmware),
                "--qemu", str(args.qemu.resolve()), "--timeout", "90"],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    raise AssertionError(f"server exited: {process.returncode}")
                try:
                    with urlopen(url + "/api/state", timeout=.5) as response:
                        assert json.load(response)["status"] == "idle"
                    break
                except OSError:
                    assert time.monotonic() < deadline, "server startup deadline"
                    time.sleep(.02)
            assert not project_path.exists() and not identities(process.pid)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page(viewport={"width": 1366, "height": 768})
                    page.set_default_timeout(6000)
                    page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                    page.goto(url)
                    page.wait_for_function("!document.querySelector('#primary-button').disabled")
                    assert page.locator("#primary-button").inner_text() == "启动"
                    page.locator("#primary-button").click()
                    page.wait_for_function(f"({UART_TEXT}).includes('LAB_READY\\n')")
                    initial = identities(process.pid)
                    assert len(initial) == 1
                    report["native_identities"].extend(initial)
                    geometry = page.evaluate("""() => ({width:document.documentElement.scrollWidth,
                        height:document.documentElement.scrollHeight,
                        receive:document.querySelector('#uart-rx').getBoundingClientRect().height})""")
                    page.screenshot(path=str(args.output / "lab-initial-1366.png"), animations="disabled")
                    assert geometry["width"] == 1366 and geometry["height"] <= 768 and geometry["receive"] >= 420, geometry
                    report["geometry"] = geometry

                    def value(selector, text):
                        page.locator(selector).evaluate("(element,value)=>{element.value=value;element.dispatchEvent(new Event('change'));}", text)

                    def choose(selector, option):
                        page.locator(selector).click()
                        page.locator(f'{selector} mdui-menu-item[value="{option}"]').click()
                        page.wait_for_function("([selector,value])=>document.querySelector(selector).value===value", arg=[selector, option])

                    def segment(selector, option):
                        page.locator(f'{selector} mdui-segmented-button[value="{option}"]').click()
                        assert page.locator(selector).evaluate("element=>element.value") == option

                    def peripheral(kind):
                        page.locator("#peripherals-button").click()
                        page.wait_for_function("document.querySelector('#peripherals-dialog').open")
                        page.locator(f'#peripheral-tabs mdui-tab[value="{kind}"]').click()

                    def close_peripheral():
                        page.locator("#peripheral-close").click()
                        page.wait_for_function("!document.querySelector('#peripherals-dialog').open && document.activeElement?.id==='peripherals-button'")

                    # A selected format remains selected when clicked again.
                    segment("#uart-mode", "text")
                    segment("#uart-receive-format", "text")

                    # RX is continuous; changing ports leaves the original capture intact.
                    before = uart_text(page)
                    choose("#uart-endpoint", "uart2")
                    assert uart_text(page) == ""
                    choose("#uart-endpoint", "uart1")
                    assert uart_text(page).startswith(before)
                    value("#uart-data", "q")
                    page.locator("#uart-data").locator("input, textarea").focus()
                    page.keyboard.press("Control+Enter")
                    page.wait_for_function(f"({UART_TEXT}).includes('UART=q\\n')")
                    page.wait_for_function("document.querySelector('#uart-tx-status').textContent==='TX 1 B · 已接受'")
                    # A raw NUL survives transport and remains visible in HEX.
                    segment("#uart-mode", "hex")
                    value("#uart-data", "00")
                    page.locator("#uart-send").click()
                    segment("#uart-receive-format", "hex")
                    page.wait_for_function("document.querySelector('.uart-hex').textContent.includes('55 41 52 54 3D 00 0A')")
                    segment("#uart-receive-format", "text")
                    # UART5 is the board RX: host transmission stays enabled.
                    choose("#uart-endpoint", "uart5")
                    assert page.locator("#uart-send").evaluate("element=>!element.disabled")
                    choose("#uart-endpoint", "uart1")
                    value("#uart-data", "0")
                    page.locator("#uart-send").click()
                    page.wait_for_function("!document.querySelector('#uart-error').hidden")
                    assert "字节对" in page.locator("#uart-error").inner_text()
                    assert page.locator("#session-error").is_hidden()
                    segment("#uart-mode", "text")
                    value("#uart-data", "q")
                    page.locator("#uart-send").click()
                    page.wait_for_function("document.querySelector('#uart-error').hidden")

                    # The actual guest USART emits terminal controls on '~'.
                    value("#uart-data", "~")
                    page.locator("#uart-send").click()
                    page.wait_for_function(f"({UART_TEXT}).trimEnd()==='RED\\nZ\\n   RGB'")
                    colored = page.locator('.uart-terminal:not([hidden]) .xterm-rows span').filter(has_text="RGB")
                    assert colored.evaluate("e=>getComputedStyle(e).color") == "rgb(10, 20, 30)"
                    assert page.locator('.uart-terminal:not([hidden]) .xterm-fg-1').inner_text() == "RED"
                    report["native_uart_terminal"] = uart_text(page)
                    page.screenshot(path=str(args.output / "uart-terminal.png"))

                    # Keyboard ownership reaches the native GPIO, then releases it.
                    key = page.locator("#key-button").locator("button")
                    key.focus()
                    page.keyboard.down("Space")
                    try:
                        page.wait_for_function("document.querySelector('#key-button').getAttribute('aria-pressed')==='true'")
                        page.wait_for_function("document.querySelector('#led-value').textContent==='#FFFFFF'")
                    finally:
                        page.keyboard.up("Space")
                    page.wait_for_function("document.querySelector('#key-button').getAttribute('aria-pressed')==='false'")
                    page.wait_for_function("document.querySelector('#led-value').textContent==='#000000'")
                    assert page.locator("#board-led").evaluate("element=>getComputedStyle(element).backgroundColor") == "rgb(0, 0, 0)"

                    peripheral("gpio")
                    value("#gpio-level", "1")
                    page.locator("#gpio-send").click()
                    page.wait_for_function("document.querySelector('#pin-summary').textContent.includes('采样 1')")
                    page.locator('#peripheral-tabs mdui-tab[value="adc"]').click()
                    value("#adc-channel", "4")
                    value("#adc-value", "12345")
                    page.locator("#adc-send").click()
                    page.wait_for_function(f"({UART_TEXT}).includes('ADC=12345\\n')")
                    page.screenshot(path=str(args.output / "peripherals-1366.png"), animations="disabled")
                    close_peripheral()
                    page.locator("#primary-button").click()
                    page.wait_for_function("document.querySelector('#primary-button').textContent==='继续'")
                    peripheral("adc")
                    page.wait_for_function("document.querySelector('#adc-send').disabled")
                    close_peripheral()
                    page.locator("#primary-button").click()
                    page.wait_for_function("document.querySelector('#primary-button').textContent==='暂停'")

                    page.locator('#channel-tabs mdui-tab[value="can"]').click()
                    page.locator("#can-send").click()
                    page.wait_for_function("[...document.querySelectorAll('#can-records tr')].some(row=>row.dataset.direction==='RX'&&row.textContent.includes('0x322'))")
                    rows = page.locator("#can-records tr").all_text_contents()
                    assert any("TX" in row and "0x321" in row and "41 42 43 44 45 46 47 48" in row for row in rows), rows
                    assert any("RX" in row and "0x322" in row and "41 42 43 44 45 46 47 48" in row for row in rows), rows
                    report["can_rows"] = rows

                    page.locator("#settings-button").click()
                    page.wait_for_function("document.querySelector('#settings-dialog').open")
                    page.locator("#advanced-settings").click()
                    page.locator("#project-timeout").locator("input").fill("80")
                    page.locator("#settings-apply").click()
                    page.wait_for_function("!document.querySelector('#settings-dialog').open")
                    assert json.loads(project_path.read_text())["timeout_secs"] == 80
                    page.locator('#channel-tabs mdui-tab[value="uart"]').click()
                    page.locator("#events-button").click()
                    page.wait_for_function("document.querySelector('#events-dialog').open")
                    page.locator("#event-log").wait_for(state="visible")
                    page.locator("#events-close").click()
                    page.wait_for_function("!document.querySelector('#events-dialog').open")
                    page.locator("#event-log").wait_for(state="hidden")
                    page.screenshot(path=str(args.output / "lab-1366.png"), animations="disabled")

                    page.set_viewport_size({"width": 390, "height": 844})
                    mobile = page.evaluate("""() => ({width:document.documentElement.scrollWidth,
                        receive:document.querySelector('#uart-rx').getBoundingClientRect().height})""")
                    assert mobile["width"] == 390 and mobile["receive"] > 200, mobile
                    report["mobile_geometry"] = mobile
                    page.screenshot(path=str(args.output / "lab-390.png"), full_page=True, animations="disabled")
                    page.locator('#channel-tabs mdui-tab[value="can"]').click()
                    mobile_can = page.evaluate("""() => ({width:document.documentElement.scrollWidth,
                        receive:document.querySelector('#can-rx').getBoundingClientRect().height})""")
                    assert mobile_can["width"] == 390 and mobile_can["receive"] > 100, mobile_can
                    report["mobile_can_geometry"] = mobile_can
                    page.screenshot(path=str(args.output / "lab-can-390.png"), full_page=True, animations="disabled")
                    page.locator('#channel-tabs mdui-tab[value="uart"]').click()
                    page.set_viewport_size({"width": 1366, "height": 768})

                    # Admission errors survive later state updates and leave the
                    # running board intact; the next operation clears the error.
                    firmware.unlink()
                    try:
                        page.locator("#restart-button").click()
                        page.wait_for_function("!document.querySelector('#session-error').hidden && !document.querySelector('#restart-button').disabled")
                        message = page.locator("#session-error").inner_text()
                        observed = page.locator("#virtual-time").inner_text()
                        page.wait_for_function("value=>document.querySelector('#virtual-time').textContent!==value", arg=observed)
                        assert page.locator("#session-error").inner_text() == message
                        assert identities(process.pid) == initial
                    finally:
                        firmware.write_bytes(args.elf.read_bytes())

                    # Rapid host clicks can precede MDUI's disabled render. One
                    # reload is admitted, and the subsequent stop cancels it.
                    delays = [0, 5, 10, 20, 30, 50]
                    for delay in delays:
                        enabled = page.evaluate("""delay => new Promise(resolve => {
                            const restart = document.querySelector('#restart-button');
                            for (let i=0; i<4; i++) restart.shadowRoot.querySelector('button').click();
                            const stop = () => {
                                const control = document.querySelector('#stop-button');
                                const button = control.shadowRoot.querySelector('button');
                                const enabled = !control.disabled && !button.disabled;
                                button.click();
                                document.querySelector('#primary-button').click();
                                restart.click();
                                resolve(enabled);
                            };
                            if (delay) setTimeout(stop, delay); else stop();
                        })""", delay)
                        assert enabled, f"stop disabled during reload at {delay} ms"
                        page.wait_for_function("document.querySelector('#session-status').textContent==='已停止' && !document.querySelector('#primary-button').disabled")
                        assert not identities(process.pid), "burst stop preceded native cleanup"
                        assert page.locator("#session-error").is_hidden()
                        page.evaluate("() => {for (let i=0; i<4; i++) document.querySelector('#primary-button').click();}")
                        page.wait_for_function("document.querySelector('#session-status').textContent==='运行中' && !document.querySelector('#restart-button').disabled")
                        started = identities(process.pid)
                        assert len(started) == 1
                        report["native_identities"].extend(started)
                    report["lifecycle_stop_delays_ms"] = delays

                    page.locator("#restart-button").click()
                    page.wait_for_function("document.querySelector('#session-status').textContent==='运行中' && !document.querySelector('#restart-button').disabled")
                    replaced = identities(process.pid)
                    assert len(replaced) == 1 and replaced != initial
                    report["native_identities"].extend(replaced)
                    page.locator("#stop-button").click()
                    page.wait_for_function("document.querySelector('#session-status').textContent==='已停止'")
                    assert not identities(process.pid), "stopped state preceded native cleanup"

                    # The main file button selects and launches an uploaded ELF,
                    # replacing the project's previous local firmware path.
                    uploaded = args.output.resolve() / "uploaded-board.elf"
                    uploaded.write_bytes(args.elf.read_bytes())
                    with page.expect_file_chooser() as chooser:
                        page.locator("#upload-button").click()
                    chooser.value.set_files(str(uploaded))
                    page.wait_for_function("document.querySelector('#firmware-name').textContent==='uploaded-board.elf'")
                    page.locator("#primary-button").click()
                    page.wait_for_function("document.querySelector('#session-status').textContent==='运行中' && !document.querySelector('#restart-button').disabled")
                    page.wait_for_function(f"({UART_TEXT}).includes('LAB_READY\\n')")
                    with urlopen(url + "/api/state", timeout=1) as response:
                        uploaded_state = json.load(response)
                    assert uploaded_state["config"]["firmware"] != str(args.elf.resolve()), uploaded_state
                    assert page.locator("#firmware-name").inner_text() == "uploaded-board.elf"
                    report["uploaded_firmware"] = uploaded_state["config"]["firmware"]
                    uploaded_identities = identities(process.pid)
                    assert len(uploaded_identities) == 1 and uploaded_identities != replaced
                    report["native_identities"].extend(uploaded_identities)
                    page.locator("#restart-button").click()
                    page.wait_for_function("document.querySelector('#session-status').textContent==='运行中' && !document.querySelector('#restart-button').disabled")
                    assert page.locator("#firmware-name").inner_text() == "uploaded-board.elf"
                    uploaded_reloaded = identities(process.pid)
                    assert len(uploaded_reloaded) == 1 and uploaded_reloaded != uploaded_identities
                    report["native_identities"].extend(uploaded_reloaded)
                    page.locator("#stop-button").click()
                    page.wait_for_function("document.querySelector('#session-status').textContent==='已停止'")
                    assert not identities(process.pid), "uploaded experiment stopped before native cleanup"
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
                assert all(not Path(f"/proc/{pid}").exists() for pid, _ in report["native_identities"])
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
