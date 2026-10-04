#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qtest --socket qmp --socket cosim \
    --wait-socket cosim --wait-socket qtest --wait-socket qmp \
    --python-arg "{cosim}" --python-arg "{qtest}" --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02 -nodefaults \
    -display none -monitor none \
    -accel qtest \
    -qtest "unix:{qtest},server=on,wait=on" \
    -qmp "unix:{qmp},server=on,wait=off" \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim <<'PY'
from dm_mc02_qmp import QmpSession
from dm_mc02_firmware_rtf import deadline_qmp_call
import json
import socket
import struct
import sys
import threading
import time

COSIM_SOCKET, QTEST_SOCKET, QMP_SOCKET = sys.argv[1:]
MAGIC = 0x32434D44
RESET, STEP, STEP_ACK, DIAGNOSTICS, RESET_ACK = 1, 2, 3, 4, 5
LEGACY_TELEMETRY, V2_TELEMETRY = 3, 7
REQUEST_COUNT = 20_000

def exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise RuntimeError("socket closed")
        result.extend(chunk)
    return bytes(result)

def read_body(sock):
    body_len = struct.unpack("<I", exact(sock, 4))[0]
    if not 28 <= body_len <= 552:
        raise RuntimeError("invalid co-sim body length: %d" % body_len)
    body = exact(sock, body_len)
    magic, version, kind, payload_len = struct.unpack_from("<IHHI", body)
    header_size = {1: 28, 2: 40}.get(version)
    if (magic != MAGIC or header_size is None or
            body_len < header_size or body_len != header_size + payload_len):
        raise RuntimeError("invalid co-sim frame header: %r" %
                           ((magic, version, kind, payload_len, body_len),))
    expected_payload = {
        (1, LEGACY_TELEMETRY): 12,
        (2, RESET): 0,
        (2, STEP_ACK): 16,
        (2, DIAGNOSTICS): 40,
        (2, RESET_ACK): 24,
        (2, 6): 20,
        (2, V2_TELEMETRY): 24,
    }.get((version, kind))
    if payload_len != expected_payload:
        raise RuntimeError("unexpected co-sim frame shape: %r" %
                           ((version, kind, payload_len),))
    return body

def read_v2_kind(sock, wanted_kind):
    for _ in range(8):
        body = read_body(sock)
        if (struct.unpack_from("<H", body, 4)[0] == 2 and
                struct.unpack_from("<H", body, 6)[0] == wanted_kind):
            return body
    raise RuntimeError("missing v2 kind: %d" % wanted_kind)

def v2_frame(kind, step_id, timestamp_ns, payload=b"", dt_ns=0, session_id=0):
    body = struct.pack("<IHHIQQQI", MAGIC, 2, kind, len(payload),
                       step_id, timestamp_ns, dt_ns, session_id) + payload
    return struct.pack("<I", len(body)) + body

def link_diagnostics():
    value = qmp_command("qom-get", {
        "path": "/machine", "property": "cosim-diagnostics"})
    return {key: int(number) for key, number in
            (field.split("=", 1) for field in value.split(","))}

def v2_fields(body):
    if struct.unpack_from("<H", body, 4)[0] != 2:
        raise RuntimeError("unexpected legacy frame in v2 reset session")
    return (struct.unpack_from("<H", body, 6)[0],
            struct.unpack_from("<Q", body, 12)[0],
            struct.unpack_from("<Q", body, 20)[0],
            struct.unpack_from("<I", body, 36)[0])

def read_qtest_ok(sock):
    response = bytearray()
    while not response.endswith(b"\n"):
        chunk = sock.recv(64)
        if not chunk:
            raise RuntimeError("qtest socket closed")
        response.extend(chunk)
    if response != b"OK\n":
        raise RuntimeError("qtest writel failed: %r" % bytes(response))

