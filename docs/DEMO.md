# Presenter script (8-10 minutes, three speakers)

The speaker split below is a suggestion; edit it as you like. Almost everything runs
from `./menu.sh`; the option number is given for each step.

## Before class (5 minutes, not part of the talk)

1. `./build.sh` - it must end with `Build OK: bin/vmv` and print no warnings.
2. `sudo apt install strace` if `command -v strace` prints nothing (needed for options 6 and 7).
3. Optional: `sudo apt install python3-matplotlib` for option 10.
4. Make the terminal at least 150 columns wide: region lines are about 111 characters
   plus the path, up to 145 in total.
5. Open a second terminal in the same folder for the `ps` commands.
6. Run options 6 and 7 once and compare with "What we measured" below - the trace
   output depends on the glibc version of the machine.
7. Start `./menu.sh` in the first terminal.

## Part 1 - Muhammad Tahzeeb Khan: intro and how to read the output (about 3 min, Labs 0, 1, 2, 5)

**Intro (30 s).** "Every process thinks it has a huge private memory. Linux already
describes that memory in text files under /proc. Our tool reads those files with plain
system calls and draws the address space, live."

**Architecture (45 s).** Show this diagram (it is what `./bin/vmv demo` does):

```
                 +------------------------------------+
                 | vmv parent (PID P)                 |
                 | readn() from pipe, render_snapshot |
                 | waitpid(T) at the end              |
                 +------------------------------------+
                     | fork() #1           | fork() #2         ^
                     v                     v                   |
  +-------------------------+   +--------------------------+   |
  | target child (PID T)    |   | sampler child (PID S)    |   | pipe:
  | run_workload():         |   | every -i seconds:        |---+ header +
  | malloc, touch pages,    |   | take_snapshot(T) reads   |     regions
  | 4 pthreads, free, exit  |<..| /proc/T/maps, status,    |
  | -> zombie until reaped  |   | stat (open/read/close)   |
  +-------------------------+   +--------------------------+
```

Say: the target is forked before the pipe exists, so only the sampler holds the write end.

**Snapshot of the menu's own shell (45 s) - option 2.** Point at:
- the four boxes: STACK at the top, then MEMORY-MAPPED AREA, HEAP, and PROGRAM at the
  bottom (like the textbook picture); each box shows its size and its address range,
- the `⋮ ... unused` lines between the boxes: most of the address space is empty,
- inside MEMORY-MAPPED AREA: the biggest libraries with their code size, then anonymous
  mmap and the kernel pages ([vdso] [vvar]),
- the footer: the number of regions; `./vmv.sh -p <pid> -D` lists every one of them.

**How to read one maps line (30 s).** In the second terminal: `grep libc /proc/$$/maps | grep r-xp`.
On our VM it printed:

```
7ff1a5424000-7ff1a55d3000 r-xp 00024000 00:1f 449220                     /usr/lib/libc.so.6
```

Six columns: address range, permissions, offset in the file, device, inode, path.
`r-xp` = readable, not writable, executable, private. With `--detail` vmv draws the same
line as:

```
  00007ff1a5424000-00007ff1a55d3000 r-xp    1.7 MiB      431 pg  SHARED-LIB     #########                      /usr/lib/libc.so.6
```

**Permission check on PID 1 (30 s) - option 5.** Point at `-r--r--r--` and
"[ -r ] says readable", then at `Permission denied`. The mode bits say yes, but the
kernel adds a ptrace-access check when the file is opened.

**strace of vmv reading /proc (15 s) - option 7.** Point at `openat(... "/proc/<pid>/maps" ...)`,
then `read(...)` lines, then `close(...)`: plain system calls, no stdio.

## Part 2 - Abdul Wali Soomro: the live demo (about 3.5 min, Labs 5, 6, 7, 8, 9)

**Live demo - option 1.** The workload changes phase every 3 seconds; the whole run
takes about 27 seconds. The current phase is printed in the header (`Phase: ...`).
Each box title shows its change since the last sample, and the lines under the boxes
list regions that appeared (`+ new`) or disappeared (`- gone`). Point at, phase by phase:

