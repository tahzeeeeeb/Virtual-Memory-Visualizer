#!/usr/bin/env python3
"""vmv_gui.py - live matplotlib window showing one process's virtual address space.

Display layer only: every number comes from `./bin/vmv csv <pid>`, read through a pipe.
Python never reads /proc itself; the C tool does all the OS work.

How the window draws:
  - a sampler thread runs `bin/vmv csv` once a second and hands each parsed snapshot to
    the window through a queue, so the window never waits for a subprocess;
  - every artist (card, block, label, line) is created once and afterwards only moved,
    faded or re-labelled; everything lives on one canvas measured in pixels at 100 dpi;
  - a 30 fps timer moves each value along a critically damped spring towards its
    target, and only the animated artists are redrawn over a saved background (blit);
  - only the cards whose moving artists changed since the last frame are redrawn, and in
    the window text and rounded boxes are painted from pixel caches (TextBitmaps,
    BoxPixels) instead of matplotlib's slow general path. Saved PNGs draw normally.

usage:
  python3 tools/vmv_gui.py <pid>                 live window, animated at 30 fps
  python3 tools/vmv_gui.py --demo                start bin/vmv_demo and watch it to the end
  python3 tools/vmv_gui.py <pid> --no-anim       live window, values jump (reduced motion)
  python3 tools/vmv_gui.py <pid> --png FILE      one settled frame, no window, then exit
  python3 tools/vmv_gui.py --demo --frames DIR   no window, every sample as frame_NNN.png
  python3 tools/vmv_gui.py --bench               time 150 animated frames offscreen
  --theme light|dark   colours (light is the default)
  --scale X            multiply every size, e.g. 1.25 for a projector

keys in the window: Space pause/resume sampling, F fullscreen, O auto focus on/off,
R restart the demo (--demo only), Q or Esc quit. Click a block to pin the focus on it,
click the empty column to unpin.

Focus view: after every sample the block or row whose size changed most (64 KiB or more)
is drawn big, with what changed; the rest shrink but stay visible. 8 s without a change
and the column eases back to the overview.
"""
import argparse
import collections
import csv
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VMV = os.path.join(ROOT, "bin", "vmv")
VMV_DEMO = os.path.join(ROOT, "bin", "vmv_demo")
BACKEND_HINT = ("Install a GUI backend: sudo pacman -S python-pyqt6   "
                "(Ubuntu: sudo apt install python3-pyqt5)")
NON_GUI_BACKENDS = {"agg", "pdf", "ps", "svg", "pgf", "cairo", "template"}
USER_TOP = 0x0000800000000000          # first address above user space on x86-64
HISTORY = 60                           # samples shown in the chart
ZOMBIE_SAMPLES = 3                     # zombie frames shown before the demo is reaped

SAMPLE_PERIOD = 1.0                    # seconds between two `bin/vmv csv` calls
FRAME_MS = 33                          # render timer: about 30 frames a second
RESPONSE = 0.4                         # spring response time in seconds
OMEGA = 2 * math.pi / RESPONSE         # spring stiffness; damping ratio 1.0 = no overshoot
DT_MAX = 0.05                          # a late frame never advances the springs more than this
SETTLED = 0.005                        # within 0.5 % of its target a spring counts as settled

# Same colour families as the terminal colours in src/classify.c, light enough for black text.
COLORS = {"CODE": "#e06666", "RODATA": "#c27ba0", "DATA": "#d4b000", "BSS": "#ffe866",
          "HEAP": "#6aa84f", "ANON-MMAP": "#76d7d7", "SHARED-LIB": "#6d8fd8",
          "FILE-MAP": "#9fc5f8", "THREAD-STACK?": "#e69be6", "STACK": "#ff4d4d",
          "GUARD": "#b0b0b0", "KERNEL-SPECIAL": "#eeeeee"}

# The four groups, top (high addresses) to bottom (low addresses), like the textbook
# picture of a process. Thread stacks, guard pages and [vdso]/[vvar] are mmap'd, so they
# belong to the memory-mapped area. [vsyscall] lies above user space and is not drawn.
GROUPS = [("STACK", {"STACK"}),
          ("MEMORY-MAPPED AREA", {"SHARED-LIB", "FILE-MAP", "ANON-MMAP", "THREAD-STACK?",
                                  "GUARD", "KERNEL-SPECIAL"}),
          ("HEAP", {"HEAP"}),
          ("PROGRAM", {"CODE", "RODATA", "DATA", "BSS"})]
GROUP_NOTE = {"STACK": "main thread stack · grows down",
              "HEAP": "malloc() memory · grows up via brk"}
# Which edge stays put when a block changes size: the stack grows down (its top edge
# stays), the heap grows up (its bottom edge stays); everything else scales about its centre.
ANCHOR = {"STACK": "top", "HEAP": "bottom"}
STATE_NAMES = {"R": "running", "S": "sleeping", "D": "disk wait", "T": "stopped",
               "t": "traced", "Z": "zombie", "X": "dead", "I": "idle"}
INFO_RE = re.compile(r"^# pid=(\d+);name=(.*?);state=(.);threads=(-?\d+);vmsize_kb=(-?\d+);"
                     r"vmrss_kb=(-?\d+);minflt=(\d+);majflt=(\d+);phase=(.*)$")

# ---- presentation -----------------------------------------------------------------
UNIT = 100.0                    # layout unit: one pixel at 100 dpi (the window is 16 x 9 in)
PT = UNIT / 72.0                # layout units per typographic point
WINDOW_INCHES = (16, 9)         # opens at 1600 x 900
FONT_FAMILIES = ("Inter", "Cantarell", "Noto Sans", "DejaVu Sans")   # first one found wins
SIZES = {"title": 20, "phase": 18, "hero": 28, "label": 12, "caption": 10,
         "focus": 12 * 1.4}             # points; "focus": the name of the focused block
DARK_TEXT = "#1D1D1F"
THEMES = {
    "light": {"bg": "#F5F5F7", "card": "#FFFFFF", "border": "#E5E5EA", "text": "#1D1D1F",
              "secondary": "#6E6E73", "fill": "#F2F2F7", "shadow": 0.10},
    "dark": {"bg": "#1C1C1E", "card": "#2C2C2E", "border": "#3A3A3C", "text": "#F5F5F7",
             "secondary": "#98989D", "fill": "#3A3A3C", "shadow": 0.35},
}
# One colour per kind of memory (the legend order).
KIND_COLORS = {"stack": "#FF9F0A", "libraries": "#0A84FF", "files": "#64D2FF",
               "anonymous": "#5E5CE6", "thread stacks": "#BF5AF2", "heap": "#30D158",
               "program": "#FF375F", "kernel": "#8E8E93"}
STATE_COLORS = {"zombie": "#FF9F0A", "reaped": "#30D158", "error": "#FF3B30"}
RAM_COLOR = "#0A84FF"            # chart and bar: what is really in RAM
WARM_CHARS = ("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
              ".,:;%+-−·≈→()[]/_…")             # rasterised before the first frame
PILL_SECONDS = 2.5               # a "+128.0 MiB" pill fades out after this
CROSSFADE = 0.2                  # phase banner crossfade in seconds
PULSE_SECONDS = 0.8              # the "live" dot pulses once per sample
CHART_SECONDS = 60               # the chart shows the last minute
PHASES = 7                       # bin/vmv_demo phases are numbered 0..7

# ---- focus view: the region that changed becomes the big block ------------------------
ANON_ROW_MIN = 1024 * 1024       # anonymous regions this big get a row of their own
FOCUS_MIN_CHANGE = 64 * 1024     # smaller size changes never move the focus
FOCUS_SHARE = 0.30               # the focused block takes this share of the column height
FOCUS_RESPONSE = 0.35            # spring response of the column (damping 1.0, no overshoot)
FOCUS_HOLD = 8.0                 # seconds without a change before the overview returns
GHOST_SECONDS = 1.5              # a freed region stays as a fading block this long
ROW_MIN = 22                     # px: the smallest block while another one is focused
RAM_PILL_MIN_KB = 1024           # VmRSS changes this big pulse the In RAM number


# ======================================================================================
# Data: run bin/vmv and turn its CSV into groups (unchanged from the first GUI version)
# ======================================================================================

def human(n):
    """Same text as format_size() in src/render.c: 512 B, 4.0 KiB, 1.5 MiB ..."""
    if n < 1024:
        return f"{n} B"
    value, units, u = n / 1024.0, ["KiB", "MiB", "GiB", "TiB", "PiB", "EiB"], 0
    while value >= 1024.0 and u < len(units) - 1:
        value, u = value / 1024.0, u + 1
    return f"{value:.1f} {units[u]}"


def read_sample(pid):
    """Run `bin/vmv csv <pid>`. Returns (exit code, info dict or None, regions, stderr)."""
    try:
        res = subprocess.run([VMV, "csv", str(pid)], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return -1, None, [], "bin/vmv csv timed out"
    if res.returncode != 0:
        return res.returncode, None, [], res.stderr.strip()
    lines = res.stdout.splitlines()
    m = INFO_RE.match(lines[0]) if lines else None
    if m is None:
        return -1, None, [], "bin/vmv csv printed no '# pid=...' info line"
    keys = ["pid", "name", "state", "threads", "vmsize_kb", "vmrss_kb", "minflt", "majflt", "phase"]
    info = dict(zip(keys, m.groups()))
    for k in ("pid", "threads", "vmsize_kb", "vmrss_kb", "minflt", "majflt"):
        info[k] = int(info[k])
    regions = []
    for row in csv.DictReader(line for line in lines if not line.startswith("#")):
        start = int(row["start"], 16)
        if start >= USER_TOP:
            continue                           # [vsyscall]: above user space, not drawn
        regions.append({"start": start, "end": int(row["end"], 16), "kind": row["kind"],
                        "size": int(row["size_bytes"]), "path": row["path"]})
    return 0, info, regions, ""


def mmap_subblocks(regions):
    """Split the memory-mapped area into rows: the top 4 files, every anonymous region of
    1 MiB or more on a row of its own, the small anonymous ones together, the thread
    stacks bin/vmv recognised, then "… N more". A big anonymous region is keyed by its
    start address, so its row keeps its identity from one sample to the next."""
    files = collections.defaultdict(lambda: {"size": 0, "hi": 0, "lo": USER_TOP, "kind": "FILE-MAP"})
    small = {"key": "anon-small", "size": 0, "hi": 0, "lo": USER_TOP, "count": 0,
             "color": COLORS["ANON-MMAP"], "cat": "anonymous"}
    big = []
    kernel = {"size": 0, "hi": 0, "lo": USER_TOP, "count": 0}
    stacks = {"key": "threads", "size": 0, "hi": 0, "lo": USER_TOP, "count": 0, "each": 0,
              "color": COLORS["THREAD-STACK?"], "cat": "thread stacks"}

    def add(entry, r):
        entry["size"] += r["size"]
        entry["hi"] = max(entry["hi"], r["end"])
        entry["lo"] = min(entry["lo"], r["start"])

    for r in regions:
        if r["kind"] == "THREAD-STACK?":       # pthread stacks get their own sub-block
            add(stacks, r)
            stacks["count"] += 1
            stacks["each"] = r["size"]
        elif r["kind"] in ("SHARED-LIB", "FILE-MAP"):
            f = files[os.path.basename(r["path"])]
            add(f, r)
            f["kind"] = r["kind"]
        elif r["kind"] == "KERNEL-SPECIAL":    # [vdso], [vvar]
            add(kernel, r)
            kernel["count"] += 1
        elif r["size"] >= ANON_ROW_MIN:        # ANON-MMAP: a big malloc, a thread stack ...
            big.append({"key": f"anon:{r['start']:x}", "label": "anonymous mmap",
                        "size": r["size"], "hi": r["end"], "lo": r["start"],
                        "color": COLORS["ANON-MMAP"], "cat": "anonymous"})
        else:                                  # small ANON-MMAP, GUARD
            add(small, r)
            small["count"] += 1
    ranked = sorted(files.items(), key=lambda kv: kv[1]["size"], reverse=True)
    subs = [{"key": "file:" + name, "label": name, "size": f["size"], "hi": f["hi"],
             "lo": f["lo"], "color": COLORS[f["kind"]],
             "cat": "libraries" if f["kind"] == "SHARED-LIB" else "files"}
            for name, f in ranked[:4]]
    if stacks["count"]:
        stacks["label"] = f"thread stacks {stacks['count']} × {human(stacks['each'])}"
        subs.append(stacks)
    subs += big
    if small["count"]:
        small["label"] = f"small anonymous ({small['count']})"
        subs.append(small)
    rest = ranked[4:]
    count = len(rest) + kernel["count"]
    if count:
        parts = [f"{len(rest)} files"] if rest else []
        parts += ["[vdso]/[vvar]"] if kernel["count"] else []
        subs.append({"key": "other", "label": f"… {count} more: " + ", ".join(parts),
                     "size": sum(f["size"] for _, f in rest) + kernel["size"],
                     "hi": max([f["hi"] for _, f in rest] + [kernel["hi"]]),
                     "lo": min([f["lo"] for _, f in rest] + [kernel["lo"]]),
                     "color": COLORS["KERNEL-SPECIAL"], "cat": "kernel"})
    return sorted(subs, key=lambda s: s["hi"], reverse=True)   # high address first


def build_groups(regions):
    """The four groups that are present, top (high address) to bottom."""
    groups = []
    for name, kinds in GROUPS:
        rs = [r for r in regions if r["kind"] in kinds]
        if rs:
            g = {"name": name, "size": sum(r["size"] for r in rs),
                 "lo": min(r["start"] for r in rs), "hi": max(r["end"] for r in rs),
                 "subs": mmap_subblocks(rs) if name == "MEMORY-MAPPED AREA" else []}
            if name == "PROGRAM":              # sizes of the four program segments
                g["parts"] = {seg: sum(r["size"] for r in rs if r["kind"] == kind)
                              for seg, kind in (("code", "CODE"), ("rodata", "RODATA"),
                                                ("data", "DATA"), ("bss", "BSS"))}
            groups.append(g)
    return groups


def block_sizes(groups):
    """Size of every block, keyed by a stable name, to compare with the previous sample."""
    sizes = {}
    for g in groups:
        sizes["group:" + g["name"]] = g["size"]
        for s in g["subs"]:
            sizes["sub:" + s["key"]] = s["size"]
    return sizes


def change_text(key, size, prev):
    """(" (+1.1 MiB)" style delta, True if the block is new or grew)."""
    if prev is None:
        return "", False
    if key not in prev:
        return "  (new)", True
    diff = size - prev[key]
    if diff == 0:
        return "", False
    return f"  ({'+' if diff > 0 else '-'}{human(abs(diff))})", diff > 0


# ======================================================================================
# Sampling: a background thread, the only code that runs or waits for a subprocess
# ======================================================================================

class Watcher:
    """Samples one process through bin/vmv and remembers what the window needs."""

    def __init__(self, pid, demo_proc=None):
        self.pid, self.proc = pid, demo_proc
        self.info, self.groups, self.last_phase = None, [], ""
        self.sizes, self.prev_sizes = {}, None
        self.history = collections.deque(maxlen=HISTORY)
        self.sample_no, self.zombie_samples = 0, 0
        self.status, self.running, self.gone = "", True, False

    def stop(self, status, gone=True):
        self.status, self.groups, self.running, self.gone = status, [], False, gone

    def step(self):
        """Take one sample; sets self.running to False when updates should stop."""
        if self.proc is not None and self.zombie_samples >= ZOMBIE_SAMPLES:
            self.proc.wait()                   # reap our child: its process-table entry goes
            code = self.proc.returncode
            self.stop(f"Reaped, exit status {code}" if code >= 0
                      else f"Reaped, killed by signal {-code}")
            return
        rc, info, regions, err = read_sample(self.pid)
        if rc == 2:
            self.stop("Process exited")
            return
        if rc == 3:
            self.stop(f"Permission denied: PID {self.pid} belongs to another user. procfs "
                      "checks ptrace access when maps is opened - try sudo.", gone=False)
            return
        if rc != 0:
            self.stop(f"bin/vmv failed (exit {rc}): {err}", gone=False)
            return
        self.sample_no += 1
        self.info = info
        self.last_phase = info["phase"] or self.last_phase
        self.groups = build_groups(regions)
        self.prev_sizes, self.sizes = (self.sizes if self.sample_no > 1 else None), block_sizes(self.groups)
        mib = lambda kb: kb / 1024.0 if kb >= 0 else float("nan")
        self.history.append((self.sample_no, mib(info["vmsize_kb"]), mib(info["vmrss_kb"])))
        if info["state"] == "Z":
            self.zombie_samples += 1
            self.status = "ZOMBIE: exited, not yet reaped"
        else:
            self.status = ""

    def snapshot(self):
        """Everything the window needs from this sample, as a plain dict it can keep."""
        return {"pid": self.pid, "info": dict(self.info) if self.info else None,
                "groups": self.groups, "sizes": dict(self.sizes),
                "prev_sizes": dict(self.prev_sizes) if self.prev_sizes is not None else None,
                "history": list(self.history), "sample_no": self.sample_no,
                "last_phase": self.last_phase, "status": self.status,
                "running": self.running, "gone": self.gone}


def start_demo(phase_sec=3, start_delay=2):
    """Start bin/vmv_demo and read its PID from the first line it prints."""
    if not os.access(VMV_DEMO, os.X_OK):
        raise RuntimeError("bin/vmv_demo not found: run ./build.sh first")
    proc = subprocess.Popen([VMV_DEMO, str(phase_sec), str(start_delay)],
                            stdout=subprocess.PIPE, text=True, cwd=ROOT)
    m = re.search(r"PID (\d+)", proc.stdout.readline())   # "vmv_demo PID 1234 - watch it ..."
    if m is None:
        proc.kill()
        proc.wait()
        raise RuntimeError("could not read the PID from bin/vmv_demo")
    return proc, int(m.group(1))


def publish(q, snap):
    """Put snap into the size-1 queue, replacing an older one the window has not read."""
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    q.put(snap)


class Sampler(threading.Thread):
    """Runs `bin/vmv csv` every SAMPLE_PERIOD seconds on its own thread.

    With --demo it also starts bin/vmv_demo and reaps it after the zombie samples, so
    every subprocess call lives here. The window only reads `snapshots`."""

    def __init__(self, pid=None, demo_args=None, period=SAMPLE_PERIOD):
        super().__init__(daemon=True)
        self.pid, self.demo_args, self.period = pid, demo_args, period
        self.snapshots = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.paused = threading.Event()        # Space in the window: no samples while set

    def run(self):
        proc = None
        start = time.monotonic()
        try:
            if self.demo_args is not None:
                proc, self.pid = start_demo(*self.demo_args)
            w = Watcher(self.pid, proc)
            while not self.stop_event.is_set():
                while self.paused.is_set() and not self.stop_event.is_set():
                    self.stop_event.wait(0.1)
                if self.stop_event.is_set():
                    break
                t0 = time.monotonic()
                w.step()
                snap = w.snapshot()
                snap["t"] = t0 - start         # seconds since sampling started (chart x axis)
                publish(self.snapshots, snap)
                if not w.running:
                    break
                self.stop_event.wait(max(0.0, self.period - (time.monotonic() - t0)))
        except RuntimeError as e:
            w = Watcher(self.pid)
            w.stop(str(e), gone=False)
            snap = w.snapshot()
            snap["t"] = 0.0
            publish(self.snapshots, snap)
        finally:
            if proc is not None:               # never leave the demo running or unreaped
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait()
                    try:                       # a killed demo cannot delete its phase file
                        os.remove(f"/tmp/vmv_phase_{proc.pid}.txt")
                    except OSError:
                        pass
                proc.stdout.close()

    def stop(self):
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=15)


