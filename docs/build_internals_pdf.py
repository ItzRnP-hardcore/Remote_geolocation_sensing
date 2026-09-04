"""Generate the IDR internals report as a PDF.

Deliberately shares the visual system with build_status_pdf.py - same palette, type
scale, table and callout treatments - so the two documents read as one family rather
than two unrelated handouts.
"""

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, ListFlowable,
                                ListItem, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)

OUT = r"C:\Users\rudra\Documents\Remote_geolocation_sensing\docs\IDR_Internals_2026-09-04.pdf"

INK = colors.HexColor("#16202B")
MUTED = colors.HexColor("#5F6B78")
RULE = colors.HexColor("#D8DDE4")
ACCENT = colors.HexColor("#1E6B5E")
WARN = colors.HexColor("#A8621B")
NEG = colors.HexColor("#9B3B3B")
BAND = colors.HexColor("#F2F4F6")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Normal"], fontName="Helvetica-Bold",
                    fontSize=15, leading=19, textColor=INK, spaceBefore=17, spaceAfter=6)
H2 = ParagraphStyle("H2", parent=ss["Normal"], fontName="Helvetica-Bold",
                    fontSize=10.5, leading=14, textColor=ACCENT, spaceBefore=12, spaceAfter=4)
BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontName="Helvetica",
                      fontSize=9.4, leading=13.6, textColor=INK, alignment=TA_LEFT,
                      spaceAfter=6)
SMALL = ParagraphStyle("SMALL", parent=BODY, fontSize=8.3, leading=11.6, textColor=MUTED)
IO = ParagraphStyle("IO", parent=BODY, fontName="Courier", fontSize=8.4, leading=11.5,
                    textColor=MUTED, spaceAfter=5)
BULLET = ParagraphStyle("BULLET", parent=BODY, leftIndent=11, bulletIndent=2, spaceAfter=3.5)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=8.4, leading=11.4, spaceAfter=0)
CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")
TITLE = ParagraphStyle("TITLE", parent=ss["Normal"], fontName="Helvetica-Bold",
                       fontSize=21, leading=25, textColor=INK, spaceAfter=3)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontName="Helvetica", fontSize=10.5,
                     leading=14, textColor=MUTED, spaceAfter=2)
EQ = ParagraphStyle("EQ", parent=BODY, fontName="Courier", fontSize=9, leading=13,
                    textColor=INK, spaceAfter=0)


def table(data, widths, align_right=(), band=True):
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
    st = [("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
          ("FONTSIZE", (0, 0), (-1, -1), 8.4),
          ("TEXTCOLOR", (0, 0), (-1, -1), INK),
          ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 4.5),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
          ("LEFTPADDING", (0, 0), (-1, -1), 6),
          ("RIGHTPADDING", (0, 0), (-1, -1), 6),
          ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
          ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
          ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
          ("FONTSIZE", (0, 0), (-1, 0), 7.6),
          ("LINEBELOW", (0, 0), (-1, 0), 0.9, MUTED),
          ("BOTTOMPADDING", (0, 0), (-1, 0), 5)]
    if band:
        for r in range(1, len(data)):
            if r % 2 == 0:
                st.append(("BACKGROUND", (0, r), (-1, r), BAND))
    for c in align_right:
        st.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(st))
    return t


def callout(text, color=ACCENT, mono=False):
    p = Paragraph(text, ParagraphStyle("CO", parent=EQ if mono else BODY,
                                       fontSize=9 if mono else 9.4,
                                       leading=13 if mono else 13.8,
                                       leftIndent=8, rightIndent=6, spaceAfter=0))
    t = Table([[p]], colWidths=[168 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BAND),
                           ("LINEBEFORE", (0, 0), (0, -1), 2.4, color),
                           ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))
    return t


def b(text):
    return Paragraph(text, BULLET, bulletText="\u2013")


