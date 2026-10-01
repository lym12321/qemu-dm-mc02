#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-qemu-system-arm}
machine=none
smoke=0

while (($#)); do
    case "$1" in
        --qemu) [[ $# -ge 2 ]] || exit 2; qemu_bin=$2; shift 2 ;;
        --machine) [[ $# -ge 2 ]] || exit 2; machine=$2; shift 2 ;;
        --smoke) smoke=1; shift ;;
        -h|--help) printf 'usage: %s [--qemu path] [--machine name] [--smoke]\n' "$0"; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

if [[ "$qemu_bin" != */* ]]; then
    command -v "$qemu_bin" >/dev/null 2>&1 || { printf 'blocked: %s not found\n' "$qemu_bin" >&2; exit 1; }
else
    [[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU binary is not executable: %s\n' "$qemu_bin" >&2; exit 1; }
fi

if ((smoke)); then
    command -v timeout >/dev/null 2>&1 || { printf '%s\n' 'blocked: timeout is required for bounded smoke' >&2; exit 1; }
    printf 'QEMU version: '; "$qemu_bin" --version | sed -n '1p'
    "$qemu_bin" -machine help >/dev/null
    sock_dir=$(mktemp -d "/tmp/dm-qemu.qemu-smoke.XXXXXX")
    cleanup_smoke() {
        rm -f -- "$sock_dir/qmp.sock"
        rmdir -- "$sock_dir"
    }
    trap cleanup_smoke EXIT
    set +e
    timeout 2s "$qemu_bin" -machine none -nodefaults -display none -monitor none -serial none -S \
        -qmp "unix:$sock_dir/qmp.sock,server=on,wait=off" >/dev/null 2>&1
    result=$?
    set -e
    if [[ $result -ne 124 ]]; then
        printf 'RESULT: smoke failed (exit=%d)\n' "$result" >&2
        exit 1
    fi
    printf '%s\n' 'RESULT: QEMU system smoke passed (-machine none)'
    exit 0
fi

if [[ "$machine" == none ]]; then
    printf '%s\n' 'refusing to start without --smoke or a non-none --machine' >&2
    exit 2
fi
printf 'machine %s requires a guest kernel; use tools/run-mc02-smoke.sh for the bounded DM-MC02 startup test\n' "$machine" >&2
exit 1
