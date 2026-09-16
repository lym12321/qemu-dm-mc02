#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
host_smoke="$root_dir/build/host/dm_mc02_cosim_smoke"

[[ -x "$host_smoke" ]] || {
    printf 'blocked: prebuilt Host co-sim smoke not found: %s\n' \
        "$host_smoke" >&2
    exit 77
}
"$host_smoke"
