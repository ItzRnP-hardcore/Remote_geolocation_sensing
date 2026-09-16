# IMU Logger — Presentation FAQ (application side only)

Answers are written the way you'd say them out loud. **Bold** is the answer; the rest is the
backup you use only if they push. Where a question has a number attached, the number is real and
comes from the source or from `extracted_sessions/20260904_195146`.

Ground rule for the whole Q&A: **if you don't know, say the measurement you'd run.** This project's
credibility is that everything claimed has a number behind it — protect that.

---

## A. Sensors

**Q1. Which sensors do you record, and at what rate?**
**Eleven streams, 1,312 samples a second in total.** Accelerometer and gyroscope at 200 Hz, both
calibrated and uncalibrated; magnetometer at 50 Hz, calibrated and uncalibrated; two rotation
vectors, gravity and linear acceleration around 100 Hz; and a barometer at 10 Hz.

**Q2. Why record uncalibrated sensors as well? Isn't that duplicate data?**
**Because Android silently subtracts a bias it estimated itself.** If you're building a filter that
estimates its own bias states, you need the raw signal *plus* what the OS believes the bias is —
otherwise the OS has already corrected the signal behind your back and you can't separate its
correction from the physics. Four extra streams turn the OS's own behaviour into an observable.

**Q3. Why two rotation vectors?**
**One uses the magnetometer, one doesn't.** A steel vehicle body distorts the magnetic field, so
`GAME_ROTATION_VECTOR` — which excludes it — doesn't swing when you pass a truck, while
`ROTATION_VECTOR` gives you absolute heading. Recording both makes the disturbance measurable
instead of assumed. We navigate on the rotation vector, measured at **10.8° RMS against the road
versus 12.6°** for the magnetometer-derived matrix.

**Q4. Why a barometer? You're not flying.**
**There's a pressure step at a tunnel mouth**, and altitude change is otherwise the weakest channel
in GNSS. It costs 10 samples a second. It's free evidence.

**Q5. Do you actually get 200 Hz? Isn't Android's sensor API unreliable?**
**We measured it: 200.0 Hz for accel and gyro, sustained over 383 seconds.** But you're right to
ask, and here's the honest part — **requested rates are hints, not contracts.** We asked for 50 Hz
on the rotation vector and got 100. We asked for 25 Hz on the barometer and got 10. That's why
every row carries its own timestamp and why we record each sensor's hardware minimum delay in
`session.json`. Nothing downstream assumes a rate.

**Q6. Do you drop samples? How would you know?**
**We'd know from the timestamps, and we count what we wrote.** Sample counts and write errors go
into `session.json`; the reference session has **502,869 samples and zero write errors**. We also
let the sensor hub batch up to a second into its hardware FIFO before waking the CPU — batched
events keep their individual, correct timestamps, so batching costs latency, never accuracy.

**Q7. What happens when the screen goes off?**
**A foreground service plus a partial wake lock keeps it recording.** Without the wake lock the CPU
suspends and delivery becomes bursty and lossy — which matters because a phone on a dashboard has
its screen off almost the whole time. The lock has a 12-hour timeout as a safety valve against a
leak, and the notification carries a Stop action so you never have to unlock the phone.

**Q8. Does it work on any Android phone?**
**minSdk 26, so Android 8 and up.** Accelerometer, gyroscope and GPS are declared as required
features. Anything else missing is logged and skipped, and the session continues with what exists
— the sensor list is data, not code, so a phone without a barometer just has one fewer stream.

**Q9. Why does `imu.csv` start 15 seconds after the session does?**
**That's deliberate:** 5 seconds of hardware stabilisation and 10 of orientation settling are
dropped at the start. Worth flagging, because it means you can't naively join `session.json`'s
start timestamp to the first IMU row.

**Q10. What phone did you test on?**
**Samsung SM-G990E on Android 16** — STM LSM6DSO accelerometer and gyroscope, AKM AK09918C
magnetometer. All of that is recorded per session, with resolution, range and FIFO depth, precisely
so results can be compared across handsets rather than assumed to transfer.

