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

## Install and run

vmv needs Linux on x86-64 and gcc (OpenMP comes with it). strace is used by menu
options 6 and 7, shellcheck by `./build.sh check`, and matplotlib with a Qt binding by
the PNG renderer and the GUI window. The commands below install all of them. Pick your
system, run its install command once, then follow "Get the code, build, run".

### Arch Linux and Arch-based (Ryoku, Manjaro, EndeavourOS)

```
sudo pacman -S --needed base-devel git strace shellcheck python-matplotlib python-pyqt6
```

### Ubuntu 22.04 / 24.04

```
sudo apt update && sudo apt install -y build-essential git strace shellcheck python3-matplotlib python3-pyqt5
```

### Kali Rolling

```
sudo apt update && sudo apt install -y build-essential git strace shellcheck python3-matplotlib python3-pyqt6
```

### Get the code, build, run (every system)

```
git clone https://github.com/tahzeeeeeb/Virtual-Memory-Visualizer.git
cd Virtual-Memory-Visualizer
./build.sh
```

```
./menu.sh                          # numbered menu with every demo (use this in class)
./bin/vmv snapshot $$              # the address space of this shell
./bin/vmv demo                     # a workload changing live, then its zombie and reaping
python3 tools/vmv_gui.py --demo    # the same demo in a window (Q quits)
```

### Notes

- Run everything as a normal user, not as root: the permission demo on PID 1 only shows
  the "permission denied" case when you are not root. Attaching strace to a process you
  did not start (`strace -p <pid>`) needs sudo; the menu's strace demos do not.
- Install Python packages from your distro as above, not with `pip install`: Arch,
  Ubuntu 23.04 and newer, and Kali refuse pip installs into the system Python.
- GUI on Wayland: the Qt backend runs natively. The Tk backend, which matplotlib falls
  back to without Qt, needs XWayland (on niri: `xwayland-satellite`). If the GUI prints
  "Install a GUI backend", install the Qt package for your distro from above.
- In a virtual machine give it at least 2 CPU cores, or the OpenMP scan (menu option 8)
  shows no speed-up.
- Expected differences between machines: newer glibc (2.44 on Arch) shows the demo's
  thread stacks as one merged anonymous region, older glibc (Ubuntu) as separate
  THREAD-STACK? and GUARD rows; page-fault counts depend on transparent huge pages;
  `[vsyscall]` may be missing.
- GCC 15 and newer (Arch) compile as C23 by default; the code builds with zero warnings
  under GCC 16.2.1. Ubuntu ships GCC 11 (22.04) and GCC 13 (24.04), which default to
  gnu17; expected to build the same, not yet tested by us.

**Tested on:** CachyOS (Arch-based, rolling) with the Ryoku desktop on niri (Wayland),
kernel 7.3.3-1-cachyos, gcc 16.2.1, glibc 2.44, Python 3.14.9, matplotlib 3.11.2 with
PyQt6 6.12.0. The GUI was also run there with Python 3.10.22, matplotlib 3.5.1 and PyQt5
5.15.6 (the versions Ubuntu 22.04 ships) in a separate Python environment. Ubuntu 22.04 /
24.04 and Kali Rolling: expected to work, not yet tested by us. Numbers in the docs come
from the tested machine and will differ a little on others.

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

`tools/vmv_gui.py` opens a 16:9 matplotlib window (1600x900) that samples the process
once a second and animates at 30 fps. Python is only the display: every number comes
from `./bin/vmv csv <pid>`.

- Top: the title, the process name and PID, and a "● live" dot that pulses once per
  sample (gray when paused or when the process has ended).
- Banner: "Phase N of 7" with progress dots and what the demo is doing. It turns amber
  for a zombie ("Zombie — exited, waiting for its parent to reap it", blocks faded to
  25 %), green once reaped ("Reaped · exit status 0"), red if the process is gone or not
  ours.
- Left: the address space, high addresses at the top. Block heights are on a log scale,
  the same rule for every block, so a bigger region is always taller. Unused gaps are
  thin "≈ X unused" bands, and PROGRAM shows its code, rodata, data and bss side by side.
  A block that changes size gets a "+128.0 MiB" pill for 2.5 s.
- Right: Reserved (VmSize) and In RAM (VmRSS) in large type, the share in RAM, page
  faults and threads; below, a chart of both over the last 60 seconds.

```
python3 tools/vmv_gui.py <pid>                    # watch any of your processes
python3 tools/vmv_gui.py --demo                   # run bin/vmv_demo and watch it to the end
python3 tools/vmv_gui.py --demo --theme dark      # dark colours (light is the default)
python3 tools/vmv_gui.py --demo --scale 1.25      # everything 25 % larger, for a projector
python3 tools/vmv_gui.py $$ --png /tmp/gui.png    # one frame, no window
python3 tools/vmv_gui.py --demo --frames /tmp/f   # no window, one PNG per sample
python3 tools/vmv_gui.py --bench                  # time 150 animated frames, no window
```

Keys in the window:

| Key        | Action                                        |
|------------|-----------------------------------------------|
| Space      | pause / resume sampling                       |
| F          | fullscreen on / off                           |
| R          | restart the demo from phase 0 (`--demo` only) |
| Q or Esc   | quit (a running demo is stopped and reaped)   |

`--scale` takes 0.5 to 3 and multiplies every size (text, blocks, margins). 1.25 is the
largest that fits the 1600x900 window comfortably; larger sizes need a bigger screen
(press F).
`--no-anim` makes values jump instead of moving (reduced motion).

The window needs a Qt backend for matplotlib: `python-pyqt6` on Arch, `python3-pyqt5` on
Ubuntu, `python3-pyqt6` on Kali (see "Install and run"). Without it the script says so and exits;
`--png`, `--frames` and `--bench` work without any GUI backend. Menu option 11 runs
`--demo`.

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
