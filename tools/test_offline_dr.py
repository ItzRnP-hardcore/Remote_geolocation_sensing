#!/usr/bin/env python3
"""Offline Neural Dead Reckoning evaluator for phone sessions.

Replays raw IMU data from a session folder through the fine-tuned TCN model
and physical gyro AHRS integration, exactly matching the Android app's
SessionRecomputer and live SensorService pipeline.
"""

import math
import os
import sys
import numpy as np
import pandas as pd
import torch
import matplotlib.pyplot as plt

sys.path.insert(0, ".")
import eval.session_eval as se
from ml_model.tcn_model import TCNModel, WINDOW_SAMPLES, IN_CHANNELS


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlam = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2.0) ** 2
    return 2.0 * R * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))


def evaluate_session_dr(sess_dir, model_path="app/src/main/assets/model_mobile.pt", plot=True, out_png=None):
    sess_id = os.path.basename(sess_dir)
    print("=" * 70)
    print(f"OFFLINE NEURAL DR EVALUATION: {sess_id}")
    print("=" * 70)

    gps_path = os.path.join(sess_dir, "gps.csv")
    imu_path = os.path.join(sess_dir, "imu.csv")
    if not os.path.exists(gps_path) or not os.path.exists(imu_path):
        print("Missing gps.csv or imu.csv")
        return None

    df_gps = pd.read_csv(gps_path)
    if len(df_gps) < 5:
        print("Not enough GPS points")
        return None

    # Load session and uniform 10 Hz grid
    sess = se.load_session(sess_dir)
    grid = se.build_grid(sess)

    t_grid = grid["t"]
    dt = 0.1  # 10 Hz
    N = len(t_grid)
    if N < WINDOW_SAMPLES:
        print(f"Session too short ({N} < {WINDOW_SAMPLES})")
        return None

    # Load PyTorch mobile model (Lite module or state_dict)
    print(f"Loading model: {model_path}...")
    try:
        model = torch.jit.load(model_path).eval()
        use_jit = True
    except Exception:
        # Fallback to TCNModel state dict
        model = TCNModel(channels=(64, 64, 64, 64, 64, 64), dilations=(1, 2, 4, 8, 16, 32))
        state = torch.load("ml_model/model_tcn_finetuned.pth", map_location="cpu", weights_only=True)
        model.load_state_dict(state)
        model.eval()
        use_jit = False

    # Extract 6-channel Earth-frame features
    acc_earth = se.earth_frame(grid["acc"], grid["R"])
    gyr = grid["gyro"]
    feats = np.concatenate([acc_earth, gyr], axis=1)  # (N, 6)

    # Initial position from first GPS fix
    start_lat = float(df_gps["lat"].iloc[0])
    start_lon = float(df_gps["lon"].iloc[0])
    curr_lat = start_lat
    curr_lon = start_lon

    # Initial heading: from first moving GPS bearing, or 0
    gps_bearings = df_gps["bearing_deg"].values
    gps_speeds = df_gps["speed_mps"].values if "speed_mps" in df_gps.columns else np.zeros(len(df_gps))
    valid_hd = gps_bearings[(gps_speeds >= 1.5) & np.isfinite(gps_bearings)]
    curr_heading_deg = float(valid_hd[0]) if len(valid_hd) > 0 else float(gps_bearings[0] if np.isfinite(gps_bearings[0]) else 0.0)

    dr_points = [(curr_lat, curr_lon, 0.0, curr_heading_deg, t_grid[0])]
    pred_speeds = []
    true_speeds_aligned = []

    # Run inference every 0.1s
    with torch.no_grad():
        for i in range(WINDOW_SAMPLES, N):
            win = feats[i - WINDOW_SAMPLES:i]  # (100, 6)
            
            # Predict speed and stationarity
            if use_jit:
                inp = torch.from_numpy(win).float().unsqueeze(0)  # (1, 100, 6)
                out = model(inp)  # tuple of (mu, logvar, stat_logit, yaw_rate)
                speed_mu = float(out[0].item())
                stat_logit = float(out[2].item())
            else:
                inp = torch.from_numpy(win.T).float().unsqueeze(0)  # (1, 6, 100)
                out = model(inp)
                speed_mu = float(out["mu"].item())
                stat_logit = float(out["stationary_logit"].item())

            is_stat = (1.0 / (1.0 + math.exp(-stat_logit))) >= 0.5
            pred_speed = 0.0 if is_stat else max(0.0, speed_mu)
            pred_speeds.append(pred_speed)

            # Ground truth speed at this step
            gt_sp = grid["speed"][i]
            if np.isfinite(gt_sp):
                true_speeds_aligned.append((pred_speed, gt_sp))

            # Gyro AHRS integration (rot around world Z axis)
            R_curr = grid["R"][i]  # 3x3 rotation matrix
            # R is column-major or row-major: R * g_body => g_world
            # World vertical up component:
            w_up = R_curr[2, 0] * gyr[i, 0] + R_curr[2, 1] * gyr[i, 1] + R_curr[2, 2] * gyr[i, 2]
            yaw_rate_cw_deg = -np.degrees(w_up)

            if not is_stat:
                curr_heading_deg = (curr_heading_deg + yaw_rate_cw_deg * dt) % 360.0

            # Step forward
            hd_rad = math.radians(curr_heading_deg)
            dist_m = pred_speed * dt
            d_lat = (dist_m * math.cos(hd_rad)) / 111132.0
            d_lon = (dist_m * math.sin(hd_rad)) / (111132.0 * math.cos(math.radians(curr_lat)))

            curr_lat += d_lat
            curr_lon += d_lon
            dr_points.append((curr_lat, curr_lon, pred_speed, curr_heading_deg, t_grid[i]))

    dr_lats = np.array([p[0] for p in dr_points])
    dr_lons = np.array([p[1] for p in dr_points])

    gps_lats = df_gps["lat"].values
    gps_lons = df_gps["lon"].values
    total_dist_m = float(np.sum(haversine(gps_lats[:-1], gps_lons[:-1], gps_lats[1:], gps_lons[1:])))
    final_drift_m = float(haversine(dr_lats[-1], dr_lons[-1], gps_lats[-1], gps_lons[-1]))

    # Compute speed metrics
    if true_speeds_aligned:
        preds = np.array([p[0] for p in true_speeds_aligned])
        trues = np.array([p[1] for p in true_speeds_aligned])
        rmse = float(np.sqrt(np.mean((preds - trues) ** 2)))
        bias = float(np.mean(preds - trues))
        corr = float(np.corrcoef(preds, trues)[0, 1]) if len(trues) > 1 else 0.0
    else:
        rmse, bias, corr = 0.0, 0.0, 0.0

    print(f"Total True GPS Distance: {total_dist_m:.1f} m")
    print(f"Final DR Drift:          {final_drift_m:.1f} m ({final_drift_m / total_dist_m * 100:.1f}%)")
    print(f"Predicted Speed RMSE:    {rmse:.2f} m/s")
    print(f"Predicted Speed Bias:    {bias:+.2f} m/s")
    print(f"Speed Correlation r:     {corr:.3f}")

    if plot:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        # Trajectory
        ax1.plot(gps_lons, gps_lats, "g.-", label=f"True GPS ({total_dist_m:.0f}m)", linewidth=2.5, markersize=3)
        ax1.plot(dr_lons, dr_lats, "r-", label=f"Fine-Tuned Neural DR (drift: {final_drift_m:.1f}m)", linewidth=2.0)
        ax1.plot(gps_lons[0], gps_lats[0], "ks", markersize=8, label="Start")
        ax1.plot(gps_lons[-1], gps_lats[-1], "g*", markersize=10, label="GPS End")
        ax1.plot(dr_lons[-1], dr_lats[-1], "r*", markersize=10, label="DR End")
        ax1.set_title(f"Trajectory Replay: {sess_id}", fontsize=12, fontweight="bold")
        ax1.set_xlabel("Longitude")
        ax1.set_ylabel("Latitude")
        ax1.legend(loc="best")
        ax1.grid(True, linestyle="--", alpha=0.5)

        # Speed Comparison
        if true_speeds_aligned:
            t_axis = np.arange(len(preds)) * 0.1
            ax2.plot(t_axis, trues * 3.6, "g-", label="True GPS Speed", alpha=0.7)
            ax2.plot(t_axis, preds * 3.6, "r-", label=f"Fine-Tuned TCN (RMSE {rmse*3.6:.1f} km/h)", alpha=0.8)
            ax2.set_title("Speed Profile: Fine-Tuned TCN vs GPS", fontsize=12, fontweight="bold")
            ax2.set_xlabel("Time (s)")
            ax2.set_ylabel("Speed (km/h)")
            ax2.legend(loc="best")
            ax2.grid(True, linestyle="--", alpha=0.5)

        plt.tight_layout()
        if out_png:
            os.makedirs(os.path.dirname(out_png), exist_ok=True)
            plt.savefig(out_png, dpi=130)
            print(f"Saved plot to: {out_png}")
        plt.close(fig)

    return {
        "session_id": sess_id,
        "total_dist_m": total_dist_m,
        "final_drift_m": final_drift_m,
        "speed_rmse": rmse,
        "speed_bias": bias,
        "speed_corr": corr,
    }


if __name__ == "__main__":
    sess = sys.argv[1] if len(sys.argv) > 1 else "extracted_sessions/sessions/20260915_184316"
    out = sys.argv[2] if len(sys.argv) > 2 else "scratch/eval_finetuned_20260915_184316.png"
    evaluate_session_dr(sess, out_png=out)
