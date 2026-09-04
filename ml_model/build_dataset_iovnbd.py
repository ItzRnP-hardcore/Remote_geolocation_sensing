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


def stationary_bias(feats, speed, n_channels=6):
    """Per-run DC offset, measured while the vehicle is stopped.

    The standardisation in train_iovnbd.py is global: one mean and standard deviation
    per channel over the whole training split. A run whose accelerometer sits at a
    different DC level - different mounting tilt, different handset, different
    temperature - is therefore presented to the network shifted, and a softplus speed
    head can absorb that shift as a constant speed offset of either sign. That is
    exactly the observed failure: +1.45 m/s bias on one test run, -3.49 on the other.

    The same fix has already been shown to work on the sibling signal in this project.
    Removing the gyroscope's per-run bias from pre-outage data cut 300 s heading drift
    from 46% to 24%; the accelerometer channels had simply never had the equivalent.

    Estimated from stationary samples rather than the whole run, because the mean over
    a whole run also contains its speed profile, and subtracting that would remove
    signal along with offset. On-device the same estimate comes from the vehicle's own
    stops, so this is deployable rather than an oracle.

    Note what this removes on the vertical channel: in the vehicle frame it still
    carries gravity, so the stationary mean is about 9.81 there, plus whatever tilt
    leakage sits on forward and lateral. The leakage is the per-session term worth
    removing; the constant gravity was already being absorbed by standardisation.
    """
    stat = np.asarray(speed) < STATIONARY_MS
    if stat.sum() >= 100:
        return feats[stat].mean(axis=0)
    # Too few stops to measure it. The median over the run is a poor substitute for a
    # stationary mean but is far more robust than the mean to the speed profile.
    return np.median(feats, axis=0)


def build(npz_path: str, frame: str, debias: str = "none"):
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

    windows, targets, run_ids, biases = [], [], [], []
    for ri, (s0, n) in enumerate(zip(starts, lengths)):
        a = acc_all[s0:s0 + n]
        g = gyr_all[s0:s0 + n]
        sp = speed_all[s0:s0 + n]
        yr = yaw_all[s0:s0 + n]
        la = lat_acc_all[s0:s0 + n]
        feats = np.concatenate([a, g], axis=1)          # (n, 6)

        if debias != "none":
            bias = stationary_bias(feats, sp)
            if debias == "accel":
                # The gyro bias is handled in the navigation path already, so this
                # ablation isolates whether the accelerometer offset is the culprit.
                bias = np.concatenate([bias[:3], np.zeros(3)])
            feats = feats - bias
            biases.append(bias)
        else:
            biases.append(np.zeros(feats.shape[1]))

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
    return X, Y, R, names, np.asarray(biases)


# IO-VNBD organises its journeys by driver, and the folder names carry it. A driver
# is a proxy for the things that actually differ between deployments - vehicle, phone,
# mounting - so holding one out measures something a held-out journey does not.
DRIVER_OF = {"M": "B", "S": "A", "Y": "D", "Vf": "E", "Vta": "E", "Vtb": "E", "Vw": "E"}


def driver_of(run_name: str) -> str:
    """Driver letter for a run, longest prefix first so Vta beats V."""
    base = run_name.split("_r")[0]
    for pref in sorted(DRIVER_OF, key=len, reverse=True):
        if base.startswith(pref):
            return DRIVER_OF[pref]
    return "?"


def split_by_driver(run_ids, n_runs, names, test_driver):
    """Hold out an entire driver.

    Splitting by journey answers "does this generalise to another drive?". It does not
    answer "does this generalise to another car and another phone in another mount?",
    which is the question the measured failure actually poses - a per-session speed
    bias with opposite signs. Held-out journeys share a driver, a vehicle and an area:
    the S3c test box overlaps the S3a training box by 46%, so the two are not
    independent in any strong sense.
    """
    which_run = np.zeros(n_runs, dtype=int)
    rest = []
    for i, nm in enumerate(names):
        if driver_of(nm) == test_driver:
            which_run[i] = 2
        else:
            rest.append(i)
    counts = np.bincount(run_ids, minlength=n_runs).astype(float)
    # Validation still comes from the training drivers, largest-first to the target.
    target = VAL_FRACTION * counts[rest].sum()
    have = 0.0
    for i in sorted(rest, key=lambda k: -counts[k]):
        if have < target:
            which_run[i] = 1
            have += counts[i]
    return (which_run[run_ids],
            [i for i in range(n_runs) if which_run[i] == 2],
            [i for i in range(n_runs) if which_run[i] == 1])


