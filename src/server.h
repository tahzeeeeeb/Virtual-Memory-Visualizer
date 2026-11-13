/* server.h - Unix domain socket server that answers "summary of PID X". */
#ifndef VMV_SERVER_H
#define VMV_SERVER_H

#define SOCK_PATH "/tmp/vmv.sock"   /* shared with src/client.c */

/* Serve requests forever. Returns 1 only if the socket could not be set up. */
int run_server(void);

#endif /* VMV_SERVER_H */
