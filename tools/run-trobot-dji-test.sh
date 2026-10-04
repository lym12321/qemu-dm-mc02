#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
dji_python=${PYTHON:-"$root_dir/.venv/bin/python"}
[[ -x "$dji_python" ]] || dji_python=python3
[[ $# -ge 1 && $# -le 3 && -f "$1" && -x "$qemu_bin" ]] || {
    printf '%s\n' 'Usage: run-trobot-dji-test.sh DjiTest.elf [report.json [firmware-source]]' >&2
    exit 1
}
report_path=${2:-"$root_dir/build/test-results/trobot-dji.json"}
firmware_source=${3:-"$root_dir/../trobot"}
# Even failure before the test child starts must replace an old PASS report.
"$dji_python" - "$report_path" <<'PY'
import json
from pathlib import Path
import sys
target = Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(dict(result='failed', error='harness did not finish',
                                 cases=[], missing=['consumer not started'])) + '\n')
PY

if "$dji_python" "$script_dir/dm_mc02_test_harness.py" --timeout 35 \
    --socket qmp --socket uart --socket can \
    --python-arg '{qmp}' --python-arg '{uart}' --python-arg '{can}' \
    --python-arg "$1" --python-arg "$qemu_bin" --python-arg "$report_path" \
    --python-arg "$firmware_source" -- \
    "$qemu_bin" -machine dm-mc02,fdcan-host-ack=on,iwdg-boot-grace-ms=0 \
    -kernel "$1" -nodefaults -display none -monitor none -S \
    -qmp 'unix:{qmp},server=on,wait=off' -serial none \
    -chardev 'socket,id=uart,path={uart},server=on,wait=off' -serial chardev:uart \
    -serial none -serial none -serial none -serial none -serial none \
    -chardev 'socket,id=can,path={can},server=on,wait=off' -serial chardev:can <<'PY'
from dm_mc02_dji_test import main
raise SystemExit(main())
PY
then
    exit 0
else
    harness_status=$?
    # The shared harness owns final process cleanup; its failure invalidates the
    # consumer report, including when all firmware assertions had passed.
    "$dji_python" - "$report_path" "$harness_status" <<'PY'
import json
from pathlib import Path
import sys
target = Path(sys.argv[1])
report = json.loads(target.read_text())
report['result'] = 'failed'
report['harness_status'] = int(sys.argv[2])
report['error'] = (report.get('error') or '') + '; bounded harness/cleanup failed'
target.write_text(json.dumps(report, indent=2) + '\n')
PY
    exit "$harness_status"
fi
