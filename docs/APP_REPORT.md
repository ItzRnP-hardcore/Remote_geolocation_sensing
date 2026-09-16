# IMU Logger — Application Report

**Scope.** This report covers the *application*: the Android app that captures the data, renders
the map, runs the offline map stack and presents the live session. The learned model is mentioned
only where the app has to host it (a thread, an asset, a CSV column); the modelling work itself is
documented separately in `ml_model/` and `eval/`.

Everything below is read out of the source at `app/src/main/java/com/example/imulogger/` and
verified against a recorded session (`extracted_sessions/20260904_195146`, Samsung SM-G990E,
Android 16, 398 s).

---

## 1. What the app is

IMU Logger is a single-activity, single-service Android app that records a **time-synchronised
inertial + GNSS trace** on a phone in a moving vehicle, and simultaneously **dead-reckons a
position from the IMU alone** so the two tracks can be compared on a map, live, in the field.

It is a measurement instrument first and a navigation demo second. The design decisions all fall
out of that: one monotonic timebase for every row, one writer thread, flush every two seconds, no
network dependency of any kind on the recording path, and a map that renders in a tunnel because
it never needed a server to begin with.

**One-sentence pitch:** *a phone-only, fully offline logger that shows you what dead reckoning
actually costs you when GPS goes away — with the raw evidence written to disk while it does.*

---

## 2. At a glance

| | |
| --- | --- |
| Package | `com.example.imulogger` |
| Language / UI | Kotlin, Android Views + ViewBinding, Material 3 |
| minSdk / targetSdk / compileSdk | 26 / 36 / 37 |
| Components | 1 activity (`MainActivity`), 1 foreground service (`SensorService`) |
| Source size | 14 Kotlin files, ~5,200 lines |
| Threads | 4 (main, `imu-logger`, `map-match`, `imu-ml`) |
| Runtime permissions | Fine + coarse location, post notifications |
| **External API keys** | **None. Zero. Not optional-none — architecturally none.** |
| Network required at runtime | No (only to *download* a map, once, ahead of time) |
| Output | 9 files per session, CSV + JSON, ~350 MB/hour |
| APK | 490 MB debug / 487 MB release (a 210 MiB map is bundled — see §12) |

### Third-party dependencies

| Library | Version | Used for |
| --- | --- | --- |
| `androidx.core / appcompat / activity / lifecycle` | 1.19.0 / 1.8.0 / 1.13.0 / 2.11.0 | Platform plumbing, lifecycle-scoped collection |
| `com.google.android.material` | 1.14.0 | Buttons, cards, bottom sheet, snackbar, progress |
| `play-services-location` | 21.4.0 | `FusedLocationProviderClient` |
| `org.osmdroid:osmdroid-android` | 6.1.20 | MapView, overlays, tile cache, CacheManager |
| `org.osmdroid:osmdroid-mapsforge` | 6.1.20 | Bridge to Mapsforge; pulls mapsforge 0.21.0 |
| `org.pytorch:pytorch_android_lite` | 2.1.0 | TorchScript inference (observation only) |

> The mapsforge version is pinned transitively on purpose: the osmdroid bridge was compiled
> against 0.21 and the render-theme API changed afterwards. Forcing a newer mapsforge breaks the
> theme-loading path.

---

## 3. Architecture

```
                    ┌──────────────────────── main thread ──────────────────────┐
                    │  MainActivity                                             │
                    │   ├─ MapView (osmdroid) + 5 overlay layers                │
                    │   ├─ status chips / session panel / FABs / settings sheet │
                    │   └─ collects 4 StateFlows, renders, never blocks         │
                    └───────────────▲───────────────────────────────────────────┘
                                    │  StateFlow: status, track, drTrack, snapTrack
                                    │  (published on a 2 s tick, never per sample)
┌───────────────────────────────────┴───────────────────────────────────────────┐
│ SensorService  (foreground, type=location, PARTIAL_WAKE_LOCK)                  │
│                                                                               │
│  imu-logger thread  ── the single writer ────────────────────────────────────  │
│    11 sensor streams ─┬─► imu.csv          (~1,312 rows/s)                     │
│                       ├─► DeadReckoner (strapdown integrator)                  │
│                       └─► deadreckon.csv   (10 Hz)                             │
│    Fused location ────►  gps.csv                                               │
│    GnssStatus ────────►  gnss_status.csv                                       │
│    GnssMeasurements ──►  gnss_raw.csv      (per satellite, per epoch)          │
│    GnssNavigationMsg ─►  gnss_nav.csv      (base64 subframes)                  │
│    2 s tick: flush all writers · publish status · re-check location sub        │
│                                                                               │
│  map-match thread ──────────────────────────────────────────────────────────  │
│    RoadNetwork (reads the .map) → RoadGraph → MapMatcher / AlongRoadTracker    │
│    results posted back to imu-logger ─► mapmatch.csv                           │
│                                                                               │
│  imu-ml thread ─────────────────────────────────────────────────────────────  │
│    IMUModelRunner, 10 Hz windows, results posted back ─► ml.csv                │
└───────────────────────────────────────────────────────────────────────────────┘
```