def read_qtest_line(sock):
    response = bytearray()
    while not response.endswith(b"\n"):
        chunk = sock.recv(64)
        if not chunk:
            raise RuntimeError("qtest socket closed")
        response.extend(chunk)
    return bytes(response)

import re

# qtest wait=on keeps QEMU initialization behind this connection.  This is
# required because the qtest server's lifetime timer is created on OPENED.
qtest = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
qtest.settimeout(3.0)
for _ in range(100):
    try:
        qtest.connect(QTEST_SOCKET)
        break
    except ConnectionRefusedError:
        time.sleep(0.01)
else:
    raise RuntimeError("could not connect to qtest socket")

qmp = QmpSession(QMP_SOCKET, timeout=3.0)

qmp_command = qmp.command

cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
cosim.settimeout(10.0)
cosim.connect(COSIM_SOCKET)
read_body(cosim)  # initial v1 compatibility telemetry
initial_session = 0x10203040
cosim.sendall(v2_frame(RESET, 0, 0, session_id=initial_session))
reset_ack = read_v2_kind(cosim, RESET_ACK)
if (v2_fields(reset_ack) != (RESET_ACK, 0, 0, initial_session) or
        struct.unpack_from("<I", reset_ack, 40)[0] != 0):
    raise RuntimeError("missing v2 RESET_ACK")

send_error = []

def send_requests():
    try:
        for index in range(1, REQUEST_COUNT + 1):
            cosim.sendall(v2_frame(DIAGNOSTICS, index, index,
                                 session_id=initial_session))
    except BaseException as exc:
        send_error.append(exc)

sender = threading.Thread(target=send_requests, daemon=True)
sender.start()
time.sleep(0.1)
if not sender.is_alive():
    raise RuntimeError("test did not establish control-plane backpressure")

for _ in range(100):
    diagnostics_text = qmp_command("qom-get", {
        "path": "/machine", "property": "cosim-diagnostics"})
    match = re.search(r"(?:^|,)tx_queue=(\d+)(?:,|$)", diagnostics_text)
    if match and int(match.group(1)) == 8:
        break
    time.sleep(0.01)
else:
    raise RuntimeError("control TX queue did not become full: %r" %
                       diagnostics_text)

# PC13 is the board power-output enable reflected in telemetry board_flags bit 0.
# The write occurs while all eight control TX slots are occupied.
qtest.sendall(b"writel 0x58020814 0x2000\n")
read_qtest_ok(qtest)
if qmp_command("qom-get", {"path": "/machine",
                              "property": "out2-enabled"}) is not True:
    raise RuntimeError("qtest did not set PC13/out2-enabled")
pending_text = qmp_command("qom-get", {"path": "/machine",
                                       "property": "cosim-diagnostics"})
if not re.search(r"(?:^|,)telemetry_pending=1(?:,|$)", pending_text):
    raise RuntimeError("PC13 change did not enter deferred telemetry state: %r" %
                       pending_text)
# Keep the qtest connection open until the shell cleanup terminates QEMU.  The
# qtest server in this pinned QEMU can receive a second CLOSED event if the
# client closes immediately after a command.

clock_error = []
clock_stop = threading.Event()

def advance_qtest_clock():
    try:
        while not clock_stop.is_set():
            qtest.sendall(b"clock_step 1000000\n")
            response = read_qtest_line(qtest)
            if not response.startswith(b"OK "):
                raise RuntimeError("qtest clock_step failed: %r" % response)
    except BaseException as exc:
        clock_error.append(exc)

clock_thread = threading.Thread(target=advance_qtest_clock, daemon=True)
clock_thread.start()

