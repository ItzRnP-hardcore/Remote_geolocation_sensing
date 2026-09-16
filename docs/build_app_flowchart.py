"""Render the single-slide flowchart of how the IMU Logger app functions.

Outputs docs/app_flowchart.png (300 dpi) and docs/app_flowchart.svg.
The SVG is the one to insert in PowerPoint if you want to recolour anything later;
the PNG is the safe one for Google Slides and for printing.

Layout rule followed throughout: no arrow crosses a box, and every text block is
sized to sit inside its own box at the chosen font size.

Two estimators, not one: the strapdown integrator and the on-device ML thread both
feed the position estimate, and the GNSS trust gate decides when a fix may correct it.
"""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = os.path.dirname(os.path.abspath(__file__))

# Palette: doc colours from build_status_pdf.py, plus the app's own track colours.
INK = "#1a1a1a"
MUTED = "#5f6368"
ACCENT = "#1f5c8b"
RULE = "#c9ced4"
BAND = "#f4f6f8"
GPS_BLUE = "#1668C6"
IMU_ORANGE = "#EB6834"
SNAP_GREEN = "#0C7A3E"
GATE = "#9a4a12"
ML_VIOLET = "#6A3D9A"

W, Y0, Y1 = 100.0, 3.6, 65.0   # coordinate space; Y0 trims the old footer band
FIG_W = 8.6                     # inches; height follows the aspect ratio
FIG_H = FIG_W * (Y1 - Y0) / W

fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
ax.set_xlim(0, W)
ax.set_ylim(Y0, Y1)
ax.axis("off")
fig.subplots_adjust(left=0, right=1, top=1, bottom=0)


def box(x0, y0, x1, y1, *, fc="white", ec=RULE, lw=1.2, r=1.6, z=2, ls="solid"):
    ax.add_patch(FancyBboxPatch(
        (x0, y0), x1 - x0, y1 - y0,
        boxstyle=f"round,pad=0,rounding_size={r}",
        facecolor=fc, edgecolor=ec, linewidth=lw, linestyle=ls, zorder=z,
    ))


def text(x, y, s, *, size=8.2, color=INK, weight="normal", ha="center", va="center",
         z=5, style="normal"):
    ax.text(x, y, s, fontsize=size, color=color, fontweight=weight, ha=ha, va=va,
            zorder=z, style=style, linespacing=1.5)


