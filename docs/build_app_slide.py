"""Assemble the single SIH-format slide: flowchart + UI screenshots + key numbers.

    python docs/build_app_flowchart.py     # 1. the diagram
    python docs/capture_screens.py         # 2. the screenshots (needs the phone)
    python docs/build_app_slide.py         # 3. this - writes docs/APP_SLIDE.pptx

Runs fine before step 2: it substitutes labelled placeholders so the layout can be
checked, and picks up the real strip the moment it exists.

SIH chrome
----------
By default the branding is drawn here: thin grey rule at the top, blue footer bar with
the page number, and a slot for the SIH logo top-right. There is deliberately NO team
name oval.

Two ways to get the real logo in:
  * drop the official deck at  docs/assets/sih_template.pptx  - its layouts (and so its
    logo and footer, however they are drawn) are used directly; or
  * drop just the logo at      docs/assets/sih_logo.png       - placed at the template's
    own position, top-right.
With neither, a dashed placeholder marks the spot so you can paste the logo in PowerPoint.
"""

import argparse
import io
import os
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Emu, Inches, Pt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENS = os.path.join(SCRIPT_DIR, "screens")
ASSETS = os.path.join(SCRIPT_DIR, "assets")
FLOWCHART = os.path.join(SCRIPT_DIR, "app_flowchart.png")
STRIP = os.path.join(SCREENS, "strip.png")
PLACEHOLDER = os.path.join(SCREENS, "strip_placeholder.png")
SIH_TEMPLATE = os.path.join(ASSETS, "sih_template.pptx")
SIH_LOGO = os.path.join(ASSETS, "sih_logo.png")
OUT = os.path.join(SCRIPT_DIR, "APP_SLIDE.pptx")

INK = RGBColor(0x1A, 0x1A, 0x1A)
MUTED = RGBColor(0x5F, 0x63, 0x68)
ACCENT = RGBColor(0x1F, 0x5C, 0x8B)
SIH_BLUE = RGBColor(0x00, 0x70, 0xC0)
RULE_GREY = RGBColor(0xD9, 0xD9, 0xD9)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

SLIDE_W, SLIDE_H = 13.333, 7.5          # inches, 16:9

# Chrome geometry, measured off the SIH template slide.
TOP_RULE_Y, TOP_RULE_H = 0.06, 0.025
FOOTER_Y = 6.98                          # blue bar top; content must end above this
FOOTER_H = SLIDE_H - FOOTER_Y
LOGO_L, LOGO_T, LOGO_W, LOGO_H = 10.65, 0.22, 2.50, 1.00

TITLE = "IMU Logger — the application"
KICKER = ("One phone, eleven sensors, no API keys, no internet at runtime  ·  "
          "records the evidence and dead-reckons at the same time")
PUNCHLINE = ("The map, the road network and the positioning all live on the device — "
             "it works in a tunnel because it never needed a server.")

# Keyed by screenshot stem, so the caption line always describes the shots that are
# actually in the strip - not however many the list happens to hold.
CAPTIONS = {
    "01_offline_map": "Offline vector map, no network",
    "02_panel": "Session panel: metrics, drift, legend",
    "03_recording": "Recording: 11 streams, 25 sats",
    "04_freerun": "Free-run: the GPS gap, measured",
}

# Three, not four: the screenshots are the point of the right-hand column, and every
# extra row here comes straight out of their height.
NUMBERS = [
    ("1,312", "IMU samples per second, 11 streams, one monotonic clock"),
    ("0", "API keys, backends, network calls at runtime"),
    ("210 MB", "one file: the basemap AND the road network for matching"),
]


# ─────────────────────────────── helpers ───────────────────────────────