### The two invariants the whole design rests on

**1. Single writer.** Every sensor, location and GNSS callback is delivered *onto the
`imu-logger` thread* — `registerListener(..., loggerHandler)` — and every file write happens
there. The map-match and ML threads never touch a writer; they post their results back to the
logger thread to be written. Consequence: **there is not a single lock or synchronised block in
the recording path**, and there is no way to interleave a half-written row.

**2. One monotonic timebase.** Every `t_ns` column in every file is
`SystemClock.elapsedRealtimeNanos` — the same clock as `SensorEvent.timestamp` and
`Location.getElapsedRealtimeNanos()`. IMU samples and GPS fixes are therefore alignable with no
offset estimation, and `dt` never goes backwards when NTP corrects the wall clock mid-drive.
`session.json` records one `(elapsed_realtime_ns, unix_epoch_ms)` pair so a trace can still be
tied to UTC afterwards.

---

## 4. Sensors

### 4.1 What is recorded

Eleven streams, registered from a declarative list of `StreamSpec(type, label, periodUs)`:

| `sensor` label | Android type | Requested | **Measured on device** | Why it is there |
| --- | --- | --- | --- | --- |
| `accel` | `TYPE_ACCELEROMETER` | 200 Hz | **200.0 Hz** | Primary dead-reckoning input |
| `gyro` | `TYPE_GYROSCOPE` | 200 Hz | **200.0 Hz** | Heading rate; the integral is the error |
| `accel_uncal` | `TYPE_ACCELEROMETER_UNCALIBRATED` | 200 Hz | **200.0 Hz** | Raw signal **plus the bias the OS is subtracting** |
| `gyro_uncal` | `TYPE_GYROSCOPE_UNCALIBRATED` | 200 Hz | **200.0 Hz** | Same, for the gyro |
| `mag` | `TYPE_MAGNETIC_FIELD` | 50 Hz | **50.0 Hz** | Absolute heading reference |
| `mag_uncal` | `TYPE_MAGNETIC_FIELD_UNCALIBRATED` | 50 Hz | **50.0 Hz** | Includes hard/soft-iron bias estimate |
| `game_rv` | `TYPE_GAME_ROTATION_VECTOR` | 100 Hz | **100.5 Hz** | Attitude **without** the magnetometer |
| `rv` | `TYPE_ROTATION_VECTOR` | 50 Hz | **100.5 Hz** | Attitude with magnetometer; drives the integrator |
| `gravity` | `TYPE_GRAVITY` | 50 Hz | **100.5 Hz** | Vendor gravity/linear split, as a cross-check |
| `linear_accel` | `TYPE_LINEAR_ACCELERATION` | 100 Hz | **100.5 Hz** | Gravity-cancelled acceleration |
| `pressure` | `TYPE_PRESSURE` | 25 Hz | **10.2 Hz** | Altitude change; the pressure step at a tunnel mouth |

Aggregate: **1,312 samples/second**, 502,869 rows in a 398 s session.

**Requested rates are hints, not contracts.** The table above is the point: `rv` and `gravity`
came back at *twice* what was asked for, and `pressure` at *less than half* — the sensor hub
delivers what its hardware `minDelay` and its own scheduling allow. This is exactly why every row
carries its own timestamp and why `session.json` records each sensor's `min_delay_us`,
`max_delay_us` and `fifo_max_events`. Nothing downstream is allowed to assume a rate.

### 4.2 Why calibrated *and* uncalibrated

A filter that estimates its own accelerometer and gyro bias states must see the raw signal plus
the bias the OS believes in — **not** a signal the OS has already silently corrected behind its
back. Recording both costs 4 extra streams and turns the OS's own correction into an observable
rather than a confound.

### 4.3 Why two rotation vectors

`GAME_ROTATION_VECTOR` excludes the magnetometer, so it does not swing when a steel vehicle body
distorts the field. `ROTATION_VECTOR` includes it, for absolute heading. Recording both makes the
magnetic disturbance measurable instead of assumed. The integrator navigates on `rv`; measured on
session `20260904_195146` against GNSS bearing it tracks the road to **10.8° RMS** versus **12.6°**
for a `getRotationMatrix(gravity, magnetometer)` matrix — and it drops the dependency on the one
sensor a vehicle corrupts.

### 4.4 Batching, and why the phone can sleep

`MAX_REPORT_LATENCY_US = 1_000_000` lets the sensor hub buffer up to **one second** of samples in
its hardware FIFO before waking the application processor. Batched events keep their individual,
correct timestamps — the FIFO stamps them, not the delivery. On the test device the accelerometer
reports `fifo_max_events: 3000`, so a second of 200 Hz data fits comfortably; the gyro reports
`0`, so it does not batch and delivers continuously.

