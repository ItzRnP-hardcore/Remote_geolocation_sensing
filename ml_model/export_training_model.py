"""Export the trained TCN to a PyTorch Lite module with on-device training support.

This creates a wrapper around TCNModel that includes methods for `forward_train`,
`backward`, and `step`, allowing Android to update the weights using its own
collected ground-truth (e.g. GPS data).

Run:  python -m ml_model.export_training_model --weights ml_model/model_tcn_finetuned.pth \
          --frame earth --out app/src/main/assets/model_mobile_train.pt
"""

from __future__ import annotations

import argparse
import os
import sys

import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tcn_model import IN_CHANNELS, WINDOW_SAMPLES, TCNModel, LOGVAR_MIN, LOGVAR_MAX  # noqa: E402
from export_model import DEFAULT_WEIGHTS, DEFAULT_EXPORT, infer_geometry

DEFAULT_TRAIN_EXPORT = os.path.join("ml_model", "model_mobile_train.pt")

class TCNTrainingWrapper(torch.nn.Module):
    def __init__(self, model: TCNModel, lr: float = 1e-4):
        super().__init__()
        self.model = model
        self.lr = lr
        
        # We put all parameters in a list because torch.jit.script doesn't 
        # support iterating over generators like self.parameters()
        self.params = []
        for p in self.model.parameters():
            self.params.append(p)

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Standard inference forward pass. Expected input (B, L, C) from Android."""
        out = self.model(x.transpose(1, 2))
        return (out["mu"], out["logvar"], out["stationary_logit"], out["yaw_rate"])

    @torch.jit.export
    def forward_train(self, x: torch.Tensor, target_disp: torch.Tensor, target_stat: torch.Tensor, target_yaw: torch.Tensor) -> torch.Tensor:
        """
        Runs forward pass and computes a simplified training loss.
        Inputs:
            x: (B, L, C) IMU data
            target_disp: (B,) ground truth displacement (e.g., from GPS speed)
            target_stat: (B,) ground truth stationary flag (0.0 or 1.0)
            target_yaw: (B,) ground truth yaw rate (e.g., from GPS heading)
        Returns:
            loss: Scalar tensor
        """
        out = self.model(x.transpose(1, 2))
        
        # Displacement Loss (Gaussian NLL)
        mu = out["mu"]
        logvar = out["logvar"].clamp(-6.0, 4.0)
        inv_var = torch.exp(-logvar)
        l_disp = 0.5 * (logvar + (target_disp - mu) ** 2 * inv_var).mean()
        
        # Stationary Loss (BCE)
        l_stat = F.binary_cross_entropy_with_logits(out["stationary_logit"], target_stat)
        
        # Yaw Rate Loss (MAE) - mask out NaNs if any
        valid_yaw = torch.isfinite(target_yaw)
        if valid_yaw.any():
            l_yaw = (out["yaw_rate"][valid_yaw] - target_yaw[valid_yaw]).abs().mean()
        else:
            l_yaw = out["yaw_rate"].sum() * 0.0 # dummy
            
        # Total loss (simplified weights)
        total_loss = l_disp + 0.5 * l_stat + 0.2 * l_yaw
        return total_loss

    @torch.jit.export
    def backward(self, loss: torch.Tensor):
        """Computes gradients for the given loss."""
        loss.backward()

    @torch.jit.export
    def step(self):
        """Performs a single SGD optimization step."""
        for p in self.params:
            if p.grad is not None:
                p.data.add_(p.grad, alpha=-self.lr)
                p.grad.zero_()
                
    @torch.jit.export
    def zero_grad(self):
        for p in self.params:
            if p.grad is not None:
                p.grad.zero_()

    @torch.jit.export
    def get_weights(self) -> list[torch.Tensor]:
        """Returns a list of the model's parameters for serialization."""
        return self.params

    @torch.jit.export
    def set_weights(self, new_weights: list[torch.Tensor]):
        """Overwrites the model's parameters with a new list of tensors."""
        if len(new_weights) != len(self.params):
            raise RuntimeError("Length of new_weights does not match model parameters.")
        for i in range(len(self.params)):
            self.params[i].data.copy_(new_weights[i])

def export(weights_path: str, export_path: str) -> None:
    if not os.path.exists(weights_path):
        raise FileNotFoundError(f"{weights_path} not found.")

    state = torch.load(weights_path, map_location="cpu", weights_only=True)
    geom = infer_geometry(state)
    if geom is None:
        raise RuntimeError("No TCN blocks found.")
    stem, channels, dilations = geom
    
    print(f"checkpoint geometry: stem {stem}, channels {channels}, dilations {dilations}")

    model = TCNModel(stem_width=stem, channels=channels, dilations=dilations)
    model.load_state_dict(state)
    # Important: Do NOT call model.eval() here.
    # The app will call training, which expects layers like BatchNorm to behave correctly.
    # For now, we leave it in train mode, though for a single window inference
    # batchnorm in train mode on a batch size of 1 will fail. 
    # TCNBlock has BatchNorm1d. We should freeze BatchNorm stats to avoid batch size 1 issues.
    model.eval() 
    # BUT wait, if we freeze BatchNorm stats by calling eval(), gradients will still flow.
    # This is standard practice for fine-tuning on small batches.

    wrapper = TCNTrainingWrapper(model)

    print("Scripting model for TorchScript training...")
    scripted = torch.jit.script(wrapper)
    
    os.makedirs(os.path.dirname(export_path) or ".", exist_ok=True)
    scripted._save_for_lite_interpreter(export_path)
    
    size_mb = os.path.getsize(export_path) / 1e6
    print(f"Wrote training-ready {export_path} ({size_mb:.1f} MB)")
    
    # Verify export loads
    loaded = torch.jit.load(export_path, map_location="cpu")
    print("Verification passed: training model loaded successfully.")

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--out", default=DEFAULT_TRAIN_EXPORT)
    ap.add_argument("--frame", choices=("earth", "vehicle"), required=True,
                    help="the frame the CHECKPOINT was trained in, from its dataset meta")
    ap.add_argument("--i-know-the-app-feeds-earth", action="store_true")
    args = ap.parse_args(argv)

    if args.frame == "vehicle" and not args.i_know_the_app_feeds_earth:
        print("REFUSING TO EXPORT vehicle frame.", file=sys.stderr)
        return 2

    export(args.weights, args.out)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
