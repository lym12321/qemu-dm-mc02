#!/usr/bin/env python3
"""Visible MDUI USB actions against actual firmware, Linux vhci and host tools."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smoke import dm_mc02_web_smoke as web
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('sim', 'qemu', 'elf', 'trobot', 'dependencies', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--usbip-port', type=int, default=33241)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'failed', 'server': {}, 'inputs': {
        str(p): web.sha(p) for p in (args.sim, args.qemu, args.elf)}}
    before = Path('/sys/devices/platform/vhci_hcd.0/status').read_text()
    server = web.Server(time.monotonic() + 60, report['server'], args.sim.resolve())
    playwright = browser = page = None
    primary = None
    try:
        server.start(args.qemu.resolve(), ['--elf', str(args.elf.resolve()),
                     '--usbip', f'127.0.0.1:{args.usbip_port}'])
        url = server.ready()
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url, wait_until='networkidle')
        web.wait_for(page, server, lambda: web.button(page, 'load-button').is_enabled(), 'load control')
        web.button(page, 'load-button').click()
        web.wait_for(page, server, lambda: web.status(page) == 'paused', 'native reset pause')
        # pointerdown prevents focus movement. Hold the visible USER button
        # while activating the already-focused Run button with Enter.
        web.button(page, 'run-button').focus()
        web.button(page, 'key-button').hover()
        page.mouse.down()
        web.wait_for(page, server, lambda: web.board(page)['key']['pressed'], 'held USER key')
        page.keyboard.press('Enter')
        web.wait_for(page, server, lambda: web.status(page) == 'running' and
                     int(web.board(page)['timestamp_ns']) > 200_000_000, 'debug boot')
        page.mouse.up()
        web.wait_for(page, server, lambda: web.button(page, 'usb-attach').is_enabled(), 'USB available')
        web.button(page, 'usb-attach').click()
        web.wait_for(page, server, lambda: web.button(page, 'usb-detach').is_enabled(), 'OS USB attachment')
        assert page.locator('#usb-status').text_content() == '已连接'
        consumer = subprocess.run(['sudo', '-n', str(Path(sys.executable).absolute()),
            str(ROOT / 'smoke/trobot_usb_os_consumer.py'), '--trobot', str(args.trobot),
            '--dependencies', str(args.dependencies)], capture_output=True, text=True, timeout=15)
        report['os_consumer'] = {'exit': consumer.returncode, 'stdout': consumer.stdout, 'stderr': consumer.stderr}
        assert consumer.returncode == 0, report['os_consumer']
        page.locator('#usb-status').scroll_into_view_if_needed()
        page.screenshot(path=str(args.output / 'usb-attached.png'), full_page=True)
        report['screenshot'] = str(args.output / 'usb-attached.png')
        web.button(page, 'usb-detach').click()
        web.wait_for(page, server, lambda: web.button(page, 'usb-attach').is_enabled(), 'USB detached')
        web.button(page, 'usb-attach').click()
        web.wait_for(page, server, lambda: web.button(page, 'usb-detach').is_enabled(), 'USB reattached')
        web.button(page, 'stop-button').click()
        web.wait_for(page, server, lambda: web.status(page) == 'stopped', 'stop while attached')
        assert web.button(page, 'usb-attach').is_disabled()
        assert web.button(page, 'usb-detach').is_disabled()
        assert Path('/sys/devices/platform/vhci_hcd.0/status').read_text() == before, 'vhci port survived Stop'
        assert not errors, errors
        report['page_errors'] = errors
        report['actions'] = ['attach', 'detach', 'reattach', 'stop_while_attached']
    except BaseException as error:
        primary = error
        report['error'] = str(error)
        if page is not None:
            report['last_state'] = page.request.get(url + '/api/state').json()
            page.screenshot(path=str(args.output / 'failed.png'), full_page=True)
        raise
    finally:
        try:
            web.cleanup(primary, [('Chromium', lambda: browser and browser.close()),
                ('Playwright', lambda: playwright and playwright.stop()), ('Web server', server.close)])
            assert Path('/sys/devices/platform/vhci_hcd.0/status').read_text() == before, 'vhci port leaked'
            if primary is None:
                report['status'] = 'passed'
        finally:
            (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
