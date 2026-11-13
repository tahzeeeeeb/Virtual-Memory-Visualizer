/* workload.h - a program that changes its own memory in labelled phases. */
#ifndef VMV_WORKLOAD_H
#define VMV_WORKLOAD_H

/*
 * Run the 8 demo phases (0..7), sleeping phase_sec seconds after each one.
 * start_delay: seconds to wait before phase 0 (time to start a watcher).
 * standalone:  1 = also print each phase and thread to stdout (bin/vmv_demo),
 *              0 = silent, because `vmv demo` is drawing the screen.
 */
void run_workload(int phase_sec, int start_delay, int standalone);

#endif /* VMV_WORKLOAD_H */