diagnostics = 0
telemetry_seen = False
telemetry_after_diagnostics = 0
while diagnostics < REQUEST_COUNT:
    body = read_body(cosim)
    version = struct.unpack_from("<H", body, 4)[0]
    kind = struct.unpack_from("<H", body, 6)[0]
    if version == 2 and kind == DIAGNOSTICS:
        diagnostics += 1
        step_id = struct.unpack_from("<Q", body, 12)[0]
        if step_id != diagnostics:
            raise RuntimeError("diagnostics response gap: %d != %d" %
                               (step_id, diagnostics))
    elif version == 1 and kind == LEGACY_TELEMETRY:
        board_flags = body[28 + 9]
        telemetry_after_diagnostics = diagnostics
        if board_flags & 1:
            telemetry_seen = True
    elif version == 2 and kind == V2_TELEMETRY:
        board_flags = struct.unpack_from("<I", body, 40 + 12)[0]
        telemetry_after_diagnostics = diagnostics
        if board_flags & 1:
            telemetry_seen = True

if not telemetry_seen:
    print("deferred telemetry diagnostics:",
          qmp_command("qom-get", {"path": "/machine",
                                    "property": "cosim-diagnostics"}),
          flush=True)
    # The final control response can drain before the deferred telemetry is
    # flushed. Keep virtual time moving while the remaining TX work settles.
    cosim.settimeout(0.2)
    deadline = time.monotonic() + 3.0
    while not telemetry_seen and time.monotonic() < deadline:
        try:
            body = read_body(cosim)
        except TimeoutError:
            continue
        version = struct.unpack_from("<H", body, 4)[0]
        kind = struct.unpack_from("<H", body, 6)[0]
        if version == 1 and kind == LEGACY_TELEMETRY:
            telemetry_after_diagnostics = diagnostics
            telemetry_seen = bool(body[28 + 9] & 1)
        elif version == 2 and kind == V2_TELEMETRY:
            telemetry_after_diagnostics = diagnostics
            telemetry_seen = bool(
                struct.unpack_from("<I", body, 40 + 12)[0] & 1)

clock_stop.set()
clock_thread.join(timeout=2.0)
if clock_thread.is_alive():
    raise RuntimeError("qtest clock thread remained blocked")
if clock_error:
    raise RuntimeError("qtest clock failed: %s" % clock_error[0])
sender.join(timeout=2.0)
if sender.is_alive():
    raise RuntimeError("request sender remained blocked after response drain")
if send_error:
    raise RuntimeError("request sender failed: %s" % send_error[0])
if not telemetry_seen:
    raise RuntimeError("pending telemetry was not retried after control drain")
if telemetry_after_diagnostics < 8:
    raise RuntimeError("telemetry was emitted before the control queue drained")

# Leave one response slot, then receive a final DIAGNOSTICS and complete STEP
# together. Filling all eight slots first would stop CharBackend receive and
# leave the STEP in the host socket, outside the RX boundary under test.
cosim.settimeout(3.0)
before_reset = link_diagnostics()
if before_reset["tx_queue"] or before_reset["telemetry_pending"]:
    raise RuntimeError("first pressure stage did not finish draining: %r" %
                       before_reset)
old_session = before_reset["v2_session"]
if old_session != initial_session:
    raise RuntimeError("pressure stage lost its identified session")
# The old STEP has valid time both before and after reset; only its session
# becomes stale. The existing qtest clock thread is stopped at this point.
qtest.sendall(b"clock_step 0\n")
clock_reply = read_qtest_line(qtest).split()
if len(clock_reply) != 2 or clock_reply[0] != b"OK":
    raise RuntimeError("could not observe frozen qtest clock: %r" % clock_reply)
frozen_time = int(clock_reply[1])
if frozen_time <= REQUEST_COUNT + 4097:
    raise RuntimeError("frozen clock cannot follow pressure diagnostics")
