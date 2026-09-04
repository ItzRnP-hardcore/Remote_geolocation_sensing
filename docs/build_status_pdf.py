"""Generate the IDR project status and roadmap PDF."""

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether,
                                PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)

OUT = r"C:\Users\rudra\Documents\Remote_geolocation_sensing\docs\IDR_Status_2026-09-04.pdf"

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#5f6368")
RULE = colors.HexColor("#d8dade")
ACCENT = colors.HexColor("#1f5c8b")
GOOD = colors.HexColor("#1e6b45")
WARN = colors.HexColor("#9a4a12")
BAND = colors.HexColor("#f2f4f6")

ss = getSampleStyleSheet()

H1 = ParagraphStyle("H1", parent=ss["Normal"], fontName="Helvetica-Bold",
                    fontSize=15, leading=19, textColor=INK,
                    spaceBefore=16, spaceAfter=7)
H2 = ParagraphStyle("H2", parent=ss["Normal"], fontName="Helvetica-Bold",
                    fontSize=10.5, leading=14, textColor=ACCENT,
                    spaceBefore=11, spaceAfter=4)
BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontName="Helvetica",
                      fontSize=9.4, leading=13.6, textColor=INK,
                      alignment=TA_LEFT, spaceAfter=6)
SMALL = ParagraphStyle("SMALL", parent=BODY, fontSize=8.3, leading=11.6,
                       textColor=MUTED)
BULLET = ParagraphStyle("BULLET", parent=BODY, leftIndent=11, bulletIndent=2,
                        spaceAfter=3.5)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=8.4, leading=11.4,
                      spaceAfter=0)
CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")
TITLE = ParagraphStyle("TITLE", parent=ss["Normal"], fontName="Helvetica-Bold",
                       fontSize=21, leading=25, textColor=INK, spaceAfter=3)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontName="Helvetica",
                     fontSize=10.5, leading=14, textColor=MUTED, spaceAfter=2)


def table(data, widths, align_right=(), header=True, band=True):
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1 if header else 0)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.4),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
    ]
    if header:
        style += [
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
            ("FONTSIZE", (0, 0), (-1, 0), 7.6),
            ("LINEBELOW", (0, 0), (-1, 0), 0.9, MUTED),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ]
    if band:
        for r in range(1 if header else 0, len(data)):
            if (r - (1 if header else 0)) % 2 == 1:
                style.append(("BACKGROUND", (0, r), (-1, r), BAND))
    for c in align_right:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def callout(text, color=ACCENT):
    p = Paragraph(text, ParagraphStyle("CO", parent=BODY, fontSize=9.6,
                                       leading=13.8, leftIndent=8,
                                       rightIndent=6, spaceAfter=0))
    t = Table([[p]], colWidths=[168 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("LINEBEFORE", (0, 0), (0, -1), 2.4, color),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def b(text):
    return Paragraph(text, BULLET, bulletText="\u2013")


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.4)
    canvas.line(21 * mm, 14 * mm, 189 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.4)
    canvas.setFillColor(MUTED)
    canvas.drawString(21 * mm, 9.6 * mm,
                      "Intelligent Dead Reckoning  |  SIH 2026  |  Task 4: Mobile Interface")
    canvas.drawRightString(189 * mm, 9.6 * mm, "Page %d" % doc.page)
    canvas.restoreState()


story = []

# ---------------------------------------------------------------- title block
story.append(Paragraph("Intelligent Dead Reckoning", TITLE))
story.append(Paragraph("Android application: status and roadmap", SUB))
story.append(Paragraph("SIH 2026  |  Task 4, Mobile Interface and UI  |  4 September 2026", SMALL))
story.append(Spacer(1, 5))
story.append(Table([[""]], colWidths=[168 * mm], rowHeights=[1.4],
                   style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)]),
                   hAlign="LEFT"))
story.append(Spacer(1, 10))

story.append(callout(
    "<b>Where the project stands.</b> The Android application is complete and recording "
    "reliably. Dead reckoning with map-matched heading feedback works and is measured. Two "
    "further accuracy features are fully built but deliberately switched off, because both "
    "depend on one unresolved defect: while running without GNSS, the integrator recovers about "
    "30% less distance than was actually travelled. That defect has now been diagnosed to a "
    "specific cause, and fixing it needs no machine-learning model and no external dependency."))