# ======================================================================================
# Animation: a critically damped spring for every value that moves
# ======================================================================================

class Spring:
    """A number that follows its target like a critically damped spring: it starts from
    where it is now (a new target bends the motion, never jumps) and never overshoots."""

    def __init__(self, value, response=RESPONSE):
        self.x = self.target = float(value)
        self.v = 0.0
        self.response = response
        self.omega = OMEGA if response == RESPONSE else 2 * math.pi / response

    def step(self, dt):
        a = self.omega * self.omega * (self.target - self.x) - 2.0 * self.omega * self.v
        self.v += a * dt
        self.x += self.v * dt

    def settled(self):
        tolerance = SETTLED * max(abs(self.target), 1.0)
        return abs(self.target - self.x) <= tolerance and abs(self.v) * self.response <= tolerance

    def snap(self):
        self.x, self.v = self.target, 0.0


# ======================================================================================
# Fast text: matplotlib 3.11 shapes and rasterises every string again on every draw
# ======================================================================================

class TextBitmaps:
    """Rendered text kept as pixels, for the animated window only.

    matplotlib 3.11 lays out every string with libraqm and rasterises its glyphs one by
    one each time it is drawn: 10 to 20 ms for a new label on our machine, far too slow
    for 30 frames a second. Here every glyph is rasterised once by FreeType (exactly the
    pixels matplotlib's draw_text gives it, checked for every glyph we use), a string is
    put together from its glyphs at the font's advance widths, and later draws blend the
    string's pixels into the frame. Saved PNGs do not use this: they draw text the
    normal way."""

    MAX_ENTRIES = 4000

    def __init__(self):
        import numpy as np                     # matplotlib's own array library
        self.np = np
        self.store = {}                        # (string, font, dpi, colour) -> pixels
        self.glyphs = {}                       # (character, font, dpi) -> glyph pixels
        self.advances = {}

    def install(self, renderer):
        """Route this renderer's draw_text through the cache (once per renderer)."""
        if getattr(renderer, "_vmv_text_cache", None) is self:
            return renderer
        original = renderer.draw_text

        def draw_text(gc, x, y, s, prop, angle, ismath=False, mtext=None):
            clipped = gc.get_clip_rectangle() is not None or gc.get_clip_path()[0] is not None
            if ismath or angle != 0 or clipped:          # rotated or clipped: draw normally
                return original(gc, x, y, s, prop, angle, ismath=ismath, mtext=mtext)
            self.blend(renderer, gc, x, y, s, prop)

        renderer.draw_text = draw_text
        renderer._vmv_text_cache = self
        return renderer

    def bitmap(self, renderer, gc, s, prop):
        key = (s, hash(prop), renderer.dpi, tuple(gc.get_rgb()[:3]), gc.get_antialiased())
        hit = self.store.get(key)
        if hit is None and key not in self.store:
            if len(self.store) > self.MAX_ENTRIES:
                self.store.clear()
            hit = self.store[key] = self.render(renderer.dpi, gc, s, prop)
        return hit

    def _font(self, prop, dpi):
        from matplotlib.font_manager import findfont, get_font
        font = get_font(findfont(prop))
        font.set_size(prop.get_size_in_points(), dpi)
        return font

    def glyph(self, ch, prop, dpi):
        """One character as FreeType draws it: its coverage (0..1) and where its top-left
        pixel sits relative to the pen on the baseline (y down). None for a blank."""
        key = (ch, hash(prop), dpi)
        if key in self.glyphs:
            return self.glyphs[key]
        from matplotlib.backends.backend_agg import get_hinting_flag
        np = self.np
        font = self._font(prop, dpi)
        font.set_text(ch, 0.0, flags=get_hinting_flag())
        font.draw_glyphs_to_bitmap(antialiased=True)
        image = np.asarray(font.get_image())
        rows, cols = np.nonzero(image.any(axis=1))[0], np.nonzero(image.any(axis=0))[0]
        hit = None
        if len(rows):
            top = font.get_width_height()[1] - font.get_descent()   # 1/64 pixels
            # FreeType's image starts at the glyph's left and top pixel edges, and one row
            # lower when the top is a whole pixel
            dx = math.floor(font.get_bitmap_offset()[0] / 64) + int(cols[0])
            dy = -math.ceil(top / 64) + int(rows[0]) - (1 if top % 64 == 0 else 0)
            crop = image[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
            hit = (crop.astype(np.float32) / 255.0, dx, dy)
        self.glyphs[key] = hit
        return hit

    def warm(self, props, dpi, chars):
        """Rasterise the usual characters of every font up front (once per dpi), so that
        a new label in the middle of an animation never waits for FreeType."""
        for prop in props:
            for ch in chars:
                self.glyph(ch, prop, dpi)
                self.advance(ch, prop, dpi)

    def advance(self, ch, prop, dpi):
        key = (ch, hash(prop), dpi)
        adv = self.advances.get(key)
        if adv is None:
            adv = self.advances[key] = (self._font(prop, dpi).load_char(ord(ch))
                                        .linearHoriAdvance / 65536.0)
        return adv

    def render(self, dpi, gc, s, prop):
        """The pixels of s in gc's colour, put together from its glyphs, and where their
        top-left corner sits relative to draw_text's (x, y)."""
        np = self.np
        placed, pen = [], 0.0
        for ch in s:
            hit = self.glyph(ch, prop, dpi)
            if hit is not None:
                placed.append((hit[0], int(round(pen)) + hit[1], hit[2]))
            pen += self.advance(ch, prop, dpi)
        if not placed:
            return None                        # only spaces
        left = min(x for _, x, _ in placed)
        top = min(y for _, _, y in placed)
        right = max(x + c.shape[1] for c, x, _ in placed)
        bottom = max(y + c.shape[0] for c, _, y in placed)
        coverage = np.zeros((bottom - top, right - left, 1), np.float32)
        for c, x, y in placed:                 # one glyph over the other, as Agg draws them
            part = coverage[y - top:y - top + c.shape[0], x - left:x - left + c.shape[1], 0]
            part += c * (1.0 - part)
        rgb = np.array([round(255 * v) for v in gc.get_rgb()[:3]], np.float32)
        colour = rgb * coverage + 0.5                    # premultiplied, +0.5 rounds
        # the same rule in whole numbers for the usual, unfaded draw:
        #   new = (old * keep + add) >> 8   with   keep + a = 256
        # it cannot overflow 16 bits: 255 * keep + 255 * a + 128 = 65408
        a = np.rint(coverage * 256.0).astype(np.uint16)
        add = np.empty(coverage.shape[:2] + (4,), np.uint16)
        add[..., :3] = rgb.astype(np.uint16) * a
        add[..., 3:] = 255 * a                           # keeps the frame's alpha at 255
        add += 128                                       # rounds
        return colour, coverage, 256 - a, add, left, top

    def blend(self, renderer, gc, x, y, s, prop):
        """Blend the cached pixels of s at the position matplotlib asked for.

        The "over" rule of alpha compositing, as Agg itself does it for text. The figure
        background is opaque, so the alpha channel of the frame stays 255 and is skipped."""
        hit = self.bitmap(renderer, gc, s, prop)
        if hit is None:
            return
        colour, coverage, keep, add, dx, dy = hit
        frame = self.np.asarray(renderer.buffer_rgba())
        top, left = int(round(y)) + dy, int(round(x)) + dx
        h, w = coverage.shape[:2]
        t0, l0 = max(top, 0), max(left, 0)
        t1, l1 = min(top + h, frame.shape[0]), min(left + w, frame.shape[1])
        if t0 >= t1 or l0 >= l1:
            return
        rows, cols = slice(t0 - top, t1 - top), slice(l0 - left, l1 - left)
        fade = gc.get_alpha()
        if fade is None or fade >= 0.999:                # whole numbers, in place: 3x faster
            dst = frame[t0:t1, l0:l1]
            mixed = dst.astype(self.np.uint16)
            mixed *= keep[rows, cols]
            mixed += add[rows, cols]
            mixed >>= 8
            dst[...] = mixed
        else:                                            # fading in or out
            dst = frame[t0:t1, l0:l1, :3]
            a = coverage[rows, cols] * fade
            dst[...] = dst * (1.0 - a) + (colour[rows, cols] - 0.5) * fade + 0.5


class BoxPixels:
    """Rounded boxes painted straight into the frame with numpy, for the animated window.

    Agg draws a rounded box as a general curved path, about 0.3 ms each, and a frame has
    twenty of them. Here a box is snapped to whole pixels: its straight parts are plain
    array fills, and each corner is one blend of a cached tile that holds the face and
    the border together. Saved PNGs draw boxes the normal way."""

    MAX_TILES = 2000

    def __init__(self):
        import numpy as np
        self.np = np
        self.masks, self.tiles = {}, {}

    def corner(self, r, inset):
        """Coverage (0..1) of the top-left n x n square (n = ceil(r)) of a box with corner
        radius r, shrunk by `inset` pixels on every side; 4 x 4 samples per pixel."""
        key = (r, inset)
        hit = self.masks.get(key)
        if hit is None:
            np = self.np
            n = max(int(math.ceil(r)), 1)
            sub = (np.arange(n * 4) + 0.5) / 4.0
            y, x = np.meshgrid(sub, sub, indexing="ij")
            round_part = (x - r) ** 2 + (y - r) ** 2 <= (r - inset) ** 2
            inside = (x >= inset) & (y >= inset) & (round_part | (x >= r) | (y >= r))
            hit = inside.reshape(n, 4, n, 4).mean(axis=(1, 3)).astype(np.float32)
            self.masks[key] = hit
        return hit

    def tile(self, r, face, edge, ring):
        """The top-left corner of a box as (keep, add) for blend(): the face, then the
        border ring over it, over whatever is beneath."""
        key = (r, face, edge, ring)
        hit = self.tiles.get(key)
        if hit is None:
            np = self.np
            if len(self.tiles) > self.MAX_TILES:
                self.tiles.clear()
            fa = self.corner(r, 0)[..., None] * face[3]                 # face coverage
            ra = ((self.corner(r, 0) - self.corner(r, ring))[..., None] * edge[3]
                  if ring else np.zeros_like(fa))                        # border coverage
            left = (1.0 - fa) * (1.0 - ra)                              # of what is beneath
            colour = (np.array(face[:3], np.float32) * 255.0 * fa * (1.0 - ra)
                      + np.array(edge[:3], np.float32) * 255.0 * ra)
            keep = np.floor(left * 256.0).astype(np.uint16)             # floor: no overflow
            add = np.empty(keep.shape[:2] + (4,), np.uint16)
            add[..., :3] = np.rint(colour * 256.0).astype(np.uint16)
            add[..., 3:] = 255 * (256 - keep)                           # alpha stays 255
            add += 128
            hit = self.tiles[key] = (keep, add)
        return hit

    def blend(self, frame, y0, x0, keep, add):
        """frame = (frame * keep + add) >> 8 over the tile's area (clipped to the frame)."""
        h, w = keep.shape[:2]
        t0, t1 = max(y0, 0), min(y0 + h, frame.shape[0])
        l0, l1 = max(x0, 0), min(x0 + w, frame.shape[1])
        if t0 >= t1 or l0 >= l1:
            return
        dst = frame[t0:t1, l0:l1]
        mixed = dst.astype(self.np.uint16)
        mixed *= keep[t0 - y0:t1 - y0, l0 - x0:l1 - x0]
        mixed += add[t0 - y0:t1 - y0, l0 - x0:l1 - x0]
        mixed >>= 8
        dst[...] = mixed

    def flat(self, frame, y0, y1, x0, x1, colour):
        """One colour over a rectangle: a plain 32-bit fill when opaque."""
        np = self.np
        t0, t1 = max(y0, 0), min(y1, frame.shape[0])
        l0, l1 = max(x0, 0), min(x1, frame.shape[1])
        if t0 >= t1 or l0 >= l1:
            return
        rgb = [int(round(255 * v)) for v in colour[:3]]
        if colour[3] >= 0.999:
            frame.view(np.uint32)[t0:t1, l0:l1, 0] = np.array(rgb + [255], np.uint8).view(np.uint32)[0]
            return
        a = int(round(colour[3] * 256))
        dst = frame[t0:t1, l0:l1]
        mixed = dst.astype(np.uint16)
        mixed *= 256 - a
        mixed += np.array([v * a + 128 for v in rgb] + [255 * a + 128], np.uint16)
        mixed >>= 8
        dst[...] = mixed

    def dashed(self, frame, y0, y1, x0, x1, colour, dash):
        """flat() in on/off pieces along the longer side: one dashed edge."""
        on, off = dash
        if x1 - x0 >= y1 - y0:
            for x in range(x0, x1, on + off):
                self.flat(frame, y0, y1, x, min(x + on, x1), colour)
        else:
            for y in range(y0, y1, on + off):
                self.flat(frame, y, min(y + on, y1), x0, x1, colour)

    def box(self, frame, x0, y0, x1, y1, r, face, edge, line, dash=None):
        """The face, then a `line`-pixel border just inside the edge (dashed if dash is
        (on, off) pixels), as Agg orders them."""
        if x1 <= x0 or y1 <= y0:
            return
        r = max(0.0, min(r, (x1 - x0) // 2, (y1 - y0) // 2))   # corners never overlap
        r = round(r * 4) / 4                                     # a few cached corner sizes
        n = int(math.ceil(r)) if r >= 0.5 else 0
        ring = max(int(round(line)), 1) if line >= 0.5 and edge[3] > 0 else 0
        if face[3] > 0:
            self.flat(frame, y0 + n, y1 - n, x0, x1, face)
            if n:
                self.flat(frame, y0, y0 + n, x0 + n, x1 - n, face)
                self.flat(frame, y1 - n, y1, x0 + n, x1 - n, face)
        if ring:
            edges = ((y0, y0 + ring, x0 + n, x1 - n), (y1 - ring, y1, x0 + n, x1 - n),
                     (y0 + n, y1 - n, x0, x0 + ring), (y0 + n, y1 - n, x1 - ring, x1))
            for e in edges:
                if dash:
                    self.dashed(frame, *e, edge, dash)
                else:
                    self.flat(frame, *e, edge)
        if n and (face[3] > 0 or ring):
            face = tuple(face[:3]) + (round(face[3] * 255) / 255,)   # bounded cache keys
            edge = tuple(edge[:3]) + (round(edge[3] * 255) / 255,)
            keep, add = self.tile(r, face, edge, ring)
            self.blend(frame, y0, x0, keep, add)
            self.blend(frame, y0, x1 - n, keep[:, ::-1], add[:, ::-1])
            self.blend(frame, y1 - n, x0, keep[::-1], add[::-1])
            self.blend(frame, y1 - n, x1 - n, keep[::-1, ::-1], add[::-1, ::-1])


def make_text_classes():
    """FastText, PropText and FastBox need matplotlib, imported only after the backend is
    chosen."""
    from matplotlib.patches import FancyBboxPatch
    from matplotlib.text import Text

    class FastText(Text):
        """A matplotlib Text that is cheap to redraw in the animated window.

        Text.draw redoes its layout work (fonts, metrics, alignment) on every draw. Here
        the draw_text calls for the current string are recorded once, relative to the
        text's anchor point, and later frames blend the cached pixels at the new anchor.
        Saved PNGs (no text cache) draw the normal way."""

        _recorded = None                       # (key, [(dx, dy, line), ...])
        _origins = {}                          # (renderer, va, font) -> left-aligned offset
        metrics = None                         # TextMetrics, set by the scene

        def _anchor(self, renderer):
            affine = getattr(renderer, "_vmv_affine", None)
            if affine is not None and self.get_transform() is self.axes.transData:
                sx, tx, sy, ty = affine        # the frame's own scale and offset, no stack
                x, y = self.get_position()
                return sx * x + tx, renderer.height - (sy * y + ty)
            x, y = self.get_transform().transform(self.get_unitless_position())
            return x, renderer.height - y      # draw_text's y counts from the top

        def _matplotlib_record(self, renderer):
            """matplotlib's own layout: the draw_text calls Text.draw would make."""
            calls, cached = [], renderer.draw_text
            renderer.draw_text = lambda gc, x, y, s, *rest, **kw: calls.append((x, y, s))
            try:
                Text.draw(self, renderer)      # matplotlib does the layout, we only listen
            finally:
                renderer.draw_text = cached
            ax, ay = self._anchor(renderer)
            return [(x - ax, y - ay, s) for x, y, s in calls]

        def _origin(self, renderer):
            """Where matplotlib puts a left-aligned line in this font, relative to the
            anchor (laid out once per font and vertical alignment)."""
            key = (id(renderer), self.get_va(), hash(self.get_fontproperties()))
            hit = FastText._origins.get(key)
            if hit is None:
                text, ha = self.get_text(), self.get_ha()
                self.set_text("0")
                self.set_ha("left")
                (dx, dy, _), = self._matplotlib_record(renderer)
                self.set_text(text)
                self.set_ha(ha)
                hit = FastText._origins[key] = (dx, dy)
            return hit

        def _record(self, renderer):
            key = (id(renderer), self.get_text(), self.get_ha(), self.get_va(),
                   hash(self.get_fontproperties()))
            if self._recorded is None or self._recorded[0] != key:
                text = self.get_text()
                if self.metrics is None or "\n" in text:
                    lines = self._matplotlib_record(renderer)
                else:
                    # one line: matplotlib's layout costs milliseconds per new string, so
                    # place it from the font's advance widths (as PropText does)
                    dx, dy = self._origin(renderer)
                    prop = self.get_fontproperties()
                    width = self.metrics.width(text, prop.get_size_in_points(),
                                               weight_key(prop.get_weight()))
                    width *= renderer.dpi / UNIT
                    shift = {"right": width, "center": width / 2}.get(self.get_ha(), 0.0)
                    lines = [(dx - shift, dy, text)]
                self._recorded = (key, lines)
            return self._recorded[1]

        def _gc(self, renderer):
            gc = renderer.new_gc()
            gc.set_foreground(self.get_color())
            gc.set_alpha(self.get_alpha())
            if hasattr(self, "get_antialiased"):       # matplotlib 3.6 and newer
                gc.set_antialiased(self.get_antialiased())
            else:                                       # 3.5: one setting for all text
                import matplotlib
                gc.set_antialiased(matplotlib.rcParams["text.antialiased"])
            return gc

        def draw(self, renderer):
            cache = getattr(renderer, "_vmv_text_cache", None)
            if (cache is None or not self.get_visible() or not self.get_text()
                    or self.get_rotation() != 0):
                return Text.draw(self, renderer)
            ax, ay = self._anchor(renderer)
            gc, prop = self._gc(renderer), self.get_fontproperties()
            for dx, dy, line in self._record(renderer):
                cache.blend(renderer, gc, ax + dx, ay + dy, line, prop)
            gc.restore()
            self.stale = False

    class PropText(FastText):
        """Text that changes every frame (counting numbers).

        A new string every frame would fill the text cache, so in the animated window the
        text is drawn character by character from it, each character placed with the
        font's own advance width. Saved PNGs draw it as one string the normal way."""

        def draw(self, renderer):
            cache = getattr(renderer, "_vmv_text_cache", None)
            text = self.get_text()
            if cache is None or not self.get_visible() or not text:
                return Text.draw(self, renderer)
            prop = self.get_fontproperties()
            size, weight = prop.get_size_in_points(), weight_key(prop.get_weight())
            scale = renderer.dpi / UNIT        # layout units -> device pixels
            dx, dy = self._origin(renderer)
            ax, ay = self._anchor(renderer)
            width = self.metrics.width(text, size, weight) * scale
            ha = self.get_ha()
            x = ax - width if ha == "right" else ax - width / 2 if ha == "center" else ax
            gc = self._gc(renderer)
            for ch in text:
                if ch != " ":
                    cache.blend(renderer, gc, x + dx, ay + dy, ch, prop)
                x += self.metrics.width(ch, size, weight) * scale
            gc.restore()
            self.stale = False

    class FastBox(FancyBboxPatch):
        """A rounded box (positioned by set_round) that the animated window paints with
        BoxPixels; saved PNGs draw it the normal way."""

        def draw(self, renderer):
            boxes = getattr(renderer, "_vmv_boxes", None)
            if (boxes is None or not self.get_visible()
                    or self.get_transform() is not self.axes.transData):
                return FancyBboxPatch.draw(self, renderer)
            sx, tx, sy, ty = renderer._vmv_affine
            x, y, w, h = self.get_x(), self.get_y(), self.get_width(), self.get_height()
            line = self.get_linewidth() * renderer.dpi / 72.0
            grow = line / 2 if line >= 2 else 0.0   # Agg centres a thick line on the outline
            xa, xb = round(sx * x + tx - grow), round(sx * (x + w) + tx + grow)
            ya = round(renderer.height - (sy * y + ty) - grow)
            yb = round(renderer.height - (sy * (y + h) + ty) + grow)
            boxes.box(boxes.np.asarray(renderer.buffer_rgba()),
                      min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb),
                      getattr(self, "_vmv_radius", 0.0) * renderer.dpi / UNIT + grow,
                      self.get_facecolor(), self.get_edgecolor(), line,
                      getattr(self, "_vmv_dash", None))
            self.stale = False

    return FastText, PropText, FastBox


# ======================================================================================
# Look: fonts, colours, text measuring
# ======================================================================================

def pick_family():
    """The first of FONT_FAMILIES that matplotlib can find."""
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in FONT_FAMILIES if f in names), "DejaVu Sans")


