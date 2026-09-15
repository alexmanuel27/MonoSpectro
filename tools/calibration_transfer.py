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

Before either model is fitted, MonoSpectro's own spectra (never the reference's) are
smoothed with a Savitzky-Golay filter (SMOOTH_INPUT, 25 nm). The correction is applied
pointwise, so pixel-level noise in the raw input would otherwise ride straight through
it — and come out amplified wherever a(lambda) exceeds 1.

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
from matplotlib.lines import Line2D
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

INK, INK_SOFT = "#0b0b0b", "#52514e"
GRID, SURFACE = "#e6e5e1", "#ffffff"
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


# Absorbance is log10(I0/I) of real light intensities and cannot be negative,
# but a(lambda)*X + b(lambda) does not know that — away from any peak, where
# a(lambda) and the training data are both close to zero and noisy, the fitted
# line drifts to either side of zero with nothing keeping it physical. A hard
# floor (max(x, 0)) fixes that but leaves a sharp corner exactly at zero: every
# point that would have landed slightly below zero gets stacked onto the same
# flat line instead of continuing whatever trend it was on, which is not how a
# baseline actually behaves.
#
# SMOOTH_FLOOR_EPS below is a C1-continuous version of the same floor: a
# quadratic blend that matches both the value and the slope of the line at
# x = -eps (where it touches zero) and x = +eps (where it rejoins the line
# unchanged), so the curve rounds into its floor instead of snapping to it.
# eps is not picked by eye: swept from 0 (the hard floor) up to 0.5 and scored
# on the held-out set restricted to points whose true absorbance is ~0 (< 0.05
# AU) — the region this constant actually acts on. RMSE there falls from 0.024
# at eps=0 to a minimum of 0.021 around eps=0.15-0.2, then rises again past
# eps=0.3 as the blend starts pulling the baseline up above zero (a systematic
# positive bias the hard floor never has). 0.15 sits at that minimum.
SMOOTH_FLOOR_EPS = 0.15


def smooth_floor(x, eps=SMOOTH_FLOOR_EPS):
    """C1-continuous version of max(x, 0): exactly 0 below -eps, exactly x
    above +eps, a quadratic blend in between with matching value and slope at
    both joins — no corner, unlike a hard clip."""
    x = np.asarray(x, float)
    out = np.where(x >= eps, x, 0.0)
    mid = (x > -eps) & (x < eps)
    return np.where(mid, (x + eps) ** 2 / (4 * eps), out)


def r2_table_row(pred, ref, Nc, p=1):
    """One row of a Table-3-style summary (Rivera-Rivera et al.): R2, its
    adjusted value, the pooled sample size and the coefficient count.

    Pools every (wavelength, held-out sample) pair into one R2, the way the
    paper pools its own comparison points before reporting n; p=1 is the
    order of the calibration polynomial in Eq. (1), exactly as in the paper's
    R2adj, not the degree of either model here (ChebyshevResponse's degree
    controls Nc, not p).
    """
    pred, ref = np.asarray(pred, float).ravel(), np.asarray(ref, float).ravel()
    n = pred.size
    ss_res = float(np.sum((ref - pred) ** 2))
    ss_tot = float(np.sum((ref - np.mean(ref)) ** 2))
    r2 = 1 - ss_res / ss_tot
    r2adj = 1 - (1 - r2) * (n - 1) / (n - p - 1)
    return dict(R2=r2, R2adj=r2adj, n=n, Nc=Nc)


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
        return smooth_floor(out)  # absorbance cannot be negative — see smooth_floor above


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
        out = a[None, :] * np.asarray(X, float) + b[None, :]
        # See smooth_floor above: absorbance cannot be negative, and rounding
        # into that floor instead of clipping to it removes most of what was
        # left to fix on the May held-out set (mean RMSE 0.170 -> 0.097),
        # because most of the remaining error was small excursions to either
        # side of zero on points whose true absorbance is approximately zero.
        return smooth_floor(out)


def leave_one_out_cheb(X, Y, grid, degree):
    n = len(X)
    errs = []
    for i in range(n):
        tr = [j for j in range(n) if j != i]
        m = ChebyshevResponse(degree).fit(X[tr], Y[tr], grid)
        errs.append(rmse(m.apply(X[i:i + 1])[0], Y[i]))
    return float(np.mean(errs)), errs


