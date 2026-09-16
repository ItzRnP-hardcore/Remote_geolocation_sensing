# Odyssey — Q&A prep sheet (application side)

For the questions after the slide: **what we used, how we built it, and why that way.**
Everything here is checked against the source in `app/src/main/java/com/example/imulogger/`.

Long-form FAQ (70 questions) is in `APP_FAQ.md`. This sheet is what you actually revise.

---

## 1. The 30-second stack answer

Have this ready verbatim — it's the single most likely opening question.

> "Native Android, Kotlin. One activity and one foreground service. Four threads: UI, a single
> logger thread that owns every callback and every file write, a map-matching thread, and an ML
> thread. osmdroid with Mapsforge for offline vector maps, Play Services for the fused location,
> PyTorch Mobile for the on-device model. No backend, no API keys, no network at runtime."

---

## 2. What we used, and what we rejected

| Layer | What we used | Why | What we rejected, and why |
| --- | --- | --- | --- |
| Language / UI | **Kotlin**, Android Views + ViewBinding, Material 3 | Views are the mature path for a `MapView`-centric screen; ViewBinding is compile-time safe | Compose — osmdroid is a View, and wrapping it buys nothing here |
| Architecture | 1 activity + 1 **foreground service** (`type=location`) | Recording must survive the screen going off and the app being backgrounded | A plain background service — Android kills it; recording would stop mid-drive |
| Sensors | `SensorManager`, 11 streams, `registerListener` with a handler | Delivering callbacks straight onto our own thread avoids a hop | Sensor libraries — none add anything over the platform API |
| Positioning | **`FusedLocationProviderClient`** (Play Services 21.4.0) + raw `LocationManager` GNSS callbacks | Fused gives the best fix; raw GNSS gives satellite health and Doppler that fused hides | Fused alone — it can't tell you *why* a fix degraded |
| Map rendering | **osmdroid 6.1.20 + osmdroid-mapsforge** (mapsforge 0.21.0) | Renders OSM vector data **on-device**; no tile server, no key, no cache to warm | Google Maps SDK — no offline tiles, caching prohibited, needs billing |
| Map data | **Mapsforge `.map`**, per region, from `download.mapsforge.org` | One file gives us basemap *and* road geometry; ODbL data, explicitly bulk-downloadable | Raster tile caches — bigger, zoom-limited; OSM's tile servers forbid bulk download |
| Road network | Parsed out of the **same `.map` file** at zoom 15 | No second download, no routing server, works offline by construction | `.osm.pbf` + OSRM — needs a server, which is the one thing we can't have |
| ML runtime | **PyTorch Mobile** (`pytorch_android_lite` 2.1.0), TorchScript | Same graph we train with; no conversion step to get wrong | TFLite — would mean a second export path and a second set of numerical bugs |
| Storage | Plain **CSV + JSON**, app-scoped external storage | Opens in pandas with no adapter; no storage permission needed | Room/SQLite — the consumer is a research pipeline, not a query engine |
| Downloads | Android **`DownloadManager`** | Files are 100–520 MB; gives resume, notification, survives process death | In-app HTTP — would have to reimplement all three |
| Build | AGP, Gradle 9.7.1, minSdk 26 / target 36 | — | — |

**Dependency count is deliberately small**: androidx core/appcompat/activity/lifecycle, Material,
play-services-location, osmdroid ×2, pytorch ×2. No analytics, no crash reporter, no DI framework,
no networking library.

---

## 3. How we implemented it — the eight things worth explaining

### 3.1 Capture
Eleven streams declared as a list of `(sensorType, label, rateHz)`. Accel and gyro at 200 Hz,
calibrated **and** uncalibrated; magnetometer ×2 at 50 Hz; two rotation vectors; gravity; linear
acceleration; barometer. We ask the sensor hub to **batch up to one second** into its hardware
FIFO before waking the CPU — batched events keep their own correct timestamps, so batching costs
latency, never accuracy. A partial wake lock keeps delivery from going lossy with the screen off.

