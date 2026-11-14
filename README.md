# Virtual Memory Visualizer (vmv)

vmv shows the virtual address space of a running Linux process as a colour-coded
terminal diagram. It reads what the kernel already publishes in `/proc/<pid>/maps`,
`/proc/<pid>/status` and `/proc/<pid>/stat`, labels every region (code, heap, stack,
shared libraries, ...) and can follow a process live while its memory changes.

Semester project for CS251 Operating Systems Lab, Sukkur IBA, BSCS-V Section F.

## Group

- Muhammad Tahzeeb Khan (lead)
- Abdul Wali Soomro
- Sameer Sethar

## Requirements

- Linux on x86-64 (Ubuntu or Kali). Install the compiler with:
  `sudo apt install build-essential`
- Optional:
  - strace, for menu options 6 and 7: `sudo apt install strace`
  - shellcheck, for `./build.sh check`: `sudo apt install shellcheck`
  - matplotlib, for the PNG renderer: `sudo apt install python3-matplotlib`

We developed and tested it on Linux 7.3.3 (CachyOS) with gcc 16.2.1 and glibc 2.44.
Numbers in the docs come from that machine and will differ a little on others.

## Build

```
./build.sh          # compiles bin/vmv, bin/vmv_demo, bin/vmv_client
./build.sh clean    # removes build/ and bin/
./build.sh check    # runs shellcheck on the scripts (if installed)
```

## Quick start

```
./menu.sh                 # numbered menu with every demo (use this in class)
./vmv.sh -p <pid>         # snapshot of one process
./vmv.sh -n bash -l       # live view of your newest bash process (Ctrl+C to stop)
./bin/vmv demo            # forks a workload and shows it changing, then its zombie
```

## bin/vmv subcommands

- `snapshot <pid> [-o FILE] [--no-color] [--detail]` - the address space as four boxes,
  highest address at the top: STACK, MEMORY-MAPPED AREA (biggest files, thread stacks,
  anonymous mmap, kernel pages), HEAP, PROGRAM, with the unused gaps between them. Fits
  80 columns x 40 lines. `--detail` prints one row per line of /proc/<pid>/maps instead.
  Example: `./bin/vmv snapshot $$`
- `live <pid> [-i SEC] [-n COUNT] [-o FILE] [--no-color] [--detail]` - redraw every SEC
  seconds; each box title shows its change since the last sample, and up to 4 lines list
  regions that appeared (`+ new`) or disappeared (`- gone`).
  Example: `./bin/vmv live $$ -i 1 -n 3`
- `demo [-i SEC] [-n COUNT] [-o FILE] [--no-color] [--detail]` - fork the demo workload and
  watch it from start to finish, including its zombie state.
  Example: `./bin/vmv demo -i 1 -o /tmp/demo.txt`
- `scan [--repeat N]` - read the status of every PID given on stdin in parallel (OpenMP)
  and print the top 10 by memory use.
  Example: `ls /proc | grep '^[0-9][0-9]*$' | ./bin/vmv scan --repeat 20`
- `csv <pid>` - the region list as CSV. The first line is `# pid=...;name=...;state=...;phase=...`
  with the header numbers; readers skip lines that start with `#`.
  Example: `./bin/vmv csv $$ > /tmp/layout.csv`
- `serve` - Unix socket server on `/tmp/vmv.sock` that answers "summary of PID X".
  Example: `./bin/vmv serve &` then `./bin/vmv_client $$`

`-o FILE` writes to a file and turns colour off. Other programs:

- `./bin/vmv_demo [PHASE_SEC] [START_DELAY]` - the demo workload on its own, to watch
  from a second terminal with `./bin/vmv live <pid>`. Example: `./bin/vmv_demo 5 10`
- `./bin/vmv_client <pid>` - asks a running `vmv serve`. Example: `./bin/vmv_client $$`
- `scripts/summarize_maps.sh <pid>` - a shell-only cross-check of the region sizes.
- `scripts/package.sh` - makes `vmv_submission_YYYYMMDD.tar.gz` and checks it.

## GUI window (optional)

`tools/vmv_gui.py` opens a matplotlib window that redraws every second: the address space
as four stacked blocks (STACK, MEMORY-MAPPED AREA, HEAP, PROGRAM), a panel with the
process numbers and phase, and a chart of VmSize and VmRSS. Python is only the display:
every number comes from `./bin/vmv csv <pid>`.

```
python3 tools/vmv_gui.py <pid>                    # watch any of your processes
python3 tools/vmv_gui.py --demo                   # run bin/vmv_demo and watch it to the end
python3 tools/vmv_gui.py $$ --png /tmp/gui.png    # one frame, no window
python3 tools/vmv_gui.py --demo --frames /tmp/f   # no window, one PNG per sample
```

The window needs a Qt backend for matplotlib: `sudo pacman -S python-pyqt6`
(Ubuntu: `sudo apt install python3-pyqt5`). Without it the script says so and exits;
`--png` and `--frames` work without any GUI backend. Menu option 11 runs `--demo`.

## Exit codes

- `0` - success
- `1` - usage error (wrong arguments)
- `2` - no such process
- `3` - permission denied (the process belongs to another user; try `sudo`)

`vmv_client` uses the same numbers: 0 for a `PID ...` reply, 2 if it cannot connect,
3 for an `ERROR ...` reply.

## Project layout

- `CLAUDE.md` - course constraints and coding rules for the project
- `README.md` - this file
- `build.sh` - compiles each `.c` file with `gcc -c`, then links; `clean` and `check` targets
- `vmv.sh` - launcher: choose a process by PID or name, choose the mode with getopts flags
- `menu.sh` - presentation menu (bash `select`) with one entry per demo
- `scripts/summarize_maps.sh` - reads `/proc/<pid>/maps` with shell tools only, as a cross-check
- `scripts/package.sh` - builds and verifies the submission archive
- `src/procfs.h` - shared structs (`struct region`, `struct proc_info`, `struct snap_hdr`) and limits
- `src/procfs.c` - reads /proc with open/read/close and parses maps, status and stat
- `src/classify.h`, `src/classify.c` - decides the kind of each region and its colour
- `src/render.h`, `src/render.c` - draws the diagram, the live diff, and the CSV
- `src/ioutil.h`, `src/ioutil.c` - `readn`/`writen`: read or write exactly n bytes on a pipe
- `src/live.h`, `src/live.c` - live and demo modes: fork, pipe, waitpid, zombie view
- `src/workload.h`, `src/workload.c` - the demo workload (malloc, page touching, pthreads, free)
- `src/demo_main.c` - `main` of `bin/vmv_demo`
- `src/scan.h`, `src/scan.c` - the OpenMP system scan
- `src/server.h`, `src/server.c` - the Unix socket server
- `src/client.c` - `main` of `bin/vmv_client`
- `src/main.c` - `main` of `bin/vmv`: argument parsing and the subcommands
- `tools/plot_layout.py` - optional matplotlib renderer: CSV to PNG
- `tools/vmv_gui.py` - optional live matplotlib window fed by `bin/vmv csv`
- `docs/LAB_MAPPING.md` - where each lab concept is used in the code
- `docs/DEMO.md` - the presenter script for class
- `docs/VIVA.md` - likely viva questions with answers

## Course constraint

Built only with concepts from CS251 Labs 0–10 - see `docs/LAB_MAPPING.md`.
