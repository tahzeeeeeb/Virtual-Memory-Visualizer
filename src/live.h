/* live.h - watch a process change over time (fork + pipe). */
#ifndef VMV_LIVE_H
#define VMV_LIVE_H

/*
 * Watch process pid: a forked sampler child takes a snapshot every interval
 * seconds and sends it through a pipe; this process draws it.
 * count == 0 means "until the process ends". Returns the exit code (0, 2 or 3).
 */
int run_live(int pid, int interval, int count, int color);

/* Fork the demo workload as a child and watch it live, including its zombie state. */
int run_demo(int interval, int count, int color);

/* The "permission denied" message shared by snapshot and live mode. */
void print_denied(int pid);

#endif /* VMV_LIVE_H */
