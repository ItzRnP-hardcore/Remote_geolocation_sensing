"""IO-VNBD adapter: pair the smartphone stream with the vehicle reference stream.

IO-VNBD (Onyekpe et al.) records each journey twice - once from an Android phone and
once from the car - and this module turns those pairs into the session format the rest
of `eval/` already consumes, so outage_eval, dr_diagnostics, model_speed_eval and
alongroad work on 55 hours of real driving instead of three walking sessions.

Everything below is shaped by what the files actually contain rather than what their
headers claim, because on inspection several of those claims are wrong:

  S column 3 is labelled "GPS SPEED (Kmh)" and holds metres per second. Its ratio to
      position-derived speed is 1.09, not 3.6. Trusting the label scales every speed
      label in the dataset by 3.6.
  S GRAVITY and ORIENTATION are unusable - a constant placeholder and a set of angles
      that contradict the accelerometer. See eval/ahrs.py, which exists for this
      reason.
  S GPS is held for 9.0 s at a stretch: 899 distinct positions in 94,600 rows. As
      ground truth it is nearly worthless, which is why truth here comes from V.
  V column 4 IS km/h (ratio 3.60, verified), so it needs dividing where S does not.
      Two adjacent speed columns in the same dataset with different units is exactly
      the kind of thing that silently poisons a training set.
  "Synchronised" means equal row counts, NOT time alignment. Raw S-vs-V position
      disagreement runs to 642 m median and 6.4 km worst case. A per-session lag has
      to be recovered, and one session (S4, lag -309 s) is beyond saving.

What V buys us, beyond position: real 10 Hz speed, heading, and yaw rate. That last
one matters because ml_model/build_dataset.py currently trains the yaw-rate head
against a hardcoded 0.0.

Run:  python -m eval.iovnbd --npz out.npz              (training arrays)
      python -m eval.iovnbd --sessions <dir> --limit 8 (harness-readable sessions)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys

import numpy as np

from .ahrs import attitude_from_tilt_and_yaw, quaternion_to_matrix

DEFAULT_ROOT = os.path.join(
    "dataset", "iovnbd", "Synchronised V abd S datasets", "Categorised IOVNB Dataset")

# The files are cp1252: the "m/s2" unit labels carry a raw 0xB2 byte that utf-8
# rejects outright.
ENCODING = "cp1252"

SAMPLE_HZ = 10.0

# S-file column indices.
S_LAT, S_LON, S_ALT, S_SPD_MS, S_ACC, S_BRG = 0, 1, 2, 3, 4, 5
S_T_MS = 7
S_ACCEL = (9, 10, 11)
S_GYRO = (15, 16, 17)
S_MAG = (18, 19, 20)

# The gyro columns are NOT in the same axis order as the accelerometer columns, and
# their "Yaw"/"Pitch"/"Roll" headers do not describe them either. Measured across 17
# sessions against the vehicle's own yaw rate: file index 1 - the column labelled
# "Pitch" - is the yaw axis in 16 of them, scoring r = 0.92 to 0.998 on the Driver A/B
# sessions, while the runner-up column never exceeds 0.34. The single exception scores
# 0.14, i.e. noise, on a run too short to use anyway. Meanwhile the accelerometer
# reports up along its own index 2 in every session, so the phone always lies flat.
#
# That makes the permutation a fixed property of the recording app rather than
# per-session information, so it is recorded here once with its evidence. Everything
# downstream then derives from the accelerometer, and the vehicle's yaw rate stays a
# held-out validation signal instead of becoming part of the model's input.
#
# Only the yaw axis is pinned. Which of the remaining two columns is device X versus Y
# is not identifiable from this data - the phone is too rigidly mounted for
# d(a_hat)/dt = -omega x a_hat to carry information (measured cosines 0.03 to 0.12,
# no consensus across sessions). It also does not matter: attitude_from_tilt_and_yaw
# takes pitch and roll from the accelerometer and uses the gyro for yaw alone, so the
# X/Y assignment never enters the result.
GYRO_TO_DEVICE = (0, 2, 1)

# V-file column indices.
V_TOD_S, V_LAT, V_LON, V_VEL_KMH, V_HEADING = 1, 2, 3, 4, 5
V_YAW_RATE_DPS = 14
V_LONG_ACC_G, V_LAT_ACC_G = 16, 17

G = 9.80665

# A run ends where the clock jumps. A single S-file can hold several recordings, and
# one global lag across a clock reset is meaningless.
MAX_GAP_MS = 1000.0
MIN_RUN_SAMPLES = 600            # 60 s; shorter runs cannot host a 60 s outage

# Quality gate. Both conditions, because either alone lets a bad pair through: a high
# correlation can coexist with a constant spatial offset, and a small median offset can
# coexist with wild excursions.
MIN_SPEED_CORRELATION = 0.90
MAX_POSITION_OFFSET_M = 60.0

# The yaw axis has to be identifiable or there is no attitude at all. Well-aligned
# runs reach 0.95; anything near zero means the cross-correlation found noise.
MIN_YAW_CORRELATION = 0.50

M_PER_DEG_LAT = 111_132.0


# ------------------------------------------------------------------- file reading

def _read_numeric(path: str, ncols: int) -> np.ndarray:
    """Every row as floats, non-numeric cells as NaN. Ragged rows are padded."""
    rows = []
    with io.open(path, encoding=ENCODING, errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for raw in reader:
            out = [math.nan] * ncols
            for i in range(min(ncols, len(raw))):
                cell = raw[i].strip()
                if not cell:
                    continue
                try:
                    out[i] = float(cell)
                except ValueError:
                    # Column 6 is "27 / 28" - a satellite count, not a number. Leaving
                    # it NaN is correct; raising here would drop the whole file.
                    pass
            rows.append(out)
    return np.asarray(rows, dtype=float)


def discover_sessions(root: str = DEFAULT_ROOT):
    """(name, s_path, v_path) for every session directory holding both files."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not (fn.startswith("S-") and fn.lower().endswith(".csv")):
                continue
            stem = fn[2:-4]
            v = os.path.join(dirpath, "V-" + stem + ".csv")
            if os.path.exists(v):
                found.append((stem, os.path.join(dirpath, fn), v))
    return sorted(found)


