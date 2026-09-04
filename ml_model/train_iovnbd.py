"""Train the speed model on IO-VNBD, and settle the physics-informed question by measurement.

The execution plan calls for "a multi-objective loss combining standard MSE against
the ground-truth speed and a physics-informed penalty enforcing the kinematic
constraint (v_t = v_{t-1} + a_t * dt)". This trains three variants so that choice is
decided by held-out error rather than by argument:

  data          Gaussian NLL on speed using the mu and logvar heads, plus BCE on the
                stationary head and MSE on the yaw-rate head. No physics.
  kinematic     data + the constraint the plan names. Consecutive windows are 5 s
                apart, so the predicted speeds must differ by the integral of forward
                acceleration between them.
  centripetal   data + a_lat = v * omega, which couples the speed head to the yaw
                head through a physical identity.

Why the third one exists. The kinematic constraint is the weaker of the two, because
the integrator already satisfies v_t = v_{t-1} + a_t*dt exactly, in closed form - a
network penalised for violating it converges toward reproducing the integrator,
including the -0.0195 m/s^2 along-track bias that eval/dr_diagnostics.py measured. A
law cannot correct an error that already obeys it.

The centripetal identity is different: it constrains two heads against each other
using only the input signal, so it says something the data loss does not. It holds
almost exactly in this dataset - regressing the vehicle's own lateral accelerometer
on v*omega gives a slope of 1.008 with r = 0.941, which is as clean a physical
identity as measured data offers.

It is applied to the PHONE's lateral channel, not the vehicle's, deliberately. On a
real handset there is no vehicle accelerometer, so a constraint built on one could
never ship. The phone channel recovers a slope of only 0.51 (r = 0.43), because the
device-to-vehicle mounting rotation is estimated rather than known - so this measures
the constraint as it would actually be available on-device, attenuation included.

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
from resnet1d import ResNet1D  # noqa: E402

DT = 0.1              # 10 Hz
STRIDE = 50           # samples between consecutive window ends; see build_dataset_iovnbd
FWD, LAT = 0, 1       # vehicle-frame accelerometer channels


def gaussian_nll(mu, logvar, target):
    """Negative log-likelihood with the model's own predicted variance.

    This is what the logvar head is for: a bare MSE would make it dead weight, and the
    variance is what gates the fusion gain in IMUModelRunner.fusionWeight.
    """
    return 0.5 * (logvar + (target - mu) ** 2 / torch.exp(logvar)).mean()


def data_loss(out, y):
    speed = gaussian_nll(out["mu"], out["logvar"], y[:, 0])
    stationary = F.binary_cross_entropy_with_logits(out["stationary_logit"], y[:, 1])
    yaw = F.mse_loss(out["yaw_rate"], y[:, 2])
    return speed + stationary + yaw, speed.item(), yaw.item()


def kinematic_residual(mu_a, mu_b, raw_b):
    """|v_b - v_a - integral of forward acceleration between the window ends|.

    The two windows are STRIDE samples apart, so the relevant acceleration is the last
    STRIDE samples of the later window. Forward is horizontal by construction, so
    gravity contributes nothing to it.
    """
    dv = raw_b[:, FWD, -STRIDE:].sum(dim=1) * DT
    return (mu_b - mu_a - dv) ** 2


def centripetal_residual(mu, yaw, raw):
    """|a_lat - v * omega| at the window end, using the phone's own lateral channel."""
    return (raw[:, LAT, -1] - mu * yaw) ** 2


def build_pairs(run_ids: np.ndarray) -> np.ndarray:
    """Index of the next window in the same run, or -1 where there is none."""
    nxt = np.full(len(run_ids), -1, dtype=np.int64)
    same = run_ids[:-1] == run_ids[1:]
    nxt[:-1][same] = np.arange(1, len(run_ids))[same]
    return nxt


