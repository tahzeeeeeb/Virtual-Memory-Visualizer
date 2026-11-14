#!/usr/bin/env python3
"""plot_layout.py - optional renderer: draw the CSV from `vmv csv <pid>` as a PNG.

usage: python3 tools/plot_layout.py LAYOUT.csv -o OUT.png [--pid PID]
The C tool never needs this file; it is here because the proposal listed matplotlib.
"""
import argparse
import csv
import math
import sys

try:
    import matplotlib
    matplotlib.use("Agg")                  # no window: only write the PNG file
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
except ImportError:
    print("matplotlib is missing: sudo apt install python3-matplotlib")
    sys.exit(1)

# Same colour families as the terminal colours in src/classify.c, light enough for black text.
COLORS = {"CODE": "#e06666", "RODATA": "#c27ba0", "DATA": "#d4b000", "BSS": "#ffe866",
          "HEAP": "#6aa84f", "ANON-MMAP": "#76d7d7", "SHARED-LIB": "#6d8fd8",
          "FILE-MAP": "#9fc5f8", "THREAD-STACK?": "#e69be6", "STACK": "#ff4d4d",
          "GUARD": "#b0b0b0", "KERNEL-SPECIAL": "#eeeeee"}
GAP = 6.0                                   # height of a gap separator (log2 units)


def human(n):
    """Same text as format_size() in src/render.c: 512 B, 4.0 KiB, 1.5 MiB ..."""
    if n < 1024:
        return f"{n} B"
    value, units = n / 1024.0, ["KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]
    u = 0
    while value >= 1024.0 and u < len(units) - 1:
        value, u = value / 1024.0, u + 1
    return f"{value:.1f} {units[u]}"


def main():
    ap = argparse.ArgumentParser(description="Draw a `vmv csv` file as a PNG.")
    ap.add_argument("csv_file")
    ap.add_argument("-o", "--output", required=True, help="PNG file to write")
    ap.add_argument("--pid", default="?", help="PID shown in the title")
    args = ap.parse_args()
    with open(args.csv_file, newline="") as f:   # skip the "# pid=..." info line
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    if not rows:
        print(f"{args.csv_file}: no regions")
        sys.exit(1)

    fig, ax = plt.subplots()
    y, prev_end, kinds = 0.0, None, set()
    for r in rows:                          # bottom = low address, top = high address
        start, end, size = int(r["start"], 16), int(r["end"], 16), int(r["size_bytes"])
        if prev_end is not None and start > prev_end:      # unmapped hole
            ax.barh(y + GAP / 2, 1, height=GAP, color="white", edgecolor="gray",
                    hatch="///", linewidth=0.5)
            ax.text(0.5, y + GAP / 2, f"{human(start - prev_end)} unmapped", ha="center",
                    va="center", fontsize=6, bbox={"facecolor": "white", "edgecolor": "none",
                                                    "pad": 0.3})
            y += GAP
        h = math.log2(size)                 # log scale: 4 KiB -> 12, 1 GiB -> 30
        ax.barh(y + h / 2, 1, height=h, color=COLORS.get(r["kind"], "white"),
                edgecolor="black", linewidth=0.4)
        name = r["path"].rsplit("/", 1)[-1] or "[anon]"
        ax.text(0.5, y + h / 2, f"{r['kind']}  {human(size)}  {name}", ha="center",
                va="center", fontsize=7)
        kinds.add(r["kind"])
        y, prev_end = y + h, end

    ax.set_xlim(0, 1), ax.set_ylim(0, y), ax.set_xticks([])
    ax.set_yticks([0, y], [rows[0]["start"], rows[-1]["end"]], fontsize=7)
    ax.set_ylabel("low addresses (bottom) -> high addresses (top)")
    ax.set_title(f"Virtual address space of PID {args.pid}")
    ax.legend(handles=[Patch(facecolor=COLORS[k], edgecolor="black", label=k)
                       for k in COLORS if k in kinds],
              loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)
    fig.set_size_inches(9, max(4.0, y * 0.018 + 1.5))
    fig.savefig(args.output, dpi=110, bbox_inches="tight")
    print(f"Wrote {args.output} ({len(rows)} regions)")


if __name__ == "__main__":
    main()