def arrow(x0, y0, x1, y1, *, color=INK, lw=1.6, z=4, ms=9, connection="arc3,rad=0",
          ls="solid"):
    ax.add_patch(FancyArrowPatch(
        (x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=ms,
        color=color, linewidth=lw, zorder=z, shrinkA=0, shrinkB=0,
        connectionstyle=connection, linestyle=ls,
    ))


# ───────────────────────────── column geometry ─────────────────────────────
COL_A0, COL_A1 = 1.0, 23.5          # inputs
COL_B0, COL_B1 = 29.0, 72.0         # the service
COL_C0, COL_C1 = 77.5, 99.0         # outputs

for x, label in (((COL_A0 + COL_A1) / 2, "INPUTS"),
                 ((COL_B0 + COL_B1) / 2, "ON-DEVICE PROCESSING"),
                 ((COL_C0 + COL_C1) / 2, "OUTPUTS")):
    text(x, 63.2, label, size=8.0, color=MUTED, weight="bold")

# ───────────────────────────── inputs ─────────────────────────────
AMID = (COL_A0 + COL_A1) / 2

box(COL_A0, 45.5, COL_A1, 60.5, fc=BAND)
text(AMID, 57.9, "IMU  ·  11 streams", size=9.4, weight="bold")
text(AMID, 51.2,
     "accel + gyro @ 200 Hz\ncalibrated AND uncalibrated\nmag · 2 rotation vectors\n"
     "gravity · barometer\n1,312 samples / second",
     size=7.3, color=MUTED)

box(COL_A0, 26.5, COL_A1, 41.5, fc=BAND)
text(AMID, 38.9, "GNSS  ·  4 subscriptions", size=9.4, weight="bold")
text(AMID, 32.2,
     "fused fix (lat/lon/speed)\nsatellite count + C/N0\nraw Doppler + carrier phase\n"
     "navigation messages\n5 constellations, 25 sats used",
     size=7.3, color=MUTED)

box(COL_A0, 7.5, COL_A1, 22.5, fc=BAND)
text(AMID, 19.9, "OFFLINE MAP", size=9.4, weight="bold")
text(AMID, 13.6,
     "Mapsforge .map, one region\nEastern zone = 210 MB\nother zones downloaded in-app\n"
     "rendered on the phone\nNO tile server · NO API key",
     size=7.3, color=MUTED)

# ───────────────────────────── the service container ─────────────────────────────
box(COL_B0, 5.0, COL_B1, 61.0, fc="white", ec=ACCENT, lw=1.7, r=2.0, z=1)
text((COL_B0 + COL_B1) / 2, 58.6,
     "SensorService — foreground service + wake lock",
     size=9.0, weight="bold", color=ACCENT)

# Row A: the single-writer band.
BAND_X0, BAND_X1 = COL_B0 + 1.5, COL_B1 - 1.5
box(BAND_X0, 47.5, BAND_X1, 56.5, fc="#eaf1f7", ec="#b0c7da")
text((COL_B0 + COL_B1) / 2, 54.3, "imu-logger thread  ·  THE SINGLE WRITER",
     size=8.4, weight="bold", color=ACCENT)
text((COL_B0 + COL_B1) / 2, 50.3,
     "every sensor, location and GNSS callback lands here,\n"
     "and so does every file write  →  no locks anywhere\n"
     "one monotonic clock:  elapsedRealtimeNanos",
     size=6.9, color=MUTED)

# Row B: two estimators either side, and the gate that decides when GNSS may correct.
ROW_B0, ROW_B1 = 30.5, 45.5
GAP = 1.6
BW = (BAND_X1 - BAND_X0 - 2 * GAP) / 3
ML_X0, ML_X1 = BAND_X0, BAND_X0 + BW
DR_X0, DR_X1 = ML_X1 + GAP, ML_X1 + GAP + BW
GT_X0, GT_X1 = DR_X1 + GAP, BAND_X1

box(ML_X0, ROW_B0, ML_X1, ROW_B1, ec=ML_VIOLET, lw=1.5)
text((ML_X0 + ML_X1) / 2, 43.3, "ML THREAD", size=7.6, weight="bold", color=ML_VIOLET)
text((ML_X0 + ML_X1) / 2, 37.0,
     "TCN over a 10 s\nwindow of IMU\n\nlearned speed and\nstationarity,\non the device",
     size=6.9, color=MUTED)

box(DR_X0, ROW_B0, DR_X1, ROW_B1, ec=IMU_ORANGE, lw=1.5)
text((DR_X0 + DR_X1) / 2, 43.3, "DEAD RECKONING", size=7.6, weight="bold", color=IMU_ORANGE)
text((DR_X0 + DR_X1) / 2, 37.0,
     "strapdown\nintegrator\n\nzero-velocity\nupdates, learned\ngravity + bias",
     size=6.9, color=MUTED)

box(GT_X0, ROW_B0, GT_X1, ROW_B1, ec=GATE, lw=1.5, ls=(0, (4, 2)))
text((GT_X0 + GT_X1) / 2, 43.3, "GNSS GATE", size=7.6, weight="bold", color=GATE)
text((GT_X0 + GT_X1) / 2, 39.6, "≥ 4 sats used\nAND  < 20 m", size=7.2, color=INK)
text((GT_X0 + GT_X1) / 2, 34.6,
     "healthy → re-anchor\nlost → free-run on\nthe two estimators", size=6.9, color=MUTED)

# Row C: map matching, full width.
ROW_C0, ROW_C1 = 8.0, 28.0
box(BAND_X0, ROW_C0, BAND_X1, ROW_C1, ec=SNAP_GREEN, lw=1.5)
text((COL_B0 + COL_B1) / 2, 25.4, "map-match thread", size=8.6, weight="bold", color=SNAP_GREEN)
text((COL_B0 + COL_B1) / 2, 17.2,
     "road graph rebuilt from the SAME regional .map file\n"
     "— no second download, no routing server —\n\n"
     "98.5% of segments land in one connected component\n\n"
     "HMM snap → the road's bearing is fed back as a heading fix\n"
     "(capped at 4° per update, so one wrong road cannot capture it)",
     size=6.9, color=MUTED)

# ───────────────────────────── internal arrows ─────────────────────────────
for cx, colour in (((ML_X0 + ML_X1) / 2, ML_VIOLET),
                   ((DR_X0 + DR_X1) / 2, INK),
                   ((GT_X0 + GT_X1) / 2, GATE)):
    arrow(cx, 47.5, cx, ROW_B1, color=colour, lw=1.4)       # band → each box

arrow(ML_X1, 38.0, DR_X0, 38.0, color=ML_VIOLET, lw=1.4)    # learned speed → the estimate
arrow(GT_X0, 38.0, DR_X1, 38.0, color=GATE, lw=1.4)         # gate → the estimate
arrow((DR_X0 + DR_X1) / 2, ROW_B0, (DR_X0 + DR_X1) / 2, ROW_C1,
      color=SNAP_GREEN, lw=1.5)                             # fused position → matcher

# ───────────────────────────── inputs → service ─────────────────────────────
arrow(COL_A1, 52.5, COL_B0, 52.5, lw=1.5, connection="arc3,rad=0.05")
arrow(COL_A1, 34.0, COL_B0, 50.0, lw=1.5, connection="arc3,rad=-0.16")
arrow(COL_A1, 15.0, COL_B0, 18.5, color=SNAP_GREEN, lw=1.5, connection="arc3,rad=-0.10")

# ───────────────────────────── outputs ─────────────────────────────
CMID = (COL_C0 + COL_C1) / 2

box(COL_C0, 33.0, COL_C1, 60.5, fc=BAND)
text(CMID, 57.9, "9 FILES PER SESSION", size=9.2, weight="bold")
text(CMID, 47.4,
     "imu.csv · gps.csv\ngnss_status.csv\ngnss_raw.csv\ngnss_nav.csv\n"
     "deadreckon.csv\nmapmatch.csv · ml.csv\nsession.json",
     size=7.2, color=MUTED)
text(CMID, 37.4,
     "350 MB / hour\nflushed every 2 s  →\na crash costs ≤ 2 s",
     size=7.4, color=INK)

box(COL_C0, 7.5, COL_C1, 29.0, fc=BAND)
text(CMID, 26.4, "LIVE MAP + PANEL", size=9.2, weight="bold")
for y, colour, lwid, dash, label in (
        (22.2, GPS_BLUE, 2.8, None, "GPS track"),
        (19.0, IMU_ORANGE, 2.6, (2.2, 1.5), "IMU only"),
        (15.8, SNAP_GREEN, 3.0, None, "snapped to road")):
    line, = ax.plot([COL_C0 + 2.2, COL_C0 + 6.2], [y, y], color=colour, lw=lwid,
                    solid_capstyle="round", zorder=5)
    if dash:
        line.set_dashes(dash)
    text(COL_C0 + 7.4, y, label, size=7.4, ha="left")
text(CMID, 11.2,
     "drift as % of distance,\ncolour-coded against\nthe 10% target",
     size=7.2, color=MUTED)

# ───────────────────────────── service → outputs ─────────────────────────────
arrow(COL_B1, 52.0, COL_C0, 48.0, lw=1.5, connection="arc3,rad=0.10")
arrow(COL_B1, 20.0, COL_C0, 20.0, color=SNAP_GREEN, lw=1.5, connection="arc3,rad=0.05")

png = os.path.join(OUT, "app_flowchart.png")
svg = os.path.join(OUT, "app_flowchart.svg")
fig.savefig(png, dpi=300, facecolor="white", bbox_inches="tight", pad_inches=0.12)
fig.savefig(svg, facecolor="white", bbox_inches="tight", pad_inches=0.12)
print("wrote", png)
print("wrote", svg)