---

## B. Time synchronisation

**Q11. How do you align IMU and GPS?**
**We don't have to — they're already on the same clock.** Every timestamp in every file is
`SystemClock.elapsedRealtimeNanos`, which is the same base as `SensorEvent.timestamp` and
`Location.getElapsedRealtimeNanos()`. You join on the column. No offset estimation, no
cross-correlation.

**Q12. Why not wall-clock time?**
**Because it jumps.** When the phone syncs over NTP mid-drive, wall time can go backwards — and
then your `dt` goes negative and the integrator produces nonsense. The monotonic clock can't. We
record one `(monotonic, UTC)` pair in `session.json` so the trace can still be tied to real-world
time afterwards.

---

## C. GNSS

**Q13. How many GNSS subscriptions do you have, and why more than one?**
**Four.** Fused position for the fix; `GnssStatus` for satellite count and signal strength; raw
measurements for per-satellite Doppler and carrier phase; and navigation messages for the broadcast
ephemeris. They answer different questions and only the first is available from a simple location
API.

**Q14. Why log satellite counts separately? The fix already tells you if it's good.**
**Because the count and the signal strength collapse *before* the fix does.** They are the earliest
available evidence you're entering a tunnel, and a much better trigger for shifting trust to the
IMU than waiting for a fix to time out. We use them directly: a fix only re-anchors the integrator
if **four or more satellites are used and accuracy is under 20 metres**.

**Q15. How many satellites do you actually see?**
**Up to 39 visible, median 25 used in a fix**, across five constellations — GPS, GLONASS, Galileo,
BeiDou and QZSS. Median horizontal accuracy on the reference session was 6 metres.

**Q16. Why record raw GNSS measurements if nothing reads them?**
**Because a velocity fix needs far fewer satellites than a position fix, and we want to test that
on real hardware.** Position needs four satellites for four unknowns; Doppler-based velocity needs
fewer once you apply the vehicle's own constraints. We have that result in simulation only, and
raw measurements are the file that makes validating it possible. It's honest logging ahead of a
consumer — and we record whether the chipset accepted the callback at all, because "the chip
withheld the data" and "we never registered" look identical from an empty CSV.

**Q17. What if the user starts recording with Location switched off?**
**It recovers, and this was a real bug we fixed.** Previously every callback refused and nothing
ever retried, so an entire drive recorded no fixes, silently. Now subscription is a state we
maintain: a system broadcast for the fast path, *plus* a poll every two seconds, because some OEM
builds — Samsung's included — throttle broadcasts to background processes. The number of
re-subscriptions is written into `session.json`, so you can see afterwards that it recovered.

**Q18. Your fused provider gives 0.59 Hz but you requested 1 Hz. Isn't that a bug?**
**No, but it's the right thing to notice.** The location request is a scheduling hint too; the
provider delivers when it has something worth delivering. We report the measured rate rather than
the requested one, and nothing downstream assumes 1 Hz.

---

## D. API keys, network and licensing

**Q19. What API keys does the app need?**
**None.** There is no key anywhere in the app and nowhere to put one. No billing account, no quota,
no rate limit, no key rotation, no leakage risk.

**Q20. Then how does the map work?**
**It renders from a file on the phone.** A Mapsforge `.map` file is OpenStreetMap data compiled to
a vector format; the phone rasterises it locally. There's no tile server behind it to consult — the
map view explicitly disables its data connection when a vector map is present.

**Q21. Isn't downloading OpenStreetMap data in bulk against their rules?**
**No — and this is the distinction people get wrong.** OSM *data* is ODbL, free, and explicitly
intended to be downloaded in bulk; that's what Geofabrik and Mapsforge builds exist for. OSM's
*public tile servers* are donated capacity and do forbid bulk downloading. Only the second is
restricted, and we use the first. Where we do touch the tile path we respect the policy rather than
routing around it — osmdroid flags those servers no-bulk and throws rather than fetch, and instead
of working around that we ask the user for a source they're entitled to use.

