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
| 4 | 2x data (yaw gate relaxed) | 19.9 h, 26 runs, 8,804 train windows | 0,1,2 | **5.30 / 5.31 / 5.45** | **+8.7% WORSE** |

## First things that beat the baseline (single seed, being confirmed)

Augmentation is the first training-side change to help. On the clean 10 runs at full
capacity:

| config | test RMSE | vs base | speed bias | 300 s drift | vs base |
|---|---|---|---|---|---|
| baseline | 4.877 | — | -1.410 | 20.89% | — |
| dropout 0.3 + rotation | **4.669** | **-4.3%** | -0.977 | 19.60% | -6.2% |
| rotation + gain + noise | 4.712 | -3.4% | -0.996 | 19.56% | -6.4% |
| quality weighting + rotation (26 runs) | 4.768 | -2.2% | -1.038 | **18.57%** | **-11.1%** |
| stationary debias + rotation | 4.875 | -0.0% | **-0.403** | — | — |

Two things worth reading carefully.

**RMSE and drift disagree**, which is why `eval/rank_models.py` exists. The lowest-RMSE
model is not the best navigator: `cl_d3rot` wins on RMSE and `wq_rot` wins at 300 s.
The baseline is still best at 60 s. The new configurations win where it matters most -
long outages - and lose on short ones.

**Bias falls faster than RMSE.** Stationary debiasing plus rotation cuts the speed bias
from -1.410 to -0.403 while barely moving RMSE. For dead reckoning that trade is
favourable: a bias integrates into displacement while zero-mean scatter partly cancels.

### Confirmed over 4 seeds: the gain is BIAS, not RMSE

| config | test RMSE mean | sd | vs base | speed bias | \|bias\| change | t |
|---|---|---|---|---|---|---|
| base | 4.919 | 0.046 | — | -1.428 | — | — |
| **dropout 0.3 + rotation** | 4.800 | 0.119 | -2.4% | **-1.002** | **-0.426** | **-6.72** |
| quality weight + rotation | 4.811 | 0.247 | -2.2% | -1.119 | -0.309 | -2.85 |
| rot + gain + noise | 4.831 | 0.233 | -1.8% | -1.145 | -0.283 | -1.44 |

The single-seed 4.3% RMSE gain shrank to 2.4% and is **not** significant (t = -2.61
against the 3.18 needed at 3 df), and the signs are not consistent across seeds.
Augmentation also triples the seed variance.

What IS solid is bias: dropout plus rotation cuts speed bias by 0.426 m/s, about 30%,
same sign on all four seeds, t = -6.72. That is the quantity that matters for dead
reckoning, because a bias integrates into displacement while zero-mean scatter partly
cancels.

Free-running drift, 3 seeds each:

| config | 30 s | 60 s | 120 s | 300 s | seed sd at 300 s |
|---|---|---|---|---|---|
| base | 16.62 | 17.90 | 17.99 | 21.12 | 0.62 |
| d3rot | 16.59 | 17.56 | 18.68 | 20.69 | 0.21 |
| rotgn | 16.91 | 18.27 | 18.47 | 20.51 | 0.96 |
| wqrot | 16.47 | 18.07 | 18.75 | **20.11** | 0.79 |

Drift gains are 2-5%, which is the same size as the seed spread. **Not conclusive.**

### Where this leaves the model

Everything tried on the training side - more data, less capacity, dropout, weight
decay, two physics losses, three augmentations, quality weighting - moves test RMSE by
at most a couple of percent, and only the bias reduction is statistically solid. The
model is close to what a 10 Hz phone IMU supports for ABSOLUTE speed on this data.

The measured wins remain on the inference side and are an order of magnitude larger:
heading from the debiased gyro rather than the model (300 s drift 46% to 24%), and
per-session offset calibration (30 s drift 20.5% to 16.0%).

Recommended default: **dropout 0.3 + rotation augmentation**, for the bias reduction
rather than for RMSE.

## Leave-one-driver-out: the honest generalisation numbers

Holding out a whole driver rather than a journey, so the test shares no vehicle, phone,
mounting or route with training.

