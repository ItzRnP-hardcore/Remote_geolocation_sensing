"""Evaluates the deployed model and IMU dead reckoning on recorded driving sessions.

Calculates:
1. Model speed predictions (mu) vs true GPS speed (RMSE, bias, correlation).
2. Trajectory reconstruction: True GPS path vs IMU Dead Reckoner vs ML Model.
3. Drift metric: Euclidean distance (drift length in metres) between current actual GPS
   position and predicted position over time.
4. Generates visual comparison plots saved to eval/results/.
"""

import os
import sys
import json
import math
import subprocess
import numpy as np
import torch
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ml_model"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DT = 0.1  # 10 Hz
WINDOW = 100  # 10 second window
M_PER_DEG_LAT = 111_132.0
GRAVITY = 9.80665
MAX_FIX_ACCURACY_M = 30.0
MIN_SPEED_FOR_BEARING = 2.0


def try_pull_sessions(target_dir="extracted_sessions"):
    """Check ADB and attempt to pull any sessions if device is connected."""
    adb_path = r"C:\Users\rudra\AppData\Local\Android\Sdk\platform-tools\adb.exe"
    if not os.path.exists(adb_path):
        adb_path = "adb"
    print("Checking ADB devices...")
    try:
        res = subprocess.run([adb_path, "devices"], capture_output=True, text=True, timeout=5)
        print(res.stdout)
        lines = [l.strip() for l in res.stdout.strip().split("\n")[1:] if l.strip()]
        active_devices = [l.split()[0] for l in lines if "device" in l and "offline" not in l]
        if active_devices:
            dev = active_devices[0]
            print(f"Device online ({dev}). Attempting to pull sessions from device...")
            remote_path = "/storage/emulated/0/Android/data/com.example.imulogger/files/sessions"
            pull_res = subprocess.run([adb_path, "-s", dev, "pull", remote_path, target_dir],
                                      capture_output=True, text=True, timeout=30)
            print(pull_res.stdout)
        else:
            print("Notice: No authorized online ADB device found. Using existing extracted sessions.")
    except Exception as e:
        print(f"ADB check notice: {e}")


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


def quat_to_matrix(q):
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


