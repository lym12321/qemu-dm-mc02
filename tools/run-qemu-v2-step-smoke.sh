#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir:$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
mode=normal
if [[ $# -ne 0 ]]; then
    [[ $# -eq 1 && $1 == --peer-exit ]] || {
        printf 'usage: %s [--peer-exit]\n' "$0" >&2
        exit 2
    }
    mode=peer-exit
fi
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

if [[ $mode == normal ]]; then
python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket cosim \
    --python-arg "$root_dir" --python-arg "{cosim}" -- \
    "$qemu_bin" -machine dm-mc02 -nodefaults -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim <<'PY'
import subprocess
import sys

root, socket_path = sys.argv[1:]
worker = [
    root + "/tools/run-worker.sh",
    "--cosim", socket_path,
    "--protocol", "v2",
    "--rate", "1000",
    "--frames", "16",
    "--adc-input", "4:4660",
    "--adc-voltage", "19:1.25",
]
process = subprocess.Popen(worker, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
stdout, stderr = process.communicate(timeout=10)
if process.returncode != 0:
    raise RuntimeError("v2 worker failed: %s\n%s" % (stdout, stderr))
print("OBSERVED: legacy worker completed QEMU v2 RESET/STEP/ACK")
PY
printf '%s\n' 'RESULT: QEMU v2 RESET/STEP/ACK smoke passed'
fi

command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2
    exit 77
}
run_dir=$(mktemp -d "/tmp/dm-qemu.v2-exchange.XXXXXX")
guest_elf="$run_dir/lab.elf"
trap 'rm -rf -- "$run_dir"' EXIT
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -O2 -Wall -Wextra -Werror -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_lab_smoke.c"

python3 - "$qemu_bin" "$guest_elf" "$mode" "$run_dir/result.json" <<'PY'
from contextlib import ExitStack
import json
from pathlib import Path
import select
import signal
import sys
import time

from sim.transports import connect_unix
from sim.transports import step_codec as protocol
from sim.transports.cosim import (
    QemuTimestampMapper, StepFramedSocket, V2SessionReset,
    poll_v2_response, send_v2_step, wait_v2_reset_ack, wait_v2_response,
)
from tools.dm_mc02_firmware_rtf import deadline_qmp_call
from tools.dm_mc02_qmp import QmpSession
from tools.dm_mc02_test_harness import running_qemu

binary, firmware, mode, result_path = sys.argv[1:]
command = [binary, "-machine", "dm-mc02", "-kernel", firmware,
           "-nodefaults", "-display", "none", "-monitor", "none", "-S",
           "-qmp", "unix:{qmp},server=on,wait=off",
           "-chardev", "socket,id=cosim,path={cosim},server=on,wait=off",
           "-serial", "chardev:cosim"]
deadline = time.monotonic() + 20.0
owners, resources, streams, controls, tx, rx = [], [], [], [], [], []
identities = []
idle_resets = {}
active = None
primary = None
report = {"schema": 1, "mode": mode, "status": "running", "epochs": [],
          "initial_reset_acks": [],
          "native_reset_acks": [], "idle_events": [],
          "acks": [], "cleanup": [],
          "limits": ["Transport admission and epoch metadata only.",
                     "No plant, global CPU barrier, calibration or RTF gate."]}


def interrupted(signum, frame):
    raise RuntimeError(f"native dual-peer exchange interrupted by signal {signum}")


for signum in (signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, interrupted)


def starttime(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    return int(text[text.rfind(")") + 2:].split()[19])


def remaining():
    for index, owner in enumerate(owners):
        code = owner.process.poll()
        if code is not None:
            raise RuntimeError(f"native peer {index} exited early ({code})")
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("native dual-peer exchange deadline expired")
    return value


def qmp(index, name, arguments=None):
    remaining()
    value = deadline_qmp_call(controls[index], deadline, "dual-peer exchange",
                             lambda: controls[index].command(name, arguments))
    remaining()
    return value


def poll_idle_peer(index, source):
    stream = streams[index]
    assert stream is not active
    response = poll_v2_response(stream, rx[index])
    if response is None:
        return None
    frame, payload = response
    active_index = streams.index(active)
    event = {"peer": index, "kind": frame.header.kind,
             "session": frame.header.session_id, "step": frame.header.step_id,
             "time_ns": frame.header.t_sim_ns, "source": source,
             "active_peer": active_index,
             "active_session": tx[active_index].session_id,
             "active_step": tx[active_index].last_step_id}
    if isinstance(payload, protocol.StepDonePayload):
        # Advisory DONE is reported exactly; missing sensors are not consumed PASS.
        event.update(status=payload.status, consumed_mask=payload.consumed_mask,
                     missing_mask=payload.missing_mask)
    report["idle_events"].append(event)
    if frame.header.kind == protocol.RESET:
        assert payload is None
        idle_resets[index] = V2SessionReset(
            frame.header.step_id, frame.header.t_sim_ns, frame.header.session_id)
    return response


def service_io(timeout):
    # Wait owns active RX; nonblocking poll owns only the other streams. Once
    # RESET is observed, leave that peer's actual ACK queued for caller handshake.
    for index, stream in enumerate(streams):
        if stream is not active and index not in idle_resets:
            poll_idle_peer(index, "service_io")
    budget = min(timeout, .005, remaining())
    _, writable, _ = select.select(
        [stream.sock for index, stream in enumerate(streams)
         if stream is active or index not in idle_resets],
        [stream.sock for stream in streams if stream.tx_pending],
        [], budget)
    for stream in streams:
        if stream.sock in writable:
            stream.flush()
    remaining()


def reset_peer(index, session_id):
    global active
    active = streams[index]
    frame = protocol.StepFrame(protocol.StepHeader(
        2, protocol.RESET, 0, 0, 0, 0, session_id), b"")
    tx[index].reset()
    tx[index].accept(frame)
    rx[index].begin_reset(0, 0, session_id)
    active.send(protocol.encode_step_frame(frame))
    ack = wait_v2_response(active, rx[index], protocol.RESET_ACK, 0, 0,
                           timeout=min(2.0, remaining()), service_io=service_io)
    value = protocol.decode_reset_ack_payload(ack.payload)
    assert value.status == 0 and ack.header.session_id == session_id
    return value.virtual_time_ns


def step_peer(index, timestamp):
    global active
    active = streams[index]
    step_id = tx[index].last_step_id + 1
    sample = protocol.ImuSampleV2((0., 0., 0.), (0., 0., 1.),
                                 (0.,) * 3, (0.,) * 3, timestamp, step_id)
    payload = protocol.encode_imu_sample_v2(sample)
    frame = protocol.StepFrame(protocol.StepHeader(
        2, protocol.STEP, len(payload), step_id, timestamp,
        timestamp - tx[index].last_t_sim_ns, tx[index].session_id), payload)
    tx[index].accept(frame)
    ack = send_v2_step(active, rx[index], protocol.encode_step_frame(frame),
                       step_id, timestamp, service_io=service_io)
    assert protocol.decode_step_ack_payload(ack.payload).status == 0
    report["acks"].append({"peer": index, "session": ack.header.session_id,
                           "step": ack.header.step_id, "time_ns": ack.header.t_sim_ns})


def validator_state(validator):
    return (validator.initialized, validator.session_id,
            validator.last_step_id, validator.last_t_sim_ns,
            validator.require_step_done)


def complete_native_reset(index, reset):
    global active
    active = streams[index]
    latest = wait_v2_reset_ack(active, rx[index], reset, service_io=service_io)
    # ACK commits transport reset metadata. The caller alone changes TX baseline.
    tx[index].reset()
    tx[index].accept(protocol.StepFrame(protocol.StepHeader(
        2, protocol.RESET, 0, latest.step_id, latest.t_sim_ns, 0,
        latest.session_id), b""))
    report["native_reset_acks"].append(
        {"peer": index, "session": latest.session_id, "step": latest.step_id,
         "time_ns": latest.t_sim_ns})
    return latest


def native_reset(index):
    global active
    qmp(index, "system_reset")
    active = streams[index]
    try:
        wait_v2_response(active, rx[index], protocol.STEP_ACK,
                         rx[index].last_step_id + 1,
                         rx[index].last_t_sim_ns + 1_000_000,
                         timeout=min(2., remaining()), service_io=service_io)
    except V2SessionReset as reset:
        return complete_native_reset(index, reset)
    else:
        raise AssertionError("native system_reset emitted no RESET marker")


try:
    with ExitStack() as stack:
        try:
            for index in range(2):
                report["stage"] = f"startup_peer_{index}"
                owner = stack.enter_context(running_qemu(
                    command, sockets=("qmp", "cosim"),
                    startup_timeout=min(3.0, remaining())))
                owners.append(owner)
                identity = {"peer": index, "pid": owner.process.pid,
                            "starttime": starttime(owner.process.pid)}
                if identity["starttime"] is None:
                    raise RuntimeError("native peer identity disappeared during startup")
                identities.append(identity)
                control = QmpSession(str(owner.sockets["qmp"]),
                                     timeout=min(2., remaining()),
                                     connect_timeout=min(2., remaining()), close_timeout=2.)
                resources.append((f"peer {index} QMP", control))
                controls.append(control)
                sock = connect_unix(str(owner.sockets["cosim"]), min(2., remaining()))
                resources.append((f"peer {index} co-sim", sock))
                streams.append(StepFramedSocket(sock))
                tx.append(protocol.StepSessionValidator(protocol.DIRECTION_HOST_TO_QEMU))
                rx.append(protocol.StepSessionValidator(protocol.DIRECTION_QEMU_TO_HOST,
                                                        require_step_done=False))
                assert qmp(index, "query-status")["running"] is False
                epoch = reset_peer(index, 71 + index)
                report["epochs"].append(epoch)
                report["initial_reset_acks"].append(
                    {"peer": index, "session": rx[index].session_id, "epoch_ns": epoch})

            report["stage"] = "advance_peer_0"
            qmp(0, "cont")
            while True:
                state = qmp(0, "qom-get", {"path": "/machine", "property": "board-state"})
                if state["timestamp_ns"] > report["epochs"][1]:
                    break
                remaining()
            qmp(0, "stop")
            assert qmp(0, "query-status")["running"] is False
            # The native RESET marker follows old-session physical bytes.
            # Do not begin a new host validator before consuming that marker.
            first_reset = native_reset(0)
            epoch0 = first_reset.t_sim_ns
            epoch1 = report["epochs"][1]
            assert epoch0 > epoch1
            report["epochs"] = [epoch0, epoch1]
            # This literal wire fixture has no physics or environmental accuracy claim.
            step_peer(0, epoch0 + 1_000_000)
            step_peer(1, 1_000_000)
            logical_origin = 123_456_789
            mappers = [QemuTimestampMapper(epoch, logical_origin)
                       for epoch in report["epochs"]]
            for mapper, epoch in zip(mappers, report["epochs"]):
                assert mapper.map(epoch + 17, 0) == logical_origin + 17
                assert mapper.unmap(logical_origin + 29, 0) == epoch + 29
            peer1_session = rx[1].session_id

            if mode == "peer-exit":
                report["stage"] = "negative_peer_exit"
                # Native exit0 before cleanup must still fail the consumer.
                qmp(1, "quit")
                owners[1].process.wait(timeout=min(2., remaining()))
                remaining()
                raise AssertionError("native peer early exit was not rejected")

            report["stage"] = "unsolicited_reset_peer_0"
            # Consume valid old idle heads before the native reset; no physical
            # tail is deleted. Readiness then arranges a real buffered marker for
            # the original active exchange's service_io, without a sleep.
            while poll_idle_peer(0, "pre_reset") is not None:
                remaining()
            assert not idle_resets
            qmp(0, "system_reset")
            assert qmp(0, "query-status")["running"] is False
            readable, _, _ = select.select([streams[0].sock], [], [],
                                            min(2., remaining()))
            assert readable, "native idle peer reset produced no readable wire"
            remaining()
            report["stage"] = "active_peer_1_with_idle_reset"
            step_peer(1, 2_000_000)
            assert 0 in idle_resets, "active wait did not poll the idle RESET"
            reset_events = [event for event in report["idle_events"]
                            if event["peer"] == 0 and event["kind"] == protocol.RESET]
            assert len(reset_events) == 1
            assert reset_events[0]["source"] == "service_io"
            assert (reset_events[0]["active_peer"], reset_events[0]["active_session"],
                    reset_events[0]["active_step"]) == (1, peer1_session, 2)
            assert tx[1].session_id == rx[1].session_id == peer1_session
            unaffected = (validator_state(tx[1]), validator_state(rx[1]),
                          mappers[1].qemu_origin_ns, mappers[1].worker_origin_ns)
            report["stage"] = "caller_handshake_idle_peer_0"
            latest = complete_native_reset(0, idle_resets[0])
            del idle_resets[0]
            assert latest.session_id != first_reset.session_id and latest.t_sim_ns >= epoch0
            assert rx[0].session_id == latest.session_id
            assert (validator_state(tx[1]), validator_state(rx[1]),
                    mappers[1].qemu_origin_ns, mappers[1].worker_origin_ns) == unaffected
            # Only the caller binds the latest peer epoch; no execution owner is present.
            mappers[0] = QemuTimestampMapper(latest.t_sim_ns, logical_origin)
            report["reset"] = {"session": latest.session_id, "time_ns": latest.t_sim_ns}
            step_peer(0, latest.t_sim_ns + 1_000_000)
            assert mappers[0].map(latest.t_sim_ns + 17, 0) == logical_origin + 17
            assert mappers[1].map(epoch1 + 17, 0) == logical_origin + 17
            remaining()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup_error = None
            for name, resource in reversed(resources):
                try:
                    resource.close()
                except BaseException as exc:
                    if primary is not None:
                        primary.add_note(f"{name} close failed: {exc}")
                    elif cleanup_error is None:
                        cleanup_error = exc
                    else:
                        cleanup_error.add_note(f"{name} close failed: {exc}")
            if primary is None and cleanup_error is not None:
                raise cleanup_error
except BaseException as exc:
    primary = exc

for identity, owner in zip(identities, owners):
    try:
        alive = starttime(identity["pid"]) == identity["starttime"]
        identity.update(alive=alive, returncode=owner.process.poll())
        report["cleanup"].append(identity)
        if alive or identity["returncode"] is None:
            raise RuntimeError(f"native peer {identity['peer']} was not reaped")
    except BaseException as exc:
        if primary is None:
            primary = exc
        else:
            primary.add_note(f"native identity cleanup check failed: {exc}")
report["status"] = "failed" if primary is not None else "passed"
if primary is not None:
    report["error"] = f"{type(primary).__name__}: {primary}"
    report["notes"] = getattr(primary, "__notes__", [])
if primary is not None:
    print("RESULT_JSON: " + json.dumps(report, sort_keys=True), flush=True)
    raise primary
Path(result_path).write_text(json.dumps(report, sort_keys=True) + "\n")
PY
result_json=$(cat "$run_dir/result.json")
rm -rf -- "$run_dir"
trap - EXIT
[[ ! -e "$run_dir" ]]
printf 'RESULT_JSON: %s\n' "$result_json"
printf '%s\n' 'RESULT: native dual-peer co-sim exchange smoke passed'
