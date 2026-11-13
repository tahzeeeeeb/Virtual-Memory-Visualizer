/* render.h - draw a snapshot as a terminal diagram. */
#ifndef VMV_RENDER_H
#define VMV_RENDER_H

#include <stddef.h>
#include "procfs.h"

/* Human-readable size: "512 B", "4.0 KiB", "1.5 MiB", ... */
void format_size(unsigned long bytes, char *out, size_t cap);

/* Print the whole diagram to stdout. color == 0 means no escape codes at all. */
void render_snapshot(const struct snap_hdr *hdr, const struct region *r, int color);

#endif /* VMV_RENDER_H */