> **Say this if pushed:** requested rates are *hints*. We asked for 50 Hz on the rotation vector
> and got 100; we asked for 25 on the barometer and got 10. That's why every row is timestamped
> and why we record each sensor's hardware minimum delay into `session.json`.

### 3.2 Timebase
Every timestamp in every file is `SystemClock.elapsedRealtimeNanos` — the same clock as
`SensorEvent.timestamp` and `Location.getElapsedRealtimeNanos()`. IMU and GPS therefore align by
joining on a column. It's monotonic, so an NTP correction mid-drive can't make `dt` go backwards.
One `(monotonic, UTC)` pair is recorded so the trace can still be tied to wall-clock time.

### 3.3 Threading
Four threads, one rule: **exactly one writer.**

| Thread | Owns |
| --- | --- |
| main | UI only — collects four `StateFlow`s |
| `imu-logger` | every sensor / location / GNSS callback, every file write, the integrator |
| `map-match` | reading the `.map`, building the road graph, running the matcher |
| `imu-ml` | model load and inference |

The matcher and the ML thread post results **back** to the logger thread to be written. Result:
no locks anywhere in the recording path. Two rules we enforce — the model is never loaded on the
main thread (it materialises a 15 MB asset), and status is never published per sensor event
(publishing from the 200 Hz gyro callback would wake the UI 200×/second; we cache and publish on a
2 s tick).

### 3.4 Dead reckoning
Strapdown integrator in ENU. Attitude from `TYPE_ROTATION_VECTOR`, acceleration rotated into the
earth frame, gravity and a learned bias removed, integrated twice. Two things keep it honest:
**zero-velocity updates** (when the accelerometer norm sits at gravity and the gyro is near zero,
velocity is forced to zero and the leftover acceleration is fed back as bias) and a **learned
gravity magnitude** rather than a hardcoded 9.80665 — this device reads about 0.9% low, and a
fixed constant would inject that straight into the vertical channel.

### 3.5 The ML thread
A temporal convolutional network over a **10-second window sampled at 10 Hz** (100 samples,
6 channels), exported as TorchScript, ~15 MB, running on its own thread. It predicts **speed and
stationarity** — deliberately, because those are what double integration is worst at. Inputs are
levelled with the same rotation-vector attitude the integrator navigates on, and the gyro is
debiased first, so what the model sees at run time matches what it saw in training. If inference
falls behind, requests are **dropped past 8 pending**, never queued — a backlog only produces
staler predictions.

> **The honest line:** the thread runs on-device today; fusing its speed into the position estimate
> is gated in code until a checkpoint beats a constant-speed baseline. *"It runs on-device, and we
> gate it until it earns its place."*

### 3.6 Offline maps
Resolution order: Mapsforge `.map` vector files → raster archives → tile cache → network (only if
Offline is off). When a `.map` is present we explicitly disable the map's data connection: there is
no server behind it to consult. Files live in app-scoped storage, so **no storage permission**.
Downloads land as `.part`, are checked for the Mapsforge magic bytes, and only then renamed to
`.map` — the renderer never sees a partial or a captive-portal HTML page.

### 3.7 Road network and matching
`RoadNetwork` reads `highway=*` ways from the `.map` at **zoom 15** (lower zooms are generalised
for rendering and would snap us to a caricature of the road), loading the 3×3 tile block around the
vehicle. `RoadGraph` recovers the topology the format doesn't store: Mapsforge quantises to
microdegrees (~11 cm), so segments that shared an OSM node come back on identical coordinates —
snapping endpoints to a **0.5 m grid** rebuilds the graph. Measured on 25,383 segments: **98.5% in
one connected component**, mean node degree 2.73.

`MapMatcher` is an online HMM — Viterbi, beam 12, ≤24 candidates per step — scoring candidates on
distance from the fix *and* agreement between the road's bearing and our course. It **declines to
snap** below 25 m of modelled uncertainty, because a road centreline is a lane-width from where you
actually drove: snapping everything moved our mean error from 4.85 m to 9.85 m, and gating brought
it to 5.92 m. The matched bearing feeds back at gain 0.35, capped 4°/update, only above 0.6
confidence.