**Q22. Why not Google Maps?**
**Three independent blockers, any one of which is fatal.** The Maps SDK exposes no offline tile
access; caching tiles is prohibited by the Platform Terms; and it requires a billing account
regardless of usage. For an app whose entire premise is working in a tunnel, the first alone rules
it out.

**Q23. So it needs no internet at all?**
**Not at runtime.** It needs the network once, ahead of time, to download a region — and after that
never again. The recording path never touches the network under any circumstance. `INTERNET` is
declared only for map tiles and map downloads, and the manifest says exactly that next to the
declaration.

**Q24. Does any data leave the phone?**
**None.** No analytics SDK, no crash reporter, no backend, no account, and backup is disabled. The
data leaves only when the operator pulls it over ADB.

**Q25. What's the tile-URL box in Settings, then? That sounds like a key.**
**It's optional, it's the user's own, and it exists for the raster fallback only.** If someone has
no vector map and wants to pre-cache raster tiles, OSM's servers won't allow it — so we ask them to
paste a template from a provider whose terms *do* permit caching. With a vector map installed, this
path is never used. The app ships no key of its own and never will.

---

## E. Maps and map matching

**Q26. How big is the offline map, and what does it cover?**
**210 MB covers West Bengal, Jharkhand, Odisha and Bihar** at full street detail, at every zoom.
Six India zones are offered in-app, from 112 MB for the north-east to 520 MB for the south.

**Q27. Why vector maps rather than caching tiles?**
**A vector file for a whole zone is smaller than a raster cache of one city.** That makes "preload a
radius" moot rather than merely solved, and it has no zoom ceiling and nothing to keep warm.

**Q28. What if the download is interrupted or corrupted?**
**It can't reach the renderer.** Downloads land as `.part`, and the renderer only ever looks for
`.map`. Before promotion we check the Mapsforge magic bytes, so a captive-portal login page or a
half-finished transfer is discarded at that moment rather than surfacing an hour later as a
mysteriously blank map. DownloadManager handles resume; a corrupt file that somehow got through
degrades to raster tiles rather than taking the map down.

**Q29. Where does the road network for map matching come from?**
**Out of the very same `.map` file.** No second download, no `.osm.pbf`, no routing server. The
file is built for rendering, but every way keeps its OSM tags, so `highway=*` centrelines come back
with full coordinates.

**Q30. Mapsforge doesn't store road topology. How do you route or track along roads?**
**We rebuild it from geometry.** The format clips ways at tile boundaries with no node identity —
but it quantises coordinates to microdegrees, about 11 cm, so two segments that shared a junction
come back on bit-identical coordinates. Snapping endpoints onto a half-metre grid rebuilds the
graph without node IDs. Measured on **25,383 segments: every segment gained a link, 98.5% landed in
one connected component**, mean node degree 2.73 — and tile-boundary damage heals for free.

**Q31. What map-matching algorithm do you use?**
**An online HMM with Viterbi and a beam** — the Newson & Krumm family, with one deliberate
departure. Their transition term uses route distance through a road graph; we use straight-line
distance plus a **heading** term. That's justified twice over: our fixes arrive every couple of
seconds, where consecutive candidates are almost always on the same or an adjacent segment, and
heading is precisely the observation an IMU can't supply for itself.

**Q32. Doesn't snapping to roads always help?**
**No, and we measured that it doesn't.** The map isn't more accurate than a well-aided integrator —
a road centreline sits a lane-width from where you actually drove, and OSM geometry has its own
error. Snapping every fix moved our mean error from **4.85 m to 9.85 m**. So the matcher now
*declines* to snap below 25 m of modelled uncertainty: that brought it to **5.92 m** and raised the
share of snaps that actually helped during a simulated outage from **61% to 95%**. Fewer
corrections, nearly all of them right.

**Q33. Doesn't feeding the road's heading back create a feedback loop?**
**Yes, and it's explicitly capped.** The matched bearing is fed back at gain 0.35, at most 4° per
update, and only when the winning hypothesis holds 60% of the beam. A wrong road has to be believed
repeatedly before it can do real damage, and a right one still pulls the heading in within a few
seconds. Position is deliberately *not* corrected — teleporting the integrator would destroy the
divergence the app exists to measure.