def weight_key(w):
    """matplotlib weight (name or number) -> 'semibold' or 'normal'."""
    if isinstance(w, str):
        return "semibold" if w in ("semibold", "demibold", "demi", "bold", "heavy",
                                   "extra bold", "black") else "normal"
    return "semibold" if w >= 600 else "normal"


class TextMetrics:
    """Width of a string in layout units, from the font's own advance widths (no shaping,
    so it is cheap enough to fit labels with "…" every time the layout changes)."""

    def __init__(self, family):
        from matplotlib.font_manager import FontProperties, findfont, get_font
        self.fonts = {w: get_font(findfont(FontProperties(family=family, weight=w)))
                      for w in ("normal", "semibold")}
        self.cache = {}

    def width(self, s, size, weight="normal"):
        font, total = self.fonts[weight], 0.0
        for ch in s:
            key = (ch, size, weight)
            adv = self.cache.get(key)
            if adv is None:
                font.set_size(size, UNIT)      # at 100 dpi one pixel is one layout unit
                adv = self.cache[key] = font.load_char(ord(ch)).linearHoriAdvance / 65536.0
            total += adv
        return total

    def fit(self, s, max_width, size, weight="normal"):
        """s, or s shortened with "…" so that it fits in max_width."""
        max_width -= 2.0                       # metrics ignore kerning: keep a small margin
        if self.width(s, size, weight) <= max_width:
            return s
        while s and self.width(s + "…", size, weight) > max_width:
            s = s[:-1]
        return s.rstrip() + "…" if s else ""


def mix(c1, c2, t):
    """The colour t of the way from c1 to c2."""
    from matplotlib.colors import to_rgb
    return tuple(a + (b - a) * t for a, b in zip(to_rgb(c1), to_rgb(c2)))


def text_on(fill):
    """White or near-black, whichever contrasts more with fill (WCAG contrast ratio)."""
    from matplotlib.colors import to_rgb

    def luminance(c):
        r, g, b = (x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in to_rgb(c))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    lf = luminance(fill)
    on_white = 1.05 / (lf + 0.05)
    on_dark = (lf + 0.05) / (luminance(DARK_TEXT) + 0.05)
    return "#FFFFFF" if on_white >= on_dark else DARK_TEXT


def short_size(n):
    """Compact size for tight labels: whole KiB below 1 MiB ("960 KiB"), else human()."""
    return f"{n // 1024} KiB" if 1024 <= n < 1024 * 1024 else human(n)


def nice_step(v):
    """Smallest 1, 2 or 5 x 10^k that is >= v (grid and axis label steps)."""
    if v <= 0:
        return 1.0
    k = 10.0 ** math.floor(math.log10(v))
    return next(m * k for m in (1, 2, 5, 10) if m * k >= v)


def anchor_of(top, h, mode):
    """The edge (or centre) of a block that stays put while its height changes (y down)."""
    return top if mode == "top" else top + h if mode == "bottom" else top + h / 2


def top_of(anchor, h, mode):
    return anchor if mode == "top" else anchor - h if mode == "bottom" else anchor - h / 2


def empty_message(snap):
    if snap["gone"]:
        return "the process is gone: nothing is left of it"
    if snap["info"] is not None and snap["info"]["state"] == "Z":
        return "address space released at exit"
    if not snap["running"]:
        return "the memory map could not be read"
    return "no user address space"


