#!/usr/bin/env python3
"""
Compare MonoSpectro measurements against a reference (commercial) spectrophotometer.

Produces two figures:

  validation_raw_vs_reference.png      as measured, both instruments on one axis
  validation_scaled_vs_reference.png   MonoSpectro rescaled by a single linear
                                       factor per channel, with r^2 annotated

and prints the agreement statistics it used.

Usage
-----
    python tools/compare_reference.py \
        --measured  my_run.xls \
        --reference reference_run.xls \
        --outdir    docs/images

Column 0 of each file must be the wavelength; every remaining column is one
measurement channel. Channels are paired in order.
"""

import argparse
import os

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

WL_MIN_DEFAULT, WL_MAX_DEFAULT = 400.0, 800.0

INK          = "#0b0b0b"
INK_SOFT     = "#52514e"
GRID         = "#e6e5e1"
SURFACE      = "#fcfcfb"
SERIES_DIY   = "#2a78d6"   # MonoSpectro
SERIES_REF   = "#52514e"   # reference instrument (neutral baseline)


def load_spectra(path):
    """Read a spectra table, tolerating the pipe-padded export some instruments
    produce. Returns a DataFrame: Wavelength + Ch_1..Ch_n."""
    df = pd.read_excel(path) if path.lower().endswith((".xls", ".xlsx")) else pd.read_csv(path)

    def clean(series):
        return pd.to_numeric(
            series.astype(str).str.replace("|", "", regex=False).str.strip(),
            errors="coerce",
        )

    out = pd.DataFrame({"Wavelength": clean(df.iloc[:, 0])})
    for i in range(1, df.shape[1]):
        out[f"Ch_{i}"] = clean(df.iloc[:, i])
    return out.dropna(subset=["Wavelength"])


def style_axes(ax, wl_min, wl_max):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
        ax.spines[side].set_linewidth(1)
    ax.grid(True, axis="y", color=GRID, linewidth=1, alpha=0.9)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_SOFT, labelsize=9, length=0)
    ax.set_xlim(wl_min, wl_max)
    ax.set_xticks(np.arange(wl_min, wl_max + 1, 100))


def plot_panel(ax, wl_d, diy, wl_r, ref, title, note, wl_min, wl_max):
    style_axes(ax, wl_min, wl_max)
    ax.plot(wl_r, ref, color=SERIES_REF, linewidth=2, linestyle=(0, (5, 3)),
            label="Reference instrument", zorder=2)
    ax.plot(wl_d, diy, color=SERIES_DIY, linewidth=2,
            label="MonoSpectro", zorder=3)
    ax.set_title(title, fontsize=11, color=INK, loc="left", pad=8, fontweight="bold")
    if note:
        ax.text(0.98, 0.94, note, transform=ax.transAxes, ha="right", va="top",
                fontsize=9, color=INK_SOFT)