### 3.8 Output and UI
Nine files per session, CSV + a JSON sidecar with the device and full sensor inventory. 64 KB
buffered writers flushed every 2 s, so a crash costs at most two seconds. ~350 MB/hour. The UI is
map-first: the diagnostics panel collapses to a button, three tracks are drawn (GPS blue, IMU-only
dashed orange, snapped green), and drift is shown as a **percentage of distance travelled** against
a 10% target — a bare metre count can't be judged without knowing how far you went.

---

## 4. Numbers card — memorise these six

1. **1,312** IMU samples/second across 11 streams; 502,869 samples in a 398 s session.
2. **4** threads, **1** writer, **0** locks in the recording path.
3. **0** API keys, backends, or runtime network calls.
4. **210 MB** *per region* — basemap **and** road network in one file.
5. **98.5%** of road segments in one connected component after the 0.5 m endpoint snap.
6. **350 MB/hour**, flushed every **2 s** — max 2 seconds of data lost on a crash.

---

## 5. Rapid-fire Q&A

### Stack and choices

**"What's the tech stack?"** → Use the 30-second answer in §1.

**"Why native Android and not Flutter / React Native?"** → We need 200 Hz sensor callbacks
delivered onto a thread we control, a foreground service with a wake lock, and raw GNSS
measurement callbacks. All three are platform APIs; a cross-platform bridge would add latency to
the hot path and still need native code for the parts that matter.

**"Why osmdroid and not Google Maps?"** → Three independent blockers, any one fatal: the Maps SDK
exposes no offline tile access, caching tiles is prohibited by their terms, and it needs a billing
account. For an app about tunnels, the first alone rules it out.

**"Why PyTorch Mobile and not TFLite?"** → We train in PyTorch, so TorchScript is the same graph
with no conversion step. A conversion is one more place for numerical differences to hide.

**"Isn't 500 MB a huge APK?"** → That's a demo build with a region bundled in assets so it works on
a phone that has never had a network. The shipping path is a few-MB APK plus the in-app downloader,
which is fully implemented — that's the third screenshot.

### Sensors

**"Why record uncalibrated sensors too?"** → Android silently subtracts a bias it estimated itself.
A filter estimating its own bias needs the raw signal *plus* what the OS believed, or the OS's
correction is indistinguishable from physics.

**"Why two rotation vectors?"** → One includes the magnetometer, one doesn't. A steel car body
distorts the field, so the game rotation vector doesn't swing near a truck; the full one gives
absolute heading. We navigate on the rotation vector — measured 10.8° RMS against the road versus
12.6° for a magnetometer-derived matrix.

**"Do you really get 200 Hz?"** → Measured 200.0 Hz sustained over 383 seconds. But requested rates
are hints — see §3.1.

**"Battery?"** → Meaningful: wake lock, 11 sensors, continuous high-accuracy GNSS, live map. Sensor
batching into the hardware FIFO is the main mitigation. We haven't measured milliamps and won't
quote a number we can't defend.

### GNSS

**"Why four GNSS subscriptions?"** → They answer different questions. Fused gives position;
`GnssStatus` gives satellite count and C/N0, which **collapse before the fix does** and are our
earliest tunnel warning; raw measurements give per-satellite Doppler; navigation messages give the
ephemeris needed to use it.

**"What's the gate?"** → A fix only re-anchors the estimate at **≥4 satellites used and <20 m
accuracy**. Below that we free-run, because anchoring to a degraded fix hides the very error we're
trying to measure.

**"Why log raw Doppler if nothing uses it?"** → A position fix needs four satellites; a *velocity*
fix from Doppler needs far fewer once vehicle constraints are applied. We have that result in
simulation and want to validate it on real hardware — this is the file that makes that possible.

### Maps and matching

**"Where does the road network come from?"** → The same `.map` file we render. No second download,
no routing server. See §3.7 for the microdegree trick.

