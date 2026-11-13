# Lab mapping

Where each CS251 lab concept is used. Every item below has a `[Lab N]` comment in the
code, so `grep -rn '\[Lab 6\]' src/ *.sh scripts/` lists the exact lines for Lab 6.
Format: concept → where → how to show it in class.

## Lab 0 - C basics: structs, pointers, malloc/free, printf/sscanf formats

- Structs that model the data → `src/procfs.h` (`struct region`, `struct proc_info`, `struct snap_hdr`) → put `struct region` next to one line of `/proc/$$/maps` and match the fields.
- Read-only lookup tables indexed by an enum → `src/classify.c` (the `names`, `colors`, `meanings` arrays used by `kind_name`, `kind_color`, `kind_meaning`) → show how one `kind` number picks the label, colour and legend text.
- Pointer to the next free struct, and `sscanf` with `%lx` (hex) and `%n` (characters consumed) → `src/procfs.c:parse_maps` → read the format string aloud while pointing at a raw maps line.
- `memset` to start from a clean struct → `src/procfs.c:read_proc_info` → point out that every field has a known value before parsing.
- `malloc`/`free` of the 1 MiB maps buffer → `src/procfs.c:take_snapshot` → explain why 1 MiB does not belong on the stack.
- `malloc` of the region arrays → `src/main.c:cmd_snapshot`, `src/live.c:sampler_loop`, `src/live.c:show_stream`, `src/server.c:run_server` → mention that MAX_REGIONS structs are about 1 MiB.
- `struct scan_result` and the `malloc`'d PID/result arrays → `src/scan.c` (`struct scan_result`, `run_scan`) → one result slot per PID.
- `malloc` that the audience watches: 20 small blocks, one 128 MiB block, then `free` → `src/workload.c:run_workload` → these three lines cause the heap growth, the new region, and its removal in the live demo.
- printf width and precision for aligned columns → `src/render.c:format_size`, `src/render.c:format_count`, `src/render.c:print_region`, `src/scan.c:run_scan` → show `%016lx` and `%-14s` and the straight columns they produce.

## Lab 1 - Linux commands, pipes, command substitution, tar

- A shell pipeline feeds our C program → `src/scan.c` (file header comment) and `vmv.sh` (scan mode in `main`) → run `ls /proc | grep '^[0-9][0-9]*$' | ./bin/vmv scan`.
- `ps | head` to list processes → `menu.sh:demo_snapshot_pick` → menu option 3.
- `nproc`, `grep` + `cut` to pull out `SCAN_TIME_SEC`, `awk` for the division → `menu.sh:demo_scan` → menu option 8 prints the speed-up line.
- `printf | sort -n -r | head`, and `cut | sort | uniq -c | sort` → `scripts/summarize_maps.sh` (`main`) → menu option 4: the top-15 list and the permission counts.
- Command substitution with `date`, `tar -czf/-xzf/-tzf`, `wc -l` → `scripts/package.sh` (`main`) → run `scripts/package.sh` and show the archive name and entry count.

## Lab 2 - file-system hierarchy, permissions, gcc compile and link

- `/proc` is a virtual file system: files of size 0 that the kernel fills when read → `src/procfs.c` (file header comment) → `ls -l /proc/$$/maps` shows size 0, `wc -c < /proc/$$/maps` shows thousands of bytes.
- `gcc -c` per file, then one link step, then `chmod 755` → `build.sh` (`all` target) → run `./build.sh` and read the printed gcc lines.
- Permission bits `r--r--r--` → `menu.sh:demo_permission` → menu option 5 shows `ls -l /proc/1/maps`.
- The `s` file type of a socket in `ls -l` → `menu.sh:demo_socket` → menu option 9 shows `srwxr-xr-x ... /tmp/vmv.sock`.

## Lab 3 - bash scripting basics

- `set -euo pipefail` → first line of `build.sh`, `vmv.sh`, `menu.sh`, `scripts/summarize_maps.sh`, `scripts/package.sh` → explain that a failing command stops the script.
- File tests `-x`, `-d`, `-r`, `-e` → `vmv.sh` (`main`: builds if `bin/vmv` is missing, checks `/proc/<pid>`), `menu.sh:main`, `menu.sh:demo_permission`, `scripts/summarize_maps.sh` (`main`), `scripts/package.sh` (`main`) → run `./vmv.sh -p 999999` (exit 2).
- Keeping an exit code under `set -e` with `rc=0; cmd || rc=$?` → `vmv.sh` (`main`), `build.sh` (`check` target) → run `./vmv.sh -p 1; echo $?` (prints 3).
- `$$` (PID of this shell) → `menu.sh:demo_snapshot_self`, `scripts/package.sh` (`main`) → menu option 2 prints "This menu is bash process <pid>".
- `$EUID` → `menu.sh:demo_permission` → explain the root check.
- Arithmetic `$(( ))`, including base-16 → `menu.sh:demo_strace_memory`, `scripts/summarize_maps.sh` (`main`) → show `$(( 16#${range#*-} - 16#${range%-*} ))`.
- `${1:?msg}` → `scripts/summarize_maps.sh` (`main`) → run it with no argument.

