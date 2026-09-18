"""Head-to-head: our speed model against pranjali2105/SIH_2026's, on OUR phone's drive.

There is no clean shared IO-VNBD test set. Her training groups include S and M, so our
held-out runs (S2_r1, S3c) are in her training data; her test sessions (a5-a8) are not in
our pipeline. The one yardstick neither model has seen - and the one that decides what
ships - is a drive recorded by this app on this phone.

Each model is fed exactly the features it was trained on:

  ours   earth frame (rotation-vector levelled, gravity removed), debiased gyro,
         100 samples, standardised by the training split's per-channel mean/sd -
         OR raw, which is what the exported asset receives, because neither
         export_model.TCNWrapper nor IMUModelRunner applies that standardisation.
  hers   tilt-levelled frame (median gravity to +z, gravity removed via the gravity
         sensor), 30 samples, her checkpoint's own norm_stats. Her gyro columns are
         IO-VNBD file order, which is NOT device-axis order (file col 1 is the yaw axis),
         so the "as trained" variant re-creates that permutation from our device axes.

Speed is scored against GNSS over the driving span, then both are integrated with the SAME
heading - the calibrated compass - so the drift comparison is about speed alone.

Run:  python -m eval.compare_teammate_model extracted_sessions/20260904_195146 \\
          --her-repo <path to SIH_2026 clone>
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
import sys

import numpy as np
import torch

from .session_eval import DT, build_grid, load_session, quat_to_matrix, resample

OUR_WINDOW = 100


def load_gravity(session_dir, t):
    """Device-frame gravity-sensor stream on the 10 Hz grid (session_eval skips it)."""
    ts, v = [], []
    with open(os.path.join(session_dir, "imu.csv"), encoding="utf-8") as fh:
        fh.readline()
        for line in fh:
            p = line.split(",")
            if p[1] == "gravity":
                ts.append(float(p[0]) / 1e9)
                v.append((float(p[3]), float(p[4]), float(p[5])))
    return resample(np.array(ts), np.array(v), t)


def load_her_model(repo, ckpt):
    spec = importlib.util.spec_from_file_location(
        "her_tcn", os.path.join(repo, "src", "model", "tcn_model.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ck = torch.load(ckpt, map_location="cpu", weights_only=False)
    sd = ck["model"]
    # The final checkpoint is narrower than the class defaults, and its config does not
    # record the geometry, so read it off the weights. Dilations double per block.
    stem = sd["stem.0.weight"].shape[0]
    chans, i = [], 0
    while f"blocks.{i}.conv1.weight" in sd:
        chans.append(sd[f"blocks.{i}.conv1.weight"].shape[0])
        i += 1
    net = mod.TCNModel(stem_width=stem, channels=tuple(chans),
                       dilations=tuple(2 ** k for k in range(len(chans))))
    net.load_state_dict(sd)
    net.eval()
    ns = ck["norm_stats"]
    return net, np.array(ns["mean"], dtype=np.float32), np.array(ns["std"], dtype=np.float32), \
        int(ck["config"]["window_samples"])


def our_norm_stats(dataset_pt):
    d = torch.load(dataset_pt, weights_only=False)
    X, split = d["windows"], d["split"].numpy()
    tr = split == 0
    return (X[tr].mean(dim=(0, 2)).numpy().astype(np.float32),
            X[tr].std(dim=(0, 2)).clamp_min(1e-6).numpy().astype(np.float32))


def slide(net, feats, window, mean=None, sd=None, dict_out=True):
    """One inference per 10 Hz sample, as the phone does; NaN until the window fills."""
    X = feats.astype(np.float32)
    if mean is not None:
        X = (X - mean) / sd
    idx = np.arange(window, len(X))
    out = np.full(len(X), np.nan)
    if len(idx) == 0:
        return out
    batch = np.stack([X[i - window:i] for i in idx]).transpose(0, 2, 1)   # (B, C, L)
    with torch.no_grad():
        mu = []
        for k in range(0, len(batch), 512):
            o = net(torch.from_numpy(np.ascontiguousarray(batch[k:k + 512])))
            mu.append((o["mu"] if dict_out else o[0]).numpy())
    out[idx] = np.concatenate(mu)
    return out


def score(pred, truth, mask):
    m = mask & np.isfinite(pred) & np.isfinite(truth)
    p, y = pred[m], truth[m]
    rmse = float(np.sqrt(np.mean((p - y) ** 2)))
    const = float(np.sqrt(np.mean((y - y.mean()) ** 2)))
    return {"rmse": rmse, "bias": float(np.mean(p - y)),
            "r": float(np.corrcoef(p, y)[0, 1]) if np.std(p) > 1e-9 else float("nan"),
            "vs_const": (1 - rmse / const) * 100, "n": int(m.sum())}


def drift(speed, heading_deg, g, drive, dur_s):
    """Median endpoint drift over outages, speed from the model, heading shared."""
    n = int(dur_s / DT)
    res = []
    for i0 in range(0, len(speed) - n, int(10 / DT)):
        i1 = i0 + n
        if not drive[i0:i1].all() or np.isnan(speed[i0:i1]).any():
            continue
        h = np.radians(heading_deg[i0:i1])
        e = np.cumsum(speed[i0:i1] * np.sin(h)) * DT
        nn = np.cumsum(speed[i0:i1] * np.cos(h)) * DT
        te = g["east"][i0:i1] - g["east"][i0]
        tn = g["north"][i0:i1] - g["north"][i0]
        dist = float(np.nansum(g["speed"][i0:i1]) * DT)
        if dist < 50:
            continue
        res.append(math.hypot(e[-1] - te[-1], nn[-1] - tn[-1]) / dist * 100)
    return float(np.median(res)) if res else float("nan")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--her-repo", required=True)
    ap.add_argument("--her-ckpt", default="results/run_tcn/best_ma3.pt",
                    help="her final tcn_physics_3s, relative to --her-repo")
    ap.add_argument("--ours", default="ml_model/model_tcn_base_earth_s0.pth")
    ap.add_argument("--our-dataset", default="ml_model/dataset_earth.pt")
    args = ap.parse_args(argv)

    s = load_session(args.session)
    g = build_grid(s)
    t, drive, truth = g["t"], g["drive"], g["speed"]
    acc, gyr = g["acc"], g["gyro"]
    grav = load_gravity(args.session, t)

    # Stand-still gyro bias in device axes (DeadReckoner's own gates).
    still = (np.abs(np.linalg.norm(acc, axis=1) - 9.80665) < 0.15) & \
            (np.linalg.norm(gyr, axis=1) < 0.05)
    gyr_db = gyr - (np.nanmean(gyr[still], axis=0) if still.sum() > 50 else 0.0)

    # ---- ours: earth frame, as IMUModelRunner builds it --------------------------------
    R = g["R"]
    lin = acc - grav
    acc_earth = np.einsum("nij,nj->ni", R, lin)
    feats_ours = np.concatenate([acc_earth, gyr_db], axis=1)

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "ml_model"))
    from tcn_model import TCNModel
    ours = TCNModel()
    ours.load_state_dict(torch.load(args.ours, map_location="cpu", weights_only=True))
    ours.eval()
    m_ours, s_ours = our_norm_stats(args.our_dataset)

    # ---- hers: tilt-levelled frame, per SIH_2026 data.windows.levelled_channels ---------
    down = np.nanmedian(grav[drive], axis=0)
    down /= np.linalg.norm(down)
    ref = np.array([1.0, 0.0, 0.0]) if abs(down[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = ref - (ref @ down) * down
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(down, e1)
    R_lvl = np.column_stack([e1, e2, down])
    her_net, her_m, her_s, her_win = load_her_model(args.her_repo,
                                                     os.path.join(args.her_repo, args.her_ckpt))
    # IO-VNBD file order == device[[0,2,1]] (the permutation is its own inverse).
    feats_her_trained = np.concatenate([lin @ R_lvl, gyr[:, [0, 2, 1]] @ R_lvl], axis=1)
    feats_her_correct = np.concatenate([lin @ R_lvl, gyr @ R_lvl], axis=1)

    preds = {
        "ours, standardised (as trained)": slide(ours, feats_ours, OUR_WINDOW, m_ours, s_ours),
        "ours, raw (what the app feeds)": slide(ours, feats_ours, OUR_WINDOW),
        "hers, file-order gyro (as trained)": slide(her_net, feats_her_trained, her_win,
                                                    her_m, her_s),
        "hers, device-order gyro": slide(her_net, feats_her_correct, her_win, her_m, her_s),
    }

    # Shared heading: the calibrated compass, so drift differences are speed alone.
    from .compass_heading_eval import (CAL_MAX_YAW_RATE, CAL_MIN_SPEED, CAL_SECONDS,
                                       circular_mean_deg, forward_axis_azimuth, wrap180)
    az, _ = forward_axis_azimuth(R, np.nanmedian(acc[drive], axis=0))
    b = g["bearing"]
    brate = np.abs(np.gradient(np.unwrap(np.radians(np.nan_to_num(b))), t))
    ok = drive & np.isfinite(b) & (np.nan_to_num(truth) > CAL_MIN_SPEED) & (brate < CAL_MAX_YAW_RATE)
    cal = np.where(ok)[0][:int(CAL_SECONDS / DT)]
    heading = (az + circular_mean_deg(wrap180(b[cal] - az[cal]))) % 360.0

    print(f"session {os.path.basename(os.path.normpath(args.session))}: "
          f"{drive.sum() * DT:.0f} s of driving, mean GNSS speed "
          f"{np.nanmean(truth[drive]):.2f} m/s\n")
    print(f"{'model / input':38s}{'RMSE':>7}{'bias':>8}{'r':>7}{'vs const':>10}"
          f"{'30 s':>8}{'60 s':>8}")
    rows = list(preds.items()) + [("GNSS truth speed (reference)", np.nan_to_num(truth))]
    for name, p in rows:
        sc = score(p, truth, drive)
        d30 = drift(p, heading, g, drive, 30)
        d60 = drift(p, heading, g, drive, 60)
        print(f"{name:38s}{sc['rmse']:7.2f}{sc['bias']:+8.2f}{sc['r']:+7.2f}"
              f"{sc['vs_const']:+9.0f}%{d30:7.1f}%{d60:7.1f}%")
    print("\n(RMSE/bias in m/s over the driving span; drift uses the calibrated compass "
          "heading for every row)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