current = before_reset
reset_request_count = 0
pressure_deadline = time.monotonic() + 4.0
for index in range(1, 4097):
    if time.monotonic() >= pressure_deadline:
        raise RuntimeError("reset pressure deadline expired: %r" % current)
    identity = REQUEST_COUNT + index
    cosim.sendall(v2_frame(DIAGNOSTICS, identity, identity,
                         session_id=old_session))
    expected_rx = before_reset["rx_frames"] + index
    for _ in range(100):
        current = deadline_qmp_call(
            qmp, pressure_deadline, "control pressure admission", link_diagnostics)
        if current["rx_frames"] == expected_rx:
            break
        if (current["rx_frames"] > expected_rx or
                time.monotonic() >= pressure_deadline):
            raise RuntimeError("unexpected reset pressure admission: %r" % current)
        time.sleep(0.001)
    else:
        raise RuntimeError("reset pressure request was not admitted: %r" % current)
    reset_request_count = index
    if current["tx_queue"] == 7:
        break
else:
    raise RuntimeError("reset stage did not leave one control slot: %r" % current)

final_identity = REQUEST_COUNT + reset_request_count + 1
old_step = v2_frame(STEP, final_identity + 1, frozen_time + 1,
                    b"\0" * 60, dt_ns=1, session_id=old_session)
batch = v2_frame(DIAGNOSTICS, final_identity, frozen_time,
                 session_id=old_session) + old_step
cosim.sendall(batch)
expected_rx = current["rx_frames"] + 1
while True:
    blocked = deadline_qmp_call(
        qmp, pressure_deadline, "complete blocked RX STEP", link_diagnostics)
    if (blocked["tx_queue"] == 8 and blocked["rx_buffered"] == len(old_step)
            and blocked["rx_frames"] == expected_rx):
        break
    if blocked["rx_frames"] > expected_rx:
        raise RuntimeError("blocked old STEP was already processed: %r" % blocked)
    time.sleep(0.001)  # Wait for chardev IO, not simulated device time.
reset_request_count += 1
if (blocked["rx_bad_frames"] != before_reset["rx_bad_frames"] or
        blocked["rx_dropped_bytes"] != before_reset["rx_dropped_bytes"] or
        blocked["imu_queue"] != before_reset["imu_queue"] or
        blocked["consume_queue"] != before_reset["consume_queue"]):
    raise RuntimeError("blocked STEP advanced input or error state: %r" % blocked)

# No virtual clock advances until both resets have preserved the RX head.
retained = []
for _ in range(2):
    deadline_qmp_call(qmp, pressure_deadline, "blocked RX reset",
                      lambda: qmp_command("system_reset"))
    state = deadline_qmp_call(
        qmp, pressure_deadline, "retained blocked RX", link_diagnostics)
    if (state["rx_buffered"] != len(old_step) or
            state["rx_frames"] != expected_rx or
            state["rx_bad_frames"] != blocked["rx_bad_frames"] or
            state["rx_dropped_bytes"] != blocked["rx_dropped_bytes"] or
            state["v2_session"] == old_session or state["imu_queue"] or
            state["consume_queue"]):
        raise RuntimeError("reset altered the blocked complete STEP: %r" % state)
    retained.append(state)
if retained[0]["v2_session"] == retained[1]["v2_session"]:
    raise RuntimeError("repeated reset reused a session")
qmp.settimeout(3.0)

clock_stop.clear()
clock_thread = threading.Thread(target=advance_qtest_clock, daemon=True)
clock_thread.start()

# Replies already committed to the socket, including a possible partial head,
# must still decode completely before the new session marker. Unsent old frames
# may be discarded at reset; any delivered old reply must remain in order.
old_replies = 0
for _ in range(reset_request_count + 2):
    body = read_body(cosim)
    kind, step_id, timestamp_ns, session_id = v2_fields(body)
    if kind == RESET:
        reset_time, new_session = timestamp_ns, session_id
        if step_id != 0 or new_session == 0 or new_session == old_session:
            raise RuntimeError("invalid reset session marker: %r" % (v2_fields(body),))
        break
    if kind != DIAGNOSTICS:
        raise RuntimeError("unexpected frame before reset marker: %r" %
                           (v2_fields(body),))
    old_replies += 1
    identity = REQUEST_COUNT + old_replies
    expected_time = frozen_time if identity == final_identity else identity
    if (step_id, timestamp_ns, session_id) != (identity, expected_time, old_session):
        raise RuntimeError("old reset-pressure reply lost framing/order: %r" %
                           (v2_fields(body),))