def layer(story, tag, tag_color, title, io, paras, box=None, box_color=WARN):
    story.append(Table([[""]], colWidths=[168 * mm], rowHeights=[0.6],
                       style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), RULE)]),
                       hAlign="LEFT"))
    story.append(Spacer(1, 7))
    story.append(Paragraph(f'<font color="{tag_color.hexval()}">{tag}</font>', SMALL))
    story.append(Paragraph(title, H1))
    story.append(Paragraph(io, IO))
    for p in paras:
        story.append(Paragraph(p, BODY))
    if box:
        story.append(callout(box, box_color))
        story.append(Spacer(1, 6))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.4)
    canvas.line(21 * mm, 14 * mm, 189 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.4)
    canvas.setFillColor(MUTED)
    canvas.drawString(21 * mm, 9.6 * mm,
                      "Intelligent dead reckoning internals  |  SIH 2026  |  Task 4")
    canvas.drawRightString(189 * mm, 9.6 * mm, "Page %d" % doc.page)
    canvas.restoreState()


story = []
story.append(Paragraph("SIH 2026  |  TASK 4  |  ENGINEERING REFERENCE", SMALL))
story.append(Paragraph("Intelligent dead reckoning internals", TITLE))
story.append(Paragraph("How every layer works, what each measurably contributes, and whether "
                       "a partial GNSS fix of fewer than four satellites can still be used.", SUB))
story.append(Spacer(1, 5))
story.append(Table([[""]], colWidths=[168 * mm], rowHeights=[1.4],
                   style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)]), hAlign="LEFT"))
story.append(Spacer(1, 9))
story.append(Paragraph(
    "Two parts. Part one walks the pipeline layer by layer, giving for each the inputs it "
    "takes, the transform it applies, and what it was measured to do. Part two answers the "
    "satellite question, which bears directly on the weakest channel in the system. Every "
    "figure is measured on recorded data through the harness in eval/, not estimated.", BODY))

story.append(Paragraph("Part I &mdash; the processing layers", H1))

layer(story, "WORKING", ACCENT, "1. Sensor acquisition",
      "in: phone hardware  ->  out: eleven timestamped streams",
      ["Eleven streams at explicit rates: accelerometer and gyroscope at 200 Hz with their "
       "uncalibrated variants, magnetometer and rotation vector at 50 Hz, game rotation vector "
       "and linear acceleration at 100 Hz, gravity at 50 Hz, barometer at 25 Hz. The "
       "uncalibrated variants are recorded deliberately: a filter that estimates its own bias "
       "states wants the raw signal, not one the operating system has already corrected.",
       "The sensor hub batches up to one second into its hardware FIFO before waking the "
       "application processor. Batched events keep their own correct timestamps, so batching "
       "costs latency but never accuracy.",
       "Everything is stamped on elapsedRealtimeNanos, the same monotonic clock "
       "SensorEvent.timestamp uses. That clock does not jump when NTP corrects the wall clock, "
       "which is what makes a reliable dt possible."])

layer(story, "WORKING", ACCENT, "2. Threading contract",
      "in: all callbacks  ->  out: one serialised write path",
      ["One thread, imu-logger, owns every sensor callback, every location and GNSS callback, "
       "every file write, and the integrator. Because there is exactly one writer, no lock "
       "appears anywhere in the recording path.",
       "Two other threads exist only because their work would stall a 200 Hz path. imu-ml holds "
       "the model, whose forward pass costs about 40 ms. map-match holds the road reader, whose "
       "first read of a new area parses several hundred ways off disk. Both receive posted work "
       "and post their results BACK to the logger thread rather than writing anything themselves."])

