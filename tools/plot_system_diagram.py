"""Explanatory diagram of the MonoSpectro instrument: the optical path and the
signal chain that turns a camera frame into an absorbance spectrum.

Drawn rather than photographed, so the geometry that matters - the grating bonded
to the lens, the 36 degree sensor tilt, the absence of a collimator - is legible.

Usage:
    python3 tools/plot_system_diagram.py [-o OUT.png]

matplotlib + numpy only.
"""

import argparse
import os

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import (FancyArrowPatch, FancyBboxPatch, Polygon,
                                Rectangle, Arc)

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
MUTED = "#52514e"
LINE = "#8a8983"
BODY = "#e8e7e2"

TILT_DEG = 36.0


def wavelength_rgb(nm):
    """Approximate sRGB of a monochromatic wavelength - for the dispersed beam,
    which is a real spectrum, not a data encoding."""
    nm = float(nm)
    if nm < 440:
        r, g, b = -(nm - 440) / 60.0, 0.0, 1.0
    elif nm < 490:
        r, g, b = 0.0, (nm - 440) / 50.0, 1.0
    elif nm < 510:
        r, g, b = 0.0, 1.0, -(nm - 510) / 20.0
    elif nm < 580:
        r, g, b = (nm - 510) / 70.0, 1.0, 0.0
    elif nm < 645:
        r, g, b = 1.0, -(nm - 645) / 65.0, 0.0
    else:
        r, g, b = 1.0, 0.0, 0.0
    if nm > 700:
        f = 0.3 + 0.7 * (780 - nm) / 80.0
    elif nm < 420:
        f = 0.3 + 0.7 * (nm - 380) / 40.0
    else:
        f = 1.0
    f = max(f, 0.0)
    return tuple(np.clip(np.array([r, g, b]) * f, 0, 1) ** 0.85)


def label(ax, x, y, text, size=8, weight="normal", color=INK, **kw):
    ax.text(x, y, text, fontsize=size, color=color, ha=kw.pop("ha", "center"),
            va=kw.pop("va", "center"), fontweight=weight, zorder=9, **kw)


def optics_panel(ax):
    ax.set_xlim(0, 112)
    ax.set_ylim(-4, 46)
    ax.axis("off")
    axis_y = 16.0

    # --- halogen lamp -------------------------------------------------------
    ax.add_patch(Rectangle((3, axis_y - 4.5), 6, 9, facecolor=BODY,
                           edgecolor=LINE, lw=0.9, zorder=3))
    for k in range(-2, 3):
        ax.plot([9, 12.5], [axis_y + k * 1.6, axis_y + k * 1.6],
                color="#d9a441", lw=1.0, zorder=2)
    label(ax, 6, axis_y - 8.5, "Halogen\n10 W", size=7.5, color=MUTED)

    # --- cuvette ------------------------------------------------------------
    ax.add_patch(Rectangle((13.5, axis_y - 6), 8, 12, facecolor="#cfe6ef",
                           edgecolor=LINE, lw=1.0, zorder=3))
    label(ax, 17.5, axis_y - 8.5, "Cuvette\n10 mm", size=7.5, color=MUTED)

    # --- entrance slit ------------------------------------------------------
    for y0 in (axis_y + 1.1, axis_y - 5.1):
        ax.add_patch(Rectangle((25, y0), 2.2, 4.0, facecolor=INK,
                               edgecolor="none", zorder=4))
    label(ax, 26.1, axis_y - 8.5, "Slit\n0.5 mm", size=7.5, color=MUTED)
    ax.plot([21.5, 25], [axis_y, axis_y], color="#d9a441", lw=1.2, zorder=2)

    # --- lens with grating bonded to its front face -------------------------
    lens_x = 40.0
    t = np.linspace(-1, 1, 60)
    ax.add_patch(Polygon(np.column_stack([lens_x + 2.2 * (1 - t ** 2),
                                          axis_y + 8.5 * t]),
                         closed=False, facecolor="none", edgecolor=LINE, lw=1.1,
                         zorder=4))
    ax.add_patch(Polygon(np.column_stack([lens_x - 2.2 * (1 - t ** 2),
                                          axis_y + 8.5 * t]),
                         closed=False, facecolor="none", edgecolor=LINE, lw=1.1,
                         zorder=4))
    ax.add_patch(Polygon(np.vstack([
        np.column_stack([lens_x + 2.2 * (1 - t ** 2), axis_y + 8.5 * t]),
        np.column_stack([lens_x - 2.2 * (1 - t ** 2), axis_y + 8.5 * t])[::-1]]),
        closed=True, facecolor="#dbeaf2", edgecolor="none", alpha=0.7, zorder=3))
    ax.add_patch(Rectangle((lens_x - 3.6, axis_y - 8.5), 1.3, 17,
                           facecolor="#c9c8c2", edgecolor=INK, lw=0.7,
                           hatch="////", zorder=5))
    ax.plot([27.2, lens_x - 3.6], [axis_y, axis_y], color="#d9a441", lw=1.2, zorder=2)
    label(ax, lens_x - 2.9, axis_y + 11.5, "grating\n1000 lines/mm\nbonded to lens",
          size=7.5, color=MUTED)
    label(ax, lens_x + 6.5, axis_y - 10.5, "M12 lens\n12 mm", size=7.5, color=MUTED)

    # --- dispersed first order ---------------------------------------------
    # sensor plane, tilted; the fan spans the first-order angles
    sx, sy = 78.0, axis_y + 5.0
    th = np.radians(TILT_DEG)
    half = 11.0
    p0 = np.array([sx - half * np.cos(th), sy - half * np.sin(th)])
    p1 = np.array([sx + half * np.cos(th), sy + half * np.sin(th)])

    lams = np.linspace(420, 760, 26)
    for i, lam in enumerate(lams):
        f = i / (len(lams) - 1)
        end = p0 + (p1 - p0) * f
        ax.plot([lens_x, end[0]], [axis_y, end[1]],
                color=wavelength_rgb(lam), lw=1.5, alpha=0.55, zorder=2)

    # sensor bar
    n = np.array([-np.sin(th), np.cos(th)]) * 1.7
    ax.add_patch(Polygon([p0 + n, p1 + n, p1 - n, p0 - n], closed=True,
                         facecolor="#2b2b2b", edgecolor=INK, lw=0.8, zorder=6))
    for i, lam in enumerate(np.linspace(420, 760, 40)):
        f = i / 39
        c = p0 + (p1 - p0) * f
        ax.add_patch(Polygon([c + n * 0.55 - (p1 - p0) / 80,
                              c + n * 0.55 + (p1 - p0) / 80,
                              c - n * 0.55 + (p1 - p0) / 80,
                              c - n * 0.55 - (p1 - p0) / 80],
                             closed=True, facecolor=wavelength_rgb(lam),
                             edgecolor="none", zorder=7))
    label(ax, sx + 13, sy - 9.5, "mono sensor\n1280$\\times$720, 8-bit",
          size=7.5, color=MUTED, ha="center")

    # tilt annotation: angle between the optical axis and the sensor plane
    ax.plot([lens_x, sx + 15], [axis_y, axis_y], color=LINE, lw=0.6,
            ls=(0, (4, 3)), zorder=1)
    ax.plot([sx - 13, sx + 15], [sy, sy], color=LINE, lw=0.6,
            ls=(0, (4, 3)), zorder=1)
    ax.add_patch(Arc((sx, sy), 20, 20, angle=0, theta1=0, theta2=TILT_DEG,
                     color=INK, lw=0.9, zorder=8))
    label(ax, sx + 12.0, sy + 3.4, "36$^\\circ$", size=8, weight="bold")

    # slit -> camera distance
    ax.annotate("", xy=(27.2, axis_y - 12.5), xytext=(lens_x - 3.6, axis_y - 12.5),
                arrowprops=dict(arrowstyle="<->", color=MUTED, lw=0.8))
    label(ax, 33.5, axis_y - 15.0, "60 mm, no collimator", size=7.5, color=MUTED)

    label(ax, 2, 42, "(a)", size=9, weight="bold", ha="left")


