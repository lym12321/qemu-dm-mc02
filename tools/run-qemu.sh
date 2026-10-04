#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-$root_dir/build/qemu/qemu-system-arm}
machine=dm-mc02
elf=${DM_MC02_ELF:-}
uart=USART1
debug=0
gdb_port=1234
gdb_port_set=0
qmp_path=
cosim_path=
smoke=0
runtime_options=0

usage() {
    cat <<EOF
usage: $0 [options]

  --elf PATH        Firmware ELF (default: DM_MC02_ELF)
  --qemu PATH       QEMU binary (default: QEMU_SYSTEM_ARM or project build)
  --machine NAME    dm-mc02, optionally followed by QEMU machine properties
  --debug           Pause at reset and enable GDB on 127.0.0.1:1234
  --gdb-port PORT   GDB port, 1..65535 (requires --debug)
  --uart NAME       Terminal UART: USART1 (default), USART2, USART3,
                    UART5, UART7, USART10, or none; case insensitive
  --qmp PATH        Native QMP Unix socket, server mode without waiting
  --cosim PATH      Co-sim Unix socket for an external worker, without waiting
  --smoke           Bounded empty-machine probe; rejects firmware options
  -h, --help        Show this help

The terminal multiplexes UART and the native QEMU monitor (Ctrl-a c to switch).
With --uart none, only the monitor uses the terminal. Co-sim always uses slot 0.
UART5 is receive-only in the default DM-MC02 profile.
EOF
}

require_value() {
    if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
        printf 'error: %s requires a value\n' "$1" >&2
        exit 2
    fi
}

while (($#)); do
    case "$1" in
        --qemu) require_value "$@"; qemu_bin=$2; shift 2 ;;
        --machine) require_value "$@"; machine=$2; shift 2 ;;
        --elf) require_value "$@"; elf=$2; runtime_options=1; shift 2 ;;
        --debug) debug=1; runtime_options=1; shift ;;
        --gdb-port) require_value "$@"; gdb_port=$2; gdb_port_set=1; runtime_options=1; shift 2 ;;
        --uart) require_value "$@"; uart=${2^^}; runtime_options=1; shift 2 ;;
        --qmp) require_value "$@"; qmp_path=$2; runtime_options=1; shift 2 ;;
        --cosim) require_value "$@"; cosim_path=$2; runtime_options=1; shift 2 ;;
        --smoke) smoke=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'error: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

if ((smoke && runtime_options)); then
    printf '%s\n' 'error: --smoke cannot be combined with firmware, UART, GDB, QMP or co-sim options' >&2
    exit 2
fi
if ((smoke)) && [[ "$machine" != dm-mc02 && "$machine" != none ]]; then
    printf '%s\n' 'error: --smoke probes an empty machine; only --machine none is applicable' >&2
    exit 2
fi