- **0 baseline** - note Reserved (VmSize), In RAM (VmRSS) and the minor fault count.
- **1 small mallocs** - the HEAP box grows (brk). In the demo the target is a forked copy
  of vmv, so the growth also shows as a `+ new HEAP` line.
- **2 malloc(128 MiB)** - MEMORY-MAPPED AREA `(+128.0 MiB)` and `+ new ANON-MMAP 128.0 MiB`;
  Reserved +128 MiB, but In RAM barely changes: demand paging, nothing is in RAM until it
  is touched.
- **3 touch 64 MiB** - In RAM +64 MiB and the minor page faults jump.
- **4 four pthreads** - point at the new 32 MiB region (4 stacks of 8 MiB: on our VM
  `+ new ANON-MMAP 32.0 MiB`, on older glibc a `thread stacks` row) and at "Threads: 5"
  (`5 threads (+4)` in the header). To prove there are 5 threads, run in the second
  terminal `ps -L -p <pid> -o pid,lwp,nlwp,comm` with the target PID from the
  `Processes:` line under the diagram: NLWP is 5 - one process, five threads.
  (Phase 4 lasts about 6 seconds; `ps -L -C vmv -o pid,lwp,nlwp,comm` needs no PID and
  lists all three vmv processes - the target is the one with NLWP 5.)
- **5 threads joined** - Threads back to 1, but the stack memory stays: glibc keeps
  freed thread stacks for reuse.
- **6 free(128 MiB)** - `- gone ANON-MMAP 128.0 MiB` (munmap); Reserved -128 MiB.
- **7 exit** - the header says `zombie`, the boxes are gone, and the ZOMBIE message
  appears. The address space is gone but the process-table entry stays until the
  parent calls waitpid().
- **End** - `Reaped target PID ...: exited with status 0` and
  `/proc/<pid> no longer exists`.

**Process tree.** While the demo runs, in the second terminal:
`ps -o pid,ppid,stat,cmd -u "$USER" | grep vmv`. You see three `./bin/vmv demo` lines:
the parent, and two children whose PPID is the parent's PID. During the zombie phase
the target shows as `Z ... [vmv] <defunct>`. (The `grep vmv` line itself may also appear.)

**strace of malloc and pthread_create - option 6.** Point at `brk(` lines (heap
growth), `mmap(NULL, 134221824, ...)` (128 MiB + 4 KiB of malloc bookkeeping),
`munmap(..., 134221824)` (the free), one `mmap` of 8392704 bytes (8 MiB stack + 4 KiB
guard) and one `clone3(` per thread. Check this on the class machine first - see the note
under "What we measured".

## Part 3 - Sameer Sethar: parallel scan, sockets, the bash layer (about 3 min, Labs 1, 3, 4, 8, 10)

**OpenMP scan - option 8.** It runs the whole-system scan 50 times with 1 thread and
then with all cores. Point at the Top 10 table, the two `SCAN_TIME_SEC` lines, and
`Speed-up on N cores`. Say why it is not N times faster: each process costs only a few
short system calls, so kernel work and thread overhead dominate.

**Unix socket - option 9.** Point at `srwxr-xr-x ... /tmp/vmv.sock` (`s` = socket),
the `PID ... VmSize=... Heap=... Stack=...` reply, the `ERROR 999999: no such process`
reply, and the server log with one line per request. The client is an unrelated
process; it only knows the socket path.

**The bash layer (1 min).**
- getopts: `./vmv.sh -h`, then `./vmv.sh -l -s; echo $?` (two modes: exit 1).
- select: the menu itself is a bash `select` loop.
- Cross-check - option 4: the shell script reads the same maps file with only bash,
  sort and cut. Its `Regions:` count equals vmv's, and "Difference: 4 kB" is the
  [vsyscall] page.
- Packaging: `scripts/package.sh` prints `Archive verified: contents identical`.

