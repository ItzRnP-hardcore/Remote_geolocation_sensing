# The 60-second script — Odyssey app slide

~150 words. Point, don't read: everything on the slide is already written down, so your voice
carries the *why*. Hand moves left → middle → right → phone.

---

**[0:00–0:08] Open — stand back, don't point yet**
> "This is our Android app. The entire navigation stack runs on the phone."

**[0:08–0:20] Left column — tap each of the three input boxes**
> "Three inputs: **eleven IMU streams** at thirteen hundred samples a second, **four GNSS
> subscriptions** — not just the fix, but satellite health and raw Doppler — and **one offline
> map file**."

**[0:20–0:38] Middle — trace top to bottom**
> "It all lands on **one writer thread**, on one monotonic clock. Two estimators run there — the
> **strapdown integrator** and an **on-device ML thread** — and the **GNSS gate** decides when a
> fix is healthy enough to correct them. Four satellites, twenty metres, or we free-run."

**[0:38–0:46] Middle bottom — the green box**
> "Then we snap to a road graph rebuilt from **that same map file**. No second download, no
> routing server."

**[0:46–0:54] Screenshots — point and hold**
> "Here it is running: the **whole downloaded region**, offline. The live session panel. And maps
> downloaded **per region**, in-app."

**[0:54–1:00] Close — the three numbers, then stop**
> "No API keys. No backend. Nothing to call at runtime. **It works in a tunnel because it never
> needed a server.**"

---

## If you get 15 seconds more

Add after the ML thread line:
> "The ML thread gives us learned speed and stationarity from a ten-second IMU window — the two
> things double integration is worst at."

## If you get cut short at 30 seconds

Say only the **open**, the **middle**, and the **close**. Skip the inputs and the screenshots.

## Don't say

- **"210 MB covers India"** — it's 210 MB *per region*. The Eastern zone. Other zones download in-app.
- **"The ML model is driving it"** — the thread runs on-device; speed fusion is still gated off in
  code pending a checkpoint that beats the baseline. If asked: *"it runs on-device, and we gate it
  until it earns its place."*
- Anything about file formats or thread names. Nobody asked.

## The three questions you will get

1. **"Why not Google Maps?"** — No offline tile access, caching is prohibited by their terms, and
   it needs a billing account. Any one of those kills it for an app about tunnels.
2. **"Does it really work with no internet?"** — Not at runtime. Once, ahead of time, to download
   the region. After that, never.
3. **"How accurate is it?"** — We show drift as a percentage of distance travelled, against a 10%
   target, live on screen. That's the honest way to state it — metres alone mean nothing without
   the distance.
