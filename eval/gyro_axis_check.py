"""Which IO-VNBD gyro column is the yaw axis, and what integrating the wrong one costs.

Written to be sent: every number in docs/Review_SIH_2026.pdf that concerns the gyroscope
comes from this file, so it can be re-run by anyone with the IO-VNBD archive.

IO-VNBD S-files carry three gyro columns headed "Yaw", "Pitch" and "Roll". The headers do
not describe them, and they are not in the accelerometer's axis order. Three checks:

  1. Per session: |r| of each raw file column against the vehicle's own yaw rate (V-file).
     Row-index aligned with no lag search, so absolute values are depressed on some runs;
     the ranking is what matters.
  2. Across the whole converted dataset (lag-aligned by eval.iovnbd): r of each column, and
     of the level-frame gyro z that SIH_2026's data.windows.levelled_channels produces when
     the columns are taken in file order.
  3. Drift over 60 s and 300 s windows with PERFECT speed, heading integrated from one
     column at a time, each column debiased from stand-still and given whichever sign suits
     it best - so a column is never penalised for a convention, only for the wrong signal.

Run:  python -m eval.gyro_axis_check
"""

from __future__ import annotations

import math
import sys

import numpy as np

sys.path.insert(0, "ml_model")

from . import iovnbd as io
from . import model_dr_eval as M

NPZ = "dataset/iovnbd_train.npz"

# Our converted npz stores gyro in DEVICE order, device = file[[0, 2, 1]]. The permutation is
# its own inverse, so file column k is device column FILE_TO_DEVICE[k].
FILE_TO_DEVICE = {0: 0, 1: 2, 2: 1}
LABEL = {0: "file 'Yaw'", 1: "file 'Pitch'", 2: "file 'Roll'"}


def per_session():
    print("1. |r| of each raw file column against the vehicle's yaw rate, per session\n")
    print(f"{'session':12s}{'Yaw':>8}{'Pitch':>8}{'Roll':>8}   yaw axis")
    rows = []
    for name, s_path, v_path in io.discover_sessions():
        try:
            s = io._read_numeric(s_path, 21)
            v = io._read_numeric(v_path, 18)
        except Exception:
            continue
        n = min(len(s), len(v))
        if n < 3000:
            continue
        g = s[:n, list(io.S_GYRO)]
        yr = v[:n, io.V_YAW_RATE_DPS]
        ok = np.all(np.isfinite(g), axis=1) & np.isfinite(yr)
        if ok.sum() < 3000:
            continue
        r = [abs(np.corrcoef(g[ok, j], yr[ok])[0, 1]) for j in range(3)]
        rows.append(r)
        print(f"{name:12s}{r[0]:8.3f}{r[1]:8.3f}{r[2]:8.3f}   {LABEL[int(np.argmax(r))]}")
    wins = [int(np.argmax(r)) for r in rows]
    print(f"\n   yaw axis over {len(rows)} sessions: " +
          ", ".join(f"{LABEL[k]} {wins.count(k)}" for k in range(3)))


def dataset_level():
    d = np.load(NPZ, allow_pickle=True)
    g, yr, acc = d["gyro"], d["truth_yaw_rate"], d["accel"]
    print("\n2. Pearson r against the vehicle's yaw rate, whole converted dataset\n")
    for k in range(3):
        col = g[:, FILE_TO_DEVICE[k]]
        m = np.isfinite(col) & np.isfinite(yr)
        print(f"   {LABEL[k]:14s} r = {np.corrcoef(col[m], yr[m])[0, 1]:+.3f}")

    # SIH_2026's levelled_channels: gyro columns in FILE order are treated as body x, y, z
    # and rotated by the accelerometer's level frame, whose +z is "down". Reconstructed per
    # run with down from the median acceleration, since the phone is clamped.
    out = np.full(len(yr), np.nan)
    for s0, n in zip(d["run_starts"], d["run_lengths"]):
        sl = slice(int(s0), int(s0) + int(n))
        down = np.nanmedian(acc[sl], axis=0)
        down /= np.linalg.norm(down)
        file_order = g[sl][:, [FILE_TO_DEVICE[k] for k in range(3)]]
        out[sl] = file_order @ down
    m = np.isfinite(out) & np.isfinite(yr)
    print(f"   {'level gyr_z (as SIH_2026 builds it)':14s} r = "
          f"{np.corrcoef(out[m], yr[m])[0, 1]:+.3f}")


def drift():
    d = np.load(NPZ, allow_pickle=True)
    runs = M.load_runs(NPZ)
    names = [str(x) for x in d["run_names"]]
    dev = {n: d["gyro"][int(s):int(s) + int(l)]
           for n, s, l in zip(names, d["run_starts"], d["run_lengths"])}
    out = {k: {60: [], 300: []} for k in range(3)}
    for run in runs:
        g = dev[run["name"]]
        still = np.nan_to_num(run["speed"], nan=99) < 0.5
        conv = M.yaw_sign_convention(run)
        for k in range(3):
            w = g[:, FILE_TO_DEVICE[k]].astype(float)
            w = w - (np.nanmean(w[still]) if still.sum() > 50 else np.nanmedian(w))
            best = None
            for sgn in (1.0, -1.0):
                run["gyro_yaw"] = sgn * np.nan_to_num(w)
                r = [M.score(run, None, None, conv, "truth", "gyro", seg)["drift_pct"]
                     for seg in M.windows_of(len(run["speed"]), 60)]
                r = [x for x in r if np.isfinite(x)]
                if r and (best is None or np.median(r) < best[0]):
                    best = (float(np.median(r)), sgn)
            if best is None:
                continue
            run["gyro_yaw"] = best[1] * np.nan_to_num(w)
            for dur in (60, 300):
                for seg in M.windows_of(len(run["speed"]), dur):
                    x = M.score(run, None, None, conv, "truth", "gyro", seg)
                    if x["distance_m"] > 100:
                        out[k][dur].append(x["drift_pct"])
    print("\n3. Median drift, PERFECT speed, heading integrated from one column\n")
    print(f"   {'heading from':16s}{'60 s':>9}{'300 s':>9}{'windows':>9}")
    for k in range(3):
        print(f"   {LABEL[k]:16s}{np.median(out[k][60]):8.1f}%{np.median(out[k][300]):8.1f}%"
              f"{len(out[k][60]):9d}")


if __name__ == "__main__":
    per_session()
    dataset_level()
    drift()
