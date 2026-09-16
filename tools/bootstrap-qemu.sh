#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
repo_url=https://gitlab.com/qemu-project/qemu.git
ref=''
download=0

usage() {
    printf 'usage: %s [--download --ref <tag-or-commit>] [--repo <url>]\n' "$0"
}
while (($#)); do
    case "$1" in
        --download) download=1; shift ;;
        --ref) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; ref=$2; shift 2 ;;
        --repo) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; repo_url=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

source_dir="$root_dir/qemu/upstream"
lock_file="$root_dir/qemu.lock"
if ((download == 0)); then
    printf '%s\n' 'QEMU bootstrap probe only: no network or filesystem download performed.'
    [[ -d "$source_dir/.git" ]] && git -C "$source_dir" rev-parse --short HEAD || printf '%s\n' 'source: missing'
    printf '%s\n' 'To download explicitly: --download --ref <fixed tag or commit>'
    exit 0
fi
if [[ -z "$ref" ]]; then
    printf '%s\n' '--ref is required with --download; do not use a floating branch.' >&2
    exit 2
fi
if [[ -e "$source_dir" ]]; then
    if [[ ! -d "$source_dir/.git" ]]; then
        printf 'refusing to use non-Git existing path: %s\n' "$source_dir" >&2
        exit 1
    fi
    printf 'source already exists; no overwrite: %s\n' "$source_dir"
else
    mkdir -p "$root_dir/qemu"
    git clone --filter=blob:none --no-checkout "$repo_url" "$source_dir"
fi
if ! git -C "$source_dir" rev-parse --verify HEAD >/dev/null 2>&1; then
    git -C "$source_dir" fetch --depth 1 origin "$ref"
fi
checkout_target="$ref"
if ! git -C "$source_dir" rev-parse --verify "$ref^{commit}" >/dev/null 2>&1; then
    checkout_target=FETCH_HEAD
fi
git -C "$source_dir" checkout --detach "$checkout_target"
commit=$(git -C "$source_dir" rev-parse HEAD)
tmp_lock=$(mktemp "$root_dir/qemu.lock.tmp.XXXXXX")
trap 'rm -f -- "$tmp_lock"' EXIT
sed -e "s#^status=.*#status=source-ready#" \
    -e "s#^upstream_url=.*#upstream_url=$repo_url#" \
    -e "s#^upstream_ref=.*#upstream_ref=$ref#" \
    -e "s#^upstream_commit=.*#upstream_commit=$commit#" \
    "$lock_file" > "$tmp_lock"
mv -- "$tmp_lock" "$lock_file"
trap - EXIT
printf 'QEMU source ready: %s (%s)\n' "$ref" "$commit"