def banner_content(snap):
    """(state, title, description, second line, phase number or None) of the banner."""
    info, status = snap["info"], snap["status"]
    if status.startswith("Reaped"):
        return "reaped", "Reaped · " + status.split(", ", 1)[-1], "", "", None
    if info is not None and info["state"] == "Z" and not snap["gone"]:
        return "zombie", "Zombie — exited, waiting for its parent to reap it", "", "", None
    if not snap["running"]:
        head, _, rest = status.partition(": ")
        return "error", head, "", rest, None
    m = re.match(r"\s*(\d+)\s*:\s*(.*)", (info["phase"] if info else "") or "")
    if m:
        return ("phase", f"Phase {m.group(1)} of {PHASES}", m.group(2).replace("->", "→"),
                "", int(m.group(1)))
    if info is not None:
        return ("watch", f"Watching {info['name']}", "memory map read from /proc once a second",
                "", None)
    return "watch", "Starting…", "", "", None


def frame_layout(W, H, s):
    """Rectangles (x, y, w, h) of the window in layout units, y grows downwards."""
    M, G = 24 * s, 16 * s
    title_h = SIZES["title"] * s * PT * 1.3
    banner_y = M + title_h + 12 * s
    banner_h = (SIZES["phase"] + SIZES["label"]) * 1.3 * s * PT + 22 * s
    legend_h = 22 * s
    content_y = banner_y + banner_h + G
    content_h = H - M - legend_h - G - content_y
    cw = W - 2 * M - G
    left_w = 0.55 * cw                          # left 55 %, right 45 %
    right_x, right_w = M + left_w + G, cw - left_w
    metrics_h = 236 * s
    return {"M": M, "W": W, "H": H,
            "title": (M, M, W - 2 * M, title_h),
            "banner": (M, banner_y, W - 2 * M, banner_h),
            "left": (M, content_y, left_w, content_h),
            "metrics": (right_x, content_y, right_w, metrics_h),
            "chart": (right_x, content_y + metrics_h + G, right_w, content_h - metrics_h - G),
            "legend": (M, H - M - legend_h, W - 2 * M, legend_h)}


def artist_look(a):
    """Everything about a moving artist that decides its pixels. If no artist of a card
    changed its look since the last frame, the card's pixels are still right."""
    import numpy as np
    from matplotlib.patches import Circle, FancyBboxPatch, Polygon
    from matplotlib.lines import Line2D
    look = (id(a), a.get_alpha(), a.get_zorder())
    if hasattr(a, "get_text"):
        return look + (a.get_text(), a.get_position(), a.get_color(), a.get_fontsize(),
                       a.get_fontweight(), a.get_ha())
    if isinstance(a, Line2D):
        xs, ys = a.get_data(orig=True)
        return look + (np.asarray(xs, float).tobytes(), np.asarray(ys, float).tobytes(),
                       a.get_color(), a.get_linewidth())
    if isinstance(a, FancyBboxPatch):
        return look + (a.get_x(), a.get_y(), a.get_width(), a.get_height(),
                       getattr(a, "_vmv_radius", None), a.get_facecolor(), a.get_edgecolor(),
                       a.get_linewidth(), getattr(a, "_vmv_dash", None))
    if isinstance(a, Circle):
        return look + (tuple(a.center), a.radius, a.get_facecolor(), a.get_edgecolor())
    if isinstance(a, Polygon):
        return look + (np.asarray(a.get_xy(), float).tobytes(), a.get_facecolor())
    return look + (object(),)                  # unknown kind: never equal, always redrawn


def fit_heights(naturals, floors, room):
    """naturals scaled down by one common factor (at most 1) so that together they fill at
    most `room`, none below its floor: the same log rule for every block, only smaller.
    If even the floors do not fit, the floors themselves are scaled down."""
    keys = list(naturals)
    total = lambda f: sum(max(floors[k], f * naturals[k]) for k in keys)
    if total(1.0) <= room:
        return {k: max(floors[k], naturals[k]) for k in keys}
    if total(0.0) > room:
        squeeze = room / total(0.0) if total(0.0) > 0 else 0.0
        return {k: floors[k] * squeeze for k in keys}
    lo, hi = 0.0, 1.0
    for _ in range(30):                        # total() grows with f: halve the interval
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if total(mid) <= room else (lo, mid)
    return {k: max(floors[k], lo * naturals[k]) for k in keys}


def set_round(patch, x, y, w, h, radius):
    """Move a rounded box; the corner radius never exceeds half the box."""
    patch.set_bounds(x, y, max(w, 0.0), max(h, 0.0))
    r = max(0.0, min(radius, h / 2, w / 2))
    if abs(r - getattr(patch, "_vmv_radius", -1.0)) > 0.25:
        patch.set_boxstyle("round", pad=0, rounding_size=r)
        patch._vmv_radius = r


# ======================================================================================
# The address-space column: blocks, rows, segments and gaps
# ======================================================================================

class ColumnItem:
    """One rounded block of the address-space column (or a gap label), with springs for
    its anchor edge, its height and its opacity. x and width come from the layout."""

    # text slots: where a label sits inside its block
    #   L1/R1: name left / size right on the first line   L2, L3: second and third line
    #   H/HR:  header of a container                       C: centred   C1/C2: centred, 2 lines
    # L1..L3 are stacked and centred as a group; a line shows once the block is tall enough
    def __init__(self, scene, key, spec, grow):
        self.scene, self.key, self.kind, self.mode = scene, key, spec["kind"], spec["mode"]
        self.dying = False
        self.anchor = Spring(anchor_of(spec["y"], spec["h"], self.mode), FOCUS_RESPONSE)
        self.h = Spring(0.0 if grow else spec["h"], FOCUS_RESPONSE)   # new: grows from zero
        self.alpha = Spring(0.0 if grow else 1.0)            # ... and fades in
        self.shadow = self.body = None
        top_level = self.kind in ("block", "container", "program")
        if self.kind != "gap":
            if top_level:
                self.shadow = scene.box("#000000", None, 0, zorder=1.5)
            self.body = scene.box(None, None, 1, zorder=2 if top_level else 2.5)
        self.texts = {}
        self.update(spec)

    def update(self, spec):
        """New targets from a new sample; the motion continues from where it is now."""
        self.dying = False
        self.x, self.w, self.spec = spec["x"], spec["w"], spec
        self.anchor.target = anchor_of(spec["y"], spec["h"], self.mode)
        self.h.target = spec["h"]
        self.alpha.target = 1.0
        if self.body is not None:
            self.body.set_facecolor(spec["fill"])
            self.body.set_edgecolor(spec["edge"])
        for slot, (text, size, weight, color) in spec["texts"].items():
            t = self.texts.get(slot)
            if t is None:
                ha = "right" if slot in ("R1", "HR") else "center" if slot.startswith("C") else "left"
                t = self.texts[slot] = self.scene.text("", size, weight, color, ha=ha, zorder=4)
            t.set_text(text)
            t.set_color(color)
            t.set_fontsize(self.scene.pt(size))      # PROGRAM's segments may change size
            t.set_fontweight(weight)
        for slot, t in self.texts.items():
            if slot not in spec["texts"]:
                t.set_text("")

    def fade_to(self, alpha):
        self.alpha.target = alpha

    def die(self):
        """Removed: shrink to zero height and fade out at the same position."""
        self.dying = True
        self.h.target, self.alpha.target = 0.0, 0.0

    def springs(self):
        return (self.anchor, self.h, self.alpha)

    def finished(self):
        return self.dying and self.alpha.x < 0.01 and self.h.x < 0.5

    def geometry(self):
        h = max(self.h.x, 0.0)
        return self.x, top_of(self.anchor.x, h, self.mode), self.w, h

    def apply(self):
        sc, s = self.scene, self.scene.s
        x, y, w, h = self.geometry()
        a = min(max(self.alpha.x, 0.0), 1.0)
        if self.body is not None:
            set_round(self.body, x, y, w, h, 6 * s)
            self.body.set_alpha(a)
        if self.shadow is not None:
            # only the shadow's bottom edge shows below the opaque block, so only that strip
            # is drawn: a full-size translucent box costs Agg up to 3 ms a frame
            sh = min(h, 14 * s)
            set_round(self.shadow, x, y + h - sh + 2 * s, w, sh, 6 * s)
            self.shadow.set_alpha(a * sc.theme["shadow"])
        pad, vpad, gap = 10 * s, 5 * s, 2 * s
        texts = self.spec["texts"]
        lh1 = sc.lh("label")
        # L1..L3: as many lines as fit, centred as a group; each one fades in over 4 px
        lines = [slot for slot in ("L1", "L2", "L3") if texts.get(slot, ("",))[0]]
        heights = [sc.lh(texts[slot][1]) for slot in lines]
        need = [2 * vpad + sum(heights[:i + 1]) + gap * i for i in range(len(lines))]
        fit = max([i + 1 for i in range(len(lines)) if h >= need[i]] or [min(len(lines), 1)])
        used = sum(heights[:fit]) + gap * (fit - 1)
        line_y, ly = {}, y + h / 2 - used / 2
        for i, slot in enumerate(lines):
            line_y[slot] = ly + heights[i] / 2
            ly += heights[i] + gap
        for slot, t in self.texts.items():
            vis = 1.0
            if slot in line_y:
                i = lines.index(slot)
                t.set_position((x + pad, line_y[slot]))
                vis = (min(max((h - heights[0] * 0.75) / (4 * s), 0.0), 1.0) if i == 0
                       else min(max((h - need[i]) / (4 * s), 0.0), 1.0))
            elif slot == "R1":
                t.set_position((x + w - pad, line_y.get("L1", y + h / 2)))
                vis = min(max((h - (heights[0] if heights else lh1) * 0.75) / (4 * s), 0.0), 1.0)
            elif slot in ("H", "HR"):
                hy = y + self.spec["head"] / 2
                t.set_position((x + pad if slot == "H" else x + w - pad, hy))
                vis = min(max((h - self.spec["head"]) / (4 * s), 0.0), 1.0)
            elif slot in ("C", "C1", "C2"):                  # PROGRAM's segments, gap labels
                two = "C2" in texts
                need2 = 2 * vpad + 2 * lh1 + gap
                show2 = min(max((h - need2) / (4 * s), 0.0), 1.0) if two else 0.0
                if slot == "C":
                    lhc = sc.lh(texts["C"][1])       # the line height of its own size
                    show1 = min(max((h - lhc * 0.75) / (4 * s), 0.0), 1.0)
                    t.set_position((x + w / 2, y + h / 2))
                    vis = min(1.0 - show2, show1) if two else show1
                    if self.kind == "gap":   # a gap label shows only where it fits whole
                        vis = 1.0 if h >= lhc * 0.85 else 0.0
                else:
                    dy = -lh1 / 2 - 1 * s if slot == "C1" else lh1 / 2 + 1 * s
                    t.set_position((x + w / 2, y + h / 2 + dy))
                    vis = show2
            t.set_alpha(a * vis)

    def artists(self):
        arts = [p for p in (self.shadow, self.body) if p is not None]
        return arts + [t for t in self.texts.values() if t.get_text()]

    def remove(self):
        for a in [p for p in (self.shadow, self.body) if p is not None] + list(self.texts.values()):
            a.remove()


class Pill:
    """A small "+128.0 MiB" label beside a block that just changed size; it fades out."""

    def __init__(self, scene, key, text, fill, born):
        self.scene, self.key, self.born = scene, key, born
        self.box = scene.box(fill, None, 0, zorder=5)
        self.label = scene.text(text, "caption", "semibold", text_on(fill), ha="center", zorder=6)
        self.width = scene.metrics.width(text, scene.pt("caption"), "semibold") + 16 * scene.s

    def alpha(self, now, animate):
        if not animate:
            return 1.0
        age = now - self.born
        return min(max(min(age / 0.15, (PILL_SECONDS - age) / 0.5), 0.0), 1.0)

    def apply(self, now, animate):
        sc, s = self.scene, self.scene.s
        item = sc.items.get(self.key)
        if item is None:
            a = 0.0
            x, yc = sc.pill_x[0], 0.0
        else:
            _, y, _, h = item.geometry()
            a, x, yc = self.alpha(now, animate) * min(item.alpha.x, 1.0), sc.pill_x[0], y + h / 2
        w = min(self.width, sc.pill_x[1] - x)
        ph = sc.lh("caption") + 8 * s
        set_round(self.box, x, yc - ph / 2, w, ph, ph / 2)
        self.box.set_alpha(a)
        self.label.set_position((x + w / 2, yc))
        self.label.set_alpha(a)

    def alive(self, now):
        return now - self.born < PILL_SECONDS

    def remove(self):
        self.box.remove()
        self.label.remove()


# ======================================================================================
# The scene: every artist is created once and then only updated
# ======================================================================================