layer(story, "LARGEST ERROR SOURCE", WARN, "3. DeadReckoner &mdash; strapdown inertial integration",
      "in: accel 200 Hz, gyro, rotation vector  ->  out: position, velocity, drift",
      ["The rotation vector gives a device-to-world matrix in ENU convention. Each accelerometer "
       "sample is rotated into world axes, gravity is removed from the vertical, a learned bias "
       "is subtracted, and the result is integrated twice. A sample whose dt exceeds 50 ms is "
       "dropped rather than integrated, because a gap means the FIFO stalled and integrating "
       "across it would fabricate a velocity step.",
       "Stand-still is detected from acceleration norm and angular rate, and during it three "
       "things happen: gravity magnitude is learned, world-frame bias is learned, and velocity "
       "is forced to zero. Learning the gravity magnitude is what absorbs the accelerometer's "
       "scale-factor error, which on this handset is about 0.9%."],
      "<b>Measured defect.</b> Free-running without GNSS, this layer recovers about 30% less "
      "distance than was actually travelled: 15.0% short at 10 s, 21.3% at 30 s, 29.5% at 60 s. "
      "The zero-velocity update was the leading suspect and is NOT the cause - it fires on 0 to "
      "2% of samples during outages, and at 30 s the shortfall is 21.3% while it fires on 0.0% "
      "of them. Dropped samples are 0.0%. The actual cause is a systematic deceleration: over 46 "
      "sixty-second outages, acceleration projected onto the direction of travel averages "
      "-0.0195 m/s<super>2</super> and is negative in 89% of them, which over 60 s removes "
      "1.17 m/s. The structural fault is that horizontal bias states are learned only during "
      "stand-still, so they are estimated exactly when they are not needed and frozen exactly "
      "when they are.", WARN)

layer(story, "WEAK CHANNEL", NEG, "4. ResNet1D &mdash; the speed model",
      "in: (B, 6, 100), 10 s at 10 Hz  ->  out: four scalars",
      ["Six channels: vehicle-frame accelerometer forward, lateral, vertical, then gyroscope on "
       "the same axes. A strided stem convolution halves time, then four residual stages each "
       "double channels and halve time again, taking 6x100 to 512x7. Global average pooling then "
       "four linear heads.",
       "There is deliberately no max-pool after the stem. At 10 Hz a 100-sample window is already "
       "short, and pooling twice early would discard the temporal detail the estimate depends on."])
story.append(table([
    ["STAGE", "OUTPUT", "PARAMETERS", "SHARE"],
    ["stem, k7 s2", "(B, 64, 50)", "2,816", "0.1%"],
    ["stage 0", "(B, 64, 50)", "49,664", "1.3%"],
    ["stage 1", "(B, 128, 25)", "181,504", "4.7%"],
    ["stage 2", "(B, 256, 13)", "723,456", "18.8%"],
    ["stage 3", "(B, 512, 7)", "2,888,704", "75.1%"],
    ["four heads", "(B, 1) each", "2,052", "0.1%"],
], [40 * mm, 38 * mm, 34 * mm, 24 * mm], align_right=(2, 3)))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "The heads are mu (speed, through softplus so it cannot go negative), logvar (clamped to "
    "[-6, 4], which weights the fusion gain downstream), stationary_logit, and yaw_rate.", BODY))
story.append(callout(
    "<b>Measured limits.</b> Test RMSE 4.88 m/s against 6.84 for predicting a constant, "
    "correlation +0.74. Held out by DRIVER rather than journey that falls to 5.66 against 6.89, "
    "a 17.8% margin rather than the 28.7% a journey split suggests, because held-out journeys "
    "share roads with training ones by up to 50% bounding-box overlap.<br/><br/>"
    "Train RMSE is 0.396 with correlation 0.999, a twelvefold train-to-test gap - but shrinking "
    "the network does not close it, so this is distribution shift between sessions, not classic "
    "variance. Everything tried (more data, less capacity, dropout, weight decay, two physics "
    "losses, three augmentations, quality weighting) moves test error by at most a couple of "
    "percent.<br/><br/>"
    "<b>The yaw head should not be used for navigation.</b> It correlates 0.82 to 0.83 with true "
    "yaw rate where the raw gyro axis it was trained from correlates 0.94 to 0.996, and carries "
    "enough bias to swing heading 893 to 1410 deg/hr.", NEG))
story.append(Spacer(1, 6))

