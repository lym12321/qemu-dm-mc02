#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# The release gate is intentionally explicit: it starts the configured
# Release trobot from reset, waits for the first scheduler tick, measures a
# continuous virtual-time window, and repeats it three times.  The collector
# owns QMP/process cleanup and keeps the old short wall-clock diagnostic mode.
# 1.0x remains the target; 0.1% accommodates host/QMP sampling jitter while
# the collector still reports the exact measured factor.
exec "$script_dir/collect-firmware-rtf.sh" \
    --warmup 0 \
    --virtual-seconds 60 \
    --runs 3 \
    --min-rtf 0.999 \
    --ready-tick 250 \
    "$@"