def build_figure(channels, wl_min, wl_max, suptitle, subtitle, outfile):
    n = len(channels)
    ncols = 2 if n > 1 else 1
    nrows = int(np.ceil(n / ncols))

    fig, axes = plt.subplots(nrows, ncols, figsize=(5.4 * ncols, 3.6 * nrows),
                             squeeze=False, facecolor=SURFACE)
    flat = axes.flatten()

    for ax, ch in zip(flat, channels):
        plot_panel(ax, ch["wl_d"], ch["diy"], ch["wl_r"], ch["ref"],
                   ch["title"], ch["note"], wl_min, wl_max)
    for ax in flat[n:]:
        ax.set_visible(False)

    for row in range(nrows):
        axes[row][0].set_ylabel("Absorbance", fontsize=10, color=INK_SOFT)
    for col in range(ncols):
        axes[nrows - 1][col].set_xlabel("Wavelength (nm)", fontsize=10, color=INK_SOFT)
        if nrows * ncols > n and n % ncols and col >= n % ncols:
            axes[nrows - 2][col].set_xlabel("Wavelength (nm)", fontsize=10, color=INK_SOFT)

    fig.suptitle(suptitle, fontsize=14, color=INK, fontweight="bold", x=0.055, ha="left", y=0.975)
    fig.text(0.055, 0.932, subtitle, fontsize=10, color=INK_SOFT, ha="left")

    handles, labels = flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", bbox_to_anchor=(0.975, 0.978),
               frameon=False, fontsize=10, labelcolor=INK_SOFT, ncols=2, handlelength=2.4)

    fig.tight_layout(rect=(0.015, 0.01, 0.985, 0.905))
    fig.subplots_adjust(hspace=0.32)
    fig.savefig(outfile, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    print(f"saved: {outfile}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--measured", required=True, help="MonoSpectro spectra (.csv/.xls/.xlsx)")
    p.add_argument("--reference", required=True, help="reference spectra (.csv/.xls/.xlsx)")
    p.add_argument("--outdir", default=".")
    p.add_argument("--labels", default="", help="comma-separated channel names")
    p.add_argument("--wl-min", type=float, default=WL_MIN_DEFAULT)
    p.add_argument("--wl-max", type=float, default=WL_MAX_DEFAULT)
    args = p.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    df_d, df_r = load_spectra(args.measured), load_spectra(args.reference)
    md = df_d["Wavelength"].between(args.wl_min, args.wl_max)
    mr = df_r["Wavelength"].between(args.wl_min, args.wl_max)
    wl_d = df_d.loc[md, "Wavelength"].values
    wl_r = df_r.loc[mr, "Wavelength"].values
    diy = df_d.loc[md].drop(columns="Wavelength").values
    ref = df_r.loc[mr].drop(columns="Wavelength").values

    n = min(diy.shape[1], ref.shape[1])
    if n == 0:
        raise SystemExit("No channels in common between the two files.")
    diy, ref = diy[:, :n], ref[:, :n]

    names = [s.strip() for s in args.labels.split(",")] if args.labels else []
    names += [f"Channel {i + 1}" for i in range(len(names), n)]

    raw_panels, scaled_panels = [], []
    print(f"{'channel':<20} {'slope':>8} {'offset':>8} {'r2':>7} {'RMSE':>8}  peak DIY / ref")
    for i in range(n):
        ref_on_d = np.interp(wl_d, wl_r, ref[:, i])
        slope, offset = np.polyfit(diy[:, i], ref_on_d, 1)
        fitted = slope * diy[:, i] + offset
        r2 = np.corrcoef(fitted, ref_on_d)[0, 1] ** 2
        rmse = float(np.sqrt(np.mean((fitted - ref_on_d) ** 2)))
        pk_d, pk_r = wl_d[np.argmax(diy[:, i])], wl_r[np.argmax(ref[:, i])]
        print(f"{names[i]:<20} {slope:8.2f} {offset:+8.3f} {r2:7.3f} {rmse:8.3f}"
              f"  {pk_d:.0f} / {pk_r:.0f} nm  ({pk_d - pk_r:+.0f})")

        raw_panels.append(dict(wl_d=wl_d, diy=diy[:, i], wl_r=wl_r, ref=ref[:, i],
                               title=names[i], note=""))
        scaled_panels.append(dict(wl_d=wl_d, diy=fitted, wl_r=wl_r, ref=ref[:, i],
                                  title=names[i],
                                  note=f"×{slope:.2f}   r² = {r2:.2f}"))

    build_figure(raw_panels, args.wl_min, args.wl_max,
                 "As measured",
                 "Raw absorbance from both instruments on the same axis — the shape is there, the scale is not.",
                 os.path.join(args.outdir, "validation_raw_vs_reference.png"))

    build_figure(scaled_panels, args.wl_min, args.wl_max,
                 "After a single linear scale factor per channel",
                 "One slope and offset per sample, fitted against the reference instrument.",
                 os.path.join(args.outdir, "validation_scaled_vs_reference.png"))


if __name__ == "__main__":
    main()