layer(story, "WORKING", ACCENT, "5. MapMatcher &mdash; online HMM",
      "in: drifting position, course, drift estimate  ->  out: snapped position, road bearing",
      ["States are candidate positions on nearby road segments; observations are the "
       "dead-reckoned fixes. Viterbi runs incrementally with a beam of 12 over at most 24 "
       "candidates, so each fix costs beam x candidates rather than a re-run over the history.",
       "This departs from Newson and Krumm deliberately. Their transition term is the difference "
       "between straight-line distance and route distance through the road graph, which needs "
       "connectivity Mapsforge does not provide. What replaces it is a heading term, and that is "
       "the point rather than a consolation: the whole reason this layer exists is that heading "
       "integration is where dead reckoning fails, so scoring a candidate on how well the road's "
       "bearing agrees with the vehicle's course is precisely the observation the IMU cannot "
       "supply for itself.",
       "Emission spread is 30 m, far wider than the 4.07 m Newson and Krumm fit for GPS, because "
       "the observation here is a dead-reckoned position that may be hundreds of metres out, and "
       "too tight a sigma would reject the correct road outright."],
      "The correction budget is the safety valve: a match is rejected if it would move the "
      "estimate further than max(drift, 12 m). Measured across four sessions, snapping at about "
      "37 m drift cut position error by 30%, but with drift near zero it made things 40 times "
      "worse by dragging a fix 0.6 m from truth onto a road 25 m away. Being unaided is not the "
      "same as being wrong yet.", ACCENT)

layer(story, "BUILT, GATED OFF", WARN, "6. RoadGraph and AlongRoadTracker",
      "in: Mapsforge geometry  ->  out: connected topology, along-road position",
      ["Mapsforge stores ways clipped per tile with no node identity, which is why the "
       "conventional answer is to route against a server. But coordinates are stored in "
       "microdegrees, about 0.11 m, so segments that once shared an OpenStreetMap node come back "
       "on identical coordinates. Snapping endpoints onto a half-metre grid rebuilds the graph "
       "entirely on the device."])
story.append(table([
    ["NETWORK", "SEGMENTS", "NODES", "LINKED", "LARGEST COMPONENT"],
    ["IIT Kharagpur", "25,383", "18,570", "100.0%", "98.5%"],
    ["Coventry - Birmingham", "155,050", "148,363", "99.99%", "99.0%"],
], [44 * mm, 26 * mm, 24 * mm, 22 * mm, 38 * mm], align_right=(1, 2, 3, 4)))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "With topology recovered, position can advance as a scalar walked along a polyline: the road "
    "supplies direction and heading error stops entering the answer at all. Fed a trustworthy "
    "distance this cuts 60 s outage error by 42%. Fed the distance the integrator currently "
    "produces it is 20% WORSE than doing nothing, which is why it ships disabled.", BODY))

layer(story, "WORKING", ACCENT, "7. Offline pipeline",
      "in: IO-VNBD S and V file pairs  ->  out: training set, evaluation",
      ["eval/iovnbd.py pairs each smartphone stream with its vehicle reference stream. Six things "
       "in those files contradict their own headers, each capable of silently poisoning "
       "everything downstream: a speed column labelled km/h that holds m/s, a gravity column that "
       "is a constant placeholder, an orientation column that contradicts the accelerometer, GPS "
       "held for 9 s at a stretch, synchronised files that are not time-aligned, and gyroscope "
       "columns permuted relative to the accelerometer.",
       "Alignment is two-stage and the second stage is not optional. A coarse lag from the "
       "9-second-held phone GPS can only localise to its own step; the residual measured 4.4 s, "
       "at which no linear combination of phone acceleration predicted vehicle acceleration "
       "(r = 0.06). Refining against two genuine 10 Hz signals took it to r = 0.45.",
       "eval/ahrs.py supplies attitude, since the dataset has none usable. It uses only what can "
       "be checked: tilt from the accelerometer, yaw from the gyro projected onto the "
       "accelerometer-derived vertical. The vehicle's own yaw rate aligns and scores it but never "
       "builds it, so it stays a held-out validation signal rather than becoming part of the "
       "input it is meant to supervise."])

story.append(Paragraph("Part II &mdash; using fewer than four satellites", H1))
story.append(Paragraph(
    "Short answer: <b>yes, and it targets exactly the channel that is currently broken</b> - but "
    "it belongs in the estimator, not in the neural network.", BODY))

story.append(Paragraph("Why four, and what fewer still contains", H2))
story.append(Paragraph(
    "A standalone position fix has four unknowns: three coordinates plus the receiver clock bias, "
    "unknown because a phone's oscillator is nowhere near good enough to keep GPS time. Each "
    "pseudorange gives one equation, so four satellites is the minimum for a snapshot fix. That "
    "is a constraint on solving for POSITION, and it says nothing about whether one, two or three "
    "satellites carry information. They plainly do - each is still a real measurement of a real "
    "geometric quantity.", BODY))