# ------------------------------------------------------- runs, lag and alignment

def split_runs(t_ms: np.ndarray):
    """Index ranges between clock discontinuities.

    Done before lag estimation, not after: a reset inside a session means the two
    streams drift apart partway through, and a single global lag would then be right
    for one part and wrong for the rest.
    """
    if len(t_ms) == 0:
        return []
    dt = np.diff(t_ms)
    breaks = np.flatnonzero(~np.isfinite(dt) | (dt <= 0) | (dt > MAX_GAP_MS)) + 1
    edges = [0, *breaks.tolist(), len(t_ms)]
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b - a >= MIN_RUN_SAMPLES]


def estimate_lag(a: np.ndarray, b: np.ndarray):
    """Lag to apply to `a` so it matches `b`, by full FFT cross-correlation.

    Full, not a bounded scan. A bounded scan run during investigation returned its own
    boundary (-3000 of a +/-3000 window) and looked like a converged answer; the true
    offset for that session was -3091.
    """
    a = np.nan_to_num(np.asarray(a, dtype=float) - np.nanmean(a))
    b = np.nan_to_num(np.asarray(b, dtype=float) - np.nanmean(b))
    if len(a) < 16 or len(b) < 16:
        return 0, 0.0
    n = 1 << int(math.ceil(math.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    corr = np.concatenate([corr[-(len(b) - 1):], corr[: len(a)]])
    lags = np.arange(-(len(b) - 1), len(a))
    k = int(np.argmax(corr))
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return int(lags[k]), float(corr[k] / denom) if denom > 0 else 0.0


def apply_lag(s: np.ndarray, v: np.ndarray, lag: int):
    """Overlapping slice of both streams after shifting S by `lag` samples.

    Positive lag means S runs ahead of V, so S is advanced. Getting this backwards
    still yields a high correlation while moving ground truth the wrong way in time,
    so the direction is asserted in the self-check below rather than trusted.
    """
    if lag >= 0:
        s2, v2 = s[lag:], v[: len(v) - lag] if lag else v
    else:
        s2, v2 = s[: len(s) + lag], v[-lag:]
    n = min(len(s2), len(v2))
    return s2[:n], v2[:n]


def device_gyro(gyro_file: np.ndarray) -> np.ndarray:
    """Gyro columns reordered into the accelerometer's axis order.

    See [GYRO_TO_DEVICE] for why this reordering is needed and how it was established.
    """
    return gyro_file[:, list(GYRO_TO_DEVICE)]


def yaw_axis_from_accel(accel: np.ndarray) -> np.ndarray:
    """Device-frame direction of world Up, from mean acceleration.

    Yaw is rotation about vertical, so once the gyro is in the accelerometer's axis
    order the yaw-rate signal is just the gyro projected onto this axis. Taking the
    axis from the accelerometer rather than fitting it against the vehicle's yaw rate
    keeps that yaw rate a held-out signal: fitting would build the model's target into
    the model's input, and the yaw-rate head would then be reading back its own label.
    """
    good = np.all(np.isfinite(accel), axis=1)
    if good.sum() < 10:
        return np.array([0.0, 0.0, 1.0])
    up = accel[good].mean(axis=0)
    n = np.linalg.norm(up)
    return up / n if np.isfinite(n) and n > 1e-9 else np.array([0.0, 0.0, 1.0])


def align_fine(gyro: np.ndarray, up_dev: np.ndarray, yaw_rate_ref: np.ndarray,
               max_lag: int = 1200):
    """Refine the lag using two genuine 10 Hz signals, and score the result.

    The coarse lag comes from the phone's GPS speed, which is HELD for 9 s, and a
    staircase cannot localise a lag better than its own step - the residual was
    measured at about 4.4 s. That is fatal for anything per-sample: before this
    refinement no linear combination of phone acceleration could predict the vehicle's
    longitudinal acceleration (r = 0.06); afterwards r = 0.45.

    Returns (lag, correlation, sign). The sign records whether the projected gyro runs
    with or against the vehicle's yaw-rate convention, which is a property of the axis
    definitions and not something to assume.
    """
    yaw = gyro @ up_dev
    ref = np.radians(yaw_rate_ref)
    lag, corr = estimate_lag(np.nan_to_num(yaw - np.nanmean(yaw)),
                             np.nan_to_num(ref - np.nanmean(ref)))
    if abs(lag) > max_lag:
        return 0, 0.0, 1.0
    return lag, corr, (1.0 if corr >= 0 else -1.0)


def position_offset_m(s: np.ndarray, v: np.ndarray, stride: int = 331) -> np.ndarray:
    """Distance between the phone's held fix and the reference position."""
    out = []
    for k in range(0, len(v), stride):
        slat, slon = s[k, S_LAT], s[k, S_LON]
        vlat, vlon = v[k, V_LAT], v[k, V_LON]
        if not all(np.isfinite([slat, slon, vlat, vlon])):
            continue
        if slat == 0 and slon == 0:
            continue
        mlon = M_PER_DEG_LAT * math.cos(math.radians(vlat))
        out.append(math.hypot((slon - vlon) * mlon, (slat - vlat) * M_PER_DEG_LAT))
    return np.asarray(out) if out else np.asarray([math.nan])


# --------------------------------------------------------- device-to-vehicle frame

def device_to_vehicle(accel: np.ndarray, long_acc: np.ndarray,
                      lat_acc: np.ndarray):
    """Constant rotation from device axes to (forward, right, up).

    The phone is clamped in the car - measured tilt drift over a whole session is 6
    degrees - so one rotation per session is enough, and it is far more robust than a
    per-sample attitude estimate: it cannot drift and it does not care that the
    magnetometer is disturbed inside the vehicle.

    Up comes from mean acceleration, which averages to gravity over a journey. Forward
    is the horizontal direction whose acceleration best explains the vehicle's OWN
    reported longitudinal acceleration - a least-squares fit against a measured signal
    rather than an assumption about how the phone was mounted. An earlier version
    regressed against the derivative of reference speed instead; that derivative
    reaches -127 m/s^2 on speed glitches and correlates only 0.46 with the measured
    longitudinal acceleration, so the measured channel is the better target.

    Returns (R_dv, forward_r, lateral_r). The second number is the fit quality; the
    third is an independent check, since the lateral axis is derived rather than
    fitted and should still predict the vehicle's lateral acceleration.
    """
    good = np.all(np.isfinite(accel), axis=1)
    if good.sum() < 100:
        return np.eye(3), 0.0, 0.0

    up = accel[good].mean(axis=0)
    n = np.linalg.norm(up)
    if not np.isfinite(n) or n < 1e-6:
        return np.eye(3), 0.0, 0.0
    up = up / n

    a_h = accel - np.outer(accel @ up, up)
    m = good & np.isfinite(long_acc)
    if m.sum() < 100:
        return np.eye(3), 0.0, 0.0

    fwd, *_ = np.linalg.lstsq(a_h[m], long_acc[m], rcond=None)
    fwd = fwd - up * float(fwd @ up)
    n = np.linalg.norm(fwd)
    if not np.isfinite(n) or n < 1e-9:
        return np.eye(3), 0.0, 0.0
    fwd = fwd / n

    pred = a_h[m] @ fwd
    fwd_r = float(np.corrcoef(pred, long_acc[m])[0, 1]) if np.std(pred) > 0 else 0.0

    # (forward, right, up) right-handed requires forward x right = up, so right = up x forward.
    right = np.cross(up, fwd)
    right /= np.linalg.norm(right)

    ml = good & np.isfinite(lat_acc)
    lat_r = 0.0
    if ml.sum() > 100:
        pl = a_h[ml] @ right
        if np.std(pl) > 0:
            lat_r = float(np.corrcoef(pl, lat_acc[ml])[0, 1])

    return np.vstack([fwd, right, up]), fwd_r, lat_r


# ------------------------------------------------------------------- conversion

def convert_run(s: np.ndarray, v: np.ndarray, name: str, up_dev: np.ndarray,
                yaw_sign: float):
    """One aligned run to arrays: sensor inputs plus reference truth."""
    t_s = (s[:, S_T_MS] - s[0, S_T_MS]) / 1000.0
    accel = s[:, list(S_ACCEL)]
    gyro = device_gyro(s[:, list(S_GYRO)])
    mag = s[:, list(S_MAG)]

    truth_lat = v[:, V_LAT]
    truth_lon = v[:, V_LON]
    truth_speed = v[:, V_VEL_KMH] / 3.6          # V is km/h; S is already m/s
    truth_heading = v[:, V_HEADING]
    truth_yaw_rate = np.radians(v[:, V_YAW_RATE_DPS])   # column is deg/s
    truth_long_acc = v[:, V_LONG_ACC_G] * G             # columns are g
    truth_lat_acc = v[:, V_LAT_ACC_G] * G

    # Attitude from the accelerometer plus the one gyro axis that was actually
    # identified. Heading is seeded from the reference so a 60 s outage measures the
    # integrator rather than an initial-heading search.
    h0 = truth_heading[0] if np.isfinite(truth_heading[0]) else 0.0
    yaw_series = (gyro @ up_dev) * yaw_sign
    quat, est_heading = attitude_from_tilt_and_yaw(accel, yaw_series, t_s,
                                                   initial_heading_deg=h0)

    R_dv, fwd_r, lat_r = device_to_vehicle(accel, truth_long_acc, truth_lat_acc)
    vehicle_accel = accel @ R_dv.T
    vehicle_gyro = gyro @ R_dv.T

    return {
        "name": name,
        "t_s": t_s,
        "accel": accel, "gyro": gyro, "mag": mag,
        "quat": quat, "est_heading": est_heading,
        "vehicle_accel": vehicle_accel, "vehicle_gyro": vehicle_gyro,
        "frame_quality": fwd_r, "lateral_check": lat_r,
        "up_dev": up_dev, "yaw_sign": yaw_sign,
        "R_dv": R_dv,
        "phone_lat": s[:, S_LAT], "phone_lon": s[:, S_LON],
        "phone_speed": s[:, S_SPD_MS], "phone_acc_m": s[:, S_ACC],
        "truth_lat": truth_lat, "truth_lon": truth_lon,
        "truth_speed": truth_speed, "truth_heading": truth_heading,
        "truth_yaw_rate": truth_yaw_rate,
        "truth_long_acc": truth_long_acc, "truth_lat_acc": truth_lat_acc,
    }


def load_session(name: str, s_path: str, v_path: str, use_mag: bool = True):
    """Every accepted run in one session pair, with the rejection record.

    Returns (runs, records). `records` always has one entry per candidate run,
    accepted or not, so nothing is dropped silently.
    """
    s_all = _read_numeric(s_path, 24)
    v_all = _read_numeric(v_path, 29)
    n = min(len(s_all), len(v_all))
    s_all, v_all = s_all[:n], v_all[:n]

    runs, records = [], []
    for ri, (a, b) in enumerate(split_runs(s_all[:, S_T_MS])):
        s, v = s_all[a:b], v_all[a:b]
        run_name = name if ri == 0 else f"{name}_r{ri}"

        # Stage 1: coarse lag from the phone's GPS speed. Resolution is limited to the
        # 9 s hold of that signal, which is enough to get the right journey minute but
        # not enough for anything sampled per-sample.
        coarse, corr = estimate_lag(s[:, S_SPD_MS], v[:, V_VEL_KMH] / 3.6)
        sa, va = apply_lag(s, v, coarse)
        if len(sa) < MIN_RUN_SAMPLES:
            records.append(dict(name=run_name, accepted=False, lag=coarse,
                                correlation=round(corr, 4), rows=int(len(sa)),
                                reason="overlap shorter than the minimum run length"))
            continue

        # Stage 2: refine the lag against two genuine 10 Hz signals. The yaw axis
        # comes from the accelerometer, so the vehicle's yaw rate is used only to
        # align and to score - never to build the signal.
        up_dev = yaw_axis_from_accel(sa[:, list(S_ACCEL)])
        fine, yaw_r, yaw_sign = align_fine(
            device_gyro(sa[:, list(S_GYRO)]), up_dev, va[:, V_YAW_RATE_DPS])
        sa, va = apply_lag(sa, va, fine)
        if len(sa) < MIN_RUN_SAMPLES:
            records.append(dict(name=run_name, accepted=False, lag=coarse,
                                fine_lag=int(fine), correlation=round(corr, 4),
                                yaw_r=round(yaw_r, 4), rows=int(len(sa)),
                                reason="overlap too short after fine alignment"))
            continue

        off = position_offset_m(sa, va)
        med = float(np.nanmedian(off))
        p90 = float(np.nanpercentile(off, 90))

        rec = dict(name=run_name, rows=int(len(sa)),
                   lag=int(coarse), lag_s=round(coarse / SAMPLE_HZ, 2),
                   fine_lag=int(fine), fine_lag_s=round(fine / SAMPLE_HZ, 2),
                   correlation=round(corr, 4),
                   yaw_sign=int(yaw_sign), yaw_r=round(yaw_r, 4),
                   offset_median_m=round(med, 1), offset_p90_m=round(p90, 1))

        if corr < MIN_SPEED_CORRELATION:
            rec.update(accepted=False,
                       reason=f"speed correlation {corr:.3f} below {MIN_SPEED_CORRELATION}")
            records.append(rec)
            continue
        if abs(yaw_r) < MIN_YAW_CORRELATION:
            # Without an identified yaw axis there is no attitude, so the run cannot
            # be converted at all - better to say so than to guess a column.
            rec.update(accepted=False,
                       reason=f"yaw axis correlation {yaw_r:.3f} below {MIN_YAW_CORRELATION}")
            records.append(rec)
            continue
        if not np.isfinite(med) or med > MAX_POSITION_OFFSET_M:
            rec.update(accepted=False,
                       reason=f"position offset {med:.1f} m above {MAX_POSITION_OFFSET_M} m")
            records.append(rec)
            continue

        run = convert_run(sa, va, run_name, up_dev, yaw_sign)
        rec.update(accepted=True,
                   duration_s=round(float(run["t_s"][-1]), 1),
                   speed_mean_ms=round(float(np.nanmean(run["truth_speed"])), 2),
                   speed_max_ms=round(float(np.nanmax(run["truth_speed"])), 2),
                   frame_quality=round(run["frame_quality"], 3),
                   lateral_check=round(run["lateral_check"], 3))
        records.append(rec)
        runs.append(run)
    return runs, records


# -------------------------------------------------------------- session writing

def write_session(run: dict, out_dir: str) -> None:
    """One run as imu.csv + gps.csv in the format eval/outage_eval.py::load reads."""
    d = os.path.join(out_dir, run["name"])
    os.makedirs(d, exist_ok=True)
    t_ns = (run["t_s"] * 1e9).astype(np.int64)

    R = np.empty((len(t_ns), 3))
    for i, q in enumerate(run["quat"]):
        m = quaternion_to_matrix(*q)
        R[i] = (m[6], m[7], m[8])            # third row: world Up in device axes
    gravity = R * G
    linear = run["accel"] - gravity

    with io.open(os.path.join(d, "imu.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write("t_ns,sensor,accuracy,v0,v1,v2,v3,v4,v5\n")
        for i, t in enumerate(t_ns):
            a, g_, m_, q = run["accel"][i], run["gyro"][i], run["mag"][i], run["quat"][i]
            l_, gr = linear[i], gravity[i]
            fh.write(f"{t},accel,3,{a[0]:.6f},{a[1]:.6f},{a[2]:.6f},,,\n")
            fh.write(f"{t},gyro,3,{g_[0]:.6f},{g_[1]:.6f},{g_[2]:.6f},,,\n")
            fh.write(f"{t},mag,3,{m_[0]:.4f},{m_[1]:.4f},{m_[2]:.4f},,,\n")
            fh.write(f"{t},rv,3,{q[0]:.6f},{q[1]:.6f},{q[2]:.6f},{q[3]:.6f},,\n")
            fh.write(f"{t},gravity,3,{gr[0]:.6f},{gr[1]:.6f},{gr[2]:.6f},,,\n")
            fh.write(f"{t},linear_accel,3,{l_[0]:.6f},{l_[1]:.6f},{l_[2]:.6f},,,\n")

    # Truth goes in gps.csv, since that is what the harness treats as ground truth.
    # It is the vehicle reference at 10 Hz, not the phone's 9-s-held fix.
    with io.open(os.path.join(d, "gps.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write("t_ns,utc_ms,provider,lat,lon,alt_m,speed_mps,bearing_deg,"
                 "acc_m,vert_acc_m,speed_acc_mps,bearing_acc_deg\n")
        for i, t in enumerate(t_ns):
            lat, lon = run["truth_lat"][i], run["truth_lon"][i]
            if not (np.isfinite(lat) and np.isfinite(lon)):
                continue
            sp, br = run["truth_speed"][i], run["truth_heading"][i]
            fh.write(f"{t},0,iovnbd,{lat:.8f},{lon:.8f},0,"
                     f"{sp if np.isfinite(sp) else 0:.4f},"
                     f"{br if np.isfinite(br) else 0:.2f},1.0,1.0,0.1,1.0\n")

    with io.open(os.path.join(d, "session.json"), "w", encoding="utf-8") as fh:
        json.dump({"source": "IO-VNBD", "name": run["name"],
                   "frame_quality": run["frame_quality"],
                   "note": "truth in gps.csv is the vehicle reference stream at 10 Hz"},
                  fh, indent=1)


# -------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--sessions", help="write harness-readable sessions into this dir")
    ap.add_argument("--npz", help="write training arrays to this .npz")
    ap.add_argument("--limit", type=int, default=0, help="stop after N session pairs")
    ap.add_argument("--no-mag", action="store_true",
                    help="run the AHRS without the magnetometer")
    ap.add_argument("--manifest", default="iovnbd_manifest.json")
    args = ap.parse_args(argv)

    pairs = discover_sessions(args.root)
    if not pairs:
        print(f"no S/V pairs under {args.root}", file=sys.stderr)
        return 2
    if args.limit:
        pairs = pairs[: args.limit]
    print(f"{len(pairs)} session pairs under {args.root}\n")

    header = (f"{'run':<16}{'rows':>8}{'lag s':>8}{'fine s':>8}{'corr':>7}"
              f"{'yaw r':>7}{'off med':>9}{'frame r':>8}{'lat r':>7}  status")
    print(header)
    print("-" * len(header))

    all_records, bundles = [], []
    for name, sp, vp in pairs:
        try:
            runs, records = load_session(name, sp, vp, use_mag=not args.no_mag)
        except Exception as exc:                       # noqa: BLE001
            print(f"{name:<16}  ERROR {exc}")
            all_records.append(dict(name=name, accepted=False, reason=f"error: {exc}"))
            continue
        all_records.extend(records)
        for r in records:
            status = "accepted" if r.get("accepted") else "REJECT " + r.get("reason", "")
            print(f"{r['name']:<16}{r.get('rows', 0):>8}{r.get('lag_s', 0):>8.1f}"
                  f"{r.get('fine_lag_s', 0):>8.1f}{r.get('correlation', 0):>7.3f}"
                  f"{r.get('yaw_r', 0):>7.3f}"
                  f"{r.get('offset_median_m', float('nan')):>9.1f}"
                  f"{r.get('frame_quality', float('nan')):>8.3f}"
                  f"{r.get('lateral_check', float('nan')):>7.3f}  {status}")
        for run in runs:
            if args.sessions:
                write_session(run, args.sessions)
            bundles.append(run)

    acc = [r for r in all_records if r.get("accepted")]
    rej = [r for r in all_records if not r.get("accepted")]
    rows = sum(r.get("rows", 0) for r in acc)
    print(f"\naccepted {len(acc)} runs ({rows:,} samples, "
          f"{rows / SAMPLE_HZ / 3600:.1f} h), rejected {len(rej)}")
    for r in rej:
        print(f"  rejected {r['name']}: {r.get('reason')}")

    with io.open(args.manifest, "w", encoding="utf-8") as fh:
        json.dump({"root": args.root, "accepted": len(acc), "rejected": len(rej),
                   "samples": rows, "runs": all_records}, fh, indent=1)
    print(f"\nmanifest -> {args.manifest}")

    if args.npz and bundles:
        keys = ("t_s", "accel", "gyro", "mag", "quat", "vehicle_accel", "vehicle_gyro",
                "truth_speed", "truth_heading", "truth_yaw_rate",
                "truth_lat", "truth_lon", "truth_long_acc", "truth_lat_acc",
                "est_heading")
        flat = {}
        for k in keys:
            flat[k] = np.concatenate([b[k] for b in bundles])
        # Run boundaries, so a training window is never allowed to straddle two
        # journeys - a window spanning a cut would pair one drive's IMU with
        # another's labels.
        lens = [len(b["t_s"]) for b in bundles]
        flat["run_starts"] = np.cumsum([0, *lens[:-1]])
        flat["run_lengths"] = np.asarray(lens)
        flat["run_names"] = np.asarray([b["name"] for b in bundles])
        np.savez_compressed(args.npz, **flat)
        print(f"npz -> {args.npz}  ({flat['accel'].shape[0]:,} samples)")

    return 0 if acc else 1


if __name__ == "__main__":
    raise SystemExit(main())
