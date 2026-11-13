/*
 * client.c - bin/vmv_client: ask a running `vmv serve` about one PID.
 *
 *   usage: vmv_client <pid>
 *
 * Same structure as the Lab 8 client example: socket -> connect -> write -> read.
 * Exit codes: 0 reply starts with "PID", 3 reply starts with "ERROR",
 *             1 usage error, 2 could not connect.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>

#include "server.h"   /* only for SOCK_PATH */

int main(int argc, char *argv[])
{
    int sfd;
    struct sockaddr_un addr;
    char reply[512];
    size_t got = 0;
    ssize_t n;

    if (argc != 2) {
        fprintf(stderr, "usage: vmv_client <pid>\n");
        exit(1);
    }

    sfd = socket(AF_UNIX, SOCK_STREAM, 0);   /* [Lab 8] */
    if (sfd < 0) {
        perror("socket");
        exit(2);
    }

    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCK_PATH, sizeof addr.sun_path - 1);

    /* [Lab 8] connect to the server's socket file */
    if (connect(sfd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        perror("connect");
        fprintf(stderr, "Is './bin/vmv serve' running?\n");
        close(sfd);
        exit(2);
    }

    /* [Lab 8] send the PID text; the server checks that it is a number */
    if (write(sfd, argv[1], strlen(argv[1])) < 0) {
        perror("write");
        close(sfd);
        exit(2);
    }

    /* [Lab 8] read until the server closes its end (read returns 0) */
    while (got < sizeof reply - 1) {
        n = read(sfd, reply + got, sizeof reply - 1 - got);
        if (n <= 0)
            break;
        got += (size_t)n;
    }
    reply[got] = '\0';
    close(sfd);

    printf("%s", reply);
    if (strncmp(reply, "PID", 3) == 0)
        exit(0);
    if (strncmp(reply, "ERROR", 5) == 0)
        exit(3);
    exit(2);   /* empty or unexpected reply */
}
