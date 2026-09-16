#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
command -v cmake >/dev/null 2>&1 || { printf '%s\n' 'blocked: cmake is required' >&2; exit 1; }
cmake -S "$root_dir" -B "$root_dir/build/host" >/dev/null
cmake --build "$root_dir/build/host" --target dm_mc02_cosim_smoke >/dev/null
"$root_dir/build/host/dm_mc02_cosim_smoke"
