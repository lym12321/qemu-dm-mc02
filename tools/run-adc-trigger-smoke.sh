#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
machine=${DM_MC02_MACHINE:-dm-mc02}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-adc-trigger.XXXXXX")
cleanup() {
    for pid in "${qemu_pids[@]:-}"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
    rm -rf -- "$run_dir"
}
qemu_pids=()
trap cleanup EXIT

for mode in 0 1 2 3 4 5 6 7 8 9 10; do
    qmp_socket="$run_dir/qmp-$mode.sock"
    cosim_socket="$run_dir/cosim-$mode.sock"
    guest_elf="$run_dir/adc-trigger-$mode.elf"
    arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
        -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
        -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_adc_trigger_smoke.ld" \
        -DADC_TRIGGER_MODE="$mode" -o "$guest_elf" \
        "$root_dir/smoke/dm_mc02_adc_trigger_smoke.c"

    "$qemu_bin" -machine "$machine" -kernel "$guest_elf" -nodefaults \
        -display none -monitor none -S \
        -chardev "socket,id=cosim,path=$cosim_socket,server=on,wait=off" \
        -serial chardev:cosim -qmp "unix:$qmp_socket,server=on,wait=off" \
        >/dev/null 2>"$run_dir/qemu-$mode.stderr" &
    qemu_pids+=("$!")
    for _ in $(seq 1 300); do
        [[ -S "$cosim_socket" && -S "$qmp_socket" ]] && break
        sleep 0.01
    done
    [[ -S "$cosim_socket" && -S "$qmp_socket" ]] || {
        sed -n '1,80p' "$run_dir/qemu-$mode.stderr" >&2
        exit 1
    }

    python3 - "$cosim_socket" "$qmp_socket" "$mode" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import socket
import struct
import sys
import time

cosim_path, qmp_path, mode_text = sys.argv[1:]
mode = int(mode_text)
MAGIC = 0x32434D44
HEADER = 28

def frame(kind, sequence, timestamp, payload=b""):
    body = struct.pack("<IHHIQQ", MAGIC, 1, kind, len(payload),
                       sequence, timestamp) + payload
    return struct.pack("<I", len(body)) + body

def recv_exact(sock, size):
    out = bytearray()
    while len(out) < size:
        part = sock.recv(size - len(out))
        if not part:
            raise RuntimeError("socket closed")
        out.extend(part)
    return bytes(out)

cosim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
cosim.settimeout(2.0)
cosim.connect(cosim_path)
outer = struct.unpack("<I", recv_exact(cosim, 4))[0]
recv_exact(cosim, outer)
cosim.sendall(frame(1, 0, 0))

def adc_input(sequence, timestamp, first, second):
    cosim.sendall(frame(5, sequence, timestamp,
                        struct.pack("<HHI", 4, first, 0)))
    cosim.sendall(frame(5, sequence + 1, timestamp + 1,
                        struct.pack("<HHI", 19, second, 0)))

qmp = QmpSession(qmp_path, timeout=2.0)
qbuf = bytearray()
command = qmp.command
def words(address, count, width):
    text = command("human-monitor-command", {
        "command-line": f"xp /{count}{width} 0x{address:x}"}) or ""
    digits = 4 if width == "hx" else 8
    return [int(value, 16) for value in
            re.findall(r"0x([0-9a-fA-F]{%d})" % digits, text)]
def wait_samples(expected):
    for _ in range(5000):
        current = words(0x20000100, 4, "hx")
        if current == expected:
            return current
        time.sleep(0.001)
    raise RuntimeError("ADC samples did not reach %r" % (expected,))
def wait_result_word(index, expected):
    for _ in range(1000):
        current = words(0x20000000 + index * 4, 1, "wx")
        if current == [expected]:
            return
    raise RuntimeError("RESULT[%d] did not reach %#x" % (index, expected))

adc_input(1, 1_000_000, 0x1111, 0x2222)
command("cont")

# Wait for the guest's fixed setup marker.  Polling observes state; it does
# not advance simulation time or participate in the expected result.
for _ in range(1000):
    marker = words(0x20000018, 1, "wx")
    if marker[:1] == [0x544f4b31]:
        break
else:
    raise RuntimeError("ADC startup marker was not observed")
