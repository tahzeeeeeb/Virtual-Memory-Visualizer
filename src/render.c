/*
 * render.c - print a snapshot as a colour-coded diagram of the address space,
 * highest address at the top (stack) and lowest at the bottom (code), like the
 * textbook picture of a process in memory.
 *
 * Two views:
 *   summary (default) - four boxes: STACK, MEMORY-MAPPED AREA, HEAP, PROGRAM;
 *                       fits 80 columns x 40 lines
 *   detail (--detail) - one row per line of /proc/<pid>/maps
 * In live mode the previous sample is passed in too, and changes are shown:
 *   summary: size change in each box title, "+ new" / "- gone" lines
 *   detail:  '+' new region  '~' same start, different end  '-' removed
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "render.h"
#include "classify.h"

#define BOLD      "\033[1m"
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

/* The detail view (--detail): one row per maps line, as in Part 1 and 2. */
static void render_detail(const struct snap_hdr *hdr, const struct region *r,
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

/* ======================================================================
 * Summary view (default): the address space as four boxes, like the
 * textbook diagram - STACK, MEMORY-MAPPED AREA, HEAP, PROGRAM - small
 * enough for 80 columns x 40 lines. The grouping is the same as GROUPS
 * in tools/vmv_gui.py, so the terminal and the GUI always agree.
 * ====================================================================== */

#define SCREEN      80   /* no summary line is wider than this */
#define BOX_IN      60   /* columns between the left and right side of a box */
#define DESC_COL    48   /* where the perms column starts in the STACK/HEAP box */
#define ROW_NAME    22   /* name column of a MEMORY-MAPPED AREA row */
#define ROW_SIZE    9    /* size column of a MEMORY-MAPPED AREA row */
#define TOP_FILES   4    /* files listed one by one before the "... N more" row */
#define MAX_CHANGES 4    /* lines for regions added/removed since the last sample */

enum group_id { G_STACK, G_MMAP, G_HEAP, G_PROGRAM, G_COUNT };

static const char *const group_title[G_COUNT] = {
    "STACK", "MEMORY-MAPPED AREA", "HEAP", "PROGRAM"
};
/* the kind whose terminal colour each box title uses */
static const int group_color_kind[G_COUNT] = { K_STACK, K_SHLIB, K_HEAP, K_CODE };

/* [Lab 0] one box: total size, lowest and highest address, number of regions */
struct group {
    unsigned long size, lo, hi;
    int count;
    const struct region *first;      /* lowest region of the group */
};

/* 0 = summary view (default), 1 = one row per maps line. Set once by main(). */
static int detail_view = 0;

void render_set_detail(int detail)
{
    detail_view = detail;
}

/* Which box a region belongs to; -1 for [vsyscall], which lies above user space. */
static int group_of(const struct region *r)
{
    if (r->start >= USER_TOP)
        return -1;
    switch (r->kind) {
    case K_STACK:
        return G_STACK;
    case K_HEAP:
        return G_HEAP;
    case K_CODE:
    case K_RODATA:
    case K_DATA:
    case K_BSS:
        return G_PROGRAM;
    default:
        return G_MMAP;      /* libraries, files, anonymous mmap, thread stacks, [vdso] */
    }
}

static void compute_groups(const struct region *r, int n, struct group g[G_COUNT])
{
    int i, k;

    memset(g, 0, G_COUNT * sizeof g[0]);
    for (i = 0; i < n; i++) {
        k = group_of(&r[i]);
        if (k < 0)
            continue;
        if (g[k].count == 0) {
            g[k].lo = r[i].start;        /* maps is ascending: the first one is the lowest */
            g[k].first = &r[i];
        }
        g[k].hi = r[i].end;              /* ... and the last one is the highest */
        g[k].size += r[i].end - r[i].start;
        g[k].count++;
    }
}

/* Screen columns of a UTF-8 string: count only the bytes that start a character,
 * because a box character such as "─" is 3 bytes but 1 column. */
static int text_cols(const char *s)
{
    int cols = 0;

    for (; *s != '\0'; s++)
        if (((unsigned char)*s & 0xC0) != 0x80)
            cols++;
    return cols;
}

/* Step over one UTF-8 character. */
static const char *next_char(const char *p)
{
    p++;
    while (((unsigned char)*p & 0xC0) == 0x80)
        p++;
    return p;
}

/*
 * Copy s into out, cut to at most max columns; a cut string gets "…".
 * keep_tail cuts at the front instead, so a path keeps its file name ("…/bin/vmv").
 */
static void fit_text(const char *s, int max, int keep_tail, char *out, size_t cap)
{
    const char *p = s;
    int cols = text_cols(s);
    int keep = max - 1;                  /* one column is left for "…" */
    size_t len;

    if (cols <= max) {
        snprintf(out, cap, "%s", s);
        return;
    }
    if (keep_tail) {
        while (cols > keep) {
            p = next_char(p);
            cols--;
        }
        snprintf(out, cap, "…%s", p);
        return;
    }
    while (keep > 0) {
        p = next_char(p);
        keep--;
    }
    len = (size_t)(p - s);
    if (len + 4 > cap)
        len = cap - 4;
    memcpy(out, s, len);
    memcpy(out + len, "…", 4);           /* 3 bytes of "…" and the '\0' */
}

static void put_repeat(const char *glyph, int n)
{
    while (n-- > 0)
        printf("%s", glyph);
}

/* Print one line cut to the screen width, in an optional colour. */
static void put_line(const char *text, const char *style, int color)
{
    char fitted[512];
    int styled = color && style != NULL;

    fit_text(text, SCREEN, 0, fitted, sizeof fitted);
    printf("%s%s%s\n", styled ? style : "", fitted, styled ? COLOR_RESET : "");
}

/* Summary sizes: whole KiB below 1 MiB ("132 KiB"), else one decimal ("1.2 MiB"). */
static void size_short(unsigned long bytes, char *out, size_t cap)
{
    if (bytes >= 1024 && bytes < 1024UL * 1024UL)
        snprintf(out, cap, "%lu KiB", bytes / 1024);
    else
        format_size(bytes, out, cap);
}

/* " (+1.3 MiB)" or " (-128.0 MiB)", or "" if nothing changed. */
static void delta_text(unsigned long now, unsigned long before, char *out, size_t cap)
{
    char s[32];

    if (now == before) {
        out[0] = '\0';
        return;
    }
    size_short(now > before ? now - before : before - now, s, sizeof s);
    snprintf(out, cap, " (%c%s)", now > before ? '+' : '-', s);
}

/* A status size in kB as text, or "n/a" when that line is missing. */
static void kb_text(long kb, char *out, size_t cap)
{
    if (kb < 0)
        snprintf(out, cap, "n/a");
    else
        size_short((unsigned long)kb * 1024UL, out, cap);
}

/* "+128.0 MiB", "-64.0 MiB" or "+0 B"; "" when either value is missing. */
static void kb_change(long now, long before, char *out, size_t cap)
{
    char s[32];

    if (now < 0 || before < 0) {
        out[0] = '\0';
        return;
    }
    size_short((unsigned long)(now > before ? now - before : before - now) * 1024UL,
               s, sizeof s);
    snprintf(out, cap, "%c%s", now >= before ? '+' : '-', s);
}

static const char *state_word(char state)
{
    switch (state) {
    case 'R': return "running";
    case 'S': return "sleeping";
    case 'D': return "waiting for disk";
    case 'T': return "stopped";
    case 't': return "stopped by debugger";
    case 'Z': return "zombie";
    case 'X': return "dead";
    case 'I': return "idle";
    default:  return "unknown state";
    }
}

/* Header: who, state, threads; then Reserved / In RAM / page faults (+ deltas). */
static void summary_header(const struct snap_hdr *hdr, const struct snap_hdr *prev_hdr,
                           int color)
{
    const struct proc_info *in = &hdr->info;
    const struct proc_info *pin = prev_hdr != NULL ? &prev_hdr->info : NULL;
    char line[256], size[32], rss[32], d1[48], d2[48], pct[16];

    if (pin != NULL)
        snprintf(d1, sizeof d1, " (%+d)", in->threads - pin->threads);
    else
        d1[0] = '\0';
    snprintf(line, sizeof line, " VIRTUAL MEMORY of PID %d (%s)  ·  %s  ·  %d thread%s%s",
             in->pid, in->name, state_word(in->state), in->threads,
             in->threads == 1 ? "" : "s", d1);
    put_line(line, NULL, color);

    kb_text(in->vm_size_kb, size, sizeof size);
    kb_text(in->vm_rss_kb, rss, sizeof rss);
    pct[0] = '\0';
    if (in->vm_size_kb > 0 && in->vm_rss_kb >= 0)       /* rounded percentage */
        snprintf(pct, sizeof pct, "%ld%%",
                 (in->vm_rss_kb * 100 + in->vm_size_kb / 2) / in->vm_size_kb);

    if (hdr->sample_no == 0) {                          /* one-shot snapshot: 2 lines */
        snprintf(line, sizeof line, " Reserved %s   In RAM %s%s%s%s   Page faults: %lu minor, "
                 "%lu major", size, rss, pct[0] ? " (" : "", pct, pct[0] ? ")" : "",
                 in->minflt, in->majflt);
        put_line(line, NULL, color);
        return;
    }

    /* live: changes since the previous sample, then faults and the sample number */
    d1[0] = d2[0] = '\0';
    if (pin != NULL) {
        kb_change(in->vm_size_kb, pin->vm_size_kb, d1, sizeof d1);
        kb_change(in->vm_rss_kb, pin->vm_rss_kb, d2, sizeof d2);
    }
    snprintf(line, sizeof line, " Reserved %s%s%s%s   In RAM %s%s%s%s%s%s",
             size, d1[0] ? " (" : "", d1, d1[0] ? ")" : "",
             rss, (pct[0] || d2[0]) ? " (" : "", pct, (pct[0] && d2[0]) ? ", " : "",
             d2, (pct[0] || d2[0]) ? ")" : "");
    put_line(line, NULL, color);
    if (pin != NULL)
        snprintf(line, sizeof line, " Page faults: %lu minor (%+ld), %lu major (%+ld)   "
                 "sample %d", in->minflt, (long)(in->minflt - pin->minflt), in->majflt,
                 (long)(in->majflt - pin->majflt), hdr->sample_no);
    else
        snprintf(line, sizeof line, " Page faults: %lu minor, %lu major   sample %d",
                 in->minflt, in->majflt, hdr->sample_no);
    put_line(line, NULL, color);
}

/* " Phase: <text>" in bold; broken at a space onto a second line if it is too long. */
static void print_phase(const char *phase, int color)
{
    char line[256], part[256];
    int cut;

    snprintf(line, sizeof line, " Phase: %s", phase);
    if (text_cols(line) <= SCREEN) {
        put_line(line, BOLD, color);
        return;
    }
    cut = SCREEN;                        /* bytes >= columns, so 80 bytes always fit */
    while (cut > 8 && line[cut] != ' ')
        cut--;
    if (cut <= 8) {                      /* no space: cut at a character boundary */
        cut = SCREEN;
        while (cut > 0 && ((unsigned char)line[cut] & 0xC0) == 0x80)
            cut--;
    }
    memcpy(part, line, (size_t)cut);
    part[cut] = '\0';
    put_line(part, BOLD, color);
    snprintf(part, sizeof part, "        %s", line + cut + (line[cut] == ' '));
    put_line(part, BOLD, color);
}

/* " ┌─ TITLE ──────── SIZE ─┐ addr" with the title in its kind's colour */
static void box_top(const char *title, const char *size, int kind, unsigned long addr,
                    int color)
{
    char t[128];
    int fill;

    fit_text(title, BOX_IN - 8 - text_cols(size), 0, t, sizeof t);
    fill = BOX_IN - 6 - text_cols(t) - text_cols(size);
    printf(" ┌─ %s%s%s ", color ? kind_color(kind) : "", t, color ? COLOR_RESET : "");
    put_repeat("─", fill);
    printf(" %s ─┐ %lx\n", size, addr);   /* [Lab 0] %lx: hex without leading zeros */
}

static void box_line(const char *text)
{
    char t[256];

    fit_text(text, BOX_IN - 2, 0, t, sizeof t);
    printf(" │ %s", t);
    put_repeat(" ", BOX_IN - 2 - text_cols(t));
    printf(" │\n");
}

static void box_bottom(unsigned long addr)
{
    printf(" └");
    put_repeat("─", BOX_IN);
    printf("┘ %lx\n", addr);
}

/* "description ............ rw-" inside the STACK or HEAP box */
static void box_desc(const char *desc, const char *perms)
{
    char line[160];

    /* [Lab 0] %.3s prints only the first 3 characters: "rw-p" -> "rw-" */
    snprintf(line, sizeof line, "%s%*s%.3s", desc, DESC_COL - text_cols(desc), "", perms);
    box_line(line);
}

/* "name                     size   detail" inside the MEMORY-MAPPED AREA box */
static void mmap_row(const char *name, const char *size, const char *detail)
{
    char nm[128], line[256];

    fit_text(name, ROW_NAME, 0, nm, sizeof nm);
    snprintf(line, sizeof line, "%s%*s%*s   %s", nm, ROW_NAME - text_cols(nm), "",
             ROW_SIZE, size, detail);
    box_line(line);
}

static void gap_line(unsigned long bytes, int color)
{
    char s[32];

    format_size(bytes, s, sizeof s);
    printf("%s      ⋮ %s unused%s\n", color ? GRAY : "", s, color ? COLOR_RESET : "");
}

/* [Lab 0] one row of the MEMORY-MAPPED AREA box: one file, all its segments added up */
struct file_row {
    const char *path;
    unsigned long size, code;            /* code = the r-x segments */
    int is_lib, shown;
};

static int find_row(const struct file_row *rows, int nrows, const char *path)
{
    int k;

    for (k = 0; k < nrows; k++)
        if (strcmp(rows[k].path, path) == 0)
            return k;
    return -1;
}

/*
 * The body of the MEMORY-MAPPED AREA box: one row per file with its segments merged,
 * the TOP_FILES biggest first, the rest as one row, then thread stacks, anonymous
 * mmap, guard/reserved and kernel pages - each only when present.
 */
static void mmap_rows(const struct region *r, int n)
{
    struct file_row *rows;
    int nrows = 0, shown = 0, i, j, k, best;
    int nstacks = 0, mixed = 0, nanon = 0, nguard = 0;
    int has_vdso = 0, has_vvar = 0, other_kernel = 0;
    unsigned long sz, stacks = 0, stack_each = 0, anon = 0, guard = 0, kernel = 0, rest = 0;
    char size[32], code[32], each[32], detail[96], name[64];
    const char *base;

    rows = malloc((size_t)n * sizeof *rows);   /* [Lab 0] at most one row per region */
    if (rows == NULL) {
        box_line("(out of memory)");
        return;
    }

    for (i = 0; i < n; i++) {
        if (group_of(&r[i]) != G_MMAP)
            continue;
        sz = r[i].end - r[i].start;
        if (r[i].kind == K_SHLIB || r[i].kind == K_FILE) {
            k = find_row(rows, nrows, r[i].path);
            if (k < 0) {                       /* first segment of this file */
                k = nrows++;
                rows[k].path = r[i].path;
                rows[k].size = rows[k].code = 0;
                rows[k].is_lib = rows[k].shown = 0;
            }
            rows[k].size += sz;
            if (r[i].perms[2] == 'x')
                rows[k].code += sz;
            if (r[i].kind == K_SHLIB)
                rows[k].is_lib = 1;
        } else if (r[i].kind == K_THREAD_STACK) {
            if (nstacks > 0 && sz != stack_each)
                mixed = 1;
            stack_each = sz;
            stacks += sz;
            nstacks++;
        } else if (r[i].kind == K_GUARD) {
            /* the guard page directly below a thread stack belongs to that stack */
            if (i + 1 < n && r[i + 1].kind == K_THREAD_STACK && r[i].end == r[i + 1].start) {
                stacks += sz;
            } else {
                guard += sz;
                nguard++;
            }
        } else if (r[i].kind == K_KERNEL) {
            kernel += sz;
            if (strcmp(r[i].path, "[vdso]") == 0)
                has_vdso = 1;
            else if (strncmp(r[i].path, "[vvar", 5) == 0)   /* [vvar], [vvar_vclock] */
                has_vvar = 1;
            else
                other_kernel = 1;
        } else {
            /* anonymous rw-p directly above a library's rw-p segment = that library's bss */
            k = -1;
            if (i > 0 && r[i - 1].kind == K_SHLIB && r[i - 1].end == r[i].start &&
                strcmp(r[i - 1].perms, "rw-p") == 0 && strcmp(r[i].perms, "rw-p") == 0)
                k = find_row(rows, nrows, r[i - 1].path);
            if (k >= 0) {
                rows[k].size += sz;
            } else {
                anon += sz;
                nanon++;
            }
        }
    }

    /* the biggest files first: pick the largest unshown row, TOP_FILES times (no qsort) */
    for (j = 0; j < TOP_FILES && j < nrows; j++) {
        best = -1;
        for (k = 0; k < nrows; k++)
            if (!rows[k].shown && (best < 0 || rows[k].size > rows[best].size))
                best = k;
        rows[best].shown = 1;
        shown++;
        base = strrchr(rows[best].path, '/');
        base = base != NULL ? base + 1 : rows[best].path;
        size_short(rows[best].size, size, sizeof size);
        if (!rows[best].is_lib) {
            snprintf(detail, sizeof detail, "file");
        } else if (rows[best].code > 0) {
            size_short(rows[best].code, code, sizeof code);
            snprintf(detail, sizeof detail, "code %s", code);
        } else {
            snprintf(detail, sizeof detail, "library");
        }
        mmap_row(base, size, detail);
    }
    if (nrows > shown) {
        for (k = 0; k < nrows; k++)
            if (!rows[k].shown)
                rest += rows[k].size;
        snprintf(name, sizeof name, "… %d more libs/files", nrows - shown);
        size_short(rest, size, sizeof size);
        mmap_row(name, size, "(--detail lists all)");
    }

    if (nstacks > 0) {
        size_short(stacks, size, sizeof size);
        size_short(stack_each, each, sizeof each);
        if (mixed)
            snprintf(detail, sizeof detail, "%d stacks (+ guards)", nstacks);
        else
            snprintf(detail, sizeof detail, "%d × %s (+ guards)", nstacks, each);
        mmap_row("thread stacks", size, detail);
    }
    if (nanon > 0) {
        size_short(anon, size, sizeof size);
        snprintf(detail, sizeof detail, "%d region%s", nanon, nanon == 1 ? "" : "s");
        mmap_row("anonymous mmap", size, detail);
    }
    if (nguard > 0) {
        size_short(guard, size, sizeof size);
        snprintf(detail, sizeof detail, "%d region%s", nguard, nguard == 1 ? "" : "s");
        mmap_row("guard/reserved", size, detail);
    }
    if (kernel > 0) {
        size_short(kernel, size, sizeof size);
        snprintf(detail, sizeof detail, "%s%s%s", has_vdso ? "[vdso] " : "",
                 has_vvar ? "[vvar] " : "", other_kernel ? "and others" : "");
        mmap_row("kernel pages", size, detail);
    }
    free(rows);                          /* [Lab 0] */
}

/* The body of the PROGRAM box: "code 960 KiB · rodata 172 KiB · data 24 KiB · bss 80 KiB" */
static void program_body(const struct region *r, int n)
{
    static const char *const label[4] = { "code", "rodata", "data", "bss" };
    unsigned long part[4] = { 0, 0, 0, 0 };
    char line[160], s[32];
    size_t len = 0;
    int i, k;

    for (i = 0; i < n; i++) {
        if (r[i].kind == K_CODE)
            part[0] += r[i].end - r[i].start;
        else if (r[i].kind == K_RODATA)
            part[1] += r[i].end - r[i].start;
        else if (r[i].kind == K_DATA)
            part[2] += r[i].end - r[i].start;
        else if (r[i].kind == K_BSS)
            part[3] += r[i].end - r[i].start;
    }
    line[0] = '\0';
    for (k = 0; k < 4; k++) {
        if (part[k] == 0)
            continue;
        size_short(part[k], s, sizeof s);
        snprintf(line + len, sizeof line - len, "%s%s %s", len > 0 ? " · " : "", label[k], s);
        len = strlen(line);
    }
    box_line(line);
}

static int has_start(const struct region *r, int n, unsigned long start)
{
    int i;

    for (i = 0; i < n; i++)
        if (r[i].start == start)
            return 1;
    return 0;
}

/* Up to MAX_CHANGES lines: regions added ("+ new") or removed ("- gone"), biggest first. */
static void print_changes(const struct region *r, int n, const struct region *prev,
                          int nprev, int color)
{
    struct change { const struct region *reg; int is_new; } *ch;
    int nch = 0, shown, i, k, best, is_new;
    char size[32], text[256], fitted[256];
    const char *name;
    unsigned long sz, best_sz;

    ch = malloc((size_t)(n + nprev) * sizeof *ch);   /* [Lab 0] */
    if (ch == NULL)
        return;
    for (i = 0; i < n; i++)
        if (!has_start(prev, nprev, r[i].start)) {
            ch[nch].reg = &r[i];
            ch[nch++].is_new = 1;
        }
    for (i = 0; i < nprev; i++)
        if (!has_start(r, n, prev[i].start)) {
            ch[nch].reg = &prev[i];
            ch[nch++].is_new = 0;
        }

    shown = nch <= MAX_CHANGES ? nch : MAX_CHANGES - 1;   /* keep a line for "... more" */
    for (k = 0; k < shown; k++) {
        best = -1;
        best_sz = 0;
        for (i = 0; i < nch; i++) {
            if (ch[i].reg == NULL)
                continue;
            sz = ch[i].reg->end - ch[i].reg->start;
            if (best < 0 || sz > best_sz) {
                best = i;
                best_sz = sz;
            }
        }
        is_new = ch[best].is_new;
        format_size(best_sz, size, sizeof size);
        name = strrchr(ch[best].reg->path, '/');
        name = name != NULL ? name + 1 : ch[best].reg->path;
        snprintf(text, sizeof text, "%s %s%s%.100s", kind_name(ch[best].reg->kind), size,
                 name[0] != '\0' ? "  " : "", name);
        fit_text(text, SCREEN - 9, 0, fitted, sizeof fitted);
        printf(" %s%s%s %s\n", color ? (is_new ? GREEN : RED) : "",
               is_new ? "+ new  " : "- gone ", color ? COLOR_RESET : "", fitted);
        ch[best].reg = NULL;             /* printed */
    }
    if (nch > shown)
        printf(" … %d more changes (--detail shows every region)\n", nch - shown);
    free(ch);
}

static void render_summary(const struct snap_hdr *hdr, const struct region *r,
                           const struct snap_hdr *prev_hdr, const struct region *prev,
                           int color, int clear_screen)
{
    const struct proc_info *in = &hdr->info;
    int n = hdr->nregions;
    int nprev = (prev_hdr != NULL && prev != NULL) ? prev_hdr->nregions : 0;
    struct group g[G_COUNT], pg[G_COUNT];
    int order[G_COUNT], used[G_COUNT] = { 0, 0, 0, 0 };
    int nbox = 0, i, j, k, best;
    unsigned long vsys = 0;
    char line[256], size[32], delta[48], sizes[96], title[128], path[96];

    /* ---- start of a new sample (same rule as the detail view) ---- */
    if (clear_screen && color)
        printf("\033[H\033[2J");
    else if (hdr->sample_no > 0)
        printf("===== sample %d =====\n", hdr->sample_no);

    summary_header(hdr, prev_hdr, color);
    if (hdr->phase[0] != '\0')
        print_phase(hdr->phase, color);
    if (hdr->truncated)
        printf(" WARNING: maps truncated at %d regions\n", MAX_REGIONS);
    printf("\n");

    if (n == 0) {                        /* same messages as the detail view */
        if (in->state == 'Z')
            printf(" ZOMBIE: this process has exited. Its address space is already released\n"
                   " (maps is empty), but its process-table entry stays until its parent\n"
                   " calls wait()/waitpid().\n");
        else
            printf(" No user-space address space (kernel thread).\n");
        return;
    }

    compute_groups(r, n, g);
    compute_groups(prev, nprev, pg);

    /* boxes ordered by their highest address: pick the highest one 4 times (no qsort) */
    for (j = 0; j < G_COUNT; j++) {
        best = -1;
        for (k = 0; k < G_COUNT; k++)
            if (g[k].count > 0 && !used[k] && (best < 0 || g[k].hi > g[best].hi))
                best = k;
        if (best < 0)
            break;
        used[best] = 1;
        order[nbox++] = best;
    }

    for (i = 0; i < n; i++)
        if (r[i].start >= USER_TOP)
            vsys += r[i].end - r[i].start;
    if (vsys > 0) {
        size_short(vsys, size, sizeof size);
        printf(" %-26sabove the kernel boundary: [vsyscall] %s\n", "HIGH ADDRESSES", size);
    } else {
        printf(" HIGH ADDRESSES\n");
    }

    for (j = 0; j < nbox; j++) {
        k = order[j];
        if (j > 0 && g[order[j - 1]].lo > g[k].hi)      /* hole between two boxes */
            gap_line(g[order[j - 1]].lo - g[k].hi, color);
        size_short(g[k].size, size, sizeof size);
        delta[0] = '\0';
        if (prev != NULL)
            delta_text(g[k].size, pg[k].size, delta, sizeof delta);
        snprintf(sizes, sizeof sizes, "%s%s", size, delta);
        if (k == G_PROGRAM) {            /* keep the end of the path: the file name */
            fit_text(g[k].first->path, BOX_IN - 16 - text_cols(sizes), 1, path, sizeof path);
            snprintf(title, sizeof title, "PROGRAM %s", path);
        } else {
            snprintf(title, sizeof title, "%s", group_title[k]);
        }
        box_top(title, sizes, group_color_kind[k], g[k].hi, color);
        if (k == G_STACK)
            box_desc("main thread stack · grows down", g[k].first->perms);
        else if (k == G_HEAP)
            box_desc("malloc() memory · grows up via brk", g[k].first->perms);
        else if (k == G_MMAP)
            mmap_rows(r, n);
        else
            program_body(r, n);
        box_bottom(g[k].lo);
    }
    if (nbox > 0) {
        format_size(g[order[nbox - 1]].lo, size, sizeof size);
        printf(" %-26s%s unused below (includes the NULL page)\n", "LOW ADDRESSES", size);
    }

    if (prev != NULL)
        print_changes(r, n, prev, nprev, color);
    printf("\n");
    snprintf(line, sizeof line, " %d region%s · sizes are virtual (reserved) · "
             "--detail shows every region", n, n == 1 ? "" : "s");
    put_line(line, NULL, color);
}

void render_snapshot(const struct snap_hdr *hdr, const struct region *r,
                     const struct snap_hdr *prev_hdr, const struct region *prev,
                     int color, int clear_screen)
{
    if (detail_view)
        render_detail(hdr, r, prev_hdr, prev, color, clear_screen);
    else
        render_summary(hdr, r, prev_hdr, prev, color, clear_screen);
}
