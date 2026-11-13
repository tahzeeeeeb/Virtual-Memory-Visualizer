/*
 * procfs.c - read a process's memory information from /proc.
 *
 * [Lab 2] /proc is the virtual filesystem from the Linux directory-hierarchy lab:
 *         its files are not stored on disk, the kernel generates them when read.
 *
 * Thread safety: Part 3 calls these functions from several OpenMP threads at once,
 * so this file has NO static or global buffers - every buffer is a local variable
 * or comes from the caller.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>

#include "procfs.h"
#include "classify.h"

/*
 * Read a whole file into buf (at most cap-1 bytes) and NUL-terminate it.
 * Returns the number of bytes read, or -1 with errno set.
 * If the file is bigger than the buffer, *truncated is set to 1 (truncated may be NULL).
 */
ssize_t read_whole_file(const char *path, char *buf, size_t cap, int *truncated)
{
    int fd;
    int saved_errno;
    size_t total = 0;
    ssize_t n;
    char extra;

    if (cap == 0) {
        errno = EINVAL;
        return -1;
    }

    fd = open(path, O_RDONLY);   /* [Lab 5] raw system call, returns a file descriptor */
    if (fd < 0)
        return -1;               /* errno already set by open() */

    /*
     * procfs files show size 0 in `ls -l` because the kernel generates their
     * contents only when they are read. We cannot ask for the size first, so we
     * keep calling read() until it returns 0 (end of file) or the buffer is full.
     */
    while (total < cap - 1) {
        n = read(fd, buf + total, cap - 1 - total);   /* [Lab 5] raw read() */
        if (n < 0) {
            saved_errno = errno;   /* close() could overwrite errno, so save it */
            close(fd);
            errno = saved_errno;
            return -1;
        }
        if (n == 0)
            break;                 /* end of file */
        total += (size_t)n;
    }
    buf[total] = '\0';

    /* Buffer full: try one more byte to see whether the file really had more. */
    if (total == cap - 1 && read(fd, &extra, 1) > 0 && truncated != NULL)
        *truncated = 1;

    close(fd);   /* [Lab 5] always release the file descriptor */
    return (ssize_t)total;
}

/*
 * Split the text of /proc/<pid>/maps into struct region entries.
 * A maps line looks like:
 *   55c05fb04000-55c05fb0f000 r-xp 00002000 00:1f 932669     /usr/bin/head
 *   start-end                 perms offset  dev   inode      path (optional)
 * The text is modified in place ('\n' becomes '\0').
 * Returns the number of regions stored in out[].
 */
int parse_maps(char *text, struct region *out, int max, int *truncated)
{
    int count = 0;
    char *line = text;
    char *next;
    char *nl;
    char *p;
    int n;
    struct region *r;

    while (*line != '\0') {
        /* Cut off one line with strchr (no strtok: it keeps hidden static state). */
        nl = strchr(line, '\n');
        if (nl != NULL) {
            *nl = '\0';
            next = nl + 1;
        } else {
            next = line + strlen(line);   /* last line had no '\n' */
        }

        if (count >= max) {               /* a line remains but out[] is full */
            if (truncated != NULL)
                *truncated = 1;
            break;
        }

        r = &out[count];   /* [Lab 0] pointer to the next free struct in the array */
        n = 0;
        /* [Lab 0] %lx reads hex numbers, %*s / %*lu read and discard dev and inode,
         * %n stores how many characters were consumed so we can find the path.
         * (Skipped fields use %*u, not %*lu: nothing is stored, so a length
         * modifier is meaningless and gcc -Wformat warns about it.) */
        if (sscanf(line, "%lx-%lx %4s %lx %*s %*u%n",
                   &r->start, &r->end, r->perms, &r->offset, &n) == 4 && n > 0) {
            p = line + n;
            while (*p == ' ')
                p++;
            /* The rest of the line is the path: may be empty, may contain spaces
             * or end in " (deleted)". strncpy does not always add '\0', so we do. */
            strncpy(r->path, p, PATH_LEN - 1);
            r->path[PATH_LEN - 1] = '\0';
            r->kind = K_ANON;   /* real kind is set later by classify_regions() */
            count++;
        }
        line = next;
    }
    return count;
}

/* Skip spaces and tabs after a "Key:" in /proc/<pid>/status. */
static char *skip_blanks(char *p)
{
    while (*p == ' ' || *p == '\t')
        p++;
    return p;
}

/* If line starts with key (e.g. "VmRSS:"), read the number of kB after it. */
static void parse_kb_line(char *line, const char *key, long *out)
{
    size_t len = strlen(key);

    if (strncmp(line, key, len) == 0)
        sscanf(line + len, " %ld", out);   /* the line ends in " kB" which we ignore */
}

/*
 * Fill info from /proc/<pid>/status (name, state, sizes) and /proc/<pid>/stat
 * (page-fault counters). Returns 0, or -1 with errno set if status cannot be read.
 */
