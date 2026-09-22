#!/usr/bin/env python3
"""Automated ADB session puller, model evaluator, and track replay tool.

Usage:
    python tools/fetch_and_eval_sessions.py --pull
    python tools/fetch_and_eval_sessions.py --session 20260915_184316 --plot
    python tools/fetch_and_eval_sessions.py --pull --all --plot
"""

import argparse
import glob
import json
import os
import subprocess
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch

ADB_PATH = os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")
if not os.path.exists(ADB_PATH):
    ADB_PATH = "adb"

PHONE_SESSIONS_DIR = "/sdcard/Android/data/com.example.imulogger/files/sessions"
LOCAL_SESSIONS_DIR = os.path.join("extracted_sessions", "sessions")
RESNET_PATH = "app/build/intermediates/assets/release/mergeReleaseAssets/model_mobile.pt"
TCN_PATH = "app/src/main/assets/model_mobile.pt"


def run_adb(cmd, timeout=30):
    """Run an ADB command and return stdout string."""
    full_cmd = [ADB_PATH] + cmd
    try:
        res = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"[ADB ERROR] Command {cmd} failed: {e.stderr}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ADB ERROR] {e}", file=sys.stderr)
        return None


def get_connected_device():
    """Detect authorized ADB device serial."""
    out = run_adb(["devices"])
    if not out:
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def pull_sessions(dest_dir=LOCAL_SESSIONS_DIR):
    """Pull all sessions from connected phone to local laptop directory in a single stream."""
    serial = get_connected_device()
    if not serial:
        print("[WARN] No authorized Android device detected via ADB.")
        return False

    print(f"[ADB] Found device: {serial}")
    os.makedirs("extracted_sessions", exist_ok=True)
    print(f"[ADB] Pulling {PHONE_SESSIONS_DIR} -> extracted_sessions/ ...")
    res = run_adb(["-s", serial, "pull", PHONE_SESSIONS_DIR, "extracted_sessions"], timeout=180)
    if res:
        print(f"[ADB] Result:\n{res}")
    return True


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2.0) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2.0) ** 2
    return 2.0 * R * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def quat_to_matrix(q):
    """(N, 4) rotation vector -> (N, 3, 3) rotation matrix."""
    x, y, z = q[:, 0], q[:, 1], q[:, 2]
    w = q[:, 3].copy()
    bad = ~np.isfinite(w)
    if bad.any():
        w[bad] = np.sqrt(np.clip(1.0 - (x[bad]**2 + y[bad]**2 + z[bad]**2), 0.0, 1.0))
    n = np.sqrt(x*x + y*y + z*z + w*w)
    n[n < 1e-12] = 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    R = np.empty((len(q), 3, 3))
    R[:, 0, 0] = 1 - 2 * (y*y + z*z)
    R[:, 0, 1] = 2 * (x*y - z*w)
    R[:, 0, 2] = 2 * (x*z + y*w)
    R[:, 1, 0] = 2 * (x*y + z*w)
    R[:, 1, 1] = 1 - 2 * (x*x + z*z)
    R[:, 1, 2] = 2 * (y*z - x*w)
    R[:, 2, 0] = 2 * (x*z - y*w)
    R[:, 2, 1] = 2 * (y*z + x*w)
    R[:, 2, 2] = 1 - 2 * (x*x + y*y)
    return R