def placeholder_strip(n=3, height=1400, gap=48):
    """Phone-shaped stand-ins, so the layout is checkable before the phone is plugged in."""
    from PIL import Image, ImageDraw, ImageFont

    os.makedirs(SCREENS, exist_ok=True)
    try:
        font = ImageFont.truetype("arialbd.ttf", 46)
    except OSError:
        font = ImageFont.load_default()
    w = round(height * 1080 / 2400)
    total = n * w + gap * (n - 1)
    img = Image.new("RGBA", (total, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i in range(n):
        x = i * (w + gap)
        draw.rounded_rectangle([x, 0, x + w - 1, height - 1], 36,
                               fill=(238, 240, 243, 255), outline=(150, 156, 164, 255), width=4)
        for j, line in enumerate(("SCREENSHOT", str(i + 1), "", "run", "capture_screens.py")):
            draw.text((x + w / 2, height / 2 - 110 + j * 56), line,
                      fill=(120, 126, 134, 255), anchor="mm", font=font)
    img.save(PLACEHOLDER)
    return PLACEHOLDER


def textbox(slide, left, top, width, height, runs, *, align=PP_ALIGN.LEFT, anchor=None):
    """runs: list of (text, size_pt, bold, colour) - one paragraph each."""
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    if anchor is not None:
        tf.vertical_anchor = anchor
    for i, (text, size, bold, colour) in enumerate(runs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = colour
        run.font.name = "Calibri"
    return box


def rect(slide, left, top, width, height, fill):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top),
                                   Inches(width), Inches(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def picture_fit(slide, path, left, top, max_w, max_h):
    """Place an image scaled to fit the box, centred horizontally in it."""
    from PIL import Image
    with Image.open(path) as im:
        aspect = im.width / im.height
    w, h = max_w, max_w / aspect
    if h > max_h:
        h, w = max_h, max_h * aspect
    return slide.shapes.add_picture(path, Inches(left + (max_w - w) / 2), Inches(top),
                                    Inches(w), Inches(h))


def add_sih_chrome(slide, page_number):
    """Thin grey rule, blue footer bar with the page number, and the logo (or its slot).

    No team-name oval, by request.
    """
    rect(slide, 0, TOP_RULE_Y, SLIDE_W, TOP_RULE_H, RULE_GREY)
    rect(slide, 0, FOOTER_Y, SLIDE_W, FOOTER_H, SIH_BLUE)
    textbox(slide, SLIDE_W - 1.3, FOOTER_Y + 0.12, 0.9, 0.3,
            [(str(page_number), 12, True, WHITE)], align=PP_ALIGN.RIGHT)

    if os.path.isfile(SIH_LOGO):
        picture_fit(slide, SIH_LOGO, LOGO_L, LOGO_T, LOGO_W, LOGO_H)
        return True

    slot = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LOGO_L), Inches(LOGO_T),
                                  Inches(LOGO_W), Inches(LOGO_H))
    slot.fill.background()
    slot.line.color.rgb = RGBColor(0xBB, 0xBB, 0xBB)
    slot.line.width = Pt(1)
    slot.shadow.inherit = False
    tf = slot.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = "paste SIH logo here"
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
    run.font.name = "Calibri"
    return False


def blank_layout(prs):
    """The emptiest layout available - preferred name first, else fewest placeholders."""
    for layout in prs.slide_layouts:
        if layout.name.strip().lower() == "blank":
            return layout
    return min(prs.slide_layouts, key=lambda lay: len(lay.placeholders))


# ─────────────────────────────── the slide ───────────────────────────────

def main(page_number=5):
    if not os.path.isfile(FLOWCHART):
        sys.exit("Missing docs/app_flowchart.png - run: python docs/build_app_flowchart.py")

    strip = STRIP if os.path.isfile(STRIP) else placeholder_strip()
    using_real = strip == STRIP

    using_template = os.path.isfile(SIH_TEMPLATE)
    prs = Presentation(SIH_TEMPLATE) if using_template else Presentation()
    if not using_template:
        prs.slide_width, prs.slide_height = Inches(SLIDE_W), Inches(SLIDE_H)
    slide = prs.slides.add_slide(blank_layout(prs))

    # With the official template, the branding comes from its master/layout; drawing our
    # own on top would double it up.
    logo_ok = True if using_template else add_sih_chrome(slide, page_number)

    # Header. Kept clear of the logo, which starts at 10.65in.
    textbox(slide, 0.45, 0.30, 9.9, 0.5, [(TITLE, 26, True, INK)])
    textbox(slide, 0.45, 0.86, 9.9, 0.34, [(KICKER, 11.5, False, ACCENT)])

    # Flowchart, left.
    picture_fit(slide, FLOWCHART, 0.35, 1.34, 8.30, 5.15)

    # Screenshots, right.
    SS_L, SS_W = 9.00, 4.00
    pic = picture_fit(slide, strip, SS_L, 1.34, SS_W, 3.30)
    strip_bottom = Emu(pic.top + pic.height).inches

    if using_real:
        present = [stem for stem in CAPTIONS
                   if os.path.isfile(os.path.join(SCREENS, stem + ".png"))]
        caption = "  ·  ".join(CAPTIONS[stem] for stem in present)
    else:
        caption = "placeholders - run docs/capture_screens.py with the phone attached"
    textbox(slide, SS_L, strip_bottom + 0.08, SS_W, 0.3,
            [(caption, 8.5, False, MUTED)], align=PP_ALIGN.CENTER)

    # Key numbers, bottom right.
    top = strip_bottom + 0.48
    for value, label in NUMBERS:
        textbox(slide, SS_L, top, 1.35, 0.32, [(value, 17, True, ACCENT)])
        textbox(slide, SS_L + 1.4, top + 0.02, SS_W - 1.4, 0.5, [(label, 9, False, MUTED)])
        top += 0.50

    # Punchline, sitting just above the blue bar.
    textbox(slide, 0.45, 6.58, 12.4, 0.32, [(PUNCHLINE, 11, True, ACCENT)])

    prs.save(OUT)
    print("wrote", OUT)
    print("  chrome     :", "from docs/assets/sih_template.pptx" if using_template
          else "drawn here (grey rule + blue bar + page %s, no team oval)" % page_number)
    if not using_template:
        print("  logo       :", "docs/assets/sih_logo.png" if logo_ok
              else "PLACEHOLDER - drop docs/assets/sih_logo.png, or paste it in PowerPoint")
    print("  screenshots:", "real" if using_real
          else "PLACEHOLDERS - capture, then re-run this")

    preview(prs)


def preview(prs, dpi=110):
    """Flat PNG mock of the slide, so the layout can be eyeballed without PowerPoint.

    Not a renderer - it re-walks the shapes and draws them with PIL at the same
    coordinates. Fonts differ slightly from PowerPoint's, so treat it as a layout
    check, not a proof of the final typography.
    """
    from PIL import Image, ImageDraw, ImageFont

    def font(px, bold=False):
        for name in (("arialbd.ttf", "calibrib.ttf") if bold else ("arial.ttf", "calibri.ttf")):
            try:
                return ImageFont.truetype(name, px)
            except OSError:
                continue
        return ImageFont.load_default()

    img = Image.new("RGB", (round(SLIDE_W * dpi), round(SLIDE_H * dpi)), "white")
    draw = ImageDraw.Draw(img)

    for sh in prs.slides[0].shapes:
        x, y = Emu(sh.left).inches * dpi, Emu(sh.top).inches * dpi
        w, h = Emu(sh.width).inches * dpi, Emu(sh.height).inches * dpi

        if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
            src = Image.open(io.BytesIO(sh.image.blob)).convert("RGBA")
            src = src.resize((max(1, round(w)), max(1, round(h))), Image.LANCZOS)
            img.paste(src, (round(x), round(y)), src)
            continue

        if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
            fill = None
            try:
                if sh.fill.type is not None and sh.fill.type == 1:      # solid
                    fill = tuple(sh.fill.fore_color.rgb)
            except (TypeError, AttributeError):
                pass
            outline = None
            try:
                outline = tuple(sh.line.color.rgb)
            except (TypeError, AttributeError):
                pass
            draw.rectangle([x, y, x + w, y + h], fill=fill, outline=outline)

        if not sh.has_text_frame:
            continue
        cursor = y
        for p in sh.text_frame.paragraphs:
            for run in p.runs:
                px = round(run.font.size.pt * dpi / 72) if run.font.size else 14
                f = font(px, bool(run.font.bold))
                colour = tuple(run.font.color.rgb) if run.font.color and \
                    run.font.color.type is not None else (0, 0, 0)
                words, line, lines = run.text.split(), "", []
                for word in words:
                    trial = f"{line} {word}".strip()
                    if draw.textlength(trial, font=f) > w and line:
                        lines.append(line)
                        line = word
                    else:
                        line = trial
                lines.append(line)
                for ln in lines:
                    tx = x
                    if p.alignment == PP_ALIGN.CENTER:
                        tx = x + (w - draw.textlength(ln, font=f)) / 2
                    elif p.alignment == PP_ALIGN.RIGHT:
                        tx = x + w - draw.textlength(ln, font=f)
                    draw.text((tx, cursor), ln, font=f, fill=colour)
                    cursor += px * 1.22

    out = os.path.join(SCRIPT_DIR, "app_slide_preview.png")
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", type=int, default=5, help="page number in the blue bar")
    main(page_number=ap.parse_args().page)
