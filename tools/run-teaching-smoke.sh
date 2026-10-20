#!/usr/bin/env bash
# Build the public first lesson, then exercise existing Lab/GDB consumers.
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd -- "$root_dir"

run_dir="$root_dir/build/teaching/validation-$(date -u +%Y%m%dT%H%M%S)-$$"
mkdir -p -- "$root_dir/build/teaching"
mkdir -- "$run_dir"
teaching_stage=build
teaching_failure_report() {
    teaching_exit=$?
    if (( teaching_exit == 0 )); then
        return
    fi
    if [[ -f "$run_dir/consumer/report.json" ]]; then
        cp -- "$run_dir/consumer/report.json" "$run_dir/report.json"
        return
    fi
    python3 - "$run_dir/report.json" "$teaching_stage" "$teaching_exit" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if not path.exists():
    path.write_text(json.dumps({"status": "failed", "stage": sys.argv[2],
                                "exit_code": int(sys.argv[3])}, indent=2) + "\n")
PY
}
trap teaching_failure_report EXIT

for profile in Debug Release; do
    cmake -S examples/teaching -B "build/teaching/$profile" \
        -DCMAKE_BUILD_TYPE="$profile" > "$run_dir/$profile-configure.log" 2>&1
    cmake --build "build/teaching/$profile" --target gpio_key \
        > "$run_dir/$profile-build.log" 2>&1
done

# Test-only mutation: preserve the original source and canonical vendor tree.
# The unchanged direct observer must reject a program which always writes low.
teaching_stage=constant-low-build
python3 - "$root_dir" "$run_dir/constant-low" <<'PY'
from pathlib import Path
import shutil
import sys

root, destination = map(Path, sys.argv[1:])
source = destination / "source/examples/teaching"
shutil.copytree(root / "examples/teaching", source)
vendor = destination / "source/third_party"
vendor.mkdir()
(vendor / "stm32h7").symlink_to(root / "third_party/stm32h7", target_is_directory=True)
main = source / "gpio_key.c"
original = main.read_text()
before = "pressed ? GPIO_PIN_SET : GPIO_PIN_RESET"
if original.count(before) != 1:
    raise ValueError("expected exactly one lesson GPIO output expression")
main.write_text(original.replace(before, "pressed ? GPIO_PIN_RESET : GPIO_PIN_RESET"))
PY
teaching_cc=$(sed -n 's/^CMAKE_C_COMPILER:[^=]*=//p' build/teaching/Debug/CMakeCache.txt)
teaching_asm=$(sed -n 's/^CMAKE_ASM_COMPILER:[^=]*=//p' build/teaching/Debug/CMakeCache.txt)
cmake -S "$run_dir/constant-low/source/examples/teaching" \
    -B "$run_dir/constant-low/build" -DCMAKE_BUILD_TYPE=Debug \
    -DCMAKE_C_COMPILER="$teaching_cc" -DCMAKE_ASM_COMPILER="$teaching_asm" \
    > "$run_dir/constant-low-configure.log" 2>&1
cmake --build "$run_dir/constant-low/build" --target gpio_key \
    > "$run_dir/constant-low-build.log" 2>&1

teaching_stage=consumer
uv run --frozen python smoke/dm_mc02_teaching_smoke.py \
    --debug-elf build/teaching/Debug/gpio_key.elf \
    --release-elf build/teaching/Release/gpio_key.elf \
    --bad-elf "$run_dir/constant-low/build/gpio_key.elf" \
    --manifest examples/teaching/gpio-key.yaml --output "$run_dir/consumer" "$@"
cp -- "$run_dir/consumer/report.json" "$run_dir/report.json"
printf 'Teaching validation report: %s\n' "$run_dir/report.json"
