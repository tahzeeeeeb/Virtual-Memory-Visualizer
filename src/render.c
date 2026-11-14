/*
 * render.c - print a snapshot as a colour-coded diagram of the address space,
 * highest address at the top (stack) and lowest at the bottom (code), like the
 * textbook picture of a process in memory.
 *
 * In live mode the previous sample is passed in too, and every change is marked:
 *   '+' new region   '~' same start, different end (grew/shrank)   '-' removed
 */
#include <stdio.h>
#include <string.h>

#include "render.h"
#include "classify.h"

#define GRAY      "\033[90m"
#define GREEN     "\033[32m"
#define YELLOW    "\033[33m"
#define RED       "\033[31m"
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

/*
 * A size from /proc/<pid>/status (in kB), or "n/a" if that line was missing.
 * If has_prev, append the change since the last sample: "(+64.0 MiB)", "(+0 B)".
 */
static void format_kb(long kb, long prev_kb, int has_prev, char *out, size_t cap)
{
    char value[32];
    char delta[32];
    long diff;

    if (kb < 0) {
        snprintf(out, cap, "n/a");
        return;
    }
    format_size((unsigned long)kb * 1024UL, value, sizeof value);
    if (!has_prev || prev_kb < 0) {
        snprintf(out, cap, "%s", value);
        return;
    }
    diff = kb - prev_kb;
    if (diff < 0)
        format_size((unsigned long)(-diff) * 1024UL, delta, sizeof delta);
    else
        format_size((unsigned long)diff * 1024UL, delta, sizeof delta);
    snprintf(out, cap, "%s (%c%s)", value, diff < 0 ? '-' : '+', delta);
}

