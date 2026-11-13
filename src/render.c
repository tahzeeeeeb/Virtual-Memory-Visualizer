/*
 * render.c - print a snapshot as a colour-coded diagram of the address space,
 * highest address at the top (stack) and lowest at the bottom (code), like the
 * textbook picture of a process in memory.
 */
#include <stdio.h>
#include <string.h>

#include "render.h"
#include "classify.h"

#define GRAY      "\033[90m"
#define BAR_MAX   30
/* First address above user space on x86-64 (47-bit user addresses). */
#define USER_TOP  0x0000800000000000UL

void format_size(unsigned long bytes, char *out, size_t cap)
{
    const char *units[] = { "KiB", "MiB", "GiB", "TiB", "PiB", "EiB" };
    double value;
    int u = 0;

    if (bytes < 1024) {
        snprintf(out, cap, "%lu B", bytes);
        return;
    }
    value = bytes / 1024.0;
    while (value >= 1024.0 && u < 5) {
        value /= 1024.0;
        u++;
    }
    snprintf(out, cap, "%.1f %s", value, units[u]);   /* [Lab 0] one decimal place */
}

/* Size from /proc/<pid>/status (in kB), or "n/a" if that line was missing. */
static void format_kb(long kb, char *out, size_t cap)
{
    if (kb < 0)
        snprintf(out, cap, "n/a");
    else
        format_size((unsigned long)kb * 1024UL, out, cap);
}

/*
 * Bar length = floor(log2(size)) - 11, clamped to 1..30.
 * Log scale because sizes range from 4 KiB (one page = 2^12 -> 1 '#') to GiB;
 * a linear bar would make every small region invisible next to a big one.
 * floor(log2) is found with an integer shift loop, so no math.h is needed.
 */
static int bar_length(unsigned long size)
{
    int log2 = 0;
    int len;

    while (size > 1) {
        size >>= 1;
        log2++;
    }
    len = log2 - 11;
    if (len < 1)
        len = 1;
    if (len > BAR_MAX)
        len = BAR_MAX;
    return len;
}

/* Print one "~~ size unmapped ~~" line for the hole [low, high). */
static void print_gap(unsigned long low, unsigned long high, const char *note, int color)
{
    char size[32];

    format_size(high - low, size, sizeof size);
    /* 37 spaces line the text up under the perms column */
    printf("%37s%s~~ %s unmapped ~~%s%s\n", "",
           color ? GRAY : "", size, note, color ? COLOR_RESET : "");
}

/* Print one region line: addresses, perms, size, pages, kind, bar, path. */
static void print_region(const struct region *r, int color)
{
    char size[32];
    char bar[BAR_MAX + 1];
    unsigned long bytes = r->end - r->start;
    int len = bar_length(bytes);

    format_size(bytes, size, sizeof size);
    memset(bar, '#', len);
    bar[len] = '\0';

    /* [Lab 0] %016lx = 16 hex digits with leading zeros, %-14s = left-aligned
     * in 14 columns, %10s / %8lu = right-aligned, so every row lines up. */
    printf("  %016lx-%016lx %s %10s %8lu pg  %s%-14s %-30s%s %s\n",
           r->start, r->end, r->perms, size, bytes / PAGE_SIZE,
           color ? kind_color(r->kind) : "", kind_name(r->kind), bar,
           color ? COLOR_RESET : "",
           r->path[0] != '\0' ? r->path : "[anon]");
}