def simulate_kinematic_dr(imu_file, start_lat, start_lon, start_speed=0.0, start_bearing_deg=0.0):
    """Run Python simulation of the on-device kinematic DeadReckoner with Centripetal comp and ZUPT."""
    lat = start_lat
    lon = start_lon
    speed = start_speed
    heading_deg = start_bearing_deg

    dt = 0.01  # approx 100 Hz
    last_t_ns = 0

    points = []
    points.append((lat, lon, speed, heading_deg, 0.0))

    rot_matrix = np.eye(3)
    stationary = False

    # Read imu.csv in chunks
    with open(imu_file, "r", encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 6:
                continue
            sensor = parts[1]
            try:
                t_ns = int(parts[0])
                v0 = float(parts[3])
                v1 = float(parts[4])
                v2 = float(parts[5])
            except ValueError:
                continue

            if sensor in ("rv", "game_rv"):
                v3 = float(parts[6]) if len(parts) > 6 and parts[6] else 0.0
                rot_matrix = quat_to_matrix(np.array([[v0, v1, v2, v3]]))[0]

            elif sensor == "gyro":
                if last_t_ns != 0:
                    dt = max(1e-3, min((t_ns - last_t_ns) / 1e9, 0.1))
                last_t_ns = t_ns

                # Rotate gyro to world vertical
                w_up = rot_matrix[6] * v0 + rot_matrix[7] * v1 + rot_matrix[8] * v2
                yaw_rate_cw_deg = np.degrees(-w_up)

                if not stationary:
                    heading_deg = (heading_deg + yaw_rate_cw_deg * dt) % 360.0

            elif sensor == "accel":
                norm_a = np.sqrt(v0*v0 + v1*v1 + v2*v2)
                if abs(norm_a - 9.80665) < 0.15:
                    stationary = True
                    speed = 0.0
                else:
                    stationary = False

                if last_t_ns != 0:
                    dt = max(1e-3, min((t_ns - last_t_ns) / 1e9, 0.1))
                last_t_ns = t_ns

                # World frame horizontal acceleration
                eax = rot_matrix[0] * v0 + rot_matrix[1] * v1 + rot_matrix[2] * v2
                eay = rot_matrix[3] * v0 + rot_matrix[4] * v1 + rot_matrix[5] * v2

                # Forward acceleration along heading
                hd_rad = np.radians(heading_deg)
                a_fwd = eax * np.sin(hd_rad) + eay * np.cos(hd_rad)

                if not stationary:
                    speed = max(0.0, speed + a_fwd * dt)
                    dist = speed * dt
                    d_lat = (dist * np.cos(hd_rad)) / 111132.0
                    d_lon = (dist * np.sin(hd_rad)) / (111132.0 * np.cos(np.radians(lat)))
                    lat += d_lat
                    lon += d_lon

                if len(points) == 0 or (t_ns - points[-1][4]) > 5e8:
                    points.append((lat, lon, speed, heading_deg, t_ns))

    return points


def evaluate_session(session_dir, plot=True, out_dir="scratch"):
    """Evaluate session and compare models and kinematics."""
    session_id = os.path.basename(session_dir)
    print("=" * 70)
    print(f"EVALUATING SESSION: {session_id}")
    print("=" * 70)

    gps_file = os.path.join(session_dir, "gps.csv")
    imu_file = os.path.join(session_dir, "imu.csv")
    dr_file = os.path.join(session_dir, "deadreckon.csv")
    recomp_file = os.path.join(session_dir, "deadreckon_recomputed.csv")
    mm_file = os.path.join(session_dir, "mapmatch.csv")

    if not os.path.exists(gps_file):
        print(f"Error: {gps_file} not found.")
        return None

    df_gps = pd.read_csv(gps_file)
    lat_col = [c for c in df_gps.columns if "lat" in c.lower()][0]
    lon_col = [c for c in df_gps.columns if "lon" in c.lower()][0]
    gps_lats = df_gps[lat_col].values
    gps_lons = df_gps[lon_col].values
    gps_speeds = df_gps["speed_mps"].values if "speed_mps" in df_gps.columns else np.zeros(len(df_gps))
    gps_bearings = df_gps["bearing_deg"].values if "bearing_deg" in df_gps.columns else np.zeros(len(df_gps))

    total_dist_m = np.sum(haversine(gps_lats[:-1], gps_lons[:-1], gps_lats[1:], gps_lons[1:]))
    last_gps_lat, last_gps_lon = gps_lats[-1], gps_lons[-1]
    max_speed_kmh = np.nanmax(gps_speeds) * 3.6 if len(gps_speeds) else 0.0

    print(f"Ground Truth GPS: {len(df_gps)} fixes, {total_dist_m:.1f} m driven, max speed: {max_speed_kmh:.1f} km/h")

    # 1. Evaluate Original Logged Dead Reckoning
    orig_drift = np.nan
    dr_lats, dr_lons = None, None
    if os.path.exists(dr_file):
        df_dr = pd.read_csv(dr_file)
        if len(df_dr) > 0 and "lat" in df_dr.columns:
            dr_lats = df_dr["lat"].values
            dr_lons = df_dr["lon"].values
            orig_drift = haversine(dr_lats[-1], dr_lons[-1], last_gps_lat, last_gps_lon)
            print(f"Original On-Device DR:  Final Drift: {orig_drift:.1f} m ({orig_drift / total_dist_m * 100:.1f}%)")

    # 2. Evaluate Map Matching
    mm_drift = np.nan
    mm_lats, mm_lons = None, None
    if os.path.exists(mm_file):
        df_mm = pd.read_csv(mm_file)
        if len(df_mm) > 0 and "lat" in df_mm.columns:
            mm_lats = df_mm["lat"].values
            mm_lons = df_mm["lon"].values
            mm_drift = haversine(mm_lats[-1], mm_lons[-1], last_gps_lat, last_gps_lon)
            print(f"Logged Map Matching:   Final Drift: {mm_drift:.1f} m ({mm_drift / total_dist_m * 100:.1f}%)")

    # 3. Evaluate Recomputed DR (if present from phone)
    recomp_drift = np.nan
    rec_lats, rec_lons = None, None
    if os.path.exists(recomp_file):
        df_rec = pd.read_csv(recomp_file)
        if len(df_rec) > 0 and "lat" in df_rec.columns:
            rec_lats = df_rec["lat"].values
            rec_lons = df_rec["lon"].values
            recomp_drift = haversine(rec_lats[-1], rec_lons[-1], last_gps_lat, last_gps_lon)
            print(f"Recomputed DR:         Final Drift: {recomp_drift:.1f} m ({recomp_drift / total_dist_m * 100:.1f}%)")

    # 4. Evaluate Models (ResNet1D vs TCN) if imu.csv is present
    models = {}
    if os.path.exists(RESNET_PATH):
        try:
            models["ResNet1D (15.5MB)"] = torch.jit.load(RESNET_PATH).eval()
        except Exception:
            pass
    if os.path.exists(TCN_PATH):
        try:
            models["TCN (0.7MB)"] = torch.jit.load(TCN_PATH).eval()
        except Exception:
            pass

    # Produce Plot
    if plot:
        os.makedirs(out_dir, exist_ok=True)
        fig, ax = plt.subplots(figsize=(11, 9))

        ax.plot(gps_lons, gps_lats, "g.-", label=f"True GPS ({total_dist_m:.0f}m)", linewidth=2.5, markersize=3)
        if dr_lats is not None:
            ax.plot(dr_lons, dr_lats, "b--", label=f"Orig DR (drift: {orig_drift:.1f}m)", alpha=0.7)
        if mm_lats is not None:
            ax.plot(mm_lons, mm_lats, "c:", label=f"Map Matched (drift: {mm_drift:.1f}m)", linewidth=2.5)
        if rec_lats is not None:
            ax.plot(rec_lons, rec_lats, "r-", label=f"Recomputed (drift: {recomp_drift:.1f}m)", linewidth=1.5, alpha=0.8)

        ax.set_title(f"Track Replay & Map-Matching Analysis — {session_id}", fontsize=13, fontweight="bold")
        ax.set_xlabel("Longitude", fontsize=11)
        ax.set_ylabel("Latitude", fontsize=11)
        ax.legend(loc="best", framealpha=0.9)
        ax.grid(True, linestyle="--", alpha=0.5)

        out_path = os.path.join(out_dir, f"session_eval_{session_id}.png")
        fig.tight_layout()
        fig.savefig(out_path, dpi=140)
        plt.close(fig)
        print(f"Saved plot to: {out_path}")

    return {
        "session_id": session_id,
        "total_dist_m": total_dist_m,
        "orig_drift_m": orig_drift,
        "mapmatch_drift_m": mm_drift,
        "recomputed_drift_m": recomp_drift,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull", action="store_true", help="Pull all sessions from phone via ADB")
    parser.add_argument("--session", type=str, help="Specific session ID to evaluate")
    parser.add_argument("--all", action="store_true", help="Evaluate all local sessions")
    parser.add_argument("--plot", action="store_true", default=True, help="Save comparison plots")
    parser.add_argument("--out-dir", type=str, default="scratch", help="Output directory for plots")
    args = parser.parse_args()

    if args.pull:
        pull_sessions()

    sessions = []
    if args.session:
        sdir = os.path.join(LOCAL_SESSIONS_DIR, args.session)
        if not os.path.exists(sdir):
            sdir = os.path.join("extracted_sessions", args.session)
        if os.path.exists(sdir):
            sessions.append(sdir)
        else:
            print(f"Error: Session {args.session} not found in {LOCAL_SESSIONS_DIR}")
            return 1
    elif args.all:
        sessions = sorted(glob.glob(os.path.join(LOCAL_SESSIONS_DIR, "*")))
        sessions = [s for s in sessions if os.path.isdir(s) and os.path.exists(os.path.join(s, "gps.csv"))]

    if not sessions and not args.pull:
        # Default: evaluate the latest driving sessions
        for sid in ["20260915_184316", "20260912_220347", "20260904_195146"]:
            sdir = os.path.join(LOCAL_SESSIONS_DIR, sid)
            if os.path.exists(sdir):
                sessions.append(sdir)

    results = []
    for sdir in sessions:
        res = evaluate_session(sdir, plot=args.plot, out_dir=args.out_dir)
        if res:
            results.append(res)

    if results:
        print("\n" + "=" * 75)
        print("SESSION EVALUATION SUMMARY TABLE")
        print("=" * 75)
        print(f"{'Session ID':<18} {'Distance':<10} {'Orig DR Drift':<16} {'MapMatch Drift':<16} {'Recomp Drift':<14}")
        print("-" * 75)
        for r in results:
            d_str = f"{r['total_dist_m']:.0f}m"
            orig_str = f"{r['orig_drift_m']:.1f}m" if not np.isnan(r['orig_drift_m']) else "N/A"
            mm_str = f"{r['mapmatch_drift_m']:.1f}m" if not np.isnan(r['mapmatch_drift_m']) else "N/A"
            rec_str = f"{r['recomputed_drift_m']:.1f}m" if not np.isnan(r['recomputed_drift_m']) else "N/A"
            print(f"{r['session_id']:<18} {d_str:<10} {orig_str:<16} {mm_str:<16} {rec_str:<14}")
        print("=" * 75)

    return 0


if __name__ == "__main__":
    sys.exit(main())
