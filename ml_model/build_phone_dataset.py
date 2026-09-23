#!/usr/bin/env python3
"""Build an Earth-frame training and evaluation dataset from real phone driving sessions.

Decimates raw IMU to uniform 10 Hz, transforms device acceleration to world/Earth
frame using the rotation vector, and extracts 10-second (100 sample) windows paired
with ground-truth GPS speed, stationary flags, and heading rates.
"""

from __future__ import annotations

import glob
import json
import math
import os
import sys

import numpy as np
import torch

sys.path.insert(0, ".")
import eval.session_eval as se

DT = 0.1                      # 10 Hz
WINDOW_SAMPLES = 100          # 10 seconds of context
STRIDE_SAMPLES = 5            # 0.5s stride for dense coverage
GRAVITY = 9.80665
MAX_ACCURACY_M = 30.0

HELD_OUT_TEST = {"20260915_184316", "20260904_195146"}
HELD_OUT_VAL = {"20260912_220347", "20260828_210037"}


def find_all_session_dirs():
    candidates = []
    for root in ["extracted_sessions/sessions", "extracted_sessions"]:
        if not os.path.exists(root):
            continue
        for d in os.listdir(root):
            p = os.path.join(root, d)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "gps.csv")) and os.path.exists(os.path.join(p, "imu.csv")):
                candidates.append(os.path.abspath(p))
    # Deduplicate by folder basename
    seen = {}
    for p in candidates:
        name = os.path.basename(p)
        if name not in seen:
            seen[name] = p
    return list(seen.values())


def process_session(sess_dir):
    name = os.path.basename(sess_dir)
    try:
        sess = se.load_session(sess_dir)
        grid = se.build_grid(sess)
    except Exception as e:
        # Log the reason so we know what to fix
        print(f"  [SKIP] {name}: {e}")
        return None

    speed = grid["speed"]
    valid_speed = np.isfinite(speed)
    if valid_speed.sum() < 20:
        print(f"  [SKIP] {name}: only {valid_speed.sum()} valid speed samples")
        return None

    # Check rotation vector availability
    if "R" not in grid or grid["R"] is None:
        print(f"  [SKIP] {name}: no rotation vector data")
        return None

    # Accelerometer in Earth frame (vertical gravity subtracted)
    try:
        acc_earth = se.earth_frame(grid["acc"], grid["R"])
    except Exception as e:
        print(f"  [SKIP] {name}: earth_frame failed: {e}")
        return None

    gyr = grid["gyro"]

    # 6 channels: eax, eay, eaz - 9.80665, gx, gy, gz
    feats = np.concatenate([acc_earth, gyr], axis=1)  # (N, 6)

    # Clean any NaNs in features
    for col in range(6):
        nans = ~np.isfinite(feats[:, col])
        if nans.any():
            feats[nans, col] = np.nanmean(feats[:, col]) if (~nans).any() else 0.0

    # Yaw rate from bearing derivative when moving
    bearing = grid["bearing"]
    yaw_rate = np.zeros(len(speed))
    moving = (speed >= 1.5) & np.isfinite(bearing)
    if moving.sum() > 2:
        un = np.unwrap(np.radians(bearing[moving]))
        t_mov = grid["t"][moving]
        d_psi = np.gradient(un, t_mov)
        # Filter unrealistically high spikes (> 1.5 rad/s)
        d_psi = np.clip(d_psi, -1.5, 1.5)
        yaw_rate[moving] = d_psi

    # Extract windows
    N = len(feats)
    windows = []
    targets = []

    for end_idx in range(WINDOW_SAMPLES, N, STRIDE_SAMPLES):
        sp = speed[end_idx]
        if not np.isfinite(sp):
            continue

        w = feats[end_idx - WINDOW_SAMPLES:end_idx].T  # (6, 100)
        if not np.all(np.isfinite(w)):
            continue

        is_stat = 1.0 if sp < 0.2 else 0.0
        yr = yaw_rate[end_idx]
        lat_acc = 0.0  # auxiliary
        if is_stat == 0.0 and abs(yr) > 0.01:
            lat_acc = float(np.clip(sp * yr, -5.0, 5.0))

        windows.append(w)
        targets.append([sp, is_stat, yr, lat_acc])

    if not windows:
        print(f"  [SKIP] {name}: no valid windows extracted")
        return None

    windows_arr = np.stack(windows).astype(np.float32)  # (B, 6, 100)
    targets_arr = np.array(targets, dtype=np.float32)    # (B, 4)

    # Speed-stratified upsampling: duplicate high-speed windows (>5 m/s)
    # to counterbalance the heavy stationary/slow bias
    fast_mask = targets_arr[:, 0] > 5.0
    n_fast = fast_mask.sum()
    if n_fast > 0 and n_fast < len(targets_arr) * 0.3:
        # Upsample fast windows to 30% of dataset
        n_needed = int(len(targets_arr) * 0.3) - n_fast
        if n_needed > 0:
            fast_idx = np.where(fast_mask)[0]
            upsample_idx = np.random.choice(fast_idx, size=min(n_needed, len(fast_idx) * 3), replace=True)
            windows_arr = np.concatenate([windows_arr, windows_arr[upsample_idx]], axis=0)
            targets_arr = np.concatenate([targets_arr, targets_arr[upsample_idx]], axis=0)

    return {
        "name": name,
        "windows": windows_arr,
        "targets": targets_arr,
    }


