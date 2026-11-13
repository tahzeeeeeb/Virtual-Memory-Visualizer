/* classify.h - decide what each memory region is (code, heap, stack, ...). */
#ifndef VMV_CLASSIFY_H
#define VMV_CLASSIFY_H

#include "procfs.h"

#define COLOR_RESET "\033[0m"

void        classify_regions(struct region *r, int n);
const char *kind_name(int kind);      /* short label, e.g. "HEAP" */
const char *kind_color(int kind);     /* ANSI colour escape code */
const char *kind_meaning(int kind);   /* one-line explanation for the legend */

#endif /* VMV_CLASSIFY_H */
