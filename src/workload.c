/*
 * workload.c - the demo workload: it changes its own address space step by step
 * and writes the current step ("phase") to /tmp/vmv_phase_<pid>.txt, which
 * take_snapshot() shows in the header so the audience knows what just happened.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/syscall.h>

#include "workload.h"

#define SMALL_BLOCKS 20
#define SMALL_SIZE   (64 * 1024)                /* below 128 KiB: comes from brk heap */
#define BIG_SIZE     (128UL * 1024 * 1024)      /* above 128 KiB: malloc uses mmap */
#define TOUCH_SIZE   (64UL * 1024 * 1024)
#define TOUCH_STEP   4096UL                     /* one write per 4 KiB page */
#define NTHREADS     4

/*
 * Settings the threads need. They are written once in run_workload() BEFORE any
 * thread is created and only read afterwards, so no mutex is needed.
 */
static int g_phase_sec;
static int g_standalone;

/* Record the current phase in /tmp/vmv_phase_<pid>.txt (and print it if standalone). */
static void set_phase(const char *text)
{
    char path[64];
    char line[160];
    int fd;
    int len;

    snprintf(path, sizeof path, "/tmp/vmv_phase_%d.txt", getpid());   /* [Lab 6] own PID */
    len = snprintf(line, sizeof line, "%s\n", text);

    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);   /* [Lab 5] create/overwrite */
    if (fd >= 0) {
        if (write(fd, line, (size_t)len) < 0)               /* [Lab 5] one write per phase */
            perror(path);
        close(fd);                                          /* [Lab 5] */
    } else {
        perror(path);
    }

    if (g_standalone) {
        printf("[vmv_demo PID %d] phase %s\n", getpid(), text);
        fflush(stdout);
    }
}

/* Thread body: announce itself, then just sleep so its stack stays visible. */
static void *worker(void *arg)
{
    int id = *(int *)arg;   /* [Lab 9] read our own slot of ids[], never a shared i */

    if (g_standalone) {
        /* [Lab 9] every thread has its own kernel thread ID (TID), but shares the PID */
        printf("thread %d TID %ld\n", id, syscall(SYS_gettid));
        fflush(stdout);
    }
    sleep(g_phase_sec * 2);
    return NULL;
}

void run_workload(int phase_sec, int start_delay, int standalone)
{
    char *small[SMALL_BLOCKS];
    volatile char *big;   /* volatile: the compiler must really do every page write */
    int ids[NTHREADS];
    pthread_t t[NTHREADS];
    int created = 0;
    unsigned long off;
    char path[64];
    int i;

    g_phase_sec = phase_sec;
    g_standalone = standalone;

    /* ---- phase 0: nothing allocated yet ---- */
    if (start_delay > 0)
        sleep(start_delay);
    set_phase("0: baseline");
    sleep(phase_sec);

    /* ---- phase 1: small blocks come from the brk heap ---- */
    for (i = 0; i < SMALL_BLOCKS; i++) {
        small[i] = malloc(SMALL_SIZE);   /* [Lab 0] never freed: keeps [heap] visible */
        if (small[i] != NULL)
            memset(small[i], 1, SMALL_SIZE);   /* touch it so the pages become resident */
    }
    set_phase("1: 20 small mallocs (64 KiB each) -> [heap] grows via brk");
    sleep(phase_sec);

    /* ---- phase 2: one big block gets its own mmap region ---- */
    big = malloc(BIG_SIZE);   /* [Lab 0] malloc decides to mmap it; we never call mmap */
    if (big == NULL) {
        perror("malloc 128 MiB");
        return;
    }
    set_phase("2: malloc(128 MiB), untouched -> new ANON-MMAP region, VmRSS barely changes (demand paging)");
    sleep(phase_sec);

    /* ---- phase 3: touching pages makes the kernel allocate them (page faults) ---- */
    for (off = 0; off < TOUCH_SIZE; off += TOUCH_STEP)
        big[off] = 1;
    set_phase("3: touched 64 MiB -> VmRSS +64 MiB, minor page faults jump");
    sleep(phase_sec);

    /* ---- phase 4: every thread gets its own stack plus a guard page ---- */
    for (i = 0; i < NTHREADS; i++) {
        ids[i] = i + 1;
        /* [Lab 9] Fix A for the loop-variable bug: pass &ids[i], a slot that never
         * changes. Passing &i would let every thread read i after the loop moved on. */
        if (pthread_create(&t[i], NULL, worker, &ids[i]) != 0) {
            fprintf(stderr, "pthread_create failed for thread %d\n", i + 1);
            break;
        }
        created++;
    }
    set_phase("4: 4 pthreads running -> 4 THREAD-STACK? regions + guard pages, Threads: 5");
    sleep(phase_sec);

    /* ---- phase 5: wait for the threads ---- */
    for (i = 0; i < created; i++)
        pthread_join(t[i], NULL);   /* [Lab 9] wait for each thread to finish */
    set_phase("5: threads joined -> their stacks may stay mapped (glibc caches thread stacks for reuse)");
    sleep(phase_sec);

    /* ---- phase 6: freeing the big block gives the region back ---- */
    free((void *)big);   /* [Lab 0] free of an mmap'd chunk -> glibc calls munmap */
    set_phase("6: free(128 MiB) -> region unmapped (munmap), VmSize drops");
    sleep(phase_sec);

    /* ---- phase 7: done ---- */
    set_phase("7: exiting");
    if (standalone) {
        /* Nobody else will clean up after a standalone run. In `vmv demo` the
         * parent removes the file after it has shown the zombie. */
        snprintf(path, sizeof path, "/tmp/vmv_phase_%d.txt", getpid());
        unlink(path);   /* [Lab 5] delete the phase file */
    }
}
