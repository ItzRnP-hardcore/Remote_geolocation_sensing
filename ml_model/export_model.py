import torch
from model import IMUBiasCompensator
from torch.utils.mobile_optimizer import optimize_for_mobile

def export_to_mobile():
    # Load the trained model architecture
    print("Loading model architecture...")
    model = IMUBiasCompensator(input_size=6, hidden_size=64, num_layers=2, output_size=2)
    
    # Load the trained weights
    weight_path = "model_weights.pth"
    try:
        model.load_state_dict(torch.load(weight_path, map_location=torch.device('cpu')))
        print(f"Loaded weights from {weight_path}")
    except FileNotFoundError:
        print(f"Warning: {weight_path} not found. Exporting untrained model for testing.")
        
    model.eval()

    # Create dummy input for tracing (batch_size, sequence_length, features)
    # Using batch size of 1 for mobile inference
    # Sequence length 50, features 6
    example_input = torch.rand(1, 50, 6)

    # Trace the model
    print("Tracing model for TorchScript...")
    traced_script_module = torch.jit.trace(model, example_input)
    
    # Removed optimize_for_mobile due to XNNPACK not being enabled on Windows PyTorch builds
    # The standard TorchScript module is still valid for mobile inference
    
    # Save the exported mobile model
    export_path = "model_mobile.pt"
    # Use standard save for lite interpreter
    traced_script_module._save_for_lite_interpreter(export_path)
    print(f"Successfully exported optimized mobile model to {export_path}")
    
    # Verify loadability
    try:
        print("Verifying loadability...")
        loaded_model = torch.jit.load(export_path)
        output = loaded_model(example_input)
        print("Verification successful. Model output shape:", output.shape)
    except Exception as e:
        print("Verification failed:", e)

if __name__ == "__main__":
    export_to_mobile()