A `PARTIAL_WAKE_LOCK` (12 h timeout as a safety valve against a leak) keeps the CPU alive with the
screen off. Without it, delivery becomes bursty and lossy the moment the screen darkens — which is
the normal state of a phone on a dashboard.

### 4.5 The 15-second start-up window

The first **15 seconds** of sensor events after the first callback are deliberately **dropped**:
5 s of hardware stabilisation, then 10 s of orientation settling. `imu.csv` therefore begins ~15 s
after `session_start_elapsed_realtime_ns`. This is an intentional gap, not lost data — but anyone
joining `session.json` timings to `imu.csv` must expect it.

### 4.6 The sensor inventory in `session.json`

Every registered sensor is recorded with `name`, `vendor`, `type`, `max_range`, `resolution`,
`power_ma`, `min_delay_us`, `max_delay_us`, `fifo_max_events`, `is_wake_up`. Without this a trace
cannot be reproduced or compared across handsets. On the test device: **STM LSM6DSO** accel + gyro
(resolution 2.39 mm/s², 0.61 mrad/s; ranges ±78.5 m/s², ±17.45 rad/s), **AKM AK09918C**
magnetometer.

---

## 5. Location and GNSS

Four independent subscriptions, all delivered onto the logger thread:

| Source | API | Rate | File |
| --- | --- | --- | --- |
| Fused position | `FusedLocationProviderClient`, `PRIORITY_HIGH_ACCURACY`, 1 s interval / 500 ms min | measured **0.59 Hz** | `gps.csv` |
| Constellation health | `LocationManager.registerGnssStatusCallback` | per epoch | `gnss_status.csv` |
| Raw observables | `registerGnssMeasurementsCallback` | per satellite per epoch | `gnss_raw.csv` |
| Broadcast ephemeris | `registerGnssNavigationMessageCallback` | as broadcast | `gnss_nav.csv` |

Measured on the test device: **up to 39 satellites visible, median 25 used in fix**, across five
constellations (GPS, GLONASS, QZSS, BeiDou, Galileo — `constellation` 1/3/4/5/6). Median
horizontal accuracy 6.0 m; max recorded speed 14.9 m/s.

**Why `gnss_status.csv` matters more than it looks.** Satellite count and C/N0 collapse *before*
the fused provider stops emitting fixes. They are therefore the **earliest available evidence**
that the vehicle is entering a tunnel, and a far better trigger for shifting trust from GNSS to
the IMU than waiting for fixes to time out. The app already uses it that way: a fix is only
allowed to re-anchor the integrator when **≥ 4 satellites are used in fix and accuracy < 20 m**.

**Why raw measurements are logged even though nothing reads them yet.** A *position* fix needs
four satellites for four unknowns. A *velocity* fix from Doppler needs only as many as there are
unknowns left after the vehicle's own constraints are applied — a flat road removes vertical
velocity, the non-holonomic constraint reduces velocity to a scalar along the gyro's heading, and
coasting the clock drift removes the last one. That result currently exists only against a
simulated constellation (`eval/scarce_gnss.py`); `gnss_raw.csv` is the file that makes validating
it against real hardware possible. Both `gnss_raw_supported` and `gnss_raw_rows` are recorded in
`session.json`, because *"the chipset withheld the data"* and *"the callback was never
registered"* are different bugs that look identical from the CSV alone.

**Recovery when Location is switched on mid-session.** This was a real, silent, whole-drive data
loss: start a session with the Location master switch off and `registerGnssStatusCallback` returns
false, the raw callbacks refuse, and the fused client has nothing to deliver — and none of it was
ever retried. The fix makes subscription **a state to be maintained, not an event that happened**:

- `ensureLocationSubscribed()` is idempotent, and tears down stale subscriptions before re-adding;
- a `PROVIDERS_CHANGED` / `MODE_CHANGED` broadcast receiver is the fast path;
- the **2-second flush tick calls it too**, because several OEM builds (Samsung among them)
  throttle or drop implicit broadcasts to background processes. One boolean read every 2 s cannot
  be throttled away.
- `session.json.summary.location_subscribes` records how many times it fired, so a recovered
  session is visible after the fact.

---

## 6. API keys, services and network posture

**The app has no API keys, and no place to put one.** This is the strongest single claim in the
application layer, so it is worth being precise about every outbound path:

| Path | Endpoint | Key? | When |
| --- | --- | --- | --- |
| Vector map download | `download.mapsforge.org/maps/v5/asia/india/<zone>.map` | **No** | Only when the user taps Download |
| Vector map rendering | — (on-device, from the `.map` file) | **No** | Always; no network at all |
| Road network for matching | — (the same `.map` file) | **No** | Always |
| Positioning | Android platform / Play Services location | **No** | During a session |
| Raster tiles *(fallback)* | OSM Mapnik via osmdroid | **No** | Only if no `.map` is installed **and** Offline is off |
| Raster preloading *(optional)* | a `{z}/{x}/{y}` template **the user pastes** | the user's own | Only if they choose to |

