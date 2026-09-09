"""Training set figure: the nine food colourings, as the reference instrument
records them and as MonoSpectro records them before any correction.

Each panel carries a swatch of the solution's own colour, computed from its
reference transmittance rather than picked by hand, so "blue" is the blue that
spectrum actually is.

Usage:
    python3 tools/plot_training_set.py [DATA_DIR] [-o OUT.png]

DATA_DIR must contain commercial/ and nuestro/ with matching <name>.xlsx files,
each holding two columns: Wave (nm) and abs. Raw data is not tracked in this
repository; point this at your own copy.

numpy / pandas / matplotlib / openpyxl only - no SciPy, so it runs on the Pi.
"""

import argparse
import os

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

LO, HI = 420.0, 780.0
SURFACE = "#fcfcfb"
INK, INK_MUTED = "#0b0b0b", "#52514e"
GRID = "#e2e1dd"

ORDER = ["blue", "grape", "green", "lemon", "lime", "pink", "red", "rose", "teal"]

_integ = np.trapezoid if hasattr(np, "trapezoid") else np.trapz


def _lobe(x, mu, s1, s2):
    s = np.where(x < mu, s1, s2)
    return np.exp(-0.5 * ((x - mu) / s) ** 2)


def cie_xyz_bar(lam):
    """Analytic multi-lobe fit to the CIE 1931 colour matching functions
    (Wyman, Sloan & Shirley 2013) - no tabulated data needed."""
    x = (1.056 * _lobe(lam, 599.8, 37.9, 31.0)
         + 0.362 * _lobe(lam, 442.0, 16.0, 26.7)
         - 0.065 * _lobe(lam, 501.1, 20.4, 26.2))
    y = (0.821 * _lobe(lam, 568.8, 46.9, 40.5)
         + 0.286 * _lobe(lam, 530.9, 16.3, 31.1))
    z = (1.217 * _lobe(lam, 437.0, 11.8, 36.0)
         + 0.681 * _lobe(lam, 459.0, 26.0, 13.8))
    return x, y, z


def spectrum_to_hex(lam, absorbance):
    """Transmitted colour of the solution under an equal-energy illuminant."""
    lam = np.asarray(lam, float)
    grid = np.arange(max(LO, lam.min()), min(HI, lam.max()) + 1.0, 1.0)
    trans = 10.0 ** (-np.interp(grid, lam, np.asarray(absorbance, float)))
    xb, yb, zb = cie_xyz_bar(grid)
    norm = _integ(yb, grid)
    X = _integ(trans * xb, grid) / norm
    Y = _integ(trans * yb, grid) / norm
    Z = _integ(trans * zb, grid) / norm
    M = np.array([[3.2406, -1.5372, -0.4986],
                  [-0.9689, 1.8758, 0.0415],
                  [0.0557, -0.2040, 1.0570]])
    rgb = np.clip(M @ np.array([X, Y, Z]), 0.0, 1.0)
    rgb = np.where(rgb <= 0.0031308, 12.92 * rgb, 1.055 * rgb ** (1 / 2.4) - 0.055)
    rgb = np.clip(rgb, 0.0, 1.0)
    return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb)


def _rel_lum(rgb):
    c = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return float(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])


def _contrast(rgb, surface_rgb):
    a, b = _rel_lum(rgb), _rel_lum(surface_rgb)
    lo, hi = min(a, b), max(a, b)
    return (hi + 0.05) / (lo + 0.05)


def ink_from(hex_color, surface=SURFACE, target=3.6):
    """Darken a swatch along its own hue until it is legible on the surface.

    The swatch keeps the solution's true colour; the curve drawn in that colour
    has to clear 3:1 against a near-white page, so pale solutions (lemon, red)
    are stepped down in lightness only - hue and saturation are untouched.
    """
    rgb = np.array([int(hex_color[i:i + 2], 16) for i in (1, 3, 5)], float) / 255
    srgb = np.array([int(surface[i:i + 2], 16) for i in (1, 3, 5)], float) / 255
    for k in np.linspace(1.0, 0.12, 60):
        cand = rgb * k
        if _contrast(cand, srgb) >= target:
            return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in cand)
    return "#333333"


