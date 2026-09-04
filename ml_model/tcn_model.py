"""CNN stem + temporal convolutional network with per-timestep physics heads.

Input (B, 6, 30) -- 3 seconds at 10 Hz in the gravity-aligned level frame.

Design notes
------------
**Stem.** Conv1d(6->64, k=7, s=1, p=3). The wide kernel acts as a
vibration/pothole filter: impulse noise from road surface is smoothed before
the dilated stack sees it. Stride 1, unlike the ResNet's stride 2 -- a
30-sample window is short enough that halving it immediately discards
structure the dilations are there to capture.

**Causal dilations.** Each TCN block left-pads by (k-1)*d and takes no right
padding, so within the stack output t depends only on inputs <= t. With
dilations 1,2,4,8 and k=3 the receptive field is 1 + 2*(1+2+4+8) = 31 samples,
which covers the whole 3-second window exactly once.

**The STEM is not causal, deliberately.** Conv1d(k=7, p=3) pads symmetrically,
so stem output t sees inputs t-3..t+3. The model as a whole therefore has a
3-sample (0.3 s) lookahead. That is harmless HERE -- the entire window is in
the past relative to the label, which is the displacement over the FOLLOWING
second, so no label information leaks. It would matter if these per-timestep
outputs were ever used for streaming inference within the window; they are
not. `CausalTCNStem` below makes the stem causal if that changes.

**Two output paths.** The three scalar heads (displacement, stationary, yaw)
read the globally pooled feature, as before. The physics heads read the
PER-TIMESTEP features, because a consistency penalty between velocity and
acceleration needs sequences and the pooled vector has none.
"""

from __future__ import annotations

import torch
import torch.nn as nn

IN_CHANNELS = 6
WINDOW_SAMPLES = 100
LOGVAR_MIN = -6.0
LOGVAR_MAX = 4.0


class CausalConv1d(nn.Conv1d):
    """Dilated convolution that cannot see the future.

    Left-pads by (kernel-1)*dilation and trims the overhang, so output t is a
    function of inputs <= t only.
    """

    def __init__(self, in_ch: int, out_ch: int, kernel_size: int, dilation: int):
        self._trim = (kernel_size - 1) * dilation
        super().__init__(in_ch, out_ch, kernel_size,
                         padding=self._trim, dilation=dilation)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = super().forward(x)
        return out[..., :-self._trim] if self._trim else out


class TCNBlock(nn.Module):
    """Two causal dilated convolutions with a residual connection."""

    def __init__(self, in_ch: int, out_ch: int, dilation: int,
                 kernel_size: int = 3):
        super().__init__()
        self.conv1 = CausalConv1d(in_ch, out_ch, kernel_size, dilation)
        self.bn1 = nn.BatchNorm1d(out_ch)
        self.conv2 = CausalConv1d(out_ch, out_ch, kernel_size, dilation)
        self.bn2 = nn.BatchNorm1d(out_ch)
        self.relu = nn.ReLU(inplace=True)
        self.skip = (nn.Conv1d(in_ch, out_ch, 1) if in_ch != out_ch
                     else nn.Identity())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        residual = self.skip(x)
        out = self.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        return self.relu(out + residual)


