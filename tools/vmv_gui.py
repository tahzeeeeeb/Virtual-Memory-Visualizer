#!/usr/bin/env python3
"""vmv_gui.py - live matplotlib window showing one process's virtual address space.

Display layer only: every number comes from `./bin/vmv csv <pid>`, read through a pipe.
Python never reads /proc itself; the C tool does all the OS work.

How the window draws:
  - a sampler thread runs `bin/vmv csv` once a second and hands each parsed snapshot to
    the window through a queue, so the window never waits for a subprocess;
  - every artist (block, label, line) is created once and afterwards only moved,
    faded or re-labelled;
  - a 30 fps timer moves each value along a critically damped spring towards its
    target, and only the animated artists are redrawn over a saved background (blit).

usage:
  python3 tools/vmv_gui.py <pid>                 live window, animated at 30 fps
  python3 tools/vmv_gui.py --demo                start bin/vmv_demo and watch it to the end
  python3 tools/vmv_gui.py <pid> --no-anim       live window, values jump (reduced motion)
  python3 tools/vmv_gui.py <pid> --png FILE      one settled frame, no window, then exit
  python3 tools/vmv_gui.py --demo --frames DIR   no window, every sample as frame_NNN.png
  python3 tools/vmv_gui.py --bench               time 150 animated frames offscreen
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
import textwrap
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
GROUP_COLOR = {"STACK": COLORS["STACK"], "HEAP": COLORS["HEAP"], "PROGRAM": COLORS["CODE"]}
GROUP_NOTE = {"STACK": "main thread stack, grows down",
              "HEAP": "brk heap: small malloc() calls grow it",
              "PROGRAM": "machine code, constants and globals (CODE RODATA DATA BSS)"}
# Which edge stays put when a block changes size: the stack grows down (its top edge
# stays), the heap grows up (its bottom edge stays); everything else scales about its centre.
ANCHOR = {"STACK": "top", "HEAP": "bottom"}
STATE_NAMES = {"R": "running", "S": "sleeping", "D": "disk wait", "T": "stopped",
               "t": "traced", "Z": "zombie", "X": "dead", "I": "idle"}
INFO_RE = re.compile(r"^# pid=(\d+);name=(.*?);state=(.);threads=(-?\d+);vmsize_kb=(-?\d+);"
                     r"vmrss_kb=(-?\d+);minflt=(\d+);majflt=(\d+);phase=(.*)$")

GROUP_MIN = 3.0      # smallest group block, in height units
SUB_MIN = 2.6        # smallest sub-block: room for one line of text
GAP_H = 1.6          # height of an "unused" band
X0, X1 = 0.33, 0.99  # left and right edge of the blocks (group names sit left of X0)
MONO_SIZE = 10.5     # info panel font size in points
LINE_PT = 1.2 * MONO_SIZE   # info panel line spacing in points


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
    """Split the memory-mapped area: top 4 files, anonymous memory, everything else."""
    files = collections.defaultdict(lambda: {"size": 0, "hi": 0, "kind": "FILE-MAP"})
    anon = {"key": "anon", "label": "anonymous (big malloc, thread stacks)",
            "size": 0, "hi": 0, "color": COLORS["ANON-MMAP"]}
    kernel = {"size": 0, "hi": 0}
    stacks = {"key": "threads", "size": 0, "hi": 0, "count": 0, "each": 0,
              "color": COLORS["THREAD-STACK?"]}
    for r in regions:
        if r["kind"] == "THREAD-STACK?":       # pthread stacks get their own sub-block
            stacks["size"] += r["size"]
            stacks["hi"] = max(stacks["hi"], r["end"])
            stacks["count"] += 1
            stacks["each"] = r["size"]
        elif r["kind"] in ("SHARED-LIB", "FILE-MAP"):
            f = files[os.path.basename(r["path"])]
            f["size"] += r["size"]
            f["hi"] = max(f["hi"], r["end"])
            f["kind"] = r["kind"]
        elif r["kind"] == "KERNEL-SPECIAL":    # [vdso], [vvar]
            kernel["size"] += r["size"]
            kernel["hi"] = max(kernel["hi"], r["end"])
        else:                                  # ANON-MMAP, GUARD
            anon["size"] += r["size"]
            anon["hi"] = max(anon["hi"], r["end"])
    ranked = sorted(files.items(), key=lambda kv: kv[1]["size"], reverse=True)
    subs = [{"key": "file:" + name, "label": name, "size": f["size"], "hi": f["hi"],
             "color": COLORS[f["kind"]]} for name, f in ranked[:4]]
    if stacks["count"]:
        stacks["label"] = f"thread stacks {stacks['count']} × {human(stacks['each'])}"
        subs.append(stacks)
    if anon["size"]:
        subs.append(anon)
    rest = ranked[4:]
    rest_size = sum(f["size"] for _, f in rest) + kernel["size"]
    if rest_size:
        parts = [f"{len(rest)} more files"] if rest else []
        parts += ["[vdso]/[vvar]"] if kernel["size"] else []
        subs.append({"key": "other", "label": "other: " + ", ".join(parts), "size": rest_size,
                     "hi": max([f["hi"] for _, f in rest] + [kernel["hi"]]),
                     "color": COLORS["KERNEL-SPECIAL"]})
    return sorted(subs, key=lambda s: s["hi"], reverse=True)   # high address first


def build_groups(regions):
    """The four groups that are present, top (high address) to bottom."""
    groups = []
    for name, kinds in GROUPS:
        rs = [r for r in regions if r["kind"] in kinds]
        if rs:
            groups.append({"name": name, "size": sum(r["size"] for r in rs),
                           "lo": min(r["start"] for r in rs), "hi": max(r["end"] for r in rs),
                           "subs": mmap_subblocks(rs) if name == "MEMORY-MAPPED AREA" else []})
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

    def run(self):
        proc = None
        try:
            if self.demo_args is not None:
                proc, self.pid = start_demo(*self.demo_args)
            w = Watcher(self.pid, proc)
            while not self.stop_event.is_set():
                t0 = time.monotonic()
                w.step()
                publish(self.snapshots, w.snapshot())
                if not w.running:
                    break
                self.stop_event.wait(max(0.0, self.period - (time.monotonic() - t0)))
        except RuntimeError as e:
            w = Watcher(self.pid)
            w.stop(str(e), gone=False)
            publish(self.snapshots, w.snapshot())
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

    def __init__(self, value):
        self.x = self.target = float(value)
        self.v = 0.0

    def step(self, dt):
        a = OMEGA * OMEGA * (self.target - self.x) - 2.0 * OMEGA * self.v
        self.v += a * dt
        self.x += self.v * dt

    def settled(self):
        tolerance = SETTLED * max(abs(self.target), 1.0)
        return abs(self.target - self.x) <= tolerance and abs(self.v) * RESPONSE <= tolerance

    def snap(self):
        self.x, self.v = self.target, 0.0


# ======================================================================================
# Fast text: matplotlib 3.11 shapes and rasterises every string again on every draw
# ======================================================================================

class TextBitmaps:
    """Rendered text kept as pixels, for the animated window only.

    matplotlib 3.11 shapes every string with libraqm and draws its glyphs again each
    time - 7 to 11 ms per label on our VM, far too slow for 30 frames a second. Each
    distinct (string, font, colour) is drawn once onto a scratch canvas with matplotlib
    itself; later draws blend those pixels into the frame (same pixels, within one
    colour level). Saved PNGs do not use this: they draw text the normal way."""

    MAX_ENTRIES = 4000

    def __init__(self):
        import numpy as np                     # matplotlib's own array library
        self.np = np
        self.store = {}

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

    def render(self, dpi, gc, s, prop):
        """Draw s once with matplotlib on a transparent scratch canvas and crop it."""
        from matplotlib.backends.backend_agg import RendererAgg
        np = self.np
        size = prop.get_size_in_points() * dpi / 72.0
        x0, y0 = int(2 * size) + 4, int(2.5 * size) + 4        # y counts from the top
        scratch = RendererAgg(int(len(s) * size + 4 * size) + 16, int(4 * size) + 16, dpi)
        g = scratch.new_gc()
        g.copy_properties(gc)
        g.set_alpha(1.0)                       # fading is applied when blending
        RendererAgg.draw_text(scratch, g, x0, y0, s, prop, 0)
        pixels = np.asarray(scratch.buffer_rgba())
        ink = pixels[..., 3]
        rows, cols = np.nonzero(ink.any(axis=1))[0], np.nonzero(ink.any(axis=0))[0]
        if len(rows) == 0:
            return None                        # only spaces
        crop = pixels[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1].astype(np.float32)
        coverage = crop[..., 3:4] / 255.0
        colour = crop[..., :3] * coverage + 0.5          # premultiplied, +0.5 rounds
        return colour, coverage, 1.0 - coverage, int(cols[0]) - x0, int(rows[0]) - y0

    def blend(self, renderer, gc, x, y, s, prop):
        """Blend the cached pixels of s at the position matplotlib asked for.

        The "over" rule of alpha compositing, as Agg itself does it for text. The figure
        background is opaque, so the alpha channel of the frame stays 255 and is skipped."""
        hit = self.bitmap(renderer, gc, s, prop)
        if hit is None:
            return
        colour, coverage, keep, dx, dy = hit
        frame = self.np.asarray(renderer.buffer_rgba())
        top, left = int(round(y)) + dy, int(round(x)) + dx
        h, w = coverage.shape[:2]
        t0, l0 = max(top, 0), max(left, 0)
        t1, l1 = min(top + h, frame.shape[0]), min(left + w, frame.shape[1])
        if t0 >= t1 or l0 >= l1:
            return
        rows, cols = slice(t0 - top, t1 - top), slice(l0 - left, l1 - left)
        dst = frame[t0:t1, l0:l1, :3]
        fade = gc.get_alpha()
        if fade is None or fade >= 0.999:
            dst[...] = dst * keep[rows, cols] + colour[rows, cols]
        else:                                            # fading in or out
            a = coverage[rows, cols] * fade
            dst[...] = dst * (1.0 - a) + (colour[rows, cols] - 0.5) * fade + 0.5


def make_text_classes():
    """FastText and NumberField need matplotlib, imported only after the backend is chosen."""
    from matplotlib.text import Text

    class FastText(Text):
        """A matplotlib Text that is cheap to redraw in the animated window.

        Text.draw redoes its layout work (fonts, metrics, alignment) on every draw. Here
        the draw_text calls for the current string are recorded once, relative to the
        text's anchor point, and later frames blend the cached pixels at the new anchor.
        Saved PNGs (no text cache) draw the normal way."""

        _recorded = None                       # (key, [(dx, dy, line), ...])

        def _anchor(self, renderer):
            x, y = self.get_transform().transform(self.get_unitless_position())
            return x, renderer.height - y      # draw_text's y counts from the top

        def _record(self, renderer):
            key = (id(renderer), self.get_text(), self.get_ha(), self.get_va(),
                   hash(self.get_fontproperties()))
            if self._recorded is None or self._recorded[0] != key:
                calls, cached = [], renderer.draw_text
                renderer.draw_text = lambda gc, x, y, s, *rest, **kw: calls.append((x, y, s))
                try:
                    Text.draw(self, renderer)  # matplotlib does the layout, we only listen
                finally:
                    renderer.draw_text = cached
                ax, ay = self._anchor(renderer)
                self._recorded = (key, [(x - ax, y - ay, s) for x, y, s in calls])
            return self._recorded[1]

        def _gc(self, renderer):
            gc = renderer.new_gc()
            gc.set_foreground(self.get_color())
            gc.set_alpha(self.get_alpha())
            gc.set_antialiased(self.get_antialiased())
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

    class NumberField(FastText):
        """A right-aligned number in a fixed-width monospace field.

        Counting numbers produce a new string every frame, and laying out a new string is
        slow here. So in the animated window the field is drawn digit by digit from the
        text cache - monospace keeps every digit on a fixed grid."""

        def __init__(self, width, **kw):
            super().__init__(0, 1, "", **kw)
            self.width = width
            self._grid_key, self._grid = None, None

        def set_value(self, s):
            self.set_text(f"{s:>{self.width}}")

        def _digit_grid(self, renderer):
            """Offset of the first digit from the anchor, and the width of one digit."""
            prop = self.get_fontproperties()
            key = (id(renderer), self.get_ha(), self.get_va(), hash(prop))
            if key != self._grid_key:
                text = self.get_text()
                self.set_text("0" * self.width)
                (dx, dy, _), = self._record(renderer)
                self.set_text(text)
                w_all = renderer.get_text_width_height_descent("0" * self.width, prop, False)[0]
                w_one = renderer.get_text_width_height_descent("0", prop, False)[0]
                step = (w_all - w_one) / (self.width - 1) if self.width > 1 else w_one
                self._grid_key, self._grid = key, (dx, dy, step)
            return self._grid

        def draw(self, renderer):
            cache = getattr(renderer, "_vmv_text_cache", None)
            if cache is None or not self.get_visible():
                return Text.draw(self, renderer)
            dx, dy, step = self._digit_grid(renderer)
            ax, ay = self._anchor(renderer)
            gc, prop = self._gc(renderer), self.get_fontproperties()
            for i, ch in enumerate(self.get_text()):
                if ch != " ":
                    cache.blend(renderer, gc, ax + dx + i * step, ay + dy, ch, prop)
            gc.restore()
            self.stale = False

    return FastText, NumberField


# ======================================================================================
# Layout: where every item of the map should be (the layout of the first GUI version)
# ======================================================================================

def anchor_of(y, h, mode):
    """The edge (or centre) of a block that stays put while its height changes."""
    return y + h if mode == "top" else y if mode == "bottom" else y + h / 2


def bottom_of(anchor, h, mode):
    return anchor - h if mode == "top" else anchor if mode == "bottom" else anchor - h / 2


def empty_message(snap):
    if snap["gone"]:
        return "the process is gone:\nnothing is left of it"
    if snap["info"] is not None and snap["info"]["state"] == "Z":
        return "address space released at exit:\nthe zombie keeps only its process-table entry"
    if not snap["running"]:
        return "the memory map could not be read"
    return "no user address space"


def map_targets(snap):
    """Target spec of every map item, bottom-up in height units.

    Returns ({key: spec}, total height, message or None)."""
    groups, prev = snap["groups"], snap["prev_sizes"]
    if not groups:
        return {}, None, empty_message(snap)
    specs = {}

    def gap(key, y, size, note=""):
        specs[key] = {"kind": "gap", "y": y, "h": GAP_H, "mode": "center",
                      "inner": f"{human(size)} unused{note}"}

    items = list(reversed(groups))             # bottom-up: PROGRAM first
    y = 0.0
    gap("gap:low", y, items[0]["lo"], " (low addresses, NULL page)")
    y += GAP_H
    for i, g in enumerate(items):
        if i > 0 and g["lo"] > items[i - 1]["hi"]:
            gap(f"gap:{items[i - 1]['name']}|{g['name']}", y, g["lo"] - items[i - 1]["hi"])
            y += GAP_H
        h = max(GROUP_MIN, math.log2(max(g["size"], 1)) - 9)   # 4 KiB -> 3, 128 MiB -> 18
        delta, grew = change_text("group:" + g["name"], g["size"], prev)
        margin = f"{g['name']}\n{human(g['size'])}{delta}"
        if g["subs"]:
            h = max(h, len(g["subs"]) * SUB_MIN)
            weights = [max(1.0, math.log2(max(s["size"], 1)) - 9) for s in g["subs"]]
            extra = h - len(g["subs"]) * SUB_MIN
            sy = y + h
            for s, wt in zip(g["subs"], weights):          # high address first, top down
                sh = SUB_MIN + extra * wt / sum(weights)
                sy -= sh
                sdelta, sgrew = change_text("sub:" + s["key"], s["size"], prev)
                specs["sub:" + s["key"]] = {"kind": "block", "y": sy, "h": sh, "mode": "center",
                                            "color": s["color"], "highlight": sgrew,
                                            "inner": f"{s['label']}   {human(s['size'])}{sdelta}"}
            specs["group:" + g["name"]] = {"kind": "label", "y": y, "h": h, "mode": "center",
                                           "margin": margin}
        else:
            specs["group:" + g["name"]] = {"kind": "block", "y": y, "h": h,
                                           "mode": ANCHOR.get(g["name"], "center"),
                                           "color": GROUP_COLOR[g["name"]], "highlight": grew,
                                           "inner": GROUP_NOTE[g["name"]], "margin": margin}
        y += h
    gap("gap:top", y, USER_TOP - items[-1]["hi"], " (up to the user/kernel boundary)")
    return specs, y + GAP_H, None


def nice_ceiling(v):
    """Smallest 1, 2 or 5 x 10^k that is >= v (stable chart limits in the window)."""
    k = 10.0 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if m * k >= v:
            return m * k
    return 10 * k


def chart_limits(history, stepped):
    """x and y limits of the chart. Saved PNGs use the exact limits of the first GUI
    version; the window rounds them up to steps, so the axes (and the saved background
    behind the animation) only change now and then instead of every second."""
    last = history[-1][0] if history else 1
    values = [v for h in history for v in h[1:] if not math.isnan(v)]
    if not stepped:
        return ((max(1, last - HISTORY + 1) - 0.5, max(last, 10) + 0.5),
                (0, max(values) * 1.3 if values else 1))
    right = max(10, -(-last // 10) * 10)
    top = nice_ceiling(max(values) * 1.3) if values and max(values) > 0 else 1
    return ((max(1, right - HISTORY + 1) - 0.5, right + 0.5), (0, top))


# ======================================================================================
# The scene: every artist is created once and then only updated
# ======================================================================================

class MapItem:
    """One thing on the map - a block, a dashed "unused" band or a group name - with
    springs for its anchor edge, its height and its opacity."""

    def __init__(self, scene, spec, grow):
        from matplotlib.patches import Rectangle
        ax, fast = scene.ax_map, scene.fast
        self.kind, self.mode, self.dying = spec["kind"], spec["mode"], False
        # a new item grows from zero height at its own address position and fades in
        self.anchor = Spring(anchor_of(spec["y"], spec["h"], self.mode))
        self.h = Spring(0.0 if grow else spec["h"])
        self.alpha = Spring(0.0 if grow else 1.0)
        self.rect = self.inner = self.margin = None
        if self.kind == "gap":
            self.rect = Rectangle((X0, 0), X1 - X0, 0, fill=False, linestyle="--",
                                  edgecolor="gray", linewidth=0.8, animated=fast)
            self.inner = scene.text(ax, fontsize=7.5, color="dimgray", ha="center", va="center")
        elif self.kind == "block":
            self.rect = Rectangle((X0, 0), X1 - X0, 0, edgecolor="black", animated=fast)
            self.inner = scene.text(ax, fontsize=8.5, zorder=4, ha="center", va="center")
        if spec.get("margin") is not None:
            self.margin = scene.text(ax, ha="right", va="center", fontsize=9, fontweight="bold")
        if self.rect is not None:
            ax.add_patch(self.rect)
        self.update(spec)

    def update(self, spec):
        """New targets from a new sample; the motion continues from where it is now."""
        self.dying = False
        self.anchor.target = anchor_of(spec["y"], spec["h"], self.mode)
        self.h.target, self.alpha.target = spec["h"], 1.0
        if self.kind == "block":
            self.rect.set_facecolor(spec["color"])
            self.rect.set_linewidth(3.2 if spec["highlight"] else 0.7)   # new or grew
            self.rect.set_zorder(3 if spec["highlight"] else 2)
        if self.inner is not None:
            self.inner.set_text(spec["inner"])
        if self.margin is not None:
            self.margin.set_text(spec["margin"])

    def die(self):
        """Removed: shrink to zero height and fade out at the same position."""
        self.dying = True
        self.h.target, self.alpha.target = 0.0, 0.0

    def springs(self):
        return (self.anchor, self.h, self.alpha)

    def apply(self):
        h = max(self.h.x, 0.0)
        y = bottom_of(self.anchor.x, h, self.mode)
        a = min(max(self.alpha.x, 0.0), 1.0)
        if self.rect is not None:
            self.rect.set_y(y)
            self.rect.set_height(h)
            self.rect.set_alpha(a)
        for text, x in ((self.inner, (X0 + X1) / 2), (self.margin, X0 - 0.02)):
            if text is not None:
                text.set_position((x, y + h / 2))
                text.set_alpha(a)

    def artists(self):
        return [a for a in (self.rect, self.inner, self.margin) if a is not None]

    def finished(self):
        return self.dying and self.alpha.x < 0.01 and self.h.x < 0.01

    def remove(self):
        for a in self.artists():
            a.remove()


class Scene:
    """The whole window: map, info panel and chart.

    fast=True  (window, --bench): artists are 'animated' and drawn over a saved
               background (blitting), text goes through TextBitmaps.
    fast=False (--png, --frames): normal matplotlib drawing for saved files.
    animate=False: every value jumps straight to its target (--no-anim, saved files)."""

    def __init__(self, fig, axes, fast, animate):
        from matplotlib.transforms import offset_copy
        self.fig, (self.ax_map, self.ax_info, self.ax_chart) = fig, axes
        self.fast, self.animate = fast, animate
        self.FastText, self.NumberField = make_text_classes()
        self.offset_copy = offset_copy
        self.items = {}                        # key -> MapItem
        self.view = Spring(1.0)                # height of the map in height units
        self.message_alpha = Spring(0.0)
        self.title = None
        self.background = None
        self.need_full_draw = True             # (fast) redraw everything, save a new background
        self.dirty = True                      # something changed that springs do not track
        self.drawn_settled = False
        self.snapshots = None
        self.last_tick = time.monotonic()
        self.text_cache = TextBitmaps() if fast else None
        self._make_map()
        self._make_info()
        self._make_chart()
        if fast:
            canvas = fig.canvas
            plain_get_renderer = canvas.get_renderer

            def get_renderer():                # every renderer the canvas makes gets the cache
                return self.text_cache.install(plain_get_renderer())

            canvas.get_renderer = get_renderer
            canvas.mpl_connect("draw_event", self._on_draw)

    # ---- construction ----------------------------------------------------------------

    def text(self, ax, x=0.0, y=0.0, s="", cls=None, **kw):
        """Add an animated text artist to ax (like ax.text, but a FastText)."""
        t = (cls or self.FastText)(x, y, s, animated=self.fast, **kw)
        ax.add_artist(t)
        t.set_clip_on(False)
        return t

    def _make_map(self):
        ax = self.ax_map
        ax.set_axis_off()
        ax.set_title("Address space (high addresses at the top)", fontsize=11)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        self.message = self.text(ax, 0.5, 0.5, ha="center", va="center", fontsize=12,
                                 color="dimgray", transform=ax.transAxes, alpha=0.0)

    def _make_info(self):
        from matplotlib.font_manager import FontProperties, findfont, get_font
        ax = self.ax_info
        ax.set_axis_off()
        font = get_font(findfont(FontProperties(family="monospace")))
        font.set_size(MONO_SIZE, 72)                       # at 72 dpi one pixel is one point
        self.char_pt = font.load_char(ord("0")).linearHoriAdvance / 65536.0
        mono = {"family": "monospace", "fontsize": MONO_SIZE, "va": "top", "ha": "left"}
        self.rows = [self.text(ax, 0, 1, transform=self._at(0, r), **mono) for r in range(6)]
        self.extras = {k: self.text(ax, 0, 1, transform=self._at(0, 0), **mono)
                       for k in ("pct_open", "pct_close", "major")}
        widths = {"threads": 3, "vmsize": 10, "vmrss": 10, "pct": 5, "minflt": 7,
                  "majflt": 5, "sample": 4}
        self.fields = {}
        for name, width in widths.items():
            f = self.NumberField(width, animated=self.fast, transform=self._at(0, 0), **mono)
            ax.add_artist(f)
            f.set_clip_on(False)
            self.fields[name] = f
        self.numbers = {k: Spring(0.0) for k in ("threads", "vmsize", "vmrss", "minflt", "majflt")}
        self.phase = self.text(ax, 0.0, 0.50, va="top", ha="left", fontsize=11,
                               fontweight="bold", transform=ax.transAxes)
        self.status = self.text(ax, 0.0, 0.22, va="top", ha="left", fontsize=12,
                                fontweight="bold", color="#c00000", transform=ax.transAxes)
        self.info = None

    def _at(self, col, row):
        """Transform for monospace column col, line row of the info panel."""
        return self.offset_copy(self.ax_info.transAxes, fig=self.fig, x=col * self.char_pt,
                                y=-row * LINE_PT, units="points")

    def _make_chart(self):
        from matplotlib.ticker import MaxNLocator
        ax = self.ax_chart
        self.size_line, = ax.plot([], [], linewidth=2, marker=".", label="VmSize (reserved)",
                                  animated=self.fast)
        self.rss_line, = ax.plot([], [], linewidth=2, marker=".", label="VmRSS (in RAM)",
                                 animated=self.fast)
        ax.set_title(f"Memory over the last {HISTORY} samples", fontsize=11)
        ax.set_xlabel("sample (one per second)")
        ax.set_ylabel("MiB")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper left", fontsize=9)
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        self.history, self.newest, self.newest_n, self.limits = [], [Spring(0), Spring(0)], None, None
        self._set_chart_limits(chart_limits([], self.fast))

    def _set_chart_limits(self, limits):
        if limits != self.limits:
            self.limits = limits
            self.ax_chart.set_xlim(*limits[0])
            self.ax_chart.set_ylim(*limits[1])
            self.need_full_draw = True         # the axes changed: a new background is needed

    # ---- new sample: only targets change ---------------------------------------------

    def set_targets(self, snap):
        info = snap["info"]
        first = self.info is None and info is not None
        grow = self.animate
        name = f" ({info['name']})" if info else ""
        title = f"Virtual Memory Visualizer - PID {snap['pid']}{name}"
        if title != self.title:
            self.title = title
            self.fig.suptitle(title, fontsize=14, fontweight="bold")
            try:
                self.fig.canvas.manager.set_window_title(f"vmv - PID {snap['pid']}")
            except AttributeError:
                pass                           # headless canvases have no window
            self.need_full_draw = True

        specs, total, message = map_targets(snap)
        for key, spec in specs.items():
            if key in self.items:
                self.items[key].update(spec)
            else:
                self.items[key] = MapItem(self, spec, grow)
        for key, item in self.items.items():
            if key not in specs:
                item.die()
        if total is not None:
            self.view.target = total
            if first or not self.animate:
                self.view.snap()               # no zoom-in from nothing on the first sample
        if message:
            self.message.set_text(message)
        self.message_alpha.target = 1.0 if message else 0.0

        self._info_targets(snap, grow and first)
        self._chart_targets(snap["history"], grow)
        self.dirty = True
        if not self.animate:
            for s in self.springs():
                s.snap()
            self._drop_finished()
        self.apply()

    def _info_targets(self, snap, count_from_zero):
        i, fields, extras = snap["info"], self.fields, self.extras
        self.snap_info = snap
        for f in fields.values():
            f.set_visible(False)
        for t in list(extras.values()) + self.rows:
            t.set_text("")
        if i is None:
            self.rows[0].set_text(f"PID {snap['pid']}")
        else:
            self.rows[0].set_text(f"PID       {i['pid']}   ({i['name']})")
            if snap["gone"]:
                self.rows[1].set_text("State     -  (gone from the process table)")
            else:
                prefix = (f"State     {i['state']}  ({STATE_NAMES.get(i['state'], '?')})"
                          f"     Threads  ")
                self.rows[1].set_text(prefix)
                self._place("threads", len(prefix), 1)
            self.rows[2].set_text("Reserved (VmSize)   ")
            self.rows[3].set_text("In RAM   (VmRSS)    ")
            self.rows[4].set_text("Page faults   minor ")
            self.rows[5].set_text("Sample    ")
            self._place("vmsize", 20, 2)
            self._place("vmrss", 20, 3)
            if i["vmsize_kb"] > 0 and i["vmrss_kb"] >= 0:
                self._place("pct", 33, 3)
                self._extra("pct_open", "  (", 30, 3)
                self._extra("pct_close", " %)", 38, 3)
            self._place("minflt", 20, 4)
            self._extra("major", "    major ", 27, 4)
            self._place("majflt", 37, 4)
            self._place("sample", 10, 5)
            targets = {"threads": i["threads"], "vmsize": i["vmsize_kb"], "vmrss": i["vmrss_kb"],
                       "minflt": i["minflt"], "majflt": i["majflt"]}
            for k, v in targets.items():
                self.numbers[k].target = float(v)
                if count_from_zero:
                    self.numbers[k].x, self.numbers[k].v = 0.0, 0.0
            self.info = i
        shown = (i["phase"] if i else "") or (snap["last_phase"] + "  (last seen)"
                                             if snap["last_phase"] else "-")
        self.phase.set_text(textwrap.fill("Phase: " + shown, width=48) if i else "")
        self.status.set_text(textwrap.fill(snap["status"], width=42) if snap["status"] else "")

    def _place(self, field, col, row):
        f = self.fields[field]
        f.set_transform(self._at(col, row))
        f.set_visible(True)

    def _extra(self, name, text, col, row):
        t = self.extras[name]
        t.set_transform(self._at(col, row))
        t.set_text(text)

    def _chart_targets(self, history, grow):
        if history and history[-1][0] != self.newest_n:
            _, size, rss = history[-1]
            for spring, target in zip(self.newest, (size, rss)):
                start = spring.x if self.newest_n is not None else (0.0 if grow else target)
                if math.isnan(target):
                    start = target = 0.0       # no value (zombie): drawn as a gap, see apply
                spring.x, spring.v, spring.target = start, 0.0, target
            self.newest_n = history[-1][0]
        self.history = history
        self._set_chart_limits(chart_limits(history, stepped=self.fast))

    # ---- every frame -----------------------------------------------------------------

    def springs(self):
        for item in self.items.values():
            yield from item.springs()
        yield self.view
        yield self.message_alpha
        yield from self.numbers.values()
        yield from self.newest

    def step(self, dt):
        """Advance every spring; True while anything is still moving."""
        moving = False
        for s in self.springs():
            s.step(dt)
            if not s.settled():
                moving = True
        self._drop_finished()
        if not moving:
            for s in self.springs():
                s.snap()
        return moving

    def _drop_finished(self):
        for key in [k for k, item in self.items.items() if item.finished()]:
            self.items.pop(key).remove()

    def apply(self):
        """Copy the current spring values into the artists."""
        for item in self.items.values():
            item.apply()
        self.ax_map.set_ylim(0, max(self.view.x, 1e-6))
        self.message.set_alpha(min(max(self.message_alpha.x, 0.0), 1.0))
        if self.info is not None:
            n, f = self.numbers, self.fields
            f["threads"].set_value(str(int(round(n["threads"].x))))
            for k, kb in (("vmsize", self.info["vmsize_kb"]), ("vmrss", self.info["vmrss_kb"])):
                f[k].set_value(human(int(max(n[k].x, 0) * 1024)) if kb >= 0 else "n/a")
            if n["vmsize"].x > 0:
                f["pct"].set_value(f"{100.0 * n['vmrss'].x / n['vmsize'].x:.1f}")
            f["minflt"].set_value(str(int(round(n["minflt"].x))))
            f["majflt"].set_value(str(int(round(n["majflt"].x))))
            f["sample"].set_value(str(self.snap_info["sample_no"]))
        xs = [h[0] for h in self.history]
        sizes = [h[1] for h in self.history]
        rsss = [h[2] for h in self.history]
        if self.history:                       # the newest point moves along its spring
            if not math.isnan(sizes[-1]):
                sizes[-1] = self.newest[0].x
            if not math.isnan(rsss[-1]):
                rsss[-1] = self.newest[1].x
        self.size_line.set_data(xs, sizes)
        self.rss_line.set_data(xs, rsss)

    def animated_artists(self):
        """Everything that moves, in drawing order (lowest zorder first)."""
        arts = [a for item in self.items.values() for a in item.artists()]
        arts.sort(key=lambda a: a.get_zorder())
        arts.append(self.message)
        arts += self.rows + list(self.extras.values()) + list(self.fields.values())
        arts += [self.phase, self.status, self.size_line, self.rss_line]
        return [a for a in arts if a.get_visible() and (a.get_alpha() is None or a.get_alpha() > 0.01)]

    # ---- blitting (fast mode) ----------------------------------------------------------

    def _on_draw(self, event):
        """After a full draw: save the background (no animated artists), then draw them."""
        canvas = self.fig.canvas
        self.background = canvas.copy_from_bbox(self.fig.bbox)
        renderer = canvas.get_renderer()
        for a in self.animated_artists():
            a.draw(renderer)

    def render(self, force=False):
        """Draw one frame: a full draw when the background changed, else a blit."""
        canvas = self.fig.canvas
        self.apply()
        if self.need_full_draw or self.background is None:
            self.need_full_draw = False
            canvas.draw()                      # fires draw_event -> _on_draw
            return
        canvas.restore_region(self.background)
        renderer = canvas.get_renderer()
        for a in self.animated_artists():
            a.draw(renderer)
        canvas.blit(self.fig.bbox)

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
    fig = plt.figure(figsize=(13, 8))
    gs = fig.add_gridspec(2, 2, width_ratios=[3, 2], left=0.02, right=0.97, top=0.92,
                          bottom=0.08, wspace=0.12, hspace=0.35)
    axes = (fig.add_subplot(gs[:, 0]), fig.add_subplot(gs[0, 1]), fig.add_subplot(gs[1, 1]))
    try:
        fig.canvas.manager.set_window_title(f"vmv - PID {pid}")
    except AttributeError:
        pass                                   # headless canvases have no window
    return fig, axes


def run_window(plt, sampler, animate):
    fig, axes = make_figure(plt, sampler.pid if sampler.pid else "?")
    scene = Scene(fig, axes, fast=True, animate=animate)
    scene.snapshots = sampler.snapshots
    timer = fig.canvas.new_timer(interval=FRAME_MS)
    timer.add_callback(scene.tick)

    def on_close(_event):
        timer.stop()
        sampler.stop_event.set()               # the sampler thread cleans up the demo

    fig.canvas.mpl_connect("close_event", on_close)
    sampler.start()
    timer.start()
    plt.show()                                 # Qt event loop until the window is closed


def run_files(plt, sampler, png=None, frames=None):
    """Saved files: every frame is a settled frame drawn the normal matplotlib way."""
    fig, axes = make_figure(plt, sampler.pid)
    scene = Scene(fig, axes, fast=False, animate=False)
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
            fig.savefig(png, dpi=100)
            print(f"Wrote {png}")
            return
        fig.savefig(os.path.join(frames, f"frame_{n:03d}.png"), dpi=100)
        n += 1
        if not snap["running"]:
            print(f"Wrote {n} frames to {frames} ({snap['status']})")
            return


def run_bench(plt, frames=150, frames_per_sample=15):
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
    fig, axes = make_figure(plt, recorded[0]["pid"])
    scene = Scene(fig, axes, fast=True, animate=True)
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
          f"{frames_per_sample} frames, {full_draws} full redraws (chart limits/title)")
    print(f"frame time: avg {sum(times) / len(times):.1f} ms, p95 {p95:.1f} ms "
          f"(max {times[-1]:.1f} ms)")


def main():
    ap = argparse.ArgumentParser(description="Live matplotlib view of a process's address space.")
    ap.add_argument("pid", nargs="?", type=int, help="process to watch")
    ap.add_argument("--demo", action="store_true", help="start bin/vmv_demo and watch it")
    ap.add_argument("--no-anim", action="store_true", help="no animation: values jump")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--png", metavar="FILE", help="save one frame and exit (no window)")
    out.add_argument("--frames", metavar="DIR", help="save every sample as DIR/frame_NNN.png")
    out.add_argument("--bench", action="store_true", help="time 150 animated frames offscreen")
    args = ap.parse_args()
    if not args.bench and ((args.pid is None) == (not args.demo)
                           or (args.pid is not None and args.pid <= 0)):
        ap.error("give exactly one of: a PID, or --demo")
    if not os.access(VMV, os.X_OK):
        sys.exit("bin/vmv not found: run ./build.sh first")
    plt = load_pyplot(headless=bool(args.png or args.frames or args.bench))
    if args.bench:
        run_bench(plt)
        return
    if args.frames:
        os.makedirs(args.frames, exist_ok=True)
    sampler = Sampler(pid=args.pid, demo_args=(3, 2) if args.demo else None)
    try:
        if args.png or args.frames:
            run_files(plt, sampler, png=args.png, frames=args.frames)
        else:
            run_window(plt, sampler, animate=not args.no_anim)
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()                         # waits for the thread; it stops the demo


if __name__ == "__main__":
    main()
