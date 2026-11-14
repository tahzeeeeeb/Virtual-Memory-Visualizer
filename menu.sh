#!/bin/bash
# menu.sh - the presentation driver: one numbered menu entry per demo.
set -euo pipefail   # [Lab 3]
cd "$(dirname "$0")" || exit 1   # repo root

usage() {
    echo "Usage: ./menu.sh    (interactive menu; type a number and press Enter)"
}

# Wait for Enter. At end of input (e.g. piped commands) just carry on.
pause() {
    read -r -p "Press Enter to return to the menu..." _ || true
}

# Ask for a PID; Enter alone means the default given as $1.
ask_pid() {
    local answer=""
    read -r -p "PID (Enter = $1): " answer || true
    if [ -z "$answer" ]; then
        answer="$1"
    fi
    echo "$answer"
}

# 1
demo_live() {
    local rc=0
    ./bin/vmv demo -i 1 || rc=$?
    echo "(vmv demo exited with status $rc)"
}

# 2
demo_snapshot_self() {
    local rc=0
    echo "This menu is bash process $$"   # [Lab 3] $$ = PID of this shell
    ./vmv.sh -p "$$" || rc=$?
    echo "(exit status $rc)"
}

# 3
demo_snapshot_pick() {
    local pid="" rc=0
    echo "Some of your processes:"
    ps -u "$USER" -o pid=,comm= | head -n 15 || true   # [Lab 1]
    read -r -p "PID: " pid || true
    ./vmv.sh -p "$pid" || rc=$?
    echo "(exit status $rc)"
}

# 4
demo_summary() {
    local pid rc=0
    pid=$(ask_pid "$$")
    scripts/summarize_maps.sh "$pid" || rc=$?
    echo "(exit status $rc)"
}

# 5
demo_permission() {
    local rc=0
    # [Lab 3] $EUID = effective user ID; 0 means root
    if [ "$EUID" -eq 0 ]; then
        echo "You are root, so every read succeeds. Run this demo as a normal user."
        return 0
    fi
    # [Lab 2] the permission bits from the Linux file-system lab
    ls -l /proc/1/maps
    # [Lab 3] file test -r only looks at the mode bits
    if [ -r /proc/1/maps ]; then
        echo "[ -r ] says readable: the mode bits are r--r--r--"
    fi
    echo "Now actually opening it:"
    head -c 1 /proc/1/maps > /dev/null || rc=$?   # expected: Permission denied
    echo "head exited with status $rc"
    echo "Why: the mode bits say everyone may read, but when /proc/<pid>/maps is opened the"
    echo "kernel also checks whether you could ptrace (debug) that process. PID 1 belongs to"
    echo "root, so open() fails with EACCES. This is why bin/vmv exits with code 3 here."
}

# 6
demo_strace_memory() {
    local stack_kb stack rc=0
    if ! command -v strace > /dev/null; then
        echo "strace is not installed: sudo apt install strace"
        return 0
    fi
    stack_kb=$(ulimit -s)
    if ! [ "$stack_kb" -ge 0 ] 2>/dev/null; then
        echo "ulimit -s is '$stack_kb', so the thread stack size is not known; using 8 MiB"
        stack_kb=8192
    fi
    stack=$(( stack_kb * 1024 ))   # [Lab 3] arithmetic: thread stack size in bytes
    echo "Tracing ./bin/vmv_demo 1 (takes about 10 s)..."
    strace -f -o /tmp/vmv_trace.txt -e trace=brk,mmap,munmap,mprotect,clone,clone3 \
        ./bin/vmv_demo 1 || rc=$?
    echo "(vmv_demo exited with status $rc)"
    echo
    echo "brk = heap grows | 134221824 = malloc(128 MiB) + 4 KiB header | $(( stack + 4096 )) = thread stack + guard page | clone3 = new thread"
    grep -E "brk\(|134221824|$(( stack + 4096 ))|clone3?\(" /tmp/vmv_trace.txt | head -n 40 || true
}

# 7
demo_strace_proc() {
    if ! command -v strace > /dev/null; then
        echo "strace is not installed: sudo apt install strace"
        return 0
    fi
    # [Lab 5] every /proc file is read with plain open/read/close system calls
    strace -e trace=openat,read,close ./bin/vmv snapshot "$$" -o /dev/null 2>&1 \
        | grep '/proc/' | head -n 20 || true
}