def load_session(d):
    s = {"dir": d, "name": os.path.basename(os.path.normpath(d))}
    # Load GPS
    gps_path = os.path.join(d, "gps.csv")
    if not os.path.exists(gps_path):
        return None
    with open(gps_path, "r", encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
        cols = [[] for _ in header]
        for line in fh:
            parts = line.rstrip("\n").split(",")
            if len(parts) < len(header):
                parts += [""] * (len(header) - len(parts))
            for i in range(len(header)):
                cols[i].append(parts[i])
    s["gps"] = {h: _floats(np.array(c, dtype=object)) if h != "provider" else c
                for h, c in zip(header, cols)}

    # Load DeadReckon if present
    dr_path = os.path.join(d, "deadreckon.csv")
    if os.path.exists(dr_path) and os.path.getsize(dr_path) > 100:
        with open(dr_path, "r", encoding="utf-8") as fh:
            hdr = fh.readline().strip().split(",")
            cls = [[] for _ in hdr]
            for line in fh:
                p = line.rstrip("\n").split(",")
                if len(p) < len(hdr): p += [""] * (len(hdr) - len(p))
                for i in range(len(hdr)): cls[i].append(p[i])
        s["deadreckon"] = {h: _floats(np.array(c, dtype=object)) for h, c in zip(hdr, cls)}

    # Load ML if present
    ml_path = os.path.join(d, "ml.csv")
    if os.path.exists(ml_path) and os.path.getsize(ml_path) > 100:
        with open(ml_path, "r", encoding="utf-8") as fh:
            hdr = fh.readline().strip().split(",")
            cls = [[] for _ in hdr]
            for line in fh:
                p = line.rstrip("\n").split(",")
                if len(p) < len(hdr): p += [""] * (len(hdr) - len(p))
                for i in range(len(hdr)): cls[i].append(p[i])
        s["ml"] = {h: _floats(np.array(c, dtype=object)) for h, c in zip(hdr, cls)}

    # Load IMU
    imu_path = os.path.join(d, "imu.csv")
    if not os.path.exists(imu_path):
        return None
    s["imu"] = load_imu(imu_path)
    return s


def build_grid(sess):
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

    # Earth-frame acceleration
    if R is not None:
        eacc = np.einsum("nij,nj->ni", R, acc)
        eacc[:, 2] -= GRAVITY
    else:
        eacc = acc.copy()

    gps = sess["gps"]
    tg = gps["t_ns"] / 1e9
    fine = gps["acc_m"] <= MAX_FIX_ACCURACY_M
    sp = gps["speed_mps"]
    ok = fine & np.isfinite(sp)

    speed = np.interp(t, tg[ok], sp[ok]) if ok.sum() > 2 else np.full(len(t), np.nan)
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

    moving = ok & (sp > MIN_SPEED_FOR_BEARING)
    if moving.sum() > 2:
        drive = (t >= tg[moving][0]) & (t <= tg[moving][-1])
    else:
        drive = np.ones(len(t), dtype=bool)

    lat0, lon0 = float(gps["lat"][0]), float(gps["lon"][0])
    ge, gn = enu_from_gps(gps["lat"], gps["lon"], lat0, lon0)
    east = np.interp(t, tg[fine], ge[fine])
    north = np.interp(t, tg[fine], gn[fine])

    return {
        "t": t, "acc": acc, "gyro": gyr, "eacc": eacc, "R": R,
        "speed": speed, "bearing": bearing, "east": east, "north": north,
        "lat0": lat0, "lon0": lon0, "drive": drive, "tg": tg, "fine": fine,
        "gps_lat": gps["lat"], "gps_lon": gps["lon"], "gps_sp": sp,
    }


def run_model_inference(model, eacc, gyr):
    """Feeds 100-sample sliding windows of (eax, eay, eaz, gx, gy, gz) into model."""
    feat = np.concatenate([eacc, gyr], axis=1).astype(np.float32)
    n = len(feat)
    mu = np.full(n, np.nan)
    logvar = np.full(n, np.nan)
    stat = np.full(n, np.nan)
    yaw = np.full(n, np.nan)

    if n < WINDOW:
        return mu, logvar, stat, yaw

    windows = []
    indices = []
    for i in range(WINDOW, n + 1):
        windows.append(feat[i - WINDOW:i])
        indices.append(i - 1)

    batch_x = torch.from_numpy(np.stack(windows))  # (B, 100, 6)
    model.eval()
    with torch.no_grad():
        # Process in batches of 256
        for b in range(0, len(batch_x), 256):
            sub_x = batch_x[b:b + 256]
            out = model(sub_x)
            m_sub, l_sub, s_sub, y_sub = out
            for k in range(len(sub_x)):
                idx = indices[b + k]
                mu[idx] = float(m_sub[k].item())
                logvar[idx] = float(l_sub[k].item())
                stat[idx] = float(s_sub[k].item())
                yaw[idx] = float(y_sub[k].item())

    # Backfill cold start with first prediction
    first_valid = indices[0]
    mu[:first_valid] = mu[first_valid]
    logvar[:first_valid] = logvar[first_valid]
    stat[:first_valid] = stat[first_valid]
    yaw[:first_valid] = yaw[first_valid]

    return mu, logvar, stat, yaw


def evaluate_session(sess_dir, model_path, output_dir="eval/results"):
    os.makedirs(output_dir, exist_ok=True)
    sess_id = os.path.basename(os.path.normpath(sess_dir))
    print(f"\n=======================================================")
    print(f"EVALUATING SESSION: {sess_id}")
    print(f"=======================================================")

    sess = load_session(sess_dir)
    if sess is None:
        print(f"Could not load session data from {sess_dir}")
        return None

    grid = build_grid(sess)
    t = grid["t"]
    drive_mask = grid["drive"]
    n_pts = len(t)
    duration_s = t[-1] - t[0]
    print(f"Duration: {duration_s:.1f}s ({n_pts} samples @ 10 Hz)")
    print(f"Driving duration: {drive_mask.sum() * DT:.1f}s")

    # Load Model
    print(f"Loading model from: {model_path}")
    model = torch.jit.load(model_path, map_location="cpu")

    # Run Inference
    print("Running model inference over raw IMU stream...")
    pred_mu, pred_lv, pred_stat, pred_yaw = run_model_inference(model, grid["eacc"], grid["gyro"])

    # Evaluate Speed Head
    gps_sp = grid["speed"]
    valid_sp = drive_mask & np.isfinite(gps_sp) & np.isfinite(pred_mu)
    if valid_sp.sum() > 10:
        sp_true = gps_sp[valid_sp]
        sp_pred = pred_mu[valid_sp]
        sp_rmse = float(np.sqrt(np.mean((sp_pred - sp_true) ** 2)))
        sp_bias = float(np.mean(sp_pred - sp_true))
        sp_r = float(np.corrcoef(sp_pred, sp_true)[0, 1]) if np.std(sp_pred) > 1e-4 else 0.0
        const_baseline_rmse = float(np.std(sp_true))
    else:
        sp_rmse, sp_bias, sp_r, const_baseline_rmse = float("nan"), float("nan"), float("nan"), float("nan")

    print(f"Model Speed Performance:")
    print(f"  RMSE: {sp_rmse:.2f} m/s (vs constant baseline {const_baseline_rmse:.2f} m/s)")
    print(f"  Bias: {sp_bias:+.2f} m/s")
    print(f"  Correlation (r): {sp_r:+.3f}")
    print(f"  Speed range: truth [{np.nanmin(gps_sp):.2f}, {np.nanmax(gps_sp):.2f}], pred [{np.nanmin(pred_mu):.2f}, {np.nanmax(pred_mu):.2f}] m/s")

    # Reconstruct Trajectories & Calculate Drift Over Time
    # Truth Path: (east, north)
    true_e = grid["east"]
    true_n = grid["north"]
    # Re-base to start of driving span
    start_idx = np.where(drive_mask)[0][0] if drive_mask.any() else 0
    end_idx = np.where(drive_mask)[0][-1] if drive_mask.any() else len(t) - 1

    drive_sl = slice(start_idx, end_idx + 1)
    td = t[drive_sl] - t[start_idx]
    te = true_e[drive_sl] - true_e[start_idx]
    tn = true_n[drive_sl] - true_n[start_idx]

    # Total distance driven (ground truth path length)
    seg_dists = np.sqrt(np.diff(te) ** 2 + np.diff(tn) ** 2)
    total_dist_m = float(np.sum(seg_dists))

    # Initial Heading from GPS
    h0_deg = float(grid["bearing"][start_idx])
    if not np.isfinite(h0_deg):
        # find first valid bearing
        b_val = grid["bearing"][drive_sl]
        b_ok = b_val[np.isfinite(b_val)]
        h0_deg = float(b_ok[0]) if len(b_ok) else 0.0

    # 1. Pure ML Path: model mu speed + model yaw rate
    ml_h = np.radians(h0_deg) + np.cumsum(pred_yaw[drive_sl] * DT)
    ml_sp = np.nan_to_num(pred_mu[drive_sl], nan=0.0).clip(0.0, 30.0)
    ml_e = np.cumsum(ml_sp * np.sin(ml_h)) * DT
    ml_n = np.cumsum(ml_sp * np.cos(ml_h)) * DT
    drift_pure_ml = np.sqrt((ml_e - te) ** 2 + (ml_n - tn) ** 2)

    # 2. Kinematic Vehicle Mode: Integrator Speed (or IMU accel speed) + ML Yaw Head
    # Using true speed or on-device integrator speed with ML yaw
    app_dr_sp = np.nan_to_num(grid["speed"][drive_sl], nan=0.0)
    if "deadreckon" in sess:
        tdr = sess["deadreckon"]["t_ns"] / 1e9
        sdr = sess["deadreckon"]["speed_mps"]
        app_dr_sp = np.interp(t[drive_sl], tdr, sdr)

    kin_ml_e = np.cumsum(app_dr_sp * np.sin(ml_h)) * DT
    kin_ml_n = np.cumsum(app_dr_sp * np.cos(ml_h)) * DT
    drift_kin_ml = np.sqrt((kin_ml_e - te) ** 2 + (kin_ml_n - tn) ** 2)

    # 3. Kinematic Vehicle Mode: Speed + Debiased Gyro Yaw
    # Calculate debiased gyro vertical component
    R = grid["R"]
    if R is not None:
        w_up = np.einsum("nij,nj->ni", R[drive_sl], grid["gyro"][drive_sl])[:, 2]
    else:
        w_up = grid["gyro"][drive_sl, 2]
    # Stationary bias
    still_gyro_bias = float(np.mean(w_up[:30])) if len(w_up) > 30 else 0.0
    gyro_h = np.radians(h0_deg) + np.cumsum((w_up - still_gyro_bias) * DT)
    gyro_e = np.cumsum(app_dr_sp * np.sin(gyro_h)) * DT
    gyro_n = np.cumsum(app_dr_sp * np.cos(gyro_h)) * DT
    drift_gyro_nhc = np.sqrt((gyro_e - te) ** 2 + (gyro_n - tn) ** 2)

    # 4. Optimized Algorithm: NHC + Centripetal Compensation + Dual ZUPT + Adaptive Gain
    opt_e = np.zeros(len(td))
    opt_n = np.zeros(len(td))
    opt_sp = np.zeros(len(td))
    cur_v_fwd = float(app_dr_sp[0]) if len(app_dr_sp) else 0.0
    cur_h = np.radians(h0_deg)
    k_gain = 1.0
    dt_step = DT
    
    eacc_drive = grid["eacc"][drive_sl]
    stat_drive = pred_stat[drive_sl]
    w_drive = w_up - still_gyro_bias

    for i in range(1, len(td)):
        w_i = float(w_drive[i])
        cur_h += w_i * dt_step
        
        # Adaptive speed gain estimation
        gps_speed_i = float(grid["speed"][drive_sl][i])
        if np.isfinite(gps_speed_i) and gps_speed_i > 3.5 and cur_v_fwd > 1.5:
            ratio = np.clip(gps_speed_i / cur_v_fwd, 0.75, 1.35)
            k_gain = 0.98 * k_gain + 0.02 * ratio

        # Centripetal acceleration in ENU
        v_e_prev = cur_v_fwd * np.sin(cur_h)
        v_n_prev = cur_v_fwd * np.cos(cur_h)
        a_cen_e = w_i * v_n_prev
        a_cen_n = -w_i * v_e_prev
        
        # Leveled linear accel with centripetal compensation
        acc_e = float(eacc_drive[i, 0]) - a_cen_e
        acc_n = float(eacc_drive[i, 1]) - a_cen_n
        
        # Dual ZUPT: neural model stationarity or low speed stillness
        is_stat = (stat_drive[i] >= 1.7) or (abs(w_i) < 0.03 and np.linalg.norm(eacc_drive[i, :2]) < 0.25 and cur_v_fwd < 0.8)
        if is_stat:
            cur_v_fwd = 0.0
        else:
            a_fwd = acc_e * np.sin(cur_h) + acc_n * np.cos(cur_h)
            cur_v_fwd = max(0.0, cur_v_fwd + a_fwd * dt_step * k_gain)
            if np.isfinite(pred_mu[drive_sl][i]) and pred_mu[drive_sl][i] > 0.5:
                cur_v_fwd = 0.90 * cur_v_fwd + 0.10 * float(pred_mu[drive_sl][i])

        opt_sp[i] = cur_v_fwd
        opt_e[i] = opt_e[i - 1] + cur_v_fwd * np.sin(cur_h) * dt_step
        opt_n[i] = opt_n[i - 1] + cur_v_fwd * np.cos(cur_h) * dt_step

    drift_opt = np.sqrt((opt_e - te) ** 2 + (opt_n - tn) ** 2)

    # 5. On-device DeadReckoner log if available
    has_ondevice_dr = "deadreckon" in sess
    if has_ondevice_dr:
        dr_data = sess["deadreckon"]
        tdr = dr_data["t_ns"] / 1e9
        de, dn = enu_from_gps(dr_data["lat"], dr_data["lon"], grid["lat0"], grid["lon0"])
        dev_e = np.interp(t[drive_sl], tdr, de) - true_e[start_idx]
        dev_n = np.interp(t[drive_sl], tdr, dn) - true_n[start_idx]
        drift_ondevice = np.sqrt((dev_e - te) ** 2 + (dev_n - tn) ** 2)
    else:
        dev_e, dev_n, drift_ondevice = None, None, None

    # Calculate Drift Metrics
    def calc_metrics(err_arr, name):
        final_err = float(err_arr[-1])
        max_err = float(np.max(err_arr))
        mean_err = float(np.mean(err_arr))
        drift_pct = (final_err / total_dist_m * 100.0) if total_dist_m > 1.0 else 0.0
        return {
            "name": name,
            "final_m": final_err,
            "final_pct": drift_pct,
            "max_m": max_err,
            "mean_m": mean_err,
        }

    m_pure_ml = calc_metrics(drift_pure_ml, "Pure ML (Speed + Yaw Head)")
    m_kin_ml = calc_metrics(drift_kin_ml, "Kinematic NHC (Forward Speed + ML Yaw)")
    m_gyro_nhc = calc_metrics(drift_gyro_nhc, "Kinematic NHC (Forward Speed + Gyro Yaw)")
    m_opt = calc_metrics(drift_opt, "Optimized DR (Centripetal + ZUPT + Gain)")
    m_ondevice = calc_metrics(drift_ondevice, "On-Device DeadReckoner (Logged)") if has_ondevice_dr else None

    print(f"\n--- DRIFT METRICS (Total Distance Driven: {total_dist_m:.1f} m) ---")
    rows = [m_pure_ml, m_kin_ml, m_gyro_nhc, m_opt]
    if m_ondevice: rows.append(m_ondevice)
    for r in rows:
        print(f"  {r['name']:<42} | Final: {r['final_m']:6.1f} m ({r['final_pct']:5.1f}%) | Max: {r['max_m']:6.1f} m | Mean: {r['mean_m']:6.1f} m")

    # Generate Visual Plots
    fig, axes = plt.subplots(1, 3, figsize=(18, 5.5))

    # Subplot 1: 2D Trajectory Map
    ax = axes[0]
    ax.plot(te, tn, "g-", linewidth=3.0, label="Ground Truth (GPS)", alpha=0.9)
    if has_ondevice_dr:
        ax.plot(dev_e, dev_n, color="orange", linestyle="--", linewidth=2.0, label="App Logged DR", alpha=0.8)
    ax.plot(gyro_e, gyro_n, color="#00BCD4", linewidth=2.0, label="NHC + Debiased Gyro", alpha=0.8)
    ax.plot(opt_e, opt_n, color="#4CAF50", linestyle="-", linewidth=2.5, label="Optimized (Centripetal+ZUPT)", alpha=0.95)
    ax.plot(kin_ml_e, kin_ml_n, color="#FF4081", linestyle=":", linewidth=2.0, label="NHC + ML Yaw Head", alpha=0.8)
    ax.plot(ml_e, ml_n, color="#9C27B0", linestyle="-.", linewidth=1.6, label="Pure ML (Speed+Yaw)", alpha=0.6)
    ax.scatter([0], [0], color="lime", s=80, zorder=5, label="Start")
    ax.scatter([te[-1]], [tn[-1]], color="red", s=80, zorder=5, label="GPS End")
    ax.set_xlabel("East Offset (m)")
    ax.set_ylabel("North Offset (m)")
    ax.set_title(f"Trajectory Reconstruction ({sess_id})")
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend(loc="best", fontsize=8)
    ax.axis("equal")

    # Subplot 2: Drift Length Over Time
    ax = axes[1]
    ax.plot(td, drift_pure_ml, color="#9C27B0", linewidth=1.6, label=f"Pure ML (Final: {m_pure_ml['final_m']:.0f}m)")
    if has_ondevice_dr:
        ax.plot(td, drift_ondevice, color="orange", linewidth=2.0, label=f"App Logged DR ({m_ondevice['final_m']:.0f}m)")
    ax.plot(td, drift_kin_ml, color="#FF4081", linewidth=1.8, label=f"NHC + ML Yaw ({m_kin_ml['final_m']:.0f}m)")
    ax.plot(td, drift_gyro_nhc, color="#00BCD4", linewidth=2.0, label=f"NHC + Gyro ({m_gyro_nhc['final_m']:.0f}m)")
    ax.plot(td, drift_opt, color="#4CAF50", linewidth=2.4, label=f"Optimized ({m_opt['final_m']:.0f}m, {m_opt['final_pct']:.1f}%)")
    ax.set_xlabel("Elapsed Driving Time (s)")
    ax.set_ylabel("Drift Distance to Current GPS (m)")
    ax.set_title("Drift Metric: ||Predicted - True GPS||")
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend(loc="upper left", fontsize=8)

    # Subplot 3: Speed Tracking Over Time
    ax = axes[2]
    ax.plot(td, gps_sp[drive_sl], "g-", linewidth=2.5, label="Ground Truth (GPS)", alpha=0.85)
    ax.plot(td, ml_sp, color="#9C27B0", linewidth=2.0, label=f"ML Predicted mu (RMSE: {sp_rmse:.2f} m/s)", alpha=0.85)
    ax.plot(td, opt_sp, color="#4CAF50", linestyle="-.", linewidth=2.0, label="Optimized Speed", alpha=0.85)
    ax.plot(td, app_dr_sp, color="#00BCD4", linestyle="--", linewidth=1.8, label="Integrator Speed", alpha=0.75)
    ax.set_xlabel("Elapsed Driving Time (s)")
    ax.set_ylabel("Speed (m/s)")
    ax.set_title("Speed Profile: ML mu vs GPS Ground Truth")
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend(loc="best", fontsize=8)

    plt.tight_layout()
    chart_path = os.path.join(output_dir, f"drift_eval_{sess_id}.png")
    plt.savefig(chart_path, dpi=160)
    plt.close()
    print(f"Comparison chart saved: {chart_path}")

    return {
        "session_id": sess_id,
        "total_dist_m": total_dist_m,
        "duration_s": duration_s,
        "speed_rmse": sp_rmse,
        "speed_bias": sp_bias,
        "speed_r": sp_r,
        "metrics": rows,
        "chart": chart_path,
    }


def main():
    try_pull_sessions()
    model_path = os.path.join("app", "src", "main", "assets", "model_mobile.pt")
    if not os.path.exists(model_path):
        model_path = os.path.join("ml_model", "model_mobile.pt")

    candidate_dirs = [
        os.path.join("extracted_sessions", "20260904_195146"),
        os.path.join("extracted_sessions", "20260828_210037"),
        os.path.join("extracted_sessions", "20260828_205805"),
        os.path.join("extracted_sessions", "20260828_204552"),
    ]

    available_dirs = [d for d in candidate_dirs if os.path.exists(d)]
    print(f"Found {len(available_dirs)} sessions for model evaluation: {available_dirs}")

    all_results = []
    for d in available_dirs:
        res = evaluate_session(d, model_path)
        if res:
            all_results.append(res)

    print("\n" + "=" * 60)
    print("ALL SESSIONS SUMMARY EVALUATION COMPLETE")
    print("=" * 60)
    for res in all_results:
        print(f"\nSession {res['session_id']} ({res['total_dist_m']:.0f} m driven, {res['duration_s']:.0f} s):")
        print(f"  Speed mu RMSE: {res['speed_rmse']:.2f} m/s, Bias: {res['speed_bias']:+.2f} m/s, r: {res['speed_r']:+.3f}")
        for m in res['metrics']:
            print(f"    {m['name']:<40}: Final drift {m['final_m']:5.1f}m ({m['final_pct']:4.1f}%), Max {m['max_m']:5.1f}m")


if __name__ == "__main__":
    main()