## Lab 4 - getopts, case, functions with local, arrays, select, mapfile

- `getopts` → `vmv.sh` (`main`) → run `./vmv.sh -h`, then `./vmv.sh -l -s` (two modes, exit 1).
- `case` patterns → `vmv.sh:is_number`, `build.sh` (case on `$1`), `scripts/summarize_maps.sh` (`main`) → show `''|*[!0-9]*) return 1`.
- Indexed arrays → `build.sh` (`CFLAGS`, `VMV_SRCS`), `vmv.sh` (`main`: `args`), `menu.sh:main` (`options`), `scripts/package.sh` (`main`: `items`) → show `VMV_SRCS` growing over the Parts.
- Associative arrays → `scripts/summarize_maps.sh` (`main`: `bytes`, `count`) → label → total bytes.
- `mapfile` → `scripts/summarize_maps.sh` (`main`) → one array element per maps line.
- `select` with `case "$REPLY"` → `menu.sh:main` → the menu itself.

## Lab 5 - system calls: open, read, write, close, dup2, unlink

- `open`/`read` loop until 0/`close` → `src/procfs.c:read_whole_file` → menu option 7 (strace) shows the calls.
- `open`/`write`/`close` of the phase file, `unlink` at the end → `src/workload.c:set_phase`, `src/workload.c:run_workload`, `src/live.c:run_demo` → `cat /tmp/vmv_phase_<pid>.txt` while `bin/vmv_demo` runs.
- `read`/`write` can move fewer bytes than asked → `src/ioutil.c:readn`, `src/ioutil.c:writen` → explain the loop.
- `read` of the request from a socket → `src/server.c:run_server` → menu option 9.
- `open` + `dup2(fd, 1)` + `close` = what the shell does for `>` → `src/main.c:redirect_stdout` → run `./bin/vmv snapshot $$ -o /tmp/s.txt` and `grep -c $'\033' /tmp/s.txt` (0).
- Watching the system calls with strace → `menu.sh:demo_strace_proc` → menu option 7.

## Lab 6 - processes: fork, getpid, sleep, waitpid, WNOHANG, kill, zombies, background jobs

- `getpid` → `src/workload.c:set_phase`, `src/demo_main.c:main`, `src/live.c:show_stream` → the "Processes:" line under each live sample.
- `fflush(stdout)` before `fork`, then `fork` → `src/live.c:start_sampler`, `src/live.c:run_demo` → explain that fork copies unflushed buffers.
- `sleep` between samples → `src/live.c:sampler_loop` → the `-i SEC` option.
- `waitpid` to reap → `src/live.c:run_live`, `src/live.c:run_demo` → no zombies left after a run.
- `waitpid(..., WNOHANG)`, `kill(SIGTERM)`, `WIFEXITED`/`WEXITSTATUS`, `WIFSIGNALED`/`WTERMSIG` → `src/live.c:run_demo` → `./bin/vmv demo -n 4` ends with "terminated by signal 15".
- Background job `&`, `$!`, `pkill`, `wait` → `menu.sh:demo_socket` → menu option 9 starts and stops the server.

## Lab 7 - process tree, comparing directory trees

- The process tree parent → two children → `src/live.c:show_stream` (the "Processes:" line) → run `ps -o pid,ppid,stat,cmd -u "$USER" | grep vmv` during the demo.
- `diff -r` of two directory trees → `scripts/package.sh` (`main`) → it prints "Archive verified: contents identical".

## Lab 8 - pipes and Unix domain sockets

- `pipe()` and closing the end each process does not use → `src/live.c:start_sampler` → explain why the parent must close the write end.
- One message = header + regions; closing the last write end = EOF → `src/live.c:sampler_loop` → the parent stops when the sampler exits.
- `readn` of header and regions → `src/live.c:show_stream` → messages are bigger than the 64 KiB pipe buffer.
- Closing the read end at the end → `src/live.c:run_live`, `src/live.c:run_demo`.
- Short reads on a pipe → `src/ioutil.c` (file header comment) → why `readn` loops.
- stdin is the read end of a shell pipe → `src/scan.c:run_scan` → the scan pipeline.
- Server: `socket(AF_UNIX, SOCK_STREAM)`, `unlink`, `bind`, `listen`, `accept`, write reply, `close` → `src/server.c:run_server` → menu option 9.
- Client: `socket`, `connect`, `write`, `read` until 0 → `src/client.c:main` → `./bin/vmv_client $$`.

## Lab 9 - pthreads

- `pthread_create` with `&ids[i]` (Fix A for the loop-variable bug) and `pthread_join` → `src/workload.c:run_workload` → phase 4 of the live demo, `ps -L` shows NLWP 5.
- Thread function reads its own slot; each thread has its own TID (`syscall(SYS_gettid)`) → `src/workload.c:worker` → `./bin/vmv_demo 1` prints 4 different TIDs.

## Lab 10 - OpenMP

- `#pragma omp parallel for schedule(dynamic) reduction(+:...)` → `src/scan.c:run_scan` → each iteration writes only `results[i]`, so no mutex.
- `omp_get_wtime` and `omp_get_max_threads` → `src/scan.c:run_scan` → menu option 8: 1 thread vs all cores.