if [[ "$qemu_bin" != */* ]]; then
    qemu_path=$(command -v "$qemu_bin") || { printf 'error: QEMU binary not found: %s\n' "$qemu_bin" >&2; exit 1; }
    qemu_bin=$qemu_path
fi
[[ -f "$qemu_bin" && -x "$qemu_bin" ]] || { printf 'error: QEMU binary is not executable: %s\n' "$qemu_bin" >&2; exit 1; }
qemu_bin=$(readlink -f -- "$qemu_bin")
qemu_command=("$qemu_bin")

if ((smoke)); then
    command -v timeout >/dev/null 2>&1 || { printf '%s\n' 'blocked: timeout is required for bounded smoke' >&2; exit 1; }
    printf 'QEMU version: '; "${qemu_command[@]}" --version | sed -n '1p'
    "${qemu_command[@]}" -machine help >/dev/null
    sock_dir=$(mktemp -d "/tmp/dm-qemu.qemu-smoke.XXXXXX")
    cleanup_smoke() {
        rm -f -- "$sock_dir/qmp.sock"
        rmdir -- "$sock_dir"
    }
    trap cleanup_smoke EXIT
    set +e
    smoke_command=("${qemu_command[@]}" -machine none -nodefaults -display none -monitor none -serial none -S
        -qmp "unix:$sock_dir/qmp.sock,server=on,wait=off")
    timeout 2s "${smoke_command[@]}" >/dev/null 2>&1
    result=$?
    set -e
    if [[ $result -ne 124 ]]; then
        printf 'RESULT: smoke failed (exit=%d)\n' "$result" >&2
        exit 1
    fi
    printf '%s\n' 'RESULT: QEMU system smoke passed (-machine none)'
    exit 0
fi

case "$machine" in
    dm-mc02|dm-mc02,*) ;;
    *) printf 'error: firmware launcher requires --machine dm-mc02 (got %s)\n' "$machine" >&2; exit 2 ;;
esac
if [[ ! "$gdb_port" =~ ^[0-9]{1,5}$ ]] || ((10#$gdb_port < 1 || 10#$gdb_port > 65535)); then
    printf 'error: --gdb-port must be an integer in 1..65535 (got %s)\n' "$gdb_port" >&2
    exit 2
fi
gdb_port=$((10#$gdb_port))
if ((gdb_port_set && !debug)); then
    printf '%s\n' 'error: --gdb-port requires --debug' >&2
    exit 2
fi
uart_names=(USART1 USART2 USART3 UART5 UART7 USART10)
case "$uart" in
    USART1|USART2|USART3|UART5|UART7|USART10|NONE) ;;
    *) printf 'error: unknown UART %s; use USART1, USART2, USART3, UART5, UART7, USART10 or none\n' "$uart" >&2; exit 2 ;;
esac
if [[ -z "$elf" ]]; then
    printf '%s\n' 'error: specify --elf PATH or set DM_MC02_ELF' >&2
    exit 2
fi
[[ -f "$elf" && -r "$elf" ]] || { printf 'error: firmware ELF is not readable: %s\n' "$elf" >&2; exit 1; }
elf=$(readlink -f -- "$elf")
machine_help=$("${qemu_command[@]}" -machine help) || { printf 'error: cannot query QEMU machine support: %s\n' "$qemu_bin" >&2; exit 1; }
if [[ ! "$machine_help" =~ (^|$'\n')dm-mc02[[:space:]] ]]; then
    printf 'error: %s does not provide the dm-mc02 machine; build the project QEMU or use --qemu\n' "$qemu_bin" >&2
    exit 1
fi

qemu_command+=(-machine "$machine" -kernel "$elf" -nodefaults -display none -monitor none)
if [[ -n "$cosim_path" ]]; then
    qemu_command+=(-chardev "socket,id=cosim,path=${cosim_path//,/,,},server=on,wait=off" -serial chardev:cosim)
else
    qemu_command+=(-serial none)
fi
for uart_name in "${uart_names[@]}"; do
    if [[ "$uart" == "$uart_name" ]]; then
        qemu_command+=(-serial mon:stdio)
    else
        qemu_command+=(-serial none)
    fi
done
if [[ "$uart" == NONE ]]; then
    qemu_command+=(-monitor stdio)
fi
if [[ -n "$qmp_path" ]]; then
    qemu_command+=(-qmp "unix:${qmp_path//,/,,},server=on,wait=off")
fi
if ((debug)); then
    qemu_command+=(-S -gdb "tcp:127.0.0.1:$gdb_port")
fi
printf 'QEMU: %s\nELF: %s\nMachine: %s\nUART: %s\n' "$qemu_bin" "$elf" "$machine" "$uart" >&2
[[ -z "$qmp_path" ]] || printf 'QMP socket: %s\n' "$qmp_path" >&2
[[ -z "$cosim_path" ]] || printf 'Co-sim socket: %s (external worker required)\n' "$cosim_path" >&2
if ((debug)); then
    printf 'Paused at reset. In GDB: target remote 127.0.0.1:%s\n' "$gdb_port" >&2
fi
exec "${qemu_command[@]}"
