#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
business_python="$root_dir/.venv/bin/python"
[[ -x "$business_python" ]] || {
    printf '%s\n' 'Project Python environment missing: uv sync --locked --group dev --extra mujoco' >&2
    exit 1
}
exec "$business_python" "$script_dir/dm_mc02_trobot_business.py" "$@"
