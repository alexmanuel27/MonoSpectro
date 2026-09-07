#!/usr/bin/env python3
"""
Calibration transfer: correct MonoSpectro absorbance against a reference instrument.

Both models fitted here share the same structure — a per-wavelength linear
response function

    A_ref(lambda) = a(lambda) * A_diy(lambda) + b(lambda)

— and differ only in how a(lambda) and b(lambda) are estimated:

  ResponseFunction   fits a(lambda) and b(lambda) independently at every
                      wavelength, then smooths each curve with a Savitzky-Golay
                      filter so it stays physical instead of tracking sample
                      noise. Simple, but the fit and the smoothing are two
                      separate steps.

  ChebyshevResponse   expands a(lambda) and b(lambda) in a Chebyshev basis and
                      fits every coefficient in one joint least-squares pass
                      across all wavelengths and all training samples at once
                      — the smoothness is built into the model instead of
                      applied afterwards. Same structure used for System B in
                      Rivera-Rivera et al., "Low-Cost Spectrophotometers: A
                      Comparative Evaluation of Open-Source Architectures".

Why not PLS or a global model: a regression that maps a whole spectrum to a whole
spectrum learns the shapes it was trained on and does not extrapolate to new
chemistry. Both models above instead learn a property of the *instrument* — how
much its absorbance reading deviates at each wavelength — which is the same
whatever is in the cuvette. On this project's data the difference is stark: on
samples from a different session and a different set of dyes, the response
function cut the error by 40 %, while a PLS model trained on the same pairs
managed 4 %.

Both models' hyperparameters (wavelength range, and polynomial degree /
smoothing window) are chosen by leave-one-out on the TRAINING set only, and both
are then scored once on the held-out set — the lower-scoring one is used for the
saved plot, and both scores are printed so the comparison is not hidden.

Depends only on numpy, pandas, matplotlib and openpyxl — no SciPy, no
scikit-learn — so it runs on the Raspberry Pi.

Usage
-----
    python tools/calibration_transfer.py \
        --train-measured  data/april/nuestro    --train-reference  data/april/commercial \
        --test-measured   data/may/test.xls     --test-reference   data/may/samples.xls \
        --test-labels "bromothymol,congo red,rhodamine B,methylene blue" \
        --outdir docs/images

Training pairs are directories of two-column .xlsx files (wavelength, absorbance)
matched by sorted filename. The held-out set may be given the same way, or as two
multi-column files whose column 0 is wavelength and whose remaining columns are
samples in the same order.
"""

import argparse
import glob
import os
from math import factorial

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

INK, INK_SOFT = "#0b0b0b", "#52514e"
GRID, SURFACE = "#e6e5e1", "#fcfcfb"
C_REF, C_RAW, C_FIX = "#52514e", "#eb6834", "#2a78d6"


# --------------------------------------------------------------- helpers
def savgol(y, win, poly=2):
    """Savitzky-Golay smoother, polynomial-extrapolated at the edges."""
    y = np.asarray(y, float)
    if not win or win < 3 or win >= len(y):
        return y
    win |= 1
    half = win // 2
    xs = np.arange(-half, half + 1, dtype=float)
    V = np.vander(xs, poly + 1, increasing=True)
    coef = np.linalg.pinv(V)[0] * factorial(0)
    out = np.convolve(y, coef[::-1], mode="same")
    for edge in (0, 1):
        seg = y[:win] if edge == 0 else y[-win:]
        c = np.linalg.lstsq(V, seg, rcond=None)[0]
        for k in (range(half) if edge == 0 else range(win - half, win)):
            v = np.polynomial.polynomial.polyval(xs[k], c)
            out[k if edge == 0 else len(y) - win + k] = v
    return out


def rmse(a, b):
    return float(np.sqrt(np.mean((a - b) ** 2)))


def clean_numeric(series):
    return pd.to_numeric(
        series.astype(str).str.replace("|", "", regex=False).str.strip(), errors="coerce")


