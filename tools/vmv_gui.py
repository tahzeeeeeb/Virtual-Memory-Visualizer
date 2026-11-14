#!/usr/bin/env python3
"""vmv_gui.py - live matplotlib window showing one process's virtual address space.

Display layer only: every number comes from `./bin/vmv csv <pid>`, read through a pipe.
Python never reads /proc itself; the C tool does all the OS work.

usage:
  python3 tools/vmv_gui.py <pid>                 live window, refreshed every second
  python3 tools/vmv_gui.py --demo                start bin/vmv_demo and watch it to the end
  python3 tools/vmv_gui.py <pid> --png FILE      one frame, no window, then exit
  python3 tools/vmv_gui.py --demo --frames DIR   no window, every sample saved as frame_NNN.png
"""
import argparse
import collections
import csv
import math
import os
import re
import subprocess
import sys
import textwrap
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
STATE_NAMES = {"R": "running", "S": "sleeping", "D": "disk wait", "T": "stopped",
               "t": "traced", "Z": "zombie", "X": "dead", "I": "idle"}
INFO_RE = re.compile(r"^# pid=(\d+);name=(.*?);state=(.);threads=(-?\d+);vmsize_kb=(-?\d+);"
                     r"vmrss_kb=(-?\d+);minflt=(\d+);majflt=(\d+);phase=(.*)$")

GROUP_MIN = 3.0      # smallest group block, in height units
SUB_MIN = 2.6        # smallest sub-block: room for one line of text
GAP_H = 1.6          # height of an "unused" band
X0, X1 = 0.33, 0.99  # left and right edge of the blocks (group names sit left of X0)


def human(n):
    """Same text as format_size() in src/render.c: 512 B, 4.0 KiB, 1.5 MiB ..."""
    if n < 1024:
        return f"{n} B"
    value, units, u = n / 1024.0, ["KiB", "MiB", "GiB", "TiB", "PiB", "EiB"], 0
    while value >= 1024.0 and u < len(units) - 1:
        value, u = value / 1024.0, u + 1
    return f"{value:.1f} {units[u]}"


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


def draw_block(ax, y, h, color, label, highlight, fontsize=8.5):
    from matplotlib.patches import Rectangle
    ax.add_patch(Rectangle((X0, y), X1 - X0, h, facecolor=color, edgecolor="black",
                           linewidth=3.2 if highlight else 0.7, zorder=3 if highlight else 2))
    ax.text((X0 + X1) / 2, y + h / 2, label, ha="center", va="center", fontsize=fontsize, zorder=4)


def draw_gap(ax, y, size, note=""):
    from matplotlib.patches import Rectangle
    ax.add_patch(Rectangle((X0, y), X1 - X0, GAP_H, fill=False, linestyle="--",
                           edgecolor="gray", linewidth=0.8))
    ax.text((X0 + X1) / 2, y + GAP_H / 2, f"{human(size)} unused{note}", ha="center",
            va="center", fontsize=7.5, color="dimgray")


def draw_map(ax, w):
    ax.clear()
    ax.set_axis_off()
    ax.set_title("Address space (high addresses at the top)", fontsize=11)
    if not w.groups:
        if w.gone:
            msg = "the process is gone:\nnothing is left of it"
        elif w.info is not None and w.info["state"] == "Z":
            msg = "address space released at exit:\nthe zombie keeps only its process-table entry"
        elif not w.running:
            msg = "the memory map could not be read"
        else:
            msg = "no user address space"
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=12, color="dimgray")
        return
    items = list(reversed(w.groups))           # draw bottom-up: PROGRAM first
    y = 0.0
    draw_gap(ax, y, items[0]["lo"], " (low addresses, NULL page)")
    y += GAP_H
    for i, g in enumerate(items):
        if i > 0 and g["lo"] > items[i - 1]["hi"]:
            draw_gap(ax, y, g["lo"] - items[i - 1]["hi"])
            y += GAP_H
        h = max(GROUP_MIN, math.log2(max(g["size"], 1)) - 9)   # 4 KiB -> 3, 128 MiB -> 18
        delta, grew = change_text("group:" + g["name"], g["size"], w.prev_sizes)
        if g["subs"]:
            h = max(h, len(g["subs"]) * SUB_MIN)
            weights = [max(1.0, math.log2(max(s["size"], 1)) - 9) for s in g["subs"]]
            extra = h - len(g["subs"]) * SUB_MIN
            sy = y + h
            for s, wt in zip(g["subs"], weights):          # high address first, top down
                sh = SUB_MIN + extra * wt / sum(weights)
                sy -= sh
                sdelta, sgrew = change_text("sub:" + s["key"], s["size"], w.prev_sizes)
                draw_block(ax, sy, sh, s["color"], f"{s['label']}   {human(s['size'])}{sdelta}", sgrew)
        else:
            draw_block(ax, y, h, GROUP_COLOR[g["name"]], GROUP_NOTE[g["name"]], grew)
        ax.text(X0 - 0.02, y + h / 2, f"{g['name']}\n{human(g['size'])}{delta}", ha="right",
                va="center", fontsize=9, fontweight="bold")
        y += h
    draw_gap(ax, y, USER_TOP - items[-1]["hi"], " (up to the user/kernel boundary)")
    y += GAP_H
    ax.set_xlim(0, 1)
    ax.set_ylim(0, y)


