#!/bin/bash
# vmv.sh - friendly launcher for bin/vmv (pick a process by PID or by name).
set -euo pipefail   # [Lab 3] stop on errors, unset variables and failed pipe stages
cd "$(dirname "$0")" || exit 1   # run from the repo root, wherever we were called from

usage() {
    cat <<'EOF'
Usage: ./vmv.sh [-p PID | -n NAME] [-l | -d | -s | -C] [-i SEC] [-c COUNT] [-r N] [-o FILE]
  (no mode)  one snapshot of the process
  -l         live view of the process
  -d         live demo workload (fork + pipe + pthreads + zombie), no PID needed
  -s         system-wide OpenMP scan of all PIDs, no PID needed
  -C         region list as CSV
  -p PID     process ID
  -n NAME    your newest process with that name (pgrep -n)
  -i SEC     live/demo: seconds between samples (default 1)
  -c COUNT   live/demo: number of samples (default 0 = until the process ends)
  -r N       scan: how many times to repeat the scan (default 20)
  -o FILE    write the output to FILE instead of the terminal
  -h         show this help
Examples: ./vmv.sh -p $$      ./vmv.sh -n firefox -l      ./vmv.sh -s -r 50
EOF
}

# [Lab 4] case pattern: empty, or containing any non-digit -> not a number
is_number() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    return 0
}

main() {
    local OPTIND=1 OPTARG="" opt
    local pid="" name="" mode="snapshot" nmodes=0
    local interval=1 count=0 repeat=20 outfile="" rc
    local -a args=()   # [Lab 4] indexed array: the bin/vmv command line

    # [Lab 4] getopts: a letter followed by ':' takes a value in $OPTARG
    while getopts "p:n:i:c:o:r:ldsCh" opt; do
        case "$opt" in
            p) pid="$OPTARG" ;;
            n) name="$OPTARG" ;;
            l) mode="live";  nmodes=$((nmodes + 1)) ;;
            d) mode="demo";  nmodes=$((nmodes + 1)) ;;
            s) mode="scan";  nmodes=$((nmodes + 1)) ;;
            C) mode="csv";   nmodes=$((nmodes + 1)) ;;
            i) interval="$OPTARG" ;;
            c) count="$OPTARG" ;;
            o) outfile="$OPTARG" ;;
            r) repeat="$OPTARG" ;;
            h) usage; exit 0 ;;
            *) usage >&2; exit 1 ;;
        esac
    done
    shift $((OPTIND - 1))

    # ---- validate the options ----
    if [ "$#" -ne 0 ] || [ "$nmodes" -gt 1 ]; then
        usage >&2
        exit 1
    fi
    if [ -n "$pid" ] && [ -n "$name" ]; then
        echo "vmv.sh: use -p or -n, not both" >&2
        exit 1
    fi
    if [ -n "$pid" ] && ! is_number "$pid"; then
        echo "vmv.sh: PID must be a number: $pid" >&2
        exit 1
    fi
    if ! is_number "$interval" || [ "$interval" -lt 1 ]; then
        echo "vmv.sh: -i needs a whole number of seconds >= 1" >&2
        exit 1
    fi
    if ! is_number "$count"; then
        echo "vmv.sh: -c needs a number" >&2
        exit 1
    fi
    if ! is_number "$repeat" || [ "$repeat" -lt 1 ]; then
        echo "vmv.sh: -r needs a number >= 1" >&2
        exit 1
    fi

    # [Lab 3] file test: -x = exists and is executable
    if [ ! -x bin/vmv ]; then
        echo "bin/vmv not built yet - running ./build.sh"
        ./build.sh
    fi

    # ---- -n NAME: find the newest process of this user with that name ----
    if [ -n "$name" ]; then
        rc=0
        pid=$(pgrep -n -u "$USER" -- "$name") || rc=$?   # [Lab 3] keep the exit code
        if [ "$rc" -ne 0 ]; then
            echo "vmv.sh: no process of yours is named '$name'" >&2
            exit 2
        fi
        echo "Found '$name': PID $pid"
    fi

    # ---- snapshot, live and csv need a PID that exists ----
    case "$mode" in
        snapshot|live|csv)
            if [ -z "$pid" ]; then
                echo "vmv.sh: $mode needs a process: use -p PID or -n NAME" >&2
                usage >&2
                exit 1
            fi
            # [Lab 3] file test: -d = directory exists; every process has /proc/<pid>/
            if [ ! -d "/proc/$pid" ]; then
                echo "vmv.sh: no such process: $pid" >&2
                exit 2
            fi
            ;;
        *)
            if [ -n "$pid" ]; then
                echo "vmv.sh: $mode does not take a PID" >&2
                exit 1
            fi
            ;;
    esac

    # ---- build the bin/vmv command line ----
    case "$mode" in
        snapshot) args=(snapshot "$pid") ;;
        live)     args=(live "$pid" -i "$interval" -n "$count") ;;
        demo)     args=(demo -i "$interval" -n "$count") ;;
        csv)      args=(csv "$pid") ;;
        scan)     args=(scan --repeat "$repeat") ;;
    esac
    if [ -n "$outfile" ]; then
        case "$mode" in
            snapshot|live|demo) args+=(-o "$outfile") ;;
            # csv and scan have no -o: redirect this script's stdout instead
            *) exec > "$outfile" ;;
        esac
    fi

    # ---- run it ----
    rc=0
    if [ "$mode" = "scan" ]; then
        # [Lab 1] pipeline: list /proc, keep only the numeric names (PIDs), feed our program
        ls /proc | grep '^[0-9][0-9]*$' | ./bin/vmv "${args[@]}" || rc=$?
    else
        ./bin/vmv "${args[@]}" || rc=$?
    fi

    if [ "$rc" -eq 3 ]; then
        echo "Hint: ls -l /proc/$pid/maps shows r--r--r--, but procfs also checks that you may ptrace-read the process when the file is opened. Other users' processes need sudo." >&2
    fi
    exit "$rc"   # [Lab 3] pass on the program's exit code
}

main "$@"
