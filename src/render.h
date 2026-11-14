/* render.h - draw a snapshot as a terminal diagram. */
#ifndef VMV_RENDER_H
#define VMV_RENDER_H

#include <stddef.h>
#include "procfs.h"

/* Human-readable size: "512 B", "4.0 KiB", "1.5 MiB", ... */
void format_size(unsigned long bytes, char *out, size_t cap);

/*
 * Print the whole diagram to stdout.
 * prev_hdr / prev: the previous sample (NULL for none) - used to show deltas and
 *                  mark new (+), resized (~) and removed (-) regions.
 * color == 0 means no escape codes at all.
 * clear_screen: clear the terminal first (only when color is on, i.e. a terminal).
 */
void render_snapshot(const struct snap_hdr *hdr, const struct region *r,
                     const struct snap_hdr *prev_hdr, const struct region *prev,
                     int color, int clear_screen);

/*
 * Print the regions as CSV (ascending address order) for other tools: first one
 * "# pid=...;phase=..." info line, then start,end,perms,kind,size_bytes,path
 * (path always in double quotes).
 */
void render_csv(const struct snap_hdr *hdr, const struct region *r);

#endif /* VMV_RENDER_H */