**PNG - option 10 (optional).** Writes `/tmp/vmv_layout.csv` and `vmv_layout.png`;
open the PNG with `xdg-open vmv_layout.png`.

**Close (15 s).** "Everything came from files the kernel already publishes, read with the
system calls from the labs."

## Optional: the GUI window (menu option 11)

`python3 tools/vmv_gui.py --demo` starts `bin/vmv_demo 3 2` and shows it in a 16:9
matplotlib window that samples once a second and animates at 30 fps. It can replace or
follow option 1 in Part 2. On the projector use
`python3 tools/vmv_gui.py --demo --scale 1.25` and press F for fullscreen; add
`--theme dark` if the room is dark.

- Banner: "Phase N of 7", progress dots and what the demo just did. Read it aloud.
- Left: the address space, high addresses at the top: STACK, MEMORY-MAPPED AREA, HEAP and
  PROGRAM (code, rodata, data, bss). Inside the memory-mapped area: the top 4 files, every
  anonymous region of 1 MiB or more on its own "anonymous mmap" row, the small ones
  together as "small anonymous (n)", then "… N more". Normally heights are on a log
  scale, so a bigger region is always a taller block.
- Right: Reserved (VmSize) and In RAM (VmRSS), the share in RAM, page faults and threads;
  below, both on a 60-second chart.

**Focus view.** After every sample the block whose size changed most (64 KiB or more; a
new region counts with its whole size) becomes the focus: it grows to 30 % of the column,
gets an outline in its own colour, its name in large type and a line saying what
changed. Every other block shrinks but stays visible, so the whole address space is
still on screen. What to point at:

