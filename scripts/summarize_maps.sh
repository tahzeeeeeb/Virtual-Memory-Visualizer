#!/bin/bash
# summarize_maps.sh <pid> - an independent cross-check of bin/vmv that uses only
# shell tools: it reads /proc/<pid>/maps itself and adds up the region sizes.
set -euo pipefail   # [Lab 3]
cd "$(dirname "$0")/.." || exit 1   # repo root

usage() {
    echo "Usage: $0 <pid>"
}

main() {
    local pid=${1:?Usage: $0 <pid>}   # [Lab 3] ${var:?msg} stops with msg if $1 is missing
    local -a lines=()
    local -A bytes=() count=()        # [Lab 4] associative arrays: label -> bytes / regions
    local line range path label size rc
    local total=0 n=0 vm_kb diff label_key

    case "$pid" in
        ''|*[!0-9]*) usage >&2; exit 1 ;;   # [Lab 4] PID must be digits only
    esac

    # [Lab 3] file test: no /proc/<pid> directory = no such process
    if [ ! -d "/proc/$pid" ]; then
        echo "summarize_maps: no such process: $pid" >&2
        exit 2
    fi
    rc=0
    # [Lab 4] mapfile: one array element per line of the file
    { mapfile -t lines < "/proc/$pid/maps"; } 2>/dev/null || rc=$?
    if [ "$rc" -ne 0 ]; then
        if [ -d "/proc/$pid" ]; then
            echo "summarize_maps: permission denied reading /proc/$pid/maps (another user's process - try sudo)" >&2
            exit 3
        fi
        echo "summarize_maps: no such process: $pid" >&2
        exit 2
    fi

    for line in "${lines[@]}"; do
        # split "start-end perms offset dev inode path"; path gets the rest (may be
        # empty). perms, offset, dev and inode are not needed here, so they go to _.
        read -r range _ _ _ _ path <<< "$line"
        # [Lab 3] arithmetic with base-16 numbers; awk is not used because mawk
        # (Ubuntu's default awk) cannot read hex
        size=$(( 16#${range#*-} - 16#${range%-*} ))
        if [ -z "$path" ]; then
            label="[anon]"
        elif [ "${path:0:1}" = "/" ]; then
            label="${path##*/}"   # file name only: /usr/lib/libc.so.6 -> libc.so.6
        else
            label="$path"         # [heap], [stack], [vdso] ...
        fi
        bytes["$label"]=$(( ${bytes["$label"]:-0} + size ))
        count["$label"]=$(( ${count["$label"]:-0} + 1 ))
        total=$(( total + size ))
        n=$(( n + 1 ))
    done

    echo "Regions: $n"
    echo
    echo "Top 15 labels by size:"
    # [Lab 1] print one line per label, then sort numerically, biggest first
    for label_key in "${!bytes[@]}"; do
        printf '%10d KiB  %4d regions  %s\n' \
            "$(( ${bytes["$label_key"]:-0} / 1024 ))" "${count["$label_key"]}" "$label_key"
    done | sort -n -r | head -n 15

    echo
    echo "Regions per permission set:"
    # [Lab 1] field 2 of every line is the perms column
    cut -d' ' -f2 "/proc/$pid/maps" | sort | uniq -c | sort -n -r

    echo
    echo "Sum of regions: $(( total / 1024 )) kB"
    rc=0
    vm_kb=$(grep VmSize "/proc/$pid/status" | awk '{ print $2 }') || rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$vm_kb" ]; then
        echo "VmSize: n/a (kernel thread or zombie)"
        return 0
    fi
    grep VmSize "/proc/$pid/status"
    diff=$(( total / 1024 - vm_kb ))
    echo "Difference: $diff kB"
    if [ -n "${bytes["[vsyscall]"]:-}" ]; then
        echo "([vsyscall] is listed in maps but is not a real VMA: expected difference 4 kB)"
    else
        echo "(no [vsyscall] region: expected difference 0 kB)"
    fi
}

main "$@"