class Scene:
    """The whole window, drawn on one full-figure canvas in layout units (y down).

    fast=True  (window, --bench): artists are 'animated' and drawn over a saved
               background (blitting), text goes through TextBitmaps.
    fast=False (--png, --frames): normal matplotlib drawing for saved files.
    animate=False: every value jumps straight to its target (--no-anim, saved files)."""

    def __init__(self, fig, fast, animate, theme="light", scale=1.0, demo=False):
        import matplotlib
        self.fig, self.fast, self.animate, self.demo = fig, fast, animate, demo
        self.theme, self.s = THEMES[theme], scale
        self.family = pick_family()
        matplotlib.rcParams["font.family"] = "sans-serif"
        matplotlib.rcParams["font.sans-serif"] = [self.family, "DejaVu Sans"]
        self.metrics = TextMetrics(self.family)
        self.FastText, self.PropText, self.FastBox = make_text_classes()
        self.FastText.metrics = self.metrics      # PropText inherits it
        fig.set_facecolor(self.theme["bg"])
        self.ax = fig.add_axes([0, 0, 1, 1])
        self.ax.set_axis_off()
        self.ax.set_autoscale_on(False)
        self.items, self.pills = {}, {}
        self.snap = self.info = None
        self.now = 0.0
        self.paused = False
        self.background = None
        self.need_full_draw = True             # (fast) redraw everything, save a new background
        self.drawn_looks = {}                  # card -> looks of its artists when last drawn
        self.dirty = True                      # something changed that springs do not track
        self.drawn_settled = False
        self.snapshots = None
        self.last_tick = time.monotonic()
        self.text_cache = TextBitmaps() if fast else None
        self.box_pixels = BoxPixels() if fast else None
        self.warmed = set()                    # dpis whose glyphs are rasterised
        self.focus_auto = True                 # O: auto focus on/off (kept across R)
        self._build()
        self._reset_state()
        self.relayout()
        if fast:
            canvas = fig.canvas
            plain_get_renderer = canvas.get_renderer

            def get_renderer(*args, **kwargs):  # every renderer the canvas makes gets the
                # caches; matplotlib 3.5 passes cleared=True, newer versions pass nothing
                renderer = self.text_cache.install(plain_get_renderer(*args, **kwargs))
                m = self.ax.transData.get_matrix()
                renderer._vmv_affine = tuple(float(v) for v in (m[0, 0], m[0, 2], m[1, 1], m[1, 2]))
                renderer._vmv_boxes = self.box_pixels
                return renderer

            canvas.get_renderer = get_renderer
            canvas.mpl_connect("draw_event", self._on_draw)
            canvas.mpl_connect("resize_event", lambda _event: self.relayout())

    # ---- small helpers -----------------------------------------------------------------

    def pt(self, key):
        return SIZES[key] * self.s

    def lh(self, key):
        """Line height of a text size in layout units."""
        return SIZES[key] * self.s * PT * 1.25

    def text(self, s="", size="label", weight="normal", color=None, ha="left", va="center",
             animated=True, cls=None, zorder=4):
        t = (cls or self.FastText)(0, 0, s, fontsize=self.pt(size), fontweight=weight,
                                   color=color or self.theme["text"], ha=ha, va=va,
                                   animated=self.fast and animated, zorder=zorder)
        self.ax.add_artist(t)
        t.set_clip_on(False)
        return t

    def box(self, fill=None, edge=None, line_px=1.0, animated=True, zorder=2):
        p = self.FastBox((0, 0), 0, 0, boxstyle="round,pad=0,rounding_size=0",
                           facecolor=fill or "none", edgecolor=edge or "none",
                           linewidth=line_px * 72.0 / UNIT, animated=self.fast and animated,
                           zorder=zorder)
        self.ax.add_patch(p)
        return p

    def circle(self, radius, zorder=3):
        from matplotlib.patches import Circle
        c = Circle((0, 0), radius, edgecolor="none", animated=self.fast, zorder=zorder)
        self.ax.add_patch(c)
        return c

    def line(self, color, width_px, zorder=3):
        from matplotlib.lines import Line2D
        ln = Line2D([], [], color=color, linewidth=width_px * 72.0 / UNIT,
                    solid_capstyle="round", animated=self.fast, zorder=zorder)
        self.ax.add_line(ln)
        return ln

    # ---- construction ----------------------------------------------------------------

    def _build(self):
        from matplotlib.patches import Polygon
        t, s = self.theme, self.s
        # static parts: drawn into the saved background
        self.cards = {k: self.box(t["card"], t["border"], animated=False, zorder=0.5)
                      for k in ("left", "metrics", "chart")}
        self.title = self.text("Virtual Memory Visualizer", "title", "semibold", animated=False)
        self.col_header = self.text("ADDRESS SPACE    high → low", "caption", "semibold",
                                    t["secondary"], animated=False)
        self.col_caption = self.text("heights on a log scale", "caption", "normal",
                                     t["secondary"], animated=False)
        self.legend = [(self.box(c, None, 0, animated=False, zorder=1),
                        self.text(name, "label", "normal", t["secondary"], animated=False))
                       for name, c in KIND_COLORS.items()]
        self.metric_labels = [self.text(x, "label", "normal", t["secondary"], animated=False)
                              for x in ("Reserved (VmSize)", "In RAM (VmRSS)", "Page faults",
                                        "Threads")]
        self.bar_track = self.box(t["border"], None, 0, animated=False, zorder=1)
        self.chart_title = self.text(f"Reserved vs In RAM, last {CHART_SECONDS} s", "label",
                                     "semibold", animated=False)
        # moving parts
        self.banner = self.box(t["card"], t["border"], zorder=1)
        self.layers = [{"title": self.text("", "phase", "semibold"),
                        "desc": self.text("", "phase", "normal", t["secondary"]),
                        "sub": self.text("", "label", "normal", t["secondary"])} for _ in range(2)]
        self.dots = [self.circle(4.5 * s) for _ in range(PHASES + 1)]
        self.proc = self.text("", "label", "normal", t["secondary"], ha="right")
        self.halo = self.circle(5 * s, zorder=3)
        self.live_dot = self.circle(5 * s, zorder=3.5)
        self.live = self.text("live", "label", "semibold", ha="right")
        self.message = self.text("", "label", "normal", t["secondary"], ha="center")
        self.hero = [self.text("", "hero", "semibold", ha="right", cls=self.PropText)
                     for _ in range(2)]
        self.bar_fill = self.box(RAM_COLOR, None, 0, zorder=2)
        self.bar_label = self.text("", "label", "normal", ha="right", cls=self.PropText)
        self.faults = self.text("", "label", "normal", ha="right", cls=self.PropText)
        self.threads = self.text("", "label", "normal", ha="right", cls=self.PropText)
        self.grid = self.line(t["border"], 1, zorder=1.5)
        self.grid_labels = [self.text("", "caption", "normal", t["secondary"], ha="right")
                            for _ in range(7)]
        self.x_labels = [self.text("", "caption", "normal", t["secondary"], ha="center")
                         for _ in range(8)]
        self.ram_fill = Polygon([[0, 0], [0, 0], [0, 0]], closed=True, facecolor=RAM_COLOR,
                                edgecolor="none", alpha=0.15, animated=self.fast, zorder=2)
        self.ax.add_patch(self.ram_fill)
        self.res_line = self.line(t["secondary"], 2)
        self.ram_line = self.line(RAM_COLOR, 2.5)
        self.end_dots = {"res": self.circle(3.5 * s, zorder=3.5), "ram": self.circle(3.5 * s, zorder=3.5)}
        self.end_dots["res"].set_facecolor(t["secondary"])
        self.end_dots["ram"].set_facecolor(RAM_COLOR)
        self.end_labels = {"res": self.text("Reserved", "label", "semibold", t["secondary"]),
                           "ram": self.text("In RAM", "label", "semibold", RAM_COLOR)}
        # focus view: an outline in the block's colour, a soft glow, the mode caption
        self.focus_glow = [self.box(None, "#000000", 4 * s, zorder=z) for z in (1.3, 1.35)]
        self.focus_ring = self.box(None, "#000000", 3 * s, zorder=2.6)
        self.mode_caption = self.text("", "caption", "semibold", t["secondary"], ha="right")
        self.ram_glow = self.box(RAM_COLOR, None, 0, zorder=1.8)
        self.ram_pill_box = self.box(RAM_COLOR, None, 0, zorder=5)
        self.ram_pill_label = self.text("", "caption", "semibold", text_on(RAM_COLOR),
                                        ha="center", zorder=6)

    def _reset_state(self):
        """Springs and per-run state (also used by R: restart the demo)."""
        self.num = {k: Spring(0.0) for k in ("vmsize", "vmrss", "minflt", "majflt", "threads")}
        self.frac = Spring(0.0)                # VmRSS / VmSize for the bar
        self.t_end, self.y_max = Spring(5.0), Spring(1.0)
        self.newest = [Spring(0.0), Spring(0.0)]
        self.history = []                      # [t, reserved MiB, in-RAM MiB]
        self.last_sample = None
        self.banner_state, self.banner_t0, self.front = None, -1e9, 0
        self.pulse_t0 = -1e9
        self.ended = False                     # True once the process is gone: sampling stopped
        self.message_alpha = Spring(0.0)
        # focus view
        self.focus_key = None                  # the block or row drawn big, or None (overview)
        self.pinned = False                    # clicked by the presenter: no timeout
        self.focus_t0 = -1e9                   # when the focus last moved (8 s hold)
        self.changes = {}                      # key -> (change line, extra line, time)
        self.ghost = None                      # a freed region fading out: key, sub, born
        self.last_sizes = self.last_threads = self.last_live = None
        self.last_subs = {}
        self.ring_alpha = Spring(0.0)
        self.ram_pill = None                   # {"delta": KiB, "born": time} of the In RAM pill
        self.ram_pulse_t0 = -1e9

    def reset(self):
        """Forget the current process (R restarts the demo)."""
        for item in self.items.values():
            item.remove()
        for pill in self.pills.values():
            pill.remove()
        self.items, self.pills = {}, {}
        self.snap = self.info = None
        self._reset_state()
        for layer in self.layers:
            for t in layer.values():
                t.set_text("")
        self.message.set_text("")
        self.dirty = True

    def relayout(self):
        """Place every card for the current window size (start, resize, fullscreen)."""
        W, H = (v * UNIT for v in self.fig.get_size_inches())
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(H, 0)
        s, m = self.s, self.metrics
        L = self.L = frame_layout(W, H, s)
        for k in ("left", "metrics", "chart"):
            set_round(self.cards[k], *L[k], 12 * s)
        tx, ty, tw, th = L["title"]
        self.title.set_position((tx, ty + th / 2))
        lx, ly, lw, lh = L["left"]
        cap = self.lh("caption")
        self.col_header.set_position((lx + 16 * s, ly + 16 * s + cap / 2))
        self.col_caption.set_position((lx + 16 * s, ly + lh - 16 * s - cap / 2))
        gutter = 104 * s                       # room for the "+128.0 MiB" pills
        self.col = (lx + 16 * s, lx + lw - 16 * s - gutter,
                    ly + 16 * s + cap + 10 * s, ly + lh - 16 * s - cap - 8 * s)
        self.pill_x = (self.col[1] + 8 * s, lx + lw - 16 * s)
        self.mode_caption.set_position((self.col[1], ly + lh - 16 * s - cap / 2))
        x, (gx, gy, gw, gh) = L["legend"][0], L["legend"]
        sq = 12 * s
        for square, label in self.legend:
            set_round(square, x, gy + gh / 2 - sq / 2, sq, sq, 3 * s)
            label.set_position((x + sq + 6 * s, gy + gh / 2))
            x += sq + 6 * s + m.width(label.get_text(), self.pt("label")) + 24 * s
        mx, my, mw, _ = L["metrics"]
        pad, hero, lab = 20 * s, self.lh("hero"), self.lh("label")
        r1 = my + pad + hero / 2
        r2 = r1 + hero + 6 * s
        rb = r2 + hero / 2 + 18 * s
        r4 = rb + 30 * s
        r5 = r4 + lab + 10 * s
        for label, y in zip(self.metric_labels, (r1, r2, r4, r5)):
            label.set_position((mx + pad, y))
        for value, y in zip(self.hero, (r1, r2)):
            value.set_position((mx + mw - pad, y))
        self.ram_row = (mx + mw - pad, r2)     # right edge and centre of the In RAM number
        self.bar_rect = (mx + pad, rb - 5 * s, mw - 2 * pad - 150 * s, 10 * s)
        set_round(self.bar_track, *self.bar_rect, 5 * s)
        for text, y in ((self.bar_label, rb), (self.faults, r4), (self.threads, r5)):
            text.set_position((mx + mw - pad, y))
        cx, cy, cw, ch = L["chart"]
        pad = 18 * s
        self.chart_title.set_position((cx + pad, cy + pad + lab / 2))
        self.plot = (cx + pad + 56 * s, cy + pad + lab + 18 * s,      # left, top
                     cx + cw - pad - 84 * s, cy + ch - pad - cap - 10 * s)   # right, bottom
        set_round(self.banner, *L["banner"], 12 * s)
        self.need_full_draw = True
        if self.snap is not None:
            self._set_topbar(self.snap)
            self._set_banner(self.snap, force=True)
            self._set_column(self.snap, relayout=True)
        else:
            self._set_topbar(None)
        self.dirty = True
        self.apply()

    # ---- new sample: only targets change ---------------------------------------------

    def set_targets(self, snap):
        if not self.animate:                   # saved frames: the clock is the sample time
            self.now = snap.get("t", self.now)
        self.snap = snap
        if not self.animate:                   # saved frames show only this sample's pills
            for pill in self.pills.values():
                pill.remove()
            self.pills = {}
        self._set_topbar(snap)
        self._set_banner(snap)
        self._set_focus(snap)
        self._focus_timers()
        self._set_column(snap)
        self._set_pills(snap)
        self._set_metrics(snap)
        self._set_chart(snap)
        self.dirty = True
        if not self.animate:
            for spring in self.springs():
                spring.snap()
            self._drop_finished()
            self.banner_t0 = self.pulse_t0 = -1e9
        self.apply()

    def _set_topbar(self, snap):
        s, m = self.s, self.metrics
        info = snap["info"] if snap else None
        pid = snap["pid"] if snap else None
        text = (f"{info['name']} · PID {pid} ·" if info else f"PID {pid} ·" if pid else "")
        self.proc.set_text(text)
        self.ended = snap is not None and not snap["running"]   # sampling has stopped for good
        self.live.set_text("ended" if self.ended else "paused" if self.paused else "live")
        tx, ty, tw, th = self.L["title"]
        right, yc = tx + tw, ty + th / 2
        live_w = m.width(self.live.get_text(), self.pt("label"), "semibold")
        self.live.set_position((right, yc))
        self.dot_xy = (right - live_w - 11 * s, yc)
        self.proc.set_position((self.dot_xy[0] - 11 * s, yc))
        self.dirty = True

    def set_paused(self, paused):
        self.paused = paused
        self._set_topbar(self.snap)

    def _set_banner(self, snap, force=False):
        content = banner_content(snap)
        if content == self.banner_state and not force:
            return
        changed = content != self.banner_state
        self.banner_state = content
        state, title, desc, sub, phase_no = content
        t, s, m = self.theme, self.s, self.metrics
        calm = state in ("phase", "watch")
        fill = t["card"] if calm else STATE_COLORS[state]
        ink = t["text"] if calm else text_on(fill)
        ink2 = t["secondary"] if calm else ink
        self.banner.set_facecolor(fill)
        self.banner.set_edgecolor(t["border"] if calm else fill)
        bx, by, bw, bh = self.L["banner"]
        pad = 22 * s
        x, right = bx + pad, bx + bw - pad
        lp, ll = self.lh("phase"), self.lh("label")
        if sub:                                # two lines, centred as a block
            y1 = by + (bh - (lp + 4 * s + ll)) / 2 + lp / 2
            y2 = y1 + lp / 2 + 4 * s + ll / 2
        else:
            y1, y2 = by + bh / 2, by + bh / 2
        title = m.fit(title, right - x, self.pt("phase"), "semibold")
        tw = m.width(title, self.pt("phase"), "semibold")
        dot_x = x + tw + 18 * s
        self.dot_spec = None
        desc_x = x + tw + 16 * s
        if phase_no is not None:
            self.dot_spec = [(dot_x + 4.5 * s + i * 15 * s, y1, i <= phase_no) for i in range(PHASES + 1)]
            desc_x = dot_x + (PHASES + 1) * 15 * s + 12 * s
        desc = m.fit(desc, right - desc_x, self.pt("phase")) if desc else ""
        sub = m.fit(sub, right - x, self.pt("label")) if sub else ""
        new = 1 - self.front if (self.animate and changed and not force) else self.front
        layer = self.layers[new]
        for key, text, pos, colour in (("title", title, (x, y1), ink), ("desc", desc, (desc_x, y1), ink2),
                                       ("sub", sub, (x, y2), ink2)):
            layer[key].set_text(text)
            layer[key].set_position(pos)
            layer[key].set_color(colour)
        if new != self.front:                  # crossfade: the new layer fades in
            self.front, self.banner_t0 = new, self.now
        else:
            for text in self.layers[1 - self.front].values():
                text.set_text("")
        self.dot_colors = (ink, t["border"])

    # ---- focus view ----------------------------------------------------------------

    def _set_focus(self, snap):
        """Compare this sample with the last one the window saw: the block or row whose
        size changed most (by at least FOCUS_MIN_CHANGE; a new row counts with its whole
        size) becomes the focus, unless the presenter pinned one or turned auto focus off."""
        info = snap["info"]
        if not snap["groups"] or info is None or info["state"] == "Z":
            self.focus_key, self.pinned, self.ghost = None, False, None   # zombie: no focus
            return
        sizes = {k: v for k, v in snap["sizes"].items() if k != "group:MEMORY-MAPPED AREA"}
        subs = {"sub:" + sub["key"]: sub for g in snap["groups"] for sub in g["subs"]}
        prev, best, best_d = self.last_sizes, None, 0
        for key in sorted(set(prev or {}) | set(sizes)):
            d = sizes.get(key, 0) - (prev or {}).get(key, 0)
            if prev is not None and abs(d) >= FOCUS_MIN_CHANGE and abs(d) > abs(best_d):
                best, best_d = key, d
        if best is not None:
            old, new = prev.get(best), sizes.get(best)
            if old is None:
                line = f"+{human(new)} · new region"
            elif new is None:
                line = f"−{human(old)} · freed"
            else:
                line = f"{'grew' if new > old else 'shrank'} {short_size(old)} → {short_size(new)}"
            more = info["threads"] - (self.last_threads or info["threads"])
            # only the two numbers side by side - bin/vmv cannot tell what the region is for
            extra = (f"appeared with +{more} thread{'s' if more != 1 else ''}"
                     if old is None and best.startswith("sub:") and more > 0 else "")
            self.changes[best] = (line, extra, self.now)
            if new is None and best in self.last_subs:     # freed: fades out where it was
                self.ghost = {"key": best, "sub": self.last_subs[best], "born": self.now}
            if self.focus_auto and not self.pinned:
                self.focus_key, self.focus_t0 = best, self.now
        if self.pinned and self.focus_key not in sizes:  # the pinned region is gone
            self.pinned, self.focus_key = False, None
        self.last_sizes, self.last_subs, self.last_threads = sizes, subs, info["threads"]

    def _focus_timers(self):
        """Time-driven focus changes (ghost gone, 8 s without a change); True if the
        column has to be laid out again."""
        changed = False
        if self.ghost is not None and self.now - self.ghost["born"] >= GHOST_SECONDS:
            if self.focus_key == self.ghost["key"]:
                self.focus_key = None          # the column settles into the overview
            self.ghost, changed = None, True
        if (self.focus_key is not None and not self.pinned
                and self.now - self.focus_t0 >= FOCUS_HOLD):
            self.focus_key, changed = None, True
        return changed

    def _with_ghost(self, groups):
        """The groups, plus the row of a region freed just now (it fades out in place)."""
        g = self.ghost
        if g is None or not g["key"].startswith("sub:"):
            return groups
        out = []
        for grp in groups:
            if grp["subs"]:
                subs = grp["subs"] + [dict(g["sub"], ghost=True)]
                grp = dict(grp, subs=sorted(subs, key=lambda r: r["hi"], reverse=True))
            out.append(grp)
        return out

    def _focus_lines(self, key, size, lo):
        """Second and third line of the focused block: what changed, or (pinned, no change
        since) its size and where it starts."""
        line, extra, when = self.changes.get(key, ("", "", -1e9))
        if self.pinned and when < self.focus_t0:
            return f"{human(size)} · starts at 0x{lo:x}", ""
        return line, extra

    def focus_blocked(self):
        """No focus for a zombie: its address space is already released."""
        info = self.snap["info"] if self.snap else None
        return info is None or info["state"] == "Z"

    def mode_text(self):
        return "pinned" if self.pinned else "auto focus" if self.focus_auto else "overview"

    def click(self, x, y):
        """Presenter: click a block to pin the focus on it, the empty column to unpin.
        Returns the key that is pinned now, or None."""
        x0, x1, top, bottom = self.col
        if (self.snap is None or not self.snap["groups"] or self.focus_blocked()
                or x is None or y is None or not (x0 <= x <= x1 and top <= y <= bottom)):
            return self.focus_key if self.pinned else None
        hit, inside = None, False
        for key, item in self.items.items():
            if item.dying or item.kind == "gap":
                continue
            ix, iy, iw, ih = item.geometry()
            if ix <= x <= ix + iw and iy <= y <= iy + ih:
                inside = True
                if item.kind in ("row", "block", "program"):
                    hit = key
                elif item.kind == "segment":
                    hit = "group:PROGRAM"
        if hit is not None and not (self.ghost and hit == self.ghost["key"]):
            self.pinned, self.focus_key, self.focus_t0 = True, hit, self.now
        elif not inside and self.pinned:       # the background: back to auto or overview
            self.pinned, self.focus_key = False, None
        self._set_column(self.snap)
        self.dirty = True
        return self.focus_key if self.pinned else None

    def toggle_auto(self):
        """O: auto focus on/off. Off is the overview (log heights only)."""
        self.focus_auto = not self.focus_auto
        self.pinned, self.focus_key = False, None
        if self.snap is not None:
            self._set_column(self.snap)
        self.dirty = True
        return self.focus_auto

    # ---- the address-space column ----------------------------------------------------

    def _column_specs(self, snap):
        """Target spec of every block, row, segment and gap.

        Overview: every block and row is 28 px + 14 px x log2(size / 4 KiB) tall, so a
        bigger one is always taller; if the column is too short, the log part shrinks by
        the same factor for all of them.
        Focus: the focused block or row takes FOCUS_SHARE of the column; the others keep
        that rule, scaled down together into the rest, never below ROW_MIN px."""
        groups = self._with_ghost(snap["groups"])
        if not groups:
            return {}, empty_message(snap)
        s, m, t = self.s, self.metrics, self.theme
        x0, x1, top, bottom = self.col
        w, avail = x1 - x0, bottom - top
        base, per = 28 * s, 14 * s
        lg = lambda size: max(0.0, math.log2(max(size, 1) / 4096.0))
        units = {}                             # top-level blocks and rows: key -> size
        for g in groups:
            for sub in g["subs"]:
                units["sub:" + sub["key"]] = sub["size"]
            if not g["subs"]:
                units["group:" + g["name"]] = g["size"]
        focus = self.focus_key if self.focus_key in units else None
        # gap band, container head, container bottom, row gap, room around the focused
        # block for its outline; the compact set only when a focused column is crowded
        dims = (22 * s, 30 * s, 8 * s, 4 * s, 6 * s if focus else 0.0)
        compact = (14 * s, 26 * s, 6 * s, 2 * s, 4 * s)

        def overhead(gap, head, ins, rgap, fm):
            total = gap * (len(groups) - 1) + 2 * fm
            return total + sum(head + ins + rgap * (len(g["subs"]) - 1) for g in groups if g["subs"])

        over = overhead(*dims)
        if focus is None:
            fixed = over + base * len(units)
            logs = sum(per * lg(v) for v in units.values())
            k = 1.0 if fixed + logs <= avail or logs <= 0 else max(0.0, (avail - fixed) / logs)
            shrink = min(1.0, avail / fixed) if fixed > 0 else 1.0   # only if even 28 px is too much
            heights = {key: (base + k * per * lg(v)) * shrink for key, v in units.items()}
        else:
            # floors: ROW_MIN px, or what one line of text needs at this scale; PROGRAM
            # also needs room for its segments' labels
            row_floor = max(ROW_MIN, 0.75 * self.lh("label") + 4 * s)
            program_floor = max(32, 2 * 5 * s + row_floor)
            floors = {key: program_floor if key == "group:PROGRAM" else row_floor
                      for key in units if key != focus}
            least = sum(floors.values())
            # the focused block never gets less than its name and two lines need
            big_min = 2 * 5 * s + self.lh("focus") + 2 * self.lh("label") + 2 * 2 * s + 4 * s
            for option in (dims, compact):
                dims, over = option, overhead(*option)
                big = min(FOCUS_SHARE * avail, avail - over - least)
                if big >= big_min:
                    break
            big = max(big, big_min)
            need = over + big + least
            shrink = min(1.0, avail / need) if need > 0 else 1.0
            naturals = {key: base + per * lg(units[key]) for key in floors}
            heights = fit_heights(naturals, {k: f * shrink for k, f in floors.items()},
                                  avail - (over + big) * shrink)
            heights[focus] = big * shrink
        gap, head_h, inset, row_gap, fm = (v * shrink for v in dims)
        slots = {}
        for g in groups:
            keys = ["sub:" + sub["key"] for sub in g["subs"]] or ["group:" + g["name"]]
            slots[g["name"]] = sum(heights[k] for k in keys) + (2 * fm if focus in keys else 0.0)
            if g["subs"]:
                slots[g["name"]] += head_h + inset + row_gap * (len(keys) - 1)
        # PROGRAM sits on the floor and the others stack upwards (the heap grows up);
        # the stack hangs from the top (it grows down) and the space between stretches
        place, y = {}, bottom
        for g in reversed(groups):
            h = slots[g["name"]]
            if g["name"] == "STACK":
                place[g["name"]] = (top, h)
            else:
                place[g["name"]] = (y - h, h)
                y -= h + gap
        specs, pad, lab = {}, 10 * s, self.pt("label")
        for g in groups:
            gy, gh = place[g["name"]]
            key = "group:" + g["name"]
            size_text = human(g["size"])
            if g["subs"]:                      # MEMORY-MAPPED AREA: a container with rows
                specs[key] = {"kind": "container", "mode": "center", "x": x0, "w": w, "y": gy,
                              "h": gh, "head": head_h, "fill": t["fill"], "edge": t["border"],
                              "texts": {"H": (m.fit(g["name"], w - 3 * pad - m.width(size_text, lab, "semibold"),
                                                    lab, "semibold"), "label", "semibold", t["text"]),
                                        "HR": (size_text, "label", "semibold", t["text"])}}
                ry, rx, rw = gy + head_h, x0 + 8 * s, w - 16 * s
                for sub in g["subs"]:
                    sk = "sub:" + sub["key"]
                    focused, ghost = sk == focus, bool(sub.get("ghost"))
                    ry += fm if focused else 0.0
                    rh = heights[sk]
                    fill = KIND_COLORS[sub["cat"]]
                    ink = text_on(fill)
                    texts = self._block_texts(sk, sub["label"], "" if ghost else human(sub["size"]),
                                              "", focused, ink, rw, sub["size"], sub["lo"], weight="normal")
                    specs[sk] = {"kind": "row", "mode": "center", "x": rx, "w": rw, "y": ry, "h": rh,
                                 "fill": fill, "edge": mix(fill, "#FFFFFF", 0.35), "texts": texts,
                                 "focus": focused, "ghost": ghost}
                    ry += rh + row_gap + (fm if focused else 0.0)
            elif g["name"] == "PROGRAM":
                focused = key == focus
                self._program_specs(specs, key, g, x0, gy + (fm if focused else 0.0), w,
                                    gh - (2 * fm if focused else 0.0))
                specs[key]["focus"] = focused
            else:                              # STACK or HEAP: one block
                focused = key == focus
                fill = KIND_COLORS["stack" if g["name"] == "STACK" else "heap"]
                texts = self._block_texts(key, g["name"], size_text, GROUP_NOTE[g["name"]],
                                          focused, text_on(fill), w, g["size"], g["lo"])
                specs[key] = {"kind": "block", "mode": ANCHOR.get(g["name"], "center"),
                              "x": x0, "w": w, "y": gy + (fm if focused else 0.0),
                              "h": gh - (2 * fm if focused else 0.0), "fill": fill,
                              "edge": mix(fill, "#FFFFFF", 0.35), "texts": texts, "focus": focused}
        for upper, lower in zip(groups, groups[1:]):   # the unused space between neighbours
            uy, uh = place[upper["name"]]
            ly, _ = place[lower["name"]]
            unused = upper["lo"] - lower["hi"]
            text = f"≈ {human(unused)} unused" if unused > 0 else ""
            specs[f"gap:{upper['name']}|{lower['name']}"] = {
                "kind": "gap", "mode": "center", "x": x0, "w": w, "y": uy + uh,
                "h": max(ly - (uy + uh), 0.0),
                "texts": {"C": (m.fit(text, w, self.pt("caption")), "caption", "normal", t["secondary"])}}
        return specs, None

    def _block_texts(self, key, name, size_text, note, focused, ink, w, size, lo, weight="semibold"):
        """Text of a block or row: name and size; focused, the name 1.4x as big and what
        changed below it; otherwise the note (STACK, HEAP) as a second line."""
        m, pad, lab = self.metrics, 10 * self.s, self.pt("label")
        name_key = "focus" if focused else "label"
        name_weight = "semibold" if focused else weight
        name_w = w - 3 * pad - m.width(size_text, lab, weight)
        texts = {"L1": (m.fit(name, name_w, self.pt(name_key), name_weight), name_key, name_weight, ink),
                 "R1": (size_text, "label", weight, ink)}
        lines = list(self._focus_lines(key, size, lo)) + [note] if focused else [note]
        lines = [line for line in lines if line]
        for slot, line in zip(("L2", "L3"), lines):
            texts[slot] = (m.fit(line, w - 2 * pad, lab), "label", "normal", ink)
        return texts

    def _program_specs(self, specs, key, g, x, y, w, h):
        """PROGRAM: a tinted block with its name and size, and the code, rodata, data and
        bss segments side by side, each as wide as its own text needs."""
        s, m, t = self.s, self.metrics, self.theme
        lab, pad, spad = self.pt("label"), 10 * s, 7 * s
        colour = KIND_COLORS["program"]
        tint = mix(colour, t["card"], 0.78)
        ink = text_on(tint)
        size_text = short_size(g["size"])
        parts = [(name, size) for name, size in g["parts"].items() if size > 0]
        gap, inset = 6 * s, 5 * s
        room = w - inset - gap * max(len(parts) - 1, 0)      # the label cell plus the segments
        two = h >= 2 * 5 * s + 2 * self.lh("label") + 2 * s
        # widest plan first, then give up detail until the row fits: the total size (it is
        # the sum of the segments), then the segment font size, then the segment sizes
        heads = [None] if two else ["PROGRAM · " + size_text, "PROGRAM"]
        plans = ([(head, "label", True) for head in heads] +
                 [(heads[-1], "caption", True), (heads[-1], "caption", False)])
        for head, font, sized in plans:
            if head is None:                                  # two lines: name over size
                label_w = max(m.width("PROGRAM", lab, "semibold"), m.width(size_text, lab))
            else:
                label_w = m.width(head, lab, "semibold")
            label_w = min(label_w + 2 * pad + 2, 0.4 * room)   # + fit()'s margin
            words = [f"{name} {short_size(size)}" if sized else name for name, size in parts]
            needs = [m.width(word, self.pt(font)) + 2 * spad + 2 for word in words]
            if label_w + sum(needs) <= room:
                break
        texts = ({"L1": ("PROGRAM", "label", "semibold", ink),
                  "L2": (m.fit(size_text, label_w - 2 * pad, lab), "label", "normal", ink)}
                 if head is None else
                 {"L1": (m.fit(head, label_w - 2 * pad, lab, "semibold"), "label", "semibold", ink)})
        specs[key] = {"kind": "program", "mode": "center", "x": x, "w": w, "y": y, "h": h,
                      "fill": tint, "edge": mix(colour, t["card"], 0.55), "texts": texts}
        spare = room - label_w - sum(needs)                   # shared out evenly; < 0 only
        squeeze = min(1.0, (room - label_w) / max(sum(needs), 1.0))   # in the last resort
        sx, sy, sh = x + label_w, y + inset, h - 2 * inset
        shades = {"code": 0.0, "rodata": 0.22, "data": 0.42, "bss": 0.6}
        for (name, size), word, need in zip(parts, words, needs):
            seg_w = need + spare / len(parts) if spare >= 0 else need * squeeze
            fill = mix(colour, "#FFFFFF", shades[name])
            seg_ink = text_on(fill)
            inner = seg_w - 2 * spad
            seg_texts = {"C": (m.fit(word, inner, self.pt(font)), font, "normal", seg_ink),
                         "C1": (m.fit(name, inner, lab, "semibold"), "label", "semibold", seg_ink),
                         "C2": (m.fit(short_size(size), inner, lab), "label", "normal", seg_ink)}
            specs["seg:" + name] = {"kind": "segment", "mode": "center", "x": sx, "w": seg_w,
                                    "y": sy, "h": sh, "fill": fill, "edge": mix(fill, "#FFFFFF", 0.35),
                                    "texts": seg_texts}
            sx += seg_w + gap

    def _set_column(self, snap, relayout=False):
        specs, message = self._column_specs(snap)
        info = snap["info"]
        zombie = info is not None and info["state"] == "Z" and not snap["gone"]
        if specs:
            self.last_live = snap
        if zombie and not specs:
            # the address space is released, but its process-table entry remains:
            # keep the picture (as the overview: no focus), faded to 25 %
            if self.last_live is not None:
                for key, spec in self._column_specs(self.last_live)[0].items():
                    if key in self.items and not self.items[key].dying:
                        self.items[key].update(spec)
            for item in self.items.values():
                if not item.dying:
                    item.fade_to(0.25)
            message = None
        else:
            grow = self.animate and not relayout
            for key, spec in specs.items():
                if key in self.items:
                    self.items[key].update(spec)
                else:
                    self.items[key] = ColumnItem(self, key, spec, grow)
            for key, item in self.items.items():
                if key not in specs:
                    item.die()
        x0, x1, top, bottom = self.col
        self.mode_caption.set_text(self.mode_text() if snap["groups"] else "")
        self.message.set_position(((x0 + x1) / 2, (top + bottom) / 2))
        if message:
            self.message.set_text(message)
        self.message_alpha.target = 1.0 if message else 0.0

    def _set_pills(self, snap):
        prev, sizes = snap["prev_sizes"], snap["sizes"]
        if prev is None:
            return
        for key, size in sizes.items():
            item_key = key
            if key == "group:MEMORY-MAPPED AREA" or key not in prev or size == prev[key]:
                continue
            item = self.items.get(item_key)
            if item is None or item.body is None:
                continue
            d = size - prev[key]
            text = f"+{human(d)}" if d > 0 else f"−{human(-d)}"
            old = self.pills.pop(item_key, None)
            if old is not None:
                old.remove()
            self.pills[item_key] = Pill(self, item_key, text, item.spec["fill"], self.now)

    def _set_metrics(self, snap):
        i = snap["info"]
        if i is None:
            return
        first = self.info is None
        prev = self.info
        if prev is not None and prev["vmrss_kb"] >= 0 and i["vmrss_kb"] >= 0:
            d = i["vmrss_kb"] - prev["vmrss_kb"]
            if abs(d) >= RAM_PILL_MIN_KB:      # In RAM changed: pulse, and a delta pill that
                p = self.ram_pill              # adds up while it is still showing
                if p is not None and self.now - p["born"] < PILL_SECONDS and (p["delta"] > 0) == (d > 0):
                    p["delta"], p["born"] = p["delta"] + d, self.now
                else:
                    self.ram_pill = {"delta": d, "born": self.now}
                self.ram_pulse_t0 = self.now
        targets = {"vmsize": i["vmsize_kb"], "vmrss": i["vmrss_kb"], "minflt": i["minflt"],
                   "majflt": i["majflt"], "threads": i["threads"]}
        for k, v in targets.items():
            self.num[k].target = float(max(v, 0))
            if first and self.animate:         # the very first values count up from zero
                self.num[k].x, self.num[k].v = 0.0, 0.0
        if i["vmsize_kb"] > 0 and i["vmrss_kb"] >= 0:
            self.frac.target = i["vmrss_kb"] / i["vmsize_kb"]
        self.info = i

    def _set_chart(self, snap):
        i = snap["info"]
        if i is None or snap["sample_no"] == self.last_sample or not snap["running"]:
            return
        mib = lambda kb: kb / 1024.0 if kb >= 0 else float("nan")
        point = [snap.get("t", 0.0), mib(i["vmsize_kb"]), mib(i["vmrss_kb"])]
        for spring, target in zip(self.newest, point[1:]):
            start = spring.x if self.history else (0.0 if self.animate else target)
            if math.isnan(target):
                start = target = 0.0           # no value (zombie): drawn as a gap
            spring.x, spring.v, spring.target = start, 0.0, target
        self.history.append(point)
        self.history = [p for p in self.history if p[0] >= point[0] - CHART_SECONDS - 2]
        self.t_end.target = max(point[0], 5.0)
        values = [v for p in self.history for v in p[1:] if not math.isnan(v)]
        self.y_max.target = max(1.15 * max(values), 1.0) if values else 1.0
        self.last_sample = snap["sample_no"]
        self.pulse_t0 = self.now               # the "live" dot pulses once per sample

    # ---- every frame -----------------------------------------------------------------

    def springs(self):
        for item in self.items.values():
            yield from item.springs()
        yield from self.num.values()
        yield from (self.frac, self.t_end, self.y_max, self.message_alpha, self.ring_alpha)
        yield from self.newest

    def step(self, dt):
        """Advance every spring and timed effect; True while anything still moves."""
        self.now += dt
        if self.paused:                        # a paused explanation keeps the focus in place
            self.focus_t0 += dt
            if self.ghost is not None:
                self.ghost["born"] += dt
        if self._focus_timers() and self.snap is not None:
            self._set_column(self.snap)
        moving = False
        for spring in self.springs():
            spring.step(dt)
            if not spring.settled():
                moving = True
        timers = (any(p.alive(self.now) for p in self.pills.values())
                  or self.ghost is not None
                  or (self.ram_pill is not None and self.now - self.ram_pill["born"] < PILL_SECONDS)
                  or self.now - self.banner_t0 < CROSSFADE
                  or (self.now - self.pulse_t0 < PULSE_SECONDS and not self.paused))
        for key in [k for k, p in self.pills.items() if not p.alive(self.now)]:
            self.pills.pop(key).remove()
        self._drop_finished()
        if not moving:
            for spring in self.springs():
                spring.snap()
        return moving or timers

    def _drop_finished(self):
        for key in [k for k, item in self.items.items() if item.finished()]:
            self.items.pop(key).remove()
            pill = self.pills.pop(key, None)
            if pill is not None:
                pill.remove()

    def apply(self):
        """Copy the current spring values into the artists."""
        t, s = self.theme, self.s
        if self.ghost is not None and self.ghost["key"] in self.items:
            fade = min(max(1.0 - (self.now - self.ghost["born"]) / GHOST_SECONDS, 0.0), 1.0)
            ghost = self.items[self.ghost["key"]]
            ghost.alpha.x = ghost.alpha.target = fade
            ghost.alpha.v = 0.0
        for item in self.items.values():
            item.apply()
        self._apply_focus()
        for pill in self.pills.values():
            pill.apply(self.now, self.animate)
        self.message.set_alpha(min(max(self.message_alpha.x, 0.0), 1.0))
        # banner: crossfade and progress dots
        f = min(max((self.now - self.banner_t0) / CROSSFADE, 0.0), 1.0) if self.animate else 1.0
        for n, layer in enumerate(self.layers):
            for text in layer.values():
                text.set_alpha(f if n == self.front else 1.0 - f)
        for n, dot in enumerate(self.dots):
            if getattr(self, "dot_spec", None):
                x, y, filled = self.dot_spec[n]
                dot.set_center((x, y))
                dot.set_facecolor(self.dot_colors[0] if filled else self.dot_colors[1])
                dot.set_visible(True)
            else:
                dot.set_visible(False)
        # top bar: the live dot pulses once per sample, gray while paused
        self.live_dot.set_center(self.dot_xy)
        quiet = self.paused or self.ended
        self.live_dot.set_facecolor(t["secondary"] if quiet else STATE_COLORS["reaped"])
        self.live.set_color(t["secondary"] if quiet else t["text"])
        age = (self.now - self.pulse_t0) / PULSE_SECONDS if self.animate else 1.0
        self.halo.set_center(self.dot_xy)
        self.halo.set_radius(5 * s * (1.0 + 1.6 * min(age, 1.0)))
        self.halo.set_facecolor(STATE_COLORS["reaped"])
        self.halo.set_alpha(0.0 if quiet or age >= 1.0 else 0.45 * (1.0 - age))
        self._apply_metrics()
        self._apply_chart()

    def _apply_focus(self):
        """The focused block's outline (dashed when pinned) and soft glow follow it."""
        s, t = self.s, self.theme
        item = self.items.get(self.focus_key) if self.focus_key is not None else None
        self.ring_alpha.target = 1.0 if item is not None and not item.dying else 0.0
        if not self.animate:                   # saved frames: no motion, straight to it
            self.ring_alpha.snap()
        a = min(max(self.ring_alpha.x, 0.0), 1.0) * (min(max(item.alpha.x, 0.0), 1.0) if item else 0.0)
        for box in [self.focus_ring] + self.focus_glow:
            box.set_visible(a > 0.01)
        if a <= 0.01:
            return
        x, y, w, h = item.geometry()
        colour = KIND_COLORS["program"] if item.kind == "program" else item.spec["fill"]
        d = 3.5 * s                            # 2 px of background, then the 3 px outline
        set_round(self.focus_ring, x - d, y - d, w + 2 * d, h + 2 * d, 6 * s + d)
        self.focus_ring.set_edgecolor(colour)
        self.focus_ring.set_alpha(a)
        on, off = 7 * s, 5 * s                 # dash and gap in pixels
        width = 3 * s
        self.focus_ring.set_linestyle((0, (on / width, off / width)) if self.pinned else "solid")
        self.focus_ring._vmv_dash = (max(int(round(on)), 1), max(int(round(off)), 1)) if self.pinned else None
        for box, d, strength in ((self.focus_glow[0], 7 * s, 0.55), (self.focus_glow[1], 11 * s, 0.25)):
            set_round(box, x - d, y - d + 2 * s, w + 2 * d, h + 2 * d, 6 * s + d)
            box.set_alpha(a * t["shadow"] * strength)

    def _apply_metrics(self):
        i = self.info
        self._apply_ram_pill()
        if i is None:
            for text in self.hero + [self.bar_label, self.faults, self.threads]:
                text.set_text("")
            self.bar_fill.set_visible(False)
            return
        n = self.num
        for text, key in zip(self.hero, ("vmsize", "vmrss")):
            text.set_text(human(int(n[key].x * 1024)) if i[key + "_kb"] >= 0 else "n/a")
        valid = i["vmsize_kb"] > 0 and i["vmrss_kb"] >= 0
        frac = min(max(self.frac.x, 0.0), 1.0)
        bx, by, bw, bh = self.bar_rect
        set_round(self.bar_fill, bx, by, max(bw * frac, bh if frac > 0 else 0.0), bh, bh / 2)
        self.bar_fill.set_visible(valid)
        self.bar_label.set_text(f"{100.0 * frac:.1f}% in RAM" if valid else "")
        self.faults.set_text(f"minor {int(round(n['minflt'].x)):,} · major {int(round(n['majflt'].x)):,}")
        state = "gone" if self.snap and self.snap["gone"] else STATE_NAMES.get(i["state"], "?")
        self.threads.set_text(f"{int(round(n['threads'].x))} · {state}")

    def _apply_ram_pill(self):
        """In RAM changed by 1 MiB or more: the number pulses and a delta pill sits beside it."""
        s, i, p = self.s, self.info, self.ram_pill
        right, yc = self.ram_row
        value = human(max(i["vmrss_kb"], 0) * 1024) if i is not None else ""
        left = right - self.metrics.width(value, self.pt("hero"), "semibold")
        age = self.now - self.ram_pulse_t0
        pulse = 0.0 if i is None or age >= PULSE_SECONDS else (1.0 - age / PULSE_SECONDS if self.animate else 1.0)
        hh = self.lh("hero")
        set_round(self.ram_glow, left - 10 * s, yc - hh / 2, right - left + 20 * s, hh, 10 * s)
        self.ram_glow.set_alpha(0.16 * pulse)
        self.ram_glow.set_visible(pulse > 0.01)
        alpha = 0.0
        if p is not None and i is not None:
            age = self.now - p["born"]
            alpha = (1.0 if age < PILL_SECONDS else 0.0) if not self.animate else \
                min(max(min(age / 0.15, (PILL_SECONDS - age) / 0.5), 0.0), 1.0)
            text = f"+{human(p['delta'] * 1024)}" if p["delta"] > 0 else f"−{human(-p['delta'] * 1024)}"
            self.ram_pill_label.set_text(text)
            pw = self.metrics.width(text, self.pt("caption"), "semibold") + 16 * s
            ph = self.lh("caption") + 8 * s
            px = left - 14 * s - pw
            set_round(self.ram_pill_box, px, yc - ph / 2, pw, ph, ph / 2)
            self.ram_pill_label.set_position((px + pw / 2, yc))
        self.ram_pill_box.set_alpha(alpha)
        self.ram_pill_label.set_alpha(alpha)
        self.ram_pill_box.set_visible(alpha > 0.01)
        self.ram_pill_label.set_visible(alpha > 0.01)

    def _apply_chart(self):
        left, top, right, bottom = self.plot
        t1 = max(self.t_end.x, 1e-6)
        t0 = max(0.0, t1 - CHART_SECONDS)
        ymax = max(self.y_max.x, 1e-6)
        X = lambda v: left + (v - t0) / max(t1 - t0, 1e-6) * (right - left)
        Y = lambda v: bottom - v / ymax * (bottom - top)
        pts = [list(p) for p in self.history if p[0] >= t0 - 1e-6]
        if pts and self.history and pts[-1] is not None:
            last = self.history[-1]
            if pts[-1][0] == last[0]:
                if not math.isnan(last[1]):
                    pts[-1][1] = self.newest[0].x
                if not math.isnan(last[2]):
                    pts[-1][2] = self.newest[1].x
        xs = [X(p[0]) for p in pts]
        self.res_line.set_data(xs, [Y(p[1]) for p in pts])
        self.ram_line.set_data(xs, [Y(p[2]) for p in pts])
        ram = [(X(p[0]), Y(p[2])) for p in pts if not math.isnan(p[2])]
        if len(ram) >= 2:
            self.ram_fill.set_xy(ram + [(ram[-1][0], bottom), (ram[0][0], bottom)])
            self.ram_fill.set_visible(True)
        else:
            self.ram_fill.set_visible(False)
        # light horizontal grid lines with their values
        min_gap = 2.0 * self.lh("caption")     # labels never closer than two caption lines
        step = nice_step(max(ymax / 4, ymax * min_gap / max(bottom - top, 1.0)))
        gx, gy, labels = [], [], []
        v = 0.0
        while v <= ymax + 1e-9 and len(labels) < len(self.grid_labels):
            gx += [left, right, float("nan")]
            gy += [Y(v), Y(v), float("nan")]
            labels.append((v, Y(v)))
            v += step
        self.grid.set_data(gx, gy)
        for n, text in enumerate(self.grid_labels):
            if n < len(labels):
                text.set_text(f"{labels[n][0]:g} MiB")
                text.set_position((left - 8 * self.s, labels[n][1]))
            else:
                text.set_text("")
        # seconds along the bottom
        tstep = max(nice_step((t1 - t0) / 5), 1.0)
        first = math.ceil(t0 / tstep) * tstep
        ticks = [first + n * tstep for n in range(len(self.x_labels)) if first + n * tstep <= t1 + 1e-9]
        cap = self.lh("caption")
        for n, text in enumerate(self.x_labels):
            if n < len(ticks):
                text.set_text(f"{ticks[n]:g} s")
                text.set_position((X(ticks[n]), bottom + 6 * self.s + cap / 2))
            else:
                text.set_text("")
        # each line is named at its right end (no boxed legend), with a dot on its newest point
        ends = {}
        for key, idx in (("res", 1), ("ram", 2)):
            vals = [p for p in pts if not math.isnan(p[idx])]
            ends[key] = Y(vals[-1][idx]) if vals else None
            self.end_dots[key].set_visible(bool(vals))
            if vals:
                self.end_dots[key].set_center((X(vals[-1][0]), Y(vals[-1][idx])))
        lab = self.lh("label")
        if ends["res"] is not None and ends["ram"] is not None and abs(ends["res"] - ends["ram"]) < lab:
            mid = (ends["res"] + ends["ram"]) / 2
            ends["res"], ends["ram"] = mid - lab / 2, mid + lab / 2   # reserved is the upper line
        for key, text in self.end_labels.items():
            text.set_visible(ends[key] is not None)
            if ends[key] is not None:
                text.set_position((right + 8 * self.s, min(max(ends[key], top), bottom)))

    def card_artists(self):
        """Everything that moves, card by card, each list in drawing order (lowest zorder
        first). The cards do not overlap, so each one can be redrawn on its own."""
        banner = [self.banner] + list(self.dots)
        for layer in self.layers:
            banner += list(layer.values())
        left = [self.message, self.mode_caption, self.focus_ring] + self.focus_glow
        for item in self.items.values():
            left += item.artists()
        for pill in self.pills.values():
            left += [pill.box, pill.label]
        chart = [self.grid] + self.grid_labels + self.x_labels
        chart += [self.ram_fill, self.res_line, self.ram_line] + list(self.end_labels.values())
        chart += list(self.end_dots.values())
        cards = {"title": [self.proc, self.halo, self.live_dot, self.live], "banner": banner,
                 "left": left, "chart": chart,
                 "metrics": self.hero + [self.bar_fill, self.bar_label, self.faults, self.threads,
                                         self.ram_glow, self.ram_pill_box, self.ram_pill_label]}
        for name, arts in cards.items():
            arts.sort(key=lambda a: a.get_zorder())
            cards[name] = [a for a in arts if a.get_visible()
                           and (a.get_alpha() is None or a.get_alpha() > 0.01)
                           and not (hasattr(a, "get_text") and not a.get_text())]
        return cards

    # ---- blitting (fast mode) ----------------------------------------------------------

    def _card_pixels(self, name, renderer):
        """A card's rectangle in renderer pixels (top-left origin, as Agg regions use),
        widened by 5 units for shadows and the pulsing dot; the gaps between cards are
        12 units or more, so the widened rectangles still never overlap."""
        x, y, w, h = self.L[name]
        pad = 5 * self.s
        fx, fy = renderer.width / self.L["W"], renderer.height / self.L["H"]
        return (max(int((x - pad) * fx), 0), max(int((y - pad) * fy), 0),
                min(int(math.ceil((x + w + pad) * fx)), int(renderer.width)),
                min(int(math.ceil((y + h + pad) * fy)), int(renderer.height)))

    def _on_draw(self, event):
        """After a full draw: save the background (no animated artists), then draw them."""
        canvas = self.fig.canvas
        self.background = canvas.copy_from_bbox(self.fig.bbox)
        renderer = canvas.get_renderer()
        if renderer.dpi not in self.warmed:   # the fonts of the moving texts, every size
            from matplotlib.font_manager import FontProperties
            props = [FontProperties(size=self.pt(key), weight=weight)
                     for key in ("label", "caption", "phase", "hero", "focus")
                     for weight in ("normal", "semibold")]
            self.text_cache.warm(props, renderer.dpi, WARM_CHARS)
            self.warmed.add(renderer.dpi)
        self.drawn_looks = {}
        for name, arts in self.card_artists().items():
            for a in arts:
                a.draw(renderer)
            self.drawn_looks[name] = [artist_look(a) for a in arts]

    def render(self, force=False):
        """Draw one frame: a full draw when the background changed, else a blit that
        redraws only the cards whose moving artists look different from last frame (the
        canvas still holds last frame's pixels for the others)."""
        canvas = self.fig.canvas
        self.apply()
        if self.need_full_draw or self.background is None:
            self.need_full_draw = False
            canvas.draw()                      # fires draw_event -> _on_draw
            return
        from matplotlib.transforms import Bbox
        renderer = canvas.get_renderer()
        for name, arts in self.card_artists().items():
            looks = [artist_look(a) for a in arts]
            if looks == self.drawn_looks.get(name):
                continue
            x0, y0, x1, y1 = self._card_pixels(name, renderer)
            canvas.restore_region(self.background, bbox=(x0, y0, x1, y1),
                                  xy=(0, 0))   # xy: where the saved region's corner goes
            for a in arts:
                a.draw(renderer)
            self.drawn_looks[name] = looks
            # only this card goes to the screen (blit counts y from the bottom)
            canvas.blit(Bbox.from_extents(x0, renderer.height - y1, x1, renderer.height - y0))

    def tick(self):
        """The 30 fps timer: read a new sample if there is one, move, draw if needed."""
        now = time.monotonic()
        dt = min(now - self.last_tick, DT_MAX)
        self.last_tick = now
        try:
            snap = self.snapshots.get_nowait()
        except queue.Empty:
            snap = None
        if snap is not None:
            self.set_targets(snap)
        moving = self.step(dt) if self.animate else False
        if moving or self.dirty or self.need_full_draw or not self.drawn_settled:
            self.render()
            self.dirty = False
            self.drawn_settled = not moving    # draw the settled state once, then rest


