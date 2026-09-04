"""Score a phone-recorded session against its own GNSS, and replay it through a checkpoint.

Everything else in `eval/` measures the model on IO-VNBD, a UK corpus recorded in 2019 on
somebody else's phone in somebody else's car. This measures it on *our* device, on the road
the app will actually be used on, which is the only place several failure modes are visible
at all: a checkpoint trained in the vehicle frame cannot be caught by an IO-VNBD score,
because the dataset builder hands it vehicle-frame features on both sides of the split.

Three questions, in the order they matter:

  1. What did the app actually do?          — replay `ml.csv` / `deadreckon.csv` against `gps.csv`
  2. What would a given checkpoint do?      — re-infer from raw `imu.csv`, both framings
  3. Where does the position error come from — speed, heading, or the map?

The session is resampled to 10 Hz because that is the rate the app infers at and the rate
IO-VNBD was recorded at. Accelerometer and gyroscope arrive at 200 Hz, so this is decimation
of an already-antialiased signal rather than interpolation of a sparse one.

Run:  python -m eval.session_eval extracted_sessions/20260904_195146
      python -m eval.session_eval <dir> --model ml_model/model_data_cl_d3rot.pth --frame both
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

DT = 0.1                      # 10 Hz, the app's inference rate
WINDOW = 100                  # samples per inference window, matches IMUModelRunner
M_PER_DEG_LAT = 111_132.0
GRAVITY = 9.80665

# GNSS fixes worse than this are not truth. The recorded session reaches 277 m accuracy
# at its worst, and scoring a model against those would measure the receiver, not the model.
MAX_FIX_ACCURACY_M = 30.0

# Below this the vehicle is stopped and heading is meaningless, so bearing-derived
# quantities are dropped rather than fed noise.
MIN_SPEED_FOR_BEARING = 2.0


# ------------------------------------------------------------------------ loading

def _read_csv(path):
    """Minimal CSV reader returning a dict of column -> object array.

    pandas is available, but this keeps the module importable in the same bare
    environment the rest of `eval/` runs in, and the files are small.
    """
    with open(path, "r", encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
        cols = [[] for _ in header]
        for line in fh:
            parts = line.rstrip("\n").split(",")
            if len(parts) < len(header):
                parts += [""] * (len(header) - len(parts))
            for i in range(len(header)):
                cols[i].append(parts[i])
    return {h: np.array(c, dtype=object) for h, c in zip(header, cols)}


def _floats(col):
    out = np.full(len(col), np.nan)
    for i, v in enumerate(col):
        if v != "" and v is not None:
            try:
                out[i] = float(v)
            except ValueError:
                pass
    return out


def load_imu(path):
    """Pivot the long-format imu.csv into per-sensor arrays.

    The file is one row per sensor event, 500k rows for a 6-minute drive, so this is the
    one place in the module where the parsing has to be a little careful about cost.
    """
    want = {"accel", "gyro", "rv", "game_rv", "mag"}
    t = {k: [] for k in want}
    v = {k: [] for k in want}
    with open(path, "r", encoding="utf-8") as fh:
        fh.readline()
        for line in fh:
            p = line.split(",")
            s = p[1]
            if s not in want:
                continue
            t[s].append(float(p[0]))
            v[s].append((p[3], p[4], p[5], p[6]))
    out = {}
    for k in want:
        if not t[k]:
            continue
        arr = np.array(v[k], dtype=object)
        vals = np.full((len(arr), 4), np.nan)
        for j in range(4):
            vals[:, j] = _floats(arr[:, j])
        out[k] = (np.array(t[k]) / 1e9, vals)
    return out


def load_session(d):
    """Read every channel of a device session directory."""
    s = {"dir": d, "name": os.path.basename(os.path.normpath(d))}
    for f in ("gps", "ml", "deadreckon", "mapmatch", "gnss_status"):
        p = os.path.join(d, f + ".csv")
        if os.path.exists(p):
            raw = _read_csv(p)
            s[f] = {k: (_floats(v) if k != "provider" and k != "road_class" else v)
                    for k, v in raw.items()}
    meta = os.path.join(d, "session.json")
    if os.path.exists(meta):
        with open(meta, "r", encoding="utf-8") as fh:
            s["meta"] = json.load(fh)
    s["imu"] = load_imu(os.path.join(d, "imu.csv"))
    return s


# ------------------------------------------------------------------------ geometry

def quat_to_matrix(q):
    """(N,4) rotation vector xyzw -> (N,3,3) device-to-world, Android's convention.

    Android's rotation vector stores (x, y, z) with w either supplied as the 4th element
    or implied by unit norm; older devices omit it. Both are handled.
    """
    x, y, z = q[:, 0], q[:, 1], q[:, 2]
    w = q[:, 3].copy()
    bad = ~np.isfinite(w)
    if bad.any():
        w[bad] = np.sqrt(np.clip(1.0 - (x[bad] ** 2 + y[bad] ** 2 + z[bad] ** 2), 0.0, 1.0))
    n = np.sqrt(x * x + y * y + z * z + w * w)
    n[n < 1e-12] = 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    R = np.empty((len(q), 3, 3))
    R[:, 0, 0] = 1 - 2 * (y * y + z * z)
    R[:, 0, 1] = 2 * (x * y - z * w)
    R[:, 0, 2] = 2 * (x * z + y * w)
    R[:, 1, 0] = 2 * (x * y + z * w)
    R[:, 1, 1] = 1 - 2 * (x * x + z * z)
    R[:, 1, 2] = 2 * (y * z - x * w)
    R[:, 2, 0] = 2 * (x * z - y * w)
    R[:, 2, 1] = 2 * (y * z + x * w)
    R[:, 2, 2] = 1 - 2 * (x * x + y * y)
    return R


def resample(t_src, v_src, t_dst):
    """Linear resample of each column onto a common clock."""
    v_src = np.atleast_2d(v_src.T).T
    out = np.empty((len(t_dst), v_src.shape[1]))
    for j in range(v_src.shape[1]):
        col = v_src[:, j]
        ok = np.isfinite(col)
        out[:, j] = np.interp(t_dst, t_src[ok], col[ok]) if ok.sum() > 1 else np.nan
    return out


def enu_from_gps(lat, lon, lat0, lon0):
    mlon = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return (lon - lon0) * mlon, (lat - lat0) * M_PER_DEG_LAT


def build_grid(sess):
    """Resample the session onto a uniform 10 Hz grid with GNSS truth attached."""
    imu = sess["imu"]
    t_a, a = imu["accel"]
    t_g, g = imu["gyro"]
    t0 = max(t_a[0], t_g[0])
    t1 = min(t_a[-1], t_g[-1])
    t = np.arange(t0, t1, DT)

    acc = resample(t_a, a[:, :3], t)
    gyr = resample(t_g, g[:, :3], t)

    rv_key = "rv" if "rv" in imu else ("game_rv" if "game_rv" in imu else None)
    R = None
    if rv_key:
        t_r, rv = imu[rv_key]
        q = resample(t_r, rv[:, :4], t)
        R = quat_to_matrix(q)

    gps = sess["gps"]
    tg = gps["t_ns"] / 1e9
    fine = gps["acc_m"] <= MAX_FIX_ACCURACY_M
    sp = gps["speed_mps"]
    ok = fine & np.isfinite(sp)
    speed = np.interp(t, tg[ok], sp[ok]) if ok.sum() > 2 else np.full(len(t), np.nan)
    # Mark samples too far from any usable fix, so gaps are not silently interpolated across.
    gap = np.full(len(t), np.inf)
    if ok.sum():
        idx = np.searchsorted(tg[ok], t).clip(1, ok.sum() - 1)
        gap = np.minimum(np.abs(t - tg[ok][idx]), np.abs(t - tg[ok][idx - 1]))
    speed[gap > 3.0] = np.nan

    brg = gps["bearing_deg"]
    okb = fine & np.isfinite(brg) & np.isfinite(sp) & (sp >= MIN_SPEED_FOR_BEARING)
    bearing = np.full(len(t), np.nan)
    if okb.sum() > 2:
        un = np.unwrap(np.radians(brg[okb]))
        bearing = np.degrees(np.interp(t, tg[okb], un)) % 360.0
        bearing[gap > 3.0] = np.nan

    # The drive is only part of the recording. This session is parked from 200 s on, with the
    # phone still logging while it was handled, and scoring across that measures the pocket
    # rather than the vehicle. The driving span is first-to-last fix above walking pace.
    moving = ok & (sp > MIN_SPEED_FOR_BEARING)
    if moving.sum() > 2:
        drive = (t >= tg[moving][0]) & (t <= tg[moving][-1])
    else:
        drive = np.ones(len(t), dtype=bool)

    lat0, lon0 = float(gps["lat"][0]), float(gps["lon"][0])
    ge, gn = enu_from_gps(gps["lat"], gps["lon"], lat0, lon0)
    east = np.interp(t, tg[fine], ge[fine])
    north = np.interp(t, tg[fine], gn[fine])

    return {"t": t, "acc": acc, "gyro": gyr, "R": R, "speed": speed,
            "bearing": bearing, "east": east, "north": north,
            "lat0": lat0, "lon0": lon0, "gap": gap, "drive": drive}


def earth_frame(acc, R):
    """Device acceleration rotated into world axes, gravity removed from the vertical."""
    if R is None:
        return acc.copy()
    out = np.einsum("nij,nj->ni", R, acc)
    out[:, 2] -= GRAVITY
    return out


def vehicle_frame(acc, gyr, speed, t):
    """(forward, right, up) features for a phone session, with no CAN bus to fit against.

    `eval.iovnbd.device_to_vehicle` fits the forward axis against the vehicle's own reported
    longitudinal acceleration. A phone session has no such channel, so the only available
    target is the derivative of GNSS speed — which that function's docstring explicitly
    records as the *worse* regressor it moved away from. The fit quality is returned so a
    caller can see how much of the resulting score is the frame estimate rather than the
    model; treat a low `fwd_r` as "this comparison is not conclusive".
    """
    good = np.all(np.isfinite(acc), axis=1)
    up = acc[good].mean(axis=0)
    up = up / np.linalg.norm(up)

    a_h = acc - np.outer(acc @ up, up)
    long_acc = np.gradient(speed, t)
    m = good & np.isfinite(long_acc) & np.isfinite(speed)
    # Smooth: 1 Hz GNSS differentiated at 10 Hz is a staircase, and its derivative is
    # dominated by the interpolation steps rather than by the vehicle.
    k = np.ones(21) / 21.0
    la = np.convolve(np.nan_to_num(long_acc), k, mode="same")

    fwd, *_ = np.linalg.lstsq(a_h[m], la[m], rcond=None)
    fwd = fwd - up * float(fwd @ up)
    n = np.linalg.norm(fwd)
    if n < 1e-9:
        return acc.copy(), gyr.copy(), 0.0
    fwd = fwd / n
    pred = a_h[m] @ fwd
    fwd_r = float(np.corrcoef(pred, la[m])[0, 1]) if np.std(pred) > 0 else 0.0

    right = np.cross(up, fwd)
    right /= np.linalg.norm(right)
    R_dv = np.vstack([fwd, right, up])
    return acc @ R_dv.T, gyr @ R_dv.T, fwd_r


# ------------------------------------------------------------------------ scoring

def score(pred, truth, label, mask=None):
    m = np.isfinite(pred) & np.isfinite(truth)
    if mask is not None:
        m &= mask
    if m.sum() < 10:
        return {"label": label, "n": int(m.sum())}
    p, y = pred[m], truth[m]
    const = float(np.sqrt(np.mean((y - y.mean()) ** 2)))
    return {
        "label": label, "n": int(m.sum()),
        "rmse": float(np.sqrt(np.mean((p - y) ** 2))),
        "bias": float(np.mean(p - y)),
        "r": float(np.corrcoef(p, y)[0, 1]) if np.std(p) > 1e-9 else float("nan"),
        "const_rmse": const,
        "shrink": float(np.std(p) / np.std(y)) if np.std(y) > 1e-9 else float("nan"),
        "pred_range": (float(p.min()), float(p.max())),
        "truth_range": (float(y.min()), float(y.max())),
    }


def show(s):
    if s.get("n", 0) < 10:
        print(f"  {s['label']:26s}  too few overlapping samples ({s.get('n', 0)})")
        return
    beat = (1 - s["rmse"] / s["const_rmse"]) * 100 if s["const_rmse"] > 0 else float("nan")
    print(f"  {s['label']:26s} n={s['n']:5d}  RMSE {s['rmse']:6.3f}  "
          f"bias {s['bias']:+6.3f}  r {s['r']:+.3f}  shrink {s['shrink']:.3f}")
    print(f"  {'':26s} constant baseline {s['const_rmse']:6.3f}  "
          f"-> {beat:+.1f}% vs constant   "
          f"pred [{s['pred_range'][0]:.2f},{s['pred_range'][1]:.2f}] "
          f"truth [{s['truth_range'][0]:.2f},{s['truth_range'][1]:.2f}]")


# ------------------------------------------------------------------------ inference

def load_model(path):
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ml_model"))
    import torch
    from resnet1d import ResNet1D
    from .rank_models import infer_shape

    state = torch.load(path, map_location="cpu", weights_only=True)
    if "model" in state and isinstance(state["model"], dict):
        state = state["model"]
    widths, blocks = infer_shape(state)
    kw = {}
    if widths:
        kw["widths"] = widths
    if blocks:
        kw["blocks"] = blocks
    try:
        net = ResNet1D(**kw)
        net.load_state_dict(state)
    except TypeError:
        net = ResNet1D()
        net.load_state_dict(state)
    net.eval()
    return net


def infer(net, acc, gyr, stride=1):
    """Slide the model over the session exactly as the phone does, one window per sample."""
    import torch

    X = np.concatenate([acc, gyr], axis=1)
    n = len(X)
    idx = list(range(WINDOW, n, stride))
    mu = np.full(n, np.nan)
    lv = np.full(n, np.nan)
    st = np.full(n, np.nan)
    yr = np.full(n, np.nan)
    if not idx:
        return mu, lv, st, yr
    batch = np.stack([X[i - WINDOW:i] for i in idx])           # (B, L, C)
    with torch.no_grad():
        out = net(torch.from_numpy(batch.transpose(0, 2, 1)).float())
    if isinstance(out, dict):
        m, l = out["mu"], out["logvar"]
        s, y = out["stationary_logit"], out["yaw_rate"]
    else:
        m, l, s, y = out
    for k, i in enumerate(idx):
        mu[i] = float(m[k]); lv[i] = float(l[k])
        st[i] = float(s[k]); yr[i] = float(y[k])
    return mu, lv, st, yr


# ------------------------------------------------------------------------ report

def report_app(sess, grid):
    """What the app itself produced, scored against its own GNSS."""
    print("\n=== 1. what the app did (from its own logs) ===")
    t = grid["t"]

    if "ml" in sess:
        ml = sess["ml"]
        tm = ml["t_ns"] / 1e9
        for ch, name in (("mu", "app mu -> speed"), ("yaw_rate", "app yaw_rate")):
            v = np.interp(t, tm, ml[ch])
            v[(t < tm[0]) | (t > tm[-1])] = np.nan
            if ch == "mu":
                show(score(v, grid["speed"], "shipped model " + name, grid["drive"]))
            else:
                gz = np.abs(grid["gyro"][:, 2])
                show(score(v, gz, "shipped model yaw|gyro|", grid["drive"]))
        neg = np.mean(sess["ml"]["yaw_rate"] < 0)
        print(f"  yaw_rate sign: {neg * 100:.1f}% of samples negative "
              f"(a real yaw rate is near 50%)")

    if "deadreckon" in sess:
        dr = sess["deadreckon"]
        td = dr["t_ns"] / 1e9
        v = np.interp(t, td, dr["speed_mps"])
        v[(t < td[0]) | (t > td[-1])] = np.nan
        show(score(v, grid["speed"], "integrator speed", grid["drive"]))
        show(score(v, grid["speed"], "integrator speed (parked)", ~grid["drive"]))
        fr = np.interp(t, td, dr["free_run"]) > 0.5
        print(f"  free-run samples: {int(fr.sum())} of {len(fr)} "
              f"({fr.sum() * DT:.0f} s simulated outage)")


def report_map(sess):
    print("\n=== 3. map matching ===")
    if "mapmatch" not in sess:
        print("  no mapmatch.csv")
        return
    mm = sess["mapmatch"]
    c = mm["correction_m"]
    c = c[np.isfinite(c)]
    conf = mm["confidence"][np.isfinite(mm["confidence"])]
    print(f"  matches {len(c)}   correction: mean {c.mean():.1f} m  "
          f"median {np.median(c):.1f} m  p90 {np.percentile(c, 90):.1f} m  max {c.max():.1f} m")
    print(f"  confidence: mean {conf.mean():.3f}  "
          f"below feedback threshold 0.6: {np.mean(conf < 0.6) * 100:.0f}%")

    # Consecutive snaps that jump much further than the vehicle travelled are the
    # symptom in the screenshots: the matched point crossing to a different road.
    lat, lon = mm["snap_lat"], mm["snap_lon"]
    t = mm["t_ns"] / 1e9
    mlon = M_PER_DEG_LAT * math.cos(math.radians(np.nanmean(lat)))
    dx = np.diff(lon) * mlon
    dy = np.diff(lat) * M_PER_DEG_LAT
    hop = np.hypot(dx, dy)
    dlat, dlon = mm["dr_lat"], mm["dr_lon"]
    ddx = np.diff(dlon) * mlon
    ddy = np.diff(dlat) * M_PER_DEG_LAT
    dhop = np.hypot(ddx, ddy)
    excess = hop - dhop
    dt = np.diff(t)
    bad = (excess > 15.0) & (dt < 5.0)
    print(f"  snap jumps >15 m beyond the integrator's own step: {int(bad.sum())} "
          f"of {len(bad)} ({bad.sum() / max(len(bad), 1) * 100:.0f}%)")
    if bad.any():
        print(f"    worst {excess[bad].max():.0f} m of unexplained lateral movement")


def report_model(sess, grid, model_path, frames):
    print(f"\n=== 2. replaying raw IMU through {os.path.basename(model_path)} ===")
    net = load_model(model_path)
    acc_e = earth_frame(grid["acc"], grid["R"])
    for fr in frames:
        if fr == "earth":
            a, g, note = acc_e, grid["gyro"], "as the app feeds it"
        else:
            a, g, r = vehicle_frame(grid["acc"], grid["gyro"], grid["speed"], grid["t"])
            note = f"as it was trained (forward-axis fit r={r:.2f})"
        mu, lv, st, yr = infer(net, a, g)
        print(f"\n  -- {fr} frame, {note}")
        show(score(mu, grid["speed"], f"mu -> speed [{fr}]", grid["drive"]))
        show(score(yr, g[:, 2], f"yaw_rate -> gyro z [{fr}]", grid["drive"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--model", help="checkpoint to replay the raw IMU through")
    ap.add_argument("--frame", choices=("earth", "vehicle", "both"), default="both")
    args = ap.parse_args(argv)

    sess = load_session(args.session)
    grid = build_grid(sess)
    m = sess.get("meta", {}).get("summary", {})
    print(f"session {sess['name']}   {len(grid['t']) * DT:.0f} s at 10 Hz   "
          f"{m.get('gps_fixes', '?')} fixes   {m.get('imu_samples', '?')} IMU samples")
    v = grid["speed"][np.isfinite(grid["speed"])]
    print(f"GNSS speed (fixes better than {MAX_FIX_ACCURACY_M:.0f} m): "
          f"mean {v.mean():.2f}  max {v.max():.2f} m/s over {len(v) * DT:.0f} s")
    dv = grid["drive"]
    print(f"driving span: {dv.sum() * DT:.0f} s of {len(dv) * DT:.0f} s recorded; "
          f"scores below are over the driving span unless marked otherwise")

    report_app(sess, grid)
    if args.model:
        frames = ("earth", "vehicle") if args.frame == "both" else (args.frame,)
        report_model(sess, grid, args.model, frames)
    report_map(sess)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
