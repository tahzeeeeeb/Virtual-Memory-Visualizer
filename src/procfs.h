/* procfs.h - read a process's memory information from the /proc filesystem. */
#ifndef VMV_PROCFS_H
#define VMV_PROCFS_H

#include <sys/types.h>   /* ssize_t, size_t */

#define MAX_REGIONS 4096
#define PATH_LEN    256
#define PAGE_SIZE   4096UL            /* x86-64 base page size */
#define MAPS_BUF    (1024 * 1024)     /* 1 MiB is enough for any normal process */

enum snap_status { SNAP_OK = 0, SNAP_GONE = 1, SNAP_DENIED = 2 };

enum region_kind { K_CODE, K_RODATA, K_DATA, K_BSS, K_HEAP, K_ANON, K_SHLIB, K_FILE,
                   K_THREAD_STACK, K_STACK, K_GUARD, K_KERNEL, K_COUNT };

/* [Lab 0] one line of /proc/<pid>/maps = one virtual memory area (VMA) */
struct region {
    unsigned long start, end, offset;
    char perms[5];                    /* e.g. "r-xp" */
    char path[PATH_LEN];              /* "" for anonymous */
    int kind;                         /* enum region_kind */
};

/* [Lab 0] fields taken from /proc/<pid>/status and /proc/<pid>/stat */
struct proc_info {
    int pid, ppid, threads;
    char name[64];
    char state;                       /* R S D T Z ... or '?' */
    long vm_size_kb, vm_rss_kb, vm_data_kb, vm_stk_kb, vm_exe_kb, vm_lib_kb, vm_swap_kb; /* -1 = line absent */
    unsigned long minflt, majflt;
};

/* [Lab 0] everything one snapshot knows, except the region array itself */
struct snap_hdr {
    struct proc_info info;
    int status;                       /* enum snap_status */
    int sample_no;                    /* 0 for a one-shot snapshot */
    int nregions;
    int truncated;                    /* 1 if maps was cut at MAX_REGIONS or MAPS_BUF */
    char phase[96];                   /* contents of /tmp/vmv_phase_<pid>.txt, or "" */
};

ssize_t read_whole_file(const char *path, char *buf, size_t cap, int *truncated);
int     parse_maps(char *text, struct region *out, int max, int *truncated);
int     read_proc_info(int pid, struct proc_info *info);
void    take_snapshot(int pid, struct snap_hdr *hdr, struct region *regions);

#endif /* VMV_PROCFS_H */