/* A counter such as page faults, plus "(+16384)" when there is a previous sample. */
static void format_count(unsigned long now, unsigned long before, int has_prev,
                         char *out, size_t cap)
{
    if (has_prev)
        snprintf(out, cap, "%lu (%+ld)", now, (long)(now - before));   /* [Lab 0] %+ld */
    else
        snprintf(out, cap, "%lu", now);
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

/*
 * Compare region r with the previous sample (simple loop, no sorting):
 * '+' no previous region had this start address -> new mapping
 * '~' same start but a different end            -> grew or shrank
 * ' ' unchanged, or there is no previous sample
 */
static char change_mark(const struct region *r, const struct region *prev, int nprev)
{
    int j;

    if (prev == NULL)
        return ' ';
    for (j = 0; j < nprev; j++)
        if (prev[j].start == r->start)
            return prev[j].end == r->end ? ' ' : '~';
    return '+';
}

/* Print one region line: mark, addresses, perms, size, pages, kind, bar, path. */
static void print_region(const struct region *r, char mark, int color)
{
    char size[32];
    char bar[BAR_MAX + 1];
    unsigned long bytes = r->end - r->start;
    int len = bar_length(bytes);
    const char *mark_color = "";

    if (color && mark == '+')
        mark_color = GREEN;
    else if (color && mark == '~')
        mark_color = YELLOW;

    format_size(bytes, size, sizeof size);
    memset(bar, '#', len);
    bar[len] = '\0';

    /* [Lab 0] %016lx = 16 hex digits with leading zeros, %-14s = left-aligned
     * in 14 columns, %10s / %8lu = right-aligned, so every row lines up. */
    printf("%s%c%s %016lx-%016lx %s %10s %8lu pg  %s%-14s %-30s%s %s\n",
           mark_color, mark, mark_color[0] ? COLOR_RESET : "",
           r->start, r->end, r->perms, size, bytes / PAGE_SIZE,
           color ? kind_color(r->kind) : "", kind_name(r->kind), bar,
           color ? COLOR_RESET : "",
           r->path[0] != '\0' ? r->path : "[anon]");
}

/* List previous regions whose start address no longer exists. */
static void print_removed(const struct region *r, int n,
                          const struct region *prev, int nprev, int color)
{
    char size[32];
    int i, j;
    int found;
    int printed = 0;

    for (j = 0; j < nprev; j++) {
        found = 0;
        for (i = 0; i < n; i++) {
            if (r[i].start == prev[j].start) {
                found = 1;
                break;
            }
        }
        if (found)
            continue;
        if (!printed)
            printf("\nRemoved since last sample:\n");
        printed = 1;
        format_size(prev[j].end - prev[j].start, size, sizeof size);
        printf("%s-%s %016lx-%016lx %s %10s  %-14s %s\n",
               color ? RED : "", color ? COLOR_RESET : "",
               prev[j].start, prev[j].end, prev[j].perms, size,
               kind_name(prev[j].kind),
               prev[j].path[0] != '\0' ? prev[j].path : "[anon]");
    }
}

void render_csv(const struct snap_hdr *hdr, const struct region *r)
{
    const struct proc_info *in = &hdr->info;
    const char *p;
    int i;

    /* One info line first, for tools that draw a live view (tools/vmv_gui.py).
     * Readers skip lines starting with '#'. phase is last because it may contain
     * any character; a newline in it is printed as a space to keep it one line.
     * vmsize_kb / vmrss_kb are -1 when status has no such line (zombie, kernel thread). */
    printf("# pid=%d;name=%s;state=%c;threads=%d;vmsize_kb=%ld;vmrss_kb=%ld;"
           "minflt=%lu;majflt=%lu;phase=",
           in->pid, in->name, in->state, in->threads, in->vm_size_kb, in->vm_rss_kb,
           in->minflt, in->majflt);
    for (p = hdr->phase; *p != '\0'; p++)
        printf("%c", (*p == '\n' || *p == '\r') ? ' ' : *p);
    printf("\n");

    printf("start,end,perms,kind,size_bytes,path\n");
    for (i = 0; i < hdr->nregions; i++) {
        printf("0x%016lx,0x%016lx,%s,%s,%lu,\"",
               r[i].start, r[i].end, r[i].perms, kind_name(r[i].kind),
               r[i].end - r[i].start);
        /* CSV rule: inside a quoted field a " is written as "" */
        for (p = r[i].path; *p != '\0'; p++) {
            if (*p == '"')
                printf("\"\"");
            else
                printf("%c", *p);
        }
        printf("\"\n");
    }
}

void render_snapshot(const struct snap_hdr *hdr, const struct region *r,
                     const struct snap_hdr *prev_hdr, const struct region *prev,
                     int color, int clear_screen)
{
    const struct proc_info *in = &hdr->info;
    const struct proc_info *pin = prev_hdr != NULL ? &prev_hdr->info : NULL;
    int has_prev = (pin != NULL);
    int nprev = (prev_hdr != NULL && prev != NULL) ? prev_hdr->nregions : 0;
    char s1[80], s2[80], s3[80], s4[80], s5[80], s6[80], s7[80];
    int count[K_COUNT];
    unsigned long bytes[K_COUNT];
    unsigned long sum = 0;
    int has_vsyscall = 0;
    int i, k;
    const char *note;

    /* ---- 0. start of a new sample ---- */
    if (clear_screen && color)
        printf("\033[H\033[2J");   /* cursor home + clear screen (terminal only) */
    else if (hdr->sample_no > 0)
        printf("===== sample %d =====\n", hdr->sample_no);   /* separator in -o files */

    /* ---- 1. header ---- */
    if (hdr->sample_no > 0)
        printf("Virtual Memory Visualizer - PID %d (%s)   sample %d\n",
               in->pid, in->name, hdr->sample_no);
    else
        printf("Virtual Memory Visualizer - PID %d (%s)\n", in->pid, in->name);

    if (has_prev)
        snprintf(s1, sizeof s1, "%d (%+d)", in->threads, in->threads - pin->threads);
    else
        snprintf(s1, sizeof s1, "%d", in->threads);
    printf("State: %c   PPid: %d   Threads: %s   Phase: %s\n",
           in->state, in->ppid, s1, hdr->phase[0] ? hdr->phase : "-");

    format_kb(in->vm_size_kb, has_prev ? pin->vm_size_kb : -1, has_prev, s1, sizeof s1);
    format_kb(in->vm_rss_kb,  has_prev ? pin->vm_rss_kb : -1, has_prev, s2, sizeof s2);
    format_kb(in->vm_data_kb, -1, 0, s3, sizeof s3);
    format_kb(in->vm_stk_kb,  -1, 0, s4, sizeof s4);
    format_kb(in->vm_exe_kb,  -1, 0, s5, sizeof s5);
    format_kb(in->vm_lib_kb,  -1, 0, s6, sizeof s6);
    format_kb(in->vm_swap_kb, -1, 0, s7, sizeof s7);
    printf("VmSize %s   VmRSS %s   VmData %s   VmStk %s   VmExe %s   VmLib %s   VmSwap %s\n",
           s1, s2, s3, s4, s5, s6, s7);

    format_count(in->minflt, has_prev ? pin->minflt : 0, has_prev, s1, sizeof s1);
    format_count(in->majflt, has_prev ? pin->majflt : 0, has_prev, s2, sizeof s2);
    printf("Page faults: minor %s   major %s   Regions: %d   Page size: %lu B\n",
           s1, s2, hdr->nregions, PAGE_SIZE);
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
        print_region(&r[i], change_mark(&r[i], prev, nprev), color);
    }
    if (r[0].start > 0)
        print_gap(0, r[0].start, " (low addresses, includes the NULL page)", color);

    if (prev != NULL)
        print_removed(r, hdr->nregions, prev, nprev, color);

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
    format_kb(in->vm_size_kb, -1, 0, s2, sizeof s2);
    printf("Sum of all regions: %s   VmSize: %s\n", s1, s2);
    if (has_vsyscall)
        printf("([vsyscall] is listed in maps but is not a real VMA, "
               "so VmSize does not count it)\n");
}
