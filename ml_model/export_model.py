"""Export the trained ResNet1D to a PyTorch Lite module for the Android app.

The wrapper exists to bridge two conventions: Android buffers samples in arrival
order and sends (B, L, C), while Conv1d wants channels first, (B, C, L). Doing
the transpose inside the graph keeps the Kotlin side free of layout concerns.

The dict output is flattened to a tuple because org.pytorch's Java bindings can
unpack an IValue tuple but not a dict.
"""

import os

import torch

from resnet1d import ResNet1D

WEIGHTS_PATH = "model_weights.pth"
EXPORT_PATH = "model_mobile.pt"
WINDOW_SAMPLES = 100
IN_CHANNELS = 6


class ResNet1DWrapper(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.model = ResNet1D()

    def forward(self, x):
        # (B, L, C) -> (B, C, L). Android sends samples in arrival order.
        x = x.transpose(1, 2)
        out = self.model(x)
        return (out["mu"], out["logvar"], out["stationary_logit"], out["yaw_rate"])


def export_to_mobile():
    if not os.path.exists(WEIGHTS_PATH):
        raise FileNotFoundError(
            f"{WEIGHTS_PATH} not found. Run train.py first — exporting without it "
            f"ships randomly initialised weights that will silently produce noise."
        )

    print("Loading model architecture...")
    wrapper = ResNet1DWrapper()

    # Without this the export is a freshly initialised network. It still traces,
    # still loads on the phone, and still returns plausible-looking floats, so
    # nothing downstream can tell that the model was never trained.
    print(f"Loading trained weights from {WEIGHTS_PATH}...")
    state = torch.load(WEIGHTS_PATH, map_location="cpu", weights_only=True)
    wrapper.model.load_state_dict(state)
    wrapper.eval()

    example_input = torch.randn(1, WINDOW_SAMPLES, IN_CHANNELS)

    print("Tracing model for TorchScript...")
    traced = torch.jit.trace(wrapper, example_input, strict=False)
    traced._save_for_lite_interpreter(EXPORT_PATH)
    print(f"Successfully exported optimized mobile model to {EXPORT_PATH}")

    # Verify against the eager model rather than merely checking that the export
    # loads. A mismatch here is the difference between shipping the trained
    # network and shipping noise, and it is invisible on the phone.
    print("Verifying exported weights match the trained model...")
    reference = ResNet1D()
    reference.load_state_dict(state)
    reference.eval()

    loaded = torch.jit.load(EXPORT_PATH, map_location="cpu")
    loaded.eval()

    torch.manual_seed(0)
    probe = torch.randn(4, WINDOW_SAMPLES, IN_CHANNELS)
    with torch.no_grad():
        expected = reference(probe.transpose(1, 2))
        actual = loaded(probe)

    for i, key in enumerate(["mu", "logvar", "stationary_logit", "yaw_rate"]):
        delta = (expected[key] - actual[i]).abs().max().item()
        status = "ok" if delta < 1e-4 else "MISMATCH"
        print(f"  {key:<18} max |delta| = {delta:.3e}  {status}")
        if delta >= 1e-4:
            raise RuntimeError(
                f"Exported {key} does not match the trained model (delta {delta:.3e}). "
                f"The .pt file must not be shipped."
            )

    print("Verification passed: the exported model matches the trained weights.")


if __name__ == "__main__":
    export_to_mobile()
