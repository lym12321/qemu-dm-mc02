#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-iwdg.XXXXXX")
cleanup() {
    if [[ -n "${qemu_pid:-}" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

guest_elf="$run_dir/iwdg.elf"
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -nostartfiles -Wl,--gc-sections \
    -Wl,--build-id=none -Wl,-T,"$root_dir/smoke/dm_mc02_smoke.ld" \
    -x c -o "$guest_elf" - <<'EOF'
#include <stdint.h>
void Reset_Handler(void);
__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = { 0x20020000u, (uint32_t)(uintptr_t)Reset_Handler };
void Reset_Handler(void)
{
    volatile uint32_t *r = (volatile uint32_t *)0x20000000u;
    volatile uint32_t *boots = (volatile uint32_t *)0x20000010u;
    volatile uint32_t *iwdg = (volatile uint32_t *)0x58004800u;
    volatile uint32_t *rcc = (volatile uint32_t *)0x58024400u;
    uint32_t boot = ++*boots;

    if (boot == 1) {
        iwdg[1] = 6;       /* locked PR write must be ignored */
        iwdg[2] = 31;      /* locked RLR write must be ignored */
        r[1] = iwdg[1];
        r[2] = iwdg[2];
        iwdg[0] = 0x5555;  /* unlock */
        iwdg[1] = 6;       /* /256 */
        iwdg[2] = 31;      /* short deterministic timeout */
        while ((iwdg[3] & 0x03u) != 0u) {
            /* PR/RLR cross into the LSI domain asynchronously. */
        }
        iwdg[0] = 0xaaaa;  /* reload */
        iwdg[0] = 0xcccc;  /* start */
        r[0] = 0x49574431; /* IWD1 */
    } else {
        r[0] = 0x49574452; /* IWDR: reset observed */
        r[3] = boot;
        r[4] = rcc[0xd0 / sizeof(uint32_t)];
        rcc[0xd0 / sizeof(uint32_t)] = 1u << 16; /* RCC_RSR.RMVF */
        r[5] = rcc[0xd0 / sizeof(uint32_t)];
        for (;;) {
            __asm__ volatile("wfi" ::: "memory");
        }
    }
    for (;;) {
        __asm__ volatile("wfi" ::: "memory");
    }
}
EOF

qmp_socket="$run_dir/qmp.sock"
"$qemu_bin" -machine dm-mc02,iwdg-boot-grace-ms=0 -kernel "$guest_elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" >/dev/null \
    2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys
import time

sock = QmpSession(sys.argv[1], timeout=2.0)
cmd = sock.command
def words(address, count):
    text = cmd("human-monitor-command", {"command-line": f"xp /{count}wx 0x{address:x}"}) or ""
    return [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{8})", text)]
cmd("cont")
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    result = words(0x20000000, 6)
    if len(result) >= 6 and result[0] == 0x49574452:
        if (result[1:3] != [0, 0xfff] or result[3] < 2 or
                (result[4] & (1 << 26)) == 0 or result[5] != 0):
            raise SystemExit(f"IWDG lock/default or reset mismatch: {result!r}")
        print("RESULT: IWDG reset reason/RCC_RSR and RMVF bare-metal smoke passed")
        print("  result words:", " ".join(f"0x{x:08x}" for x in result))
        cmd("quit")
        break
    time.sleep(0.005)
else:
    raise SystemExit(f"IWDG timeout reset was not observed: {words(0x20000000, 6)!r}")
PY
