"""Draw the zoom trajectory of this case from a zoom study.

    os-optics-kit zoom studies/zoom.json --out zoom
    python figures.py zoom --out images
    os-optics-kit zoom studies/zoom_constrained.json --out constrained
    python figures.py constrained --out images --name constrained.png

ZOOM is the directory written by ``os-optics-kit zoom``. The script reads ZOOM/trajectory.json and writes
NAME (default trajectory.png): the variator and compensator positions against the requested focal length, the travel
limits that lie within the plotted span, the anchors, and every sample's status.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

DPI = 120
MAX_BYTES = 400 * 1024
MARKERS = {"PASS": ("o", "#1b7837"), "FAIL": ("s", "#b2182b"), "INFEASIBLE": ("X", "#d6604d"),
           "UNSOLVED": ("x", "#4d4d4d"), "UNVERIFIED": ("D", "#e08214")}


def draw(trajectory, out, name="trajectory.png"):
    groups = {g["group_id"]: g for g in trajectory["spec"]["groups"]}
    moving = trajectory["moving_groups"]
    samples = [s for s in trajectory["samples"] if s.get("positions_mm")]
    fig, axes = plt.subplots(1, len(moving), figsize=(9.6, 4.0), sharex=True)
    for ax, group_id in zip(axes, moving):
        efl = [s["efl_requested_mm"] for s in samples]
        pos = [s["positions_mm"][group_id] for s in samples]
        ax.plot(efl, pos, color="#2166ac", linewidth=1.4, zorder=1)
        for status, (marker, color) in MARKERS.items():
            chosen = [s for s in samples if s["status"] == status]
            if chosen:
                ax.scatter([s["efl_requested_mm"] for s in chosen], [s["positions_mm"][group_id] for s in chosen],
                           marker=marker, color=color, s=22, zorder=2, label=status)
        for anchor in trajectory["anchors"]:
            if anchor["solved_mm"]:
                value = anchor["solved_mm"][moving.index(group_id)]
                ax.annotate(anchor["label"], (anchor["efl_mm"], value), textcoords="offset points", xytext=(4, 6),
                            fontsize=8)
        low, high = groups[group_id]["travel_mm"]
        pad = 0.1 * (max(pos) - min(pos))
        bottom, top = min(pos) - pad, max(pos) + pad
        for bound in (low, high):  # a travel limit is drawn where it lies within the trajectory's span
            if bottom <= bound <= top:
                ax.axhline(bound, color="#999999", linestyle="--", linewidth=0.9)
        ax.set_ylim(bottom, top)
        ax.set_title(f"{group_id} (travel {low:g} to {high:g} mm)", fontsize=10)
        ax.set_xlabel("requested EFL (mm)")
        ax.set_ylabel("position along the axis (mm)")
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8, loc="best")
    identity = trajectory["identity"]
    fig.suptitle(f"Zoom trajectory, image plane fixed at {trajectory['image_plane']['position_along_axis_mm']:g} mm "
                 f"(os-optics {identity['engine_version']})", fontsize=11)
    fig.tight_layout()
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    if path.stat().st_size > MAX_BYTES:
        raise SystemExit(f"{path} is larger than {MAX_BYTES} bytes")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("zoom", type=Path, help="directory written by os-optics-kit zoom")
    parser.add_argument("--out", type=Path, default=Path("images"))
    parser.add_argument("--name", default="trajectory.png", help="file name of the figure")
    args = parser.parse_args(argv)
    trajectory = json.loads((args.zoom / "trajectory.json").read_text(encoding="utf-8"))
    print(draw(trajectory, args.out, args.name).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
