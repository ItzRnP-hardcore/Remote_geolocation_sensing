import numpy as np
import pandas as pd
from scipy.signal import butter, lfilter
import torch
from torch.utils.data import Dataset, DataLoader

class ClassicalFilters:
    @staticmethod
    def butter_lowpass(cutoff, fs, order=5):
        nyq = 0.5 * fs
        normal_cutoff = cutoff / nyq
        b, a = butter(order, normal_cutoff, btype='low', analog=False)
        return b, a

    @staticmethod
    def butter_highpass(cutoff, fs, order=5):
        nyq = 0.5 * fs
        normal_cutoff = cutoff / nyq
        b, a = butter(order, normal_cutoff, btype='high', analog=False)
        return b, a

    @staticmethod
    def apply_lowpass_filter(data, cutoff=5.0, fs=100.0, order=5):
        """
        Applies a lowpass filter to remove high-frequency vibration noise.
        """
        b, a = ClassicalFilters.butter_lowpass(cutoff, fs, order=order)
        y = lfilter(b, a, data, axis=0)
        return y
        
    @staticmethod
    def apply_highpass_filter(data, cutoff=0.1, fs=100.0, order=5):
        """
        Applies a high block filter (highpass) to remove low-frequency drift/white noise,
        although bias instability (very low frequency) is better handled by ML.
        """
        b, a = ClassicalFilters.butter_highpass(cutoff, fs, order=order)
        y = lfilter(b, a, data, axis=0)
        return y


class IMUDataset(Dataset):
    def __init__(self, csv_file, window_size=50, is_dummy=False):
        """
        Args:
            csv_file (string): Path to the csv file with IMU data.
            window_size (int): Number of time steps to use in one sequence.
            is_dummy (bool): If true, generates random dummy data for testing.
        """
        self.window_size = window_size
        self.is_dummy = is_dummy
        
        if is_dummy:
            # Generate dummy data for 1000 samples
            # Assuming 6 features: ax, ay, az, gx, gy, gz
            self.data_raw = np.random.randn(1000, 6)
            # Dummy target: predicting position (x, y) or velocity (vx, vy)
            self.targets = np.random.randn(1000, 2)
        else:
            try:
                # Assuming the CSV has columns for acc and gyro, and target pos/vel
                # Use latin1 encoding to handle special characters like ²
                df = pd.read_csv(csv_file, encoding='latin1')
                # Ensure the CSV is actually downloaded, not a Git LFS pointer
                if len(df) < 10:
                    raise ValueError("File is too small. Is it a Git LFS pointer?")
                
                # Use column indices to avoid encoding issues with special characters like ² or 
                feature_indices = [9, 10, 11, 15, 16, 17] # Accel X, Y, Z, Gyro Yaw, Pitch, Roll
                target_indices = [0, 1] # GPS Lat, Lon
                
                self.data_raw = df.iloc[:, feature_indices].values
                self.targets = df.iloc[:, target_indices].values
            except Exception as e:
                print(f"Failed to load dataset: {e}")
                print("Falling back to dummy data for testing.")
                self.is_dummy = True
                self.data_raw = np.random.randn(1000, 6)
                self.targets = np.random.randn(1000, 2)
        
        # Apply classical filtering (Preprocessing)
        # Apply lowpass to remove vehicle vibration
        self.data_filtered = ClassicalFilters.apply_lowpass_filter(self.data_raw, cutoff=5.0, fs=100.0)

    def __len__(self):
        return len(self.data_filtered) - self.window_size

    def __getitem__(self, idx):
        # Return a sequence of IMU data and the target at the end of the sequence
        x = self.data_filtered[idx:idx + self.window_size, :]
        y = self.targets[idx + self.window_size, :]
        
        return torch.tensor(x, dtype=torch.float32), torch.tensor(y, dtype=torch.float32)

def get_dataloader(csv_file='dataset.csv', batch_size=32, window_size=50, is_dummy=True):
    dataset = IMUDataset(csv_file=csv_file, window_size=window_size, is_dummy=is_dummy)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    return dataloader
