#!/usr/bin/env python3
"""Fine-tune the Earth-frame TCN model on real phone driving session dataset.

Calibrates the speed mu head, stationary logit, and yaw rate to the actual
sensor characteristics, noise floor, and vibration profile of the phone.
"""

import math
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tcn_model import TCNModel, WINDOW_SAMPLES, IN_CHANNELS
from export_model import TCNWrapper, HEADS


def compute_loss(out, targets, pos_weight=1.0):
    mu = out["mu"].squeeze(-1)
    lv = out["logvar"].squeeze(-1)
    stat = out["stationary_logit"].squeeze(-1)
    yr = out["yaw_rate"].squeeze(-1)

    t_speed = targets[:, 0]
    t_stat = targets[:, 1]
    t_yr = targets[:, 2]

    # Gaussian NLL for speed
    prec = torch.exp(-lv)
    loss_speed = 0.5 * torch.mean(prec * (mu - t_speed) ** 2 + lv)

    # BCE with logits for stationary detection
    loss_stat = F.binary_cross_entropy_with_logits(
        stat, t_stat, pos_weight=torch.tensor(pos_weight, device=stat.device)
    )

    # Smooth L1 for yaw rate
    loss_yaw = F.smooth_l1_loss(yr, t_yr)

    # Stationary consistency: if stationary is 1, speed should be zero
    loss_stat_speed = torch.mean(torch.sigmoid(stat) * (mu ** 2))

    total = loss_speed + 2.0 * loss_stat + 1.0 * loss_yaw + 0.5 * loss_stat_speed
    return {
        "total": total,
        "speed": loss_speed.item(),
        "stat": loss_stat.item(),
        "yaw": loss_yaw.item(),
    }


def evaluate(model, X, Y, batch_size=128):
    model.eval()
    mus, stats, yrs = [], [], []
    with torch.no_grad():
        for i in range(0, len(X), batch_size):
            xb = X[i:i + batch_size]
            out = model(xb)
            mus.append(out["mu"].cpu())
            stats.append(out["stationary_logit"].cpu())
            yrs.append(out["yaw_rate"].cpu())

    mu = torch.cat(mus, dim=0).squeeze(-1)
    stat = torch.cat(stats, dim=0).squeeze(-1)
    yr = torch.cat(yrs, dim=0).squeeze(-1)

    t_speed = Y[:, 0].cpu()
    t_stat = Y[:, 1].cpu()
    t_yr = Y[:, 2].cpu()

    speed_rmse = torch.sqrt(torch.mean((mu - t_speed) ** 2)).item()
    speed_bias = torch.mean(mu - t_speed).item()
    stat_pred = (torch.sigmoid(stat) >= 0.5).float()
    stat_acc = torch.mean((stat_pred == t_stat).float()).item()
    yaw_rmse = torch.sqrt(torch.mean((yr - t_yr) ** 2)).item()

    corr = np.corrcoef(mu.numpy(), t_speed.numpy())[0, 1] if len(t_speed) > 1 else 0.0

    return {
        "speed_rmse": speed_rmse,
        "speed_bias": speed_bias,
        "speed_corr": corr,
        "stat_acc": stat_acc,
        "yaw_rmse": yaw_rmse,
    }


