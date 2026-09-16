# IMU Logger — Presentation Script (application side)

> **Presenting one slide only?** Use **§0** below and stop there. The rest of this document
> is the long-form version, kept for a longer session or as your Q&A backing.
>
> Build the slide with:
> ```bash
> python docs/build_app_flowchart.py && python docs/capture_screens.py && python docs/build_app_slide.py
> ```

---

## §0. THE SINGLE-SLIDE SCRIPT — 3 minutes

The slide is `docs/APP_SLIDE.pptx`: flowchart left, phone screenshots top right, three
numbers under them. Talk it **left to right, once**, then point at the phone. Do not read
the boxes aloud — the audience can read; your job is the *why*.

**[0:00 — the frame]** *(stand back from the slide, don't point yet)*

> "This is the application layer of our dead-reckoning work — the part that runs on the
> phone, in the car, with the radio switched off. One activity, one service, four threads.
> Everything on this slide happens on the device."

**[0:20 — the left column, inputs]** *(point at the three input boxes in turn)*

> "Three inputs. **Eleven sensor streams** — thirteen hundred samples a second, accelerometer
> and gyro at 200 hertz, and note we take both the *calibrated* and the *uncalibrated* version
> of each, because Android silently subtracts a bias it estimated itself and a filter that
> estimates its own bias needs to see what the OS did.
>
> **Four GNSS subscriptions**, not one — the fix, but also satellite count and signal strength,
> and the raw per-satellite Doppler. And **one map file**, on the phone. No tile server, no key."

**[1:00 — the middle, the service]** *(trace top to bottom)*

> "It all lands on **one thread**. Every sensor callback, every GNSS callback, every file
> write — one writer, which is why there isn't a lock anywhere in this app. And one monotonic
> clock, so IMU and GPS align by joining on a column instead of estimating an offset.
>
> Then it splits. **Dead reckoning** integrates the IMU. The **trust gate** decides whether GPS
> is allowed to correct it — four satellites, twenty metres, or it free-runs. And **map
> matching** snaps the result to a road, using a road graph we rebuild *out of that same map
> file* — no second download, no routing server."

**[2:00 — the right, outputs, and the phone]** *(point to outputs, then to the screenshots)*

> "Out comes nine files a session, flushed every two seconds — a crash costs you two seconds of
> data — and this."

*(Point at the screenshots. This is the part they'll remember.)*

> "That's the app in airplane mode. Blue is GPS. Dashed orange is the IMU on its own. When we
> withhold GPS on purpose — that button — you watch them come apart, and **that gap is the
> honest cost of a tunnel**, measured rather than claimed."

**[2:40 — the close]** *(the three numbers, then the footer line)*

> "Thirteen hundred samples a second. **Zero** API keys, zero backends. Two hundred and ten
> megabytes for both the basemap and the road network.
>
> It works in a tunnel because it never needed a server. Happy to take questions."

### If you get only 60 seconds

Say exactly this, pointing at the flowchart once and the phone once:

> "Phone-only offline navigation logger. Eleven sensors at thirteen hundred samples a second,
> four GNSS subscriptions, all on one monotonic clock, one writer thread, nine files out.
> The map and the road network are one file on the device — no API key, no server, nothing to
> call at runtime. And this button withholds GPS on purpose, so the gap between the blue track
> and the orange one is the measured cost of losing GPS, not an estimate of it."

### Pointing map — where to put your hand

| When you say | Point at |
| --- | --- |
| "eleven sensor streams" | top-left input box |
| "four subscriptions, not one" | middle-left input box |
| "one map file, no key" | bottom-left input box |
| "one thread, one clock" | the blue band across the service |
| "trust gate" | the dashed brown box |
| "same map file" | draw your finger from the map box to the green box |
| "nine files, two seconds" | top-right output box |
| "watch them come apart" | the screenshots — and hold there |

### Anticipate these three, they come every time

1. **"Why not Google Maps?"** — No offline tile access, caching prohibited by their terms, and
   it needs a billing account. Any one of those kills it for an app about tunnels.
2. **"Is the ML model driving this?"** — No. It runs, it's logged, it's displayed, and it is
   deliberately not in the navigation path until a checkpoint beats a constant baseline.
3. **"Does it really need no internet?"** — Not at runtime. Once, ahead of time, to download the
   region. After that, never.

Everything else: `docs/APP_FAQ.md`, 70 questions.

---

## The long-form version (10 min + demo)

**Runtime:** 10 minutes spoken + 3–4 minutes demo + Q&A.
**Audience:** technical evaluators / faculty / judges.
**What this covers:** the app. The model gets one honest sentence and no more.

Conventions below: **[SLIDE n]** marks the slide change, *(italics)* are stage directions, and
bold numbers are the ones worth saying slowly because someone will write them down.

---

## Before you walk in — 5-minute checklist

| Check | Why |
| --- | --- |
| Phone charged > 60%, screen timeout ≥ 5 min | The demo runs a wake lock; a dead phone ends the talk |
| `eastern-zone.map` installed (Settings → Eastern shows *Installed*) | Otherwise the map is blank and the story collapses |
| Location master switch **ON**, precise location granted | Saves 30 s of fumbling |
| Airplane mode **ON** | This is the demo. Everything still works. |
| One previous session on the device | Fallback if live GNSS fails indoors |
| `extracted_sessions/20260904_195146` open on the laptop | Fallback screenshots and the real numbers |
| App killed and relaunched once | Clean state, no leftover free-run flag |

**The one-line framing to have ready if someone stops you at any point:**
*"It's a phone-only, fully offline navigation logger — no API keys, no server, no internet — that
shows you live what dead reckoning costs when GPS goes away, and writes the raw evidence to disk
while it does."*

---

## [SLIDE 1] Title — 20 seconds

> "IMU Logger. This is the application layer of our dead-reckoning work — the part that runs on
> the phone, in the car, with the radio off.
>
> I'm going to talk about four things: **the sensors**, **the map stack**, **the interface**, and
> **what lands on disk**. I'll spend about a minute on the model at the end, because on the
> application side it is deliberately a passenger, not a driver."

---

## [SLIDE 2] The problem in one picture — 45 seconds

*(Slide: a road going into a tunnel, GPS track stopping at the mouth.)*

> "GPS stops in a tunnel. Everyone knows that. What almost nobody can tell you is **how wrong you
> get, how fast** — because to know that you need the inertial data and the GPS ground truth on
> the *same clock*, recorded from the *same device*, at a rate that's actually useful.
>
> That instrument didn't exist for us, so we built it. And once you have it, you may as well let
> it navigate at the same time — so the app does both. It records the evidence and it runs the
> dead reckoning, side by side, and it draws you the gap."

---

## [SLIDE 3] What the app is — 40 seconds

> "One activity, one foreground service, fourteen Kotlin files, about five thousand lines.
> Four threads.
>
> **Zero API keys.** Not 'we removed them for the demo' — there is nowhere in this app to put one.
> No billing account, no quota, no rate limit, no vendor who can change their terms on us. I'll
> come back to that, because it's the decision I'd defend hardest."

---

## [SLIDE 4] Sensors — 2 minutes *(the meat)*

*(Slide: the eleven-stream table.)*

> "Eleven sensor streams. **Thirteen hundred samples a second**, aggregate.
>
> Accelerometer and gyroscope at **200 hertz** — that's the primary input, and 200 is where a
> consumer sensor hub actually delivers rather than where the API lets you ask.
>
> Now the parts that get asked about.
>
> **Why calibrated *and* uncalibrated?** Because Android silently subtracts a bias it estimates
> itself. If you're building a filter that estimates its own bias states, you need the raw signal
> *plus* what the OS thinks the bias is — otherwise the OS has already 'helped' you behind your
> back and you can't tell what you're looking at. Four extra streams, and the OS's own correction
> becomes something we can measure instead of something we have to trust.
>
> **Why two rotation vectors?** One includes the magnetometer, one doesn't. A steel car body
> distorts the magnetic field — so the one *without* the magnetometer doesn't swing when you drive
> past a truck, and the one *with* it gives you absolute heading. Recording both makes the
> disturbance measurable rather than assumed. We navigate on the rotation vector, and we measured
> that: **10.8 degrees RMS against the road, versus 12.6** for the magnetometer matrix. Better
> *and* it drops the dependency on the sensor a vehicle corrupts.
>
> **A barometer, at 10 hertz.** There is a pressure step at a tunnel mouth. That is free evidence
> and we take it.
>
> One number I want to put on record because it's a lesson, not a boast —"

*(Point at the Requested vs Measured columns.)*

> "**We asked for 50 hertz on the rotation vector and got 100. We asked for 25 on the barometer
> and got 10.** Sample rates in Android are a *hint*, not a contract; you get what the hardware's
> minimum delay and the hub's scheduler give you. That's exactly why every single row carries its
> own timestamp, and why we write the full sensor inventory — vendor, resolution, range, FIFO
> depth, minimum delay — into `session.json` for every session. Nothing downstream is allowed to
> assume a rate."

*(If time is tight, cut everything after "…a tunnel mouth" and keep the rate lesson.)*

---

## [SLIDE 5] The timebase — 45 seconds *(do not skip this)*

> "This is the slide I'd keep if I could only keep one.
>
> Every timestamp in every file is `elapsedRealtimeNanos` — the **monotonic** clock. Same clock as
> the sensor events, same clock as the GPS fixes.
>
> Two consequences. One: IMU and GPS align **with no offset estimation at all** — you just join
> on the column. Two: when the phone syncs its clock over NTP mid-drive, wall time jumps, and if
> you'd stamped rows with wall time your `dt` goes *backwards* and your integrator produces
> nonsense. Ours can't.
>
> And we write one wall-clock pair into `session.json`, so you can still tie the whole trace to UTC
> afterwards if you need to."

---

## [SLIDE 6] GNSS — 1 minute 15

> "Four separate GNSS subscriptions, not one.
>
> The fused provider gives us position. `GnssStatus` gives us **satellite count and signal
> strength** — and that's the interesting one, because **those collapse *before* the provider
> stops giving you fixes**. It's the earliest warning you can get that you're entering a tunnel,
> and a much better trigger than waiting for a fix to time out. We use it directly: a fix only
> re-anchors the integrator if **four or more satellites are used and accuracy is under 20 metres**.
>
> On the test device: up to **39 satellites visible, median 25 used**, across five constellations
> — GPS, GLONASS, Galileo, BeiDou and QZSS.
>
> We also log **raw per-satellite measurements** — Doppler, carrier phase, the receiver clock —
> and the broadcast navigation messages. Nothing reads them yet. They're there because a *position*
> fix needs four satellites, but a *velocity* fix from Doppler needs far fewer once you apply the
> vehicle's own constraints, and we want to test that against real hardware instead of a
> simulation.
>
> One robustness note that cost us a whole drive to find: if you start recording with Location
> switched off, every callback refuses and **nothing ever retries**. So subscription is now a
> *state we maintain*, not an event that happened — a system broadcast for speed, plus a poll every
> two seconds, because some OEM builds throttle broadcasts to background apps. And we count the
> re-subscriptions into `session.json` so you can see afterwards that it recovered."

---

## [SLIDE 7] No API keys — 1 minute *(the differentiator — say it slowly)*

> "Here is every path this app can take to the outside world.
>
> Downloading a map file: **no key**. Rendering the map: **no network at all**. Getting the road
> network for map-matching: **the same file, no key**. Positioning: the platform.
>
> Not one credential anywhere in the app.
>
> Why not Google Maps? Three independent blockers, any one of which kills it: the SDK gives you
> **no offline tile access**, caching tiles is **prohibited by their terms**, and it needs a
> **billing account** regardless of usage.
>
> And a distinction people get wrong: OpenStreetMap **data** is ODbL, free, and explicitly meant to
> be downloaded in bulk. OpenStreetMap's **public tile servers** are donated capacity and forbid
> bulk downloading. Only the second is restricted, and we use the first. Where we *do* touch the
> tile path, we respect the policy rather than routing around it — osmdroid throws rather than bulk
> fetch, and instead of working around that we ask the user to bring a source they're entitled to
> use.
>
> The practical version: **this app cannot be broken by a vendor changing their pricing.**"

---

## [SLIDE 8] The map stack — 1 minute 30

> "The map is Mapsforge vector data, rendered on the phone.
>
> A `.map` file is OpenStreetMap data compiled into a vector format. The phone rasterises it
> locally. There is no tile server, no key, no usage policy, and no cache to keep warm — and it's
> **smaller than a raster cache of a single city** while covering four states. Two hundred and ten
> megabytes gives us West Bengal, Jharkhand, Odisha and Bihar, at every zoom level, forever.
>
> Downloads go through Android's own `DownloadManager` — those files are up to half a gigabyte and
> the user will background the app long before one finishes. We get resume, notification, and
> survival across process death for free. And we check the **Mapsforge magic bytes** before the
> file is promoted, so a captive-portal login page or a half-finished transfer is thrown away
> *there*, rather than showing up an hour later as a mysteriously blank map.
>
> Now the bit I like most —"

*(Beat.)*

> "**The road network for map-matching comes out of that same file.** No second download, no
> `.osm.pbf`, no routing server. The file is built for *rendering*, but every road keeps its OSM
> tags, so we can read the centrelines straight out of it.
>
> The catch is that the format stores no topology — roads are chopped at tile edges with no node
> identity. But Mapsforge quantises coordinates to about **eleven centimetres**, so two roads that
> shared a junction come back on *bit-identical* coordinates. Snap the endpoints to a half-metre
> grid and the graph rebuilds itself. Measured on twenty-five thousand segments: **98.5% in one
> connected component**, mean node degree 2.7. Tile-boundary damage heals for free.
>
> On top of that runs an HMM map matcher — and one design note: it **refuses to snap** when the
> position is already good. The map isn't more accurate than a well-aided integrator; a road
> centreline is a lane-width from where you actually drove. Snapping everything made our mean error
> *worse* — 4.85 metres to 9.85. Gating brought it to 5.9 and took the share of *helpful* snaps
> during an outage from 61% to 95%. **Fewer corrections, nearly all of them right.**"

---

## [SLIDE 9] The interface — 1 minute

> "The principle is: **the panel is diagnostics, the map is the app.** So the panel collapses to a
> single button and the map gets the whole screen — and that button is tinted with the record
> state, so nothing important is hidden by being collapsed.
>
> Three tracks on the map. **Blue is GPS. Dashed orange is the IMU on its own. Green is snapped to
> the road.** Dashed, not just a different colour, so they stay separable where they overlap and
> for anyone who can't tell blue from orange.
>
> The headline number is **drift** — and it's shown as a *percentage of distance travelled*, not
> as bare metres, because a bare metre count means nothing until you know how far you went. It goes
> green under 5%, amber to 10%, red beyond, against our 10% target. And it refuses to show a
> percentage until you've travelled 50 metres, because below that you're measuring GPS noise, not
> error.
>
> The legend doubles as the speed readout — GPS speed and IMU speed side by side. That's the most
> legible live proof that the integrator is doing something, and it costs no extra chrome.
>
> Small thing, big consequence: the status panel updates **on a two-second tick, never per sample**.
> Publishing from the gyro callback would allocate an object and wake the UI **two hundred times a
> second**. That's in the code as a comment marked 'must not regress'."

---

## [SLIDE 10] What lands on disk — 45 seconds

> "Nine files per session. IMU, GPS, GNSS status, raw GNSS, navigation messages, dead reckoning,
> map matches, model outputs, and a JSON sidecar with the device and the full sensor inventory.
>
> **Three hundred and fifty megabytes an hour**, flushed every two seconds — so a crash or a
> battery pull costs you **at most two seconds of data**.
>
> One format decision worth mentioning: unused columns are left **empty, not zero-filled**. Pandas
> reads them as NaN. If we'd written zeros, a missing axis and a genuine zero reading would be
> indistinguishable, and you'd never know which one you had."

---

## [SLIDE 11] The model, briefly — 30 seconds *(deliberately short)*

> "There's a TorchScript model on board. It runs on its own thread at 10 hertz, its outputs are
> written to `ml.csv`, and the UI shows them.
>
> **Nothing in the navigation path consumes them.** That's a switch in the code that is
> deliberately off, and the reason is in a comment with the measurement next to it: scored against
> GPS speed, the current checkpoint is worse than predicting a constant. So the plumbing is
> finished and correct, and it stays gated until a checkpoint earns it.
>
> I'd rather show you an app that knows what it can't do yet than one that quietly does it wrong."

---

## [DEMO] 3–4 minutes

*(Mirror the phone. If you can't mirror, hold it up and narrate — the panel numbers are large.)*

**Choreograph it in this order. Do not improvise the order; the story is in the sequence.**

1. **Airplane mode is on.** Say it out loud, show the status bar.
   > "Radio's off. Watch the map."

2. **Open the app.** The map is already there, framed on the installed zone.
   > "That's West Bengal, rendered on the phone, from a file. No tiles were fetched. There's
   > nothing to fetch."

3. **Pan and zoom in to street level.**
   > "Still no network. This goes all the way down to individual streets."

4. **Tap Start recording.**
   > "Foreground service, wake lock, eleven sensors, four GNSS subscriptions."
   *(Expand the panel.)*
   > "Sample count climbing, satellites in view, fix age, accuracy."

5. **Wait for the GNSS chip to go green and the blue track to start.**
   > "Blue is GPS. Behind it, the orange dashed line is the IMU integrating on its own — right now
   > they're on top of each other, because GPS keeps re-anchoring the integrator."

6. **Tap Free-run (simulate tunnel).** *(The panel FAB turns red.)*
   > "That withholds GPS from the integrator on purpose. We're now in a tunnel as far as the
   > navigation is concerned — but we still have the real GPS track underneath as ground truth, so
   > you can *see* the error instead of guessing at it. That's the whole point of the button: you
   > don't need to find a tunnel to demonstrate a tunnel."

7. **Walk / drive 20–30 seconds.** Point at the two tracks separating and at the drift number.
   > "There it is. That gap is the honest answer to 'what does losing GPS cost you'."

8. **Tap Free-run again to re-anchor.**
   > "And it snaps back the moment a healthy fix is allowed in — four satellites, under twenty
   > metres."

9. **Tap Stop.** Show the session path in the panel.
   > "That's a directory of nine files, on disk, ready to pull over ADB. Zero write errors."

**Demo fallbacks — decide now, not on stage:**

| If | Do |
| --- | --- |
| No GNSS fix indoors | Skip to the recorded session screenshots. *"This is the same screen from a real drive."* |
| Map is blank | Settings → check the Eastern zone row. If not installed, show the empty-state card and say the download is a 210 MB one-time fetch. |
| Someone asks to see it fail | Turn Location off mid-session, then back on. It recovers, and `session.json` counts the re-subscription. |
| Time is short | Cut steps 3 and 8. Never cut 6 and 7. |

---

## [SLIDE 12] Close — 30 seconds

> "So: an offline-first Android instrument. **Eleven sensors at thirteen hundred samples a second,
> on one monotonic clock. Zero API keys. A map and a road network from a single file, with no
> server behind either. And a live, honest read of what dead reckoning costs you.**
>
> The thing I'd most want you to take away is that we can *show* you the error rather than assert
> it — and everything we've claimed here is in a CSV on that phone. Happy to take questions."

---

## Timing card *(tear this off and hold it)*

| Slide | Topic | Target | Running |
| --- | --- | --- | --- |
| 1 | Title | 0:20 | 0:20 |
| 2 | Problem | 0:45 | 1:05 |
| 3 | What it is | 0:40 | 1:45 |
| 4 | **Sensors** | 2:00 | 3:45 |
| 5 | Timebase | 0:45 | 4:30 |
| 6 | GNSS | 1:15 | 5:45 |
| 7 | **No API keys** | 1:00 | 6:45 |
| 8 | **Map stack** | 1:30 | 8:15 |
| 9 | UI | 1:00 | 9:15 |
| 10 | Data on disk | 0:45 | 10:00 |
| 11 | Model (short) | 0:30 | 10:30 |
| — | **Demo** | 3:30 | 14:00 |
| 12 | Close | 0:30 | 14:30 |

**If you are given 5 minutes instead of 15:** slides 3, 4 (rate lesson only), 7, 8 (road-network
half only), then demo steps 4–7. Nothing else.

---

## Six numbers to memorise

You will be asked for at least three of these, and reaching for notes costs you more than getting
one slightly wrong.

1. **1,312 samples per second**, 11 streams, 502,869 samples in a 398-second session.
2. **Zero API keys**, zero backend, zero analytics.
3. **210 MB** covers four states; the road network comes out of the *same* file.
4. **98.5%** of road segments land in one connected component after the 0.5 m endpoint snap.
5. **350 MB per hour**, flushed every **2 seconds** — max 2 seconds of data lost on a crash.
6. **10.8° RMS** heading against the road on the rotation vector, versus 12.6° on the magnetometer.
