# Model improvement log

Running record of what has been tried, what it measured, and what is queued. Kept so
that a session which is interrupted — context exhausted, usage limit, machine restart —
can be resumed without re-deriving anything. Newest state at the top of each section.

## 2026-09-05: TCN replaces ResNet1D, and the yaw head is retired

Three results, in descending order of how much they matter.

### 1. Do not integrate the model's yaw head. Use the debiased gyro.

Free-running drift on the held-out test runs, `model_tcn_base_fx`:

| heading source | 60 s | 300 s |
|---|---|---|
| model yaw head | 47.19% | 51.83% |
| **debiased gyro** | **17.72%** | **19.33%** |

Across four checkpoints (3 seeds + the physics variant) the gyro heading is not just
better, it is *stable*, which the yaw head is not:

| checkpoint | free-run 60 s (own yaw) | shippable 60 s | shippable 300 s |
|---|---|---|---|
| seed 0 | 47.19% | 17.72% | 19.33% |
| seed 1 | 86.87% | 18.54% | 19.15% |
| seed 2 | 54.28% | 17.04% | 19.47% |
| physics 0.3 | 63.90% | 18.38% | 19.07% |
| **spread** | **47-87%** | **17.0-18.5%** | **19.1-19.5%** (sd 0.17) |

Integrating the yaw head makes drift a lottery over the seed; integrating the gyro
makes it a constant. Note also that seed 1 has the BEST test RMSE (3.875) and the
WORST free run (86.87%) - the RMSE/drift divergence this log already warned about,
now with a 2x example.

A 63% cut for no retraining — it is an inference-time choice. At 17.72% the free run
now equals the speed-only bound (17.37%), so **heading has stopped being the binding
constraint and speed is again the thing to improve.**

Why the head cannot win: the gyro channel it is trained from correlates 0.943 with
truth yaw rate at a bias of -0.03 deg/s, against the head's 0.746-0.822. A learned
approximation to a signal cannot beat the signal. Keep the head as an auxiliary
training target only. `eval/model_dr_eval.py` now carries a `gyro` heading source so
this stays measured rather than remembered.

### 2. The TCN is a straight win over ResNet1D, on the same yardstick

Identical test split (S2_r1 + S3c, 2,577 windows, constant 6.848):

| | ResNet1D (4 seeds) | TCN `tcn_base` (3 seeds) |
|---|---|---|
| test speed RMSE | 4.919 +/- 0.046 | **4.078 +/- 0.185** (-17.1%) |
| best seed | — | 3.875 |
| vs constant | +28.2% | **+40.4%** |
| train -> test gap | **12.3x** | **1.7x** |
| parameters | 3,848,196 | **152,966** |
| exported asset | 15 MB | **0.7 MB** |

The 12.3x overfitting gap that this log called "the central finding" is gone. That
finding is now closed: it was an artefact of 778 parameters per training example, and a
dilated TCN at 4% of the capacity does not have it. `resnet1d.py` and the ResNet-only
`train.py` are deleted; git history retains both, and every `model_data*.pth` /
`model_cen*` / `model_kin*` checkpoint is historical and no longer loadable.

### 3. The NHC penalty is an L2 penalty on speed, not a constraint

`nhc_penalty` evaluates to `w * mu^2 * mean_T(sin^2(psi_drift))`, and psi comes from the
gyro, which is an **input**. The only gradient path that reduces it is shrinking `mu`.

Ablation on the fixed split, one seed each:

| variant | test RMSE | vs const | bias | shrink | sigma |
|---|---|---|---|---|---|
| `tcn_base` (all aux 0) | **4.236** | +38.1% | -0.454 | 0.699 | 1.89 |
| `+ w_physics 0.3` | 4.111 | +40.0% | — | 0.686 | — |
| `+ gain removed` | 4.376 | +36.1% | -0.764 | 0.696 | 1.79 |
| `w_nhc = 0.05` | 4.304 | +37.1% | -0.827 | 0.755 | 3.20 |
| `w_nhc = 0.2 + phys 0.3 + smooth 0.1` | **7.838** | **-14.5%** | **-5.427** | **0.311** | 4.92 |

`w_physics = 0.3` lands at 4.111, inside the base's seed spread [3.875, 4.236], so the
physics term is **neutral**: `v_seq`/`a_seq` are separate heads nothing reads at
inference, so it is an auxiliary task rather than a constraint on the deliverable.

Raising `w_nhc` 0.05 -> 0.2 costs **+85% test RMSE**. Solving the NLL/NHC stationarity
condition at the model's own sigma^2 = 28.8 predicts `mu = 0.424 x truth`; measured
0.343. At 0.05 it is harmless and mildly improves shrinkage; at 0.2 it dominates the
loss. `losses.py` pre-registered this outcome and it came true.

### Established, do not re-litigate (2026-09-05 additions)

- **Debiasing did not cost the model gravity.** The up-channel per-run bias spread is
  0.007 m/s^2, 1.2% of that channel's sd, and per-channel standardisation removes the
  constant 9.81 in every configuration. The hypothesis is refuted, not untested.
- **Keep `--debias all`.** Per-run spread as a fraction of channel sd: acc_fwd 15.5%,
  acc_right 12.1%, gyro channels 1.3-2.9%. The accelerometer half carries the larger
  correction and fixes the opposite-sign speed bias; the gyro half is small per sample
  but *integrates* (0.004 rad/s ~ 825 deg/hr) and is what makes result 1 work.
  `--debias gyro` now exists but discards the larger half.
