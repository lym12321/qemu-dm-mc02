#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
python_bin=${SIM_PYTHON:-"$root_dir/.venv/bin/python"}
if [[ ! -x "$python_bin" ]]; then
    python_bin=$(command -v python3) || { printf '%s\n' 'BLOCKED: Python 3 is required' >&2; exit 78; }
fi
exec "$python_bin" "$root_dir/smoke/simulation_smoke.py" "$@"