if mode == 0:
    wait_samples([0x1111, 0x2222, 0xa5a5, 0xa5a5])
    command("stop")
    first = words(0x20000100, 4, "hx")
    cr = words(0x40022008, 1, "wx")
    if len(first) < 4 or len(cr) < 1:
        raise RuntimeError("missing ADC readback")
    if first[:2] != [0x1111, 0x2222] or first[2:] != [0xa5a5, 0xa5a5]:
        raise RuntimeError("non-continuous first sequence mismatch: %r" % first)
    if cr[0] & (1 << 2):
        raise RuntimeError("non-continuous conversion did not clear ADSTART")
    adc_input(3, 3_000_000, 0x3333, 0x4444)
    command("cont")
    command("stop")
    second = words(0x20000100, 4, "hx")
    if second != first:
        raise RuntimeError("non-continuous conversion repeated: %r -> %r" %
                           (first, second))
    print("RESULT: ADC software trigger non-continuous one-sequence smoke passed")
elif mode == 1:
    # Pause before judging the first buffer: a fast host may already have
    # executed several identical continuous sequences by the time it polls.
    command("stop")
    cr = words(0x40022008, 1, "wx")
    if len(cr) < 1 or not (cr[0] & (1 << 2)):
        raise RuntimeError("continuous conversion did not remain started")
    command("cont")
    # The link is serviced by the running virtual machine; deliver the
    # changed source after resuming so it is consumed deterministically.
    adc_input(3, 3_000_000, 0x3333, 0x4444)
    wait_samples([0x3333, 0x4444, 0x3333, 0x4444])
    command("stop")
    second = words(0x20000100, 4, "hx")
    if second != [0x3333, 0x4444, 0x3333, 0x4444]:
        raise RuntimeError("continuous virtual repeat mismatch: %r" % second)
    print("RESULT: ADC software trigger continuous virtual-time repeat smoke passed")
elif mode == 2:
    # TIM8 is the selected conversion source and DMA is configured before it
    # is enabled.  Its first update event is the conversion trigger.
    wait_samples([0x1111, 0x2222, 0xa5a5, 0xa5a5])
    command("stop")
    result = words(0x20000000, 10, "wx")
    cr = words(0x40022008, 1, "wx")
    tim8_cr1 = words(0x40010400, 1, "wx")
    tim8_psc = words(0x40010428, 1, "wx")
    tim8_arr = words(0x4001042c, 1, "wx")
    if len(result) < 10 or len(cr) < 1 or len(tim8_cr1) < 1 or len(tim8_psc) < 1 or len(tim8_arr) < 1:
        raise RuntimeError("missing TIM8 external-trigger readback")
    if result[3] != ((7 << 5) | (1 << 10) | 1) or result[7] != 0x54494d31 or result[8] != 0x444d4131 or result[9] != 0x45585437:
        raise RuntimeError("TIM8/ADC setup ordering mismatch: %r" % result)
    if not (tim8_cr1[0] & 1) or tim8_psc[0] != 239 or tim8_arr[0] != 99:
        raise RuntimeError("TIM8 PSC/ARR/CEN mismatch: %r" %
                           (tim8_cr1, tim8_psc, tim8_arr))
    if cr[0] & (1 << 2):
        raise RuntimeError("TIM8 external trigger did not clear ADSTART")
    samples = words(0x20000100, 4, "hx")
    if samples != [0x1111, 0x2222, 0xa5a5, 0xa5a5]:
        raise RuntimeError("TIM8 external-trigger samples mismatch: %r" % samples)
    print("RESULT: ADC TIM8 rising external-trigger one-sequence smoke passed")
elif mode in (6, 7, 8, 9):
    # Validate the timer master-mode boundary rather than accepting a board
    # callback that emits TIM8_TRGO regardless of CR2.MMS/MMS2.
    wait_samples([0x1111, 0x2222, 0xa5a5, 0xa5a5])
    command("stop")
    result = words(0x20000000, 12, "wx")
    cr2 = words(0x40010404, 1, "wx")
    egr = words(0x40010414, 1, "wx")
    cr1 = words(0x40010400, 1, "wx")
    ccr1 = words(0x40010434, 1, "wx")
    samples = words(0x20000100, 4, "hx")
    if (len(result) < 12 or len(cr2) < 1 or len(egr) < 1 or
            len(cr1) < 1 or len(ccr1) < 1):
        raise RuntimeError("missing TIM8 master-mode readback")
    expected_cr2 = {6: 0, 7: 1 << 4, 8: 3 << 4,
                    9: 2 << 20}[mode]
    if cr2[0] != expected_cr2:
        raise RuntimeError("TIM8 CR2 master-mode mismatch: %r" % cr2)
    if mode == 6 and (not (egr[0] & 1) or (cr1[0] & 1)):
        raise RuntimeError("UG reset trigger did not remain stopped")
    if mode == 7 and not (cr1[0] & 1):
        raise RuntimeError("enable trigger did not start TIM8")
    if mode == 8 and ccr1[0] != 50:
        raise RuntimeError("compare trigger CCR1 mismatch: %r" % ccr1)
    if samples != [0x1111, 0x2222, 0xa5a5, 0xa5a5]:
        raise RuntimeError("TIM8 master-mode samples mismatch: %r" % samples)
    label = {6: "UG reset", 7: "CEN enable", 8: "CC1 compare",
             9: "TRGO2 update"}[mode]
    print("RESULT: ADC TIM8 %s master-trigger smoke passed" % label)
