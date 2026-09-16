#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-}
if [[ -z "$qemu_bin" ]]; then
    # Performance measurements must use the current source build.  Falling
    # back to the historical build/qemu-release binary can silently report
    # numbers for a different machine model and invalidate comparisons.
    qemu_bin="$root_dir/build/qemu/qemu-system-arm"
fi
elf=${DM_MC02_ELF:-"$root_dir/../trobot/build/Release/trobot.elf"}
warmup=0.25
duration=1.0
virtual_seconds=''
runs=1
min_rtf=''
ready_tick=1
poll_interval=0.250
iwdg_boot_grace_ms=''
adc_accurate_timing=0
tcg_tb_size=''
tcg_thread=''

usage() {
    printf 'usage: %s [--elf path] [--warmup seconds] [--duration seconds] [--virtual-seconds seconds] [--runs count] [--min-rtf factor] [--ready-tick ticks] [--poll-interval seconds] [--iwdg-boot-grace-ms ms] [--adc-accurate-timing] [--tcg-tb-size bytes] [--tcg-thread single|multi]\n' "$0"
}

while (($#)); do
    case "$1" in
        --elf) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; elf=$2; shift 2 ;;
        --warmup) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; warmup=$2; shift 2 ;;
        --duration) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; duration=$2; shift 2 ;;
        --virtual-seconds) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; virtual_seconds=$2; shift 2 ;;
        --runs) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; runs=$2; shift 2 ;;
        --min-rtf) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; min_rtf=$2; shift 2 ;;
        --ready-tick) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; ready_tick=$2; shift 2 ;;
        --poll-interval) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; poll_interval=$2; shift 2 ;;
        --iwdg-boot-grace-ms) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; iwdg_boot_grace_ms=$2; shift 2 ;;
        --adc-accurate-timing) adc_accurate_timing=1; shift ;;
        --tcg-tb-size) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; tcg_tb_size=$2; shift 2 ;;
        --tcg-thread) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; tcg_thread=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }
[[ -f "$elf" ]] || { printf 'blocked: firmware ELF not found: %s\n' "$elf" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
command -v arm-none-eabi-nm >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-nm is required to locate xTickCount' >&2
    exit 1
}

tick_address=$(arm-none-eabi-nm -n "$elf" | awk '
    $3 == "xTickCount" && !found { print "0x" $1; found = 1 }
')
[[ -n "$tick_address" ]] || {
    printf 'blocked: xTickCount is not present in %s\n' "$elf" >&2
    exit 1
}

python3 - "$root_dir" "$qemu_bin" "$elf" "$tick_address" "$warmup" "$duration" "$virtual_seconds" "$runs" "$min_rtf" "$ready_tick" "$poll_interval" "$iwdg_boot_grace_ms" "$adc_accurate_timing" "$tcg_tb_size" "$tcg_thread" <<'PY'
from dm_mc02_qmp import QmpSession
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

(
    root, qemu, elf, tick_address, warmup, duration, virtual_seconds,
    runs, min_rtf, ready_tick, poll_interval, iwdg_boot_grace_ms,
    adc_accurate_timing, tcg_tb_size, tcg_thread,
) = sys.argv[1:]
warmup = float(warmup)
duration = float(duration)
virtual_seconds = None if virtual_seconds == "" else float(virtual_seconds)
runs = int(runs)
min_rtf = None if min_rtf == "" else float(min_rtf)
ready_tick = int(ready_tick)
poll_interval = float(poll_interval)
if warmup < 0 or duration <= 0:
    raise SystemExit("warmup must be >= 0 and duration must be > 0")
if virtual_seconds is not None and virtual_seconds <= 0:
    raise SystemExit("virtual seconds must be > 0")
if runs < 1 or ready_tick < 0 or poll_interval <= 0:
    raise SystemExit("runs/ready-tick/poll-interval have invalid values")
if min_rtf is not None and min_rtf < 0:
    raise SystemExit("minimum RTF must be non-negative")

def read_tick(sock):
    result = sock.command(
        "human-monitor-command",
        {"command-line": "xp /1wx " + tick_address},
    )
    matches = re.findall(r"0x([0-9a-fA-F]{1,8})", str(result or ""))
    if not matches:
        raise RuntimeError("cannot parse xTickCount from QMP response: %r" % result)
    return int(matches[-1], 16)

def read_iwdg_timeouts(sock):
    result = sock.command(
        "qom-get",
        {"path": "/machine", "property": "iwdg-diagnostics"},
    )
    diagnostics = str(result or "")
    match = re.search(r"timeouts=(\d+)", diagnostics)
    if not match:
        raise RuntimeError("cannot read IWDG diagnostics: %r" % diagnostics)
    return int(match.group(1)), diagnostics

def process_cpu_seconds(pid):
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii") as stream:
            fields = stream.read().split()
        ticks = int(fields[13]) + int(fields[14])
        return ticks / os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    except (FileNotFoundError, IndexError, ValueError):
        return None

def process_rss_kib(pid):
    try:
        with open("/proc/%d/status" % pid, encoding="ascii") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, IndexError, ValueError):
        return None
    return None