class TCNModel(nn.Module):
    """CNN stem -> dilated TCN -> three scalar heads + two per-timestep heads."""

    def __init__(self, in_channels: int = IN_CHANNELS,
                 stem_width: int = 64,
                 channels: tuple[int, ...] = (64, 64, 64, 64, 64, 64),
                 dilations: tuple[int, ...] = (1, 2, 4, 8, 16, 32),
                 kernel_size: int = 3):
        super().__init__()
        if len(channels) != len(dilations):
            raise ValueError("channels and dilations must be the same length")

        self.stem = nn.Sequential(
            nn.Conv1d(in_channels, stem_width, kernel_size=7, stride=1,
                      padding=3, bias=False),
            nn.BatchNorm1d(stem_width),
            nn.ReLU(inplace=True))

        blocks, ch = [], stem_width
        for out_ch, d in zip(channels, dilations):
            blocks.append(TCNBlock(ch, out_ch, d, kernel_size))
            ch = out_ch
        self.blocks = nn.Sequential(*blocks)
        self.pool = nn.AdaptiveAvgPool1d(1)

        self.head_disp = nn.Linear(ch, 2)        # mu, logvar
        self.head_stationary = nn.Linear(ch, 1)
        self.head_yaw = nn.Linear(ch, 1)
        self.softplus = nn.Softplus()

        # Per-timestep physics heads. Velocity and acceleration are predicted
        # INDEPENDENTLY so the physics penalty has something to constrain --
        # see the note in losses.physics_penalty.
        self.head_v_seq = nn.Conv1d(ch, 1, kernel_size=1)
        self.head_a_seq = nn.Conv1d(ch, 1, kernel_size=1)

        for m in self.modules():
            if isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out",
                                        nonlinearity="relu")
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1.0)
                nn.init.constant_(m.bias, 0.0)

    def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
        if x.dim() != 3 or x.shape[1] != IN_CHANNELS:
            raise ValueError(
                f"expected (B, {IN_CHANNELS}, T), got {tuple(x.shape)}")
        seq = self.blocks(self.stem(x))          # (B, C, T)
        pooled = self.pool(seq).flatten(1)       # (B, C)

        disp = self.head_disp(pooled)
        logvar = disp[:, 1].clamp(LOGVAR_MIN, LOGVAR_MAX)
        return {
            "mu": self.softplus(disp[:, 0]),
            "logvar": logvar,
            "stationary_logit": self.head_stationary(pooled).squeeze(-1),
            "yaw_rate": self.head_yaw(pooled).squeeze(-1),
            # Velocity is non-negative; acceleration is signed.
            "v_seq": self.softplus(self.head_v_seq(seq).squeeze(1)),
            "a_seq": self.head_a_seq(seq).squeeze(1),
        }


def count_parameters(model: nn.Module) -> tuple[int, int]:
    total = sum(p.numel() for p in model.parameters())
    return total, sum(p.numel() for p in model.parameters() if p.requires_grad)


def receptive_field(dilations, kernel_size: int = 3, blocks_per: int = 2) -> int:
    return 1 + blocks_per * sum((kernel_size - 1) * d for d in dilations)


def test_shapes(batch: int = 8, verbose: bool = True) -> dict:
    model = TCNModel()
    model.eval()
    x = torch.randn(batch, IN_CHANNELS, WINDOW_SAMPLES)
    with torch.no_grad():
        out = model(x)
    for name in ("mu", "logvar", "stationary_logit", "yaw_rate"):
        assert out[name].shape == (batch,), f"{name}: {tuple(out[name].shape)}"
    for name in ("v_seq", "a_seq"):
        assert out[name].shape == (batch, WINDOW_SAMPLES), \
            f"{name}: {tuple(out[name].shape)}"
    assert (out["mu"] >= 0).all() and (out["v_seq"] >= 0).all()

    # Causality of the TCN STACK (the stem is deliberately non-causal; see
    # the module docstring). Perturb the last timestep of the stem output and
    # confirm no earlier block output moves.
    with torch.no_grad():
        h = model.stem(x)
        h2 = h.clone()
        h2[:, :, -1] += 100.0
        s1, s2 = model.blocks(h), model.blocks(h2)
    assert torch.allclose(s1[..., :-1], s2[..., :-1], atol=1e-4), \
        "TCN stack is not causal: a future input changed an earlier output"

    # And confirm the stem's lookahead is exactly the 3 samples we claim.
    with torch.no_grad():
        xa = x.clone(); xa[:, :, -1] += 100.0
        d = (model.stem(xa) - model.stem(x)).abs().sum(dim=(0, 1))
    lookahead = int((d > 1e-6).sum().item()) - 1
    assert lookahead == 3, f"stem lookahead {lookahead}, expected 3"

    if verbose:
        total, _ = count_parameters(model)
        print(f"input {tuple(x.shape)}")
        for k, v in out.items():
            print(f"  {k:<18}{tuple(v.shape)}")
        print(f"\nparameters: {total:,} ({total / 1e6:.2f} M)")
        print(f"receptive field: {receptive_field((1, 2, 4, 8, 16, 32))} samples "
              f"(window is {WINDOW_SAMPLES})")
        print(f"TCN stack causal: yes;  stem lookahead: {lookahead} samples "
              f"({lookahead / 10:.1f} s, harmless — window precedes the label)")
    return out


if __name__ == "__main__":
    test_shapes()