Consequences worth stating plainly:

- **No billing account, no quota, no rate limit, no key rotation, no key-leakage risk.**
- **No dependency on a vendor staying online**, changing terms, or deprecating a free tier.
- The recording path never touches the network. `INTERNET` is declared *only* for map tiles and
  the map download, and the manifest says so in a comment next to the declaration.
- Nothing about the user's position, sensors or trace is transmitted anywhere. There is no
  analytics SDK, no crash reporter, no backend. `android:allowBackup="false"`.

**Why not Google Maps.** Three independent blockers, any one of which is fatal here: the Maps SDK
exposes no offline tile access; caching tiles is prohibited by the Platform Terms; and it requires
a billing account regardless of usage.

**The OSM distinction people get wrong.** OSM *data* (ODbL, free, explicitly meant to be
bulk-downloaded — Geofabrik, Mapsforge builds) is not the same thing as OSM's *public tile
servers* (donated capacity, bulk download forbidden). Only the second is restricted. Everything
this app does uses the first. Where the app does touch the tile path it respects the policy rather
than routing around it: osmdroid tags Mapnik `FLAG_NO_BULK` and throws `TileSourcePolicyException`,
so the Preload button asks the user to bring a source *they* are entitled to bulk-fetch from
(Thunderforest and Stadia issue free keys without a payment card) instead of quietly hammering
someone else's servers. osmdroid's user agent is set to the package name, because the default
string is blocked outright by OSM.

---

## 7. The map stack

### 7.1 Source resolution order

`OfflineMaps.apply()` resolves in a fixed order, first hit wins:

1. **Mapsforge `.map` vector files** — OpenStreetMap data compiled to a vector format and
   rasterised *on the phone*. No tile server, no key, no usage policy, no cache to keep warm.
   When one is present the map explicitly calls `setUseDataConnection(false)`: there is no server
   behind it to consult.
2. **Raster archives** — `.mbtiles`, `.sqlite`, `.gpkg`, `.gemf`, `.zip`, whatever osmdroid's
   `ArchiveFileFactory` recognises.
3. **The osmdroid tile cache**, then the network — and only if **Offline** is toggled off.

Everything lives in `Android/data/com.example.imulogger/files/osmdroid/` — app-scoped, so **no
storage permission is required**. A corrupt or wrong-version `.map` degrades to raster tiles
rather than taking the map down with it.

`OfflineMaps.configure()` must run *before* any MapView is inflated: osmdroid reads its
configuration during view construction, and a MapView built too early writes its cache to the
legacy `/sdcard/osmdroid` path that scoped storage will not grant.

### 7.2 Why vector beats raster here

A vector file covering an entire zone is **smaller than a raster cache of one city** — which is
what makes "preload a radius" moot rather than merely solved. `eastern-zone.map` is 210 MiB and
covers West Bengal, Jharkhand, Odisha and Bihar at full street detail, at every zoom, indefinitely.

### 7.3 Render themes (the map "models" on screen)

| Mode | Theme | Note |
| --- | --- | --- |
| Night (default) | bundled `assets/custom_theme.xml` (64 KB) | Dark, with city labels shrunk so they stop dominating |
| Day | `InternalRenderTheme.DEFAULT` | Mapsforge's stock styling |
| Fallback | built-in default | If theme loading throws, the map still renders |

This is the **render** theme, not the app theme. The overlay UI stays dark in both, because a
light card floating over a light basemap loses all separation. A separate `ColorMatrix` inversion
path exists for the raster fallback in dark mode.

### 7.4 The downloader

Regional maps are fetched through the system `DownloadManager`, not an in-app HTTP call, because
these files are 100–520 MB and the user will background the app long before one finishes.
DownloadManager already provides the notification, the resume (`Accept-Ranges: bytes`) and
survival across process death; an in-app downloader would have to reimplement all three.

Six India zones are offered, with rough extents so the app can say *"you are probably in the
Eastern zone"* before anything is installed:

| Zone | Approx. size | Covers |
| --- | --- | --- |
| Eastern | 210 MB | WB, Jharkhand, Odisha, Bihar |
| Northern | 205 MB | Delhi, UP, Punjab, Rajasthan |
| Western | 203 MB | Maharashtra, Gujarat, Goa |
| Central | 313 MB | MP, Chhattisgarh |
| Southern | 520 MB | KA, TN, KL, AP, TS |
| North-eastern | 112 MB | Assam and the seven sisters |

Overlapping boxes are resolved by area — a point in Bihar is inside both the Northern and Eastern
boxes, and Eastern is the tighter fit, so it wins.