story.append(Spacer(1, 4))

# ------------------------------------------------------------------- pipeline
story.append(Paragraph("1. What runs on the phone", H1))
story.append(Paragraph(
    "A foreground service records eleven sensor streams and drives four estimators in parallel "
    "across three threads. There is exactly one writer thread, so no locking is needed anywhere "
    "in the recording path.", BODY))

story.append(table([
    ["THREAD", "OWNS", "RATE"],
    ["imu-logger", "Sensor, GNSS and location callbacks; all file writes; the integrator", "200 Hz"],
    ["imu-ml", "Model load and inference (about 40 ms per forward pass)", "10 Hz"],
    ["map-match", "Mapsforge tile reads, HMM matcher, road graph", "0.5 Hz"],
], [26 * mm, 118 * mm, 24 * mm], align_right=(2,)))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "Every session writes <font face='Helvetica-Bold'>imu.csv</font>, "
    "<font face='Helvetica-Bold'>gps.csv</font>, "
    "<font face='Helvetica-Bold'>gnss_status.csv</font>, "
    "<font face='Helvetica-Bold'>deadreckon.csv</font>, "
    "<font face='Helvetica-Bold'>ml.csv</font>, "
    "<font face='Helvetica-Bold'>mapmatch.csv</font> and "
    "<font face='Helvetica-Bold'>session.json</font>. All timestamps share the monotonic "
    "elapsed-realtime clock, so they can be aligned exactly; session.json records the mapping "
    "to UTC. The map draws three tracks at once, GPS, IMU-only and map-snapped, so the "
    "divergence a tunnel would cause is visible live.", BODY))

# --------------------------------------------------------------------- works
story.append(Paragraph("2. What works today", H1))

story.append(Paragraph("Application and interface (Task 4)", H2))
story.append(Paragraph(
    "Essentially complete. Offline OpenStreetMap rendering, radius preloading, downloadable "
    "map zones for other parts of India, theme and settings screens, collapsible session card, "
    "and a locate button that zooms to a 1 km radius only when currently showing more. Records "
    "reliably on the Samsung S21 FE.", BODY))

story.append(Paragraph("Dead reckoning with map-matched heading feedback", H2))
story.append(Paragraph(
    "A strapdown inertial integrator free-runs whenever GNSS is unavailable. An online HMM map "
    "matcher snaps the drifting estimate to the road network and feeds the matched road bearing "
    "back as a heading observation. Measured across 46 to 57 synthetic outages per duration:", BODY))
story.append(table([
    ["ESTIMATOR", "10 s", "30 s", "60 s"],
    ["Dead reckoning + heading feedback (CRSE)", "10.2 m", "36.0 m", "87.5 m"],
], [92 * mm, 25 * mm, 25 * mm, 26 * mm], align_right=(1, 2, 3)))
story.append(Spacer(1, 5))

story.append(Paragraph("On-device road topology", H2))
story.append(Paragraph(
    "The strongest technical result so far. Mapsforge map files store ways clipped per tile with "
    "no node identity, so the network has no connectivity and the conventional answer is to route "
    "against an OSRM server, which an offline application cannot do. However, coordinates are "
    "stored in microdegrees, roughly 0.11 m, so segments that once shared an OpenStreetMap node "
    "still come back on identical coordinates. Snapping endpoints onto a half-metre grid rebuilds "
    "the graph entirely on the device.", BODY))
story.append(table([
    ["MEASURE", "RESULT"],
    ["Segments around IIT Kharagpur", "25,383"],
    ["Nodes recovered", "18,570"],
    ["Segments gaining at least one link", "100.0%"],
    ["Segments in a single connected component", "98.5%"],
    ["Mean node degree", "2.73"],
], [92 * mm, 40 * mm], align_right=(1,)))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "No server, no additional download, and it works underground. Tile-boundary clipping heals "
    "for free, because both halves of a clipped way end on the same border coordinate.", BODY))

story.append(Paragraph("Evaluation harness", H2))
story.append(Paragraph(
    "CRSE, CAE and AEPS implemented to the same definitions the model side of the project uses, "
    "so results are directly comparable. Synthetic outages are cut from recorded sessions and "
    "scored against GPS ground truth.", BODY))

