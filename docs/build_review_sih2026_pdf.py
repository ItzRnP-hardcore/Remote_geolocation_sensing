"""Generate the review of pranjali2105/SIH_2026 as a PDF.

Same visual system as build_status_pdf.py and build_internals_pdf.py - palette, type scale,
tables and callouts - so the documents read as one family. The style block is duplicated
rather than imported because those modules build their own PDF at import time.

Every number is reproducible: eval/gyro_axis_check.py for the gyroscope findings,
eval/compare_teammate_model.py for the head-to-head.
"""

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

OUT = r"C:\Users\rudra\Documents\Remote_geolocation_sensing\docs\Review_SIH_2026_2026-09-18.pdf"

INK = colors.HexColor("#16202B")
MUTED = colors.HexColor("#5F6B78")
RULE = colors.HexColor("#D8DDE4")
ACCENT = colors.HexColor("#1E6B5E")
WARN = colors.HexColor("#A8621B")
NEG = colors.HexColor("#9B3B3B")
BAND = colors.HexColor("#F2F4F6")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Normal"], fontName="Helvetica-Bold",
                    fontSize=15, leading=19, textColor=INK, spaceBefore=15, spaceAfter=6)
H2 = ParagraphStyle("H2", parent=ss["Normal"], fontName="Helvetica-Bold",
                    fontSize=10.5, leading=14, textColor=ACCENT, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontName="Helvetica",
                      fontSize=9.4, leading=13.6, textColor=INK, alignment=TA_LEFT,
                      spaceAfter=6)
SMALL = ParagraphStyle("SMALL", parent=BODY, fontSize=8.3, leading=11.6, textColor=MUTED)
BULLET = ParagraphStyle("BULLET", parent=BODY, leftIndent=11, bulletIndent=2, spaceAfter=3.5)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=8.4, leading=11.4, spaceAfter=0)
CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")
TITLE = ParagraphStyle("TITLE", parent=ss["Normal"], fontName="Helvetica-Bold",
                       fontSize=21, leading=25, textColor=INK, spaceAfter=3)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontName="Helvetica", fontSize=10.5,
                     leading=14, textColor=MUTED, spaceAfter=2)
EQ = ParagraphStyle("EQ", parent=BODY, fontName="Courier", fontSize=8.8, leading=12.6,
                    textColor=INK, spaceAfter=0)


def table(data, widths, align_right=(), highlight_row=None):
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
    for r in range(1, len(data)):
        if r % 2 == 0:
            st.append(("BACKGROUND", (0, r), (-1, r), BAND))
    if highlight_row is not None:
        st.append(("FONTNAME", (0, highlight_row), (-1, highlight_row), "Helvetica-Bold"))
    for c in align_right:
        st.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(st))
    return t


def callout(text, color=ACCENT, mono=False):
    p = Paragraph(text, ParagraphStyle("CO", parent=EQ if mono else BODY,
                                       fontSize=8.8 if mono else 9.4,
                                       leading=12.6 if mono else 13.8,
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


def rule(story, color=RULE, h=0.6):
    story.append(Table([[""]], colWidths=[168 * mm], rowHeights=[h],
                       style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), color)]),
                       hAlign="LEFT"))


def section(story, tag, tag_color, title):
    rule(story)
    story.append(Spacer(1, 7))
    story.append(Paragraph(f'<font color="{tag_color.hexval()}">{tag}</font>', SMALL))
    story.append(Paragraph(title, H1))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.4)
    canvas.line(21 * mm, 14 * mm, 189 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.4)
    canvas.setFillColor(MUTED)
    canvas.drawString(21 * mm, 9.6 * mm,
                      "Review of pranjali2105/SIH_2026  |  SIH 2026  |  Task 4")
    canvas.drawRightString(189 * mm, 9.6 * mm, "Page %d" % doc.page)
    canvas.restoreState()


def P(text):
    return Paragraph(text, CELL)


def PB(text):
    return Paragraph(text, CELLB)


story = []
story.append(Paragraph("SIH 2026  |  TASK 4  |  CROSS-TEAM REVIEW", SMALL))
story.append(Paragraph("Review of SIH_2026: findings for Pranjali", TITLE))
story.append(Paragraph("A gyroscope column that is not what its header says, what that changes "
                       "and what it does not, and how your model and ours do on our phone.", SUB))
