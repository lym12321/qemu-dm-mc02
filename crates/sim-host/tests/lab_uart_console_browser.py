#!/usr/bin/env python3
"""Browser boundary: raw UART chunks to the actual offline terminal component."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
UART_TEXT = "Array.from(document.querySelectorAll('.uart-terminal:not([hidden]) .xterm-rows > div')).map(row=>row.textContent.trimEnd()).join('\\n')"


def uart_text(page):
    return page.evaluate(UART_TEXT).rstrip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    report = {'status': 'failed', 'errors': []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = browser.new_page(viewport={'width': 900, 'height': 600})
                page.on('pageerror', lambda error: report['errors'].append(str(error)))
                page.set_content('<div id="uart-rx"></div><span id="count"></span><span id="tx"></span>')
                page.add_style_tag(path=str(ROOT / 'third_party/xterm/xterm.css'))
                page.add_style_tag(content='''[hidden]{display:none!important} #uart-rx{width:100%;height:400px;position:relative}
                    .uart-terminal,.uart-hex{position:absolute;inset:0;margin:0;overflow:auto}''')
                for path in ['third_party/xterm/xterm.js', 'third_party/xterm/addon-fit.js', 'sim/lab/static/uart-console.js']:
                    page.add_script_tag(path=str(ROOT / path))
                page.evaluate("window.view=new UartConsole(document.querySelector('#uart-rx'),document.querySelector('#count'),document.querySelector('#tx'));view.render('uart1',false,true)")

                def receive(data, port='uart1'):
                    page.evaluate('([port,bytes])=>{view.receive(port,new Uint8Array(bytes));view.render(view.endpoint,false,true)}', [port, list(data)])

                def expect(text):
                    page.wait_for_function(f'expected=>({UART_TEXT}).trimEnd()===expected', arg=text)

                # Escape and multi-byte UTF-8 sequences cross actual write calls.
                for fragment in [b'\x1b[31', b'mR\xe4', b'\xb8', b'\xad\x1b[0m\nabc\bX\rZ\x1b[K']:
                    receive(fragment)
                expect('R中\nZ')
                red = page.locator('.uart-terminal:not([hidden]) .xterm-fg-1')
                assert ''.join(red.all_inner_texts()) == 'R中'
                report['red_color'] = red.first.evaluate('e=>getComputedStyle(e).color')
                receive(b'\x1b[2J\x1b[HNEW\x1b[2;4H\x1b[38;2;10;20;30mXY\x1b[0m')
                expect('NEW\n   XY')
                color = page.locator('.uart-terminal:not([hidden]) .xterm-rows span').filter(has_text='XY')
                assert color.evaluate('e=>getComputedStyle(e).color') == 'rgb(10, 20, 30)'

                receive(b'\x1b[32mSECOND', 'uart2')
                page.evaluate("view.render('uart2',false,true)")
                expect('SECOND')
                page.evaluate("view.render('uart1',false,true)")
                expect('NEW\n   XY')
                receive(b'\x00')
                page.evaluate("view.render('uart1',true,true)")
                raw = page.locator('.uart-hex').inner_text()
                assert '1B 5B 33 31 6D 52 E4 B8 AD' in raw and raw.endswith('00')
                page.evaluate("view.render('uart1',false,true)")
                expect('NEW\n   XY')

                # Clear during a queued partial sequence must not contaminate
                # the next screen, decoder, or independent UART.
                page.evaluate("view.receive('uart1',new Uint8Array([27,91,51,49,0xe4]));view.clear('uart1');view.receive('uart1',new TextEncoder().encode('fresh'));view.render('uart1',false,true)")
                expect('fresh')
                page.evaluate("view.render('uart2',false,true)")
                expect('SECOND')
                page.set_viewport_size({'width': 390, 'height': 600})
                page.wait_for_function("document.querySelector('.uart-terminal:not([hidden]) .xterm-screen').clientWidth<=document.querySelector('#uart-rx').clientWidth")
                page.evaluate("view.render('uart1',false,true)")
                expect('fresh')

                page.evaluate("view.receive('uart1',new Uint8Array(262144));view.receive('uart1',new Uint8Array([65]));view.render('uart1',false,true)")
                assert '终端积压' in page.locator('#count').inner_text()
                page.evaluate("view.render('uart1',true,true)")
                assert len(page.locator('.uart-hex').inner_text().split()) == 262144
                page.evaluate("view.clear('uart1');view.receive('uart1',new TextEncoder().encode('recovered'));view.render('uart1',false,true)")
                expect('recovered')
                assert page.locator('#count').inner_text() == 'RX 9 B'
                page.screenshot(path=str(args.output / 'terminal.png'))
                assert not report['errors'], report['errors']
                report['status'] = 'passed'
            finally:
                browser.close()
    finally:
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
