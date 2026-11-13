/*
 * scan.c - system-wide scan: read /proc/<pid>/status for every PID in parallel.
 *
 * The PIDs come from a shell pipeline on stdin, for example
 *     ls /proc | grep '^[0-9][0-9]*$' | ./bin/vmv scan
 * [Lab 1] ls and grep do the "list the directory and keep the numbers" part,
 *         so the C code needs no opendir/readdir.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <omp.h>

#include "scan.h"
#include "procfs.h"
#include "render.h"

#define MAX_PIDS 65536
#define TOP_N    10

/* [Lab 0] one result slot per PID; thread i writes only slot i */
struct scan_result {
    int valid;               /* 0 = the process exited before we could read it */
    struct proc_info info;
};

int run_scan(int repeat)
{
    int *pids;
    struct scan_result *results;
    char *picked;
    int npids = 0;
    int pid;
    int r, k, j, best;
    long total_rss_kb = 0;
    int total_threads = 0;
    int n_valid = 0;
    int n_kernel = 0;
    double t_start, t_total;
    char size_s[32], rss_s[32];

    /* ---- read the PID list ---- */
    pids = malloc(MAX_PIDS * sizeof *pids);   /* [Lab 0] */
    if (pids == NULL) {
        perror("malloc");
        return 1;
    }
    /* [Lab 8] stdin is the read end of the shell pipe `ls /proc | grep ... |` */
    while (npids < MAX_PIDS && scanf("%d", &pid) == 1)
        pids[npids++] = pid;
    if (npids == 0) {
        printf("vmv scan: no PIDs on stdin\n");
        free(pids);
        return 1;
    }

    results = malloc((size_t)npids * sizeof *results);   /* [Lab 0] */
    picked = malloc((size_t)npids);                      /* [Lab 0] */
    if (results == NULL || picked == NULL) {
        perror("malloc");
        free(pids);
        free(results);
        free(picked);
        return 1;
    }

    /* ---- the parallel scan, repeated only to make the time measurable ---- */
    t_start = omp_get_wtime();   /* [Lab 10] wall-clock time in seconds */
    for (r = 0; r < repeat; r++) {
        total_rss_kb = 0;
        total_threads = 0;
        n_valid = 0;
        n_kernel = 0;

        /*
         * [Lab 10] parallel for: the iterations are split across the threads.
         * Each iteration writes ONLY results[i], so no two threads ever write the
         * same memory and no mutex is needed. The four counters are combined with
         * reduction(+:...): every thread gets a private copy, and OpenMP adds the
         * copies together at the end.
         * [Lab 10] schedule(dynamic): a busy process with many threads costs more to
         * read than an idle one, so threads take the next PID when they are free
         * instead of getting a fixed equal-sized block (static).
         */
        #pragma omp parallel for schedule(dynamic) reduction(+:total_rss_kb, total_threads, n_valid, n_kernel)
        for (int i = 0; i < npids; i++) {
            if (read_proc_info(pids[i], &results[i].info) < 0) {
                results[i].valid = 0;   /* exited after `ls` listed it */
                continue;
            }
            results[i].valid = 1;
            n_valid++;
            total_threads += results[i].info.threads;
            if (results[i].info.vm_rss_kb < 0)
                n_kernel++;   /* no VmRSS line: a kernel thread (or a zombie) */
            else
                total_rss_kb += results[i].info.vm_rss_kb;
        }
    }
    t_total = omp_get_wtime() - t_start;   /* [Lab 10] */

    /* ---- header ---- */
    printf("System scan: %d PIDs from stdin, %d readable, %d exited before they could be read\n",
           npids, n_valid, npids - n_valid);
    printf("OpenMP threads: %d   repeat: %d   total: %.4f s   per scan: %.3f ms\n",
           omp_get_max_threads(), repeat, t_total, t_total * 1000.0 / repeat);   /* [Lab 10] */

    /* ---- top 10 by VmRSS: pick the biggest not-yet-picked one, 10 times (no qsort) ---- */
    memset(picked, 0, (size_t)npids);
    printf("Top 10 by VmRSS:\n");
    printf("%7s  %-16s  %-2s %5s  %11s  %11s\n", "PID", "NAME", "ST", "THR", "VmSize", "VmRSS");
    for (k = 0; k < TOP_N; k++) {
        best = -1;
        for (j = 0; j < npids; j++) {
            if (!results[j].valid || picked[j] || results[j].info.vm_rss_kb < 0)
                continue;
            if (best < 0 || results[j].info.vm_rss_kb > results[best].info.vm_rss_kb)
                best = j;
        }
        if (best < 0)
            break;   /* fewer than 10 user processes */
        picked[best] = 1;
        format_size((unsigned long)results[best].info.vm_size_kb * 1024UL, size_s, sizeof size_s);
        format_size((unsigned long)results[best].info.vm_rss_kb * 1024UL, rss_s, sizeof rss_s);
        /* [Lab 0] widths keep the columns aligned; %-16.16s also cuts long names */
        printf("%7d  %-16.16s  %-2c %5d  %11s  %11s\n",
               results[best].info.pid, results[best].info.name, results[best].info.state,
               results[best].info.threads, size_s, rss_s);
    }

    /* ---- totals and machine-readable lines (read by the Part 4 menu) ---- */
    format_size((unsigned long)total_rss_kb * 1024UL, rss_s, sizeof rss_s);
    printf("Totals (last scan): VmRSS sum %s (overcounts: shared library pages are counted "
           "once per process), threads %d, kernel threads %d\n",
           rss_s, total_threads, n_kernel);
    printf("SCAN_TIME_SEC=%.4f\n", t_total);
    printf("SCAN_THREADS=%d\n", omp_get_max_threads());   /* [Lab 10] */

    free(pids);
    free(results);
    free(picked);
    return 0;
}
