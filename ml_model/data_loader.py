import numpy as np
import pandas as pd
from scipy.signal import butter, lfilter
import torch
from torch.utils.data import Dataset, DataLoader

class PhysicsPreprocessor:
    @staticmethod
    def get_rotation_matrix(gravity, geomag):
        """
        Calculates a rotation matrix from gravity and geomagnetic vectors.
        (Simplified version of Android's SensorManager.getRotationMatrix)
        """
        A = np.copy(gravity)
        E = np.copy(geomag)
        
        norm_A = np.linalg.norm(A)
        if norm_A == 0:
            return np.eye(3)
        A = A / norm_A
        
        norm_E = np.linalg.norm(E)
        if norm_E == 0:
            return np.eye(3)
        E = E / norm_E
        
        # H = E x A
        H = np.cross(E, A)
        norm_H = np.linalg.norm(H)
        if norm_H == 0:
            return np.eye(3)
        H = H / norm_H
        
        # M = A x H
        M = np.cross(A, H)
        
        R = np.array([H, M, A])
        return R

class IMUDataset(Dataset):
    def __init__(self, csv_file, window_size=50, is_dummy=False, hz=100):
        self.window_size = window_size
        self.is_dummy = is_dummy
        self.hz = hz
        
        # Data requirements:
        # Inputs: Transformed Accel (3), Gyro (3), Last GPS Speed (1), Last GPS Bearing (1) = 8 features
        # Targets: Delta Velocity (1), Delta Bearing (1) = 2 targets
        
        if is_dummy:
            # Generate dummy dataset
            length = 2000
            # Raw: Accel(3), Gyro(3), Mag(3), Gravity(3), GPS Speed(1), GPS Bearing(1)
            raw_data = np.random.randn(length, 14)
            # Make dummy gravity point mostly down
            raw_data[:, 9:12] = np.array([0, 0, 9.81]) + np.random.randn(length, 3)*0.1
            # Make dummy mag point mostly North
            raw_data[:, 6:9] = np.array([0, 30, 30]) + np.random.randn(length, 3)*0.1
        else:
            try:
                df = pd.read_csv(csv_file, encoding='latin1')
                if len(df) < hz * 15:
                    raise ValueError("File too short for calibration.")
                # Extracted from CSV: [Ax, Ay, Az, Gx, Gy, Gz, Mx, My, Mz, GravX, GravY, GravZ, GpsSpeed, GpsBearing]
                raw_data = df.iloc[:, [9,10,11, 15,16,17, 18,19,20, 21,22,23, 3,4]].values
            except Exception as e:
                print(f"Failed to load dataset: {e}. Falling back to dummy data.")
                self.is_dummy = True
                length = 2000
                raw_data = np.random.randn(length, 14)
                raw_data[:, 9:12] = np.array([0, 0, 9.81]) + np.random.randn(length, 3)*0.1
                raw_data[:, 6:9] = np.array([0, 30, 30]) + np.random.randn(length, 3)*0.1

        # --- CALIBRATION PHASE (First 15 seconds) ---
        # 0-5s: Grace period (skipped)
        # 5-15s: Log orientation to find base Rotation Matrix
        start_idx = int(5 * self.hz)
        end_idx = int(15 * self.hz)
        if len(raw_data) <= end_idx:
            # If dataset is too short, just use whatever is available
            start_idx = 0
            end_idx = len(raw_data)
        
        avg_gravity = np.mean(raw_data[start_idx:end_idx, 9:12], axis=0)
        avg_mag = np.mean(raw_data[start_idx:end_idx, 6:9], axis=0)
        
        self.R_base = PhysicsPreprocessor.get_rotation_matrix(avg_gravity, avg_mag)
        
        # --- PREPROCESSING ---
        # Discard the 15s calibration phase for training
        valid_data = raw_data[end_idx:]
        
        if len(valid_data) > 1:
            acc = valid_data[:-1, 0:3]
            gyro = valid_data[:-1, 3:6]
            mag = valid_data[:-1, 6:9]
            grav = valid_data[:-1, 9:12]
            gps_speed = valid_data[:-1, 12:13]
            gps_bearing = valid_data[:-1, 13:14]
            
            # 1. Cancel gravity
            linear_acc = acc - grav
            
            # 2. Transform linear acc to Earth frame
            # Vectorized cross products for rotation matrices
            norm_grav = np.linalg.norm(grav, axis=1, keepdims=True)
            norm_grav[norm_grav == 0] = 1.0
            A = grav / norm_grav
            
            norm_mag = np.linalg.norm(mag, axis=1, keepdims=True)
            norm_mag[norm_mag == 0] = 1.0
            E = mag / norm_mag
            
            H = np.cross(E, A)
            norm_H = np.linalg.norm(H, axis=1, keepdims=True)
            norm_H[norm_H == 0] = 1.0
            H = H / norm_H
            
            M = np.cross(A, H)
            
            # Apply rotation R to each linear_acc vector
            # R is [H, M, A] (3x3). For vector v, R * v = [H.v, M.v, A.v]
            earth_acc_x = np.sum(H * linear_acc, axis=1, keepdims=True)
            earth_acc_y = np.sum(M * linear_acc, axis=1, keepdims=True)
            earth_acc_z = np.sum(A * linear_acc, axis=1, keepdims=True)
            earth_acc = np.concatenate([earth_acc_x, earth_acc_y, earth_acc_z], axis=1)
            
            self.data_filtered = np.concatenate([earth_acc, gyro, gps_speed, gps_bearing], axis=1)
            
            next_gps_speed = valid_data[1:, 12:13]
            next_gps_bearing = valid_data[1:, 13:14]
            
            delta_v = next_gps_speed - gps_speed
            delta_theta = next_gps_bearing - gps_bearing
            self.targets = np.concatenate([delta_v, delta_theta], axis=1)
        else:
            self.data_filtered = np.empty((0, 8))
            self.targets = np.empty((0, 2))

    def __len__(self):
        if len(self.data_filtered) <= self.window_size:
            return 0
        return len(self.data_filtered) - self.window_size

    def __getitem__(self, idx):
        x = self.data_filtered[idx:idx + self.window_size, :]
        y = self.targets[idx + self.window_size, :]
        return torch.tensor(x, dtype=torch.float32), torch.tensor(y, dtype=torch.float32)

def get_dataloader(csv_file='dataset.csv', batch_size=32, window_size=50, is_dummy=True):
    dataset = IMUDataset(csv_file=csv_file, window_size=window_size, is_dummy=is_dummy)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    return dataloader
