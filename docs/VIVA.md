# Viva questions and answers

Short answers in plain words, each naming the code involved. "On our VM" means our
development machine: Linux 7.3.3, glibc 2.44, 8 cores, transparent huge pages "always",
`ulimit -s` 8192. See docs/DEMO.md for the full list of measurements.

## Reading /proc

**1. Why do you use open/read/close instead of fopen?**
The labs (Lab 5) teach the system calls directly, and we wanted the audience to see them.
`read_whole_file()` in `src/procfs.c` calls `open`, loops on `read`, then calls `close`.
glibc's `open()` is a thin wrapper around the `openat` system call, so strace shows
`openat/read/close` (menu option 7). `fopen` would end up making the same calls, with a
buffer in between. (We could not run strace on our VM - it was not installed - so check
option 7 on the class machine.)

**2. `ls -l` shows /proc files with size 0. How can they have content?**
They are not stored anywhere: the kernel builds the text at the moment you read it, so it
cannot report a size in advance. On our VM `ls -l /proc/<pid>/maps` showed 0 and
`wc -c` on the same file gave 3943 bytes. That is why `read_whole_file()` keeps calling
`read()` until it returns 0 instead of asking for the file size first.

**3. What are the six columns of a maps line?**
Address range (start-end), permissions, offset into the file, device, inode, and path.
Example from our VM: `7ff1a5424000-7ff1a55d3000 r-xp 00024000 00:1f 449220 /usr/lib/libc.so.6`.
Anonymous memory has inode 0 and no path. `parse_maps()` in `src/procfs.c` reads the
first four with `sscanf` (`%lx` for hex) and takes the rest of the line as the path.

**4. What is the difference between p and s in the permissions?**
`p` is a private mapping: if the process writes to it, it gets its own copy of the page
(copy-on-write) and nobody else sees the change. `s` is a shared mapping: writes are
visible to every process that maps the same thing. On our VM the shell had one `r--s`
region, `/usr/lib/gconv/gconv-modules.cache`; everything else was private.

## The diagram

**5. Why are the bars on a log scale and the gaps compressed into one line?**
Region sizes range from 4 KiB to hundreds of MiB, and user space on x86-64 is 128 TiB
(47-bit addresses). Drawn to scale, every region would be invisible. `bar_length()` in
`src/render.c` uses floor(log2(size)) - 11, and `print_gap()` prints one
`~~ size unmapped ~~` line per hole - on our VM the holes reached 41.8 TiB and 85.7 TiB.

**6. What is the region at 0xffffffffff600000?**
It is `[vsyscall]`, an old kernel page for fast system calls. It sits above the user/kernel
boundary. maps lists it, but it is not a real VMA of the process, so VmSize does not count
it. On our VM the shell script cross-check gave a sum of all regions of 9108 kB against a
VmSize of 9104 kB: the 4 kB difference is that page. `render_snapshot()` prints a note when it
is present.

## Memory behaviour in the live demo

**7. What is demand paging, and where do you show it?**
`malloc()` only reserves virtual addresses; a page gets a physical frame the first time it
is touched, and each first touch is a minor page fault. In phase 2 (`run_workload()` in
`src/workload.c`) we malloc 128 MiB and do not touch it. On our VM VmSize grew by
128.0 MiB, VmRSS by only 4.0 KiB, and the minor fault count by 1.

**8. What is the difference between a minor and a major page fault?**
A minor fault is solved in memory: the kernel gives the page a frame (or finds it already in
the page cache) without disk I/O. A major fault has to read the page from disk, which is
much slower. Our demo only causes minor faults - the major count stayed 0 in every sample.

**9. Touching 64 MiB is 16384 pages. Why did the fault count go up by much less?**
Transparent huge pages were set to `[always]` on our VM, so the kernel can map 2 MiB at a
time for a large anonymous region. Phase 3 raised the minor fault count by 32 in total
(7 + 25 over two samples) while VmRSS grew by 64 MiB: 64 MiB / 2 MiB = 32. With THP set to
"never" we would expect close to 16384.

**10. Why do the 64 KiB mallocs grow [heap] while the 128 MiB malloc creates a new region?**
glibc's malloc uses `brk` (moving the end of the heap) for requests below its mmap
threshold, 128 KiB by default, and a separate `mmap` above it. glibc raises the threshold
on its own after large blocks are freed, up to 32 MiB. On our VM phase 1 added 1.1 MiB of
heap and phase 2 added one region of 32769 pages = 134221824 bytes (128 MiB plus 4 KiB of
malloc bookkeeping). `free()` of that block is a `munmap`, so in phase 6 the region
disappears. strace should show `brk(...)` and `mmap(NULL, 134221824, ...)`; we did not
check that on our VM (no strace). In `vmv demo` the heap growth shows as a second
`[heap]` row because the target is a forked child: the kernel did not merge the new heap
area with the one inherited from the parent. In standalone `bin/vmv_demo` it showed as one
row that grew (`~`).

**11. Why does each thread add memory, and why is there only one maps file?**
Every pthread needs its own stack - by default the size of `ulimit -s` (8 MiB on our VM) -
plus a 4 KiB no-access guard page below it, so a stack overflow crashes instead of
overwriting other memory. All threads share one address space, so the whole process has one
maps file; `ps -L` showed NLWP 5 for the target in phase 4. On our VM the four stacks
appeared as one 32 MiB region of 8196 pages = 4 x (2048 + 1) pages, with no separate guard
rows. Our explanation: glibc 2.44 marks the guard page with madvise() instead of a separate
`---p` mapping, so the stacks merge. Older glibc versions show each guard as its own `---p`
region, and strace shows `mmap(..., PROT_NONE, ...)` + `mprotect` + `clone3`; we did not
test an older glibc ourselves. The threads are created in `run_workload()` in `src/workload.c`.

