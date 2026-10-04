"""Regression tests for the standalone SocketCAN bridge."""

from __future__ import annotations

import importlib.util
import errno
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "tools" / "dm_mc02_socketcan.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_socketcan_test", MODULE)
assert SPEC is not None and SPEC.loader is not None
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class SetupSocket(socket.socket):
    """Real fd with only CAN setup calls isolated; no vcan/physical CAN claim."""

    def __init__(self, *, fileno, failed_stage=None, setup_error=None,
                 close_error=None):
        super().__init__(fileno=fileno)
        self.failed_stage = failed_stage
        self.setup_error = setup_error
        self.close_error = close_error
        self.close_calls = 0

    def setsockopt(self, *args):
        if self.failed_stage == "setsockopt":
            raise self.setup_error

    def bind(self, address):
        if self.failed_stage == "bind":
            raise self.setup_error

    def close(self):
        self.close_calls += 1
        super().close()
        if self.close_error is not None:
            raise self.close_error


class SocketCanOpenTests(unittest.TestCase):
    def setup_socket(self, **kwargs):
        acquired, peer = socket.socketpair()
        sock = SetupSocket(fileno=acquired.detach(), **kwargs)
        # Base close keeps teardown independent of injected close diagnostics.
        self.addCleanup(socket.socket.close, sock)
        self.addCleanup(peer.close)
        return sock, sock.fileno()

    def assert_closed_once(self, sock, fd):
        self.assertEqual(sock.close_calls, 1)
        self.assertEqual(sock.fileno(), -1)
        with self.assertRaises(OSError) as raised:
            os.fstat(fd)
        self.assertEqual(raised.exception.errno, errno.EBADF)

    def assert_setup_interrupt_cleanup(self, stage):
        primary = KeyboardInterrupt(f"interrupted {stage}")
        sock, fd = self.setup_socket(failed_stage=stage, setup_error=primary)
        with patch.object(mod.socket, "socket", return_value=sock):
            with self.assertRaises(KeyboardInterrupt) as raised:
                mod.open_socketcan("fixture")
        self.assertIs(raised.exception, primary)
        self.assert_closed_once(sock, fd)

    def test_setsockopt_interrupt_closes_fd_and_preserves_exception(self):
        self.assert_setup_interrupt_cleanup("setsockopt")

    def test_bind_interrupt_closes_fd_and_preserves_exception(self):
        self.assert_setup_interrupt_cleanup("bind")

    def test_bind_oserror_retains_socketcan_mapping_and_cause(self):
        primary = OSError(errno.ENODEV, "no such CAN device")
        sock, fd = self.setup_socket(failed_stage="bind", setup_error=primary)
        with patch.object(mod.socket, "socket", return_value=sock):
            with self.assertRaisesRegex(
                    mod.SocketCANError, "interface is unavailable: fixture") as raised:
                mod.open_socketcan("fixture", enable_fd=False)
        self.assertIs(raised.exception.__cause__, primary)
        self.assert_closed_once(sock, fd)

    def assert_mapped_failure_survives_close(self, cleanup):
        primary = OSError(errno.ENODEV, "no such CAN device")
        sock, fd = self.setup_socket(
            failed_stage="bind", setup_error=primary, close_error=cleanup)
        with patch.object(mod.socket, "socket", return_value=sock):
            # Catch cleanup interrupts too, so a regression is a failing test
            # instead of aborting collection of the remaining lifecycle cases.
            with self.assertRaises(BaseException) as raised:
                mod.open_socketcan("fixture")
        self.assertIsInstance(raised.exception, mod.SocketCANError)
        self.assertIn("interface is unavailable", str(raised.exception))
        self.assertIs(raised.exception.__cause__, primary)
        notes = "\n".join(getattr(raised.exception, "__notes__", ()))
        self.assertIn("cleanup", notes)
        self.assertIn(str(cleanup), notes)
        self.assert_closed_once(sock, fd)

    def test_close_error_cannot_replace_mapped_setup_failure(self):
        self.assert_mapped_failure_survives_close(OSError("close failed"))

    def test_close_interrupt_cannot_replace_mapped_setup_failure(self):
        self.assert_mapped_failure_survives_close(KeyboardInterrupt("close interrupted"))

    def test_constructor_failure_does_not_close_nonexistent_socket(self):
        primary = OSError(errno.ENODEV, "socket construction failed")
        with patch.object(mod.socket, "socket", side_effect=primary) as constructor:
            with self.assertRaises(mod.SocketCANError) as raised:
                mod.open_socketcan("fixture")
        self.assertIs(raised.exception.__cause__, primary)
        constructor.assert_called_once_with(socket.AF_CAN, socket.SOCK_RAW,
                                            socket.CAN_RAW)
        constructor.return_value.close.assert_not_called()
        self.assertFalse(getattr(raised.exception, "__notes__", ()))

    def test_success_transfers_open_fd_to_caller(self):
        sock, fd = self.setup_socket()
        with patch.object(mod.socket, "socket", return_value=sock):
            returned = mod.open_socketcan("fixture")
        self.assertIs(returned, sock)
        self.assertEqual(sock.close_calls, 0)
        self.assertEqual(sock.fileno(), fd)
        os.fstat(fd)
        returned.close()
        self.assert_closed_once(sock, fd)


