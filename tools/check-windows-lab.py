#!/usr/bin/env python3
"""Black-box native Windows Lab, Web and ARM GDB package acceptance.

Python, aiohttp, psutil and GDB are independent test tools. The product receives
only the Windows system PATH and discovers its adjacent QEMU without overrides.
The existing bare-metal fixtures supply the UART/CAN/ADC observations.
"""

import argparse
import ast
import asyncio
import base64
import ctypes
import json
import os
from pathlib import Path
import queue
import select
import signal
import socket
import subprocess
import tempfile
import threading
import time

from aiohttp import ClientSession, FormData, WSServerHandshakeError
import psutil


TIMEOUT = 10
TAIL_BYTES = 12000


def port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def identity(process):
    return {"pid": process.pid, "created": process.create_time()}


def alive(value):
    try:
        return psutil.Process(value["pid"]).create_time() == value["created"]
    except psutil.NoSuchProcess:
        return False


class ExternalQmp:
    """Independent consumer of QEMU's native external Unix QMP endpoint."""

    def __init__(self, path):
        self.socket = socket.socket(getattr(socket, "AF_UNIX", 1), socket.SOCK_STREAM)
        self.stream = None
        self.serial = 0
        try:
            if os.name == "nt":
                # CPython on Windows need not expose sockaddr_un conversion even
                # though Windows and canonical QEMU support native AF_UNIX.
                class SockaddrUn(ctypes.Structure):
                    _fields_ = [("family", ctypes.c_ushort), ("path", ctypes.c_char * 108)]

                encoded = os.fsencode(path)
                if len(encoded) >= 108:
                    raise ValueError("external QMP test path exceeds sockaddr_un")
                address = SockaddrUn(1, encoded)
                winsock = ctypes.WinDLL("ws2_32", use_last_error=True)
                winsock.connect.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
                winsock.connect.restype = ctypes.c_int
                winsock.WSAGetLastError.restype = ctypes.c_int
                self.socket.setblocking(False)
                if winsock.connect(self.socket.fileno(), ctypes.byref(address), ctypes.sizeof(address)):
                    error = winsock.WSAGetLastError()
                    if error not in [10035, 10036, 10037]:
                        raise OSError(error, "native Windows AF_UNIX connect")
                    _, writable, exceptional = select.select([], [self.socket], [self.socket], TIMEOUT)
                    if not writable or exceptional:
                        raise TimeoutError("external Windows QMP connect deadline")
                    error = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if error:
                        raise OSError(error, "native Windows AF_UNIX connect")
            else:
                self.socket.settimeout(TIMEOUT)
                self.socket.connect(str(path))
            self.socket.settimeout(TIMEOUT)
            self.stream = self.socket.makefile("rwb", buffering=0)
            assert "QMP" in self._read(), "external native QMP greeting missing"
            self.command("qmp_capabilities")
        except BaseException:
            self.close()
            raise

    def _read(self):
        line = self.stream.readline(65537)
        if not line or len(line) > 65536:
            raise RuntimeError("external QMP disconnected or line exceeds bound")
        return json.loads(line)

    def command(self, name):
        self.serial += 1
        self.socket.sendall(json.dumps({"execute": name, "id": self.serial}).encode() + b"\r\n")
        for _ in range(128):
            value = self._read()
            if value.get("id") == self.serial:
                assert "return" in value, value
                return value["return"]
        raise BufferError("external QMP reply exceeded event bound")

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self.socket.close()


