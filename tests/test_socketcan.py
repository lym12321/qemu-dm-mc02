"""Regression tests for the standalone SocketCAN bridge."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import sys
import unittest


MODULE = Path(__file__).resolve().parents[1] / "tools" / "dm_mc02_socketcan.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_socketcan_test", MODULE)
assert SPEC is not None and SPEC.loader is not None
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


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