story.append(Spacer(1, 5))
rule(story, ACCENT, 1.4)
story.append(Spacer(1, 9))

story.append(Paragraph(
    "This reviews <font face='Courier'>pranjali2105/SIH_2026</font> at <font face='Courier'>47ccec9"
    "</font>, including your final <font face='Courier'>tcn_physics_3s</font> checkpoint "
    "(<font face='Courier'>results/run_tcn/best_ma3.pt</font>). Your last three commits (5-16 Sep) "
    "add the Viterbi decoder, the scheduled-sampling experiment and the drift curves; none of them "
    "touches model code, so the model reviewed here is the one from 1-2 September.", BODY))
story.append(Paragraph(
    "Your findings log made this review possible. Because every result states what it was measured "
    "on, it was possible to trace one of them back to a single line of data loading &mdash; which is "
    "the most useful thing a results log can do.", BODY))

story.append(callout(
    "<b>In one paragraph.</b> In IO-VNBD S-files the gyroscope column headed <b>\"Yaw\" is never the "
    "yaw axis</b>; the one headed <b>\"Pitch\"</b> is, in 30 of 32 sessions. Your pipeline reads the "
    "columns in file order, so the \"raw gyro\" heading your model integrates is the \"Roll\" column "
    "(r = -0.215 with the vehicle's yaw rate), and the INS baseline integrates \"Yaw\" "
    "(r = +0.041). The true axis gives r = +0.905. With perfect speed, heading from the column you "
    "use drifts <b>49.0%</b> of distance at 60 s; from the true axis, <b>5.2%</b>. This likely "
    "explains the headline that heading integration is the dominant error. Everything that does not "
    "read that column &mdash; the map-matched rows, the oracle-heading rows, the speed and "
    "domain-gap findings &mdash; is unaffected.", WARN))

# ------------------------------------------------------------------------------------ 1
section(story, "FINDING 1 &mdash; DATA LOADING", NEG,
        "The column headed \"Yaw\" is not the yaw axis")
story.append(Paragraph(
    "Each raw gyro column was correlated with the vehicle's own yaw rate from the paired V-file. "
    "Per session this uses row-index alignment with no lag search, which depresses the absolute "
    "values on some runs; the <i>ranking</i> is what matters.", BODY))

rows = [["Session", "\"Yaw\"  |r|", "\"Pitch\"  |r|", "\"Roll\"  |r|", "Yaw axis"]]
for s, a, p, r, w in (("S1", ".071", ".935", ".342", "\"Pitch\""),
                      ("S3c", ".047", ".949", ".256", "\"Pitch\""),
                      ("M", ".066", ".636", ".133", "\"Pitch\""),
                      ("Vw3", ".002", ".330", ".018", "\"Pitch\""),
                      ("S2", ".001", ".000", ".005", "noise"),
                      ("Vw16a", ".003", ".002", ".019", "noise")):
    rows.append([s, a, p, r, w])
rows.append(["All 32 sessions", "0 wins", "30 wins", "2 wins", "\"Pitch\""])
story.append(table(rows, [36 * mm, 30 * mm, 30 * mm, 30 * mm, 42 * mm], align_right=(1, 2, 3),
                   highlight_row=len(rows) - 1))
story.append(Spacer(1, 5))
lead_in = Paragraph(
    "The two sessions where \"Roll\" wins are ones where no column exceeds 0.02 &mdash; there is no "
    "yaw signal to rank. Across the whole lag-aligned dataset the picture is unambiguous:", BODY)

rows = [["Channel", "r with yaw rate", "Read by (SIH_2026)"],
        [P("file column \"Yaw\""), P("+0.041"), P("<font face='Courier'>baseline/ins_dr.py:85</font> "
          "&mdash; <font face='Courier'>_find(df, \"GYROSCOPE\", \"YAW\")</font>")],
        [P("file column \"Roll\""), P("-0.215"), P("&mdash;")],
        [P("level-frame gyr_z, as <font face='Courier'>levelled_channels</font> builds it"),
         P("<b>-0.215</b>"),
         P("<font face='Courier'>ModelPredictor(heading_source=\"gyro\")</font>; the NHC target "
           "<font face='Courier'>gyro_yaw = w[:, 5]</font> in <font face='Courier'>train.py</font>")],
        [PB("file column \"Pitch\" &mdash; the real yaw axis"), PB("+0.905"), P("nothing")]]
