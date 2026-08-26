#!/usr/bin/env python3
"""
Compare MonoSpectro measurements against a reference (commercial) spectrophotometer.

Applies a wavelength-dependent quadratic response correction to the raw DIY
absorbance values and plots them, channel by channel, against the reference
instrument -- once before correction and once after.

The correction table is a CSV with one row per wavelength:

    Nanometers,coef_A,coef_B,coef_C
    400.18,3.2260,-5.9454,1.8059
    ...

and is applied as:  A_corrected = a*x^2 + b*x + c

Usage
-----
    python tools/compare_reference.py \
        --calibration calibration.csv \
        --measured    my_run.xls \
        --reference   reference_run.xls \
        --outdir      docs/images

Both spectra files are read with pandas; column 0 must be the wavelength and
every remaining column one measurement channel.
"""

import argparse
import os

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

WL_MIN_DEFAULT = 400.0
WL_MAX_DEFAULT = 800.0

SERIES_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
                 "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]


def load_spectra(path):
    """Read a spectra table, tolerating the pipe-padded export some
    instruments produce. Returns a DataFrame: Wavelength + Ch_1..Ch_n."""
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


def apply_correction(wavelengths, raw, cal):
    """Interpolate the per-wavelength quadratic coefficients onto the measured
    grid and apply them to every channel at once."""
    a = np.interp(wavelengths, cal.index.values, cal["coef_A"].values)
    b = np.interp(wavelengths, cal.index.values, cal["coef_B"].values)
    c = np.interp(wavelengths, cal.index.values, cal["coef_C"].values)
    return a[:, None] * raw**2 + b[:, None] * raw + c[:, None]


def plot_comparison(wl_m, data_m, wl_r, data_r, label, outfile, wl_min, wl_max):
    n = data_m.shape[1]
    ncols = min(2, n)
    nrows = int(np.ceil(n / ncols))

    fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 4 * nrows), squeeze=False)
    axes = axes.flatten()

    for i in range(n):
        ax = axes[i]
        ax.plot(wl_m, data_m[:, i], color=SERIES_COLORS[i % len(SERIES_COLORS)],
                linewidth=2, label=f"MonoSpectro ({label})")
        ax.plot(wl_r, data_r[:, i], color="black", linestyle="--",
                linewidth=1.6, label="Reference instrument")
        ax.set_title(f"Channel {i + 1}", fontsize=13, fontweight="bold")
        ax.set_xlabel("Wavelength (nm)")
        ax.set_ylabel("Absorbance")
        ax.set_xlim(wl_min, wl_max)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=9)

    for j in range(n, len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(f"MonoSpectro {label} vs reference spectrophotometer",
                 fontsize=15, fontweight="bold")
    fig.tight_layout()
    fig.savefig(outfile, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"saved: {outfile}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--calibration", required=True, help="response-correction CSV")
    p.add_argument("--measured", required=True, help="MonoSpectro spectra (.csv/.xls/.xlsx)")
    p.add_argument("--reference", required=True, help="reference spectra (.csv/.xls/.xlsx)")
    p.add_argument("--outdir", default=".", help="where to write the figures")
    p.add_argument("--wl-min", type=float, default=WL_MIN_DEFAULT)
    p.add_argument("--wl-max", type=float, default=WL_MAX_DEFAULT)
    args = p.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    cal = pd.read_csv(args.calibration).set_index("Nanometers").sort_index()
    df_m = load_spectra(args.measured)
    df_r = load_spectra(args.reference)

    mask_m = df_m["Wavelength"].between(args.wl_min, args.wl_max)
    mask_r = df_r["Wavelength"].between(args.wl_min, args.wl_max)

    wl_m = df_m.loc[mask_m, "Wavelength"].values
    wl_r = df_r.loc[mask_r, "Wavelength"].values
    raw_m = df_m.loc[mask_m].drop(columns="Wavelength").values
    raw_r = df_r.loc[mask_r].drop(columns="Wavelength").values

    n = min(raw_m.shape[1], raw_r.shape[1])
    if n == 0:
        raise SystemExit("No channels in common between the two files.")
    raw_m, raw_r = raw_m[:, :n], raw_r[:, :n]
    print(f"comparing {n} channel(s) over {args.wl_min:.0f}-{args.wl_max:.0f} nm")

    corrected = apply_correction(wl_m, raw_m, cal)

    plot_comparison(wl_m, raw_m, wl_r, raw_r, "uncorrected",
                    os.path.join(args.outdir, "validation_uncalibrated_vs_reference.png"),
                    args.wl_min, args.wl_max)
    plot_comparison(wl_m, corrected, wl_r, raw_r, "calibrated",
                    os.path.join(args.outdir, "validation_calibrated_vs_reference.png"),
                    args.wl_min, args.wl_max)


if __name__ == "__main__":
    main()