| fold | runs held out | base test | d3rot test | constant | base beats constant |
|---|---|---|---|---|---|
| A | 6 (S\*) | 5.396 | **4.328** | 6.559 | 17.7% |
| B | 3 (M\*) | **3.913** | 4.631 | 5.683 | 31.1% |
| D | 1 (Y\*) | **4.221** | 4.743 | 5.334 | 20.9% |
| E | 16 (V\*) | **9.114** | 9.351 | 9.985 | 8.7% |
| **mean** | | **5.661** | 5.763 | 6.890 | **17.8%** |

Three things this settles.

**The journey split was optimistic by about 15%.** Cross-driver test RMSE is 5.661
against 4.919 on the journey split, and the margin over a constant falls from 28.7% to
17.8%. The earlier numbers were partly measuring familiar roads, exactly as the 46-50%
bounding-box overlap suggested.

**The model does still generalise.** Every fold beats its constant baseline, so it is
reading something real from the IMU rather than only memorising sessions.

**Dropout plus rotation does NOT transfer across drivers.** It wins one fold of four,
by a lot (A: 5.396 to 4.328, -19.8%), and loses the other three, for a mean 1.8% worse.
Its confirmed benefit was bias reduction on the journey split; that does not survive a
change of vehicle. It should not be adopted as a default on this evidence.

**Driver E is the hard case** - 8.7% over constant against 31.1% for driver B. E is the
16 loosely-mounted runs, which is consistent with everything else measured about them.

Per-fold bias also swings sign by driver: A -1.678, B +1.051, D +1.603, E -5.091. The
opposite-sign per-session bias is a cross-vehicle effect, not a quirk of two runs.

## The central finding (2026-09-04)

The model is **massively overfitting**, not data-limited. On the 10-run set:

| split | RMSE | corr | sd(pred)/sd(truth) |
|---|---|---|---|
| train | **0.396** | 0.999 | 0.968 |
| val | 2.796 | 0.865 | 0.913 |
| test | **4.877** | 0.739 | **0.629** |

A 12x train-to-test gap with train correlation 0.999 means it has memorised the
training windows. 3,848,196 parameters against 4,949 training windows is ~778 per
example. The per-run bias that looked like a calibration problem is the downstream
symptom: on an unseen session the model falls back toward the training mean, which
is why bias correlates -0.818 with a run's mean speed and why test shrinkage drops
to 0.629.

Two hypotheses were tested and rejected before landing here: the mounting-rotation
estimate (bias correlates only +0.28 with frame quality, and a better angle
estimator made it worse) and irreducible observability (ruled out by the near-perfect
train fit).

So the levers are capacity, regularisation and data - in that order - not features.

## Data is exhausted at 26 runs

Checked, so nobody repeats it:

- The synchronised archive ships each of its 72 journeys **twice**, under
  `Categorised` and `Uncategorised`, trimmed differently (all 72 stems shared, none
  with matching file size). Using both would put the same journey on both sides of a
  split. Only `Categorised` is used.
- The unsynchronised archive has 97 S-files. 72 duplicate the ones above; the 25
  genuinely new journeys have **no V pair**, so no ground truth. No labelled data
  there.
- So IO-VNBD gives 26 usable runs / 19.9 h and no more. Further data must come from
  our own recording or another corpus. Zenodo, UCI, GitHub and HuggingFace are all
  reachable from this machine if that becomes the priority.

## Held-out journeys are not independent

Every number in the table above holds out *journeys*. That answers "does this
generalise to another drive", not "to another car, phone and mounting" - which is
what the per-session bias failure actually asks.

The test boxes overlap the training boxes: `S3c` against `S3a` 46% and against `M_r2`
50%; `S2_r1` against `M_r2` 34%. All 26 runs are a few routes around Coventry, so a
model can learn a road's vibration signature and habitual speed and score well without
generalising.

`--test-driver {A,B,D,E}` now holds out a whole driver. Folds: A (6 runs, S\*),
B (3, M\*), D (1, Y\*), E (16, V\*). Expect worse numbers; they will be the honest ones.

Also verified: no run family spans train and test under the journey split - runs cut
from one file by a clock reset (`S4`, `S4_r1`) land together. Two families span train
and validation, which inflates validation only.

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