story.append(KeepTogether([lead_in,
                           table(rows, [54 * mm, 26 * mm, 88 * mm], highlight_row=4)]))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "<b>Why level-frame gyr_z becomes \"Roll\".</b> <font face='Courier'>levelled_channels</font> "
    "takes the three gyro columns in file order and rotates them with the level frame built from "
    "the accelerometer. The accelerometer's axes are in device order, but the gyro columns are not: "
    "file order is device <font face='Courier'>[0, 2, 1]</font>. The phone is clamped almost flat, "
    "so \"down\" is essentially the device's z axis, and the level-frame z of the gyro picks up file "
    "column 2 &mdash; \"Roll\". Rebuilding your gyr_z exactly as your code does gives the same "
    "r = -0.215 as that raw column, which confirms the mapping directly.", BODY))
story.append(Paragraph(
    "Your own normalisation statistics show the symptom: in "
    "<font face='Courier'>results/windows/norm_stats.json</font> the largest gyro spread is on "
    "<b>gyr_y</b> (0.256 rad/s), not gyr_z (0.156). For a car, yaw dominates angular rate, so it is "
    "sitting in a horizontal channel.", BODY))

# ------------------------------------------------------------------------------------ 2
section(story, "FINDING 2 &mdash; IMPACT", WARN, "What integrating the wrong column costs")
story.append(Paragraph(
    "To isolate the axis and nothing else: <b>perfect (GNSS) speed</b>, heading integrated from one "
    "column at a time, each column debiased from its stand-still samples and given whichever sign "
    "suits it best &mdash; so a column is never penalised for a convention, only for carrying the "
    "wrong signal. 26 runs, 743 windows.", BODY))
rows = [["Heading integrated from", "Drift at 60 s", "Drift at 300 s"],
        ["file \"Yaw\"  (your INS baseline)", "53.6%", "82.9%"],
        ["file \"Roll\"  (your model's raw-gyro heading)", "49.0%", "79.6%"],
        ["file \"Pitch\"  (the real yaw axis)", "5.2%", "11.4%"]]
story.append(table(rows, [100 * mm, 34 * mm, 34 * mm], align_right=(1, 2), highlight_row=3))
story.append(Spacer(1, 6))

story.append(Paragraph("Results this touches", H2))
for t in (
    "<b>&sect;0b headline</b> &mdash; \"our model + raw-gyro heading\" at 648.6 m against 294.8 m with "
    "oracle heading. The gap is measured against the \"Roll\" column.",
    "<b>&sect;1</b> &mdash; \"heading integration, not displacement, is the dominant error\". With the "
    "real axis, heading error from the gyro is small; this conclusion likely does not survive.",
    "<b>&sect;2</b> &mdash; \"the yaw head is worse than the raw gyro\". The direction probably "
    "survives: in our pipeline, with the right axis, the head scores r = 0.857 against the gyro's "
    "0.943. But your numbers compare the head against the wrong channel.",
    "<b>&sect;8</b> &mdash; the non-holonomic ESKF \"did not beat raw gyro\", and the NHC training "
    "penalty. Both read the same channel.",
    "<b>The INS baseline</b> (310.5 m at 60 s) integrates a column with essentially no yaw signal, "
    "so the classical baseline is understated."):
    story.append(b(t))

story.append(KeepTogether([Paragraph("Results this does not touch", H2)] + [b(t) for t in (
    "Every <font face='Courier'>*_true_heading</font> row, and constant-velocity DR, which holds the "
    "GNSS heading.",
    "<b>Every map-matched row</b>: the road supplies the direction, so no gyro is integrated. Your "
    "best result stands exactly as reported.",
    "The displacement results, including regression toward the training mean (&sect;3), the "
    "domain-gap analysis (&sect;7b) and despiking (&sect;7c).")]))

