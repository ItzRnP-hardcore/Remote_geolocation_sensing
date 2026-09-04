"""Rank trained TCN checkpoints by free-running drift, not by test RMSE.

Test RMSE is the training objective, not the deliverable. What the project needs is
position after a GNSS outage, and the two come apart: a model with slightly worse RMSE
but less per-session bias navigates better, because a bias integrates into displacement
while zero-mean scatter partly cancels. Every checkpoint is therefore scored the way it
would actually be used.

Each model is anchored once from GNSS and then fed nothing but phone IMU. Heading comes
from the raw gyro with its bias removed from pre-outage data, never from the model's yaw
head - measured, that head is worse than the gyro it was trained from (r 0.82 against
0.94-0.996) and carries enough bias to swing heading by 900-1400 deg/hr. Speed comes
from the model, optionally with an offset calibrated on the GNSS available before the
outage.

Run:  python -m eval.rank_models --glob "ml_model/model_data_*.pth"
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import math
import os
import sys

import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml_model"))
from tcn_model import TCNModel  # noqa: E402
from export_model import infer_geometry  # noqa: E402

from .model_dr_eval import (DT, load_runs, predict, truth_en, windows_of,
                            yaw_sign_convention)

CAL_S = 120.0          # seconds of pre-outage GNSS used for bias and calibration
DURATIONS = (30, 60, 120, 300)




def drift_for(model, norm_mean, norm_sd, runs, calibrate=True):
    agg = {}
    for run in runs:
        sign = yaw_sign_convention(run)
        sp, yw = predict(model, run["feat"], norm_mean, norm_sd)
        if sp is None:
            continue
        gyro = run["gyro_up"]
        yt = run["yaw_rate"]
        truth = run["speed"]
        te, tn = truth_en(run)
        W = int(CAL_S / DT)

        for dur in DURATIONS:
            for w in windows_of(len(sp), dur):
                pre = slice(max(0, w.start - W), w.start)
                if pre.stop - pre.start < 300:
                    continue
                ok = np.isfinite(sp[pre]) & np.isfinite(truth[pre])
                if ok.sum() < 300:
                    continue
                gb = np.nanmean(gyro[pre] - yt[pre])
                off = np.mean(truth[pre][ok] - sp[pre][ok]) if calibrate else 0.0

                s_ = sp[w] + off
                y_ = gyro[w] - gb
                h = np.radians(run["heading"][w][0]) + np.cumsum(sign * y_) * DT
                e = np.cumsum(s_ * np.sin(h)) * DT
                n = np.cumsum(s_ * np.cos(h)) * DT
                a = te[w] - te[w][0]
                b = tn[w] - tn[w][0]
                dist = float(np.sum(np.abs(truth[w])) * DT)
                if dist <= 100:
                    continue
                agg.setdefault(dur, []).append(
                    math.hypot(e[-1] - a[-1], n[-1] - b[-1]) / dist * 100)
    return {d: float(np.median(v)) for d, v in agg.items() if v}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glob", default="ml_model/model_data*.pth")
    ap.add_argument("--npz", default=os.path.join("dataset", "iovnbd_train_wide.npz"))
    ap.add_argument("--runs", default="S2_r1,S3c", help="held-out runs to score on")
    ap.add_argument("--no-calibrate", action="store_true")
    args = ap.parse_args(argv)

    wanted = {x.strip() for x in args.runs.split(",") if x.strip()}
    d = np.load(args.npz, allow_pickle=True)
    names = [str(x) for x in d["run_names"]]
    starts, lens = d["run_starts"], d["run_lengths"]
    runs = [r for r in load_runs(args.npz) if r["name"] in wanted]
    for r in runs:
        i = names.index(r["name"])
        sl = slice(int(starts[i]), int(starts[i]) + int(lens[i]))
        r["gyro_up"] = d["vehicle_gyro"][sl][:, 2]
    if not runs:
        print(f"no runs matched {sorted(wanted)}")
        return 1

    paths = sorted(globlib.glob(args.glob))
    if not paths:
        print(f"no checkpoints matched {args.glob}")
        return 1

    print(f"scoring {len(paths)} checkpoints on {[r['name'] for r in runs]}, "
          f"heading from debiased gyro, "
          f"speed {'offset-calibrated' if not args.no_calibrate else 'raw'}\n")
    head = (f"{'checkpoint':<26}{'params':>10}{'test RMSE':>11}"
            + "".join(f"{str(x) + 's':>8}" for x in DURATIONS))
    print(head)
    print("-" * len(head))

    rows = []
    for p in paths:
        tag = os.path.basename(p)[len("model_data"):-len(".pth")].lstrip("_") or "base"
        rf = os.path.join("ml_model", f"train_iovnbd_results_{tag}.json")
        if not os.path.exists(rf):
            rf = os.path.join("ml_model", "train_iovnbd_results.json")
        if not os.path.exists(rf):
            print(f"{tag:<26}  (no results file, cannot recover normalisation)")
            continue
        res = json.load(open(rf, encoding="utf-8"))
        nm = np.asarray(res["norm_mean"], dtype=float)
        nsd = np.asarray(res["norm_sd"], dtype=float)
        rmse = next((r["test"]["speed_rmse"] for r in res["results"]), float("nan"))

        state = torch.load(p, weights_only=True)
        geom = infer_geometry(state)
        if geom is None:
            print(f"{tag:<26}  (not a TCN checkpoint, skipped)")
            continue
        stem, channels, dilations = geom
        model = TCNModel(stem_width=stem, channels=channels, dilations=dilations)
        model.load_state_dict(state)
        n_par = sum(q.numel() for q in model.parameters())

        dr = drift_for(model, nm, nsd, runs, calibrate=not args.no_calibrate)
        rows.append((tag, n_par, rmse, dr))
        print(f"{tag:<26}{n_par:>10,}{rmse:>11.3f}"
              + "".join(f"{dr.get(x, float('nan')):>8.2f}" for x in DURATIONS))

    if rows:
        best = min(rows, key=lambda r: r[3].get(60, float("inf")))
        print(f"\nbest at 60 s: {best[0]} ({best[1]:,} params, "
              f"{best[3].get(60, float('nan')):.2f}% drift, test RMSE {best[2]:.3f})")
        # Worth stating plainly: these two orderings are not the same question.
        by_rmse = min(rows, key=lambda r: r[2])
        if by_rmse[0] != best[0]:
            print(f"note: lowest test RMSE is {by_rmse[0]} ({by_rmse[2]:.3f}), which is "
                  f"NOT the best navigator - RMSE and drift disagree here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