**Q34. What performance do you get from the matcher?**
On the reference session, **104 matches with a median correction of 8.2 m and median confidence
0.92**, across primary, secondary, tertiary, residential, service and unclassified roads.

**Q35. Doesn't reading the map file on every fix hurt the sample rate?**
**It can't — it's on its own thread.** Reading a Mapsforge tile is disk I/O, and the first read of a
new area parses a few hundred ways. Neither belongs on the 200 Hz logging thread. Results are
posted back to the logger thread to be written, so there's still exactly one writer.

---

## F. User interface

**Q36. Why is the diagnostics panel hidden by default?**
**The panel is diagnostics; the map is the app.** Collapsing it gives the map the whole screen —
and the button it collapses into carries the record state as its colour, so nothing important is
hidden by being hidden.

**Q37. What are the three lines on the map?**
**Blue is GPS. Dashed orange is the IMU integrating on its own. Green is the dead-reckoned track
snapped to the road.** The orange one is dashed rather than merely a different colour, so the two
stay separable where they overlap and for anyone who can't distinguish blue from orange.

**Q38. Why show drift as a percentage rather than in metres?**
**Because a bare metre count can't be judged without knowing how far you went.** 100 m of error over
200 m is a disaster; over 20 km it's excellent. The percentage is colour-coded against our 10%
target — and it refuses to display at all until you've travelled 50 metres, below which the ratio
is dominated by GNSS noise rather than by real error.

**Q39. What is the "Free-run" button for?**
**It withholds GPS from the integrator on purpose, so you can demonstrate a tunnel without finding
one.** Crucially the real GPS track keeps recording underneath as ground truth, so the growing gap
between the two lines *is* the measured error, not an estimate of it.

**Q40. How does the UI stay responsive at 1,300 samples a second?**
**The UI never sees a sample.** Status is published on a two-second tick, and the track is a
separate flow so a counter refresh doesn't rebuild a polyline that hasn't changed. Publishing from
the gyro callback would allocate a status object and wake the UI collector 200 times a second —
that's marked in the code as a regression that must not come back.

**Q41. What happens if the user has no map for where they are?**
**A chip tells them, and offers the right zone.** That was a genuine confusion in an earlier build:
the GNSS chip said "good" and the map said nothing, with no explanation. Now the app compares the
position against each installed map's header extent and, if none covers it, names the zone that
probably does.

**Q42. Is there a dark mode?**
**The map has day and night render themes**, toggled from the top bar — night is a bundled custom
theme with city labels shrunk so they stop dominating. The overlay UI itself stays dark in both, on
purpose: a light card floating over a light basemap loses all separation.

**Q43. Anything for accessibility?**
Content descriptions on every icon button, window-inset-aware layout instead of hardcoded padding,
large numeric readouts, and the dashed-versus-solid track distinction so colour isn't the only
channel. It hasn't been through a formal accessibility audit — that's fair to say plainly.

---

## G. Data and output

**Q44. What files come out of a session?**
**Nine**, in one directory named by timestamp: IMU, GPS, GNSS status, raw GNSS, GNSS navigation
messages, dead reckoning, map matches, model outputs, and a `session.json` sidecar carrying the
device, the full sensor inventory and the clock sync.

**Q45. How much data per hour?**
**About 350 MB**, almost all of it `imu.csv` — everything else together is under 2%.

**Q46. What if the app crashes mid-drive?**
**You lose at most two seconds.** Writers are 64 KB buffered and flushed on a two-second tick.
`session.json` is written at the start as well as the end, so even a session that never closed
cleanly still has its device and sensor metadata.

**Q47. Why CSV rather than a binary format or a database?**
**Because the consumer is a research pipeline, and CSV opens in pandas with no adapter.** At
350 MB/hour the write cost isn't the bottleneck, and the debuggability of being able to `head` a
file mid-drive is worth more than the bytes.

