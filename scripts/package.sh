#!/bin/bash
# package.sh - build the submission archive and verify it against the originals.
set -euo pipefail   # [Lab 3]
cd "$(dirname "$0")/.." || exit 1   # repo root

usage() {
    echo "Usage: scripts/package.sh"
    echo "Creates vmv_submission_YYYYMMDD.tar.gz in the repo root (never build/ or bin/)."
}

main() {
    local archive tmp item rc
    local -a items=(CLAUDE.md build.sh vmv.sh menu.sh src scripts)   # [Lab 4] array

    if [ "$#" -ne 0 ]; then
        usage >&2
        exit 1
    fi

    # [Lab 3] file test: add the optional parts only if they exist
    for item in README.md docs tools; do
        if [ -e "$item" ]; then
            items+=("$item")
        fi
    done

    # [Lab 1] command substitution: today's date becomes part of the file name
    archive="vmv_submission_$(date +%Y%m%d).tar.gz"
    tar -czf "$archive" "${items[@]}"   # [Lab 1] c = create, z = gzip, f = file name
    echo "Created $archive with: ${items[*]}"

    # ---- extract into a temporary directory and compare with the originals ----
    tmp="/tmp/vmv_pkg_check_$$"   # [Lab 3] $$ = this script's PID: a unique name
    mkdir -p "$tmp"
    tar -xzf "$archive" -C "$tmp"   # [Lab 1] x = extract, -C = into this directory

    rc=0
    for item in "${items[@]}"; do
        diff -r "$item" "$tmp/$item" || rc=1   # [Lab 7] recursive compare of two trees
    done
    if [ "$rc" -eq 0 ]; then
        echo "Archive verified: contents identical"
    else
        echo "Archive differs from the originals (see diff above)"
    fi
    rm -rf "$tmp"

    # [Lab 1] tar -t lists the entries (files and directories), wc -l counts them
    echo "Entries in archive: $(tar -tzf "$archive" | wc -l)"
    exit "$rc"
}

main "$@"
