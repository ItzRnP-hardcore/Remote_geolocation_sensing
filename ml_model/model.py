import torch
import torch.nn as nn

class IMUBiasCompensator(nn.Module):
    def __init__(self, input_size=8, hidden_size=64, num_layers=2, output_size=2):
        """
        LSTM-based model to learn and compensate for Bias Instability.
        
        Args:
            input_size (int): Number of features (e.g., Earth-Accel x3, Gyro x3, Speed x1, Bearing x1).
            hidden_size (int): Number of hidden units in LSTM.
            num_layers (int): Number of LSTM layers.
            output_size (int): Number of target outputs (delta_v, delta_theta).
        """
        super(IMUBiasCompensator, self).__init__()
        
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        
        # LSTM layer to model temporal dependencies and drifting bias
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
        
        # Fully connected layer for the final prediction
        self.fc = nn.Linear(hidden_size, output_size)
        
    def forward(self, x):
        # Initialize hidden state and cell state
        h0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size).to(x.device)
        c0 = torch.zeros(self.num_layers, x.size(0), self.hidden_size).to(x.device)
        
        # Forward propagate LSTM
        # out: tensor of shape (batch_size, seq_length, hidden_size)
        out, _ = self.lstm(x, (h0, c0))
        
        # Decode the hidden state of the last time step
        # out[:, -1, :] takes the output of the last sequence step
        out = self.fc(out[:, -1, :])
        
        return out