**Q48. Why is `imu.csv` in long format rather than one row per timestamp?**
**Because the streams arrive at different rates.** Any wide format would have to invent an
alignment between 200 Hz and 10 Hz channels — and inventing it in the logger is exactly the wrong
place to do it, since the analysis can then never see what was actually sampled.

**Q49. Why are unused columns empty instead of zero?**
**So that a missing axis and a genuine zero reading stay distinguishable.** Pandas reads an empty
field as NaN. Zero-filling would silently manufacture data — and for GNSS clock fields in
particular, zero is a perfectly legal value.

**Q50. How do you get the data off the phone?**
`adb pull /sdcard/Android/data/com.example.imulogger/files/sessions`. It's app-scoped storage, so no
storage permission is needed to write it.

---

## H. Permissions, battery and privacy

**Q51. What permissions does it need, and why?**
**Precise location** (GNSS fixes, and a hard requirement for a location-type foreground service),
**notifications** (the ongoing recording notification), plus install-time foreground-service, wake
lock, and high-sampling-rate-sensors — the last of which Android requires for anything above
200 Hz. `INTERNET` is for map tiles and map downloads only.

**Q52. Why no storage permission?**
**Because the output goes to app-scoped external storage**, which needs none. It's also why the
files are visible over ADB without root.

**Q53. What's the battery cost?**
It's meaningful — a wake lock, eleven sensors, continuous high-accuracy GNSS and a live map. Sensor
batching into the hardware FIFO is the main mitigation, since it lets the application processor stay
asleep for up to a second at a time. We haven't published a milliamp figure; the honest answer is
that we haven't measured it properly and would need a power monitor to say anything defensible.

**Q54. Is the user's location data safe?**
**It never leaves the phone.** No backend, no analytics, no crash reporter, no account, backup
disabled. Sensitive data goes to app-scoped storage that other apps can't read.

---

## I. Architecture and engineering

**Q55. How many threads, and why?**
**Four.** Main for the UI only. `imu-logger` owns every sensor, location and GNSS callback and every
file write. `map-match` reads the map file and runs the matcher. `imu-ml` runs inference. The
map-match and ML threads post their results *back* to the logger thread to be written.

**Q56. How do you handle concurrency between them?**
**We don't have to — there's exactly one writer.** Because every write happens on the logger thread,
there is not a single lock or synchronised block in the recording path, and no way to interleave a
half-written row. That's a design constraint, not an accident.

**Q57. Why not do inference on the logger thread?**
**A forward pass takes tens of milliseconds**, which would stall 200 Hz sensor delivery. And it
can't go on the main thread either — loading the model materialises a 15 MB asset and builds a
TorchScript module, which would risk an ANR. If inference falls behind, requests are **dropped past
8 pending and counted**, never queued: a backlog only produces staler predictions.

**Q58. What happens if the service is killed by the system?**
`START_STICKY` restarts it. The restart arrives with a null intent, which is why the session guard
is set **synchronously on the main thread** — otherwise a restart, or two fast taps on Start, would
register every listener twice.

**Q59. Why is the APK almost 500 MB?**
**Because a 210 MB map is bundled in assets for demo purposes**, and it's then extracted to the app
directory on first launch. That's a demo convenience, not the intended distribution: the in-app
downloader is fully implemented, so the real build ships a few-MB APK and the user picks their own
region. Worth saying plainly rather than being caught by it.

**Q60. Why isn't the map in the repository?**
**GitHub rejects blobs over 100 MB**, and Git LFS's free tier is 1 GB of storage *and* 1 GB of
bandwidth a month — four clones of a 210 MB file exhaust the allowance and then block everyone's
pushes until it resets. It ships as a release asset instead: 2 GB limit, no bandwidth quota, and it
stays out of everyone's clone.

**Q61. Is it tested?**
**Thinly, and I'd rather say so.** There's one unit test around Mapsforge, and no instrumentation or
UI tests. What we do have instead is measurement: every non-obvious decision in the codebase has a
number next to it from a recorded session, and the `eval/` scripts re-run those. For an instrument,
that's the more useful kind of evidence — but it isn't a substitute for a test suite.