def main():
    print("=" * 70)
    print("BUILDING PHONE EARTH-FRAME TRAINING DATASET")
    print("=" * 70)

    session_dirs = find_all_session_dirs()
    print(f"Found {len(session_dirs)} candidate sessions.")

    all_windows = []
    all_targets = []
    all_run_ids = []
    all_splits = []
    run_names = []

    total_valid_runs = 0
    for s_dir in session_dirs:
        res = process_session(s_dir)
        if res is None:
            continue

        name = res["name"]
        w = res["windows"]
        tgt = res["targets"]
        run_id = total_valid_runs
        total_valid_runs += 1
        run_names.append(name)

        if name in HELD_OUT_TEST:
            split_code = 2  # test
        elif name in HELD_OUT_VAL:
            split_code = 1  # val
        else:
            split_code = 0  # train

        n_w = len(w)
        all_windows.append(w)
        all_targets.append(tgt)
        all_run_ids.append(np.full(n_w, run_id, dtype=np.int64))
        all_splits.append(np.full(n_w, split_code, dtype=np.int64))

        sp_mean = np.mean(tgt[:, 0])
        stat_pct = np.mean(tgt[:, 1]) * 100.0
        split_name = ["TRAIN", "VAL", "TEST"][split_code]
        print(f"  [{split_name:5}] {name}: {n_w} windows, mean speed: {sp_mean:.2f} m/s, stopped: {stat_pct:.1f}%")

    if not all_windows:
        print("Error: No valid windows extracted!")
        return 1

    W = np.concatenate(all_windows, axis=0)
    Y = np.concatenate(all_targets, axis=0)
    R = np.concatenate(all_run_ids, axis=0)
    S = np.concatenate(all_splits, axis=0)

    print("\nDataset Summary:")
    print(f"  Total windows: {len(W):,}")
    print(f"  Train: {(S == 0).sum():,}  |  Val: {(S == 1).sum():,}  |  Test: {(S == 2).sum():,}")
    print(f"  Speed stats: min={Y[:, 0].min():.2f}, mean={Y[:, 0].mean():.2f}, max={Y[:, 0].max():.2f} m/s")
    print(f"  Stationary ratio: {np.mean(Y[:, 1] == 1.0) * 100:.1f}%")

    out_path = os.path.join("ml_model", "dataset_phone_earth.pt")
    out_dict = {
        "windows": torch.from_numpy(W),
        "targets": torch.from_numpy(Y),
        "run_ids": torch.from_numpy(R),
        "split": torch.from_numpy(S),
        "run_names": run_names,
        "frame": "earth",
        "target_names": ["speed_mps", "stationary", "yaw_rate_rads", "lateral_acc_ms2"],
    }
    torch.save(out_dict, out_path)
    print(f"\nSaved dataset to {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
