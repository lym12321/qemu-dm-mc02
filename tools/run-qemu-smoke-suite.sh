#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

exec python3 "$script_dir/dm_mc02_test_gate.py" \
    --no-build --smoke-only "$@"
