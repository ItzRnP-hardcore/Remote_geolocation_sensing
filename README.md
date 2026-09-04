# ImuLogger

Recording layer for the dead-reckoning project: captures a time-synchronised IMU + GNSS trace on
the phone so a fusion filter (and later a map-matching model) has something trustworthy to consume.

## Build and run

```bash
./gradlew installDebug
```

Requires the Android SDK path in `local.properties` (already written for this machine) and JDK 17+.
The Gradle build lives at the repo root, so `Remote_geolocation_sensing` is the folder to open.
The Gradle wrapper pulls Gradle 9.7.1 on first run. From Android Studio, just open the project
root and press Run.

In the app: tap **Start recording**, accept precise location, drive. The notification carries a
Stop action so the screen can stay off. Files are flushed every 2 s, so a crash or a battery pull
costs at most two seconds of data.

## Output

One directory per session under
`Android/data/com.example.imulogger/files/sessions/<yyyyMMdd_HHmmss>/`:

| File | Contents |
| --- | --- |
| `imu.csv` | `t_ns,sensor,accuracy,v0..v5` — one row per sample, long format |
| `gps.csv` | `t_ns,utc_ms,provider,lat,lon,alt_m,speed_mps,bearing_deg,acc_m,vert_acc_m,speed_acc_mps,bearing_acc_deg` |
| `gnss_status.csv` | `t_ns,sats_visible,sats_used,mean_cn0_used_dbhz,max_cn0_dbhz` |
| `deadreckon.csv` | `t_ns,lat,lon,speed_mps,drift_m,bias_e,bias_n,bias_u,stationary,free_run` at 10 Hz |
| `ml.csv` | `t_ns,mu,logvar,stationary_logit,yaw_rate` at 10 Hz — model outputs, observation only |
| `session.json` | Device, per-sensor inventory (vendor, resolution, range, FIFO depth), clock sync, end-of-run summary |

Pull a session with:

```bash
adb pull /sdcard/Android/data/com.example.imulogger/files/sessions
```

### Timebase

Every `t_ns` column is nanoseconds on the **`SystemClock.elapsedRealtimeNanos`** monotonic clock —
the same base as `SensorEvent.timestamp` and `Location.getElapsedRealtimeNanos()`. IMU samples and
GPS fixes are therefore directly alignable with no offset estimation, and `dt` never goes backwards
when NTP corrects the wall clock mid-drive. `session.json.clock_sync` records one
`(elapsed_realtime_ns, unix_epoch_ms)` pair if a trace has to be tied to UTC.

### Sensor streams in `imu.csv`

| `sensor` | Rate | Values |
| --- | --- | --- |
| `accel`, `gyro` | 200 Hz | x, y, z |
| `accel_uncal`, `gyro_uncal` | 200 Hz | x, y, z, bias_x, bias_y, bias_z |
| `mag`, `mag_uncal` | 50 Hz | x, y, z (+ iron bias for uncal) |
| `game_rv` | 100 Hz | quaternion, no magnetometer |
| `rv` | 50 Hz | quaternion, magnetometer-referenced |
| `gravity`, `linear_accel` | 50 / 100 Hz | x, y, z |
| `pressure` | 25 Hz | hPa |

Unused columns are left empty rather than zero-filled, so `pandas.read_csv` gives `NaN` and never
mistakes a missing axis for a real zero.

Uncalibrated streams are recorded alongside the calibrated ones deliberately: a filter that
estimates its own accelerometer and gyro bias states should see the raw signal plus the bias the OS
believes in, not a signal the OS has already corrected behind its back.

`gnss_status.csv` exists for the tunnel case. Satellite count and C/N0 collapse *before* the fused
provider stops emitting fixes, which makes them the earliest available evidence for shifting trust
from GNSS to the IMU, and a much better trigger than waiting for fixes to time out.


## Dead reckoning

`DeadReckoner` runs live beside GNSS so the two can be compared on the map: the blue track is GPS,
the dashed orange track is the IMU integrating on its own. It is a strapdown integrator, not a
filter — there is no covariance anywhere in it. The point is to make the error visible so a real
filter has something to be tuned against.

Attitude comes from `TYPE_ROTATION_VECTOR`, acceleration is rotated into ENU, gravity and a learned
bias are removed, and the result is integrated twice. Two things keep that honest:

- **Zero-velocity updates.** While the phone is stationary (accelerometer norm near gravity, gyro
  near zero) velocity is forced to zero and the leftover acceleration is fed back as bias.
- **A learned gravity magnitude** rather than a hardcoded 9.80665. This device reads about 0.9%
  low, and a fixed constant would inject that straight into the vertical channel.

The integrator is re-anchored to GNSS only while the fix is healthy (4+ satellites used, accuracy
under 20 m). During an outage it free-runs, so the gap between the two tracks is the real cost of
the tunnel. **Free-run** in the UI withholds GNSS on purpose, so that behaviour can be exercised on
an open road instead of waiting for a real tunnel.

## Offline maps

The map is osmdroid. Sources are resolved in this order, and the first one that exists wins:

1. **Mapsforge `.map` vector files** — rendered on-device from OpenStreetMap data. No tile server,
   no API key, no usage policy, no cache to keep warm. This is the recommended route.
2. **Raster archives** (`.mbtiles`, `.sqlite`, `.gpkg`, `.gemf`, `.zip`).
3. **The tile cache**, then the network — and only if **Offline** is toggled off.

Everything goes in `Android/data/com.example.imulogger/files/osmdroid/`. Push with:

```bash
adb push eastern-zone.map /sdcard/Android/data/com.example.imulogger/files/osmdroid/
```

When vector maps are present the app frames them on launch, so you can confirm the file loaded
before you have a fix.

