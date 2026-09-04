"""Multi-task loss for the displacement model.

    total = 1.0 * gaussian_nll(displacement)
          + 0.5 * bce(stationary)
          + 0.2 * mae(yaw_rate, valid windows only)
          + 0.1 * smoothness(|mu_t - mu_{t-1}| within a session)

Two things here are load-bearing rather than cosmetic:

* **`pos_weight` on the stationary BCE.** Stationary windows are only 3.3-6.2%
  of each split. Unweighted BCE is minimised by predicting "moving" everywhere,
  which scores ~95% accuracy and learns nothing. The weight is the measured
  negative/positive ratio.
* **The smoothness term is session-scoped.** Consecutive windows overlap by 9 s
  and are 1 s apart, so their displacements really are near-continuous — but
  only within one session. Applied across a session boundary it would penalise
  a discontinuity that physically exists.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from tcn_model import LOGVAR_MAX, LOGVAR_MIN

W_DISPLACEMENT = 1.0
W_STATIONARY = 0.5
W_PHYSICS = 0.3
W_YAW = 0.2
W_SMOOTHNESS = 0.1
# Non-holonomic lateral-velocity penalty. Deliberately small: a soft nudge,
# not a hard constraint.
W_NHC = 0.05

# Sample interval of the per-timestep sequences. The window is 10 Hz, so
# dt = 0.1 s. Using dt = 1.0 would state that consecutive samples are a second
# apart and inflate the acceleration term tenfold.
PHYSICS_DT = 0.1


def gaussian_nll(mu: torch.Tensor, logvar: torch.Tensor,
                 target: torch.Tensor,
                 weights: torch.Tensor | None = None) -> torch.Tensor:
    """Negative log-likelihood of a Gaussian with predicted variance.

    0.5 * (logvar + (y - mu)^2 / exp(logvar)), constant dropped. The model can
    down-weight windows it cannot predict, but `logvar` is clamped so it cannot
    escape the loss entirely by declaring everything uncertain.
    """
    logvar = logvar.clamp(LOGVAR_MIN, LOGVAR_MAX)
    inv_var = torch.exp(-logvar)
    per_sample = 0.5 * (logvar + (target - mu) ** 2 * inv_var)
    if weights is None:
        return per_sample.mean()
    # Weighted mean, normalised by the weights so the loss scale — and hence
    # the balance against the other three task terms — does not shift.
    w = weights.to(per_sample.dtype)
    return (per_sample * w).sum() / w.sum().clamp_min(1e-8)


def stationary_bce(logit: torch.Tensor, target: torch.Tensor,
                   pos_weight: torch.Tensor | float | None = None
                   ) -> torch.Tensor:
    """BCE for the stationary head, with positive-class up-weighting."""
    target = target.float()
    if pos_weight is None:
        pw = None
    elif isinstance(pos_weight, torch.Tensor):
        pw = pos_weight.to(logit.device, logit.dtype)
    else:
        pw = torch.as_tensor(float(pos_weight), device=logit.device,
                             dtype=logit.dtype)
    return F.binary_cross_entropy_with_logits(logit, target, pos_weight=pw)


def compute_pos_weight(is_stationary) -> float:
    """negatives / positives, the standard BCE positive-class weight.

    Returns 1.0 if a batch happens to contain no positives, so a single
    unlucky batch cannot produce an infinite weight.
    """
    t = torch.as_tensor(is_stationary).float()
    pos = float(t.sum())
    neg = float(t.numel() - pos)
    return neg / pos if pos > 0 else 1.0


def physics_penalty(v_pred_seq: torch.Tensor, a_pred_seq: torch.Tensor,
                    dt: float = PHYSICS_DT) -> torch.Tensor:
    """Kinematic consistency: v[t+1] should equal v[t] + a[t]*dt.

    IMPORTANT -- why acceleration is a SEPARATE head. If `a_pred_seq` were the
    finite difference of `v_pred_seq`, this residual would be identically zero
    by construction:

        v[1:] - (v[:-1] + ((v[1:] - v[:-1]) / dt) * dt) == 0

    and the term would silently contribute nothing to the loss. The penalty
    only carries information when velocity and acceleration are predicted
    INDEPENDENTLY, so it constrains two separate outputs to agree with
    kinematics. `TCNModel` therefore exposes `v_seq` and `a_seq` as distinct
    heads, and this is asserted in tests.
    """
    if v_pred_seq.shape[-1] < 2:
        return v_pred_seq.sum() * 0.0
    residual = v_pred_seq[:, 1:] - (v_pred_seq[:, :-1] + a_pred_seq[:, :-1] * dt)
    return (residual ** 2).mean()


def nhc_penalty(v_pred: torch.Tensor, gyro_yaw_rate: torch.Tensor,
                dt: float = PHYSICS_DT) -> torch.Tensor:
    """Non-holonomic lateral-velocity penalty, applied during TRAINING.

    Accumulates heading drift across the window from the RAW gyro (not the
    yaw-correction head, which is disabled in the final configuration), then
    penalises the implied lateral velocity component:

        psi_drift = cumsum(omega * dt)
        v_lat     = v * sin(psi_drift)

    A road vehicle cannot move sideways, so v_lat should be ~0.

    dt NOTE: the spec suggested dt=1.0, but the window is sampled at 10 Hz, so
    consecutive samples are 0.1 s apart. With dt=1.0 the accumulated psi_drift
    is 10x too large and runs through the nonlinearity of sin() -- for a 3 s
    window at a modest 0.1 rad/s that is 3 rad rather than 0.3 rad, i.e. past
    where sin is even monotone. PHYSICS_DT (0.1 s) is used; `dt` is exposed so
    the alternative can be tested.

    PRE-REGISTERED PREDICTION (see findings.md): psi_drift from the raw gyro
    conflates genuine heading change with the phone's fixed but unidentified
    mount rotation -- the same ambiguity that defeated the inference-time ESKF.
    The penalty should therefore fail to distinguish real turns from
    mount-induced yaw, and instead teach the model to suppress speed whenever
    the yaw rate is nonzero. Expected signature: WORSE under-prediction during
    genuine cornering, with no drift improvement.
    """
    if gyro_yaw_rate.dim() != 2:
        raise ValueError(f"gyro_yaw_rate must be (B, T), got "
                         f"{tuple(gyro_yaw_rate.shape)}")
    psi_drift = torch.cumsum(gyro_yaw_rate * dt, dim=1)
    v_lat = v_pred.unsqueeze(1) * torch.sin(psi_drift)
    return (v_lat ** 2).mean()


def yaw_rate_mae(pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    """MAE over valid windows only.

    The true yaw rate is NaN below 5 m/s, where GPS heading is meaningless.
    Those windows are dropped rather than filled: a zero fill would teach the
    model that slow driving means no rotation.
    """
    valid = torch.isfinite(target)
    if not bool(valid.any()):
        return pred.sum() * 0.0        # keeps the graph, contributes nothing
    return (pred[valid] - target[valid]).abs().mean()


def smoothness(mu: torch.Tensor, session_id: torch.Tensor,
               t0: torch.Tensor | None = None,
               max_gap_s: float = 1.5) -> torch.Tensor:
    """Mean |mu_t - mu_{t-1}| over consecutive windows of the SAME session.

    Requires the batch to be in session/time order. Pairs spanning a session
    change are dropped; if `t0` is given, so are pairs separated by more than
    `max_gap_s`, which would otherwise link windows across a gap where the
    displacement genuinely jumps.
    """
    if mu.numel() < 2:
        return mu.sum() * 0.0
    same = session_id[1:] == session_id[:-1]
    if t0 is not None:
        same = same & ((t0[1:] - t0[:-1]) <= max_gap_s) & ((t0[1:] - t0[:-1]) > 0)
    if not bool(same.any()):
        return mu.sum() * 0.0
    return (mu[1:] - mu[:-1]).abs()[same].mean()


def multitask_loss(outputs: dict[str, torch.Tensor],
                   targets: dict[str, torch.Tensor],
                   pos_weight: torch.Tensor | float | None = None,
                   weights: dict[str, float] | None = None) -> dict[str, torch.Tensor]:
    """All four terms plus the weighted total.

    Returns every component so training logs show which task is moving,
    rather than a single number that hides a collapsed head.
    """
    w = {"displacement": W_DISPLACEMENT, "stationary": W_STATIONARY,
         "physics": W_PHYSICS, "yaw": W_YAW, "smoothness": W_SMOOTHNESS,
         "nhc": W_NHC}
    if weights:
        w.update(weights)

    l_disp = gaussian_nll(outputs["mu"], outputs["logvar"],
                          targets["displacement"], targets.get("weight"))
    l_stat = stationary_bce(outputs["stationary_logit"],
                            targets["is_stationary"], pos_weight)
    l_yaw = yaw_rate_mae(outputs["yaw_rate"], targets["yaw_rate"])
    l_smooth = smoothness(outputs["mu"], targets["session_id"],
                          targets.get("t0"))
    if "v_seq" in outputs and "a_seq" in outputs:
        l_phys = physics_penalty(outputs["v_seq"], outputs["a_seq"])
    else:
        l_phys = l_disp.new_zeros(())
    if targets.get("gyro_yaw") is not None:
        l_nhc = nhc_penalty(outputs["mu"], targets["gyro_yaw"])
    else:
        l_nhc = l_disp.new_zeros(())

    total = (w["displacement"] * l_disp + w["stationary"] * l_stat
             + w["physics"] * l_phys + w["yaw"] * l_yaw
             + w["smoothness"] * l_smooth + w["nhc"] * l_nhc)
    return {"total": total, "displacement": l_disp, "stationary": l_stat,
            "physics": l_phys, "yaw": l_yaw, "smoothness": l_smooth,
            "nhc": l_nhc}


def test_shapes(batch: int = 16, verbose: bool = True) -> dict:
    """Random batch through model + loss, asserting shapes and finiteness."""
    from tcn_model import IN_CHANNELS, WINDOW_SAMPLES, TCNModel

    torch.manual_seed(0)
    model = TCNModel()
    x = torch.randn(batch, IN_CHANNELS, WINDOW_SAMPLES)
    out = model(x)

    yaw_target = torch.randn(batch)
    yaw_target[::3] = float("nan")          # exercise the valid-only path
    stationary = (torch.rand(batch) < 0.05).float()
    targets = {
        "displacement": torch.rand(batch) * 30.0,
        "is_stationary": stationary,
        "yaw_rate": yaw_target,
        "session_id": torch.tensor([0] * (batch // 2) + [1] * (batch - batch // 2)),
        "t0": torch.arange(batch, dtype=torch.float32),
    }
    losses = multitask_loss(out, targets,
                            pos_weight=compute_pos_weight(stationary))
    for name, value in losses.items():
        assert value.shape == (), f"{name} should be scalar, got {tuple(value.shape)}"
        assert torch.isfinite(value), f"{name} is not finite"
    losses["total"].backward()
    assert any(p.grad is not None and torch.isfinite(p.grad).all()
               for p in model.parameters()), "no finite gradients"

    if verbose:
        print(f"batch {batch}, stationary positives "
              f"{int(stationary.sum())}/{batch}, "
              f"pos_weight {compute_pos_weight(stationary):.2f}")
        for name, value in losses.items():
            print(f"  {name:<14}{value.item():+.4f}")
        print("  backward pass OK, gradients finite")
    return losses


if __name__ == "__main__":
    test_shapes()