void render_snapshot(const struct snap_hdr *hdr, const struct region *r, int color)
{
    const struct proc_info *in = &hdr->info;
    char s1[32], s2[32], s3[32], s4[32], s5[32], s6[32], s7[32];
    int count[K_COUNT];
    unsigned long bytes[K_COUNT];
    unsigned long sum = 0;
    int has_vsyscall = 0;
    int i, k;
    const char *note;

    /* ---- 1. header ---- */
    printf("Virtual Memory Visualizer - PID %d (%s)\n", in->pid, in->name);
    printf("State: %c   PPid: %d   Threads: %d   Phase: %s\n",
           in->state, in->ppid, in->threads, hdr->phase[0] ? hdr->phase : "-");
    format_kb(in->vm_size_kb, s1, sizeof s1);
    format_kb(in->vm_rss_kb,  s2, sizeof s2);
    format_kb(in->vm_data_kb, s3, sizeof s3);
    format_kb(in->vm_stk_kb,  s4, sizeof s4);
    format_kb(in->vm_exe_kb,  s5, sizeof s5);
    format_kb(in->vm_lib_kb,  s6, sizeof s6);
    format_kb(in->vm_swap_kb, s7, sizeof s7);
    printf("VmSize %s   VmRSS %s   VmData %s   VmStk %s   VmExe %s   VmLib %s   VmSwap %s\n",
           s1, s2, s3, s4, s5, s6, s7);
    printf("Page faults: minor %lu   major %lu   Regions: %d   Page size: %lu B\n",
           in->minflt, in->majflt, hdr->nregions, PAGE_SIZE);
    if (hdr->truncated)
        printf("WARNING: maps truncated at %d regions\n", MAX_REGIONS);
    printf("\n");

    /* ---- 2. no address space at all ---- */
    if (hdr->nregions == 0) {
        if (in->state == 'Z')
            printf("ZOMBIE: this process has exited. Its address space is already "
                   "released (maps is empty), but its process-table entry stays "
                   "until its parent calls wait()/waitpid().\n");
        else
            printf("No user-space address space (kernel thread).\n");
        return;
    }

    /* ---- 3 + 4. regions from highest to lowest address, with the gaps ----
     * maps is already sorted ascending, so walking i backwards needs no sort. */
    for (i = hdr->nregions - 1; i >= 0; i--) {
        if (i < hdr->nregions - 1 && r[i].end < r[i + 1].start) {
            /* r[i] is the lower region, r[i + 1] the upper one */
            note = "";
            if (r[i].end <= USER_TOP && USER_TOP < r[i + 1].start)
                note = " (user/kernel boundary: user space ends at "
                       "0x00007fffffffffff = 128 TiB)";
            print_gap(r[i].end, r[i + 1].start, note, color);
        }
        print_region(&r[i], color);
    }
    if (r[0].start > 0)
        print_gap(0, r[0].start, " (low addresses, includes the NULL page)", color);

    /* count regions and bytes per kind for the legend and the totals */
    for (k = 0; k < K_COUNT; k++) {
        count[k] = 0;
        bytes[k] = 0;
    }
    for (i = 0; i < hdr->nregions; i++) {
        k = r[i].kind;
        if (k < 0 || k >= K_COUNT)
            continue;
        count[k]++;
        bytes[k] += r[i].end - r[i].start;
        sum += r[i].end - r[i].start;
        if (strcmp(r[i].path, "[vsyscall]") == 0)
            has_vsyscall = 1;
    }

    /* ---- 5. legend: only the kinds that appear ---- */
    printf("\nLegend:\n");
    for (k = 0; k < K_COUNT; k++) {
        if (count[k] == 0)
            continue;
        printf("  %s%-14s%s %s\n", color ? kind_color(k) : "", kind_name(k),
               color ? COLOR_RESET : "", kind_meaning(k));
    }
    printf("perms: r=read w=write x=execute p=private (copy-on-write) s=shared - "
           "the same rwx idea as ls -l (Lab 2), applied to memory pages\n");

    /* ---- 6. totals by kind ---- */
    printf("\nTotals by kind:\n");
    for (k = 0; k < K_COUNT; k++) {
        if (count[k] == 0)
            continue;
        format_size(bytes[k], s1, sizeof s1);
        printf("%-14s %4d regions %10s\n", kind_name(k), count[k], s1);
    }
    format_size(sum, s1, sizeof s1);
    format_kb(in->vm_size_kb, s2, sizeof s2);
    printf("Sum of all regions: %s   VmSize: %s\n", s1, s2);
    if (has_vsyscall)
        printf("([vsyscall] is listed in maps but is not a real VMA, "
               "so VmSize does not count it)\n");
}
