/*
 * live.c - live mode: watch a process's address space change over time.
 *
 * Two processes cooperate:
 *
 *    SAMPLER child                         vmv PARENT
 *    take_snapshot() every interval  ---->  readn() header + regions
 *    writen() into the pipe          pipe   render_snapshot() with diff
 *
 * Splitting the work this way is the producer/consumer pattern from the pipe lab:
 * the sampler never prints, the parent never reads /proc.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <signal.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/wait.h>

#include "live.h"
#include "procfs.h"
#include "render.h"
#include "ioutil.h"
#include "workload.h"

#define ZOMBIE_SAMPLES 3   /* how many zombie samples to show before stopping */

void print_denied(int pid)
{
    fprintf(stderr, "vmv: permission denied reading /proc/%d/maps - the process "
            "belongs to another user. Try: sudo ./bin/vmv snapshot %d\n", pid, pid);
}

/*
 * The sampler child's whole life. Each message on the pipe is:
 *   struct snap_hdr, followed by hdr.nregions x struct region.
 * Never returns: it ends with exit(0), which also closes its write end.
 */
static void sampler_loop(int pid, int wfd, int interval, int count)
{
    struct snap_hdr hdr;
    struct region *regions;
    int sample;
    int zombie_samples = 0;

    regions = malloc(MAX_REGIONS * sizeof *regions);   /* [Lab 0] */
    if (regions == NULL) {
        perror("malloc");
        close(wfd);
        exit(1);
    }

    for (sample = 1; count == 0 || sample <= count; sample++) {
        take_snapshot(pid, &hdr, regions);
        hdr.sample_no = sample;

        /* [Lab 8] send one message: fixed-size header, then the regions */
        if (writen(wfd, &hdr, sizeof hdr) < 0 ||
            writen(wfd, regions, (size_t)hdr.nregions * sizeof *regions) < 0)
            break;   /* parent went away */

        if (hdr.status != SNAP_OK)
            break;   /* gone or denied: nothing more to watch */

        if (hdr.info.state == 'Z') {
            zombie_samples++;
            if (zombie_samples >= ZOMBIE_SAMPLES)
                break;   /* shown the zombie long enough */
        } else {
            zombie_samples = 0;
        }

        if (count == 0 || sample < count)
            sleep(interval);   /* [Lab 6] wait between samples */
    }

    close(wfd);    /* [Lab 8] closing the last write end = EOF for the parent */
    free(regions);
    exit(0);
}

/*
 * Parent side: read every message from the pipe and draw it.
 * demo == 1 also prints the process-tree line under each sample.
 * Returns the exit code: 0, 2 (no such process) or 3 (permission denied).
 */
static int show_stream(int rfd, int pid, int color, int demo,
                       pid_t target, pid_t sampler)
{
    struct snap_hdr cur_hdr;
    struct snap_hdr prev_hdr;
    struct region *cur;
    struct region *prev;
    int have_prev = 0;
    int samples_shown = 0;
    int rc = 0;
    ssize_t got;

    cur = malloc(MAX_REGIONS * sizeof *cur);     /* [Lab 0] */
    prev = malloc(MAX_REGIONS * sizeof *prev);   /* [Lab 0] */
    if (cur == NULL || prev == NULL) {
        perror("malloc");
        free(cur);
        free(prev);
        return 1;
    }

    for (;;) {
        got = readn(rfd, &cur_hdr, sizeof cur_hdr);   /* [Lab 8] */
        if (got == 0)
            break;   /* EOF: the sampler has finished and closed its write end */
        if (got < 0) {
            perror("vmv: reading from sampler pipe");
            break;
        }
        if (cur_hdr.nregions < 0 || cur_hdr.nregions > MAX_REGIONS) {
            fprintf(stderr, "vmv: bad message from sampler (nregions %d)\n",
                    cur_hdr.nregions);
            break;
        }
        if (readn(rfd, cur, (size_t)cur_hdr.nregions * sizeof *cur) < 0) {   /* [Lab 8] */
            perror("vmv: reading regions from sampler pipe");
            break;
        }

        if (cur_hdr.status == SNAP_GONE) {
            if (samples_shown == 0) {
                fprintf(stderr, "vmv: no such process: %d\n", pid);
                rc = 2;
            } else {
                printf("Process %d has exited and was reaped by its own parent, "
                       "so /proc/%d is gone.\n", pid, pid);
            }
            continue;   /* the sampler stops after this, so the next read is EOF */
        }
        if (cur_hdr.status == SNAP_DENIED) {
            print_denied(pid);
            rc = 3;
            continue;
        }

        render_snapshot(&cur_hdr, cur, have_prev ? &prev_hdr : NULL,
                        have_prev ? prev : NULL, color, 1);
        if (demo)
            /* [Lab 7] the process tree: parent -> two children */
            printf("Processes: vmv %d -> target %d, sampler %d (pipe: sampler -> vmv)\n",
                   getpid(), (int)target, (int)sampler);   /* [Lab 6] getpid */
        fflush(stdout);
        samples_shown++;

        /* remember this sample so the next one can be compared with it */
        memcpy(&prev_hdr, &cur_hdr, sizeof cur_hdr);
        memcpy(prev, cur, (size_t)cur_hdr.nregions * sizeof *cur);
        have_prev = 1;
    }

    free(cur);
    free(prev);
    return rc;
}