**12. Do thread stacks go away after pthread_join?**
Not always: glibc keeps freed thread stacks in a cache to reuse them for the next thread.
On our VM, after the join in phase 5 the thread count went from 5 to 1, but the 32 MiB
stack region stayed mapped until the process exited.

**13. Why does a zombie have an empty maps file?**
When a process exits, the kernel releases its whole address space at once. What is left is
the process-table entry with its exit status, kept until the parent calls `waitpid()`. On our
VM the demo showed 3 samples with `State: Z` and `Regions: 0`, then `run_demo()` in
`src/live.c` reaped it and `/proc/<pid>` disappeared.

## Processes, pipes and files

**14. Why is the target forked before the pipe is created, and why close the unused pipe ends?**
`read()` on a pipe returns 0 (end of file) only when every copy of the write end is closed.
If the target or the parent kept a copy of the write end, the parent's `readn()` would never
see EOF and vmv would hang - the classic Lab 8 bug. So `run_demo()` forks the target first,
and `start_sampler()` in `src/live.c` closes the read end in the sampler and the write end in
the parent.

**15. Why fflush(stdout) before fork()?**
`printf` collects text in a buffer inside the process. `fork()` copies the whole process,
including that buffer, so text that was not flushed yet would be printed twice - once by the
parent and once by the child. `start_sampler()` and `run_demo()` flush before every fork.

**16. How does `-o FILE` work?**
`redirect_stdout()` in `src/main.c` opens the file, calls `dup2(fd, 1)` so file descriptor 1
(stdout) points at it, and closes the extra descriptor - exactly what the shell does for
`> FILE`. Every later `printf` goes into the file; errors still go to stderr (fd 2). Colour
is turned off, so the file contains no escape codes.

## Threads and OpenMP

**17. Why pass &ids[i] to pthread_create and not &i?**
This is the Lab 9 loop-variable bug. `&i` is the address of one variable that the loop keeps
changing, so a thread may read `i` after the loop has moved on, and two threads can get the
same number. `run_workload()` stores each value in its own slot `ids[i]`, which never
changes, and passes that address.

**18. Why is there no mutex anywhere?**
Synchronization is Lab 11, which the course has not covered yet, so we designed it away.
The demo threads write nothing shared. In the scan (`run_scan()` in `src/scan.c`) iteration
`i` writes only `results[i]`, and the totals use `reduction(+:...)`: each thread adds into
its own private copy and OpenMP combines them at the end.

**19. Why schedule(dynamic), and why is the speed-up not 8x?**
Some processes cost more to read than others (more threads, longer status), so dynamic
scheduling lets a free thread take the next PID instead of getting a fixed block. On our VM
50 scans of about 345 PIDs took 0.5452 s with 1 thread and 0.1077 s with 8: a 5.06x speed-up
on 8 cores. Other runs gave between 2.5x and 5.2x. Each PID costs only about 32 µs (two short
open/read/close cycles), so kernel work, thread start-up and the busy desktop limit the gain.

## Permissions and design choices

**20. ls -l shows /proc/1/maps as r--r--r-- and [ -r ] says readable. Why does reading it fail?**
The mode bits are only part of the check. When maps is opened, procfs also checks whether you
are allowed to ptrace (debug) that process; PID 1 belongs to root, so `open()` fails with
`EACCES`. `test -r` only looks at the mode bits. Menu option 5 shows all three steps.
`take_snapshot()` in `src/procfs.c` turns `EACCES` into `SNAP_DENIED`, and `src/main.c`
turns that into exit code 3.

**21. Why does the scan's VmRSS sum overcount?**
VmRSS counts every page a process has in RAM, including shared library pages. libc is in
memory once but counted once per process using it. On our VM the sum was 9.9 GiB. Using PSS
(proportional set size) from `/proc/<pid>/smaps_rollup` would fix this - future work.

**22. Why does the scan read PIDs from a shell pipeline instead of reading /proc with opendir?**
The directory-reading functions (opendir/readdir) were not in the labs. The pipeline
`ls /proc | grep '^[0-9][0-9]*$' | ./bin/vmv scan` also shows Lab 1 and Lab 8 pipes feeding a
C program. In every run on our VM 2 to 4 PIDs "exited before they could be read" - most
likely the `ls` and `grep` processes of that same pipeline.

**23. Why does THREAD-STACK? have a question mark?**
maps does not label thread stacks. `classify_regions()` in `src/classify.c` guesses one when
it sees anonymous `rw-p` memory of at least 64 KiB directly above an anonymous `---p` guard
page; a malloc arena can occasionally look the same. On our VM the label never appeared,
because with glibc 2.44 there were no separate guard regions (see question 11).

**24. Why does the demo target show up as "vmv" and not "vmv_demo"?**
`run_demo()` forks a copy of vmv and calls `run_workload()` in it; it does not exec a new
program, because the exec family is not part of the labs. So the target runs vmv's own code
and its maps shows `bin/vmv` as the executable. `bin/vmv_demo` is the same workload built as
its own program, for watching from a second terminal.

**25. What would you add next?**
Per-region RSS from `/proc/<pid>/smaps`, to show how much of each region is really in RAM.
Present/swapped bits per page from `/proc/<pid>/pagemap` (the physical frame numbers need
root). And a copy-on-write view after fork, showing pages that parent and child still share.
