"""1-D ResNet over a 10 s IMU window.

Input  (B, 6, 100)  — levelled accelerometer XYZ and gyroscope XYZ at 10 Hz.
Output three heads:

  displacement   mu (metres travelled in the next second) and log sigma^2,
                 so the model reports its own uncertainty rather than a bare
                 point estimate
  stationary     logit for "the vehicle is not moving"
  yaw_rate       a correction to the integrated gyro yaw rate

No max-pool after the stem: at 10 Hz a 100-sample window is already short, and
pooling twice in the first two layers would throw away the temporal detail the
displacement estimate depends on. The stem's stride 2 plus three strided stages
takes 100 -> 7 samples, which is enough.

Run:  python -m model.resnet1d      (shape test + parameter count)
"""

from __future__ import annotations

import torch
import torch.nn as nn

IN_CHANNELS = 6
WINDOW_SAMPLES = 100

# Clamp bounds for log sigma^2. Below -6 the Gaussian NLL rewards shrinking
# variance without bound on easy windows; above 4 the model can mute the loss
# on hard ones by declaring everything uncertain.
LOGVAR_MIN = -6.0
LOGVAR_MAX = 4.0


class BasicBlock(nn.Module):
    """Two 3-wide convolutions with an identity (or projected) shortcut."""

    expansion = 1

    def __init__(self, in_ch: int, out_ch: int, stride: int = 1):
        super().__init__()
        self.conv1 = nn.Conv1d(in_ch, out_ch, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn1 = nn.BatchNorm1d(out_ch)
        self.conv2 = nn.Conv1d(out_ch, out_ch, kernel_size=3, stride=1,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm1d(out_ch)
        self.relu = nn.ReLU(inplace=True)

        self.downsample = None
        if stride != 1 or in_ch != out_ch:
            self.downsample = nn.Sequential(
                nn.Conv1d(in_ch, out_ch, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm1d(out_ch))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        identity = x if self.downsample is None else self.downsample(x)
        out = self.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        return self.relu(out + identity)


class ResNet1D(nn.Module):
    """Displacement / stationary / yaw-rate network."""

    def __init__(self, in_channels: int = IN_CHANNELS,
                 widths: tuple[int, ...] = (64, 128, 256, 512),
                 blocks_per_stage: int = 2):
        super().__init__()
        self.stem = nn.Sequential(
            nn.Conv1d(in_channels, widths[0], kernel_size=7, stride=2,
                      padding=3, bias=False),
            nn.BatchNorm1d(widths[0]),
            nn.ReLU(inplace=True))

        stages = []
        in_ch = widths[0]
        for i, width in enumerate(widths):
            stride = 1 if i == 0 else 2
            blocks = [BasicBlock(in_ch, width, stride)]
            blocks += [BasicBlock(width, width, 1)
                       for _ in range(blocks_per_stage - 1)]
            stages.append(nn.Sequential(*blocks))
            in_ch = width
        self.stages = nn.Sequential(*stages)
        self.pool = nn.AdaptiveAvgPool1d(1)

        feat = widths[-1]
        # Displacement is non-negative, so mu comes through softplus rather
        # than a raw linear output that could go negative on easy windows.
        self.head_mu = nn.Linear(feat, 1)
        self.head_logvar = nn.Linear(feat, 1)
        self.head_stationary = nn.Linear(feat, 1)
        self.head_yaw = nn.Linear(feat, 1)
        self.softplus = nn.Softplus()

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
        h = self.pool(self.stages(self.stem(x))).flatten(1)
        logvar = self.head_logvar(h).squeeze(-1).clamp(LOGVAR_MIN, LOGVAR_MAX)
        return {
            "mu": self.softplus(self.head_mu(h)).squeeze(-1),
            "logvar": logvar,
            "stationary_logit": self.head_stationary(h).squeeze(-1),
            "yaw_rate": self.head_yaw(h).squeeze(-1),
        }


def count_parameters(model: nn.Module) -> tuple[int, int]:
    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return total, trainable


def test_shapes(batch: int = 8, verbose: bool = True) -> dict[str, torch.Tensor]:
    """Run a random batch and assert every output shape."""
    model = ResNet1D()
    model.eval()
    x = torch.randn(batch, IN_CHANNELS, WINDOW_SAMPLES)
    with torch.no_grad():
        out = model(x)

    assert set(out) == {"mu", "logvar", "stationary_logit", "yaw_rate"}
    for name, tensor in out.items():
        assert tensor.shape == (batch,), \
            f"{name}: expected ({batch},), got {tuple(tensor.shape)}"
        assert torch.isfinite(tensor).all(), f"{name} contains non-finite values"
    assert (out["mu"] >= 0).all(), "displacement mu must be non-negative"
    assert (out["logvar"] >= LOGVAR_MIN).all() and (out["logvar"] <= LOGVAR_MAX).all()

    if verbose:
        total, trainable = count_parameters(model)
        print(f"input           {tuple(x.shape)}")
        for name, tensor in out.items():
            print(f"  {name:<18}{tuple(tensor.shape)}")
        print(f"\nparameters: {total:,} total, {trainable:,} trainable "
              f"({total / 1e6:.2f} M)")
        # Confirm the temporal reduction is what the architecture claims.
        with torch.no_grad():
            t = model.stages(model.stem(x)).shape[-1]
        print(f"temporal length {WINDOW_SAMPLES} -> {t} after stem + stages")
    return out


if __name__ == "__main__":
    test_shapes()
