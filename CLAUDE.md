# CLAUDE.md

## Project

**Virtual Memory Visualizer (vmv)** is the semester project for CS251 Operating
Systems Lab (Sukkur IBA, BSCS-V Section F), built by Muhammad Tahzeeb Khan (lead),
Abdul Wali Soomro and Sameer Sethar. It reads what Linux already exposes for a running
process (`/proc/<pid>/maps`, `/proc/<pid>/status`, `/proc/<pid>/stat`) and draws that
process's virtual address space as a colour-coded terminal diagram. It is demoed live
in class and defended in a viva, so readability beats cleverness: small functions,
plain C, and comments that explain the OS concept behind each step.

## Course constraint — only Labs 0–10

- C with gcc, never C++. Library: stdio (printf, fprintf, snprintf, sscanf, scanf, perror, fflush), stdlib (malloc, free, exit, atoi), string.h (strcmp, strncmp, strcpy, strncpy, strlen, strchr, strrchr, strstr, memset, memcpy), ctype.h (isdigit, toupper), errno.h.
- System calls: open, read, write, close, dup, dup2, pipe, fork, getpid, getppid, sleep, unlink, wait, waitpid (WNOHANG, WIFEXITED, WEXITSTATUS, WIFSIGNALED, WTERMSIG), kill with SIGTERM, socket/bind/listen/accept/connect (AF_UNIX + SOCK_STREAM only), syscall(SYS_gettid).
- Pthreads: pthread_create, pthread_join, pthread_exit, pthread_self, pthread_cancel.
- OpenMP: parallel, parallel for, schedule(static|dynamic), private, shared, reduction, sections, num_threads, omp_get_thread_num, omp_get_num_threads, omp_get_max_threads, omp_get_wtime.
- Bash: pipes, redirection, grep, sort, uniq, cut, awk, sed, head, tail, wc, find, tar, diff, command substitution, getopts, select, case, functions with local, indexed and associative arrays, mapfile, file tests, set -euo pipefail, ps, pgrep, pkill, kill, background &, strace, nproc.

## Forbidden

exec family, system(), popen(), opendir/readdir, calling mmap/munmap directly (only
malloc/free may trigger them), signal()/sigaction() handlers, pthread_mutex_*,
pthread_cond_*, semaphores, atomics (synchronization is Lab 11 — not covered yet),
strtok, qsort, realloc, select/poll/epoll, ncurses or any third-party C library,
Makefile/CMake (Lab 2 taught gcc -c + link, which build.sh does), -std=c99/-std=c11
flags (strict modes hide syscall(); use gcc's default dialect). Python is allowed ONLY
in tools/plot_layout.py (Part 5).

## Rules

- Every use of a lab concept gets a tag comment `/* [Lab N] what and why */` in C or
  `# [Lab N] what and why` in bash — ONE lab number per tag (use two tags if two labs
  apply), because these tags are grepped to build docs/LAB_MAPPING.md and must be
  accurate.
- No static or global buffers in src/procfs.c (Part 3 calls it from OpenMP threads).
- Always `fflush(stdout)` before `fork()`.
- `./build.sh` must finish with zero warnings under -Wall -Wextra.
- Never commit.
