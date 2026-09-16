# Odyssey — app slide presentation script

One slide. Three lengths: **3 minutes** (full), **60 seconds** (the cut), **30 seconds** (the
emergency cut). Pick before you walk up, not while you're standing there.

Delivery rules, all three lengths:

- **Point, don't read.** Everything is written on the slide. Your voice carries the *why*.
- **Move left → middle → right → phone.** One direction, once. Backtracking loses people.
- **Land the last line and stop.** Don't trail off into "yeah, so, that's basically it".

---

## THE 3-MINUTE SCRIPT

### [0:00 – 0:15] Open — stand back from the slide, hands down

> "This is the Android application. The important thing about it is that **the entire navigation
> stack runs on the phone** — the sensing, the estimation, the map, and the road matching. There
> is no server anywhere in this diagram, and that is a design decision, not a limitation."

*Beat. Then step to the slide.*

### [0:15 – 0:45] Inputs — tap each of the three boxes as you name it

> "Three inputs.
>
> **Eleven IMU streams**, thirteen hundred samples a second. Accelerometer and gyroscope at two
> hundred hertz — and we record both the *calibrated* and the *uncalibrated* version of each,
> because Android silently subtracts a bias it estimated itself, and a filter that estimates its
> own bias needs to see what the OS did.
>
> **Four GNSS subscriptions**, not one. The fused position, yes — but also satellite count and
> signal strength, and the raw per-satellite Doppler. The satellite count matters because it
> **collapses before the fix does**: it is the earliest warning you get that you are entering a
> tunnel.
>
> And **one offline map file**. Two hundred and ten megabytes for a region — no tile server, no
> API key."

### [0:45 – 1:35] The service — trace top to bottom with your finger

> "All of it lands on **one thread**. Every sensor callback, every GNSS callback, every file
> write — a single writer, which is why there is not a lock anywhere in this app. And one
> monotonic clock, so the IMU and the GPS align by joining on a column instead of estimating an
> offset between two clocks.

*Move down to the three boxes.*

> Then it splits three ways.
>
> **Dead reckoning** is the classical path — a strapdown integrator with zero-velocity updates and
> learned gravity and bias.
>
> **The ML thread** is the second estimator, and it runs on the device: a temporal convolutional
> network over a ten-second window of IMU, giving us learned speed and stationarity. Those are
> precisely the two things double integration is worst at, so the network is aimed at the
> integrator's weakness rather than duplicating its strength.
>
> And the **GNSS gate** decides when a fix is trustworthy enough to correct either of them — four
> satellites used, accuracy under twenty metres. Below that we free-run on the two estimators."

### [1:35 – 2:05] Map matching — point at the green box

> "Finally we snap to the road network. And here is the part I'd most want you to notice —
>
> **that road graph is rebuilt out of the same map file we're already rendering.** No second
> download, no `.osm.pbf`, no routing server. The format doesn't store road topology, but it
> quantises coordinates to about eleven centimetres, so two roads that shared a junction come back
> on identical coordinates — snap the endpoints to a half-metre grid and the graph rebuilds
> itself. Ninety-eight and a half percent of segments land in one connected component.
>
> The matched road's bearing then feeds back as a heading correction, capped at four degrees per
> update so one wrong road can't capture the estimate."

### [2:05 – 2:35] The phone — point at the screenshots and hold there

> "And here it is actually running.
>
> On the left, the **whole downloaded region**, rendered offline — that's the entire Eastern zone
> on the device, with our position in it.
>
> In the middle, street level with the **live session panel** — sample counts, satellites, drift,
> and the three tracks: GPS in blue, IMU-only in dashed orange, snapped-to-road in green.
>
> On the right, the **map manager** — regions are downloaded in-app, one at a time, and you can
> see the Eastern zone installed and the others available."

### [2:35 – 3:00] Close — the three numbers, then the punchline, then stop

> "So: thirteen hundred samples a second. **Zero** API keys, zero backends, zero network calls at
> runtime. Two hundred and ten megabytes per region, carrying both the basemap and the road
> network.
>
> **The map, the road network and the positioning all live on the device — it works in a tunnel
> because it never needed a server.**"

*Stop. Hands down. Wait for the question.*

---

## THE 60-SECOND CUT

- **[0:00]** "This is our Android app. The entire navigation stack runs on the phone."
- **[0:08]** *(tap the three input boxes)* "Three inputs: **eleven IMU streams** at thirteen
  hundred samples a second, **four GNSS subscriptions** — not just the fix, but satellite health
  and raw Doppler — and **one offline map file**."
- **[0:20]** *(trace the middle)* "It all lands on **one writer thread**, on one monotonic clock.
  Two estimators run there — the **strapdown integrator** and an **on-device ML thread** — and the
  **GNSS gate** decides when a fix is healthy enough to correct them. Four satellites, twenty
  metres, or we free-run."
- **[0:38]** *(green box)* "Then we snap to a road graph rebuilt from **that same map file**. No
  second download, no routing server."
- **[0:46]** *(point at the phone, hold)* "Here it is running: the **whole downloaded region**,
  offline. The live session panel. And maps downloaded **per region**, in-app."
- **[0:54]** "No API keys. No backend. Nothing to call at runtime. **It works in a tunnel because
  it never needed a server.**"

---

## THE 30-SECOND CUT

Open, middle, close. Skip the inputs and the screenshots entirely.

> "Our Android app runs the whole navigation stack on the phone. Sensors land on a single writer
> thread on one monotonic clock; two estimators run there — a strapdown integrator and an
> on-device ML thread — and a GNSS gate decides when a fix is good enough to correct them. We snap
> to a road graph rebuilt from the same offline map file we render. No API keys, no backend,
> nothing to call at runtime — it works in a tunnel because it never needed a server."

---

## Pointing map

| When you say | Put your hand on |
| --- | --- |
| "eleven IMU streams" | top-left input box |
| "four subscriptions, not one" | middle-left input box |
| "one offline map file" | bottom-left input box |
| "one thread, one clock" | the blue band across the service |
| "strapdown integrator" | the orange box |
| "the ML thread" | the violet box |
| "GNSS gate" | the dashed brown box |
| "that same map file" | drag your finger from the map box to the green box |
| "the whole downloaded region" | leftmost phone |
| "live session panel" | middle phone |
| "downloaded per region" | right phone |
| the closing line | step back, no pointing |

---

## Two things not to say

1. **Never let "210 MB" sound like it covers India.** It is 210 MB *per region* — the Eastern
   zone. Other zones are downloaded in-app, 112 MB to 520 MB each. Your own screenshot shows the
   list, so an overclaim here is instantly visible.
2. **Never say the ML model is steering the navigation.** The thread runs on-device and produces
   speed and stationarity; the fusion into the position estimate is gated off in code until a
   checkpoint beats the baseline. If asked directly: *"it runs on-device, and we gate it until it
   earns its place."* That answer is stronger than the overclaim would have been.

## One thing worth checking before you present

Your settings screenshot reads **"323 MB installed on this device"**, not 210 — there is a second
`.map` file on the phone alongside the Eastern zone. Either delete the stray one and re-take that
screenshot, or don't say "210" while that panel is on screen.
