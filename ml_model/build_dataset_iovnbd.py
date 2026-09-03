"""Build the training set from IO-VNBD instead of from our own walking sessions.

Replaces build_dataset.py, which produces 304 windows from a handful of on-foot
recordings around campus, with roughly thirty times as many windows of real driving
at real speeds. Three things change beyond the row count.

Real yaw-rate labels. build_dataset.py sets `df_merged['yaw_rate'] = 0.0` - a dummy -
so the model's fourth head has been regressing on a constant. IO-VNBD's vehicle
stream reports actual yaw rate at 10 Hz.

Real speeds. Our own sessions top out around 3.4 m/s, below the 5.0 m/s gate the
reference literature uses, so none of their outages were comparable. Here 75% of
samples are above it and the maximum is 32.5 m/s.

Splits by run, not by window. Windows overlap by 50% at stride 50, so a random split
puts near-duplicate windows on both sides of it and reports a validation score that
is partly memorisation. Splitting on run boundaries is the only honest option, and it
is worth being explicit about because the model side of this project has splits that
cannot currently be reproduced.

Two feature framings are offered because the better one is an open question worth
measuring rather than asserting:

  earth    levelled acceleration + gyro in the world frame, which is what
           resnet1d.py documents and what the current export expects.
  vehicle  acceleration and gyro resolved into (forward, right, up) using the
           per-session mounting rotation. Speed is then a scalar along one axis,
           which is the framing the reference work relies on, and it makes the
           non-holonomic constraint expressible: lateral velocity should stay near
           zero, and the vehicle's own lateral accelerometer is available to check it.

Run:  python -m ml_model.build_dataset_iovnbd --npz dataset/iovnbd_train.npz \
          --out ml_model/dataset_iovnbd.pt --frame vehicle
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import torch

WINDOW = 100          # 10 s at 10 Hz, matching resnet1d.WINDOW_SAMPLES
STRIDE = 50           # 50% overlap, as in build_dataset.py
STATIONARY_MS = 0.5   # same threshold build_dataset.py uses

# Fractions of RUNS, not of windows. Held-out runs are whole journeys.
VAL_FRACTION = 0.2
TEST_FRACTION = 0.2


def earth_frame(accel, quat):
    """Acceleration rotated into world (East, North, Up), gravity still included.

    Mirrors what the app does before feeding the model, and matches the row-major
    convention in eval/ahrs.quaternion_to_matrix.
    """
    x, y, z, w = quat[:, 0], quat[:, 1], quat[:, 2], quat[:, 3]
    sq1, sq2, sq3 = 2 * x * x, 2 * y * y, 2 * z * z
    xy, zw = 2 * x * y, 2 * z * w
    xz, yw = 2 * x * z, 2 * y * w
    yz, xw = 2 * y * z, 2 * x * w
    ax, ay, az = accel[:, 0], accel[:, 1], accel[:, 2]
    return np.stack([
        (1 - sq2 - sq3) * ax + (xy - zw) * ay + (xz + yw) * az,
        (xy + zw) * ax + (1 - sq1 - sq3) * ay + (yz - xw) * az,
        (xz - yw) * ax + (yz + xw) * ay + (1 - sq1 - sq2) * az,
    ], axis=1)


def build(npz_path: str, frame: str):
    d = np.load(npz_path, allow_pickle=True)
    starts = d["run_starts"]
    lengths = d["run_lengths"]
    names = [str(n) for n in d["run_names"]]

    if frame == "vehicle":
        acc_all, gyr_all = d["vehicle_accel"], d["vehicle_gyro"]
    elif frame == "earth":
        acc_all = earth_frame(d["accel"], d["quat"])
        gyr_all = d["gyro"]
    else:
        raise ValueError(f"unknown frame {frame!r}")

    speed_all = d["truth_speed"]
    yaw_all = d["truth_yaw_rate"]
    lat_acc_all = d["truth_lat_acc"]

    windows, targets, run_ids = [], [], []
    for ri, (s0, n) in enumerate(zip(starts, lengths)):
        a = acc_all[s0:s0 + n]
        g = gyr_all[s0:s0 + n]
        sp = speed_all[s0:s0 + n]
        yr = yaw_all[s0:s0 + n]
        la = lat_acc_all[s0:s0 + n]
        feats = np.concatenate([a, g], axis=1)          # (n, 6)

        # A window may not straddle a run boundary: the slice above already
        # guarantees that, since each run is indexed independently.
        for i in range(0, n - WINDOW, STRIDE):
            x = feats[i:i + WINDOW]
            end = i + WINDOW - 1
            y_sp, y_yr, y_la = sp[end], yr[end], la[end]
            if not np.isfinite(x).all():
                continue
            if not (np.isfinite(y_sp) and np.isfinite(y_yr)):
                continue
            windows.append(x.T)                          # (6, WINDOW)
            targets.append([y_sp,
                            1.0 if y_sp < STATIONARY_MS else 0.0,
                            y_yr,
                            y_la if np.isfinite(y_la) else 0.0])
            run_ids.append(ri)

    X = np.asarray(windows, dtype=np.float32)
    Y = np.asarray(targets, dtype=np.float32)
    R = np.asarray(run_ids, dtype=np.int64)
    return X, Y, R, names


def split_by_run(run_ids, n_runs, seed: int = 0):
    """Whole runs to train/val/test. Deterministic, so a rerun is comparable."""
    rng = np.random.default_rng(seed)
    order = rng.permutation(n_runs)
    n_test = max(1, int(round(TEST_FRACTION * n_runs)))
    n_val = max(1, int(round(VAL_FRACTION * n_runs)))
    test_runs = set(order[:n_test].tolist())
    val_runs = set(order[n_test:n_test + n_val].tolist())
    which = np.where(np.isin(run_ids, list(test_runs)), 2,
                     np.where(np.isin(run_ids, list(val_runs)), 1, 0))
    return which, sorted(test_runs), sorted(val_runs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--npz", default=os.path.join("dataset", "iovnbd_train.npz"))
    ap.add_argument("--out", default=os.path.join("ml_model", "dataset_iovnbd.pt"))
    ap.add_argument("--frame", choices=("vehicle", "earth"), default="vehicle")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    X, Y, R, names = build(args.npz, args.frame)
    if not len(X):
        print("no windows built")
        return 1
    n_runs = len(names)
    which, test_runs, val_runs = split_by_run(R, n_runs, args.seed)

    print(f"frame        : {args.frame}")
    print(f"windows      : {len(X):,}  shape {tuple(X.shape[1:])}")
    print(f"runs         : {n_runs}")
    print(f"  train {int((which == 0).sum()):>6}  val {int((which == 1).sum()):>6}"
          f"  test {int((which == 2).sum()):>6}   (split by run, no window overlap across it)")
    print(f"held-out val runs : {[names[i] for i in val_runs]}")
    print(f"held-out test runs: {[names[i] for i in test_runs]}")
    print()
    print(f"{'target':<22}{'mean':>10}{'sd':>10}{'min':>10}{'max':>10}")
    for i, nm in enumerate(("speed m/s", "stationary", "yaw rate rad/s",
                            "lateral acc m/s^2")):
        c = Y[:, i]
        print(f"{nm:<22}{c.mean():>10.4f}{c.std():>10.4f}{c.min():>10.4f}{c.max():>10.4f}")
    print()
    print(f"windows above the 5.0 m/s reference gate: "
          f"{(Y[:, 0] >= 5.0).mean() * 100:.1f}%")
    print(f"yaw-rate labels that are non-zero       : "
          f"{(Y[:, 2] != 0).mean() * 100:.1f}%   "
          f"(build_dataset.py had 0.0% - the head was regressing on a constant)")

    torch.save({
        "windows": torch.tensor(X),
        "targets": torch.tensor(Y),
        "run_ids": torch.tensor(R),
        "split": torch.tensor(which),
        "run_names": names,
        "frame": args.frame,
        "target_names": ["speed_mps", "stationary", "yaw_rate_rads", "lateral_acc_ms2"],
    }, args.out)
    print(f"\nwrote {args.out}")

    meta = args.out.replace(".pt", "_meta.json")
    with open(meta, "w", encoding="utf-8") as fh:
        json.dump({"frame": args.frame, "windows": int(len(X)), "runs": n_runs,
                   "val_runs": [names[i] for i in val_runs],
                   "test_runs": [names[i] for i in test_runs],
                   "window": WINDOW, "stride": STRIDE, "seed": args.seed}, fh, indent=1)
    print(f"wrote {meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
