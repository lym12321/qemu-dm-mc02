#!/usr/bin/env python3
"""Independent HTTP/WebSocket consumer of the compiled native Web Lab."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import signal
import socket
import subprocess
import time

from aiohttp import ClientSession, FormData, WSServerHandshakeError


def form(data, name="lesson.elf", field="firmware"):
    value = FormData()
    value.add_field(field, data, filename=name, content_type="application/octet-stream")
    return value


def identities(pid):
    result = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            children = (task / "children").read_text().split()
        except FileNotFoundError:
            continue
        for child in children:
            path = Path(f"/proc/{child}")
            try:
                argv = (path / "cmdline").read_bytes().split(b"\0")
                stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                continue
            if b"-machine" in argv:
                result.append((int(child), stat[19]))
    return result


async def scenario(url, args, process, report):
    async with ClientSession() as client:
        async with asyncio.timeout(10):
            while True:
                if process.poll() is not None:
                    raise AssertionError(f"server exited early: {process.returncode}")
                try:
                    response = await client.get(url + "/api/state")
                    break
                except OSError:
                    await asyncio.sleep(.01)
        assert (await response.json())["status"] == "idle"
        assert not identities(process.pid), "opening UI started a native MCU"
        assert "libpython" not in Path(f"/proc/{process.pid}/maps").read_text()
        for path, content in [("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")]:
            response = await client.get(url + path)
            assert response.status == 200 and content in response.headers["Content-Type"]
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert await response.read()
        assert (await client.get(url + "/session.py")).status == 404
        for headers in [{"Host":"foreign.example"}, {"Origin":"https://foreign.example"}]:
            response = await client.get(url + "/api/state", headers=headers)
            assert response.status == 403 and "error" in await response.json()
        assert (await client.get(url + "/api/state", headers={"Origin":url})).status == 200
        for data in [b"", b"not multipart"]:
            response = await client.post(url + "/api/firmware", data=data)
            assert response.status == 400
        response = await client.post(url + "/api/firmware", data=form(b""))
        assert response.status == 400
        response = await client.post(url + "/api/firmware", data=form(b"bad", field="other"))
        assert response.status == 400
        duplicate = form(b"first")
        duplicate.add_field("extra", b"second", filename="extra.elf")
        assert (await client.post(url + "/api/firmware", data=duplicate)).status == 400
        for asset in [args.elf, args.adc]:
            response = await client.post(url + "/api/firmware", data=form(asset.read_bytes(), asset.name))
            assert response.status == 200, await response.text()
            assert (await response.json())["firmware"]["name"] == asset.name
        # Select minimal firmware last, then exercise every native lifecycle.
        assert (await client.post(url + "/api/firmware", data=form(args.elf.read_bytes(), args.elf.name))).status == 200
        events, uart = [], bytearray()
        async def receive(ws, predicate):
            async with asyncio.timeout(5):
                while True:
                    value = await ws.receive_json()
                    events.append(value)
                    if value.get("type") == "uart":
                        assert isinstance(value["timestamp_ns"], str) and isinstance(value["seq"], str)
                        uart.extend(base64.b64decode(value["data_b64"], validate=True))
                    if predicate(value):
                        return value
        serial = 0
        async def command(ws, action, generation, values=None):
            nonlocal serial
            serial += 1
            identifier = str(serial)
            await ws.send_json({"id":identifier, "session_id":generation, "action":action, "args":{} if values is None else values})
            return await receive(ws, lambda value: value.get("type") == "command_result" and value.get("id") == identifier)
        ws = await client.ws_connect(url + "/api/ws", origin=url)
        try:
            await receive(ws, lambda value:value.get("type") == "state")
            try:
                await client.ws_connect(url + "/api/ws")
                raise AssertionError("second controller admitted")
            except WSServerHandshakeError as error:
                assert error.status == 409
            await ws.send_str("not JSON")
            malformed = await receive(ws, lambda value:value.get("type") == "command_result")
            assert malformed["id"] is None and not malformed["accepted"]
            loaded = await command(ws, "load", None)
            assert loaded["ok"], loaded
            generation = loaded["session_id"]
            paused = await receive(ws, lambda value:value.get("type") == "state" and value["status"] == "paused")
            assert isinstance(paused["board"]["timestamp_ns"],str) and len(paused["pins"]) == 128
            report["native_identities"].extend(identities(process.pid))
            assert len(report["native_identities"]) == 1
            assert (await client.post(url + "/api/firmware", data=form(b"active"))).status == 409
            assert not (await command(ws,"run","stale"))["accepted"]
            assert not (await command(ws,"key",generation,{"pressed":1}))["accepted"]
            assert (await command(ws,"run",generation))["ok"]
            if b"SIM_READY minimal-board\n" not in uart:
                await receive(ws, lambda _: b"SIM_READY minimal-board\n" in uart)
            assert (await command(ws,"key",generation,{"pressed":True}))["ok"]
            pressed = await receive(ws, lambda value:value.get("type") == "state" and value.get("board",{}).get("key",{}).get("pressed"))
            assert int(pressed["board"]["gpio"]["B"]["sampled"]) & 1
            await ws.close()
            # Disconnect releases momentary key and preserves the finite native session.
            async with asyncio.timeout(5):
                while True:
                    state = await (await client.get(url + "/api/state")).json()
                    if not state["board"]["key"]["pressed"]:
                        break
                    await asyncio.sleep(.01)
            assert state["session_id"] == generation and state["status"] == "running"
            ws = await client.ws_connect(url + "/api/ws")
            replay = await receive(ws, lambda value:value.get("type") == "uart")
            assert replay["session_id"] == generation
            stopped = await command(ws,"stop",generation)
            assert stopped["ok"] and not identities(process.pid), stopped
            restarted = await command(ws,"restart",generation)
            assert restarted["ok"] and restarted["session_id"] != generation, restarted
            report["native_identities"].extend(identities(process.pid))
            generation = restarted["session_id"]
            assert (await command(ws,"stop",generation))["ok"] and not identities(process.pid)
            response = await client.post(url + "/api/firmware", data=form(args.adc.read_bytes(),args.adc.name))
            assert response.status == 200
            loaded = await command(ws,"load",generation)
            assert loaded["ok"], loaded
            generation = loaded["session_id"]
            report["native_identities"].extend(identities(process.pid))
            refused = await command(ws,"adc",generation,{"channel":4,"raw":1})
            assert not refused["ok"] and "requires running QEMU" in refused["error"]
            uart.clear()
            assert (await command(ws,"run",generation))["ok"]
            if b"LAB_READY\n" not in uart:
                await receive(ws,lambda _:b"LAB_READY\n" in uart)
            assert (await command(ws,"adc",generation,{"channel":4,"raw":12345}))["ok"]
            if b"ADC=12345\n" not in uart:
                await receive(ws,lambda _:b"ADC=12345\n" in uart)
            assert (await command(ws,"adc",generation,{"channel":4,"voltage_uv":1650000}))["ok"]
            if b"ADC=32768\n" not in uart:
                await receive(ws,lambda _:b"ADC=32768\n" in uart)
            # Server TERM must stop this still-active native owner and the live WS.
            report["events"] = events
        finally:
            await ws.close()


def main():
    parser = argparse.ArgumentParser()
    for option in ["sim", "qemu", "elf", "adc", "output"]:
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir()
    report = {"status":"failed", "native_identities":[]}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port = sock.getsockname()[1]
    process = None
    primary = None
    try:
        with (args.output / "stdout").open("wb") as stdout, (args.output / "stderr").open("wb") as stderr:
            process = subprocess.Popen([str(args.sim.resolve()),"lab","web","--port",str(port),"--qemu",str(args.qemu.resolve()),"--startup-timeout","3","--timeout","30"],stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr)
            try:
                asyncio.run(scenario(f"http://127.0.0.1:{port}",args,process,report))
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        raise AssertionError("server failed bounded TERM cleanup")
                report["server_exit"] = process.returncode
        assert process.returncode == 130, (args.output/"stderr").read_text()
        assert not (args.output/"stderr").read_text().replace("error: Web Lab interrupted\n", "")
        assert all(not Path(f"/proc/{pid}").exists() for pid,_ in report["native_identities"])
        report["status"] = "passed"
    except BaseException as error:
        primary = error
        report["error"] = repr(error)
        raise
    finally:
        (args.output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps({key:value for key,value in report.items() if key != "events"},indent=2),flush=True)

if __name__ == "__main__":
    main()
