#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
sim_bin=${SIM_EXECUTABLE:-"$root_dir/target/release/sim"}
[[ -x "$sim_bin" ]] || {
    printf 'blocked: Rust sim not found: %s (build sim-host with the worker feature)\n' "$sim_bin" >&2
    exit 1
}
exec "$sim_bin" worker "$@"