- **`gain` augmentation helps.** Removing it measured 3.3% worse (4.236 -> 4.376), so
  the "amplitude is the speed cue" argument does not survive contact with the data.
- **The `smoothness` term is broken as wired.** Its docstring requires batches in
  session/time order; `train_iovnbd.py` shuffles with `randperm`. 20% of adjacent pairs
  are same-run *random* windows, making it a second shrinkage-toward-run-mean term.
  Either sort batches or leave `w_smooth = 0`.
- **Pin the split.** A rebuild without `--fixed-test S2_r1,S3c` silently moved the test
  set to M + S4_r1, making every number incomparable to this log. Always pass it.

### On-device heading: measured on our own recording, not IO-VNBD

`eval/session_eval.py` now scores every heading source the phone records against that
session's own GNSS bearing, over the 188 s driving span of `20260904_195146`:

| source | RMS error | drift |
|---|---|---|
| **rotation vector (`rv`)** | **10.8 deg** | -433 deg/hr |
| accel + magnetometer (what the app used) | 12.6 deg | -423 deg/hr |
| gyro integrated, debiased in DEVICE frame | 16.1 deg | -682 deg/hr |
| game_rv (no magnetometer) | 31.8 deg | +377 deg/hr |
| gyro integrated, raw | 35.9 deg | +879 deg/hr |

Two traps here, both of which produced a plausible-looking wrong answer first.

**Debias in device axes, not world axes.** The offset belongs to the sensor die, so it is
constant in the handset's own frame. Removing it from the world-frame vertical projection
instead measured 43.7 deg RMS - *worse than not debiasing at all* - because the projection
depends on how the phone was tilted at each stop. Done in device axes it is 16.1 deg.

**Stand-still must mean the DEVICE is still.** Gating on GNSS speed learns +1.95 deg/s of
"bias" on this recording, which is the phone being handled while the car sat parked from
200 s on. `DeadReckoner`'s accelerometer- and gyro-norm gates already had this right.

The app now levels the model on the rotation vector rather than the magnetometer matrix,
and subtracts the device-frame gyro bias before inference. Note this does NOT contradict
the IO-VNBD result above: there the comparison was the model's yaw head against the gyro,
and the head lost. Here it is the gyro against a magnetometer-referenced attitude, which
is absolute and does not accumulate - over a 188 s drive that wins, and it keeps working
in a tunnel because it is not GNSS.

### Earth frame costs nothing, and it is still not enough

Trained on `dataset_earth.pt` (the framing `IMUModelRunner` actually feeds), same fixed
split, two seeds:

| framing | test RMSE | shippable drift 60 s | 300 s |
|---|---|---|---|
| vehicle (3 seeds) | 4.078 +/- 0.185 | 17.0-18.5% | 19.1-19.5% |
| **earth** (2 seeds) | **4.149** (4.168, 4.129) | 17.8-19.5% | 18.7-20.0% |

Identical inside seed noise. **Train in the earth frame** - it is free, and it is the only
framing that can be exported without either changing the app or estimating a
device-to-vehicle rotation on-device that a phone has no CAN bus to fit against.

But fixing the frame did NOT make the model transfer. Replayed on our own recording
`20260904_195146`, earth-frame checkpoint, earth-frame features:

| | RMSE | vs constant | r | bias |
|---|---|---|---|---|
| model `mu` | 4.864 | **-127%** | **-0.191** | +2.723 |
| **the integrator alone** | **1.384** | **+34.6%** | **+0.806** | -0.308 |

The integrator is **3.5x better than the model** on this hardware, and the model is
anti-correlated with truth, so no affine recalibration rescues it - there is no signal to
rescale. This is not a frame bug: `build_dataset_iovnbd.earth_frame` and
`session_eval.quat_to_matrix` agree to 9e-15, so the comparison is valid. It is the domain
gap - different country, car, handset, mounting, and a 6.3 m/s campus speed profile against
IO-VNBD's 9.2 m/s.

**Do not ship the speed model.** Ship the integrator, which already beats a constant by
34.6% on our own data. The model becomes worth shipping when it is trained on our own
recordings, and that needs far more than the ~10 minutes of moving data currently on disk.

### Where the integrator actually fails

Not stand-still, and not ZUPT. On `20260904_195146` the integrator tracks GNSS speed to
1.38 m/s RMSE while driving, then runs to 56.8 m/s after parking - because GNSS accuracy
collapsed from 6 m to a 200 m median and the fix rate fell to 0.15 Hz, leaving it genuinely
unaided for ~190 s. Short outages are fine (14 free-run segments, all under 15 s, max
12.5 m/s and r = 0.687). Unaided speed error grows without bound over minutes, which is
what the map, not the model, is there to arrest.

### The gap that caps all of this: train/serve skew

Nothing above reaches the phone yet. `IMUModelRunner` feeds **earth-frame** levelled
acceleration plus **raw device gyro** with **no bias removal**; the checkpoints are
trained on **vehicle-frame** (forward, right, up) features with the per-run stationary
bias subtracted. Measured consequence, on our own Kharagpur recording: every checkpoint
scores worse than predicting a constant, and the shipped asset is worse than a constant
by 208%. `ml_model/export_model.py` now refuses a vehicle-frame export unless
overridden. `dataset_earth.pt` is built and ready to train the matching framing.

## Fixed yardstick (unchanged)

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