story.append(Paragraph("The fix", H2))
story.append(Paragraph(
    "Reorder the gyro columns into the accelerometer's axis order <b>before</b> levelling. In "
    "<font face='Courier'>levelled_channels</font>, take the three columns as "
    "(\"Yaw\", \"Roll\", \"Pitch\") rather than (\"Yaw\", \"Pitch\", \"Roll\"):", BODY))
story.append(callout(
    "gyr = [_find(s.df, r\"GYROSCOPE\", p) for p in (r\"YAW\", r\"ROLL\", r\"PITCH\")]<br/>"
    "# equivalently: device = file[:, [0, 2, 1]]", ACCENT, mono=True))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "and have <font face='Courier'>ins_dr.py</font> integrate the vertical component rather than "
    "the column headed \"Yaw\". A one-line check that it worked: level-frame gyr_z should then "
    "correlate at about +0.9 with the V-file yaw rate, not -0.2. Worth re-running afterwards: "
    "<font face='Courier'>final_scoring</font> with <font face='Courier'>heading_source=\"gyro\"</font>, "
    "the INS baseline, and the NHC run. Only the yaw axis is pinned by this data; which of the "
    "other two columns is device x and which is y is not identifiable from a clamped phone, and "
    "does not matter for heading.", BODY))

# ------------------------------------------------------------------------------------ 3
section(story, "FINDING 3 &mdash; INDEPENDENT OF THE AXIS", WARN,
        "The NHC penalty, as written, can only shrink speed")
story.append(Paragraph(
    "<font face='Courier'>nhc_penalty</font> computes <font face='Courier'>v_lat = mu &middot; "
    "sin(psi)</font> with <font face='Courier'>psi = cumsum(gyro &middot; dt)</font>. The gyro is an "
    "<i>input</i>, so the penalty is:", BODY))
story.append(callout("L_nhc  =  w &middot; mu&sup2; &middot; mean_t( sin&sup2;(psi_t) )", ACCENT,
                     mono=True))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "Its only gradient path is through <font face='Courier'>mu</font>, and it is minimised by "
    "<font face='Courier'>mu = 0</font>: it is an L2 penalty on predicted speed rather than a "
    "constraint. Your pre-registered prediction in <font face='Courier'>losses.py</font> anticipated "
    "exactly this outcome. We measured it in our fork, which uses your "
    "<font face='Courier'>losses.py</font>: raising the weight from 0.05 to 0.2 cost <b>+85%</b> test "
    "RMSE, and solving the loss's stationarity condition at the model's own variance predicted "
    "mu = 0.424 &times; truth against a measured 0.343. At your 0.05 the effect is smaller, which is "
    "consistent with your finding that it did not help. If a physics coupling is wanted, the "
    "centripetal identity <font face='Courier'>a_lat = v &middot; omega</font> constrains two heads "
    "against each other instead of shrinking one; it was the only physics term that ever helped in "
    "our runs (-1.35%).", BODY))
story.append(Paragraph(
    "<b>Smaller point.</b> <font face='Courier'>smoothness()</font> expects batches in session/time "
    "order, but training uses <font face='Courier'>shuffle=True</font>. Your "
    "<font face='Courier'>t0</font> guard stops it becoming a shrinkage term (it did become one in "
    "our fork, which dropped the guard), but in shuffled batches adjacent pairs almost never fall "
    "within 1.5 s, so the term is effectively off. That is a reading of the code, not a "
    "measurement.", BODY))

# ------------------------------------------------------------------------------------ 4
section(story, "FINDING 4 &mdash; ON OUR PHONE", MUTED, "Your model and ours on a real drive")
story.append(Paragraph(
    "There is no clean shared IO-VNBD test set: your training groups include S and M, which "
    "contain our held-out runs, and your test sessions (a5-a8) are not in our pipeline. The one "
    "yardstick neither model has seen is a drive recorded by our app on a Samsung SM-G990E around "
    "IIT Kharagpur (188 s of driving, mean 6.7 m/s). Each model was fed exactly the features it was "
    "trained on; both were then integrated with the same calibrated-compass heading, so the drift "
    "columns compare speed alone.", BODY))
