/*
 * main.c - command-line entry point of the Virtual Memory Visualizer.
 *
 * Part 1 supports one command:
 *   vmv snapshot <pid> [-o FILE] [--no-color]
 *
 * Exit codes: 0 ok, 1 usage error, 2 no such process, 3 permission denied.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <fcntl.h>
#include <unistd.h>

#include "procfs.h"
#include "render.h"

static void usage(void)
{
    fprintf(stderr, "usage: vmv snapshot <pid> [-o FILE] [--no-color]\n");
    exit(1);
}

/* 1 if s is made only of digits (and is short enough not to overflow atoi). */
static int is_pid_string(const char *s)
{
    size_t i;
    size_t len = strlen(s);

    if (len == 0 || len > 9)   /* Linux PIDs are at most 7 digits (4194304) */
        return 0;
    for (i = 0; i < len; i++)
        if (!isdigit((unsigned char)s[i]))
            return 0;
    return 1;
}

int main(int argc, char *argv[])
{
    int i;
    int pid = -1;
    int color = 1;               /* colour on by default */
    const char *outfile = NULL;
    int fd;
    struct snap_hdr hdr;
    struct region *regions;
    int rc;

    /* ---- parse argv by hand: options may come in any order ---- */
    if (argc < 2 || strcmp(argv[1], "snapshot") != 0)
        usage();
    for (i = 2; i < argc; i++) {
        if (strcmp(argv[i], "-o") == 0) {
            if (i + 1 >= argc || outfile != NULL)
                usage();
            outfile = argv[++i];
            color = 0;           /* a file should not contain escape codes */
        } else if (strcmp(argv[i], "--no-color") == 0) {
            color = 0;
        } else if (pid == -1 && is_pid_string(argv[i])) {
            pid = atoi(argv[i]);
        } else {
            usage();
        }
    }
    if (pid < 0)
        usage();

    /* [Lab 0] MAX_REGIONS structs (~1 MiB) are too big for the stack: use malloc */
    regions = malloc(MAX_REGIONS * sizeof *regions);
    if (regions == NULL) {
        perror("malloc");
        return 1;
    }

    take_snapshot(pid, &hdr, regions);

    if (hdr.status == SNAP_GONE) {
        fprintf(stderr, "vmv: no such process: %d\n", pid);
        rc = 2;
    } else if (hdr.status == SNAP_DENIED) {
        fprintf(stderr, "vmv: permission denied reading /proc/%d/maps - the process "
                "belongs to another user. Try: sudo ./bin/vmv snapshot %d\n", pid, pid);
        rc = 3;
    } else {
        rc = 0;
        if (outfile != NULL) {
            /* Redirect only after a good snapshot, so a failed run leaves no empty file. */
            fd = open(outfile, O_WRONLY | O_CREAT | O_TRUNC, 0644);   /* [Lab 5] */
            if (fd < 0) {
                perror(outfile);
                free(regions);
                return 1;
            }
            /* [Lab 5] dup2 makes fd 1 (stdout) point at the file - exactly what the
             * shell does for `> FILE`. printf now writes into the file; errors
             * still go to stderr (fd 2), which we did not touch. */
            dup2(fd, 1);
            close(fd);           /* [Lab 5] fd 1 keeps the file open, the copy is not needed */
        }
        render_snapshot(&hdr, regions, color);
        fflush(stdout);
    }

    free(regions);   /* [Lab 0] release the region array */
    return rc;
}