elif mode == 10:
    # TIM3 channel 4 uses a board route override for the OC4REF master event.
    wait_samples([0x1111, 0x2222, 0xa5a5, 0xa5a5])
    command("stop")
    result = words(0x20000000, 12, "wx")
    cr = words(0x40022008, 1, "wx")
    tim3_cr1 = words(0x40000400, 1, "wx")
    tim3_cr2 = words(0x40000404, 1, "wx")
    tim3_ccmr2 = words(0x4000041c, 1, "wx")
    if (len(result) < 12 or len(cr) < 1 or len(tim3_cr1) < 1 or
            len(tim3_cr2) < 1 or len(tim3_ccmr2) < 1):
        raise RuntimeError("missing TIM3 OC4REF readback")
    if result[3] != ((15 << 5) | (1 << 10) | 1) or result[7] != 0x54494d33 or result[8] != 0x444d4133 or result[9] != 0x54433434:
        raise RuntimeError("TIM3/ADC setup ordering mismatch: %r" % result)
    if tim3_cr2[0] != (7 << 4) or tim3_ccmr2[0] != (6 << 12) or not (tim3_cr1[0] & 1):
        raise RuntimeError("TIM3 OC4REF configuration mismatch: %r %r %r" %
                           (tim3_cr1, tim3_cr2, tim3_ccmr2))
    if cr[0] & (1 << 2):
        raise RuntimeError("TIM3 external trigger did not clear ADSTART")
    samples = words(0x20000100, 4, "hx")
    if samples != [0x1111, 0x2222, 0xa5a5, 0xa5a5]:
        raise RuntimeError("TIM3 OC4REF samples mismatch: %r" % samples)
    print("RESULT: ADC TIM3_CH4 OC4REF external-trigger smoke passed")
elif mode == 4 or mode == 5:
    # Source 0 is not TIM8_TRGO, and a falling-only ADC accepts neither of
    # the rising TIM8 update events.  In both cases the DMA destination must
    # retain its initialization pattern.
    time.sleep(0.02)
    command("stop")
    samples = words(0x20000100, 4, "hx")
    if samples != [0xa5a5, 0xa5a5, 0xa5a5, 0xa5a5]:
        raise RuntimeError("unexpected ADC trigger acceptance: %r" % samples)
    print("RESULT: ADC trigger source/edge rejection smoke passed (mode %d)" % mode)
else:
    result = words(0x20000000, 9, "wx")
    cr = words(0x40022008, 1, "wx")
    if len(result) < 9 or len(cr) < 1:
        raise RuntimeError("missing ADCAL readback")
    if not (result[7] & (1 << 31)):
        raise RuntimeError("ADCAL write was not retained in recorded CR: %r" % result[7])
    if result[8] != 0x43414c31:
        raise RuntimeError("ADCAL completion marker missing: %r" % result[8])
    if cr[0] & (1 << 31):
        raise RuntimeError("ADCAL did not clear after virtual-time wait")
    # This mode intentionally uses one non-continuous sequence, so a fast
    # virtual run may have already cleared ADSTART by the time QMP reads it.
    # The completed DMA sequence below proves the post-calibration restart.
    if not (cr[0] & 1):
        raise RuntimeError("ADC did not re-enable after calibration")
    samples = words(0x20000100, 4, "hx")
    if samples != [0x1111, 0x2222, 0xa5a5, 0xa5a5]:
        raise RuntimeError("post-ADCAL samples mismatch: %r" % samples)
    print("RESULT: ADCAL virtual-time clear and post-calibration DMA smoke passed")
command("quit")
qmp.close()
cosim.close()
PY
done
