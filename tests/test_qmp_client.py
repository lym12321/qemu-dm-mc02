"""Tool boundary tests: fragmented messages, events, IDs and error replies."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import json
import logging
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from dm_mc02_qmp import QmpSession
from qemu.qmp import QMPClient
from qemu.qmp.message import Message


@contextmanager
def batch_peer(test, handler):
    """Real socket framing; isolate only the independent native QMP producer."""
    with tempfile.TemporaryDirectory(prefix="dm-qmp-batch-") as directory:
        address = str(Path(directory) / "qmp.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(address)
        server.listen(1)
        requests, errors = [], []

        def serve():
            try:
                with server.accept()[0] as peer:
                    peer.settimeout(3)
                    buffered = ""
                    decoder = json.JSONDecoder()
                    def read():
                        nonlocal buffered
                        while True:
                            try:
                                request, end = decoder.raw_decode(buffered)
                            except json.JSONDecodeError:
                                raw = peer.recv(4096)
                                test.assertTrue(raw, "unexpected QMP request EOF")
                                buffered += raw.decode("utf-8")
                            else:
                                buffered = buffered[end:].lstrip()
                                break
                        requests.append(request)
                        return request
                    def reply(request, **value):
                        raw = (json.dumps(dict(value, id=request.get("id"))) + "\r\n").encode()
                        peer.sendall(raw[:7])
                        peer.sendall(raw[7:])
                    peer.sendall(b'{"QMP":{"version":{"qemu":{"major":8,'
                        b'"minor":2,"micro":2},"package":""},"capabilities":[]}}\r\n')
                    capability = read()
                    test.assertEqual(capability["execute"], "qmp_capabilities")
                    reply(capability, **{"return": {}})
                    if handler(peer, read, reply) is not False:
                        test.assertEqual(buffered, "")
                        test.assertEqual(peer.recv(1), b"")
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield address, requests
        finally:
            thread.join(timeout=4)
            server.close()
            test.assertFalse(thread.is_alive())
            test.assertEqual(errors, [])


class QmpClientTest(unittest.TestCase):
    def test_disabled_debug_hooks_preserve_message_without_formatting(self):
        client = QMPClient()
        client.logger = logging.Logger("qmp-test-child", logging.NOTSET)
        client.logger.parent = logging.Logger("qmp-test-parent", logging.WARNING)
        self.assertEqual(client.logger.getEffectiveLevel(), logging.WARNING)
        message = Message(b'{"execute":"query-status","id":"literal"}')
        with patch.object(Message, "__str__", side_effect=AssertionError("unexpected formatting")) as formatted:
            self.assertIs(client._cb_outbound(message), message)
            self.assertIs(client._cb_inbound(message), message)
            self.assertEqual(bytes(message), b'{"execute":"query-status","id":"literal"}')
            formatted.assert_not_called()

    def test_enabled_debug_hooks_capture_exact_snapshot_before_later_mutation(self):
        records = []
        class DeferredHandler(logging.Handler):
            def emit(self, record):
                records.append(record)
        handler = DeferredHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
        client = QMPClient()
        client.logger = logging.Logger("qmp-test-snapshot", logging.DEBUG)
        client.logger.addHandler(handler)
        outbound = Message(b'{"execute":"query-status","id":"literal"}')
        inbound = Message(b'{"return":{"status":"paused"},"id":"literal"}')
        self.assertIs(client._cb_outbound(outbound), outbound)
        self.assertIs(client._cb_inbound(inbound), inbound)
        self.assertEqual(bytes(outbound), b'{"execute":"query-status","id":"literal"}')
        self.assertEqual(bytes(inbound), b'{"return":{"status":"paused"},"id":"literal"}')
        outbound["execute"] = "changed"
        inbound["return"] = {"status": "running"}
        original_outbound = '{\n  "execute": "query-status",\n  "id": "literal"\n}'
        original_inbound = '{\n  "return": {\n    "status": "paused"\n  },\n  "id": "literal"\n}'
        self.assertEqual([record.args for record in records],
                         [(original_outbound,), (original_inbound,)])
        self.assertTrue(all(type(record.args[0]) is str for record in records))
        self.assertEqual([handler.format(record) for record in records],
                         ["DEBUG:--> " + original_outbound, "DEBUG:<-- " + original_inbound])

    def test_enabled_debug_formatting_exception_propagates_but_disabled_does_not(self):
        client = QMPClient()
        client.logger = logging.Logger("qmp-test-exception", logging.DEBUG)
        message = Message(b'{"return":{}}')
        failure = ValueError("message formatting failed")
        with patch.object(Message, "__str__", side_effect=failure) as formatted:
            with self.assertRaises(ValueError) as caught:
                client._cb_inbound(message)
            self.assertIs(caught.exception, failure)
            formatted.assert_called_once_with()
            client.logger.disabled = True
            self.assertIs(client._cb_inbound(message), message)
            self.assertEqual(formatted.call_count, 1)
            self.assertEqual(bytes(message), b'{"return":{}}')

    def assert_batch_settled(self, client):
        # Test-only white-box evidence for upstream pending requests and the
        # owned batch tasks. Ordinary reader/writer tasks live until close.
        self.assertEqual(client._qmp._pending, {})
        live = [task for task in asyncio.all_tasks(client._aloop)
                if any(name in task.get_coro().__qualname__
                       for name in ("cmd_many", "execute_msg"))]
        self.assertEqual(live, [])

    def test_many_routes_reverse_fragmented_replies_and_events_in_input_order(self):
        def serve(peer, read, reply):
            batch = [read() for _ in range(3)]
            self.assertEqual(batch, [
                {"execute": "query-a", "arguments": {"path": "/machine"}, "id": "__qmp#00000"},
                {"execute": "query-b", "id": "__qmp#00001"},
                {"execute": "query-c", "arguments": {}, "id": "__qmp#00002"},
            ])
            peer.sendall(b'{"event":"RESET","data":{"guest":1}}\r\n')
            for request, value in zip(reversed(batch), (None, [2], {"value": 1})):
                reply(request, **{"return": value})
        with batch_peer(self, serve) as (address, _), QmpSession(address) as client:
            arguments = {"path": "/machine"}
            # The caller mutates its dict after admission, before upstream tasks
            # execute; native bytes must still be the admitted original snapshot.
            client._aloop.call_soon(arguments.update, {"path": "/changed"})
            self.assertEqual(client.command_many((("query-a", arguments),
                ("query-b", None), ("query-c", {}))), ({"value": 1}, [2], None))
            self.assertEqual(client.pull_event(), {"event": "RESET", "data": {"guest": 1}})
            self.assert_batch_settled(client)

    def test_many_waits_for_every_reply_and_raises_first_input_error_with_later_notes(self):
        def serve(peer, read, reply):
            batch = [read() for _ in range(3)]
            reply(batch[2], error={"class": "GenericError", "desc": "later input error"})
            reply(batch[0], error={"class": "CommandNotFound", "desc": "first input error"})
            reply(batch[1], **{"return": "completed middle query"})
        with batch_peer(self, serve) as (address, _), QmpSession(address) as client:
            execute, originals = client._qmp.execute_msg, {}
            async def observed_execute(message):
                try:
                    return await execute(message)
                except BaseException as error:
                    originals[message["execute"]] = error
                    raise
            client._qmp.execute_msg = observed_execute
            with self.assertRaises(Exception) as caught:
                client.command_many((("first", None), ("middle", None), ("last", None)))
            self.assertIs(caught.exception, originals["first"])
            self.assertIn("first input error", str(caught.exception))
            self.assertIn("later input error", "\n".join(caught.exception.__notes__))
            self.assert_batch_settled(client)

    def test_many_timeout_and_keyboard_interrupt_cancel_all_owned_requests(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                def serve(peer, read, reply):
                    first, _ = read(), read()
                    if interrupted:
                        os.kill(os.getpid(), signal.SIGINT)
                    else:
                        reply(first, error={"class": "GenericError",
                                            "desc": "completed before timeout"})
                with batch_peer(self, serve) as (address, _):
                    client = QmpSession(address, timeout=.05 if not interrupted else 1.,
                                        connect_timeout=1., close_timeout=.2)
                    original_tasks = asyncio.all_tasks(client._aloop)
                    started = time.monotonic()
                    try:
                        with self.assertRaises(KeyboardInterrupt if interrupted else TimeoutError) as caught:
                            client.command_many((("wait-a", None), ("wait-b", None)))
                        if not interrupted:
                            self.assertIn("completed before timeout",
                                          "\n".join(caught.exception.__notes__))
                        self.assertLess(time.monotonic() - started, 1.5)
                        self.assert_batch_settled(client)
                        self.assertEqual(asyncio.all_tasks(client._aloop), original_tasks)
                    finally:
                        client.close()
                    self.assertEqual(asyncio.all_tasks(client._aloop), set())

    def test_many_disconnect_settles_every_request_and_preserves_upstream_error(self):
        def serve(peer, read, reply):
            read(), read()
            return False  # Native peer disconnects with both requests outstanding.
        with batch_peer(self, serve) as (address, _):
            client = QmpSession(address, timeout=.2, close_timeout=.2)
            try:
                with self.assertRaises(Exception) as caught:
                    client.command_many((("pending-a", None), ("pending-b", None)))
                self.assertIn("Disconnected", str(caught.exception))
                self.assert_batch_settled(client)
            finally:
                client.close()
            self.assertEqual(asyncio.all_tasks(client._aloop), set())

    def test_many_rejects_every_bad_group_before_sending_any_partial_request(self):
        def serve(peer, read, reply):
            marker = read()
            self.assertEqual(marker, {"execute": "admission-complete", "arguments": {}, "id": "__qmp#00000"})
            reply(marker, **{"return": "no partial batch"})
        with batch_peer(self, serve) as (address, _), QmpSession(address) as client:
            class Text(str):
                pass
            invalid = [[], (), (("a", None),) * 9,
                (("a", None), ["b", None]), (("a", None), ("", None)),
                (("a", None), ("b\0c", None)), (("a", None), (Text("b"), None)),
                (("a", None), ("b", [])), (("a", None), ("b", {"bad": object()}))]
            for batch in invalid:
                with self.subTest(batch=batch), self.assertRaises((TypeError, ValueError)):
                    client.command_many(batch)
            for timeout in (None, 0, -1, True, float("nan"), float("inf"), 1 << 1024):
                client.settimeout(timeout)
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    client.command_many((("a", None),))
            client.settimeout(1.)
            self.assertEqual(client.command("admission-complete"), "no partial batch")
            self.assert_batch_settled(client)

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
