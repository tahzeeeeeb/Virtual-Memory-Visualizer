/*
 * server.c - `vmv serve`: an iterative Unix domain socket server.
 *
 * A client (bin/vmv_client) connects, sends a PID as decimal text, and gets back
 * one summary line. Same structure as the Lab 8 server example:
 *     socket -> unlink old path -> bind -> listen -> accept loop
 * One client at a time: no fork per client, no threads.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>

#include "server.h"
#include "procfs.h"
#include "classify.h"
#include "render.h"
#include "ioutil.h"

/* A /proc/<pid>/status size in kB as text, or "n/a" if the line was missing. */
static void kb_text(long kb, char *out, size_t cap)
{
    if (kb < 0)
        snprintf(out, cap, "n/a");
    else
        format_size((unsigned long)kb * 1024UL, out, cap);
}

/* Total size of all regions of one kind as text, or "none" if there are none. */
static void kind_size_text(const struct region *r, int n, int kind, char *out, size_t cap)
{
    unsigned long total = 0;
    int found = 0;
    int i;

    /* add up every region of this kind: a forked child can have two [heap] rows */
    for (i = 0; i < n; i++) {
        if (r[i].kind == kind) {
            total += r[i].end - r[i].start;
            found = 1;
        }
    }
    if (found)
        format_size(total, out, cap);
    else
        snprintf(out, cap, "none");
}

/* 1 if s is 1..9 digits. */
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

/* Build the one-line reply for request text req. Returns 1 for OK, 0 for ERROR. */
static int build_reply(const char *req, char *reply, size_t cap,
                       struct snap_hdr *hdr, struct region *regions)
{
    int pid;
    char vmsize[32], vmrss[32], heap[32], stack[32];

    if (!is_digits(req)) {
        snprintf(reply, cap, "ERROR: bad request\n");
        return 0;
    }
    pid = atoi(req);

    take_snapshot(pid, hdr, regions);
    if (hdr->status == SNAP_GONE) {
        snprintf(reply, cap, "ERROR %d: no such process\n", pid);
        return 0;
    }
    if (hdr->status == SNAP_DENIED) {
        snprintf(reply, cap, "ERROR %d: permission denied (owned by another user)\n", pid);
        return 0;
    }

    kb_text(hdr->info.vm_size_kb, vmsize, sizeof vmsize);
    kb_text(hdr->info.vm_rss_kb, vmrss, sizeof vmrss);
    kind_size_text(regions, hdr->nregions, K_HEAP, heap, sizeof heap);
    kind_size_text(regions, hdr->nregions, K_STACK, stack, sizeof stack);
    snprintf(reply, cap,
             "PID %d (%s) State=%c Threads=%d VmSize=%s VmRSS=%s Regions=%d Heap=%s Stack=%s\n",
             pid, hdr->info.name, hdr->info.state, hdr->info.threads,
             vmsize, vmrss, hdr->nregions, heap, stack);
    return 1;
}

int run_server(void)
{
    int sfd, cfd;
    struct sockaddr_un addr;
    char req[32];
    char reply[512];
    ssize_t n;
    struct snap_hdr hdr;
    struct region *regions;
    int ok;

    regions = malloc(MAX_REGIONS * sizeof *regions);   /* [Lab 0] reused for every request */
    if (regions == NULL) {
        perror("malloc");
        return 1;
    }

    /* [Lab 8] AF_UNIX = local socket with a file-system name; SOCK_STREAM = byte stream */
    sfd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sfd < 0) {
        perror("socket");
        free(regions);
        return 1;
    }

    /* [Lab 8] remove a socket file left behind by a previous run (e.g. after Ctrl+C),
     * otherwise bind() fails with "Address already in use" */
    unlink(SOCK_PATH);

    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCK_PATH, sizeof addr.sun_path - 1);

    /* [Lab 8] bind gives the socket its name: this creates /tmp/vmv.sock */
    if (bind(sfd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        perror("bind");
        close(sfd);
        free(regions);
        return 1;
    }
    /* [Lab 8] listen: accept connections, queue up to 5 waiting clients */
    if (listen(sfd, 5) < 0) {
        perror("listen");
        close(sfd);
        free(regions);
        return 1;
    }

    printf("Server: listening on %s (Ctrl+C to stop)\n", SOCK_PATH);
    fflush(stdout);   /* the log may be a file: make each line appear at once */

    for (;;) {
        /* [Lab 8] accept blocks until a client connects, then returns a NEW fd
         * just for that client; sfd keeps listening */
        cfd = accept(sfd, NULL, NULL);
        if (cfd < 0) {
            perror("accept");
            continue;
        }

        n = read(cfd, req, sizeof req - 1);   /* [Lab 5] up to 31 bytes of PID text */
        if (n < 0)
            n = 0;
        req[n] = '\0';
        while (n > 0 && (req[n - 1] == '\n' || req[n - 1] == '\r'))
            req[--n] = '\0';   /* accept "1234\n" too */

        ok = build_reply(req, reply, sizeof reply, &hdr, regions);
        if (writen(cfd, reply, strlen(reply)) < 0)   /* [Lab 8] send the reply */
            perror("write");
        close(cfd);   /* [Lab 8] done with this client: closing tells it "end of reply" */

        printf("Server: request for PID %s -> %s\n", req, ok ? "OK" : "ERROR");
        fflush(stdout);
    }
}