def draw_info(ax, w):
    ax.clear()
    ax.set_axis_off()
    i = w.info
    if i is not None:
        size, rss = i["vmsize_kb"], i["vmrss_kb"]
        pct = f"  ({100.0 * rss / size:.1f} %)" if size > 0 and rss >= 0 else ""
        state = ("-  (gone from the process table)" if w.gone
                 else f"{i['state']}  ({STATE_NAMES.get(i['state'], '?')})     Threads  {i['threads']}")
        lines = [f"PID       {i['pid']}   ({i['name']})",
                 f"State     {state}",
                 f"Reserved (VmSize)   {human(size * 1024) if size >= 0 else 'n/a'}",
                 f"In RAM   (VmRSS)    {human(rss * 1024) if rss >= 0 else 'n/a'}{pct}",
                 f"Page faults   minor {i['minflt']}    major {i['majflt']}",
                 f"Sample    {w.sample_no}"]
        ax.text(0.0, 1.0, "\n".join(lines), va="top", ha="left", family="monospace",
                fontsize=10.5, transform=ax.transAxes)
        shown = i["phase"] or (w.last_phase + "  (last seen)" if w.last_phase else "-")
        phase = textwrap.fill("Phase: " + shown, width=48)
        ax.text(0.0, 0.50, phase, va="top", ha="left", fontsize=11, fontweight="bold",
                transform=ax.transAxes)
    else:
        ax.text(0.0, 1.0, f"PID {w.pid}", va="top", ha="left", family="monospace",
                fontsize=10.5, transform=ax.transAxes)
    if w.status:
        ax.text(0.0, 0.22, textwrap.fill(w.status, width=42), va="top", ha="left", fontsize=12,
                fontweight="bold", color="#c00000", transform=ax.transAxes)


def draw_chart(ax, w):
    from matplotlib.ticker import MaxNLocator
    ax.clear()
    xs = [h[0] for h in w.history]
    ax.plot(xs, [h[1] for h in w.history], linewidth=2, marker=".", label="VmSize (reserved)")
    ax.plot(xs, [h[2] for h in w.history], linewidth=2, marker=".", label="VmRSS (in RAM)")
    last = xs[-1] if xs else 1
    ax.set_xlim(max(1, last - HISTORY + 1) - 0.5, max(last, 10) + 0.5)   # a 60-sample window
    values = [v for h in w.history for v in h[1:] if not math.isnan(v)]
    ax.set_ylim(0, max(values) * 1.3 if values else 1)   # headroom keeps lines below the legend
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_title(f"Memory over the last {HISTORY} samples", fontsize=11)
    ax.set_xlabel("sample (one per second)")
    ax.set_ylabel("MiB")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)


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


def draw(fig, axes, w):
    name = f" ({w.info['name']})" if w.info else ""
    fig.suptitle(f"Virtual Memory Visualizer - PID {w.pid}{name}", fontsize=14, fontweight="bold")
    draw_map(axes[0], w)
    draw_info(axes[1], w)
    draw_chart(axes[2], w)
    fig.canvas.draw_idle()


def start_demo():
    """Start bin/vmv_demo 3 2 and read its PID from the first line it prints."""
    if not os.access(VMV_DEMO, os.X_OK):
        sys.exit("bin/vmv_demo not found: run ./build.sh first")
    proc = subprocess.Popen([VMV_DEMO, "3", "2"], stdout=subprocess.PIPE, text=True, cwd=ROOT)
    m = re.search(r"PID (\d+)", proc.stdout.readline())   # "vmv_demo PID 1234 - watch it ..."
    if m is None:
        proc.kill()
        proc.wait()
        sys.exit("could not read the PID from bin/vmv_demo")
    return proc, int(m.group(1))


def main():
    ap = argparse.ArgumentParser(description="Live matplotlib view of a process's address space.")
    ap.add_argument("pid", nargs="?", type=int, help="process to watch")
    ap.add_argument("--demo", action="store_true", help="start bin/vmv_demo and watch it")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--png", metavar="FILE", help="save one frame and exit (no window)")
    out.add_argument("--frames", metavar="DIR", help="save every sample as DIR/frame_NNN.png")
    args = ap.parse_args()
    if (args.pid is None) == (not args.demo) or (args.pid is not None and args.pid <= 0):
        ap.error("give exactly one of: a PID, or --demo")
    if not os.access(VMV, os.X_OK):
        sys.exit("bin/vmv not found: run ./build.sh first")
    plt = load_pyplot(headless=bool(args.png or args.frames))

    proc = None
    try:
        proc, pid = start_demo() if args.demo else (None, args.pid)
        w = Watcher(pid, proc)
        fig, axes = make_figure(plt, pid)
        if args.png:
            w.step()
            draw(fig, axes, w)
            fig.savefig(args.png, dpi=100)
            print(f"Wrote {args.png}")
        elif args.frames:
            os.makedirs(args.frames, exist_ok=True)
            n = 0
            while True:
                t0 = time.monotonic()
                w.step()
                draw(fig, axes, w)
                fig.savefig(os.path.join(args.frames, f"frame_{n:03d}.png"), dpi=100)
                n += 1
                if not w.running:
                    break
                time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
            print(f"Wrote {n} frames to {args.frames} ({w.status})")
        else:
            plt.ion()
            fig.show()
            while plt.fignum_exists(fig.number):
                t0 = time.monotonic()
                w.step()
                draw(fig, axes, w)
                if not w.running:
                    break
                plt.pause(max(0.05, 1.0 - (time.monotonic() - t0)))
            if plt.fignum_exists(fig.number):     # keep the last frame until the window is closed
                plt.ioff()
                plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None:                       # never leave the demo running or unreaped
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
            proc.stdout.close()


if __name__ == "__main__":
    main()