The integrity story is the interesting part:

- Downloads land as `<zone>.map.part`. `MapsforgeSource` only ever looks for `.map`, so a transfer
  in flight — or one that arrived truncated — is **never handed to the renderer**.
- Before promotion the file is checked for the Mapsforge magic bytes (`mapsforge binary OSM`).
  A captive-portal HTML page or a half-transfer is discarded *there*, rather than surfacing later
  as an unexplained fallback to blank raster tiles.
- The finished file belongs to the system download provider, not the app, so a plain rename can be
  refused even inside our own external-files directory. The code reads the header through
  `DownloadManager.openDownloadedFile()` (granted regardless of ownership) and falls back to
  copying through that same descriptor into a temp file, renamed only once complete.
- Promotion is driven from **both** the activity lifecycle (`onResume`) and the
  `ACTION_DOWNLOAD_COMPLETE` broadcast, because the user will almost always have left the app by
  the time 200 MB lands. It runs on its own thread, not `lifecycleScope`, so a screen rotation
  cannot cancel a half-gigabyte copy.
- Wi-Fi-only versus any-network is put to the user as an explicit choice rather than defaulted.
  Half a gigabyte is their data allowance to spend.

The same "temp file, verify, rename" discipline governs extracting the bundled asset map: writing
straight to the destination means a kill mid-copy leaves a truncated file that `exists()` happily
accepts forever, after which Mapsforge fails to parse it and the app silently falls back to blank
tiles with nothing on screen to say why.

### 7.5 The road model, extracted from the same file

This is the part that surprises people: **the road network needs no extra download.** A `.map`
file is built for rendering, but every way keeps its OSM tags, so `highway=*` centrelines come
back from `MapFile.readMapData()` with full coordinates.

- **`RoadNetwork`** reads roads at **z15** (lower zooms are generalised for rendering — vertices
  get dropped and short links disappear — which would snap the vehicle to a caricature of the road
  it is actually on). It loads the **3×3 tile block** around the vehicle so a car near a tile edge
  still sees the road continuing, and caches tiles; at z15 each covers roughly 1 km here. Only
  drivable classes are kept — footways and cycleways are excluded deliberately, because they offer
  the matcher tempting parallel candidates a few metres from the carriageway. A probe of one z15
  tile over IIT Kharagpur returned 106 ways and 660 vertices.
- **`RoadGraph`** recovers *topology*, which the format does not store: ways are clipped per tile
  with no node identity. The trick is that Mapsforge quantises coordinates to microdegrees
  (~0.11 m), so two segments that shared an OSM node come back on *identical* coordinates.
  Snapping endpoints onto a **0.5 m grid** rebuilds the graph without node IDs, and tile-boundary
  clipping heals for free. Measured on 25,383 segments around IIT Kharagpur: every segment gained
  at least one link, **98.5% landed in a single connected component**, mean node degree 2.73.
- **`MapMatcher`** is an online HMM (Viterbi, beam 12, ≤ 24 candidates per step) that snaps the
  drifting dead-reckoned position onto a road. It departs from Newson & Krumm deliberately: their
  transition term needs route distance through a graph, and here fixes arrive every couple of
  seconds, where consecutive candidates are almost always on the same or an adjacent segment.
  What replaces it is a **heading term** — and that is the point rather than a consolation, since
  heading is precisely the observation the IMU cannot supply for itself.
  It declines to snap at all below 25 m of modelled uncertainty, because the map is not more
  accurate than a well-aided integrator: measured, snapping every fix moved mean error from 4.85 m
  to 9.85 m; gating brought it to 5.92 m and raised the share of helpful snaps during a simulated
  outage from 61% to 95%.
  Matched road bearing is fed back into the integrator at gain 0.35, capped at 4° per update, and
  only above 0.6 confidence — a safety valve on a feedback loop that would otherwise believe one
  wrong road and then keep agreeing with itself.
- **`AlongRoadTracker`** goes further: once confidently on a road, the road supplies *direction*
  and the integrator only supplies *distance*. It is **off by default**, and the reason is honest
  — given true distance it cuts 60 s outage error nearly in half (87.5 m → 50.5 m), but fed the
  integrator's own distance it is 20% *worse*, because free-running accumulates ~37% less distance
  than was actually travelled. Finished machinery waiting on a trustworthy distance channel.

On the recorded session the matcher produced 104 matches across `primary`, `secondary`,
`tertiary`, `residential`, `service` and `unclassified` roads, with a **median correction of
8.2 m** (p90 34.1 m, max 55.8 m) and **median confidence 0.92**.

---

## 8. The user interface

### 8.1 Principle

**The panel is diagnostics; the map is the app.** The session panel is collapsed to a single
button by default so the map gets the whole screen — and that button carries the record state as
its tint, so nothing important is hidden by being collapsed.