# ======================================================================================
# Running it
# ======================================================================================

def load_pyplot(headless):
    """Import pyplot with a working backend, or exit with an install hint."""
    try:
        import matplotlib
    except ImportError:
        sys.exit("matplotlib is missing: sudo pacman -S python-matplotlib   "
                 "(Ubuntu: sudo apt install python3-matplotlib)")
    if headless:
        matplotlib.use("Agg")                  # no window: only write PNG files
    matplotlib.rcParams["toolbar"] = "None"    # no toolbar and no coordinate readout
    for key in [k for k in matplotlib.rcParams if k.startswith("keymap.")]:
        matplotlib.rcParams[key] = []          # our own keys only (Space, F, R, Q, Esc)
    import matplotlib.pyplot as plt
    if not headless:
        if "MPLBACKEND" not in os.environ:
            try:
                plt.switch_backend("QtAgg")    # native Wayland window through Qt
            except ImportError:
                pass                           # keep matplotlib's own default choice
        if plt.get_backend().lower() in NON_GUI_BACKENDS:
            print(BACKEND_HINT)
            sys.exit(1)
    return plt


def make_figure(plt, pid):
    fig = plt.figure(figsize=WINDOW_INCHES, dpi=UNIT)
    try:
        fig.canvas.manager.set_window_title(f"vmv - PID {pid}")
    except AttributeError:
        pass                                   # headless canvases have no window
    return fig


