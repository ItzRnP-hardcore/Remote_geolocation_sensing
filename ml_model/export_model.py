"""Export the trained TCN to a PyTorch Lite module for the Android app.

The wrapper exists to bridge two conventions: Android buffers samples in arrival
order and sends (B, L, C), while Conv1d wants channels first, (B, C, L). Doing
the transpose inside the graph keeps the Kotlin side free of layout concerns.

The dict output is flattened to a tuple because org.pytorch's Java bindings can
unpack an IValue tuple but not a dict. Only the four scalar heads are exported:
`v_seq` and `a_seq` exist to give `losses.physics_penalty` two independent
sequences to constrain against each other, and nothing at inference reads them,
so shipping them would cost bandwidth for no behaviour.

FRAME CONTRACT — the thing most likely to be wrong on any given day.
`IMUModelRunner` feeds earth-frame levelled acceleration plus RAW DEVICE gyro,
with no bias removal. A checkpoint trained from `build_dataset_iovnbd.py
--frame vehicle --debias all` expects (forward, right, up) with the per-run
stationary bias already subtracted. Those are different inputs, and a model
exported across that gap produces confident nonsense rather than an error.
`--frame` records what the checkpoint was trained on and refuses to export a
vehicle-frame model unless `--i-know-the-app-feeds-earth` is passed, because
this has silently shipped once already.

Run:  python -m ml_model.export_model --weights ml_model/model_tcn_base_fx.pth \
          --frame earth --out app/src/main/assets/model_mobile.pt
"""

from __future__ import annotations

import argparse
import os
import sys

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tcn_model import IN_CHANNELS, WINDOW_SAMPLES, TCNModel  # noqa: E402

DEFAULT_WEIGHTS = os.path.join("ml_model", "model_tcn_base_fx.pth")
DEFAULT_EXPORT = os.path.join("ml_model", "model_mobile.pt")
HEADS = ("mu", "logvar", "stationary_logit", "yaw_rate")


class TCNWrapper(torch.nn.Module):
    def __init__(self, model: TCNModel):
        super().__init__()
        self.model = model

    def forward(self, x):
        # (B, L, C) -> (B, C, L). Android sends samples in arrival order.
        out = self.model(x.transpose(1, 2))
        return (out["mu"], out["logvar"], out["stationary_logit"], out["yaw_rate"])


def infer_geometry(state):
    """Recover channels and dilations from a checkpoint so it loads itself.

    Nothing records which capacity a checkpoint was trained at outside its
    filename, and the filename is not load-bearing. The block count comes from
    the state dict; dilations are reconstructed as the doubling sequence
    `TCNModel` defaults to, which is the only one the trainer ever uses.
    """
    channels = []
    i = 0
    while f"blocks.{i}.conv1.weight" in state:
        channels.append(state[f"blocks.{i}.conv1.weight"].shape[0])
        i += 1
    if not channels:
        return None, None
    stem = state["stem.0.weight"].shape[0] if "stem.0.weight" in state else 64
    dilations = tuple(2 ** k for k in range(len(channels)))
    return (stem, tuple(channels), dilations)


def export(weights_path: str, export_path: str) -> None:
    if not os.path.exists(weights_path):
        raise FileNotFoundError(
            f"{weights_path} not found. Train first — exporting without it ships "
            f"randomly initialised weights that will silently produce noise."
        )

    state = torch.load(weights_path, map_location="cpu", weights_only=True)
    geom = infer_geometry(state)
    if geom is None:
        raise RuntimeError(
            f"{weights_path} has no TCN blocks. A ResNet1D checkpoint cannot be "
            f"exported by this script; retrain with ml_model/train_iovnbd.py."
        )
    stem, channels, dilations = geom
    print(f"checkpoint geometry: stem {stem}, channels {channels}, dilations {dilations}")

    model = TCNModel(stem_width=stem, channels=channels, dilations=dilations)
    # Without this the export is a freshly initialised network. It still traces,
    # still loads on the phone, and still returns plausible-looking floats, so
    # nothing downstream can tell that the model was never trained.
    model.load_state_dict(state)
    model.eval()

    wrapper = TCNWrapper(model).eval()
    example = torch.randn(1, WINDOW_SAMPLES, IN_CHANNELS)

    print("Tracing model for TorchScript...")
    traced = torch.jit.trace(wrapper, example, strict=False)
    os.makedirs(os.path.dirname(export_path) or ".", exist_ok=True)
    traced._save_for_lite_interpreter(export_path)
    size_mb = os.path.getsize(export_path) / 1e6
    print(f"wrote {export_path} ({size_mb:.1f} MB)")

    # Verify against the eager model rather than merely checking that the export
    # loads. A mismatch here is the difference between shipping the trained
    # network and shipping noise, and it is invisible on the phone.
    print("Verifying exported weights match the trained model...")
    loaded = torch.jit.load(export_path, map_location="cpu").eval()
    torch.manual_seed(0)
    probe = torch.randn(4, WINDOW_SAMPLES, IN_CHANNELS)
    with torch.no_grad():
        expected = model(probe.transpose(1, 2))
        actual = loaded(probe)

    for i, key in enumerate(HEADS):
        delta = (expected[key] - actual[i]).abs().max().item()
        print(f"  {key:<18} max |delta| = {delta:.3e}  "
              f"{'ok' if delta < 1e-4 else 'MISMATCH'}")
        if delta >= 1e-4:
            raise RuntimeError(
                f"Exported {key} does not match the trained model "
                f"(delta {delta:.3e}). The .pt file must not be shipped."
            )
    print("Verification passed: the exported model matches the trained weights.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--out", default=DEFAULT_EXPORT)
    ap.add_argument("--frame", choices=("earth", "vehicle"), required=True,
                    help="the frame the CHECKPOINT was trained in, from its dataset meta")
    ap.add_argument("--i-know-the-app-feeds-earth", action="store_true",
                    help="export a vehicle-frame checkpoint anyway")
    args = ap.parse_args(argv)

    if args.frame == "vehicle" and not args.i_know_the_app_feeds_earth:
        print(
            "REFUSING TO EXPORT.\n"
            "  This checkpoint was trained on vehicle-frame (forward, right, up)\n"
            "  features with a per-run stationary bias removed. IMUModelRunner\n"
            "  feeds earth-frame levelled acceleration and raw device gyro, with\n"
            "  no bias removal. The model would receive inputs it has never seen\n"
            "  and return confident nonsense.\n\n"
            "  Fix one side first:\n"
            "    - rebuild with --frame earth and retrain, or\n"
            "    - make SensorService produce vehicle-frame, debiased features.\n\n"
            "  Pass --i-know-the-app-feeds-earth to override.",
            file=sys.stderr)
        return 2

    export(args.weights, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