### 8.2 Screen anatomy

```
┌────────────────────────────────────────────────┐
│ [Recording] [GNSS good]        [☾] [⚙]         │  top bar, window-inset aware
│ [No offline map here · Download Eastern zone]  │  coverage hint (conditional)
│                                                │
│                  MAP                           │  osmdroid, overlays:
│         ── blue: GPS track                     │   snapLine, trackLine, drLine,
│         ┄┄ orange dashed: IMU-only track       │   drMarker, marker, my-location
│         ━━ green: snapped-to-road track        │   + quality repaint overlays
│                                                │
│                                       (◎)      │  centre-on-me FAB (mini)
│                                       (▤)      │  panel FAB, tinted by state
│ ┌────────────────────────────────────────────┐ │
│ │ elapsed   imu    fixes   sats              │ │
│ │  3:42    276.4k   237    25/39             │ │
│ │                                            │ │
│ │ drift                    ■ GPS · 43 km/h   │ │
│ │ 128 m                    ■ Snapped · 8 m   │ │
│ │ 4.1% of 3.12 km ✓ under 10%  ■ IMU·41km/h  │ │
│ │                                            │ │
│ │ 200 Hz · C/N0 31 dB-Hz · fix 1s ago · ±6 m │ │
│ │ Map matching · eastern-zone.map            │ │
│ │ ┌────────── Stop recording ─────────────┐  │ │
│ │ └───── Free-run (simulate tunnel) ──────┘  │ │
│ └────────────────────────────────────────────┘ │
└────────────────────────────────────────────────┘
```

### 8.3 Controls

| Control | Behaviour |
| --- | --- |
| **Start / Stop recording** | Starts the foreground service; the label becomes *Grant location permission* when the permission is missing |
| **Free-run (simulate tunnel)** | Withholds GNSS from the integrator on purpose, so the tunnel case can be demonstrated on an open road. Turns the panel FAB red. |
| **Centre-on-me FAB** | Frames a 1 km radius — but only *tightens* if the map is currently showing more than that, because a closer view is treated as deliberate |
| **Panel FAB** | Expand/collapse the diagnostics card with a 180 ms transition; tinted blue while recording, red on free-run or error |
| **Theme button** | Day/night Mapsforge render theme; rebuilds the tile provider in place |
| **Settings (⚙)** | Bottom sheet: zone list with live download progress, plus a collapsed *Advanced* group for the offline toggle, tile-URL template and preload |
| **Map drag** | Any manual pan sets `followPosition = false` — the camera stops yanking the user back |
| **Notification** | Carries a **Stop** action, so the phone can sit face-down with the screen off for the whole drive |

### 8.4 Live feedback, and where it comes from

- **Drift metric**, colour-coded against the project's < 10% benchmark: green under 5%, amber to
  10%, red beyond — falling back to absolute thresholds (25 m / 100 m) until at least 50 m has
  been travelled, below which the ratio is dominated by GNSS noise rather than by real error. The
  subtitle spells it out: *"4.1% of 3.12 km ✓ under 10%"*.
- **GNSS chip**: good / weak / lost / idle, derived in `LoggerStatus` rather than in the UI, so
  the display and the filter can never disagree about what the constellation is doing.
- **The track legend doubles as the speed readout** — *GPS · 43 km/h*, *IMU · 41 km/h*,
  *Snapped · 8 m (92%)*. Folding live speed into the legend gives the most legible real-time proof
  that something is working, without another row of chrome.
- **Track quality shading**: contiguous runs of weak/lost fixes are drawn as their own overlays on
  top of the base polyline, starting one point early so they visually join the healthy track on
  either side. osmdroid has no multi-coloured polyline; this is how the *cause* of a divergence
  gets drawn, not just the divergence.
- **Coverage hint chip**: position known, but no installed map contains it. This is the confusion
  the app used to leave unexplained — the GNSS chip says "good", the map says nothing. Tapping it
  offers the zone that probably covers you.
- **Empty-state card**: shown only when offline mode genuinely has nothing on disk (online mode
  can still fetch, so it is not "blank"). It links straight to the zone list rather than quoting a
  filesystem path at the user — though the path is there, in muted text, for whoever wants to
  `adb push`.
- **Snackbar with a View action** after a zone installs, because the map only reframes itself when
  there is nothing better to show; without it a map downloaded mid-session is installed but
  invisible.

### 8.5 Details that matter more than they look

- **Status is published on the 2 s tick, never per sensor event.** Publishing from the gyro
  callback would allocate a status object and wake the UI collector **200 times a second**. The
  value is cached into a field and published by the periodic task. This is a documented
  must-not-regress.
- **Track and status are separate StateFlows**, so a two-second counter refresh does not force the
  map to rebuild a polyline that has not changed — and the track survives on screen after
  recording stops.