else:
    raise RuntimeError("reset marker was not delivered after old replies")
if old_replies == 0:
    raise RuntimeError("reset pressure produced no committed old reply")
if reset_time != frozen_time or new_session != retained[-1]["v2_session"]:
    raise RuntimeError("RESET did not use the frozen clock/latest session")

ack = read_body(cosim)
if (v2_fields(ack) != (RESET_ACK, 0, reset_time, new_session) or
        struct.unpack_from("<I", ack, 40)[0] != 0):
    raise RuntimeError("reset ACK was not ordered after RESET: %r" % (v2_fields(ack),))
snapshot = read_body(cosim)
kind, _, snapshot_time, session_id = v2_fields(snapshot)
if (kind != V2_TELEMETRY or session_id != new_session or
        snapshot_time <= reset_time or struct.unpack_from("<I", snapshot, 52)[0] & 1):
        raise RuntimeError("reset telemetry did not describe the new session: %r" %
                       ((v2_fields(snapshot), reset_time,
                         struct.unpack_from("<I", snapshot, 52)[0]),))

drain_deadline = time.monotonic() + 2.0
while True:
    rejected = deadline_qmp_call(
        qmp, drain_deadline, "stale complete RX rejection", link_diagnostics)
    if rejected["rx_buffered"] == 0:
        break
    time.sleep(0.001)
if (rejected["rx_frames"] != expected_rx or
        rejected["rx_bad_frames"] != blocked["rx_bad_frames"] + 1 or
        rejected["rx_dropped_bytes"] != blocked["rx_dropped_bytes"] + len(old_step) or
        rejected["imu_queue"] or rejected["consume_queue"]):
    raise RuntimeError("old complete STEP was not rejected exactly once: %r" % rejected)
qmp.settimeout(3.0)

cosim.sendall(v2_frame(STEP, 1, reset_time + 1, b"\0" * 60,
                     dt_ns=1, session_id=new_session))
step_ack = read_body(cosim)
if (v2_fields(step_ack) != (STEP_ACK, 1, reset_time + 1, new_session) or
        struct.unpack_from("<I", step_ack, 40)[0] != 0):
    raise RuntimeError("first STEP after pressure/reset was not accepted: %r" %
                       (v2_fields(step_ack),))

clock_stop.set()
clock_thread.join(timeout=2.0)
if clock_thread.is_alive() or clock_error:
    raise RuntimeError("reset qtest clock failed: %r" % clock_error)
after_reset = link_diagnostics()
if (after_reset["rx_buffered"] or after_reset["rx_frames"] != expected_rx + 1 or
        after_reset["rx_bad_frames"] != rejected["rx_bad_frames"] or
        after_reset["rx_dropped_bytes"] != rejected["rx_dropped_bytes"]):
    raise RuntimeError("new session STEP did not recover after blocked RX reset")
print("BLOCKED_RX_RESET_EVIDENCE: " + json.dumps({
    "before": before_reset, "blocked": blocked, "retained": retained,
    "rejected": rejected, "after": after_reset, "frozen_time": frozen_time,
    "old_step_hex": old_step.hex(), "batch_hex": batch.hex(),
    "old_replies": old_replies, "requests": reset_request_count,
    "reset_time": reset_time, "new_session": new_session,
}, sort_keys=True))
qmp.close()
cosim.close()
print("reset pressure evidence: old replies=%d/%d, tx_short_writes=%d->%d" %
      (old_replies, reset_request_count, before_reset["tx_short_writes"],
       after_reset["tx_short_writes"]))
print("RESULT: QEMU telemetry retry and reset framing survived full control TX backpressure")
PY