# 8
demo_scan() {
    local out1 outn t1 tn cores
    cores=$(nproc)   # [Lab 1] number of CPU cores
    echo "=== 1 thread ==="
    out1=$(OMP_NUM_THREADS=1 ./vmv.sh -s -r 50) || true
    echo "$out1"
    echo
    echo "=== $cores threads ==="
    outn=$(OMP_NUM_THREADS="$cores" ./vmv.sh -s -r 50) || true
    echo "$outn"
    echo
    # [Lab 1] pull the machine-readable line out with grep + cut
    t1=$(echo "$out1" | grep '^SCAN_TIME_SEC=' | cut -d= -f2) || true
    tn=$(echo "$outn" | grep '^SCAN_TIME_SEC=' | cut -d= -f2) || true
    if [ -z "$t1" ] || [ -z "$tn" ]; then
        echo "Could not read SCAN_TIME_SEC from the scan output."
        return 0
    fi
    echo "1 thread: $t1 s   $cores threads: $tn s"
    # [Lab 1] awk does the floating-point division that bash arithmetic cannot
    echo "Speed-up on $cores cores: $(awk -v a="$t1" -v b="$tn" 'BEGIN { if (b > 0) printf "%.2f", a / b; else printf "n/a" }')x"
}

# 9
demo_socket() {
    local pid server
    rm -f /tmp/vmv_server.log
    ./bin/vmv serve > /tmp/vmv_server.log 2>&1 &   # [Lab 6] run the server in the background
    server=$!                                    # [Lab 6] $! = PID of that background job
    sleep 1
    ls -l /tmp/vmv.sock || true   # [Lab 2] the first letter 's' = socket file type
    pid=$(ask_pid "$$")
    echo "--- client asks about PID $pid"
    ./bin/vmv_client "$pid" || echo "(client exit status $?)"
    echo "--- client asks about PID 999999"
    ./bin/vmv_client 999999 || echo "(client exit status $?)"
    pkill -f 'bin/vmv serve' || true   # [Lab 6] stop the server (SIGTERM)
    wait "$server" 2>/dev/null || true   # [Lab 6] reap it so no zombie is left
    echo "--- server log:"
    cat /tmp/vmv_server.log
}

# 10
demo_csv() {
    ./bin/vmv csv "$$" > /tmp/vmv_layout.csv
    echo "Wrote /tmp/vmv_layout.csv ($(wc -l < /tmp/vmv_layout.csv) lines). First lines:"
    head -n 5 /tmp/vmv_layout.csv
    if python3 -c 'import matplotlib' 2>/dev/null && [ -f tools/plot_layout.py ]; then
        python3 tools/plot_layout.py /tmp/vmv_layout.csv -o vmv_layout.png
    else
        echo "Optional: sudo apt install python3-matplotlib (tools/plot_layout.py is added in Part 5)"
    fi
}

# 11
demo_gui() {
    local rc=0
    # the window gets every number from ./bin/vmv csv; it closes the demo when you close it
    python3 tools/vmv_gui.py --demo || rc=$?
    echo "(vmv_gui exited with status $rc)"
}

main() {
    local choice   # select stores the chosen text here; we switch on $REPLY (the number)
    local -a options=(   # [Lab 4] indexed array of menu entries
        "Live demo: fork + pipe + pthreads + zombie"
        "Snapshot: this menu's own bash process"
        "Snapshot: pick one of my processes"
        "Raw maps summary (shell cross-check)"
        "Permission check on PID 1 (Lab 2 vs procfs)"
        "strace: malloc -> brk/mmap, pthread_create -> mmap + mprotect + clone3"
        "strace: vmv reads /proc with open/read/close"
        "OpenMP scan: 1 thread vs all cores"
        "Unix socket: server + client"
        "CSV + matplotlib PNG (optional)"
        "GUI live demo (matplotlib window)"
        "Quit"
    )

    if [ "$#" -ne 0 ]; then
        usage >&2
        exit 1
    fi
    # [Lab 3] file tests: build first if any binary is missing
    if [ ! -x bin/vmv ] || [ ! -x bin/vmv_demo ] || [ ! -x bin/vmv_client ]; then
        ./build.sh
    fi

    echo "Virtual Memory Visualizer - CS251 Operating Systems Lab"
    PS3="Choose a demo: "
    # [Lab 4] select prints the numbered list and reads a number into $REPLY
    # shellcheck disable=SC2034  # [Lab 4] select needs a variable name; the case branches on $REPLY
    select choice in "${options[@]}"; do
        # Each demo runs in an `||` context: a failure prints a message and the menu
        # carries on instead of set -e ending the whole script.
        case "$REPLY" in   # [Lab 4]
            1)  demo_live            || echo "Demo failed." ;;
            2)  demo_snapshot_self   || echo "Demo failed." ;;
            3)  demo_snapshot_pick   || echo "Demo failed." ;;
            4)  demo_summary         || echo "Demo failed." ;;
            5)  demo_permission      || echo "Demo failed." ;;
            6)  demo_strace_memory   || echo "Demo failed." ;;
            7)  demo_strace_proc     || echo "Demo failed." ;;
            8)  demo_scan            || echo "Demo failed." ;;
            9)  demo_socket          || echo "Demo failed." ;;
            10) demo_csv             || echo "Demo failed." ;;
            11) demo_gui             || echo "Demo failed." ;;
            12) echo "Bye."; break ;;
            *)  echo "Please type a number from 1 to ${#options[@]}."; continue ;;
        esac
        pause
    done
    exit 0
}

main "$@"