- **The UI follows the service, not the last tap.** If recording stops on its own, the button and
  chips correct themselves; state is collected under `repeatOnLifecycle(STARTED)`.
- **Window insets, not a hardcoded 48 dp.** The status bar is a different height on other devices
  and in landscape; hardcoding clips the chips or leaves them floating.
- **Dashed IMU track**, so the two tracks stay distinguishable where they overlap and for anyone
  who cannot separate blue from orange.
- **The settings sheet polls only while a download is actually in flight.** DownloadManager
  exposes no progress callback; ticking unconditionally would keep the window from ever going
  idle, burning battery and blocking UI automation from settling.
- **Markers rotate with the device azimuth**, taken from the same rotation-vector attitude the
  integrator navigates on.

---

## 9. What lands on disk

One directory per session:
`Android/data/com.example.imulogger/files/sessions/<yyyyMMdd_HHmmss>/`

| File | Contents | Rate |
| --- | --- | --- |
| `imu.csv` | `t_ns,sensor,accuracy,v0..v5` — long format, one row per sample | ~1,312 rows/s |
| `gps.csv` | `t_ns,utc_ms,provider,lat,lon,alt_m,speed_mps,bearing_deg,acc_m,vert_acc_m,speed_acc_mps,bearing_acc_deg` | ~0.6 Hz |
| `gnss_status.csv` | `t_ns,sats_visible,sats_used,mean_cn0_used_dbhz,max_cn0_dbhz` | per epoch |
| `gnss_raw.csv` | receiver clock + 13 per-satellite fields (Doppler, code phase, carrier phase, C/N0, multipath) | per sat per epoch |
| `gnss_nav.csv` | `t_ns,type,svid,message_id,submessage_id,status,data_base64` | as broadcast |
| `deadreckon.csv` | `t_ns,lat,lon,speed_mps,drift_m,bias_e,bias_n,bias_u,stationary,free_run,gyro_bias_x/y/z,gyro_bias_valid` | 10 Hz |
| `mapmatch.csv` | `t_ns,dr_lat,dr_lon,snap_lat,snap_lon,correction_m,road_class,confidence,heading_applied_deg,mode` | per snap |
| `ml.csv` | `t_ns,mu,logvar,stationary_logit,yaw_rate` — observation only | 10 Hz |
| `session.json` | Device, full per-sensor inventory, clock sync, end-of-run summary | written twice (start + finish) |

**Format decisions.**

- Unused columns are left **empty rather than zero-filled**, so `pandas.read_csv` gives `NaN` and
  never mistakes a missing axis for a real zero. Same for absent GNSS clock fields, where `0` is a
  legal value and a sentinel would be indistinguishable from a real reading.
- `gnss_raw.csv` repeats the receiver clock on every row rather than using a sidecar. It *is*
  redundant — but a pseudorange rate is meaningless without the clock drift it was measured
  against, and one self-contained row per observable is far harder to mis-join later than two
  files and a timestamp.
- Long format for `imu.csv` rather than a wide join, because streams arrive at different rates and
  any wide format would have to invent an alignment.

**Volume.** 37.1 MB of `imu.csv` for 383 s ≈ **97 KB/s ≈ 350 MB/hour**; every other file together
is under 2% of that. Writers are 64 KB buffered and flushed every 2 s, so **a crash or a battery
pull costs at most two seconds of data**.

**Pull a session:**

```bash
adb pull /sdcard/Android/data/com.example.imulogger/files/sessions
```

---

## 10. Reliability

| Failure | Handling |
| --- | --- |
| App killed / battery pulled | 2 s flush cadence; ≤ 2 s of data lost |
| Screen off, phone in a pocket | Foreground service + `PARTIAL_WAKE_LOCK` (12 h cap) |
| Process death during a session | `START_STICKY`; the `sessionActive` guard is set **synchronously on the main thread** so a restart with a null intent cannot double-register every listener |
| Double-tap on Start | Same guard — the published status cannot serve as one, because the session is set up asynchronously on the logger thread |
| Location switched off mid-drive | Subscriptions dropped and re-established; broadcast + 2 s poll; the count is recorded in `session.json` |
| Location off *at start* | Same path — this is the case that used to blind an entire drive silently |
| `getExternalFilesDir()` returns null | Falls back to internal storage rather than throwing inside the writer |
| A sensor is absent on the device | Logged and skipped; the session continues with whatever exists |
| Raw GNSS refused by the chipset | Recorded as `gnss_raw_supported: false`, distinct from "supported but silent" |
| Corrupt / truncated `.map` | Magic-byte check on install; the renderer never sees a `.part`; a corrupt file degrades to raster tiles |
| Model fails to load | Caught; the session records everything else and simply never writes `ml.csv` rows |
| Inference falls behind | Requests dropped past 8 pending and counted, rather than queued — a backlog only produces staler predictions |
| Write error | Counted in `write_errors`, surfaced in the UI and in `session.json`; only the first is logged, so a failing disk cannot spam logcat |
| Permission denied | The service refuses to start and reports through the status flow; it stops well inside the 5 s `startForeground` window to avoid `ForegroundServiceDidNotStartInTimeException` |