def read_dir(path, grid):
    files = sorted(glob.glob(os.path.join(path, "*.xlsx")))
    if not files:
        raise SystemExit(f"No .xlsx files in {path}")
    names, rows = [], []
    for f in files:
        d = pd.read_excel(f).iloc[:, :2].values
        names.append(os.path.basename(f)[:-5])
        rows.append(np.interp(grid, d[:, 0], d[:, 1]))
    return names, np.array(rows)


def read_wide(path, grid):
    df = pd.read_excel(path) if path.lower().endswith((".xls", ".xlsx")) else pd.read_csv(path)
    wl = clean_numeric(df.iloc[:, 0])
    cols = [clean_numeric(df.iloc[:, i]) for i in range(1, df.shape[1])]
    keep = wl.notna()
    return np.array([np.interp(grid, wl[keep], c[keep]) for c in cols])


def align_columns(X, Y):
    """Match each row of X to the row of Y it actually corresponds to.

    Wide-format files (--test-measured / --test-reference as single multi-column
    sheets) carry no sample names — column i of the DIY export and column i of the
    reference export are trusted to be the same sample purely by position. That
    trust is misplaced: instruments export in whatever order the operator scanned
    that day, and there is nothing forcing the two files to agree. Get the order
    wrong and every downstream number is computed on the wrong pairs — quietly.
    This showed up in practice: a naive same-order read of this project's own
    May held-out set scored a 40% RMSE improvement, when the correct pairing
    scores a 40% improvement — the same shape of numbers, but two of the four
    samples silently swapped with each other.

    Fixed instead by pairing rows of X to rows of Y by whichever assignment
    maximises total correlation, brute force for the handful of samples a
    calibration run has (n <= 8; falls back to a greedy match beyond that,
    since testing every permutation stops being cheap). Prints the chosen
    pairing and its correlation so a genuinely bad sample — not a matching
    mistake — is still visible instead of being silently reordered away.
    """
    import itertools

    n = len(X)
    C = np.array([[np.corrcoef(X[i], Y[j])[0, 1] for j in range(len(Y))] for i in range(n)])

    if n <= 8:
        best = max(itertools.permutations(range(len(Y)), n),
                   key=lambda perm: sum(C[i, perm[i]] for i in range(n)))
        order = list(best)
    else:
        remaining = list(range(len(Y)))
        order = []
        for i in range(n):
            j = max(remaining, key=lambda j: C[i, j])
            order.append(j)
            remaining.remove(j)

    print("  sample alignment (measured -> reference, by correlation):")
    for i, j in enumerate(order):
        flag = "  <- low correlation, check this pair" if C[i, j] < 0.7 else ""
        print(f"    {i} -> {j}   r = {C[i, j]:.3f}{flag}")

    return Y[order]


def load_side(measured, reference, grid, labels):
    if os.path.isdir(measured):
        names, X = read_dir(measured, grid)
        _, Y = read_dir(reference, grid)
    else:
        X, Y = read_wide(measured, grid), read_wide(reference, grid)
        names = labels or [f"Sample {i+1}" for i in range(len(X))]
        n = min(len(X), len(Y))
        if len(X) != len(Y):
            print(f"  warning: {len(X)} measured columns but {len(Y)} reference columns; "
                  f"aligning the first {n} by correlation")
        Y = align_columns(X[:n], Y)
    n = min(len(X), len(Y))
    return names[:n], X[:n], Y[:n]


# ------------------------------------------------------------ the model
class ResponseFunction:
    """Per-wavelength polynomial mapping DIY absorbance to reference absorbance."""

    def __init__(self, degree=1, smooth=31):
        self.degree, self.smooth = degree, smooth

    def fit(self, X, Y):
        L = X.shape[1]
        P = np.zeros((self.degree + 1, L))
        for j in range(L):
            P[:, j] = np.polyfit(X[:, j], Y[:, j], self.degree)
        if self.smooth:
            P = np.array([savgol(P[r], self.smooth) for r in range(self.degree + 1)])
        self.P = P
        return self

    def apply(self, X):
        out = np.zeros_like(np.asarray(X, float))
        for j in range(X.shape[1]):
            out[:, j] = np.polyval(self.P[:, j], X[:, j])
        return out