def chain_panel(ax):
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 22)
    ax.axis("off")

    steps = ["camera\nframe", "ROI\nrow-average", "pixel $\\rightarrow$ nm\npolynomial",
             "$A=\\log_{10}(I_0/I)$", "live chart\n+ CSV"]
    w, gap, y = 15.5, 4.6, 8.0
    x = 4.0
    centers = []
    for i, s in enumerate(steps):
        ax.add_patch(FancyBboxPatch((x, y), w, 8.4,
                                    boxstyle="round,pad=0,rounding_size=1.2",
                                    facecolor="#f2f1ec" if i < 4 else "#e4eef4",
                                    edgecolor=LINE, lw=0.9, zorder=3))
        label(ax, x + w / 2, y + 4.2, s, size=7.5)
        centers.append(x + w / 2)
        if i:
            ax.add_patch(FancyArrowPatch((x - gap + 0.4, y + 4.2), (x - 0.6, y + 4.2),
                                         arrowstyle="-|>", mutation_scale=9,
                                         color=MUTED, lw=0.9, zorder=4))
        x += w + gap
    right = x - gap

    ax.plot([4, 4 + 4 * w + 3 * gap], [19, 19], color=LINE, lw=0.8, zorder=2)
    label(ax, 4 + (4 * w + 3 * gap) / 2, 20.6, "Raspberry Pi 4 (Flask, picamera2)",
          size=7.5, color=MUTED)
    ax.plot([right - w, right], [19, 19], color=LINE, lw=0.8, zorder=2)
    label(ax, right - w / 2, 20.6, "browser", size=7.5, color=MUTED)

    label(ax, 2, 20.6, "(b)", size=9, weight="bold", ha="left")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="docs/images/system_diagram.png")
    args = ap.parse_args()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7.0, 4.3),
                                   gridspec_kw=dict(height_ratios=[2.35, 1]))
    fig.patch.set_facecolor(SURFACE)
    for a in (ax1, ax2):
        a.set_facecolor(SURFACE)
    optics_panel(ax1)
    chain_panel(ax2)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01, hspace=0.05)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    fig.savefig(args.out, dpi=300, facecolor=SURFACE)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