**Allocation discipline on the hot path.** A single reused `StringBuilder(160)` for row
formatting, a reused `FloatArray(9)` rotation matrix and `FloatArray(3)` scratch buffers — because
`getRotationMatrix` runs on every gyro sample, and allocating there is 600 short-lived objects a
second straight into the GC.

---

## 11. Permissions and privacy

| Permission | Type | Why |
| --- | --- | --- |
| `ACCESS_FINE_LOCATION` | runtime | GNSS fixes; also a hard requirement for a `location` foreground service |
| `ACCESS_COARSE_LOCATION` | runtime | Requested alongside; Android requires the pair |
| `POST_NOTIFICATIONS` | runtime (API 33+) | The ongoing recording notification |
| `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_LOCATION` | install | Recording with the screen off |
| `HIGH_SAMPLING_RATE_SENSORS` | install | Anything above 200 Hz requires it |
| `WAKE_LOCK` | install | Keeps sensor delivery from becoming lossy with the screen off |
| `INTERNET`, `ACCESS_NETWORK_STATE` | install | **Map tiles and map downloads only** — never sensor or location data |

Required hardware features are declared: accelerometer, gyroscope, GPS.

**Privacy posture.** No storage permission (output is app-scoped). No backup
(`allowBackup="false"`). No analytics, no crash reporter, no backend, no account. The data never
leaves the phone unless the operator pulls it over ADB.

---

## 12. Build and deployment

```bash
./gradlew installDebug
```

Needs the SDK path in `local.properties`; the Gradle wrapper pulls Gradle 9.7.1 on first run.

`noCompress += listOf("map", "pt")` leaves the Mapsforge map and the TorchScript model **stored,
not DEFLATE'd**. Both are already compact binary formats, compressing them only inflates build
time, and an uncompressed asset is the one form `AssetManager.openFd` can measure without a full
read — which is exactly what the extraction guard needs.

**The APK size trade-off, stated honestly.** `eastern-zone.map` is bundled in `assets/`
(210.5 MiB), which makes the APK **490 MB debug / 487 MB release**, and it is then *extracted* to
the app's files directory on first launch — so the device carries roughly 420 MB for one zone
while both copies exist. The alternative, and the right route for anything but a demo build, is to
ship a lean APK and let the in-app downloader fetch the zone: identical behaviour, a few MB of
APK, and the user picks their own region. The download path is fully implemented; bundling exists
so a demo works on a phone that has never seen a network.

The map is deliberately **not in the repo**: GitHub rejects blobs over 100 MB, and Git LFS's free
tier is 1 GB of storage *and* 1 GB of bandwidth per month — four clones of a 210 MB file exhaust
the monthly allowance and then block everyone's pushes until it resets. It ships as a release
asset instead (2 GB limit, no bandwidth quota, and it stays out of everyone's clone).

---

## 13. Known gaps on the application side

Honest list, app layer only:

1. **`README.md`'s output table is out of date.** It documents 6 files; the app writes 9 —
   `gnss_raw.csv`, `gnss_nav.csv` and `mapmatch.csv` are missing from it. It also says "three
   threads" where there are now four.
2. **The 15 s start-up drop is undocumented in the README**, and it means `imu.csv` does not begin
   at `session_start_elapsed_realtime_ns`.
3. **The fused provider delivers ~0.6 Hz, not the 1 Hz requested.** Not a defect, but consumers
   must not assume 1 Hz.
4. **`AlongRoadTracker` and model speed fusion are both gated off**, correctly — which means the
   shipped app's navigation path is dead reckoning plus HMM heading feedback, and nothing else.
5. **No automated UI or instrumentation tests.** The only test is `MapsforgeTest`.
6. **No JNI boundary and no WebSocket client**, where the wider execution plan expects the Android
   app to be a thin polling layer over a C++ engine. This app does everything in-process in
   Kotlin. Both are defensible; it needs a team decision before integration, not at merge time.

---

## Appendix — the reference session

`extracted_sessions/20260904_195146`

| | |
| --- | --- |
| Device | Samsung SM-G990E (`r9s`), Android 16, SDK 36 |
| Duration | 398.4 s |
| IMU samples | 502,869 across 11 streams (1,312/s) |
| GPS fixes | 237 (0.59 Hz), median accuracy 6.0 m, max speed 14.9 m/s |
| Satellites | up to 39 visible, median 25 used, 5 constellations |
| Dead-reckoning rows | 3,758 at 10 Hz |
| Map matches | 104; median correction 8.2 m, median confidence 0.92 |
| Write errors | **0** |
| `imu.csv` | 37.1 MB |