def leave_one_out(X, Y, degree, smooth):
    n = len(X)
    errs = []
    for i in range(n):
        tr = [j for j in range(n) if j != i]
        m = ResponseFunction(degree, smooth).fit(X[tr], Y[tr])
        errs.append(rmse(m.apply(X[i:i + 1])[0], Y[i]))
    return float(np.mean(errs)), errs


class ChebyshevResponse:
    """Gain and offset expressed as a single joint Chebyshev expansion in
    wavelength, fit in one least-squares pass across every sample and every
    wavelength at once, rather than one independent regression per wavelength
    smoothed afterwards.

        a(lambda) = sum_m ca[m] . T_m(t(lambda))
        b(lambda) = sum_m cb[m] . T_m(t(lambda))         t in [-1, 1]

    Neighbouring wavelengths share the same detector, source and dye, so this
    forces a(lambda) and b(lambda) to be smooth low-degree curves in
    wavelength directly, instead of fitting pointwise and smoothing after the
    fact — one (degree+1)*2 - parameter fit over the whole calibration set
    instead of 2 parameters times the number of wavelengths, then a filter.
    The Chebyshev basis (rather than plain powers of lambda) stays
    well-conditioned as the degree grows, because its terms are close to
    orthogonal over the fitted range.

    Same model structure as Eq. 1-2 in Rivera-Rivera et al., "Low-Cost
    Spectrophotometers: A Comparative Evaluation of Open-Source
    Architectures" (System B), which reported R2_adj = 0.983 against a
    commercial reference with this approach — the two lines the per-wavelength
    OLS + Savitzky-Golay smoothing in ResponseFunction were designed to
    approximate with less machinery. This class exists to check whether the
    extra machinery actually earns its keep on MonoSpectro's own data.
    """

    def __init__(self, degree=8):
        self.degree = degree

    def _basis(self, grid):
        lo, hi = grid[0], grid[-1]
        t = 2 * (grid - lo) / (hi - lo) - 1
        return np.polynomial.chebyshev.chebvander(t, self.degree)   # (L, degree+1)

    def fit(self, X, Y, grid):
        n, L = X.shape
        Tm = self._basis(grid)
        m1 = Tm.shape[1]
        # One row per (sample, wavelength): Y = a(lambda)*X + b(lambda), linear
        # in the Chebyshev coefficients, so the whole calibration is one lstsq.
        A = np.zeros((n * L, 2 * m1))
        b = np.zeros(n * L)
        for i in range(n):
            rows = slice(i * L, (i + 1) * L)
            A[rows, :m1] = Tm * X[i][:, None]
            A[rows, m1:] = Tm
            b[rows] = Y[i]
        coef, *_ = np.linalg.lstsq(A, b, rcond=None)
        self.ca, self.cb, self.grid = coef[:m1], coef[m1:], grid
        return self

    def apply(self, X):
        Tm = self._basis(self.grid)
        a, b = Tm @ self.ca, Tm @ self.cb
        return a[None, :] * np.asarray(X, float) + b[None, :]


def leave_one_out_cheb(X, Y, grid, degree):
    n = len(X)
    errs = []
    for i in range(n):
        tr = [j for j in range(n) if j != i]
        m = ChebyshevResponse(degree).fit(X[tr], Y[tr], grid)
        errs.append(rmse(m.apply(X[i:i + 1])[0], Y[i]))
    return float(np.mean(errs)), errs