story.append(b("<b>Pseudorange</b> constrains position and clock bias jointly. It needs three "
               "more friends to be useful on its own."))
story.append(b("<b>Pseudorange rate</b>, the Doppler, constrains VELOCITY and clock drift. This "
               "is the interesting one."))
story.append(Spacer(1, 4))
story.append(Paragraph(
    "The current code discards both. SensorService records only GnssStatus, and treats "
    "satellitesUsedInFix below 4 as unusable, at which point the integrator free-runs as though "
    "the sky were empty.", BODY))

story.append(Paragraph("Why Doppler is the right target here", H2))
story.append(Paragraph(
    "Every measurement in part one points the same way: after heading is taken from the gyro "
    "rather than the model, <b>speed is the binding constraint</b>. The model manages 4.88 m/s "
    "RMSE, the integrator runs 30% short on distance, and free-running drift is 46% of distance "
    "travelled at 300 s.", BODY))
story.append(Paragraph(
    "A pseudorange rate is a direct measurement of velocity projected onto the line of sight, and "
    "Android reports its own uncertainty through getPseudorangeRateUncertaintyMetersPerSecond, "
    "typically well under 1 m/s on modern chipsets. That is roughly an order of magnitude better "
    "than anything the speed model produces, and it requires no position fix whatsoever.", BODY))

story.append(Paragraph("The vehicle constraint collapses the requirement", H2))
story.append(Paragraph(
    "For a general receiver, velocity has four unknowns - three components plus clock drift - so "
    "four satellites again. A road vehicle is not general. It travels along its heading, and its "
    "vertical velocity is nil:", BODY))
story.append(callout("v = s . [sin h, cos h, 0]       h from the gyro<br/><br/>"
                     "rho_dot = e . (v_sat - v) + c . (drift_rx - drift_sat) + noise",
                     WARN, mono=True))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "With heading supplied by the integrator, the unknowns collapse to two: forward speed s and "
    "receiver clock drift. So <b>two satellites determine vehicle speed</b>. And because a "
    "phone's clock drift is stable over tens of seconds, coasting it from the last healthy period "
    "leaves one unknown, so <b>a single satellite is enough to pin speed</b>.", BODY))
story.append(callout(
    "<b>The counterintuitive part:</b> which satellite matters more than how many. The "
    "measurement carries information about speed in proportion to the along-track component of "
    "the line of sight. A satellite directly overhead contributes almost nothing about horizontal "
    "speed; a low-elevation satellite ahead or behind contributes the most. That inverts the "
    "usual preference, where high elevation is favoured for less multipath.", ACCENT))
story.append(Spacer(1, 6))

story.append(Paragraph("Implementation", H2))
steps = [
    "<b>Record raw measurements.</b> Register a GnssMeasurementsCallback and write a new "
    "gnss_raw.csv: per satellite the svid, constellation, pseudorange rate and its uncertainty, "
    "accumulated delta range and its state, C/N0; plus GnssClock drift and bias. None of this is "
    "captured today, so this is step zero regardless of what is done with it.",
    "<b>Cache ephemeris while healthy.</b> Satellite position and velocity are needed to form the "
    "line of sight and to remove the satellite's own motion, which projects to hundreds of m/s "
    "and cannot be ignored. Register GnssNavigationMessage and keep the broadcast ephemeris; it "
    "stays valid for hours, and a tunnel transit is minutes.",
    "<b>Shortcut worth knowing.</b> GnssStatus already exposes per-satellite azimuth and "
    "elevation while tracking, which gives the line-of-sight direction without decoding ephemeris "
    "at all. Only the satellite-velocity correction still needs it.",
    "<b>Solve for speed.</b> With one or two satellites, least-squares the equation above for s, "
    "weighting by the reported uncertainty and rejecting anything under about 20 dB-Hz as "
    "multipath-prone. Reject epochs where the along-track geometry term is near zero, since those "
    "measure nothing useful.",
    "<b>Feed it as a velocity observation</b>, not a position one. DeadReckoner already has the "
    "right shape for this in applyModelSpeed, which blends a speed estimate into velocity "
    "magnitude and leaves direction alone. A Doppler speed enters the same way with a far smaller "
    "variance.",
    "<b>Validate the sign convention empirically.</b> Android's pseudorange rate sign has bitten "
    "people. This project has already been caught twice by exactly this class of bug - two "
    "heading sign errors that produced valid rotation matrices and plausible tracks, just "
    "rotated. Confirm against a stretch of healthy GNSS before trusting it.",
]
story.append(ListFlowable([ListItem(Paragraph(s, BODY), leftIndent=16) for s in steps],
                          bulletType="1", start="1", leftIndent=16, bulletFontSize=9))

