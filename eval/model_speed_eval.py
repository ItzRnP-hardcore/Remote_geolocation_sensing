"""Is the model's speed head good enough to steer the integrator?

Answers the one question that gates IMUModelRunner.speedFusionEnabled, using
ml.csv and gps.csv from sessions recorded on the phone. That is the honest test:
it scores the model as actually exported, quantised and run on the device,
against the GPS speed recorded beside it at the same instant.

The bar is not "correlated with the truth". The bar is "better than a constant".
A model that always predicts the mean speed has RMSE equal to the standard
deviation of the truth; anything worse than that is actively subtracting
information, and feeding it into the velocity channel would degrade a
dead-reckoned position rather than improve it.

Run:  python -m eval.model_speed_eval <sessions_dir>
"""

from __future__ import annotations

import csv
import os
import sys

import numpy as np


def load_pair(session_dir: str):
    """Model mu and the GPS speed interpolated onto the same timestamps."""
    ml_path = os.path.join(session_dir, "ml.csv")
    gps_path = os.path.join(session_dir, "gps.csv")
    if not (os.path.exists(ml_path) and os.path.exists(gps_path)):
        return None

    mt, mu = [], []
    for row in csv.DictReader(open(ml_path)):
        try:
            mt.append(float(row["t_ns"]))
            mu.append(float(row["mu"]))
        except (KeyError, ValueError):
            continue

    gt, gs = [], []
    for row in csv.DictReader(open(gps_path)):
        try:
            speed = float(row["speed_mps"])
        except (KeyError, ValueError, TypeError):
            continue
        if np.isfinite(speed):
            gt.append(float(row["t_ns"]))
            gs.append(speed)

    if len(mt) < 10 or len(gt) < 5:
        return None

    mt, mu = np.array(mt), np.array(mu)
    gt, gs = np.array(gt), np.array(gs)

    # Only score inferences that fall inside the GPS record; extrapolating the
    # truth beyond its own endpoints would invent the thing being measured.
    inside = (mt >= gt[0]) & (mt <= gt[-1])
    mt, mu = mt[inside], mu[inside]
    if len(mt) < 10:
        return None
    return mu, np.interp(mt, gt, gs)


def main(sessions_dir: str) -> int:
    rows, all_mu, all_gps = [], [], []
    for name in sorted(os.listdir(sessions_dir)):
        pair = load_pair(os.path.join(sessions_dir, name))
        if pair is None:
            continue
        mu, gps = pair
        all_mu.append(mu)
        all_gps.append(gps)
        rows.append((name, mu, gps))

    if not rows:
        print(f"No scorable sessions in {sessions_dir}")
        return 2

    header = (f"{'session':<20}{'n':>6}{'mu mean':>9}{'mu sd':>8}"
              f"{'gps mean':>10}{'gps sd':>8}{'corr':>8}{'RMSE':>8}")
    print(header)
    print("-" * len(header))
    for name, mu, gps in rows:
        corr = float(np.corrcoef(mu, gps)[0, 1]) if mu.std() > 0 else float("nan")
        rmse = float(np.sqrt(np.mean((mu - gps) ** 2)))
        print(f"{name:<20}{len(mu):>6}{mu.mean():>9.3f}{mu.std():>8.3f}"
              f"{gps.mean():>10.3f}{gps.std():>8.3f}{corr:>8.3f}{rmse:>8.3f}")

    mu = np.concatenate(all_mu)
    gps = np.concatenate(all_gps)
    corr = float(np.corrcoef(mu, gps)[0, 1])
    rmse = float(np.sqrt(np.mean((mu - gps) ** 2)))
    baseline = float(gps.std())

    print("-" * len(header))
    print(f"{'ALL':<20}{len(mu):>6}{mu.mean():>9.3f}{mu.std():>8.3f}"
          f"{gps.mean():>10.3f}{gps.std():>8.3f}{corr:>8.3f}{rmse:>8.3f}")
    print()
    print(f"constant-prediction baseline RMSE: {baseline:.3f} m/s")
    print(f"model RMSE:                        {rmse:.3f} m/s")
    print(f"mu covers {mu.min():.2f}-{mu.max():.2f} m/s; "
          f"truth covers {gps.min():.2f}-{gps.max():.2f} m/s")
    print()

    if rmse < baseline and corr > 0.5:
        print("VERDICT: beats a constant and tracks the truth. Consider setting")
        print("         IMUModelRunner.speedFusionEnabled = true, then re-run")
        print("         eval/outage_eval.py to confirm end-to-end position error improves.")
        return 0

    print("VERDICT: not fit to steer the integrator. Keep speedFusionEnabled = false.")
    if rmse >= baseline:
        print(f"         RMSE {rmse:.3f} is no better than predicting the constant "
              f"{gps.mean():.3f} m/s.")
    if corr <= 0.5:
        print(f"         Correlation {corr:.3f} means the output barely follows, or "
              f"opposes, real speed.")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
