#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 1
}

python3 - "$script_dir/dm_mc02_socketcan.py" <<'PY'
import importlib.util
import socket
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("dm_mc02_socketcan", path)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

def check(condition, message):
    if not condition:
        raise AssertionError(message)

classic = mod.CanFrame(0x123, data=b"12345678", timestamp_ns=17)
wire = mod.pack_wire_frame(classic)
check(len(wire) == 84, "wire size")
check(mod.unpack_wire_frame(wire) == classic, "classic wire round trip")
check(mod.unpack_can_frame(mod.pack_can_frame(classic)) == classic.__class__(
    classic.can_id, classic.flags, classic.dlc, classic.data), "classic CAN round trip")

extended_rtr = mod.CanFrame(0x1ABCDE, mod.WIRE_EXTENDED | mod.WIRE_RTR, dlc=4, data=b"ABCD")
check(mod.unpack_can_frame(mod.pack_can_frame(extended_rtr)) == extended_rtr, "extended RTR round trip")

fd = mod.CanFrame(0x456, mod.WIRE_FD | mod.WIRE_BRS, data=bytes(range(64)), timestamp_ns=99)
check(mod.unpack_wire_frame(mod.pack_wire_frame(fd)) == fd, "FD wire round trip")
check(mod.unpack_can_frame(mod.pack_can_frame(fd)) == fd.__class__(
    fd.can_id, fd.flags, fd.dlc, fd.data), "FD CAN round trip")
check([mod.dlc_to_length(x) for x in (8, 9, 10, 13, 15)] == [8, 12, 16, 32, 64], "DLC map")

# The stream side is deliberately exercised with partial writes.
wire_tx, wire_rx = socket.socketpair()
can_tx, can_rx = socket.socketpair()
try:
    encoded = mod.pack_wire_frame(extended_rtr)
    wire_tx.sendall(encoded[:11])
    wire_tx.sendall(encoded[11:])
    check(mod.bridge_once(wire_rx, can_rx, direction="wire-to-can", timeout=0.1) == 1, "wire-to-CAN")
    check(mod.unpack_can_frame(can_tx.recv(72)) == extended_rtr, "bridged classic")

    can_tx.sendall(mod.pack_can_frame(fd))
    check(mod.bridge_once(wire_rx, can_rx, direction="can-to-wire",
                          timeout=0.1, timestamp_ns=fd.timestamp_ns) == 1, "CAN-to-wire")
    check(mod.unpack_wire_frame(wire_tx.recv(84)) == fd, "bridged FD")
finally:
    for sock in (wire_tx, wire_rx, can_tx, can_rx):
        sock.close()

for bad in (b"", b"x" * 83, b"x" * 85):
    try:
        mod.unpack_wire_frame(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid wire length accepted")

try:
    mod.CanFrame(1, mod.WIRE_BRS, data=b"x")
except ValueError:
    pass
else:
    raise AssertionError("classic BRS accepted")

print("RESULT: SocketCAN codec and socketpair bridge smoke passed")
PY