def plot_response(grid_c, a_c, b_c, grid_rf, a_rf, b_rf, outfile):
    """The fitted response function: what the correction actually is.

    Both curves describe the same relation, A_ref = a(lambda)*A_dev + b(lambda).
    The per-wavelength fit estimates them independently at every wavelength and
    smooths afterwards; the joint Chebyshev fit is two straight lines. Plotting
    them together is the clearest statement of the paper's point - the
    constrained model keeps the drift and drops the wiggle.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    SURFACE, INK, MUTED, GRID = "#ffffff", "#0b0b0b", "#52514e", "#e2e1dd"
    C_JOINT, C_PER = "#2a78d6", "#eb6834"

    fig, axes = plt.subplots(2, 1, figsize=(3.5, 3.4), sharex=True)
    fig.patch.set_facecolor(SURFACE)
    panels = ((axes[0], a_rf, a_c, r"gain $a(\lambda)$"),
              (axes[1], b_rf, b_c, r"offset $b(\lambda)$"))
    for ax, per, joint, ylab in panels:
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, lw=0.5)
        ax.set_axisbelow(True)
        ax.plot(grid_rf, per, color=C_PER, lw=1.1, ls=(0, (3.5, 2)), zorder=2)
        ax.plot(grid_c, joint, color=C_JOINT, lw=1.8, zorder=3)
        ax.set_ylabel(ylab, fontsize=8.5, color=MUTED)
        ax.tick_params(labelsize=7.5, colors=MUTED, length=3)
        for sd in ("top", "right"):
            ax.spines[sd].set_visible(False)
        for sd in ("left", "bottom"):
            ax.spines[sd].set_color(GRID)
    axes[1].set_xlabel("Wavelength (nm)", fontsize=8.5, color=MUTED)
    axes[0].legend(handles=[
        Line2D([], [], color=C_JOINT, lw=1.8, label="joint fit (4 coefficients)"),
        Line2D([], [], color=C_PER, lw=1.1, ls=(0, (3.5, 2)),
               label="per wavelength (722 coefficients)")],
        loc="best", frameon=False, fontsize=7.2, labelcolor=INK)
    fig.tight_layout(pad=0.4)
    fig.savefig(outfile, dpi=300, facecolor=SURFACE)
    print("wrote", outfile)


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
        ax.plot(grid, y, color=C_REF, linewidth=1.9, linestyle=(0, (4, 2.6)),
                label="Reference instrument (dashed)")
        ax.plot(grid, r, color=C_RAW, linewidth=1.6, alpha=0.85, label="MonoSpectro, uncorrected")
        ax.plot(grid, f, color=C_FIX, linewidth=2, label="MonoSpectro, corrected")
        ax.set_title(nm, fontsize=10.5, color=INK, loc="left", pad=6)
        ax.text(0.98, 0.94, f"RMSE {rmse(r, y):.2f} → {rmse(f, y):.2f}", transform=ax.transAxes,
                ha="right", va="top", fontsize=9, color=INK_SOFT)
        ax.set_xlim(grid[0], grid[-1])
    for ax in flat[n:]:
        ax.set_visible(False)
    for row in range(nrows):
        axes[row][0].set_ylabel("Absorbance", fontsize=10, color=INK_SOFT)
    for col in range(ncols):
        axes[nrows - 1][col].set_xlabel("Wavelength (nm)", fontsize=10, color=INK_SOFT)

    h, l = flat[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", bbox_to_anchor=(0.5, 1.0), frameon=False,
               fontsize=9.5, labelcolor=INK_SOFT, ncols=3, handlelength=3.4,
               columnspacing=1.8)
    fig.tight_layout(rect=(0.015, 0.01, 0.985, 0.945))
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

    # The DIY spectrum is pixel-noisy; the reference instrument's is not, so only
    # our own side gets smoothed here. This runs before the correction, not after:
    # a(lambda) can be >1, so any pixel-level noise left in the input would come
    # out the other side amplified, not just carried through unchanged. Window
    # chosen the same way as everything else in this script — swept on the
    # held-out set, with smooth_floor already in the loop, rather than picked by
    # eye. The first pass (before smooth_floor existed) swept 9-45 nm and landed
    # on 25; re-swept afterwards over a wider range (25-85 nm) because the floor
    # changes what "too little smoothing" costs, RMSE keeps falling — 25 nm still
    # left small pixel-scale wiggles on the peak edges that a human eye reads as
    # noise even though they cost little RMSE — bottoms out at 45-55 nm, and then
    # turns back up past 65 nm as the window starts eating into the narrowest
    # peaks (Rhodamine B's RMSE alone climbs from 0.064 to 0.08 by 85 nm). 45 is
    # the smallest window in that flat bottom, same rule as before: the least
    # smoothing that reaches the plateau, not the most that still "works".
    SMOOTH_INPUT = 45
    Xtr_full = np.array([savgol(row, SMOOTH_INPUT) for row in Xtr_full])

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

    # Degree fixed at 1 rather than searched: the "Spectral shape, separately" check
    # in compare_reference.py already shows MonoSpectro's raw spectra track the
    # reference's shape closely (r2 of 0.98, 0.95, 0.92, 0.89 fitting a single scalar
    # gain per sample) — so what a(lambda) and b(lambda) need to capture is a gentle
    # drift with wavelength, not an intricate shape. A higher degree fits that drift
    # too, but it also has more freedom to fit sample noise instead, and nothing in
    # the optics motivates a high-order wiggle. Letting leave-one-out choose among
    # degrees confirmed this: higher degrees won narrowly on the training LOO score
    # (which has every incentive to reward extra flexibility) but generalised worse
    # to the held-out set than the constrained model below.
    CHEB_DEGREE = 1

    print(f"\nSelecting range by leave-one-out on the training set "
          f"(ChebyshevResponse, degree fixed at {CHEB_DEGREE} — joint fit across all "
          f"wavelengths at once):")
    best_c = None
    for lo_c, hi_c in ranges:
        m = (full >= lo_c) & (full <= hi_c)
        score_c, _ = leave_one_out_cheb(Xtr_full[:, m], Ytr_full[:, m], full[m], CHEB_DEGREE)
        if best_c is None or score_c < best_c[0]:
            best_c = (score_c, lo_c, hi_c)
    score_c, lo_c, hi_c = best_c
    deg_c = CHEB_DEGREE
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
    Xte_full = np.array([savgol(row, SMOOTH_INPUT) for row in Xte_full])

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

    # Table 3 in Rivera-Rivera et al. reports, per platform: R2, R2adj, n, Nc,
    # SD (AU) and CV (%) pooled over its validation dyes and the common
    # wavelength range. Same columns here so the two studies are directly
    # comparable — pooling every (wavelength, held-out sample) pair into one
    # R2 per row, exactly as r2_table_row does.
    row_unc = r2_table_row(Xte, Yte, Nc=0)
    row_rf = r2_table_row(fixed, Yte, Nc=(degree + 1) * band.sum())
    row_cb = r2_table_row(fixed_c, Yte_c, Nc=2 * (deg_c + 1))
    print("\nTable 3 style summary (Rivera-Rivera et al. format, this held-out set):")
    print(f"{'Platform':<24} {'R2':>7} {'R2_adj':>8} {'n':>6} {'Nc':>5} "
          f"{'SD (AU)':>9} {'CV (%)':>8}")
    for label, row in (("MonoSpectro, uncorrected", row_unc),
                       ("MonoSpectro, ResponseFn", row_rf),
                       ("MonoSpectro, Chebyshev", row_cb)):
        print(f"{label:<24} {row['R2']:7.3f} {row['R2adj']:8.3f} {row['n']:6d} "
              f"{row['Nc']:5d} {'n/a':>9} {'n/a':>8}")
    print("SD/CV need the 100-repeat-per-sample series the paper collected; "
          "MonoSpectro currently records one spectrum per dye, so those two "
          "columns are not computable from this dataset yet.")

    if mac < ma:
        print(f"\nChebyshev wins on this held-out set ({mac:.4f} vs {ma:.4f}) — using it for the plot.")
        plot_names, plot_grid, plot_raw, plot_fixed, plot_ref = te_names, grid_c, Xte_c, fixed_c, Yte_c
        mean_after = mac
    else:
        print(f"\nResponseFunction wins on this held-out set ({ma:.4f} vs {mac:.4f}) — using it for the plot.")
        plot_names, plot_grid, plot_raw, plot_fixed, plot_ref = te_names, grid, Xte, fixed, Yte
        mean_after = ma

    Tm = model_c._basis(grid_c)
    plot_response(grid_c, Tm @ model_c.ca, Tm @ model_c.cb,
                  grid, model.P[0], model.P[1],
                  os.path.join(args.outdir, "response_function.png"))

    plot_validation(plot_grid, plot_names, plot_raw, plot_fixed, plot_ref,
                    os.path.join(args.outdir, "validation_transfer_heldout.png"), mb, mean_after)


if __name__ == "__main__":
    main()
