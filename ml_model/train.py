import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import TensorDataset, DataLoader
from resnet1d import ResNet1D
import time

def train():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # Load data
    print("Loading dataset...")
    x, y = torch.load("dataset.pt", weights_only=True)
    # x shape: (N, 6, 100), y shape: (N, 3) (speed_mps, stationary, yaw_rate)
    
    # We want to predict distance over next second. For 10Hz, speed_mps * 1s is roughly displacement.
    # The network predicts logvar for Gaussian NLL, but let's just use standard MSE for the mean
    # and BCE for stationary.
    
    dataset = TensorDataset(x, y)
    train_size = int(0.8 * len(dataset))
    val_size = len(dataset) - train_size
    train_dataset, val_dataset = torch.utils.data.random_split(dataset, [train_size, val_size])
    
    train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=32)

    model = ResNet1D(in_channels=6).to(device)
    optimizer = optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    
    # Loss functions
    mse_loss = nn.MSELoss()
    bce_loss = nn.BCEWithLogitsLoss()

    epochs = 20
    best_val_loss = float('inf')

    print("Starting training...")
    for epoch in range(epochs):
        model.train()
        train_loss = 0.0
        
        for batch_x, batch_y in train_loader:
            batch_x, batch_y = batch_x.to(device), batch_y.to(device)
            
            optimizer.zero_grad()
            
            out = model(batch_x)
            mu = out["mu"]
            stat_logit = out["stationary_logit"]
            
            # True values
            true_mu = batch_y[:, 0]
            true_stat = batch_y[:, 1]
            
            # Losses
            loss_mu = mse_loss(mu, true_mu)
            loss_stat = bce_loss(stat_logit, true_stat)
            
            loss = loss_mu + loss_stat
            loss.backward()
            optimizer.step()
            
            train_loss += loss.item() * batch_x.size(0)
            
        train_loss /= len(train_loader.dataset)
        
        # Validation
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for batch_x, batch_y in val_loader:
                batch_x, batch_y = batch_x.to(device), batch_y.to(device)
                out = model(batch_x)
                mu = out["mu"]
                stat_logit = out["stationary_logit"]
                
                true_mu = batch_y[:, 0]
                true_stat = batch_y[:, 1]
                
                loss_mu = mse_loss(mu, true_mu)
                loss_stat = bce_loss(stat_logit, true_stat)
                loss = loss_mu + loss_stat
                
                val_loss += loss.item() * batch_x.size(0)
                
        val_loss /= len(val_loader.dataset)
        
        print(f"Epoch {epoch+1:02d}/{epochs:02d} | Train Loss: {train_loss:.4f} | Val Loss: {val_loss:.4f}")
        
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save(model.state_dict(), "model_weights.pth")
            
    print("Training complete! Best model saved to model_weights.pth")

if __name__ == "__main__":
    train()
