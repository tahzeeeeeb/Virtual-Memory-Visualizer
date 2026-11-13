/*
 * main.c - command-line entry point of the Virtual Memory Visualizer.
 *
 *   vmv snapshot <pid> [-o FILE] [--no-color]
 *   vmv live <pid> [-i SEC] [-n COUNT] [-o FILE] [--no-color]
 *   vmv demo [-i SEC] [-n COUNT] [-o FILE] [--no-color]
 *   vmv scan [--repeat N]     (PIDs on stdin)
 *   vmv csv <pid>
 *   vmv serve
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
#include "live.h"
#include "scan.h"
#include "server.h"

enum command { CMD_SNAPSHOT, CMD_LIVE, CMD_DEMO, CMD_SCAN, CMD_CSV, CMD_SERVE };

static void usage(void)
{
    fprintf(stderr,
            "usage: vmv snapshot <pid> [-o FILE] [--no-color]\n"
            "       vmv live <pid> [-i SEC] [-n COUNT] [-o FILE] [--no-color]\n"
            "       vmv demo [-i SEC] [-n COUNT] [-o FILE] [--no-color]\n"
            "       vmv scan [--repeat N]     (PIDs on stdin, e.g.  ls /proc | grep '^[0-9][0-9]*$' | ./bin/vmv scan)\n"
            "       vmv csv <pid>\n"
            "       vmv serve\n");
    exit(1);
}

/* 1 if s is made only of digits (and is short enough not to overflow atoi). */
static int is_digits(const char *s)
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

/* Send stdout into FILE. Returns 0, or -1 (error already printed). */
static int redirect_stdout(const char *file)
{
    int fd;

    fd = open(file, O_WRONLY | O_CREAT | O_TRUNC, 0644);   /* [Lab 5] */
    if (fd < 0) {
        perror(file);
        return -1;
    }
    /* [Lab 5] dup2 makes fd 1 (stdout) point at the file - exactly what the
     * shell does for `> FILE`. printf now writes into the file; errors
     * still go to stderr (fd 2), which we did not touch. */
    dup2(fd, 1);
    close(fd);   /* [Lab 5] fd 1 keeps the file open, the copy is not needed */
    return 0;
}

/* snapshot (Part 1) and csv (Part 3): one snapshot, printed once. */
static int cmd_snapshot(int pid, const char *outfile, int color, int as_csv)
{
    struct snap_hdr hdr;
    struct region *regions;
    int rc;

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
        print_denied(pid);
        rc = 3;
    } else {
        rc = 0;
        /* Redirect only after a good snapshot, so a failed run leaves no empty file. */
        if (outfile != NULL && redirect_stdout(outfile) < 0) {
            free(regions);
            return 1;
        }
        if (as_csv)
            render_csv(regions, hdr.nregions);
        else
            render_snapshot(&hdr, regions, NULL, NULL, color, 0);
        fflush(stdout);
    }

    free(regions);   /* [Lab 0] release the region array */
    return rc;
}

int main(int argc, char *argv[])
{
    enum command cmd;
    int i;
    int pid = -1;
    int interval = 1;            /* -i: seconds between samples */
    int count = 0;               /* -n: 0 = until the process ends */
    int repeat = 1;              /* --repeat: how many times scan runs */
    int color = 1;               /* colour on by default */
    const char *outfile = NULL;
    int takes_pid, takes_output, takes_timing;
    int rc;

    if (argc < 2)
        usage();
    if (strcmp(argv[1], "snapshot") == 0)
        cmd = CMD_SNAPSHOT;
    else if (strcmp(argv[1], "live") == 0)
        cmd = CMD_LIVE;
    else if (strcmp(argv[1], "demo") == 0)
        cmd = CMD_DEMO;
    else if (strcmp(argv[1], "scan") == 0)
        cmd = CMD_SCAN;
    else if (strcmp(argv[1], "csv") == 0)
        cmd = CMD_CSV;
    else if (strcmp(argv[1], "serve") == 0)
        cmd = CMD_SERVE;
    else
        usage();

    /* which options each command accepts */
    takes_pid    = (cmd == CMD_SNAPSHOT || cmd == CMD_LIVE || cmd == CMD_CSV);
    takes_output = (cmd == CMD_SNAPSHOT || cmd == CMD_LIVE || cmd == CMD_DEMO);
    takes_timing = (cmd == CMD_LIVE || cmd == CMD_DEMO);

    /* ---- parse the rest of argv by hand: options may come in any order ---- */
    for (i = 2; i < argc; i++) {
        if (strcmp(argv[i], "-o") == 0 && takes_output) {
            if (i + 1 >= argc || outfile != NULL)
                usage();
            outfile = argv[++i];
            color = 0;           /* a file should not contain escape codes */
        } else if (strcmp(argv[i], "--no-color") == 0 && takes_output) {
            color = 0;
        } else if (strcmp(argv[i], "-i") == 0 && takes_timing) {
            if (i + 1 >= argc || !is_digits(argv[i + 1]))
                usage();
            interval = atoi(argv[++i]);
            if (interval < 1)    /* whole seconds, because sleep() takes seconds */
                usage();
        } else if (strcmp(argv[i], "-n") == 0 && takes_timing) {
            if (i + 1 >= argc || !is_digits(argv[i + 1]))
                usage();
            count = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--repeat") == 0 && cmd == CMD_SCAN) {
            if (i + 1 >= argc || !is_digits(argv[i + 1]))
                usage();
            repeat = atoi(argv[++i]);
            if (repeat < 1)
                usage();
        } else if (takes_pid && pid == -1 && is_digits(argv[i])) {
            pid = atoi(argv[i]);
        } else {
            usage();
        }
    }
    if (takes_pid && pid < 0)
        usage();

    switch (cmd) {
    case CMD_SNAPSHOT:
        return cmd_snapshot(pid, outfile, color, 0);
    case CMD_CSV:
        return cmd_snapshot(pid, NULL, 0, 1);
    case CMD_SCAN:
        return run_scan(repeat);
    case CMD_SERVE:
        return run_server();
    default:
        break;
    }

    /* live and demo: redirect first, the forked children inherit fd 1 */
    if (outfile != NULL && redirect_stdout(outfile) < 0)
        return 1;
    if (cmd == CMD_LIVE)
        rc = run_live(pid, interval, count, color);
    else
        rc = run_demo(interval, count, color);
    fflush(stdout);
    return rc;
}