/*
 * Create the pipe and fork the sampler. On success returns the sampler's PID and
 * stores the read end in *rfd; the parent no longer holds the write end.
 */
static pid_t start_sampler(int pid, int interval, int count, int *rfd)
{
    int p[2];
    pid_t sampler;

    if (pipe(p) < 0) {   /* [Lab 8] p[0] = read end, p[1] = write end */
        perror("pipe");
        return -1;
    }

    fflush(stdout);   /* [Lab 6] fork copies unflushed stdio buffers - flush first */
    sampler = fork();   /* [Lab 6] */
    if (sampler < 0) {
        perror("fork");
        close(p[0]);
        close(p[1]);
        return -1;
    }
    if (sampler == 0) {
        close(p[0]);   /* [Lab 8] the sampler only writes: close the unused read end */
        sampler_loop(pid, p[1], interval, count);   /* never returns */
    }

    /*
     * [Lab 8] The parent only reads, so it closes the write end. If it kept a copy,
     * the pipe would still have a writer after the sampler exits, readn would never
     * return 0 and vmv would hang forever - the classic pipe-lab bug.
     */
    close(p[1]);
    *rfd = p[0];
    return sampler;
}

int run_live(int pid, int interval, int count, int color)
{
    int rfd;
    int rc;
    pid_t sampler;

    sampler = start_sampler(pid, interval, count, &rfd);
    if (sampler < 0)
        return 1;

    rc = show_stream(rfd, pid, color, 0, 0, sampler);

    close(rfd);                    /* [Lab 8] */
    waitpid(sampler, NULL, 0);     /* [Lab 6] reap the sampler: no zombie left behind */
    return rc;
}

int run_demo(int interval, int count, int color)
{
    pid_t target;
    pid_t sampler;
    pid_t w;
    int rfd;
    int st = 0;
    char path[64];
    struct proc_info info;

    /*
     * 1. Fork the TARGET first, BEFORE the pipe exists, so it never inherits a
     *    write end (otherwise the parent would never see EOF while it runs).
     */
    fflush(stdout);   /* [Lab 6] flush before fork */
    target = fork();  /* [Lab 6] */
    if (target < 0) {
        perror("fork");
        return 1;
    }
    if (target == 0) {
        run_workload(3, 0, 0);   /* silent: the parent owns the screen */
        exit(0);
    }

    /* 2. the sampler watches the target */
    sampler = start_sampler(target, interval, count, &rfd);
    if (sampler < 0) {
        kill(target, SIGTERM);       /* [Lab 6] do not leave the workload running */
        waitpid(target, NULL, 0);    /* [Lab 6] */
        return 1;
    }

    /* 3. draw every sample, then the process tree */
    show_stream(rfd, target, color, 1, target, sampler);
    close(rfd);   /* [Lab 8] */

    /*
     * 4. The stream has ended. Until now the target, once finished, stayed a
     *    ZOMBIE because we (its parent) had not waited for it yet.
     */
    w = waitpid(target, &st, WNOHANG);   /* [Lab 6] check without blocking */
    if (w == 0) {
        /* still running: the -n limit stopped the sampler first */
        kill(target, SIGTERM);           /* [Lab 6] ask it to terminate */
        waitpid(target, &st, 0);         /* [Lab 6] now wait for it for real */
    }

    /* 5. how did it end? */
    if (WIFEXITED(st))                   /* [Lab 6] */
        printf("Reaped target PID %d: exited with status %d\n", (int)target, WEXITSTATUS(st));
    else if (WIFSIGNALED(st))            /* [Lab 6] */
        printf("Reaped target PID %d: terminated by signal %d\n", (int)target, WTERMSIG(st));

    /* 6. after waitpid the process-table entry is really gone */
    if (read_proc_info(target, &info) < 0 && errno == ENOENT)
        printf("/proc/%d no longer exists - the process-table entry is gone.\n", (int)target);

    /* 7. clean up the phase file the workload left for us */
    snprintf(path, sizeof path, "/tmp/vmv_phase_%d.txt", (int)target);
    unlink(path);                  /* [Lab 5] */
    waitpid(sampler, NULL, 0);     /* [Lab 6] reap the sampler too */
    fflush(stdout);
    return 0;
}
