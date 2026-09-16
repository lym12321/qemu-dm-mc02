"""Tool boundary tests: fragmented messages, events, IDs and error replies."""
from pathlib import Path
import json
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from dm_mc02_qmp import QmpSession


class QmpClientTest(unittest.TestCase):
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
