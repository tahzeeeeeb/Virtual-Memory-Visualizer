/*
 * demo_main.c - bin/vmv_demo: run the demo workload on its own, so it can be
 * watched from another terminal with ./bin/vmv live <pid>.
 *
 * usage: vmv_demo [PHASE_SEC] [START_DELAY]   (defaults 3 and 0)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <unistd.h>

#include "workload.h"

static void usage(void)
{
    fprintf(stderr, "usage: vmv_demo [PHASE_SEC] [START_DELAY]\n");
    exit(1);
}

/* 1 if s is a non-empty string of at most 9 digits (no atoi overflow). */
static int is_digits(const char *s)
{
    size_t i;
    size_t len = strlen(s);

    if (len == 0 || len > 9)
        return 0;
    for (i = 0; i < len; i++)
        if (!isdigit((unsigned char)s[i]))
            return 0;
    return 1;
}

int main(int argc, char *argv[])
{
    int phase_sec = 3;
    int start_delay = 0;

    if (argc > 3)
        usage();
    if (argc >= 2) {
        if (!is_digits(argv[1]))
            usage();
        phase_sec = atoi(argv[1]);
    }
    if (argc == 3) {
        if (!is_digits(argv[2]))
            usage();
        start_delay = atoi(argv[2]);
    }

    /* [Lab 6] getpid: tell the user which process to watch */
    printf("vmv_demo PID %d - watch it with: ./bin/vmv live %d\n", getpid(), getpid());
    fflush(stdout);

    run_workload(phase_sec, start_delay, 1);
    exit(0);
}