def guest_reset(previous_tick, current_tick):
    # The normal test window is far from the 32-bit FreeRTOS tick wrap.  A
    # decrease in that window is therefore the first observable reset state.
    return current_tick < previous_tick and previous_tick < 0xffff0000

def run_one(run_number):
    run_dir = tempfile.mkdtemp(prefix="dm-mc02-firmware-rtf-")
    qmp_path = os.path.join(run_dir, "qmp.sock")
    process = None
    qmp = None
    try:
        machine = "dm-mc02"
        if iwdg_boot_grace_ms:
            grace_ms = int(iwdg_boot_grace_ms)
            if grace_ms < 0:
                raise SystemExit("iwdg boot grace must be non-negative")
            machine += ",iwdg-boot-grace-ms=%d" % grace_ms
        if adc_accurate_timing == "1":
            machine += ",adc-accurate-timing=on"
        command = [qemu]
        if tcg_tb_size or tcg_thread:
            properties = []
            if tcg_thread:
                if tcg_thread not in ("single", "multi"):
                    raise SystemExit("tcg thread must be single or multi")
                properties.append("thread=%s" % tcg_thread)
            if tcg_tb_size:
                properties.append("tb-size=%d" % int(tcg_tb_size))
            command += ["-accel", "tcg," + ",".join(properties)]
        command += [
            "-machine", machine, "-kernel", elf, "-nodefaults",
            "-display", "none", "-monitor", "none", "-serial", "none",
            "-qmp", "unix:%s,server=on,wait=off" % qmp_path,
        ]
        startup_start = time.monotonic_ns()
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5.0
        while not os.path.exists(qmp_path) and time.monotonic() < deadline:
            if process.poll() is not None:
                error = process.stderr.read().strip()
                raise RuntimeError("QEMU exited during startup: %s" % error)
            time.sleep(0.005)
        if not os.path.exists(qmp_path):
            raise RuntimeError("QEMU did not create its QMP socket")

        qmp = QmpSession(qmp_path, timeout=2.0)
        initial_timeouts, _ = read_iwdg_timeouts(qmp)
        start_tick = read_tick(qmp)
        ready_tick_value = start_tick
        previous_tick = start_tick
        startup_ready_ns = time.monotonic_ns()

        if virtual_seconds is not None:
            while ready_tick_value < ready_tick:
                time.sleep(poll_interval)
                ready_tick_value = read_tick(qmp)
                if guest_reset(previous_tick, ready_tick_value):
                    raise RuntimeError("guest reset before startup-ready point")
                previous_tick = ready_tick_value
            startup_ready_ns = time.monotonic_ns()
            sample_tick = ready_tick_value
            target_ticks = int(round(virtual_seconds * 1000.0))
            sample_start_ns = startup_ready_ns
            cpu_start = process_cpu_seconds(process.pid)
            rss_kib = process_rss_kib(process.pid)
            timeout = time.monotonic() + max(30.0, virtual_seconds * 4.0 + 10.0)
            while True:
                time.sleep(poll_interval)
                end_tick = read_tick(qmp)
                if guest_reset(previous_tick, end_tick):
                    reset_timeouts, reset_diagnostics = read_iwdg_timeouts(
                        qmp)
                    raise RuntimeError(
                        "guest reset during virtual-time sample: "
                        "tick %d -> %d, IWDG timeouts=%d, diagnostics=%s" %
                        (previous_tick, end_tick, reset_timeouts,
                         reset_diagnostics))
                previous_tick = end_tick
                tick_delta = (end_tick - sample_tick) & 0xffffffff
                if tick_delta >= target_ticks:
                    break
                if time.monotonic() >= timeout:
                    raise RuntimeError("virtual-time sample exceeded host timeout")
            sample_end_ns = time.monotonic_ns()
        else:
            time.sleep(warmup)
            warm_tick = read_tick(qmp)
            sample_start_ns = time.monotonic_ns()
            sample_tick = warm_tick
            cpu_start = process_cpu_seconds(process.pid)
            rss_kib = process_rss_kib(process.pid)
            time.sleep(duration)
            end_tick = read_tick(qmp)
            sample_end_ns = time.monotonic_ns()
            tick_delta = (end_tick - sample_tick) & 0xffffffff

        cpu_end = process_cpu_seconds(process.pid)
        final_timeouts, diagnostics = read_iwdg_timeouts(qmp)
        elapsed_seconds = (sample_end_ns - sample_start_ns) / 1_000_000_000.0
        virtual_interval = tick_delta / 1000.0
        rtf = virtual_interval / elapsed_seconds if elapsed_seconds else 0.0
        cpu_utilization = None
        if cpu_start is not None and cpu_end is not None:
            cpu_utilization = (cpu_end - cpu_start) / elapsed_seconds * 100.0
        result = {
            "run": run_number,
            "startup_latency": (startup_ready_ns - startup_start) / 1_000_000_000.0,
            "elapsed": elapsed_seconds,
            "tick_delta": tick_delta,
            "virtual_seconds": virtual_interval,
            "rtf": rtf,
            "cpu": cpu_utilization,
            "rss": rss_kib,
            "initial_timeouts": initial_timeouts,
            "final_timeouts": final_timeouts,
            "diagnostics": diagnostics,
        }
        mode = "gate" if virtual_seconds is not None else "baseline"
        passed = final_timeouts == initial_timeouts and (min_rtf is None or rtf >= min_rtf)
        print("RESULT: QEMU firmware RTF %s run %d %s" %
              (mode, run_number, "passed" if passed else "FAILED"))
        print("  startup latency: %.6f s" % result["startup_latency"])
        print("  host sample interval: %.6f s" % elapsed_seconds)
        print("  FreeRTOS tick delta: %d" % tick_delta)
        print("  virtual sample interval: %.6f s" % virtual_interval)
        print("  realtime factor: %.6fx" % rtf)
        print("  effective FreeRTOS ticks/s: %.3f" % (tick_delta / elapsed_seconds))
        if cpu_utilization is not None:
            print("  QEMU process CPU utilization: %.1f%%" % cpu_utilization)
        if rss_kib is not None:
            print("  QEMU process RSS at sample start: %d KiB" % rss_kib)
        print("  IWDG diagnostics: %s" % diagnostics)
        if not passed:
            raise RuntimeError("RTF gate failed or guest reset detected")
        return result
    finally:
        if qmp is not None:
            try:
                qmp.close()
            except OSError:
                pass
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
            if process.stderr is not None:
                process.stderr.close()
        shutil.rmtree(run_dir, ignore_errors=True)

results = []
for run_number in range(1, runs + 1):
    results.append(run_one(run_number))

if virtual_seconds is not None and runs > 1:
    def values(key):
        return [item[key] for item in results if item[key] is not None]
    def summary(key):
        data = values(key)
        return "min=%.6f mean=%.6f max=%.6f" % (
            min(data), sum(data) / len(data), max(data)) if data else "n/a"
    print("RESULT: QEMU firmware %.3f-virtual-second RTF gate passed" %
          virtual_seconds)
    print("  runs: %d" % runs)
    print("  startup latency: %s s" % summary("startup_latency"))
    print("  realtime factor: %s" % summary("rtf"))
    print("  CPU utilization: %s%%" % summary("cpu"))
    print("  RSS at sample start: %s KiB" % summary("rss"))
PY
