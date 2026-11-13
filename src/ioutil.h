/* ioutil.h - read or write an exact number of bytes on a file descriptor. */
#ifndef VMV_IOUTIL_H
#define VMV_IOUTIL_H

#include <sys/types.h>   /* ssize_t, size_t */

/* Returns n, 0 if EOF came before ANY byte, -1 on error or EOF mid-message. */
ssize_t readn(int fd, void *buf, size_t n);

/* Returns n, or -1 on error. */
ssize_t writen(int fd, const void *buf, size_t n);

#endif /* VMV_IOUTIL_H */