# ----------------------------------------------------------------- gated off
story.append(Paragraph("3. Built, tested, and deliberately switched off", H1))
story.append(Paragraph(
    "Two complete features are one line from being live. Both are disabled because measurement "
    "showed they would currently make the application worse, not better.", BODY))

story.append(Paragraph("Along-road tracking", H2))
story.append(Paragraph(
    "Walks position as a scalar along a road polyline, so the road supplies direction and heading "
    "error stops entering the answer altogether. This is the mechanism behind the accuracy of the "
    "server-based approach, reproduced without a server.", BODY))
story.append(table([
    ["CONFIGURATION", "10 s", "30 s", "60 s"],
    ["Dead reckoning + heading feedback (baseline)", "10.2 m", "36.0 m", "87.5 m"],
    ["Along-road, fed the integrator's distance", "12.6 m", "40.8 m", "104.7 m"],
    ["Along-road, fed true distance", "8.3 m", "22.3 m", "50.5 m"],
], [92 * mm, 25 * mm, 25 * mm, 26 * mm], align_right=(1, 2, 3)))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "Given a trustworthy distance it cuts 60-second outage error by <b>42%</b>. Given the distance "
    "we can currently supply, it is 20% worse than doing nothing. The idea and the implementation "
    "are both sound; the input is not.", BODY))
story.append(Paragraph(
    "Two findings from building it are worth recording. Seeding matters: entering the road at the "
    "moment GNSS is lost, when position is still exact, is 42% better, whereas entering later from "
    "a drifted position is 20% worse. Same code, different starting point. And an earlier "
    "encouraging result was withdrawn after it failed to survive a denser sample; it had been "
    "measured on five outages.", SMALL))

story.append(Paragraph("Model speed fusion", H2))
story.append(Paragraph(
    "The integrator can now accept the neural network's speed estimate, blended into velocity "
    "magnitude only and weighted by a scalar Kalman gain formed from the model's own reported "
    "variance. A confident stand-still prediction sets the target speed to zero. It is applied "
    "only while unaided, since GNSS velocity beats anything a phone-IMU network can offer.", BODY))
story.append(Paragraph(
    "Retrained on IO-VNBD, the model beats a constant for the first time. Against the "
    "held-out test runs it reaches 4.88 m/s speed RMSE where predicting a constant gives "
    "6.84, and correlation with truth is +0.74. The previous checkpoint was twice WORSE "
    "than a constant and anti-correlated at -0.22, and its yaw head had been regressing "
    "on a hardcoded zero.", BODY))
story.append(Paragraph(
    "It is still not accurate enough to steer the integrator, and the reason has been "
    "diagnosed. The model overfits by a factor of twelve:", BODY))
story.append(table([
    ["SPLIT", "SPEED RMSE", "CORRELATION", "SD(PRED)/SD(TRUTH)"],
    ["train", "0.396 m/s", "0.999", "0.968"],
    ["validation", "2.796 m/s", "0.865", "0.913"],
    ["test", "4.877 m/s", "0.739", "0.629"],
], [40 * mm, 34 * mm, 34 * mm, 44 * mm], align_right=(1, 2, 3)))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "A train correlation of 0.999 means it has memorised the training windows: 3,848,196 "
    "parameters against 8,804 of them. On an unseen session it falls back toward the "
    "training mean, which is why its per-run bias correlates -0.82 with that run's mean "
    "speed and why the spread of its predictions collapses to 0.63 of the truth's. Two "
    "other explanations were tested and rejected - the estimated mounting rotation "
    "(correlation with bias only +0.28) and irreducible observability (ruled out by the "
    "near-perfect training fit). The levers are capacity, regularisation and data.", BODY))

# ------------------------------------------------------------------ blocker
story.append(Paragraph("4. The blocking defect, and its cause", H1))
story.append(callout(
    "<b>While free-running without GNSS, the integrator recovers about 30% less distance than "
    "was actually travelled.</b> This is the single largest error source in the system, and it is "
    "what keeps both features above switched off.", WARN))
story.append(Spacer(1, 7))
story.append(table([
    ["OUTAGE DURATION", "10 s", "30 s", "60 s"],
    ["Distance shortfall", "15.0%", "21.3%", "29.5%"],
    ["Zero-velocity updates firing", "1.9%", "0.0%", "1.4%"],
    ["Samples dropped by the timing gate", "0.0%", "0.0%", "0.0%"],
], [92 * mm, 25 * mm, 25 * mm, 26 * mm], align_right=(1, 2, 3)))
story.append(Spacer(1, 7))