- Phase 1: HEAP is the big block, "grew 132 KiB → 1.4 MiB" (brk moved the heap's end).
- Phase 2: a new "anonymous mmap" row, "+128.0 MiB · new region". In RAM barely moves:
  demand paging.
- Phase 3: no region changes size, so the focus stays on the 128 MiB row. In RAM pulses
  and shows a pill of about "+64.0 MiB": the touched pages are now in RAM.
- Phase 4: another new row, "+32.0 MiB · new region", "appeared with +4 threads". The
  window only shows that both happened in the same sample; explain yourself that each
  thread needs a stack and that glibc maps them with mmap.
- Phase 6: "−128.0 MiB · freed" fades out where the region was (free() handed it back
  with munmap), then the column returns to the overview.
- Zombie: no focus; every block fades.

The focus stays until the next change of 64 KiB or more; after 8 s without one the
column eases back to the overview. The caption under the column says which mode is on.

**Presenter control.** Click a block to pin the focus on it (dashed outline, caption
"pinned", no 8 s timeout); its second line shows its size and start address. Click the
empty column to unpin. O turns auto focus off and on (off is "overview": log heights
only, no focus until you pin one).

Keys: Space pauses sampling (to talk over a phase; the dot turns gray and the focus
holds), F fullscreen, O auto focus on/off, R restarts the demo from phase 0 (if a
question needs a phase again), Q or Esc quits. The old demo is stopped and reaped on R
and Q.

Install the window backend before class: `sudo pacman -S python-pyqt6`
(Ubuntu: `sudo apt install python3-pyqt5`). Without it the script prints that command and
exits. Python only draws; every number comes from `./bin/vmv csv`.

## What we measured on our VM

Machine: Linux 7.3.3-1-cachyos, x86-64, 8 cores (`nproc`), glibc 2.44, gcc 16.2.1,
transparent huge pages `[always] madvise never`, `ulimit -s` 8192 (KiB). The numbers
differ slightly on every machine and every run.

- Snapshot of a zsh shell: 45 regions, VmSize 8.9 MiB, VmRSS 4.5 MiB. The shell script
  cross-check gave sum of regions 9108 kB vs VmSize 9104 kB (difference 4 kB = [vsyscall]).
  The menu's own bash process had 38 or 39 regions, depending on the run.
- `ls -l /proc/<pid>/maps` shows size 0; `wc -c` on the same file gave 3943 bytes.
- Live demo (28 samples, `-i 1`):
  - baseline: VmSize 3.6 MiB, VmRSS 1.3 MiB, 31 regions, 30 minor faults;
  - phase 1: a new 1.1 MiB `[heap]` row (288 pages) above the first 132 KiB row;
    VmSize +1.1 MiB, VmRSS +1.4 MiB, minor faults +325;
  - phase 2: a new 128.0 MiB region of 32769 pages (= 134221824 bytes); VmSize +128.0 MiB,
    VmRSS +4.0 KiB, minor faults +1;
  - phase 3: VmRSS +64 MiB over two samples (+14.0 MiB, +50.0 MiB); minor faults
    +32 in total (7 + 25), not 16384, because THP is set to "always" (64 MiB / 2 MiB = 32);
  - phase 4: Threads 1 → 5, VmSize +32.0 MiB; `ps -L` showed NLWP 5;
  - phase 5: Threads 5 → 1, the 32 MiB of stacks stayed mapped;
  - phase 6: the 128 MiB region was listed as removed; VmSize -128.0 MiB, VmRSS -64.0 MiB;
  - zombie: 3 samples with `State: Z`, `Regions: 0`; then
    `Reaped target PID ...: exited with status 0` and `/proc/... no longer exists`.
- Scan of about 345 PIDs, 50 repeats: 1 thread 0.5452 s, 8 threads 0.1077 s, speed-up
  5.06x. Other runs on the same busy desktop gave between 2.5x and 5.2x. In every run,
  2 to 4 PIDs were "exited before they could be read" - most likely the `ls` and `grep`
  of the pipeline itself.
- Socket: `PID 51919 (menu.sh) State=S Threads=1 VmSize=8.4 MiB VmRSS=4.3 MiB Regions=39 Heap=264.0 KiB Stack=136.0 KiB`
  and `ERROR 999999: no such process`.

Differences we saw on our VM that may not happen on the class machine:

- **Phase 4 showed no `THREAD-STACK?` and no `GUARD` rows.** The four stacks appeared
  as one 32 MiB ANON-MMAP row of 8196 pages (= 4 x (2048 + 1) pages: 8 MiB of stack
  plus a 4 KiB guard page each). strace of `./bin/vmv_demo 1` showed why: for each thread
  glibc 2.44 maps 8392704 bytes read-write and then calls
  `madvise(<lowest page>, 4096, MADV_GUARD_INSTALL)`, so the guard lives inside the
  mapping, maps has no `---p` row, and the stacks merge into one region. On an older glibc
  (for example Ubuntu 24.04) we expect separate `GUARD` + `THREAD-STACK?` rows; we have
  not tested this. Menu option 6 does not trace `madvise`, so it shows the `mmap` and
  `clone3` calls but not the guard installation.
- **Menu options 6 and 7 themselves have not been rehearsed yet.** The direct strace above
  confirmed the 8392704-byte stack `mmap` and `clone3`; 134221824 matches the 128 MiB
  region we measured. The exact system calls depend on glibc - run both options once
  before class.

## If something goes wrong live

- Build fails or a binary is missing: `./build.sh clean && ./build.sh`.
- `strace is not installed`: `sudo apt install strace`.
- The server says "Address already in use" or an old `/tmp/vmv.sock` is left over:
  just start it again - the server removes the old socket file before `bind()`.
- The demo is too fast to talk over: in terminal 2 run `./bin/vmv_demo 5 10`
  (5 s per phase, starts after 10 s) and copy the PID it prints; in terminal 1 run
  `./bin/vmv live <pid>`. You get the same phases at a slower pace. The zombie phase
  does not appear this way, because the shell reaps `vmv_demo` at once; use option 1 for that.
- `permission denied` on a process: it belongs to another user. Pick one of your own
  (option 3), or use `sudo`.
- The screen looks garbled: the terminal is too narrow - make it wider or zoom out.
