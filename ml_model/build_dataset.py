import os
import pandas as pd
import numpy as np
import torch
from scipy.spatial.transform import Rotation
import math

def process_session(session_path):
    print(f"Processing session: {session_path}")
    imu_path = os.path.join(session_path, 'imu.csv')
    gps_path = os.path.join(session_path, 'gps.csv')
    
    if not os.path.exists(imu_path) or not os.path.exists(gps_path):
        print("Missing IMU or GPS data. Skipping.")
        return None

    # Load IMU
    df_imu = pd.read_csv(imu_path)
    # Pivot to get time-aligned sensor readings (approximate by sorting and ffill)
    df_imu = df_imu.sort_values('t_ns')
    
    # Separate sensors
    df_linacc = df_imu[df_imu['sensor'] == 'linear_accel'][['t_ns', 'v0', 'v1', 'v2']].rename(columns={'v0': 'lax', 'v1': 'lay', 'v2': 'laz'})
    df_gyro = df_imu[df_imu['sensor'] == 'gyro'][['t_ns', 'v0', 'v1', 'v2']].rename(columns={'v0': 'gx', 'v1': 'gy', 'v2': 'gz'})
    df_rv = df_imu[df_imu['sensor'] == 'rv'][['t_ns', 'v0', 'v1', 'v2', 'v3']].rename(columns={'v0': 'qx', 'v1': 'qy', 'v2': 'qz', 'v3': 'qw'})
    
    # Merge on nearest timestamp (resample to 10Hz)
    # Convert t_ns to datetime for resampling
    df_linacc['time'] = pd.to_datetime(df_linacc['t_ns'], unit='ns')
    df_gyro['time'] = pd.to_datetime(df_gyro['t_ns'], unit='ns')
    df_rv['time'] = pd.to_datetime(df_rv['t_ns'], unit='ns')
    
    df_linacc.set_index('time', inplace=True)
    df_gyro.set_index('time', inplace=True)
    df_rv.set_index('time', inplace=True)
    
    # Resample to 100ms (10Hz)
    df_linacc = df_linacc.resample('100ms').mean().interpolate()
    df_gyro = df_gyro.resample('100ms').mean().interpolate()
    df_rv = df_rv.resample('100ms').mean().interpolate()
    
    # Combine
    df_combined = pd.concat([df_linacc, df_gyro, df_rv], axis=1).dropna()
    if df_combined.empty:
        return None
        
    # Apply Rotation Vector to Linear Acceleration to get Earth Frame Acceleration
    # Android Quat is [qx, qy, qz, qw]
    quats = df_combined[['qx', 'qy', 'qz', 'qw']].values
    # Normalize quaternions to avoid scipy warnings
    norms = np.linalg.norm(quats, axis=1, keepdims=True)
    norms[norms == 0] = 1 # Avoid division by zero
    quats = quats / norms
    
    rotations = Rotation.from_quat(quats)
    lin_acc = df_combined[['lax', 'lay', 'laz']].values
    # Rotate vectors
    earth_acc = rotations.apply(lin_acc)
    
    df_combined['earth_ax'] = earth_acc[:, 0]
    df_combined['earth_ay'] = earth_acc[:, 1]
    df_combined['earth_az'] = earth_acc[:, 2]
    
    # Now load GPS for labels
    df_gps = pd.read_csv(gps_path)
    if df_gps.empty:
        return None
        
    df_gps['time'] = pd.to_datetime(df_gps['t_ns'], unit='ns')
    df_gps.set_index('time', inplace=True)
    df_gps = df_gps.select_dtypes(include=[np.number]).resample('100ms').mean().interpolate() # Interp to 10Hz
    
    # Calculate displacement (mu) per second.
    if 'speed_mps' not in df_gps.columns:
        df_gps['speed_mps'] = 0.0
        
    df_merged = df_combined.join(df_gps[['speed_mps']], how='inner').dropna()
    
    # Stationary is when speed < 0.5 m/s
    df_merged['stationary'] = (df_merged['speed_mps'] < 0.5).astype(float)
    df_merged['yaw_rate'] = 0.0 # Dummy target for yaw rate
    
    # Create 100-sample windows
    features = df_merged[['earth_ax', 'earth_ay', 'earth_az', 'gx', 'gy', 'gz']].values
    targets = df_merged[['speed_mps', 'stationary', 'yaw_rate']].values
    
    windows = []
    window_targets = []
    
    for i in range(0, len(features) - 100, 50): # 50-sample stride (5 seconds overlap)
        x = features[i:i+100]
        # Target for the window is the end target of the window
        y = targets[i+99] 
        
        # Shape: (6, 100)
        windows.append(x.T)
        window_targets.append(y)
        
    return windows, window_targets

if __name__ == '__main__':
    sessions_dir = r"C:\Users\rudra\Documents\Remote_geolocation_sensing\dataset\device_sessions\sessions"
    all_windows = []
    all_targets = []
    
    for session_name in os.listdir(sessions_dir):
        session_path = os.path.join(sessions_dir, session_name)
        if os.path.isdir(session_path):
            result = process_session(session_path)
            if result is not None:
                w, t = result
                all_windows.extend(w)
                all_targets.extend(t)
                
    if len(all_windows) == 0:
        print("No windows generated!")
        exit(1)
        
    all_windows = np.array(all_windows, dtype=np.float32)
    all_targets = np.array(all_targets, dtype=np.float32)
    
    print(f"Generated {len(all_windows)} windows of shape {all_windows.shape[1:]}")
    
    torch.save((torch.tensor(all_windows), torch.tensor(all_targets)), "dataset.pt")
    print("Saved dataset.pt")