story.append(Paragraph("What was ruled out", H2))
story.append(b("<b>The zero-velocity update is not the cause.</b> It was the leading hypothesis, "
               "and it is wrong. It fires on 0 to 2% of samples during outages, and at 30 seconds "
               "the shortfall is 21.3% while it fires on 0.0% of them. Disabling it entirely moves "
               "the 60-second shortfall only from 29.5% to 24.7%."))
story.append(b("<b>Dropped samples are not the cause.</b> The timing gate discards 0.0%."))
story.append(Spacer(1, 4))

story.append(Paragraph("What it actually is", H2))
story.append(Paragraph(
    "A systematic deceleration. Over 46 sixty-second outages, the levelled acceleration projected "
    "onto the direction of travel averages <b>-0.0195 m/s<super>2</super></b>, and is negative in <b>89%</b> of "
    "them. Over 60 seconds that alone removes 1.17 m/s, roughly a third of a 3.4 m/s cruise, which "
    "accounts for the entire shortfall. The shortfall also grows with duration exactly as a "
    "constant deceleration would.", BODY))
story.append(Paragraph(
    "Two supporting measurements point at where it comes from. The levelled vertical axis reads "
    "9.7574 m/s<super>2</super> while the accelerometer magnitude averages 9.8864, so gravity is not aligned "
    "with the estimated vertical and leaks into the horizontal plane. And the assumed gravity "
    "constant, 9.8066 m/s<super>2</super>, is 0.08 m/s<super>2</super> away from this device's own mean.", BODY))
story.append(callout(
    "<b>The structural fault:</b> the horizontal bias states are learned only during stand-still, "
    "so they are estimated exactly when they are not needed and frozen exactly when they are. "
    "During a tunnel transit, nothing corrects them.", WARN))
story.append(Spacer(1, 7))
story.append(Paragraph(
    "Whole sessions are unaffected, with a distance ratio of 0.992, because stand-still periods "
    "keep re-anchoring the estimate. The shortfall is specific to unanchored free-running, which "
    "is precisely the case the application exists for.", BODY))
story.append(Paragraph(
    "A separate and distinct failure was also found: in one near-stationary session, 39.8% of "
    "moving samples were wrongly called stationary and the integrator recovered 11 m of 93 m. That "
    "is a real defect, but it is not the cause of the general shortfall.", SMALL))

# ------------------------------------------------------------------ roadmap
story.append(Paragraph("5. The physics-informed loss, settled by measurement", H1))
story.append(Paragraph(
    "The execution plan specifies a penalty enforcing v(t) = v(t-1) + a*dt. Measured "
    "against a held-out test set it HURTS at every weight tried: +1.9% at 0.01, +3.8% at "
    "0.05, and +23.6% at 0.2, where training destabilises outright. The reason is that "
    "the integrator already satisfies that identity exactly and in closed form, so "
    "penalising violations of it teaches the network nothing the data does not already "
    "carry.", BODY))
story.append(Paragraph(
    "A different constraint does slightly better. The centripetal identity, lateral "
    "acceleration equals speed times yaw rate, couples two heads through physics using "
    "only the input signal, and it holds almost exactly in this data - regressing the "
    "vehicle's own lateral accelerometer on that product gives a slope of 1.008 with "
    "correlation 0.941. Applied to the phone's own lateral channel, as it would have to "
    "be on a real handset, it is worth -1.35%, consistent in sign across four seeds but "
    "short of statistical significance. It is not the thing standing between this project "
    "and working navigation.", BODY))

story.append(Paragraph("6. What to build next", H1))
story.append(Paragraph(
    "Ordered by value per unit of effort. Item 1 is now well specified by the diagnosis above and "
    "depends on nobody else.", BODY))