story.append(Paragraph("What it is worth, honestly", H2))
story.append(Paragraph(
    "In a deep tunnel there are zero satellites and this contributes nothing. Its value is at the "
    "margins, and the margins are common: tunnel entry and exit, urban canyons, underpasses, "
    "dense tree cover, multi-storey car parks - everywhere the count drops below four and the "
    "current code throws the sky away.", BODY))
story.append(Paragraph(
    "The mechanism that matters is bounding drift rather than fixing position. Speed error "
    "integrates into displacement, so a sporadic single-satellite Doppler that resets the speed "
    "estimate every few tens of seconds prevents the error that dominates a long outage from ever "
    "accumulating. Against a measured 46% drift at 300 s, that is a large lever.", BODY))
story.append(callout(
    "<b>It does not belong in the neural network.</b> Pseudoranges relate to position through "
    "exact, known geometry. A convolutional network fed raw ranges would have to rediscover from "
    "data what can simply be written down, and would need far more data than this project has to "
    "do it badly. The model's job stays what it is - inertial signal to speed - and its logvar "
    "head is the right place for it to participate, by telling the filter how much to trust it "
    "against a Doppler measurement that arrives with its own stated uncertainty.", NEG))
story.append(Spacer(1, 8))

final_head = Paragraph("Where it sits against everything else", H2)
rows = [[Paragraph(h, ParagraphStyle("th", parent=CELL, fontName="Helvetica-Bold",
                                     fontSize=7.6, textColor=MUTED))
         for h in ("CHANGE", "MEASURED EFFECT", "NEEDS")]]
for c, e, n in (
    ("Gyro heading instead of the model's yaw head, gyro bias removed from pre-outage data",
     "300 s drift 46% to 24%", "nothing"),
    ("Per-session speed offset calibration", "30 s drift 20.5% to 16.0%", "nothing"),
    ("Sub-four-satellite Doppler speed aiding", "not yet measured", "raw GNSS logging"),
    ("Along-road tracking", "60 s error -42%", "a good distance channel"),
    ("Any training-side model change", "at most a couple of percent", "-"),
):
    rows.append([Paragraph(c, CELL), Paragraph(e, CELLB), Paragraph(n, CELL)])
story.append(KeepTogether([final_head,
                          table(rows, [86 * mm, 46 * mm, 32 * mm], band=True)]))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "The pattern is consistent: this system's accuracy lives in the estimator and the constraints "
    "available to it, not in the network. Sub-four-satellite aiding fits that pattern exactly, "
    "which is a good reason to expect it to work, and a good reason to measure it before "
    "believing it.", BODY))

story.append(Spacer(1, 10))
story.append(Paragraph(
    "All figures measured through eval/ on recorded sessions and on IO-VNBD. Reproduce with "
    "eval/dr_diagnostics.py, eval/model_dr_eval.py and eval/rank_models.py; the full experiment "
    "log is in ml_model/PROGRESS.md. Sub-four-satellite aiding is a proposal, not a result - it "
    "is the only item here without a measured number. 4 September 2026.", SMALL))

doc = BaseDocTemplate(OUT, pagesize=A4, leftMargin=21 * mm, rightMargin=21 * mm,
                      topMargin=18 * mm, bottomMargin=20 * mm,
                      title="Intelligent dead reckoning internals", author="SIH 2026 Task 4")
frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=footer)])
doc.build(story)
print("wrote", OUT)