**"Doesn't snapping always help?"** → No, and we measured it. Snapping every fix made us worse
(4.85 m → 9.85 m). Gating below 25 m uncertainty brought it to 5.92 m and raised the share of
helpful snaps during an outage from 61% to 95%.

**"Isn't feeding the road heading back a feedback loop?"** → Yes, and it's capped: gain 0.35, at
most 4° per update, only above 0.6 confidence. A wrong road has to be believed repeatedly before it
does damage; a right one still pulls the heading in within seconds.

**"Is downloading OSM data allowed?"** → Yes. OSM *data* is ODbL and explicitly meant to be
downloaded in bulk. OSM's *public tile servers* forbid bulk download. Only the second is
restricted, and we use the first.

### The ML thread

**"What model, and what does it predict?"** → A temporal convolutional network over a 10-second,
10 Hz IMU window; it predicts speed and stationarity — the two things double integration is worst
at.

**"Is it driving the navigation right now?"** → It runs on-device and its outputs are logged and
displayed. Fusing its speed into the position estimate is gated in code until a checkpoint beats a
constant baseline. We'd rather ship a gate than a wrong number.

**"Why not just use the ML model instead of the integrator?"** → Because they fail differently. The
integrator is excellent over short horizons and drifts; the network is steady but coarse. The
architecture keeps both and lets the GNSS gate arbitrate.

*(Anything deeper — training data, architecture, accuracy — is the modelling side: "my colleague
can take that", or point at the separate model report.)*

### Engineering

**"How do you handle concurrency?"** → We don't have to. Exactly one thread writes, so there's no
lock in the recording path and no way to interleave a half-written row.

**"What if the app crashes mid-drive?"** → At most 2 seconds lost — 64 KB buffers flushed on a 2 s
tick. `session.json` is written at session start as well as end, so even an unclean stop keeps the
device and sensor metadata.

**"What if the user starts with Location switched off?"** → It recovers. That was a real bug: every
callback refused and nothing retried, so a whole drive recorded no fixes silently. Subscription is
now a *state we maintain* — a system broadcast for speed plus a 2 s poll, because some OEM builds
throttle broadcasts to background apps — and the re-subscribe count goes into `session.json`.

**"Is it tested?"** → Thinly, and I'd rather say so. One unit test around Mapsforge, no
instrumentation tests. What we do have is measurement: every non-obvious decision has a number
next to it from a recorded session, and `eval/` re-runs them.

---

## 6. Traps — the questions designed to catch you

**"So it's just a data logger?"** → Three things say otherwise: the road network comes out of a
*rendering* file so the whole thing works offline; we rebuild road topology the format doesn't
store, from coordinate quantisation alone; and the free-run control makes GNSS-outage error
measurable on an open road, which is what turns a demo into an instrument.

**"Your drift looks large."** → It is, and that's the finding. Nothing that integrates a consumer
MEMS accelerometer twice is a good navigator. The app exists to make that error *visible* so a
real filter has something to be tuned against — there's deliberately no covariance in the
integrator; it's the measurement rig, not the filter.

**"Does this work outside India?"** → Anywhere Mapsforge publishes a region, which is most of the
world. The zone list is a convenience; any `.map` file dropped in works identically, and multiple
files merge into one view.

**"What breaks first in production?"** → Storage and battery. 350 MB/hour fills a phone in a day of
driving. Both are inherent to running a research instrument at full rate; a production build would
decimate the streams it doesn't need. The recording *correctness* I'd defend as-is.

**"What would you do next?"** → Ship the lean APK and stop bundling the map; add instrumentation
tests around the session lifecycle, since that's where our real bugs have been; and validate the
ML speed head against GNSS so the fusion gate can come off.

---

## 7. If you don't know the answer

Say the measurement you'd run. This project's credibility is that every claim has a number behind
it — protect that rather than guessing.

> "I don't have that number in front of me. The way we'd settle it is X, and the data to do it is
> already in the session files."
