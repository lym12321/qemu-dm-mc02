"""Tool boundary tests: fragmented messages, events, IDs and error replies."""
import asyncio
from pathlib import Path
import json
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from dm_mc02_qmp import QmpSession


class QmpClientTest(unittest.TestCase):
    def test_connect_timeout_covers_missing_qmp_greeting(self):
        with tempfile.TemporaryDirectory(prefix="dm-qmp-timeout-") as directory:
            address = str(Path(directory) / "qmp.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(address)
            server.listen(1)
            accepted = threading.Event()
            release = threading.Event()

            def serve():
                with server.accept()[0]:
                    accepted.set()
                    release.wait(timeout=2)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    QmpSession(address, timeout=1.0, connect_timeout=0.05)
                self.assertTrue(accepted.wait(timeout=1))
                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                release.set()
                thread.join(timeout=2)
                server.close()

    def test_close_timeout_bounds_a_stalled_upstream_disconnect(self):
        with tempfile.TemporaryDirectory(prefix="dm-qmp-close-") as directory:
            address = str(Path(directory) / "qmp.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(address)
            server.listen(1)
            errors = []

            def serve():
                try:
                    with server.accept()[0] as peer:
                        peer.settimeout(2)
                        peer.sendall(
                            b'{"QMP":{"version":{"qemu":{"major":8,'
                            b'"minor":2,"micro":2},"package":""},'
                            b'"capabilities":[]}}\r\n'
                        )
                        data = b""
                        while True:
                            data += peer.recv(4096)
                            try:
                                request = json.loads(data)
                                break
                            except json.JSONDecodeError:
                                continue
                        response = {"return": {}, "id": request.get("id")}
                        peer.sendall((json.dumps(response) + "\r\n").encode())
                        self.assertEqual(peer.recv(1), b"")
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            client = QmpSession(address, close_timeout=0.05)
            original_disconnect = client._qmp.disconnect

            async def stalled_disconnect():
                await asyncio.get_running_loop().create_future()

            client._qmp.disconnect = stalled_disconnect
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    client.close()
                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                client._qmp.disconnect = original_disconnect
                super(QmpSession, client).close()
                thread.join(timeout=2)
                server.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_fragmented_event_and_error_routing(self):
        with tempfile.TemporaryDirectory(prefix="dm-qmp-test-") as directory:
            address = str(Path(directory) / "qmp.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(address)
            server.listen(1)
            requests = []
            errors = []

            def serve():
                try:
                    with server.accept()[0] as peer:
                        peer.settimeout(3)
                        peer.sendall(b'{"QMP":{"version":{"qemu":{"major":8,'
                                     b'"minor":2,"micro":2},"package":""},'
                                     b'"capabilities":[]}}\r\n')
                        for index in range(4):
                            data = b""
                            while True:
                                data += peer.recv(4096)
                                try:
                                    request = json.loads(data)
                                    break
                                except json.JSONDecodeError:
                                    continue
                            requests.append(request)
                            response = {"id": request.get("id")}
                            if index == 0:
                                response["return"] = {}
                            elif index == 1:
                                peer.sendall(b'{"event":"RESET","data":{}}\r\n')
                                response["return"] = {"status": "paused"}
                            else:
                                response["error"] = {"class": "CommandNotFound",
                                                     "desc": "fixture rejection"}
                            data = (json.dumps(response) + "\r\n").encode()
                            peer.sendall(data[:7])
                            peer.sendall(data[7:])
                        self.assertEqual(peer.recv(1), b"")
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                with QmpSession(address) as client:
                    self.assertEqual(client.command("query-status"), {"status": "paused"})
                    self.assertEqual(client.pull_event()["event"], "RESET")
                    self.assertIn("error", client.command_raw("bad-raw"))
                    with self.assertRaises(Exception) as caught:
                        client.command("bad-value")
                    self.assertIn("fixture rejection", str(caught.exception))
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual([r["execute"] for r in requests],
                                 ["qmp_capabilities", "query-status", "bad-raw", "bad-value"])
                self.assertNotEqual(requests[1]["id"], requests[3]["id"])
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
