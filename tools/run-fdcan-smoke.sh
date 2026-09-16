#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-fdcan.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
can_socket="$run_dir/can1.sock"
guest_elf="$run_dir/fdcan.elf"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_fdcan_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_fdcan_smoke.c"

"$qemu_bin" -machine dm-mc02,fdcan-host-ack=on -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -S \
    -serial none -serial none -serial none -serial none -serial none \
    -serial none -serial none \
    -chardev "socket,id=can1,path=$can_socket,server=on,wait=off" \
    -serial chardev:can1 -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$can_socket" && -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$can_socket" && -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

python3 - "$can_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import struct
import sys
import time

can_path, qmp_path = sys.argv[1:]
WIRE = 84
can = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
can.settimeout(2)
can.connect(can_path)
qmp = QmpSession(qmp_path, timeout=2)

command = qmp.command

def recv_exact(size):
    data = bytearray()
    while len(data) < size:
        part = can.recv(size - len(data))
        if not part:
            raise RuntimeError("CAN chardev closed")
        data.extend(part)
    return bytes(data)

def rx_fill():
    text = command("human-monitor-command",
                   {"command-line": "xp /1wx 0x4000a0a4"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if not words:
        raise RuntimeError("FDCAN RX FIFO status was not readable")
    return words[0] & 0x7f

command("cont")
outbound = recv_exact(WIRE)
can_id, flags, dlc = struct.unpack_from("<IIB", outbound)
if (can_id, flags, dlc, outbound[20:28]) != (0x123, 0, 8, b"\x11\x22\x33\x44\x55\x66\x77\x88"):
    raise RuntimeError("unexpected FDCAN TX frame")
print("FDCAN TX:", hex(can_id), "dlc", dlc)
ecr_text = command("human-monitor-command",
                   {"command-line": "xp /1wx 0x4000a040"})
ecr_words = [int(v, 16) for v in re.findall(
    r"0x([0-9a-fA-F]{8})", ecr_text or "")]
if not ecr_words or (ecr_words[0] & 0xff):
    raise RuntimeError("external host ACK policy did not prevent TEC: %r" %
                       ecr_words)
print("FDCAN host ACK policy: TEC remains zero")
time.sleep(0.05)  # let the guest finish RX FIFO configuration

# Malformed host frames must not enter the guest FIFO.  In particular, the
# three-byte reserved area and classic-CAN DLC range are part of the wire
# contract, even though they are not used by the M_CAN message element.
malformed = bytearray(WIRE)
struct.pack_into("<IIB", malformed, 0, 0x321, 0, 8)
malformed[9] = 1
can.sendall(malformed)
time.sleep(0.01)
if rx_fill() != 0:
    raise RuntimeError("FDCAN accepted a non-zero reserved field")

malformed = bytearray(WIRE)
struct.pack_into("<IIB", malformed, 0, 0x321, 0, 0x10)
can.sendall(malformed)
time.sleep(0.01)
if rx_fill() != 0:
    raise RuntimeError("FDCAN accepted a wire DLC with non-zero high bits")

malformed = bytearray(WIRE)
struct.pack_into("<IIB", malformed, 0, 0x321, 0, 9)
can.sendall(malformed)
time.sleep(0.01)
if rx_fill() != 0:
    raise RuntimeError("FDCAN accepted a classic CAN DLC above 8")

# With the guest's configured standard filters and global reject policy, a
# valid but non-matching frame must be discarded before it reaches FIFO0.
unmatched = bytearray(WIRE)
struct.pack_into("<IIB", unmatched, 0, 0x777, 0, 1)
unmatched[20] = ord("U")
can.sendall(unmatched)
time.sleep(0.01)
if rx_fill() != 0:
    raise RuntimeError("FDCAN accepted a non-matching standard ID")

inbound = bytearray(WIRE)
struct.pack_into("<IIB", inbound, 0, 0x321, 0, 8)
inbound[20:28] = b"ABCDEFGH"
can.sendall(inbound)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /10wx 0x20000000"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 10 and words[0] == 0x43414e31 and words[5] == 0x43414e32:
        if words[1] != (0x321 << 18) or words[2] != (8 << 16):
            status = command("human-monitor-command", {"command-line": "xp /4wx 0x4000a0a4"})
            ram = command("human-monitor-command", {"command-line": "xp /4wx 0x4000ac80"})
            raise RuntimeError("FDCAN RX header mismatch: %r status=%s ram=%s" % (words, status, ram))
        if words[3] != 0x44434241 or words[4] != 0x48474645:
            raise RuntimeError("FDCAN RX payload mismatch: %r" % words)
        if words[9] != 0x43414e33:
            raise RuntimeError("FDCAN RX interrupt was not delivered: %r" % words)
        print("FDCAN RX: classic message RAM readback passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("FDCAN RX frame was not observed")

fd = bytearray(WIRE)
struct.pack_into("<IIB", fd, 0, 0x456, 0xc, 15)  # CAN-FD + BRS, DLC 15 = 64 bytes
fd[20:84] = bytes(range(64))
can.sendall(fd)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    text = command("human-monitor-command", {"command-line": "xp /9wx 0x20000000"})
    words = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
    if len(words) >= 9 and words[5] == 0x43414e32:
        if words[6] != 0x03020100 or words[7] != 0x3f3e3d3c:
            raise RuntimeError("FDCAN FD payload mismatch: %r" % words)
        print("FDCAN RX: CAN-FD 64-byte message RAM readback passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("FDCAN CAN-FD frame was not observed")

# The guest then emits a deliberately inconsistent TX element: DLC=64 while
# TXESC provides only eight data bytes.  The bytes after the first eight must
# be zero-filled, never copied from the following message element.
malformed_tx = recv_exact(WIRE)
tx_id, tx_flags, tx_dlc = struct.unpack_from("<IIB", malformed_tx)
if (tx_id, tx_flags, tx_dlc) != (0x456, 0xc, 15):
    raise RuntimeError("unexpected bounded TX frame header: %r" %
                       ((hex(tx_id), hex(tx_flags), tx_dlc),))

# The guest changed filter 0 to SFEC=4 after the second TX.  Wait for its
# marker before injecting the frame so the host/guest handoff is deterministic.
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    marker_text = command("human-monitor-command",
                          {"command-line": "xp /11wx 0x20000000"})
    marker_words = [int(v, 16) for v in re.findall(
        r"0x([0-9a-fA-F]{8})", marker_text or "")]
    if len(marker_words) >= 11 and marker_words[10] == 0x48504d31:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("guest did not arm priority-only filter")

# A matching frame must set HPMS/HPM without being placed into FIFO0.
priority = bytearray(WIRE)
struct.pack_into("<IIB", priority, 0, 0x321, 0, 1)
priority[20] = ord("P")
can.sendall(priority)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    marker_text = command("human-monitor-command",
                          {"command-line": "xp /16wx 0x20000000"})
    marker_words = [int(v, 16) for v in re.findall(
        r"0x([0-9a-fA-F]{8})", marker_text or "")]
    hpms_text = command("human-monitor-command",
                        {"command-line": "xp /1wx 0x4000a094"})
    ir_text = command("human-monitor-command",
                      {"command-line": "xp /1wx 0x4000a050"})
    fifo_text = command("human-monitor-command",
                        {"command-line": "xp /1wx 0x4000a0a4"})
    hpms_words = [int(v, 16) for v in re.findall(
        r"0x([0-9a-fA-F]{8})", hpms_text or "")]
    ir_words = [int(v, 16) for v in re.findall(
        r"0x([0-9a-fA-F]{8})", ir_text or "")]
    fifo_words = [int(v, 16) for v in re.findall(
        r"0x([0-9a-fA-F]{8})", fifo_text or "")]
    if (len(marker_words) >= 12 and marker_words[11] == 0x49543131 and
            hpms_words and ir_words and fifo_words):
        hpms = hpms_words[0]
        if ((hpms >> 8) & 0x7f) != 0 or (hpms & 0x3f) != 0 or (hpms >> 6) & 3:
            raise RuntimeError("priority HPMS mismatch: %r" %
                               ((hex(hpms), hex(ir_words[0])),))
        if fifo_words[0] & 0x7f:
            raise RuntimeError("priority-only frame entered RX FIFO")
        print("FDCAN RX: priority-only HPMS/HPM passed")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("priority-only FDCAN frame was not observed: hpms=%r ir=%r fifo=%r" %
                       (hpms_words, ir_words, fifo_words))
if malformed_tx[20:28] != b"\x01\x02\x03\x04\x05\x06\x07\x08":
    raise RuntimeError("bounded TX payload prefix mismatch")
if malformed_tx[28:84] != b"\x00" * 56:
    raise RuntimeError("TX element boundary was over-read")
print("FDCAN TX: TX element boundary smoke passed")

def qom_bool(property_name):
    value = command("qom-get", {"path": "/machine", "property": property_name})
    if not isinstance(value, bool):
        raise RuntimeError("QOM property %s was not boolean: %r" %
                           (property_name, value))
    return value

# Switch to the strict policy after firmware has raised PC15.  This keeps the
# normal CAN protocol part of the smoke independent from firmware power-on
# ordering while still exercising the runtime switched-rail transition.
if not qom_bool("mcu-power-good"):
    raise RuntimeError("MCU power unexpectedly low before electrical policy change")
odr_before_text = command("human-monitor-command",
                          {"command-line": "xp /1wx 0x58020814"}) or ""
odr_before_words = [int(v, 16) for v in re.findall(
    r"0x([0-9a-fA-F]{8})", odr_before_text)]
marker_before_text = command("human-monitor-command",
                             {"command-line": "xp /16wx 0x20000000"}) or ""
marker_before_words = [int(v, 16) for v in re.findall(
    r"0x([0-9a-fA-F]{8})", marker_before_text)]
if (not odr_before_words or not (odr_before_words[0] & (1 << 15)) or
        len(marker_before_words) < 16 or marker_before_words[15] != 1):
    raise RuntimeError("guest did not leave PC15 high without restarting: "
                       "odr=%s markers=%r" %
                       (odr_before_text, marker_before_words))
command("qom-set", {"path": "/machine", "property": "electrical-power",
                     "value": True})
switch_enabled = qom_bool("5v-switch-enabled")
system_5v_good = qom_bool("system-5v-good")
if not switch_enabled or not system_5v_good:
    odr_text = command("human-monitor-command",
                       {"command-line": "xp /1wx 0x58020814"}) or ""
    raise RuntimeError("5 V rail did not start enabled and good: switch=%r good=%r" %
                       (switch_enabled, system_5v_good) +
                       " GPIOC_ODR=%s" % odr_text)
if not qom_bool("mcu-power-good"):
    raise RuntimeError("MCU power unexpectedly low during transceiver test")
# Release the guest's deterministic wait; it will now pull PC15 low and hold
# the rail off long enough for the host to inject a frame during the outage.
arm = bytearray(WIRE)
struct.pack_into("<IIB", arm, 0, 0x456, 0, 1)
arm[20] = ord("A")
can.sendall(arm)
# The CAN transceiver follows PC15's switched 5 V rail.  The guest toggles the
# actual GPIO below, while VIN stays at its valid default and the MCU remains
# running.
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    marker_text = command("human-monitor-command",
                          {"command-line": "xp /16wx 0x20000000"})
    marker_words = [int(v, 16) for v in re.findall(
        r"0x([0-9a-fA-F]{8})", marker_text or "")]
    if len(marker_words) >= 16 and marker_words[12] == 0x354f4646:
        if qom_bool("5v-switch-enabled") or qom_bool("system-5v-good"):
            raise RuntimeError("QOM still reports 5 V good while PC15 is low")
        if marker_words[15] != 1:
            raise RuntimeError("MCU restarted during transceiver rail test")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("guest did not switch CAN transceiver 5 V off")
off = bytearray(WIRE)
struct.pack_into("<IIB", off, 0, 0x456, 0, 1)
off[20] = ord("X")
can.sendall(off)
time.sleep(0.01)
if rx_fill() != 0:
    raise RuntimeError("FDCAN received a frame while 5 V rail was off")

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    marker_text = command("human-monitor-command",
                          {"command-line": "xp /16wx 0x20000000"})
    marker_words = [int(v, 16) for v in re.findall(
        r"0x([0-9a-fA-F]{8})", marker_text or "")]
    if len(marker_words) >= 16 and marker_words[13] == 0x354f4e31:
        if not qom_bool("5v-switch-enabled") or not qom_bool("system-5v-good"):
            raise RuntimeError("QOM did not restore 5 V good after PC15 high")
        if marker_words[15] != 1:
            raise RuntimeError("MCU restarted during transceiver rail restore")
        break
    time.sleep(0.01)
else:
    raise RuntimeError("guest did not restore CAN transceiver 5 V")
can.sendall(off)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline and rx_fill() == 0:
    time.sleep(0.01)
if rx_fill() == 0:
    raise RuntimeError("FDCAN did not recover after 5 V rail restore")
command("quit")
qmp.close()
can.close()
PY