---

## J. The ML thread — short answers, then redirect

**Q62. What is the ML thread, and what does it predict?**
**A temporal convolutional network running on-device**, over a 10-second window of IMU sampled at
10 Hz — 100 samples, 6 channels, exported as TorchScript. It predicts **speed and stationarity**,
which is deliberate: those are precisely the two quantities double integration is worst at, so the
network is aimed at the integrator's weakness rather than duplicating its strength.

**Q63. Is it steering the navigation right now?**
**It runs on the device, and its outputs are logged and displayed — but the fusion into the
position estimate is gated off in code.** Scored against GPS speed, the current checkpoint is worse
than predicting a constant, so switching it on today would make the app worse. The plumbing is
finished and correct; it stays gated until a checkpoint beats the baseline. *"It runs on-device,
and we gate it until it earns its place."* That's a stronger answer than the overclaim.

**Q64. Why keep the integrator at all, if you have a model?**
**Because they fail differently.** The strapdown integrator is excellent over short horizons and
drifts over long ones; the network is steadier but coarser. The architecture keeps both as
estimators and lets the GNSS trust gate arbitrate between them — that's why the slide shows three
boxes in that row, not one.

**Q65. What does the app do to host the model properly?**
Its own thread, so a ~40 ms forward pass never stalls 200 Hz sensor delivery and the model is never
loaded on the main thread; 10 Hz windows matching the training rate; inputs levelled with the same
rotation-vector attitude the integrator navigates on and the gyro debiased first, so run time
matches training; **drop-not-queue** backpressure past 8 pending requests; and every output written
to `ml.csv` beside GPS ground truth on the same clock — exactly what's needed to evaluate the next
checkpoint offline.

*(For anything beyond this — architecture, training data, accuracy — redirect: "that's the modelling
side, and my colleague can take that" or "that's in the separate model report.")*

---

## K. Awkward questions — have these ready

**Q66. "This is just a data logger. Where's the innovation?"**
**Three places, all in the app layer.** One: the road network for map matching is extracted from a
*rendering* file, so there's no second download and no routing server — that's what makes the whole
thing work offline. Two: we rebuild road topology the format doesn't store, from coordinate
quantisation alone, at 98.5% single-component. Three: the free-run control makes GNSS outage error
*measurable on an open road*, which is what turns this from a demo into an instrument.

**Q67. "Why build this instead of using an existing logging app?"**
**Because nothing existing gives you all four at once**: raw and calibrated IMU on one monotonic
clock, raw GNSS measurements, a live dead-reckoning comparison, and an offline map with a road
network. Most loggers give you the first. We needed the last three to say anything about tunnels.

**Q68. "Your drift numbers seem large."**
**They are, and that's the finding.** Nothing that integrates a consumer MEMS accelerometer twice is
a good navigator — the app exists to make that error *visible* so a real filter has something to be
tuned against. The integrator has no covariance anywhere in it, deliberately; it's the measurement
rig, not the filter.

**Q69. "What breaks first if I use this in production?"**
**Storage and battery.** 350 MB an hour fills a phone in a day of driving, and the wake lock plus
continuous high-accuracy GNSS is a real drain. Both are inherent to being a research instrument at
full rate; a production build would decimate the streams it doesn't need. The recording *correctness*
I'd defend as-is.

**Q70. "Does it work outside India?"**
**Yes — anywhere Mapsforge publishes a region**, which is most of the world. The six India zones in
the picker are a convenience list; any `.map` file dropped into the app folder works identically,
and the app merges multiple files into one view.

**Q71. "What would you do next on the app side?"**
**Three things, in order.** Ship the lean APK and stop bundling the map. Add instrumentation tests
around the session lifecycle, which is where our real bugs have been — the double-registration and
the Location-off case were both lifecycle bugs. And decide the integration boundary: the wider plan
expects the app to be a thin layer over a C++ engine, and today it does everything in-process in
Kotlin. That's a team decision, and it should happen before integration rather than at merge time.
