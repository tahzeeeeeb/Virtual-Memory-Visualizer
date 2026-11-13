/*
 * ioutil.c - "read exactly n bytes" / "write exactly n bytes" helpers.
 *
 * [Lab 8] A pipe read can return fewer bytes than requested when a message is
 * bigger than the pipe buffer (64 KiB on Linux): the kernel hands over whatever
 * is in the pipe right now. One snapshot can be over 1 MiB, so we must loop.
 */
#include <errno.h>
#include <unistd.h>

#include "ioutil.h"

ssize_t readn(int fd, void *buf, size_t n)
{
    char *p = buf;
    size_t got = 0;
    ssize_t r;

    while (got < n) {
        r = read(fd, p + got, n - got);   /* [Lab 5] may return less than asked */
        if (r < 0)
            return -1;
        if (r == 0) {                     /* EOF: every write end is closed */
            if (got == 0)
                return 0;                 /* clean end of the stream */
            errno = EIO;                  /* stream ended in the middle of a message */
            return -1;
        }
        got += (size_t)r;
    }
    return (ssize_t)n;
}

ssize_t writen(int fd, const void *buf, size_t n)
{
    const char *p = buf;
    size_t done = 0;
    ssize_t w;

    while (done < n) {
        w = write(fd, p + done, n - done);   /* [Lab 5] may also write less than asked */
        if (w < 0)
            return -1;
        done += (size_t)w;
    }
    return (ssize_t)n;
}