def split_by_run(run_ids, n_runs, seed: int = 0, names=None, fixed_test=()):
    """Whole runs to train/val/test, balanced by WINDOW count.

    Splitting on run boundaries is non-negotiable - windows overlap 50%, so any split
    that cuts between them reports partial memorisation. But assigning a fixed FRACTION
    OF RUNS is wrong when run lengths differ by an order of magnitude: on the 26-run
    set that put 4,729 windows in validation against 5,755 in training. So runs are
    assigned greedily, longest first, to whichever split is furthest below its target
    share of windows.

    [fixed_test] pins named runs to the test split. Holding the test set constant
    across experiments is what makes "did more data help" answerable at all; letting it
    move means every comparison also changes its own yardstick.
    """
    counts = np.bincount(run_ids, minlength=n_runs).astype(float)
    which_run = np.zeros(n_runs, dtype=int)
    assigned = np.zeros(n_runs, dtype=bool)

    pinned = set()
    if names is not None:
        for i, nm in enumerate(names):
            if nm in fixed_test:
                which_run[i] = 2
                assigned[i] = True
                pinned.add(i)

    total = counts.sum()
    target = {0: (1 - VAL_FRACTION - TEST_FRACTION) * total,
              1: VAL_FRACTION * total,
              2: TEST_FRACTION * total}
    have = {0: 0.0, 1: 0.0, 2: float(counts[list(pinned)].sum()) if pinned else 0.0}

    # Pinning is exclusive: naming the test runs means those runs AND NO OTHERS are
    # the test set. Topping it up to a target share would quietly change the yardstick
    # between experiments, which defeats the point of pinning it.
    splits = (0, 1) if pinned else (0, 1, 2)

    rng = np.random.default_rng(seed)
    order = sorted((i for i in range(n_runs) if not assigned[i]),
                   key=lambda i: (-counts[i], rng.random()))
    for i in order:
        # Furthest below target, measured as a shortfall fraction so the splits
        # compete on the same scale.
        pick = min(splits, key=lambda k: (have[k] - target[k]) / max(target[k], 1))
        which_run[i] = pick
        have[pick] += counts[i]

    which = which_run[run_ids]
    return (which,
            [i for i in range(n_runs) if which_run[i] == 2],
            [i for i in range(n_runs) if which_run[i] == 1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--npz", default=os.path.join("dataset", "iovnbd_train.npz"))
    ap.add_argument("--out", default=os.path.join("ml_model", "dataset_iovnbd.pt"))
    ap.add_argument("--frame", choices=("vehicle", "earth"), default="vehicle")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--test-driver", default="",
                    help="hold out an entire driver (A, B, D or E) as the test set")
    ap.add_argument("--debias", choices=("none", "all", "accel"), default="none",
                    help="subtract each run's stationary-mean offset")
    ap.add_argument("--fixed-test", default="",
                    help="comma-separated run names pinned to the test split")
    args = ap.parse_args(argv)

    X, Y, R, names, biases = build(args.npz, args.frame, args.debias)
    if not len(X):
        print("no windows built")
        return 1
    n_runs = len(names)
    if args.test_driver:
        which, test_runs, val_runs = split_by_driver(R, n_runs, names, args.test_driver)
    else:
        fixed = {x.strip() for x in args.fixed_test.split(",") if x.strip()}
        which, test_runs, val_runs = split_by_run(R, n_runs, args.seed, names, fixed)

    print(f"frame        : {args.frame}   debias: {args.debias}")
    print(f"windows      : {len(X):,}  shape {tuple(X.shape[1:])}")
    print(f"runs         : {n_runs}")
    print(f"  train {int((which == 0).sum()):>6}  val {int((which == 1).sum()):>6}"
          f"  test {int((which == 2).sum()):>6}   (split by run, no window overlap across it)")
    from collections import Counter
    drivers = Counter(driver_of(n) for n in names)
    print(f"drivers present   : {dict(sorted(drivers.items()))}")
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
        "run_bias": torch.tensor(biases, dtype=torch.float32),
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
