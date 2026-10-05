#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
referee_python="$root_dir/.venv/bin/python"
[[ -x "$referee_python" ]] || {
    printf '%s\n' 'Project Python environment missing: uv sync --locked --group dev' >&2
    exit 1
}
if ! "$referee_python" -c 'import crccheck; import yaml' >/dev/null 2>&1; then
    printf '%s\n' 'Project Python dependencies missing: uv sync --locked --group dev' >&2
    exit 1
fi
exec "$referee_python" "$script_dir/dm_mc02_referee_test.py" "$@"