class Window:
    """The live window: owns the sampler (which R replaces) and handles the keys."""

    def __init__(self, plt, args):
        self.plt, self.args = plt, args
        self.sampler = self.new_sampler()
        self.fig = make_figure(plt, self.sampler.pid or "?")
        self.scene = Scene(self.fig, fast=True, animate=not args.no_anim, theme=args.theme,
                           scale=args.scale, demo=args.demo)
        self.scene.snapshots = self.sampler.snapshots
        self.timer = self.fig.canvas.new_timer(interval=FRAME_MS)
        self.timer.add_callback(self.scene.tick)
        self.fig.canvas.mpl_connect("key_press_event", lambda e: self.handle_key(e.key))
        self.fig.canvas.mpl_connect("button_press_event", self.handle_click)
        self.fig.canvas.mpl_connect("close_event", self.on_close)

    def new_sampler(self):
        a = self.args
        return Sampler(pid=a.pid, demo_args=(3, 2) if a.demo else None)

    def handle_click(self, event):
        """Click a block: pin the focus on it; click the empty column: unpin."""
        if event.inaxes is not None:
            return self.scene.click(event.xdata, event.ydata)
        return None

    def handle_key(self, key):
        """Space pause/resume, F fullscreen, R restart the demo, O auto focus on/off,
        Q or Esc quit."""
        key = (key or "").lower()
        if key == " ":
            paused = not self.scene.paused
            (self.sampler.paused.set if paused else self.sampler.paused.clear)()
            self.scene.set_paused(paused)
        elif key == "f":
            self.fig.canvas.manager.full_screen_toggle()
        elif key == "o":
            self.scene.toggle_auto()
        elif key in ("q", "escape"):
            self.plt.close(self.fig)
        elif key == "r" and self.args.demo:
            self.sampler.stop()                # its thread stops and reaps the old demo
            self.sampler = self.new_sampler()
            self.scene.reset()
            self.scene.set_paused(False)
            self.scene.snapshots = self.sampler.snapshots
            self.sampler.start()
        return key

    def on_close(self, _event):
        self.timer.stop()
        self.sampler.stop_event.set()          # the sampler thread cleans up the demo

    def run(self):
        self.sampler.start()
        self.timer.start()
        self.plt.show()                        # Qt event loop until the window is closed