ROADMAP = [
    ("1", "Gyro and accelerometer bias observers",
     "Learn both biases while GNSS is healthy and carry them into the outage. Measured: "
     "replacing the model's yaw head with the raw gyro and removing its bias from "
     "pre-outage data alone takes 300 s free-running drift from 46.1% of distance "
     "travelled to 24.0%. Needs no model and no new data.", "Nothing"),
    ("2", "Cut model capacity and regularise",
     "The model overfits twelvefold. Two of the reviewed papers independently recommend "
     "shrinking the network; the constructor now spans 3.85M down to 28,580 parameters, "
     "with dropout and weight decay exposed.", "Nothing"),
    ("3", "Per-session output calibration",
     "Fit the model against GNSS over the two minutes before an outage and apply an "
     "offset during it. Measured worth about 20% at 30-120 s. Fitting a gain as well "
     "overfits and is worse.", "Item 1"),
    ("4", "Record driving data at 200 Hz",
     "IO-VNBD's phone stream is 10 Hz, so its Nyquist is 5 Hz and the vibration band that "
     "encodes absolute speed is not sampled at all. Our own app already records the "
     "accelerometer at 200 Hz, which no public dataset here can validate.", "Field time"),
    ("5", "Along-road tracking",
     "Even the best configuration still drifts 19-24% at 300 s, and that residual is "
     "largely cross-track, which is what the road network removes. The topology and the "
     "tracker are already built and gated off.", "Items 1-3"),
    ("6", "Road-test on the phone",
     "None of the above has run on the handset with real motion. Everything reported here "
     "is measured in the Python harness.", "Field time"),
]

rows = [[Paragraph(h, ParagraphStyle("th", parent=CELL, fontName="Helvetica-Bold",
                                     fontSize=7.6, textColor=MUTED))
         for h in ("#", "WORK", "WHY IT MATTERS", "DEPENDS ON")]]
for n, work, why, dep in ROADMAP:
    rows.append([Paragraph(n, CELLB), Paragraph(work, CELLB),
                 Paragraph(why, CELL), Paragraph(dep, CELL)])
story.append(table(rows, [7 * mm, 37 * mm, 92 * mm, 26 * mm], header=True))
story.append(Spacer(1, 6))

story.append(Paragraph(
    "The verification commands already exist and both currently fail closed, which is the "
    "intended behaviour:", BODY))
story.append(table([
    ["COMMAND", "CHECKS"],
    ["python -m eval.dr_diagnostics <sessions>", "Where the distance shortfall goes"],
    ["python -m eval.model_speed_eval <sessions>", "Whether the model beats a constant"],
    ["python -m eval.outage_eval <sessions>", "End-to-end outage position error"],
], [72 * mm, 84 * mm]))

# ------------------------------------------------------------- coordination
story.append(Paragraph("7. Notes for the team", H1))
story.append(b("<b>The on-device road matcher is new capability worth sharing.</b> It solves "
               "offline, on the handset, what the model-side pipeline currently uses an OSRM "
               "server for. Road topology does not require a routing server."))
story.append(b("<b>The training splits are not reproducible.</b> The data preparation directory "
               "on the model side is not committed, so results cannot be independently checked."))
story.append(b("<b>CORRECTION: IO-VNBD does contain phone IMU data, and is now the "
               "training set.</b> An earlier version of this document said it could not "
               "validate this integrator. That was wrong. The dataset README states the "
               "sensors include the inertial sensors and GPS receiver of an Android phone "
               "at 10 Hz, about 58 hours over 4,400 km. The copy that looked unusable was "
               "a set of Git LFS pointer stubs; the real archives were alongside it."))
story.append(b("<b>The phone stream is paired with a 10 Hz vehicle reference</b> carrying "
               "position, speed, heading and a real yaw rate. After per-run time alignment "
               "and quality gating, 26 runs and 19.9 hours are usable, giving 14,323 "
               "training windows against the 304 the previous pipeline produced."))
story.append(b("<b>Reported numbers come from three walking-pace sessions.</b> Every outage in "
               "them falls below the 5.0 m/s start-speed gate used in the reference literature, so "
               "these results are comparable in definition but not directly in magnitude. This is "
               "the strongest argument for item 2 above."))

story.append(Spacer(1, 10))
story.append(Paragraph(
    "All measurements in this document are reproducible from the committed evaluation harness "
    "against the recorded sessions. Figures were produced on 4 September 2026.", SMALL))

doc = BaseDocTemplate(OUT, pagesize=A4,
                      leftMargin=21 * mm, rightMargin=21 * mm,
                      topMargin=18 * mm, bottomMargin=20 * mm,
                      title="Intelligent Dead Reckoning: status and roadmap",
                      author="SIH 2026 Task 4")
frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=footer)])
doc.build(story)
print("wrote", OUT)
