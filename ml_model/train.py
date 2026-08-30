import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, random_split
from data_loader import get_dataloader, IMUDataset
from model import IMUBiasCompensator
import os
import copy

def train_model():
    # Hyperparameters
    batch_size = 64
    learning_rate = 0.001
    num_epochs = 100
    window_size = 50
    patience = 10 # Early stopping patience
    
    # Device configuration
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}")
    
    # Initialize Dataset (Use real data once downloaded, or dummy for testing)
    csv_path = '../dataset/IO-VNBD_original/Synchronised V abd S datasets/Categorised IOVNB Dataset/M (Driver B)/S-M.csv'
    is_dummy = not os.path.exists(csv_path)
    
    print(f"Loading data... (Using {'dummy' if is_dummy else 'real'} data)")
    full_dataset = IMUDataset(csv_file=csv_path, window_size=window_size, is_dummy=is_dummy)
    
    # Train/Validation Split (80/20)
    train_size = int(0.8 * len(full_dataset))
    val_size = len(full_dataset) - train_size
    train_dataset, val_dataset = random_split(full_dataset, [train_size, val_size])
    
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)
    
    # Initialize the model
    # Input size is 6 (ax, ay, az, gx, gy, gz)
    # Output size is 2 (e.g., target location/velocity variables to correct)
    model = IMUBiasCompensator(input_size=6, hidden_size=64, num_layers=2, output_size=2).to(device)
    
    # Loss and optimizer
    criterion = nn.MSELoss()  # Mean Squared Error for regression
    optimizer = optim.Adam(model.parameters(), lr=learning_rate)
    
    # Learning rate scheduler
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=5)
    
    print("Starting iterative training...")
    
    best_val_loss = float('inf')
    best_model_weights = copy.deepcopy(model.state_dict())
    epochs_no_improve = 0
    
    for epoch in range(num_epochs):
        # --- Training Phase ---
        model.train()
        train_loss = 0.0
        for features, targets in train_loader:
            features = features.to(device)
            targets = targets.to(device)
            
            optimizer.zero_grad()
            outputs = model(features)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            
            train_loss += loss.item()
            
        avg_train_loss = train_loss / len(train_loader)
        
        # --- Validation Phase ---
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for features, targets in val_loader:
                features = features.to(device)
                targets = targets.to(device)
                
                outputs = model(features)
                loss = criterion(outputs, targets)
                val_loss += loss.item()
                
        avg_val_loss = val_loss / len(val_loader)
        
        # Step the scheduler
        scheduler.step(avg_val_loss)
        
        print(f"Epoch [{epoch+1}/{num_epochs}], Train Loss: {avg_train_loss:.6f}, Val Loss: {avg_val_loss:.6f}")
        
        # Early Stopping and Checkpointing
        if avg_val_loss < best_val_loss:
            best_val_loss = avg_val_loss
            best_model_weights = copy.deepcopy(model.state_dict())
            epochs_no_improve = 0
        else:
            epochs_no_improve += 1
            if epochs_no_improve >= patience:
                print("Early stopping triggered!")
                break
                
    # Save the best model weights
    model.load_state_dict(best_model_weights)
    save_path = 'model_weights.pth'
    torch.save(model.state_dict(), save_path)
    print(f"Training complete. Best Val Loss: {best_val_loss:.6f}")
    print(f"Model saved to {save_path}")

if __name__ == '__main__':
    train_model()
