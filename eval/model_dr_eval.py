"""Free-run the model as a dead reckoner and measure where the error actually comes from.

The question this answers is the deployment question, not a regression score: give the
model a starting position and speed from GNSS, then feed it nothing but phone IMU and
let it navigate. How far has it drifted by the end, as a fraction of the distance
actually driven?

Position comes from two model outputs and nothing else:

    heading(t)  = heading(0) + integral of the yaw-rate head
    speed(t)    = the mu head
    position(t) = position(0) + integral of speed * (sin heading, cos heading)

Reporting one number for that would hide the thing worth knowing, so the same run is
integrated four ways. Each swaps one channel for ground truth, which turns a single
error figure into an attribution:

    truth + truth    both channels true. Must come out near zero - this is the check
                     that the integrator itself is right, and if it does not hold
                     nothing else in the table means anything.
    model + truth    model speed, true heading. Isolates the speed channel.
    truth + model    true speed, model heading. Isolates the yaw channel.
    model + model    the real thing.

Drift is reported against distance travelled rather than against time, because a
percentage is what transfers between a 60 s tunnel and a 10 minute one, and because
absolute metres flatter whichever run happened to be slowest.

Run:  python -m eval.model_dr_eval --model ml_model/model_data.pth
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml_model"))
from resnet1d import ResNet1D  # noqa: E402

DT = 0.1
WINDOW = 100
M_PER_DEG_LAT = 111_132.0


def load_runs(npz_path: str):
    d = np.load(npz_path, allow_pickle=True)
    starts, lengths = d["run_starts"], d["run_lengths"]
    names = [str(x) for x in d["run_names"]]
    runs = []
    for i, (s0, n) in enumerate(zip(starts, lengths)):
        sl = slice(int(s0), int(s0) + int(n))
        runs.append({
            "name": names[i],
            "feat": np.concatenate([d["vehicle_accel"][sl], d["vehicle_gyro"][sl]], axis=1),
            "speed": d["truth_speed"][sl],
            "heading": d["truth_heading"][sl],
            "yaw_rate": d["truth_yaw_rate"][sl],
            "lat": d["truth_lat"][sl],
            "lon": d["truth_lon"][sl],
        })
    return runs


def yaw_sign_convention(run) -> float:
    """Whether integrating +yaw_rate or -yaw_rate reproduces the reference heading.

    Determined from the data rather than assumed: the reference heading is a compass
    bearing and the yaw rate is a right-handed rotation, and which way that maps is a
    property of the recording, not something to guess. Getting it backwards mirrors
    every turn and still produces a plausible-looking track.
    """
    h = np.radians(run["heading"])
    dh = np.unwrap(h)
    dh = np.gradient(dh) / DT
    w = run["yaw_rate"]
    m = np.isfinite(dh) & np.isfinite(w) & (np.abs(dh) < 1.0)
    if m.sum() < 100:
        return -1.0
    return 1.0 if np.corrcoef(dh[m], w[m])[0, 1] > 0 else -1.0


def predict(model, feat, norm_mean, norm_sd, stride: int = 10):
    """Model speed and yaw rate on a sliding window, held between updates.

    Stride 10 gives a fresh prediction every second, matching the per-second
    convention the outage metrics use. The window needs WINDOW samples of history, so
    the first WINDOW-1 samples cannot be predicted at all - they are filled from the
    first available prediction, which is what a real device would do on cold start.
    """
    n = len(feat)
    x = (feat - norm_mean) / norm_sd
    idx = list(range(WINDOW, n + 1, stride))
    if not idx:
        return None, None
    batch = np.stack([x[i - WINDOW:i].T for i in idx]).astype(np.float32)

    mus, yaws = [], []
    model.eval()
    with torch.no_grad():
        for i in range(0, len(batch), 256):
            o = model(torch.from_numpy(batch[i:i + 256]))
            mus.append(o["mu"].numpy())
            yaws.append(o["yaw_rate"].numpy())
    mu = np.concatenate(mus)
    yaw = np.concatenate(yaws)

    speed = np.empty(n)
    yawr = np.empty(n)
    ends = np.array(idx) - 1
    speed[:] = mu[0]
    yawr[:] = yaw[0]
    for k, e in enumerate(ends):
        nxt = ends[k + 1] if k + 1 < len(ends) else n
        speed[e:nxt] = mu[k]
        yawr[e:nxt] = yaw[k]
    return speed, yawr


def integrate(speed, yaw_rate, h0_deg, sign):
    """Dead reckon east/north offsets in metres from a starting heading."""
    heading = np.radians(h0_deg) + np.cumsum(sign * yaw_rate) * DT
    east = np.cumsum(speed * np.sin(heading)) * DT
    north = np.cumsum(speed * np.cos(heading)) * DT
    return east, north, np.degrees(heading)


def truth_en(run):
    lat0, lon0 = run["lat"][0], run["lon"][0]
    ml = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return (run["lon"] - lon0) * ml, (run["lat"] - lat0) * M_PER_DEG_LAT


def score(run, model_speed, model_yaw, sign, a, b, seg=None):
    """Drift for one channel combination over an index range."""
    sl = slice(0, len(run["speed"])) if seg is None else seg
    sp = (run["speed"] if a == "truth" else model_speed)[sl]
    te, tn = truth_en(run)
    te, tn = te[sl], tn[sl]
    h0 = run["heading"][sl][0]
    if b == "heading":
        # The reference heading used directly, integrating no rate at all. This is the
        # only combination that isolates the position integrator, and it is the row
        # that has to be near zero before any other row means anything.
        hd = run["heading"][sl]
        h = np.radians(hd)
        e = np.cumsum(sp * np.sin(h)) * DT
        n = np.cumsum(sp * np.cos(h)) * DT
    else:
        yw = (run["yaw_rate"] if b == "truth" else model_yaw)[sl]
        e, n, hd = integrate(sp, yw, h0, sign)
    # Truth offsets are absolute from run start; re-base both to the window start.
    te = te - te[0]
    tn = tn - tn[0]
    err = np.hypot(e - te, n - tn)
    dist = float(np.sum(np.abs(run["speed"][sl])) * DT)
    return {
        "final_error_m": float(err[-1]),
        "max_error_m": float(err.max()),
        "distance_m": dist,
        "drift_pct": float(err[-1] / dist * 100) if dist > 1 else float("nan"),
        "heading_final_err_deg": float(((hd[-1] - run["heading"][sl][-1] + 180) % 360) - 180),
        "east": e, "north": n, "err": err, "heading": hd,
    }


def analyse_channels(run, model_speed, model_yaw, sign):
    """Where each channel goes wrong, in its own units."""
    sp_t, sp_m = run["speed"], model_speed
    yw_t, yw_m = run["yaw_rate"], model_yaw
    m = np.isfinite(sp_t) & np.isfinite(sp_m)
    my = np.isfinite(yw_t) & np.isfinite(yw_m)
    dh = np.degrees(np.mean(sign * yw_m[my] - sign * yw_t[my])) * 3600
    return {
        "speed_bias_ms": float(np.mean(sp_m[m] - sp_t[m])),
        "speed_rmse_ms": float(np.sqrt(np.mean((sp_m[m] - sp_t[m]) ** 2))),
        "speed_rel_bias_pct": float(np.mean(sp_m[m] - sp_t[m]) / np.mean(sp_t[m]) * 100),
        "speed_corr": float(np.corrcoef(sp_m[m], sp_t[m])[0, 1]),
        "yaw_bias_dps": float(np.degrees(np.mean(yw_m[my] - yw_t[my]))),
        "yaw_rmse_dps": float(np.degrees(np.sqrt(np.mean((yw_m[my] - yw_t[my]) ** 2)))),
        "yaw_corr": float(np.corrcoef(yw_m[my], yw_t[my])[0, 1]),
        "heading_drift_deg_per_hr": float(dh),
    }


# Drift is a ratio, so a window the vehicle barely moved through divides a real
# error by a near-zero distance and produces a meaningless percentage. An earlier
# version reported 10.2% mean against a 4.4% median for exactly this reason.
MIN_WINDOW_DISTANCE_M = 100.0


def windows_of(n, seconds, stride_s=60):
    step = int(stride_s / DT)
    span = int(seconds / DT)
    return [slice(i, i + span) for i in range(0, n - span, step)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--npz", default=os.path.join("dataset", "iovnbd_train.npz"))
    ap.add_argument("--model", default=os.path.join("ml_model", "model_data.pth"))
    ap.add_argument("--results", default=os.path.join("ml_model", "train_iovnbd_results.json"))
    ap.add_argument("--dataset", default=os.path.join("ml_model", "dataset_iovnbd.pt"))
    ap.add_argument("--runs", default="", help="comma-separated run names (default: test runs)")
    ap.add_argument("--durations", default="30,60,120,300")
    args = ap.parse_args(argv)

    # Normalisation must be the training set's, not this run's, or the model sees
    # inputs on a scale it never met.
    with open(args.results, encoding="utf-8") as fh:
        res = json.load(fh)
    norm_mean = np.asarray(res["norm_mean"], dtype=float)
    norm_sd = np.asarray(res["norm_sd"], dtype=float)

    d = torch.load(args.dataset, weights_only=False)
    names = d["run_names"]
    split = d["split"].numpy()
    run_ids = d["run_ids"].numpy()
    test_names = {names[i] for i in np.unique(run_ids[split == 2])}

    model = ResNet1D()
    model.load_state_dict(torch.load(args.model, weights_only=True))

    wanted = ({x.strip() for x in args.runs.split(",") if x.strip()}
              if args.runs else test_names)
    runs = [r for r in load_runs(args.npz) if r["name"] in wanted]
    if not runs:
        print(f"no runs matched {sorted(wanted)}")
        return 1
    print(f"model {os.path.basename(args.model)}   runs {[r['name'] for r in runs]} "
          f"({'held-out test' if not args.runs else 'explicit'})\n")

    durations = [int(x) for x in args.durations.split(",")]
    combos = [("truth", "heading"), ("truth", "truth"), ("model", "truth"),
              ("truth", "model"), ("model", "model")]
    labels = {("truth", "heading"): "truth speed + truth heading (integrator check)",
              ("truth", "truth"): "truth speed + integrated truth yaw",
              ("model", "truth"): "model speed + truth yaw  (speed error alone)",
              ("truth", "model"): "truth speed + model yaw  (yaw error alone)",
              ("model", "model"): "model speed + model yaw  (free-running)"}

    agg = {c: {t: [] for t in durations} for c in combos}
    for run in runs:
        sign = yaw_sign_convention(run)
        sp, yw = predict(model, run["feat"], norm_mean, norm_sd)
        if sp is None:
            continue
        ch = analyse_channels(run, sp, yw, sign)
        print(f"--- {run['name']}  ({len(sp)/10/60:.1f} min, "
              f"{np.nansum(run['speed'])*DT/1000:.1f} km, yaw sign {sign:+.0f}) ---")
        print(f"  speed : bias {ch['speed_bias_ms']:+.3f} m/s ({ch['speed_rel_bias_pct']:+.1f}%)"
              f"  RMSE {ch['speed_rmse_ms']:.3f}  r {ch['speed_corr']:+.3f}")
        print(f"  yaw   : bias {ch['yaw_bias_dps']:+.4f} deg/s  RMSE {ch['yaw_rmse_dps']:.3f}"
              f"  r {ch['yaw_corr']:+.3f}  -> heading drift "
              f"{ch['heading_drift_deg_per_hr']:+.0f} deg/hr")
        for t in durations:
            for c in combos:
                for sl in windows_of(len(sp), t):
                    s = score(run, sp, yw, sign, c[0], c[1], sl)
                    if (np.isfinite(s["drift_pct"])
                            and s["distance_m"] >= MIN_WINDOW_DISTANCE_M):
                        agg[c][t].append(s)
        print()

    print(f"{'combination':<44}{'dur':>5}{'n':>5}{'drift % med':>13}{'mean':>8}"
          f"{'p90':>8}{'err m':>9}{'dist m':>9}")
    print("-" * 101)
    for c in combos:
        for t in durations:
            rows = agg[c][t]
            if not rows:
                continue
            dp = np.array([r["drift_pct"] for r in rows])
            er = np.array([r["final_error_m"] for r in rows])
            di = np.array([r["distance_m"] for r in rows])
            print(f"{labels[c]:<44}{t:>5}{len(rows):>5}{np.median(dp):>13.2f}"
                  f"{dp.mean():>8.2f}{np.percentile(dp, 90):>8.2f}"
                  f"{np.median(er):>9.1f}{np.median(di):>9.0f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
