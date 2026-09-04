# Model improvement log

Running record of what has been tried, what it measured, and what is queued. Kept so
that a session which is interrupted — context exhausted, usage limit, machine restart —
can be resumed without re-deriving anything. Newest state at the top of each section.

## Fixed yardstick

All speed numbers below are **test speed RMSE in m/s** on the SAME held-out set:
runs `S2_r1` + `S3c`, 2,577 windows, never trained on. `--fixed-test S2_r1,S3c` pins
it, and pinning is exclusive, so the test set cannot drift between experiments.

Reference points on that set:
- predicting a constant (the train-mean speed): **6.844**
- the shipped pre-IO-VNBD checkpoint: worse than a constant, and anti-correlated
  with truth (r = −0.224)

## Results

| # | change | dataset | seeds | test RMSE | vs baseline |
|---|---|---|---|---|---|
| 1 | baseline, data loss only | 12.9 h, 10 runs, 4,949 train windows | 0,1,2,3 | **4.919** ± 0.046 | — |
| 2 | + centripetal loss `a_lat = v·ω`, w=0.01 | same | 0,1,2,3 | 4.852 ± 0.068 | −1.35%, sign consistent 4/4, paired t = −2.83 on 3 df — suggestive, not significant |
| 3 | + kinematic loss `v_t = v_{t-1} + a·dt` | same | 0 | 4.969 / 5.063 / 6.030 at w = 0.01 / 0.05 / 0.2 | **hurts monotonically**; refutes the execution plan's constraint |
| 4 | 2× data (yaw gate relaxed) | 19.9 h, 26 runs, 8,804 train windows | 0,1,2 | *running* | — |

## What is queued

1. **(running)** Does doubling the data help? Experiment 4.
2. Research-paper mining workflow — shortlist of techniques to try next.
3. **Predict Δspeed anchored to GNSS rather than absolute speed.** Strongest
   diagnosed failure: per-run speed bias has OPPOSITE signs (+1.45 and −3.49 m/s), so
   the model is not learning a transferable absolute scale. Anchoring sidesteps it.
4. Unsynchronised IO-VNBD set — 98 further S-files, 362 MB, untouched.
5. Per-channel input augmentation (mounting-rotation jitter, gain jitter) to attack
   the same cross-session generalisation failure.

## Established, do not re-litigate

- The **yaw head is worse than the raw gyro** it was trained from (r 0.82–0.83 vs
  0.94–0.996). Do not use the model for heading. Keep the head only as an auxiliary
  training signal.
- The **kinematic physics loss hurts** at every weight tried.
- Free-running drift, anchor once from GNSS then IMU only, on the test runs:
  29% of distance at 30 s → 46% at 300 s. Swapping the model's yaw for the raw gyro
  and removing the gyro bias from pre-outage data alone takes 300 s to **24%**.
  After that, **speed is the binding constraint**.
- The **position integrator is verified**: truth speed + truth heading drifts
  0.22–0.59%, so error measured above it is real.

## Reproducing

```
python -m eval.iovnbd --min-yaw-corr 0.0 --npz dataset/iovnbd_train_wide.npz \
    --manifest dataset/iovnbd_manifest_wide.json
python -m ml_model.build_dataset_iovnbd --npz dataset/iovnbd_train_wide.npz \
    --out ml_model/dataset_wide.pt --frame vehicle --fixed-test S2_r1,S3c
python -m ml_model.train_iovnbd --data ml_model/dataset_wide.pt --epochs 40 \
    --sweep --only data --seed 0 --tag _wide_s0
python -m eval.model_dr_eval --model ml_model/model_data.pth   # end-to-end drift
```

`dataset/` and the sweep checkpoints are gitignored; everything under `dataset/` is
regenerable from the zips in `dataset/IO-VNBD_original/`.