class Application:
    """One test-owned CLI; product owns QEMU and its bounded cleanup."""

    def __init__(self, command, cwd, env, result):
        self.result = result
        self.result.update(command=command, native_identities=[])
        self.lines = queue.Queue(maxsize=4096)
        self.tail = bytearray()
        self.uart = bytearray()
        self.reader_error = None
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(command, cwd=cwd, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, creationflags=flags)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def __enter__(self):
        return self

    def __exit__(self, kind, primary, traceback):
        try:
            self.close()
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f"application cleanup: {error}")
        return False

    def _read(self):
        try:
            while line := self.process.stdout.readline(TAIL_BYTES + 1):
                if len(line) > TAIL_BYTES:
                    raise BufferError("application output line exceeds bound")
                self.tail.extend(line)
                del self.tail[:-TAIL_BYTES]
                self.lines.put_nowait(line.decode("utf-8", errors="replace").rstrip("\r\n"))
        except BaseException as error:
            self.reader_error = repr(error)

    def send(self, text):
        self.process.stdin.write(text.encode("utf-8"))
        self.process.stdin.flush()

    def until(self, predicate):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if self.reader_error:
                raise RuntimeError(self.reader_error)
            try:
                line = self.lines.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError(f"CLI exited early: {self.process.returncode}")
                continue
            if line.startswith("UART ") and ' b"' in line:
                self.uart.extend(ast.literal_eval('b"' + line.split(' b"', 1)[1]))
                if len(self.uart) > 65536:
                    raise BufferError("fixture UART exceeds bound")
            if predicate(line):
                return line
        raise TimeoutError(f"CLI observation deadline; tail={bytes(self.tail)!r}")

    def marker(self, expected):
        return self.until(lambda line: expected in line)

    def uart_contains(self, expected, start=0):
        if expected not in self.uart[start:]:
            self.until(lambda _: expected in self.uart[start:])

    def snapshot(self):
        self.send("state\n")
        return json.loads(self.until(lambda line: line.startswith('{"') and '"schema":' in line))

    def natives(self):
        try:
            children = psutil.Process(self.process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        native = [identity(child) for child in children if child.name().lower().startswith("qemu-system-arm")]
        for item in native:
            if item not in self.result["native_identities"]:
                self.result["native_identities"].append(item)
        return native

    def no_native(self):
        assert not self.natives(), "native process still owned after stop result"
        assert not any(alive(item) for item in self.result["native_identities"]), "native identity survived cleanup"

    def finish(self):
        self.send("quit")
        self.process.stdin.close()  # Final command without newline is accepted at EOF.
        self.marker("Lab finished:")
        self.no_native()
        assert self.process.wait(timeout=5) == 0

    def interrupt(self):
        self.process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        assert self.process.wait(timeout=8) == 130
        assert not any(alive(item) for item in self.result["native_identities"])

    def close(self):
        cleanup = []
        try:
            if self.process.poll() is None:
                self.process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    cleanup.append("CLI failed bounded interrupt cleanup")
                    self.process.kill()
                    self.process.wait(timeout=5)
            for item in self.result["native_identities"]:
                if alive(item):
                    cleanup.append(f"native identity leaked: {item}")
                    leaked = psutil.Process(item["pid"])
                    leaked.kill()
                    leaked.wait(timeout=5)
            self.reader.join(timeout=3)
            if self.reader.is_alive():
                cleanup.append("output pipe remained open after process cleanup")
            else:
                self.process.stdout.close()
            if not self.process.stdin.closed:
                self.process.stdin.close()
            if self.reader_error:
                cleanup.append(self.reader_error)
        except BaseException as error:
            cleanup.append(repr(error))
        self.result.update(exit_code=self.process.returncode,
                           output_tail=bytes(self.tail).decode("utf-8", errors="replace"),
                           cleanup_errors=cleanup)
        if cleanup:
            raise AssertionError("; ".join(cleanup))


def gdb_case(args, cwd, env, result):
    gdb_port = port()
    qmp_path = cwd / "debug.qmp"
    app = Application([str(args.sim), "lab", "interactive", "--elf", str(args.elf),
                       "--qmp", str(qmp_path),
                       "--gdb-port", str(gdb_port), "--paused", "--timeout", "40",
                       "--startup-timeout", "5"], cwd, env, result)
    with app:
        app.marker("Lab connected")
        assert len(app.natives()) == 1
        assert not app.uart, "guest ran before debugger continuation"
        qmp = ExternalQmp(qmp_path)
        try:
            assert not qmp.command("query-status")["running"]
        finally:
            qmp.close()
        commands = cwd / "debug.gdb"
        commands.write_text("\n".join([
            "set pagination off", "set confirm off", "set remotetimeout 5",
            f"target remote 127.0.0.1:{gdb_port}",
            "info registers pc sp", "break firmware_main", "continue",
            "if ((unsigned long)$pc & ~1) != ((unsigned long)firmware_main & ~1)",
            "  quit 2", "end",
            'printf "GDB_BREAK_OK\\n"',
            "set $saved_r4 = $r4", "set $r4 = 0x13579bdf",
            "if $r4 != 0x13579bdf", "  quit 3", "end",
            'printf "GDB_REGISTER_OK\\n"', "set $r4 = $saved_r4",
            "set {unsigned int}&ticks_ms = 0x2468ace0",
            "if *(unsigned int*)&ticks_ms != 0x2468ace0", "  quit 4", "end",
            "x/wx &ticks_ms", 'printf "GDB_MEMORY_OK\\n"',
            "set $before = $pc", "stepi", "if $pc == $before", "  quit 5", "end",
            'printf "GDB_STEP_OK\\n"', "monitor system_reset", "continue",
            "if ((unsigned long)$pc & ~1) != ((unsigned long)firmware_main & ~1)",
            "  quit 7", "end",
            "if ticks_ms != 0", "  quit 6", "end",
            'printf "GDB_RESET_OK\\n"', "detach", "quit", "",
        ]), encoding="utf-8")
        debug_env = env.copy()
        debug_env["PATH"] = str(args.gdb.parent) + os.pathsep + env["PATH"]
        debug = subprocess.run([str(args.gdb), "-q", "--batch", "-nx", str(args.elf),
                                "-x", str(commands)], cwd=cwd, env=debug_env,
                               capture_output=True, timeout=20)
        result["gdb"] = {"exit_code": debug.returncode,
                         "stdout": debug.stdout[-TAIL_BYTES:].decode(errors="replace"),
                         "stderr": debug.stderr[-TAIL_BYTES:].decode(errors="replace")}
        assert debug.returncode == 0, result["gdb"]
        for operation in ["BREAK", "REGISTER", "MEMORY", "STEP", "RESET"]:
            assert f"GDB_{operation}_OK" in result["gdb"]["stdout"], result["gdb"]
        qmp = ExternalQmp(qmp_path)
        try:
            assert qmp.command("query-status")["running"]
        finally:
            qmp.close()
        result["external_qmp_status"] = ["paused", "running"]
        app.uart_contains(b"SIM_READY minimal-board\n")
        app.finish()
        result["external_qmp_path_after_cleanup"] = qmp_path.exists()


def interactive_case(args, cwd, env, result):
    qmp_path = cwd / "interactive.qmp"
    app = Application([str(args.sim), "lab", "interactive", "--elf", str(args.lab_elf),
                       "--qmp", str(qmp_path),
                       "--can", "can1", "--adc", "--timeout", "40",
                       "--startup-timeout", "5"], cwd, env, result)
    with app:
        app.uart_contains(b"LAB_READY\n")
        assert len(app.natives()) == 1
        app.send("uart uart1 q\n")
        app.uart_contains(b"UART=q\n")
        cursor = len(app.uart)
        app.send("key press\n")
        app.uart_contains(b"KEY=0\n", cursor)
        assert app.snapshot()["key"]["pressed"]
        app.send("gpio B0 1\nvin 24000\n")
        snapshot = app.snapshot()
        assert int(snapshot["gpio"]["B"]["sampled"]) & 1 and snapshot["vin_mv"] == 24000
        app.send("pause\n")
        before = app.snapshot()["timestamp_ns"]
        assert app.snapshot()["timestamp_ns"] == before, "native pause advanced virtual time"
        qmp = ExternalQmp(qmp_path)
        try:
            assert not qmp.command("query-status")["running"]
        finally:
            qmp.close()
        app.send("adc ch4 raw 1\n")
        app.marker("requires running QEMU; use cont first")
        app.send("cont\nadc ch4 voltage_uv 1650000\n")
        app.uart_contains(b"ADC=32768\n")
        app.send("adc ch4 raw 12345\n")
        app.uart_contains(b"ADC=12345\n")
        qmp = ExternalQmp(qmp_path)
        try:
            assert qmp.command("query-status")["running"]
        finally:
            qmp.close()
        result["external_qmp_status"] = ["paused", "running"]
        app.send("can can1 0x321 0 8 4142434445464748\n")
        app.marker("id=0x322 flags=0x0 dlc=8 data=4142434445464748")
        cursor = len(app.uart)
        app.send("key release\n")
        app.uart_contains(b"KEY=1\n", cursor)
        assert not app.snapshot()["key"]["pressed"]
        app.send("x" * 4097)
        app.marker("command line exceeds 4096 bytes")
        app.send("discarded suffix\nvin 12345\n")
        assert app.snapshot()["vin_mv"] == 12345
        app.finish()
        result["external_qmp_path_after_cleanup"] = qmp_path.exists()


async def web_scenario(args, app, url, result):
    async with ClientSession() as client:
        async with asyncio.timeout(TIMEOUT):
            while True:
                assert app.process.poll() is None, "Web server exited before HTTP readiness"
                try:
                    response = await client.get(url + "/api/state")
                    break
                except OSError:
                    await asyncio.sleep(.02)
        assert (await response.json())["status"] == "idle" and not app.natives()
        for path, content in [("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")]:
            response = await client.get(url + path)
            assert response.status == 200 and content in response.headers["Content-Type"]
            assert await response.read()
        assert (await client.get(url + "/api/state", headers={"Origin": "https://foreign.example"})).status == 403
        invalid = FormData()
        invalid.add_field("other", b"bad", filename="bad.elf")
        assert (await client.post(url + "/api/firmware", data=invalid)).status == 400
        firmware = FormData()
        firmware.add_field("firmware", args.lab_elf.read_bytes(), filename="lab.elf")
        response = await client.post(url + "/api/firmware", data=firmware)
        assert response.status == 200, await response.text()
        uart = bytearray()
        serial = 0
        events = []

        async def receive(ws, predicate):
            async with asyncio.timeout(TIMEOUT):
                while True:
                    value = await ws.receive_json()
                    events.append(value)
                    assert len(events) <= 4096, "Web fixture event bound exceeded"
                    if value.get("type") == "uart":
                        uart.extend(base64.b64decode(value["data_b64"], validate=True))
                    if predicate(value):
                        return value

        async def command(ws, action, generation, values=None):
            nonlocal serial
            serial += 1
            identifier = str(serial)
            await ws.send_json({"id": identifier, "session_id": generation,
                                "action": action, "args": values or {}})
            return await receive(ws, lambda value: value.get("type") == "command_result" and value.get("id") == identifier)

        async with client.ws_connect(url + "/api/ws", origin=url) as ws:
            await receive(ws, lambda value: value.get("type") == "state")
            try:
                second = await client.ws_connect(url + "/api/ws", origin=url)
            except WSServerHandshakeError as error:
                assert error.status == 409
            else:
                await second.close()
                raise AssertionError("second Web controller admitted")
            loaded = await command(ws, "load", None)
            assert loaded["ok"], loaded
            generation = loaded["session_id"]
            await receive(ws, lambda value: value.get("type") == "state" and value["status"] == "paused")
            assert len(app.natives()) == 1
            assert not (await command(ws, "run", "stale"))["accepted"]
            refused = await command(ws, "adc", generation, {"channel": 4, "raw": 1})
            assert not refused["ok"] and "requires running QEMU" in refused["error"]
            assert (await command(ws, "run", generation))["ok"]
            if b"LAB_READY\n" not in uart:
                await receive(ws, lambda _: b"LAB_READY\n" in uart)
            assert (await command(ws, "key", generation, {"pressed": True}))["ok"]
            await receive(ws, lambda value: value.get("type") == "state" and value.get("board", {}).get("key", {}).get("pressed"))
        # Releasing the sole controller releases momentary input, not the session.
        async with asyncio.timeout(TIMEOUT):
            while True:
                state = await (await client.get(url + "/api/state")).json()
                if not state["board"]["key"]["pressed"]:
                    break
                await asyncio.sleep(.02)
        assert state["session_id"] == generation and state["status"] == "running"
        async with client.ws_connect(url + "/api/ws", origin=url) as ws:
            replay = await receive(ws, lambda value: value.get("type") == "uart")
            assert replay["session_id"] == generation
            assert (await command(ws, "stop", generation))["ok"]
            app.no_native()
            restarted = await command(ws, "restart", generation)
            assert restarted["ok"] and restarted["session_id"] != generation, restarted
            assert len(app.natives()) == 1
            result["web_events"] = events
            app.interrupt()  # Ctrl-Break cleans a live native owner, not just an idle server.


def web_case(args, cwd, env, result):
    web_port = port()
    app = Application([str(args.sim), "lab", "web", "--port", str(web_port),
                       "--startup-timeout", "5", "--timeout", "40"], cwd, env, result)
    with app:
        asyncio.run(web_scenario(args, app, f"http://127.0.0.1:{web_port}", result))


def lifecycle_case(args, cwd, env, result, unexpected_exit):
    qmp_path = cwd / "failure.qmp"
    command = [str(args.sim), "lab", "interactive", "--elf", str(args.elf),
               "--timeout", "7" if not unexpected_exit else "40", "--startup-timeout", "5"]
    if unexpected_exit:
        command += ["--qmp", str(qmp_path)]
    app = Application(command, cwd, env, result)
    with app:
        app.uart_contains(b"SIM_READY minimal-board\n")
        assert len(app.natives()) == 1
        if unexpected_exit:
            qmp = ExternalQmp(qmp_path)
            try:
                qmp.command("quit")
            finally:
                qmp.close()
        assert app.process.wait(timeout=10) != 0, "failed Lab reported success"
        app.reader.join(timeout=3)
        assert not app.reader.is_alive(), "output remained open after Lab failure"
        app.no_native()
        text = bytes(app.tail).decode(errors="replace")
        assert "Lab finished:" not in text, text
        assert "error:" in text, text
        if unexpected_exit:
            assert any(word in text.lower() for word in
                       ["exited", "closed", "eof", "end of file", "broken pipe", "reset"]), text
        else:
            assert "deadline" in text.lower(), text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["sim", "elf", "lab-elf", "gdb", "report"]:
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    for name in ["sim", "elf", "lab_elf", "gdb"]:
        setattr(args, name, getattr(args, name).resolve(strict=True))
    if args.report.exists():
        parser.error("--report must be a new path")
    report = {"schema": "dm-mc02/windows-lab-smoke/1", "passed": False}
    try:
        with tempfile.TemporaryDirectory(prefix="dm-lab-") as temporary:
            cwd = Path(temporary)
            env = os.environ.copy()
            for name in ["QEMU_SYSTEM_ARM", "PYTHONPATH", "PYTHONHOME"]:
                env.pop(name, None)
            empty = cwd / "empty-path"
            empty.mkdir()
            paths = [str(empty)]
            if os.name == "nt":
                system = Path(os.environ["SystemRoot"])
                paths += [str(system / "System32"), str(system)]
            env["PATH"] = os.pathsep.join(paths)
            report["environment"] = {"cwd": str(cwd), "PATH": env["PATH"]}
            cases = [("debug", gdb_case), ("interactive", interactive_case), ("web", web_case),
                     ("deadline", lambda *values: lifecycle_case(*values, unexpected_exit=False)),
                     ("unexpected_exit", lambda *values: lifecycle_case(*values, unexpected_exit=True))]
            for name, case in cases:
                result = report[name] = {"passed": False}
                case(args, cwd, env, result)
                result["passed"] = True
        report["passed"] = True
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["notes"] = getattr(error, "__notes__", [])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    diagnostic = {key: ({field: value for field, value in item.items() if field != "web_events"}
                        if isinstance(item, dict) else item)
                  for key, item in report.items()}
    print(json.dumps(diagnostic, indent=2), flush=True)
    print(f"Windows Lab package {'passed' if report['passed'] else 'FAILED'}: {args.report}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