class SocketCanTests(unittest.TestCase):
    def assertValueError(self, function, *args, **kwargs):
        with self.assertRaises(ValueError):
            function(*args, **kwargs)

    def test_classic_extended_rtr_and_fd_codec_round_trips(self):
        frames = [
            mod.CanFrame(0x123, data=b"12345678", timestamp_ns=17),
            mod.CanFrame(0x1ABCDE, mod.WIRE_EXTENDED | mod.WIRE_RTR,
                         dlc=4, data=b"ABCD"),
            mod.CanFrame(0x456, mod.WIRE_FD | mod.WIRE_BRS,
                         data=bytes(range(64)), timestamp_ns=99),
        ]
        for frame in frames:
            with self.subTest(frame=frame):
                self.assertEqual(
                    mod.unpack_wire_frame(mod.pack_wire_frame(frame)), frame)
                # SocketCAN does not carry the co-simulation timestamp.
                can_frame = mod.CanFrame(frame.can_id, frame.flags, frame.dlc,
                                         frame.data)
                self.assertEqual(
                    mod.unpack_can_frame(mod.pack_can_frame(frame)), can_frame)

    def test_frame_and_wire_validation(self):
        self.assertValueError(mod.CanFrame, 0x800)
        self.assertValueError(
            mod.CanFrame, 1, mod.WIRE_FD | mod.WIRE_RTR, data=b"x")

        raw = bytearray(mod.pack_wire_frame(mod.CanFrame(1, data=b"x")))
        raw[9] = 1
        self.assertValueError(mod.unpack_wire_frame, raw)

        raw = bytearray(mod.pack_can_frame(mod.CanFrame(1, data=b"x")))
        raw[5] = 1
        self.assertValueError(mod.unpack_can_frame, raw)

        raw = bytearray(mod.pack_can_frame(mod.CanFrame(1, data=b"x")))
        raw[0] = 0x00
        raw[1] = 0x10
        self.assertValueError(mod.unpack_can_frame, raw)

        raw = bytearray(mod.pack_can_frame(mod.CanFrame(
            1, mod.WIRE_FD, data=b"x")))
        raw[6] = 1
        self.assertValueError(mod.unpack_can_frame, raw)

        raw = bytearray(mod.pack_wire_frame(mod.CanFrame(1, data=b"x")))
        raw[4] = mod.WIRE_FD | mod.WIRE_RTR
        self.assertValueError(mod.unpack_wire_frame, raw)

        raw = bytearray(mod.pack_can_frame(mod.CanFrame(
            1, mod.WIRE_FD, data=b"x")))
        raw[5] |= mod.CANFD_ESI
        self.assertValueError(mod.unpack_can_frame, raw)

        raw = bytearray(mod.pack_can_frame(mod.CanFrame(
            1, mod.WIRE_FD, data=b"x")))
        raw[5] |= 0x08
        self.assertValueError(mod.unpack_can_frame, raw)

    def test_wire_partial_frame_survives_calls(self):
        wire_tx, wire_rx = socket.socketpair()
        can_tx, can_rx = socket.socketpair()
        try:
            frame = mod.CanFrame(0x123, data=b"partial")
            encoded = mod.pack_wire_frame(frame)
            wire_tx.sendall(encoded[:13])
            self.assertEqual(
                mod.bridge_once(wire_rx, can_rx,
                                direction="wire-to-can", timeout=0.1), 0)
            wire_tx.sendall(encoded[13:])
            self.assertEqual(
                mod.bridge_once(wire_rx, can_rx,
                                direction="wire-to-can", timeout=0.1), 1)
            self.assertEqual(mod.unpack_can_frame(can_tx.recv(16)), frame)
        finally:
            for sock in (wire_tx, wire_rx, can_tx, can_rx):
                sock.close()

    def test_both_direction_is_fair_and_drains_buffered_wire(self):
        wire_tx, wire_rx = socket.socketpair()
        can_tx, can_rx = socket.socketpair()
        try:
            wire_frames = [mod.CanFrame(0x100 + i, data=bytes([i]))
                           for i in range(2)]
            can_frame = mod.CanFrame(0x200, data=b"c")
            wire_tx.sendall(b"".join(mod.pack_wire_frame(f)
                                      for f in wire_frames))
            can_tx.sendall(mod.pack_can_frame(can_frame))

            self.assertEqual(mod.bridge_once(wire_rx, can_rx,
                                             timeout=0.1), 1)
            self.assertEqual(mod.unpack_can_frame(can_tx.recv(16)),
                             wire_frames[0])
            self.assertEqual(mod.bridge_once(wire_rx, can_rx,
                                             timeout=0.1), 1)
            self.assertEqual(mod.unpack_wire_frame(wire_tx.recv(mod.WIRE_SIZE)),
                             can_frame)
            self.assertEqual(mod.bridge_once(wire_rx, can_rx,
                                             timeout=0.1), 1)
            self.assertEqual(mod.unpack_can_frame(can_tx.recv(16)),
                             wire_frames[1])
        finally:
            for sock in (wire_tx, wire_rx, can_tx, can_rx):
                sock.close()


if __name__ == "__main__":
    unittest.main()