def evaluate(model, Xs, Xr, Y, mean_speed, batch=256):
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
        # The bar from eval/model_speed_eval.py: beat predicting a constant.
        "constant_rmse": float(torch.sqrt(((sp - mean_speed) ** 2).mean())),
        "yaw_rmse": float(torch.sqrt(((yaw - yr) ** 2).mean())),
        "yaw_corr": float(np.corrcoef(yaw.numpy(), yr.numpy())[0, 1]),
        "mean_sigma": float(torch.exp(0.5 * lv).mean()),
        "centripetal_resid": float(centripetal_residual(mu, yaw, Xr).mean()),
    }


def train_variant(name, weight_kin, weight_cen, data, epochs, lr, seed, out_dir,
                  tag="", widths=(64, 128, 256, 512), blocks=2, dropout=0.0):
    Xs_tr, Xr_tr, Y_tr, nxt_tr = data["train"]
    Xs_va, Xr_va, Y_va, _ = data["val"]
    Xs_te, Xr_te, Y_te, _ = data["test"]
    mean_speed = data["mean_speed"]

    torch.manual_seed(seed)
    np.random.seed(seed)
    model = ResNet1D(widths=tuple(widths), blocks_per_stage=blocks,
                     dropout=dropout)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    n = len(Xs_tr)
    best = (math.inf, None, -1)
    history = []
    t0 = time.time()

    for ep in range(epochs):
        model.train()
        perm = torch.randperm(n)
        tot = kin_tot = cen_tot = 0.0
        steps = 0
        for i in range(0, n - 1, 64):
            idx = perm[i:i + 64]
            xb, yb = Xs_tr[idx], Y_tr[idx]
            out = model(xb)
            loss, _, _ = data_loss(out, yb)

            kin = torch.tensor(0.0)
            if weight_kin > 0:
                j = nxt_tr[idx]
                ok = j >= 0
                if ok.any():
                    ja = j[ok]
                    on = model(Xs_tr[ja])
                    kin = kinematic_residual(out["mu"][ok], on["mu"], Xr_tr[ja]).mean()
                    loss = loss + weight_kin * kin

            cen = torch.tensor(0.0)
            if weight_cen > 0:
                cen = centripetal_residual(out["mu"], out["yaw_rate"], Xr_tr[idx]).mean()
                loss = loss + weight_cen * cen

            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            tot += loss.item(); kin_tot += float(kin); cen_tot += float(cen); steps += 1
        sched.step()

        va = evaluate(model, Xs_va, Xr_va, Y_va, mean_speed)
        history.append({"epoch": ep, "loss": tot / steps, "kin": kin_tot / steps,
                        "cen": cen_tot / steps, "val_rmse": va["speed_rmse"]})
        if va["speed_rmse"] < best[0]:
            best = (va["speed_rmse"], {k: v.clone() for k, v in model.state_dict().items()}, ep)
        if ep % 5 == 0 or ep == epochs - 1:
            print(f"  [{name}] epoch {ep:>3}  loss {tot/steps:>8.4f}  "
                  f"kin {kin_tot/steps:>7.4f}  cen {cen_tot/steps:>7.4f}  "
                  f"val speed RMSE {va['speed_rmse']:>6.3f}")

    model.load_state_dict(best[1])
    te = evaluate(model, Xs_te, Xr_te, Y_te, mean_speed)
    va = evaluate(model, Xs_va, Xr_va, Y_va, mean_speed)
    torch.save(model.state_dict(), os.path.join(out_dir, f"model_{name}{tag}.pth"))
    return {"variant": name, "best_epoch": best[2], "minutes": (time.time() - t0) / 60,
            "val": va, "test": te, "history": history}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.path.join("ml_model", "dataset_iovnbd.pt"))
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--widths", default="64,128,256,512",
                    help="channel widths per stage; shrink to cut capacity")
    ap.add_argument("--blocks", type=int, default=2)
    ap.add_argument("--dropout", type=float, default=0.0)
    ap.add_argument("--w-kin", type=float, default=0.05)
    ap.add_argument("--w-cen", type=float, default=0.05)
    ap.add_argument("--out", default="ml_model")
    ap.add_argument("--only", default="",
                    help="comma-separated variant names to run (default: all)")
    ap.add_argument("--tag", default="", help="suffix for output filenames")
    ap.add_argument("--sweep", action="store_true",
                    help="sweep both physics weights over two decades")
    args = ap.parse_args(argv)

    d = torch.load(args.data, weights_only=False)
    X = d["windows"]; Y = d["targets"]; split = d["split"].numpy()
    runs = d["run_ids"].numpy()
    print(f"dataset {tuple(X.shape)}  frame={d['frame']}  runs={len(d['run_names'])}")

    # Standardise per channel on the TRAINING split only. The raw tensor is kept
    # alongside, because the physics terms are statements about metres and seconds and
    # would be meaningless in standardised units.
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
        packs[key] = (Xs[m], X[m], Y[m], torch.tensor(nxt))
    packs["mean_speed"] = Y[tr][:, 0].mean()
    print(f"train {len(packs['train'][0])}  val {len(packs['val'][0])}  "
          f"test {len(packs['test'][0])}   constant-baseline speed "
          f"{float(packs['mean_speed']):.2f} m/s")
    print(f"paired windows available in train: "
          f"{int((packs['train'][3] >= 0).sum())} of {len(packs['train'][0])}\n")

    if args.sweep:
        # One weight per physics term proves nothing: a null result could just mean
        # the weight was wrong. Sweeping two decades either side makes "it does not
        # help" a finding rather than an artefact.
        variants = [("data", 0.0, 0.0)]
        for w in (0.01, 0.05, 0.2):
            variants.append((f"kin{w}", w, 0.0))
        for w in (0.01, 0.05, 0.2):
            variants.append((f"cen{w}", 0.0, w))
    else:
        variants = [("data", 0.0, 0.0),
                    ("kinematic", args.w_kin, 0.0),
                    ("centripetal", 0.0, args.w_cen)]
    if args.only:
        keep = {x.strip() for x in args.only.split(",") if x.strip()}
        variants = [v for v in variants if v[0] in keep]
    results = []
    for name, wk, wc in variants:
        print(f"--- {name} (w_kin={wk}, w_cen={wc}) ---")
        widths = tuple(int(x) for x in args.widths.split(","))
        results.append(train_variant(name, wk, wc, packs, args.epochs, args.lr,
                                     args.seed, args.out, args.tag, widths,
                                     args.blocks, args.dropout))
        print()

    print(f"{'variant':<14}{'val RMSE':>10}{'test RMSE':>11}{'const':>9}"
          f"{'test MAE':>10}{'corr':>7}{'yaw RMSE':>10}{'yaw r':>7}{'min':>7}")
    print("-" * 85)
    for r in results:
        t = r["test"]
        print(f"{r['variant']:<14}{r['val']['speed_rmse']:>10.3f}{t['speed_rmse']:>11.3f}"
              f"{t['constant_rmse']:>9.3f}{t['speed_mae']:>10.3f}{t['speed_corr']:>7.3f}"
              f"{t['yaw_rmse']:>10.4f}{t['yaw_corr']:>7.3f}{r['minutes']:>7.1f}")

    base = results[0]["test"]["speed_rmse"]
    print()
    for r in results[1:]:
        d_ = (r["test"]["speed_rmse"] - base) / base * 100
        print(f"{r['variant']:<14} vs data-only: {d_:+.1f}% test speed RMSE")
    print(f"\nconstant-prediction baseline RMSE: {results[0]['test']['constant_rmse']:.3f} m/s")

    path = os.path.join(args.out, f"train_iovnbd_results{args.tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"args": vars(args), "results": results,
                   "norm_mean": mu_c.squeeze().tolist(),
                   "norm_sd": sd_c.squeeze().tolist()}, fh, indent=1)
    print(f"results -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