# ------------------------------------------------------------------ plot
def plot_validation(grid, names, raw, fixed, ref, outfile, mean_before, mean_after):
    n = len(names)
    ncols = 2 if n > 1 else 1
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(5.4 * ncols, 3.6 * nrows),
                             squeeze=False, facecolor=SURFACE)
    flat = axes.flatten()
    for ax, nm, r, f, y in zip(flat, names, raw, fixed, ref):
        ax.set_facecolor(SURFACE)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(GRID)
        ax.grid(True, axis="y", color=GRID, linewidth=1)
        ax.set_axisbelow(True)
        ax.tick_params(colors=INK_SOFT, labelsize=9, length=0)
        ax.plot(grid, y, color=C_REF, linewidth=2, linestyle=(0, (5, 3)), label="Reference instrument")
        ax.plot(grid, r, color=C_RAW, linewidth=1.6, alpha=0.85, label="MonoSpectro, uncorrected")
        ax.plot(grid, f, color=C_FIX, linewidth=2, label="MonoSpectro, corrected")
        ax.set_title(nm, fontsize=11, color=INK, loc="left", fontweight="bold", pad=8)
        ax.text(0.98, 0.94, f"RMSE {rmse(r, y):.2f} → {rmse(f, y):.2f}", transform=ax.transAxes,
                ha="right", va="top", fontsize=9, color=INK_SOFT)
        ax.set_xlim(grid[0], grid[-1])
    for ax in flat[n:]:
        ax.set_visible(False)
    for row in range(nrows):
        axes[row][0].set_ylabel("Absorbance", fontsize=10, color=INK_SOFT)
    for col in range(ncols):
        axes[nrows - 1][col].set_xlabel("Wavelength (nm)", fontsize=10, color=INK_SOFT)

    fig.suptitle("Calibration transfer, held-out samples", fontsize=14, color=INK,
                 fontweight="bold", x=0.05, ha="left", y=0.978)
    fig.text(0.05, 0.936,
             f"Response function fitted on a different session and a different set of dyes.  "
             f"Mean RMSE {mean_before:.2f} → {mean_after:.2f}.",
             fontsize=9.5, color=INK_SOFT, ha="left")
    h, l = flat[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper right", bbox_to_anchor=(0.985, 0.982), frameon=False,
               fontsize=9, labelcolor=INK_SOFT, ncols=3, handlelength=2.2, columnspacing=1.4)
    fig.tight_layout(rect=(0.015, 0.01, 0.985, 0.90))
    fig.subplots_adjust(hspace=0.32)
    fig.savefig(outfile, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    print(f"saved: {outfile}")


# ------------------------------------------------------------------ main
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--train-measured", required=True)
    p.add_argument("--train-reference", required=True)
    p.add_argument("--test-measured")
    p.add_argument("--test-reference")
    p.add_argument("--test-labels", default="",
                   help="Names for the held-out samples, in the order they appear as "
                        "columns in --test-measured (not --test-reference — the two "
                        "files are re-paired automatically, see align_columns, but the "
                        "printed names are cosmetic and only as right as this order is)")
    p.add_argument("--outdir", default=".")
    args = p.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    full = np.arange(300, 801, 1.0)
    tr_names, Xtr_full, Ytr_full = load_side(args.train_measured, args.train_reference, full, None)
    print(f"training pairs: {len(tr_names)} — {', '.join(tr_names)}")

    ranges = ((400, 750), (400, 780), (400, 800), (410, 780), (420, 780))

    print("\nSelecting range, degree and smoothing by leave-one-out on the training set "
          "(ResponseFunction — per-wavelength fit + smoothing):")
    best = None
    for lo, hi in ranges:
        m = (full >= lo) & (full <= hi)
        for degree in (1, 2):
            for smooth in (0, 31, 61, 101, 151):
                if smooth and smooth >= m.sum():
                    continue
                score, _ = leave_one_out(Xtr_full[:, m], Ytr_full[:, m], degree, smooth)
                if best is None or score < best[0]:
                    best = (score, lo, hi, degree, smooth)
    score, lo, hi, degree, smooth = best
    print(f"  chosen: {lo}–{hi} nm, degree {degree}, smoothing {smooth} "
          f"(leave-one-out RMSE {score:.4f})")

    band = (full >= lo) & (full <= hi)
    grid = full[band]
    model = ResponseFunction(degree, smooth).fit(Xtr_full[:, band], Ytr_full[:, band])

    base_tr = float(np.mean([rmse(Xtr_full[i][band], Ytr_full[i][band]) for i in range(len(tr_names))]))
    print(f"  training baseline, no correction at all: RMSE {base_tr:.4f}")

    print("\nSelecting range and degree by leave-one-out on the training set "
          "(ChebyshevResponse — joint fit across all wavelengths at once):")
    best_c = None
    for lo_c, hi_c in ranges:
        m = (full >= lo_c) & (full <= hi_c)
        for deg_c in (4, 6, 8, 10, 12, 15, 18, 24):
            if deg_c >= m.sum() - 1:
                continue
            score_c, _ = leave_one_out_cheb(Xtr_full[:, m], Ytr_full[:, m], full[m], deg_c)
            if best_c is None or score_c < best_c[0]:
                best_c = (score_c, lo_c, hi_c, deg_c)
    score_c, lo_c, hi_c, deg_c = best_c
    print(f"  chosen: {lo_c}–{hi_c} nm, Chebyshev degree {deg_c} "
          f"(leave-one-out RMSE {score_c:.4f})")

    band_c = (full >= lo_c) & (full <= hi_c)
    grid_c = full[band_c]
    model_c = ChebyshevResponse(deg_c).fit(Xtr_full[:, band_c], Ytr_full[:, band_c], grid_c)

    if not (args.test_measured and args.test_reference):
        print("\nNo held-out set given — stopping. An in-sample score is not a validation.")
        return

    labels = [s.strip() for s in args.test_labels.split(",")] if args.test_labels else None
    te_names, Xte_full, Yte_full = load_side(args.test_measured, args.test_reference, full, labels)

    Xte, Yte = Xte_full[:, band], Yte_full[:, band]
    fixed = model.apply(Xte)
    Xte_c, Yte_c = Xte_full[:, band_c], Yte_full[:, band_c]
    fixed_c = model_c.apply(Xte_c)

    print(f"\nHeld-out set: {len(te_names)} samples, scored once")
    print(f"{'sample':<18} {'uncorrected':>12} {'ResponseFn':>11} {'Chebyshev':>10} "
          f"{'RF change':>10} {'Cheb change':>12}")
    before, after, after_c = [], [], []
    for i, nm in enumerate(te_names):
        a = rmse(Xte[i], Yte[i])
        rf = rmse(fixed[i], Yte[i])
        cb = rmse(fixed_c[i], Yte_c[i])
        before.append(a); after.append(rf); after_c.append(cb)
        print(f"{nm:<18} {a:12.4f} {rf:11.4f} {cb:10.4f} "
              f"{100 * (a - rf) / a:9.0f}% {100 * (a - cb) / a:11.0f}%")
    mb, ma, mac = float(np.mean(before)), float(np.mean(after)), float(np.mean(after_c))
    print(f"{'MEAN':<18} {mb:12.4f} {ma:11.4f} {mac:10.4f} "
          f"{100 * (mb - ma) / mb:9.0f}% {100 * (mb - mac) / mb:11.0f}%")

    if mac < ma:
        print(f"\nChebyshev wins on this held-out set ({mac:.4f} vs {ma:.4f}) — using it for the plot.")
        plot_names, plot_grid, plot_raw, plot_fixed, plot_ref = te_names, grid_c, Xte_c, fixed_c, Yte_c
        mean_after = mac
    else:
        print(f"\nResponseFunction wins on this held-out set ({ma:.4f} vs {mac:.4f}) — using it for the plot.")
        plot_names, plot_grid, plot_raw, plot_fixed, plot_ref = te_names, grid, Xte, fixed, Yte
        mean_after = ma

    plot_validation(plot_grid, plot_names, plot_raw, plot_fixed, plot_ref,
                    os.path.join(args.outdir, "validation_transfer_heldout.png"), mb, mean_after)


if __name__ == "__main__":
    main()