def run_files(plt, args, sampler, png=None, frames=None):
    """Saved files: every frame is a settled frame drawn the normal matplotlib way."""
    fig = make_figure(plt, sampler.pid)
    scene = Scene(fig, fast=False, animate=False, theme=args.theme, scale=args.scale,
                  demo=args.demo)
    sampler.start()
    n = 0
    while True:
        try:
            snap = sampler.snapshots.get(timeout=30)
        except queue.Empty:
            print("no sample from bin/vmv for 30 s - giving up")
            return
        scene.set_targets(snap)
        if png:
            fig.savefig(png, dpi=UNIT)
            print(f"Wrote {png}")
            return
        fig.savefig(os.path.join(frames, f"frame_{n:03d}.png"), dpi=UNIT)
        n += 1
        if not snap["running"]:
            print(f"Wrote {n} frames to {frames} ({snap['status']})")
            return


def run_bench(plt, args, frames=150, frames_per_sample=15):
    """Record a vmv_demo run, then time 150 animated frames offscreen (Agg)."""
    print("recording a bin/vmv_demo run (about 15 s) ...")
    sampler = Sampler(demo_args=(1, 0), period=0.5)
    sampler.start()
    recorded = []
    while True:
        snap = sampler.snapshots.get(timeout=30)
        recorded.append(snap)
        if not snap["running"]:
            break
    sampler.stop()
    fig = make_figure(plt, recorded[0]["pid"])
    scene = Scene(fig, fast=True, animate=True, theme=args.theme, scale=args.scale, demo=True)
    scene.set_targets(recorded[0])
    scene.render()                             # first full draw: background + text cache
    times, full_draws = [], 0
    for f in range(frames):
        t0 = time.perf_counter()
        if f % frames_per_sample == 0:         # a new sample every half second
            scene.set_targets(recorded[(f // frames_per_sample) % len(recorded)])
        scene.step(1.0 / 30)
        full_draws += scene.need_full_draw
        scene.render()                         # every frame is drawn (worst case)
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    p95 = times[int(0.95 * len(times)) - 1]
    print(f"recorded {len(recorded)} samples; {frames} frames, a new sample every "
          f"{frames_per_sample} frames, {full_draws} full redraws")
    print(f"frame time: avg {sum(times) / len(times):.1f} ms, p95 {p95:.1f} ms "
          f"(max {times[-1]:.1f} ms)")


def main():
    ap = argparse.ArgumentParser(description="Live matplotlib view of a process's address space.")
    ap.add_argument("pid", nargs="?", type=int, help="process to watch")
    ap.add_argument("--demo", action="store_true", help="start bin/vmv_demo and watch it")
    ap.add_argument("--no-anim", action="store_true", help="no animation: values jump")
    ap.add_argument("--theme", choices=sorted(THEMES), default="light", help="colours")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply every size (1.25 for projectors)")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--png", metavar="FILE", help="save one frame and exit (no window)")
    out.add_argument("--frames", metavar="DIR", help="save every sample as DIR/frame_NNN.png")
    out.add_argument("--bench", action="store_true", help="time 150 animated frames offscreen")
    args = ap.parse_args()
    if not args.bench and ((args.pid is None) == (not args.demo)
                           or (args.pid is not None and args.pid <= 0)):
        ap.error("give exactly one of: a PID, or --demo")
    if not 0.5 <= args.scale <= 3.0:
        ap.error("--scale must be between 0.5 and 3")
    if not os.access(VMV, os.X_OK):
        sys.exit("bin/vmv not found: run ./build.sh first")
    plt = load_pyplot(headless=bool(args.png or args.frames or args.bench))
    if args.bench:
        run_bench(plt, args)
        return
    if args.frames:
        os.makedirs(args.frames, exist_ok=True)
    window = sampler = None
    try:
        if args.png or args.frames:
            sampler = Sampler(pid=args.pid, demo_args=(3, 2) if args.demo else None)
            run_files(plt, args, sampler, png=args.png, frames=args.frames)
        else:
            window = Window(plt, args)
            window.run()
    except KeyboardInterrupt:
        pass
    finally:
        if window is not None:
            sampler = window.sampler
        if sampler is not None:
            sampler.stop()                     # waits for the thread; it stops the demo


if __name__ == "__main__":
    main()
