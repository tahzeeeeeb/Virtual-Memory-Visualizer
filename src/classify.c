/*
 * classify.c - give every region from /proc/<pid>/maps a kind (CODE, HEAP, ...).
 *
 * The kernel only tells us address range, permissions and backing file. The kind
 * is worked out from those with the simple rules in classify_regions() below.
 */
#include <string.h>
#include <ctype.h>

#include "classify.h"

/* [Lab 0] read-only lookup tables indexed by enum region_kind */
static const char *const names[K_COUNT] = {
    "CODE", "RODATA", "DATA", "BSS", "HEAP", "ANON-MMAP", "SHARED-LIB", "FILE-MAP",
    "THREAD-STACK?", "STACK", "GUARD", "KERNEL-SPECIAL"
};

static const char *const colors[K_COUNT] = {
    "\033[31m",     /* CODE           red */
    "\033[35m",     /* RODATA         magenta */
    "\033[33m",     /* DATA           yellow */
    "\033[93m",     /* BSS            bright yellow */
    "\033[32m",     /* HEAP           green */
    "\033[36m",     /* ANON-MMAP      cyan */
    "\033[34m",     /* SHARED-LIB     blue */
    "\033[94m",     /* FILE-MAP       bright blue */
    "\033[95m",     /* THREAD-STACK?  bright magenta */
    "\033[1;91m",   /* STACK          bold bright red */
    "\033[90m",     /* GUARD          gray */
    "\033[37m"      /* KERNEL-SPECIAL white */
};

static const char *const meanings[K_COUNT] = {
    "executable's machine code (r-x)",
    "executable's read-only data: constants, string literals",
    "executable's initialised globals (rw-)",
    "zero-initialised globals, right after DATA",
    "brk heap: small malloc() calls grow it",
    "anonymous mmap: big malloc() (>= 128 KiB by default), malloc arenas",
    ".so libraries (libc, libgomp...), shared between processes",
    "other memory-mapped files (locale archive, caches)",
    "likely a pthread stack (anon rw- directly above a guard page)",
    "main thread stack, grows downward",
    "no-access pages (---p): guard pages or reserved address space",
    "[vdso]/[vvar]/[vsyscall]: kernel-provided pages for fast system calls"
};

const char *kind_name(int kind)
{
    return (kind >= 0 && kind < K_COUNT) ? names[kind] : "?";
}

const char *kind_color(int kind)
{
    return (kind >= 0 && kind < K_COUNT) ? colors[kind] : "";
}

const char *kind_meaning(int kind)
{
    return (kind >= 0 && kind < K_COUNT) ? meanings[kind] : "";
}

/*
 * 1 if path names a shared library: ".so" at the very end (system.so) or ".so."
 * followed by a digit (libc.so.6, ld-linux-x86-64.so.2). /etc/ld.so.cache has
 * ".so" in the middle of a word, so it is an ordinary mapped file.
 */
static int is_shared_lib(const char *path)
{
    const char *p = strstr(path, ".so");

    while (p != NULL) {
        if (p[3] == '\0' || (p[3] == '.' && isdigit((unsigned char)p[4])))
            return 1;
        p = strstr(p + 1, ".so");   /* look for a later ".so" */
    }
    return 0;
}

/*
 * Set r[i].kind for every region. maps is sorted by ascending address, so r[i-1]
 * is the region just below r[i] - some rules look at that neighbour.
 */
void classify_regions(struct region *r, int n)
{
    const char *exe = "";   /* path of the program's own executable file */
    int i;
    int anon;
    int touches_prev;

    /* The executable is the first file-backed region (mapped lowest by the loader). */
    for (i = 0; i < n; i++) {
        if (r[i].path[0] != '\0' && r[i].path[0] != '[') {
            exe = r[i].path;
            break;
        }
    }

    for (i = 0; i < n; i++) {
        anon = (r[i].path[0] == '\0');
        touches_prev = (i > 0 && r[i - 1].end == r[i].start);

        if (strcmp(r[i].path, "[heap]") == 0) {
            r[i].kind = K_HEAP;                                  /* rule 1 */
        } else if (strcmp(r[i].path, "[stack]") == 0) {
            r[i].kind = K_STACK;                                 /* rule 2 */
        } else if (r[i].path[0] == '[') {
            r[i].kind = K_KERNEL;                                /* rule 3: [vdso] ... */
        } else if (!anon && strcmp(r[i].path, exe) == 0) {       /* rule 4 */
            if (r[i].perms[2] == 'x')
                r[i].kind = K_CODE;
            else if (r[i].perms[1] == 'w')
                r[i].kind = K_DATA;
            else
                r[i].kind = K_RODATA;
        } else if (!anon && is_shared_lib(r[i].path)) {
            r[i].kind = K_SHLIB;                                 /* rule 5 */
        } else if (!anon) {
            r[i].kind = K_FILE;                                  /* rule 6 */
        } else if (strcmp(r[i].perms, "---p") == 0) {
            r[i].kind = K_GUARD;                                 /* rule 7 */
        } else if (strcmp(r[i].perms, "rw-p") == 0 && touches_prev &&
                   r[i - 1].kind == K_DATA) {
            r[i].kind = K_BSS;                                   /* rule 8 */
        } else if (strcmp(r[i].perms, "rw-p") == 0 && touches_prev &&
                   r[i - 1].path[0] == '\0' &&
                   strcmp(r[i - 1].perms, "---p") == 0 &&
                   r[i].end - r[i].start >= 64 * 1024UL) {
            /* rule 9: glibc puts a no-access guard page directly below each
             * pthread stack. This is a heuristic, hence the "?" in the label. */
            r[i].kind = K_THREAD_STACK;
        } else {
            r[i].kind = K_ANON;                                  /* rule 10 */
        }
    }
}