### The map is not in the repo

`eastern-zone.map` is 210 MB. GitHub rejects any blob over 100 MB, and Git LFS's free tier is
1 GB of storage *and* 1 GB of bandwidth per month — four clones of a 210 MB file exhausts the
monthly allowance and then blocks everyone's pushes until it resets. So the map is distributed
as a release asset (2 GB limit, no bandwidth quota, and it stays out of everyone's clone).

```bash
gh release download maps-v1 -p eastern-zone.map
adb push eastern-zone.map /sdcard/Android/data/com.example.imulogger/files/osmdroid/
```

The app checks that directory before anything else and only falls back to raster tiles when no
`.map` file is there, so a side-loaded map and a bundled one behave identically. To bundle it into
the APK instead, drop it in `app/src/main/assets/` — at the cost of a 466 MB APK on every install.

### Where to get `.map` files

`download.mapsforge.org/maps/v5/` publishes free per-region builds. India is split into zones
rather than states — `asia/india/eastern-zone.map` (210 MB) covers West Bengal. The whole of
`asia/india.map` is 1.5 GB if you want it.

A `bremen.map` (14 MB) is currently on the device from testing; delete it once you have the zone
you want, or leave it — multiple `.map` files are merged into one view.

### Why raster preloading needs a tile source of your own

Kept for the raster path only. OpenStreetMap's public tile servers forbid bulk downloading, and
osmdroid enforces it: Mapnik carries `FLAG_NO_BULK` and `CacheManager` throws
`TileSourcePolicyException` rather than fetch. Tap Preload and paste a `{z}/{x}/{y}` URL from a
provider whose terms permit caching — Thunderforest and Stadia Maps issue free keys without a
payment card.

With Mapsforge in place this is mostly moot: a vector file covering a whole zone is smaller than a
raster cache of one city, and needs no preloading at all.

Google Maps is not an option: the Maps SDK exposes no offline tile access, caching tiles is
prohibited by the Platform Terms, and it requires a billing account regardless.

### Note on OSM

The distinction that matters is between OSM **data** (ODbL, free, explicitly meant to be
downloaded in bulk — Geofabrik, Mapsforge builds) and OSM's **public tile servers** (donated
capacity, no bulk download). Only the second is restricted. Everything above uses OSM data.


## Threading

Three threads, with a deliberate single-writer rule.

| Thread | Owns |
| --- | --- |
| main | UI only |
| `imu-logger` | every sensor / location / GNSS callback, all file writes, the integrator |
| `imu-ml` | model load and inference |

Inference is posted to `imu-ml` and its result posted back to `imu-logger` to be written, so there
is still exactly one thread touching the writers and no locking anywhere. Requests are dropped
past `ML_MAX_PENDING` rather than queued — a backlog only yields staler predictions.

Two things that must not regress: the model must not be loaded on the main thread (it materialises
a 15 MB asset and builds a TorchScript module), and per-sensor-event writes to the status
StateFlow must not come back. Gyro runs at 200 Hz; publishing from there allocates a status object
and wakes the UI collector 200 times a second. Cache into a field, let the 2 s tick publish it.

## Model outputs are observed, not consumed

`ml.csv` records what the network predicts, and the UI shows it, but nothing in the navigation path
acts on it yet. That is deliberate until `mu` has been validated against GNSS ground truth:

- `mu` is trained against `speed_mps`, not displacement, despite the docstring in `tcn_model.py`.
- `yaw_rate` now carries real signal (86.3% non-zero labels), but **must not be integrated**:
  free-running drift at 300 s is 51.8% using the head against 19.3% using the debiased gyro, and
  across seeds the head ranges 47-87% while the gyro holds 19.1-19.5%. Keep it as an auxiliary
  training target only.
- The training set is now IO-VNBD: 4,765 windows over 10 runs, speeds to 32.4 m/s. It is
  also a different country, car and handset, which is why `eval/session_eval.py` exists.
- Levelling skew: **closed.** The app now levels with the same rotation-vector attitude the
  integrator navigates on, rather than `getRotationMatrix(gravity, magnetometer)`. Measured on
  session `20260904_195146` against GNSS bearing, the rotation vector tracks the road to 10.8 deg
  RMS against the magnetometer matrix's 12.6, and it does not depend on the one sensor a steel
  vehicle disturbs.
- Gyro bias: **closed.** `DeadReckoner` learns the device-frame gyroscope offset during
  stand-still and `SensorService` subtracts it before the model sees it, matching the
  `--debias all` features the checkpoint is trained on. Device frame matters: removing the same
  offset in the world frame measured *worse* than not removing it at all (43.7 deg RMS against
  35.9), because the projection depends on how the phone happened to be tilted at each stop.
  Done correctly it takes integrated heading from 35.9 to 16.1 deg RMS.
- Frame skew: **still open, and it is the one that blocks shipping.** The checkpoints are trained
  on vehicle-frame (forward, right, up) features; the app feeds earth frame. `export_model.py`
  refuses that combination rather than shipping it silently. Either train on `dataset_earth.pt`
  or estimate the device-to-vehicle rotation on-device.

When it is ready, couple it as a **velocity** update along the current heading, not a position
offset — see the note where `applyMLCorrection` used to live in `DeadReckoner.kt`.

## Open question for Task 4

The execution plan has the Android app as a thin polling layer that pushes into a C++ engine's
shared memory and reads a 10 Hz map-matched JSON payload back over a local WebSocket. This app
instead does everything in-process in Kotlin. Both are defensible, but Tasks 2 and 3 are building
against the plan's contract and there is currently no JNI boundary and no WebSocket client here.
That needs a team decision before integration, not at merge time.