int read_proc_info(int pid, struct proc_info *info)
{
    char path[64];
    char buf[8192];        /* local: safe when several threads call this at once */
    char statbuf[1024];    /* local: same reason */
    char *line;
    char *nl;
    char *p;
    unsigned long minflt, majflt;

    memset(info, 0, sizeof *info);   /* [Lab 0] start from a clean struct */
    info->pid = pid;
    info->state = '?';
    info->vm_size_kb = info->vm_rss_kb = info->vm_data_kb = info->vm_stk_kb = -1;
    info->vm_exe_kb = info->vm_lib_kb = info->vm_swap_kb = -1;

    /* ---- /proc/<pid>/status: one "Key:<tab>value" pair per line ---- */
    snprintf(path, sizeof path, "/proc/%d/status", pid);
    if (read_whole_file(path, buf, sizeof buf, NULL) < 0)
        return -1;   /* errno from open()/read(), e.g. ENOENT if pid does not exist */

    line = buf;
    while (*line != '\0') {
        nl = strchr(line, '\n');
        if (nl != NULL)
            *nl = '\0';

        if (strncmp(line, "Name:", 5) == 0) {
            p = skip_blanks(line + 5);
            strncpy(info->name, p, sizeof info->name - 1);
            info->name[sizeof info->name - 1] = '\0';
        } else if (strncmp(line, "State:", 6) == 0) {
            sscanf(line + 6, " %c", &info->state);   /* "S (sleeping)" -> 'S' */
        } else if (strncmp(line, "PPid:", 5) == 0) {
            sscanf(line + 5, " %d", &info->ppid);
        } else if (strncmp(line, "Threads:", 8) == 0) {
            sscanf(line + 8, " %d", &info->threads);
        } else {
            parse_kb_line(line, "VmSize:", &info->vm_size_kb);
            parse_kb_line(line, "VmRSS:",  &info->vm_rss_kb);
            parse_kb_line(line, "VmData:", &info->vm_data_kb);
            parse_kb_line(line, "VmStk:",  &info->vm_stk_kb);
            parse_kb_line(line, "VmExe:",  &info->vm_exe_kb);
            parse_kb_line(line, "VmLib:",  &info->vm_lib_kb);
            parse_kb_line(line, "VmSwap:", &info->vm_swap_kb);
        }

        if (nl == NULL)
            break;
        line = nl + 1;
    }

    /* ---- /proc/<pid>/stat: one line, "pid (name) state ppid ..." ---- */
    snprintf(path, sizeof path, "/proc/%d/stat", pid);
    if (read_whole_file(path, statbuf, sizeof statbuf, NULL) < 0)
        return 0;   /* status worked, so keep that data; faults stay 0 */

    /* The name in parentheses can itself contain spaces and ')', so we look for
     * the LAST ')' - everything after it is plain space-separated numbers. */
    p = strrchr(statbuf, ')');
    if (p != NULL &&
        sscanf(p + 1, " %*c %*d %*d %*d %*d %*d %*u %lu %*u %lu",
               &minflt, &majflt) == 2) {
        /* fields 10 (minflt) and 12 (majflt) of /proc/<pid>/stat */
        info->minflt = minflt;
        info->majflt = majflt;
    }
    return 0;
}

/*
 * Take one snapshot of process pid: header info plus all its memory regions.
 * regions must have room for MAX_REGIONS entries. The result is in hdr->status.
 */
void take_snapshot(int pid, struct snap_hdr *hdr, struct region *regions)
{
    char path[64];
    char phase[128];   /* local buffer for the optional phase file */
    char *maps;
    ssize_t n;
    size_t len;
    int truncated = 0;
    char *last_nl;

    /* 1. clean header */
    memset(hdr, 0, sizeof *hdr);
    hdr->info.pid = pid;

    /* 2. name, state, sizes, page faults */
    if (read_proc_info(pid, &hdr->info) < 0) {
        if (errno != ENOENT && errno != ESRCH) {
            snprintf(path, sizeof path, "/proc/%d/status", pid);
            perror(path);   /* unexpected error: tell the user why */
        }
        hdr->status = SNAP_GONE;
        return;
    }

    /* 3. the memory map itself */
    maps = malloc(MAPS_BUF);   /* [Lab 0] 1 MiB is too big for the stack, use the heap */
    if (maps == NULL) {
        perror("malloc");
        hdr->status = SNAP_GONE;
        return;
    }

    /*
     * Reading another user's /proc/<pid>/maps fails with EACCES even though
     * `ls -l` shows r--r--r--: procfs does an extra ptrace-access check at open(),
     * so only the owner (or root) may see another process's memory layout.
     */
    snprintf(path, sizeof path, "/proc/%d/maps", pid);
    n = read_whole_file(path, maps, MAPS_BUF, &truncated);

    if (n < 0 && errno == EACCES) {
        hdr->status = SNAP_DENIED;          /* belongs to another user */
        hdr->nregions = 0;
    } else if (n < 0 && errno == ENOENT) {
        hdr->status = SNAP_GONE;            /* exited between status and maps */
    } else if ((n < 0 && errno == ESRCH) || n == 0) {
        /* No user address space: a zombie if state is 'Z', else a kernel thread. */
        hdr->status = SNAP_OK;
        hdr->nregions = 0;
    } else if (n < 0) {
        perror(path);                       /* any other error: report and give up */
        hdr->status = SNAP_GONE;
    } else {
        /* If the buffer filled up, the last line may be cut in half: drop it. */
        if (truncated) {
            last_nl = strrchr(maps, '\n');
            if (last_nl != NULL)
                last_nl[1] = '\0';
        }
        hdr->nregions = parse_maps(maps, regions, MAX_REGIONS, &truncated);
        classify_regions(regions, hdr->nregions);
        hdr->status = SNAP_OK;
    }
    hdr->truncated = truncated;
    free(maps);   /* [Lab 0] every malloc gets a matching free */

    /* 4. optional phase label written by the demo workload (Part 2) */
    snprintf(path, sizeof path, "/tmp/vmv_phase_%d.txt", pid);
    if (read_whole_file(path, phase, sizeof phase, NULL) > 0) {
        len = strlen(phase);
        while (len > 0 && phase[len - 1] == '\n')
            phase[--len] = '\0';            /* drop the trailing newline */
        strncpy(hdr->phase, phase, sizeof hdr->phase - 1);
        hdr->phase[sizeof hdr->phase - 1] = '\0';
    }
}