def load_pair(data_dir, name):
    def read(side):
        d = pd.read_excel(os.path.join(data_dir, side, name + ".xlsx"))
        w = d.iloc[:, 0].to_numpy(float)
        a = d.iloc[:, 1].to_numpy(float)
        keep = (w >= LO) & (w <= HI)
        return w[keep], a[keep]
    return read("commercial"), read("nuestro")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", nargs="?", default="DATOS DE ARAMIS 19-5/14_04_26")
    ap.add_argument("-o", "--out", default="docs/images/training_set.png")
    args = ap.parse_args()

    pairs = {n: load_pair(args.data_dir, n) for n in ORDER}
    ymax = max(max(r[1].max(), d[1].max()) for r, d in pairs.values())
    ytop = float(np.ceil(ymax * 10) / 10)

    fig, axes = plt.subplots(3, 3, figsize=(6.8, 4.9), sharex=True, sharey=True)
    fig.patch.set_facecolor(SURFACE)

    for ax, name in zip(axes.ravel(), ORDER):
        (rw, ra), (dw, da) = pairs[name]
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, lw=0.5, zorder=0)
        ax.set_axisbelow(True)
        # put the swatch in whichever top corner the curves leave free
        span = HI - LO
        left_peak = ra[rw < LO + 0.45 * span].max(initial=0.0)
        right_peak = ra[rw > HI - 0.45 * span].max(initial=0.0)
        if left_peak <= right_peak:
            sx, tx, ha = 0.05, 0.185, "left"
        else:
            sx, tx, ha = 0.95, 0.815, "right"
        swatch = spectrum_to_hex(rw, ra)
        ink = ink_from(swatch)
        ax.plot(rw, ra, color=ink, lw=1.7, solid_capstyle="round", zorder=3)
        ax.plot(dw, da, color=ink, lw=1.3, ls=(0, (3.5, 2)), alpha=0.85, zorder=2)
        ax.add_patch(Rectangle((sx - (0.10 if ha == "right" else 0.0), 0.80),
                               0.10, 0.15, transform=ax.transAxes,
                               facecolor=swatch, edgecolor=INK_MUTED,
                               linewidth=0.6, zorder=5))
        ax.text(tx, 0.875, name, transform=ax.transAxes, fontsize=8.5,
                color=INK, va="center", ha=ha, zorder=5)

        ax.set_xlim(LO, HI)
        ax.set_ylim(0, ytop * 1.03)
        ax.tick_params(labelsize=7.5, colors=INK_MUTED, length=3)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)

    fig.supxlabel("Wavelength (nm)", fontsize=9, color=INK_MUTED)
    fig.supylabel("Absorbance (AU)", fontsize=9, color=INK_MUTED)

    fig.legend(handles=[Line2D([], [], color=INK, lw=1.7,
                               label="Reference instrument"),
                        Line2D([], [], color=INK, lw=1.3, ls=(0, (3.5, 2)),
                               label="MonoSpectro, uncorrected")],
               loc="upper center", ncol=2, frameon=False, fontsize=8.5,
               labelcolor=INK, bbox_to_anchor=(0.5, 1.005))

    fig.tight_layout(rect=(0.012, 0.012, 1, 0.945))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    fig.savefig(args.out, dpi=300, facecolor=SURFACE)
    print("wrote", args.out)
    surf = np.array([int(SURFACE[i:i + 2], 16) for i in (1, 3, 5)], float) / 255
    for n in ORDER:
        rw, ra = pairs[n][0]
        sw = spectrum_to_hex(rw, ra)
        ik = ink_from(sw)
        rgb = np.array([int(ik[i:i + 2], 16) for i in (1, 3, 5)], float) / 255
        print(f"  {n:6s} swatch {sw}  curve {ik}  contrast {_contrast(rgb, surf):.2f}:1")


if __name__ == "__main__":
    main()
