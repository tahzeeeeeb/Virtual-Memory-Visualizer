/* scan.h - read the status of many processes in parallel (OpenMP). */
#ifndef VMV_SCAN_H
#define VMV_SCAN_H

/*
 * Read PIDs from stdin, scan them all `repeat` times in parallel, print the
 * top 10 by VmRSS and the timing. Returns the exit code (0, or 1 if no PIDs).
 */
int run_scan(int repeat);

#endif /* VMV_SCAN_H */
