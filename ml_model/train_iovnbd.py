"""Train the speed model on IO-VNBD using the TCN model and multitask loss.

Run:  python -m ml_model.train_iovnbd --epochs 40
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tcn_model import TCNModel
from losses import multitask_loss, compute_pos_weight

DT = 0.1              # 10 Hz
STRIDE = 50           # samples between consecutive window ends; see build_dataset_iovnbd
FWD, LAT = 0, 1       # vehicle-frame accelerometer channels


def augment(raw, kinds, gen):
    x = raw
    if "rot" in kinds:
        b = x.shape[0]
        th = torch.rand(b, generator=gen) * (2 * math.pi)
        c, s_ = torch.cos(th)[:, None], torch.sin(th)[:, None]
        x = x.clone()
        for f, r in ((0, 1), (3, 4)):          # accel fwd/right, then gyro fwd/right
            xf, xr = x[:, f, :].clone(), x[:, r, :].clone()
            x[:, f, :] = c * xf - s_ * xr
            x[:, r, :] = s_ * xf + c * xr
    if "gain" in kinds:
        g = 1.0 + 0.1 * torch.randn(x.shape[0], 1, 1, generator=gen)
        x = x * g
    if "noise" in kinds:
        sd = x.std(dim=(0, 2), keepdim=True)
        x = x + 0.05 * sd * torch.randn(x.shape, generator=gen)
    return x


def build_pairs(run_ids: np.ndarray) -> np.ndarray:
    """Index of the next window in the same run, or -1 where there is none."""
    nxt = np.full(len(run_ids), -1, dtype=np.int64)
    same = run_ids[:-1] == run_ids[1:]
    nxt[:-1][same] = np.arange(1, len(run_ids))[same]
    return nxt


def evaluate(model, Xs, Xr, Y, runs, mean_speed, batch=256):
    model.eval()
    mus, yaws, lvs = [], [], []
    with torch.no_grad():
        for i in range(0, len(Xs), batch):
            o = model(Xs[i:i + batch])
            mus.append(o["mu"]); yaws.append(o["yaw_rate"]); lvs.append(o["logvar"])
    mu = torch.cat(mus); yaw = torch.cat(yaws); lv = torch.cat(lvs)
    sp, yr = Y[:, 0], Y[:, 2]
    err = mu - sp
    return {
        "speed_rmse": float(torch.sqrt((err ** 2).mean())),
        "speed_mae": float(err.abs().mean()),
        "speed_bias": float(err.mean()),
        "speed_corr": float(np.corrcoef(mu.numpy(), sp.numpy())[0, 1]),
        "constant_rmse": float(torch.sqrt(((sp - mean_speed) ** 2).mean())),
        "yaw_rmse": float(torch.sqrt(((yaw - yr) ** 2).mean())),
        "yaw_corr": float(np.corrcoef(yaw.numpy(), yr.numpy())[0, 1]),
        "mean_sigma": float(torch.exp(0.5 * lv).mean()),
        "shrinkage": float(mu.std() / sp.std()) if float(sp.std()) > 0 else float("nan"),
    }


def train_variant(name, weights, data, epochs, lr, seed, out_dir,
                  tag="", widths=(64, 64, 64, 64, 64, 64), dropout=0.0,
                  weight_decay=0.0, augment_kinds=()):
    Xs_tr, Xr_tr, Y_tr, nxt_tr, runs_tr = data["train"]
    W_tr = data.get("train_w")
    Xs_va, Xr_va, Y_va, _, runs_va = data["val"]
    Xs_te, Xr_te, Y_te, _, runs_te = data["test"]
    mean_speed = data["mean_speed"]

    torch.manual_seed(seed)
    np.random.seed(seed)
    
    # We use TCNModel with proper dilations for 100 samples
    dilations = (1, 2, 4, 8, 16, 32)
    model = TCNModel(channels=tuple(widths), dilations=dilations)
    
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    n = len(Xs_tr)
    gen = torch.Generator().manual_seed(seed + 977)
    best = (math.inf, None, -1)
    history = []
    t0 = time.time()

    pos_weight = compute_pos_weight(Y_tr[:, 1])

    for ep in range(epochs):
        model.train()
        perm = torch.randperm(n)
        tot = 0.0
        steps = 0
        for i in range(0, n - 1, 64):
            idx = perm[i:i + 64]
            yb = Y_tr[idx]
            wb = W_tr[idx] if W_tr is not None else None
            if augment_kinds:
                raw = augment(Xr_tr[idx], augment_kinds, gen)
                xb = (raw - data["norm_mean"]) / data["norm_sd"]
            else:
                raw, xb = Xr_tr[idx], Xs_tr[idx]
                
            out = model(xb)
            
            targets = {
                "displacement": yb[:, 0],
                "is_stationary": yb[:, 1],
                "yaw_rate": yb[:, 2],
                "session_id": runs_tr[idx],
                "gyro_yaw": raw[:, 5, :]  # Channel 5 is gyro_z (yaw)
            }
            if wb is not None:
                targets["weight"] = wb
                
            losses = multitask_loss(out, targets, pos_weight=pos_weight, weights=weights)
            loss = losses["total"]
            
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            
            tot += loss.item()
            steps += 1
            
        sched.step()

        va = evaluate(model, Xs_va, Xr_va, Y_va, runs_va, mean_speed)
        history.append({"epoch": ep, "loss": tot / steps, "val_rmse": va["speed_rmse"]})
        if va["speed_rmse"] < best[0]:
            best = (va["speed_rmse"], {k: v.clone() for k, v in model.state_dict().items()}, ep)
        if ep % 5 == 0 or ep == epochs - 1:
            print(f"  [{name}] epoch {ep:>3}  loss {tot/steps:>8.4f}  "
                  f"val speed RMSE {va['speed_rmse']:>6.3f}")

    model.load_state_dict(best[1])
    te = evaluate(model, Xs_te, Xr_te, Y_te, runs_te, mean_speed)
    va = evaluate(model, Xs_va, Xr_va, Y_va, runs_va, mean_speed)
    tr = evaluate(model, Xs_tr, Xr_tr, Y_tr, runs_tr, mean_speed)
    torch.save(model.state_dict(), os.path.join(out_dir, f"model_{name}{tag}.pth"))
    return {"variant": name, "best_epoch": best[2], "minutes": (time.time() - t0) / 60,
            "params": sum(p.numel() for p in model.parameters()),
            "train": tr, "val": va, "test": te, "history": history}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.path.join("ml_model", "dataset_iovnbd.pt"))
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--widths", default="64,64,64,64,64,64",
                    help="channel widths per stage")
    ap.add_argument("--dropout", type=float, default=0.0)
    ap.add_argument("--weight-decay", type=float, default=0.0)
    ap.add_argument("--weight-by-quality", action="store_true",
                    help="weight each window by its run's frame-estimate quality")
    ap.add_argument("--augment", default="",
                    help="comma-separated: rot, gain, noise")
    # The multitask weights are the main thing worth sweeping now that the loss has six
    # terms rather than one. Left hardcoded they cannot be ablated, and `nhc` in
    # particular needs ablating: nhc_penalty is w * mu^2 * mean(sin^2(psi)), whose only
    # gradient path is to shrink mu, so it acts as an L2 penalty on predicted speed.
    ap.add_argument("--w-nhc", type=float, default=None)
    ap.add_argument("--w-physics", type=float, default=None)
    ap.add_argument("--w-smooth", type=float, default=None)
    ap.add_argument("--out", default="ml_model")
    ap.add_argument("--only", default="",
                    help="comma-separated variant names to run (default: all)")
    ap.add_argument("--tag", default="", help="suffix for output filenames")
    args = ap.parse_args(argv)

    d = torch.load(args.data, weights_only=False)
    X = d["windows"]; Y = d["targets"]; split = d["split"].numpy()
    runs = d["run_ids"].numpy()
    runs_tensor = torch.tensor(runs)
    print(f"dataset {tuple(X.shape)}  frame={d['frame']}  runs={len(d['run_names'])}")

    tr = split == 0
    mu_c = X[tr].mean(dim=(0, 2), keepdim=True)
    sd_c = X[tr].std(dim=(0, 2), keepdim=True).clamp_min(1e-6)
    Xs = (X - mu_c) / sd_c

    nxt_all = build_pairs(runs)
    packs = {}
    for key, code in (("train", 0), ("val", 1), ("test", 2)):
        m = split == code
        idx = np.flatnonzero(m)
        remap = -np.ones(len(runs), dtype=np.int64)
        remap[idx] = np.arange(len(idx))
        nxt = nxt_all[idx]
        nxt = np.where(nxt >= 0, remap[np.clip(nxt, 0, None)], -1)
        packs[key] = (Xs[m], X[m], Y[m], torch.tensor(nxt), runs_tensor[m])
    packs["mean_speed"] = Y[tr][:, 0].mean()
    if args.weight_by_quality and "weights" in d:
        packs["train_w"] = d["weights"][tr]
    packs["norm_mean"] = mu_c
    packs["norm_sd"] = sd_c
    print(f"train {len(packs['train'][0])}  val {len(packs['val'][0])}  "
          f"test {len(packs['test'][0])}   constant-baseline speed "
          f"{float(packs['mean_speed']):.2f} m/s")

    variants = [
        ("tcn_base", {"nhc": 0.0, "physics": 0.0, "smoothness": 0.0}),
        ("tcn_nhc", {"nhc": 0.2, "physics": 0.3, "smoothness": 0.1})
    ]
    over = {k: v for k, v in (("nhc", args.w_nhc), ("physics", args.w_physics),
                              ("smoothness", args.w_smooth)) if v is not None}
    if over:
        variants.append(("tcn_custom",
                         {"nhc": 0.0, "physics": 0.0, "smoothness": 0.0, **over}))
    if args.only:
        keep = {x.strip() for x in args.only.split(",") if x.strip()}
        variants = [v for v in variants if v[0] in keep]
        
    results = []
    for name, weights in variants:
        print(f"--- {name} (weights={weights}) ---")
        widths = tuple(int(x) for x in args.widths.split(","))
        results.append(train_variant(name, weights, packs, args.epochs, args.lr,
                                     args.seed, args.out, args.tag, widths,
                                     args.dropout, args.weight_decay,
                                     tuple(x.strip() for x in args.augment.split(",")
                                           if x.strip())))
        print()

    print(f"{'variant':<14}{'params':>10}{'train':>8}{'val':>8}{'test':>8}{'gap x':>7}"
          f"{'const':>8}{'corr':>7}{'shrink':>8}{'yaw r':>7}{'min':>6}")
    print("-" * 95)
    for r in results:
        t, tr_ = r["test"], r.get("train", {})
        gap = t["speed_rmse"] / tr_["speed_rmse"] if tr_.get("speed_rmse") else float("nan")
        print(f"{r['variant']:<14}{r.get('params', 0):>10,}{tr_.get('speed_rmse', float('nan')):>8.3f}"
              f"{r['val']['speed_rmse']:>8.3f}{t['speed_rmse']:>8.3f}{gap:>7.1f}"
              f"{t['constant_rmse']:>8.3f}{t['speed_corr']:>7.3f}"
              f"{t.get('shrinkage', float('nan')):>8.3f}{t['yaw_corr']:>7.3f}{r['minutes']:>6.1f}")

    path = os.path.join(args.out, f"train_iovnbd_results{args.tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"args": vars(args), "results": results,
                   "norm_mean": mu_c.squeeze().tolist(),
                   "norm_sd": sd_c.squeeze().tolist()}, fh, indent=1)
    print(f"results -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