rows = [["Model", "Speed RMSE", "Bias", "r", "vs constant", "Drift 60 s"],
        ["ours (TCN, 10 s window)", "4.86 m/s", "+2.70", "-0.19", "-127%", "19.3%"],
        ["yours, file-order gyro (as trained)", "9.69 m/s", "+9.27", "+0.12", "-358%", "97.5%"],
        ["yours, device-order gyro", "9.52 m/s", "+9.07", "+0.10", "-350%", "100.1%"],
        ["the app's plain integrator", "1.38 m/s", "-0.31", "+0.81", "+35%", "—"]]
story.append(KeepTogether(table(rows, [58 * mm, 22 * mm, 17 * mm, 17 * mm, 25 * mm, 22 * mm],
                                align_right=(1, 2, 3, 4, 5), highlight_row=4)))
story.append(Spacer(1, 5))
story.append(Paragraph(
    "Neither model transfers: both are worse than predicting a constant. Yours over-predicts by "
    "about 9 m/s on a campus drive averaging 6.7 m/s &mdash; the regression toward the training "
    "mean you document in &sect;3, seen from outside the training distribution. Ours fails the same "
    "way, less badly. The phone's own integrator beats both, which says the next gain for either of "
    "us comes from training data recorded on the target device, not from architecture. One drive "
    "on one phone: enough to show neither model transfers, not enough to rank them finely.", BODY))

# ------------------------------------------------------------------------------------ 5
section(story, "WHAT WE ARE TAKING FROM YOUR WORK", ACCENT, "Your best result, and what it changes for us")
story.append(Paragraph(
    "The strongest predictor in <font face='Courier'>results/mapmatch.md</font> is not a learned model "
    "at all: <b>constant-velocity DR walked along the road graph</b>, 285 m to 111-126 m of drift "
    "at 60 s. It does not integrate a gyro, so Finding 1 cannot touch it. The Viterbi, HMM and plain "
    "along-road variants land within a few metres of each other, which is itself informative: the "
    "gain is the road constraint, not the decoder.", BODY))
story.append(Paragraph(
    "Our app already has an along-road tracker, disabled by default. Your numbers are the case for "
    "enabling it with held-GNSS speed during long outages, and that is what we plan to do next. We "
    "also found the same failure your &sect;2 describes on our side, and have switched the app's "
    "outage heading to a calibrated compass with a learned mount offset (5.6-7.5% drift at 60 s on "
    "our drive, against 96.6% for the yaw head).", BODY))

rule(story)
story.append(Spacer(1, 7))
story.append(Paragraph("Reproducing every number here", H2))
story.append(Paragraph(
    "From <font face='Courier'>github.com/ItzRnP-hardcore/Remote_geolocation_sensing</font>, with the "
    "IO-VNBD archive converted by <font face='Courier'>eval/iovnbd.py</font>:", BODY))
story.append(callout(
    "# Findings 1 and 2<br/>"
    "python -m eval.gyro_axis_check<br/><br/>"
    "# Finding 4<br/>"
    "python -m eval.compare_teammate_model extracted_sessions/20260904_195146<br/>"
    "&nbsp;&nbsp;&nbsp;&nbsp;--her-repo &lt;path to SIH_2026&gt;", ACCENT,
    mono=True))
story.append(Spacer(1, 8))
story.append(Paragraph(
    "The axis mapping and the evidence behind it are documented at "
    "<font face='Courier'>GYRO_TO_DEVICE</font> in <font face='Courier'>eval/iovnbd.py</font>. Finding 3's "
    "measurements are in <font face='Courier'>ml_model/PROGRESS.md</font>. Happy to be shown wrong on "
    "any of it &mdash; the scripts are there so it can be. 18 September 2026.", SMALL))

doc = BaseDocTemplate(OUT, pagesize=A4, leftMargin=21 * mm, rightMargin=21 * mm,
                      topMargin=18 * mm, bottomMargin=20 * mm,
                      title="Review of SIH_2026: findings for Pranjali", author="SIH 2026 Task 4")
frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=footer)])
doc.build(story)
print("wrote", OUT)