def main():
    print("=" * 70)
    print("FINE-TUNING TCN MODEL ON PHONE DRIVING SESSIONS")
    print("=" * 70)

    data_path = os.path.join("ml_model", "dataset_phone_earth.pt")
    if not os.path.exists(data_path):
        print(f"Error: {data_path} not found. Run build_phone_dataset.py first.")
        return 1

    d = torch.load(data_path, map_location="cpu", weights_only=True)
    W = d["windows"]   # (N, 6, 100)
    Y = d["targets"]   # (N, 4)
    S = d["split"]     # (N)

    # Train / Val / Test splits
    train_mask = (S == 0)
    val_mask = (S == 1)
    test_mask = (S == 2)

    X_train, Y_train = W[train_mask], Y[train_mask]
    X_val, Y_val = W[val_mask], Y[val_mask]
    X_test, Y_test = W[test_mask], Y[test_mask]

    print(f"Dataset: Train={len(X_train)}, Val={len(X_val)}, Test={len(X_test)}")

    # Model architecture
    model = TCNModel(channels=(64, 64, 64, 64, 64, 64), dilations=(1, 2, 4, 8, 16, 32))

    # Load initial weights from IOVNBD wide earth checkpoint
    init_weights = "ml_model/model_tcn_base_wide_earth.pth"
    if not os.path.exists(init_weights):
        init_weights = "ml_model/model_tcn_base_earth_s0.pth"
    if not os.path.exists(init_weights):
        init_weights = "ml_model/model_tcn_base_fx.pth"
    if os.path.exists(init_weights):
        print(f"Loading initial weights from {init_weights}...")
        state = torch.load(init_weights, map_location="cpu", weights_only=True)
        model.load_state_dict(state, strict=False)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Training on device: {device}")
    model.to(device)

    X_train = X_train.to(device)
    Y_train = Y_train.to(device)
    X_val = X_val.to(device)
    Y_val = Y_val.to(device)
    X_test = X_test.to(device)
    Y_test = Y_test.to(device)

    # Initial baseline evaluation
    base_eval = evaluate(model, X_test, Y_test)
    print("\nPre-finetune Test Set Metrics:")
    print(f"  Speed RMSE: {base_eval['speed_rmse']:.3f} m/s, Bias: {base_eval['speed_bias']:+.3f} m/s, Corr: {base_eval['speed_corr']:.3f}")
    print(f"  Stationary Accuracy: {base_eval['stat_acc']*100:.1f}%, Yaw RMSE: {base_eval['yaw_rmse']:.4f} rad/s")

    epochs = 25
    batch_size = 64
    lr = 3e-4
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs, eta_min=1e-5)

    pos_weight = float((Y_train[:, 1] == 0).sum() / max(1, (Y_train[:, 1] == 1).sum()))
    print(f"Stationary class balance pos_weight: {pos_weight:.2f}")

    best_val_rmse = float("inf")
    best_state = None

    print(f"\nTraining for {epochs} epochs...")
    n_train = len(X_train)

    for ep in range(epochs):
        model.train()
        perm = torch.randperm(n_train, device=device)
        total_loss = 0.0
        batches = 0

        for i in range(0, n_train - batch_size + 1, batch_size):
            idx = perm[i:i + batch_size]
            xb = X_train[idx]
            yb = Y_train[idx]

            # Augmentation: slight noise & gain
            if model.training:
                gain = 1.0 + 0.05 * torch.randn(len(xb), 1, 1, device=device)
                noise = 0.02 * torch.randn_like(xb)
                xb = xb * gain + noise

            out = model(xb)
            loss_dict = compute_loss(out, yb, pos_weight=pos_weight)
            loss = loss_dict["total"]

            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()

            total_loss += loss.item()
            batches += 1

        sched.step()

        val_metrics = evaluate(model, X_val, Y_val)
        if val_metrics["speed_rmse"] < best_val_rmse:
            best_val_rmse = val_metrics["speed_rmse"]
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

        if ep % 5 == 0 or ep == epochs - 1:
            print(f"  Epoch {ep:>2}/{epochs} | Loss: {total_loss/batches:.4f} | "
                  f"Val Speed RMSE: {val_metrics['speed_rmse']:.3f} m/s (bias {val_metrics['speed_bias']:+.2f}), "
                  f"Stat Acc: {val_metrics['stat_acc']*100:.1f}%")

    # Load best model
    if best_state is not None:
        model.load_state_dict(best_state)

    final_test = evaluate(model, X_test, Y_test)
    print("\n" + "=" * 70)
    print("FINAL FINE-TUNED TEST EVALUATION")
    print("=" * 70)
    print(f"  Speed RMSE: {final_test['speed_rmse']:.3f} m/s (was {base_eval['speed_rmse']:.3f} m/s)")
    print(f"  Speed Bias: {final_test['speed_bias']:+.3f} m/s (was {base_eval['speed_bias']:+.3f} m/s)")
    print(f"  Speed Correlation r: {final_test['speed_corr']:.3f} (was {base_eval['speed_corr']:.3f})")
    print(f"  Stationary Accuracy: {final_test['stat_acc']*100:.1f}%")
    print(f"  Yaw RMSE: {final_test['yaw_rmse']:.4f} rad/s")

    # Save fine-tuned checkpoint
    out_pth = "ml_model/model_tcn_finetuned.pth"
    torch.save(model.state_dict(), out_pth)
    print(f"\nSaved fine-tuned weights to {out_pth}")

    # Export to TorchScript Lite module for Android
    print("Exporting TorchScript Lite module for Android...")
    model.cpu().eval()
    wrapper = TCNWrapper(model).eval()
    example = torch.randn(1, WINDOW_SAMPLES, IN_CHANNELS)

    traced = torch.jit.trace(wrapper, example, strict=False)

    for export_path in ["app/src/main/assets/model_mobile.pt", "ml_model/model_mobile.pt"]:
        os.makedirs(os.path.dirname(export_path), exist_ok=True)
        traced._save_for_lite_interpreter(export_path)
        print(f"  Exported -> {export_path} ({os.path.getsize(export_path)/1e3:.1f} KB)")

    print("\nFine-tuning and model export completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
