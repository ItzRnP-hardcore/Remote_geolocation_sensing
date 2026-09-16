"""Fill the SIH Odyssey template slide with the IMU Logger application slide.

    python docs/build_app_flowchart.py     # 1. the diagram
    python docs/capture_screens.py         # 2. the screenshots (needs the phone)
    python docs/build_odyssey_slide.py     # 3. this

Works on the real SIH deck, so the logo, the blue footer bar and the page number are the
template's own, not a reconstruction. The team-name oval (Group 8) is deleted, as asked.

The original is copied to *.original.pptx before anything is written.
"""

import os
import shutil
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENS = os.path.join(SCRIPT_DIR, "screens")
FLOWCHART = os.path.join(SCRIPT_DIR, "app_flowchart.png")
STRIP = os.path.join(SCREENS, "strip.png")

DECK = r"C:\Users\rudra\Downloads\SIH_Presentation_Odyssey.pptx"
BACKUP = DECK.replace(".pptx", ".original.pptx")

# Shapes to remove from the template slide, by name. Group 8 is the ODYSSEY team logo
# (freeform + "ODYSSEY" text box) and is deliberately kept: it replaces the slide title.
DROP_SHAPES = set()

INK = RGBColor(0x1A, 0x1A, 0x1A)
MUTED = RGBColor(0x5F, 0x63, 0x68)
ACCENT = RGBColor(0x1F, 0x5C, 0x8B)

KICKER = ("One phone · eleven sensors · no API keys · no internet at runtime  —  "
          "strapdown integrator AND an on-device ML thread, matched to the road")
PUNCHLINE = ("The map, the road network and the positioning all live on the device — "
             "it works in a tunnel because it never needed a server.")

CAPTIONS = {
    "01_offline_map": "Whole downloaded region, rendered offline",
    "02_panel": "Local view + live session panel",
    "03_settings": "Offline maps, downloaded per region",
    "04_recording": "Recording: 11 streams, 25 satellites",
    "05_freerun": "Free-run: the GPS gap, measured",
}

NUMBERS = [
    ("1,312", "IMU samples per second, 11 streams, one monotonic clock"),
    ("0", "API keys, backends, or network calls at runtime"),
    ("210 MB", "per region — the Eastern zone's basemap AND its road network"),
]


def textbox(slide, left, top, width, height, text, size, bold, colour,
            align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = colour
    run.font.name = "Calibri"
    return box


def picture_fit(slide, path, left, top, max_w, max_h):
    from PIL import Image
    with Image.open(path) as im:
        aspect = im.width / im.height
    w, h = max_w, max_w / aspect
    if h > max_h:
        h, w = max_h, max_h * aspect
    return slide.shapes.add_picture(path, Inches(left + (max_w - w) / 2), Inches(top),
                                    Inches(w), Inches(h))


def main():
    for path, what in ((DECK, "the deck"), (FLOWCHART, "the flowchart"), (STRIP, "the strip")):
        if not os.path.isfile(path):
            sys.exit(f"Missing {what}: {path}")

    # Idempotent by construction: the pristine template is kept as BACKUP and is always the
    # input, so re-running replaces the slide's content instead of stacking another copy on it.
    if not os.path.isfile(BACKUP):
        shutil.copy2(DECK, BACKUP)
        print("backed up ->", BACKUP)

    prs = Presentation(BACKUP)
    slide = prs.slides[0]
    W, H = Emu(prs.slide_width).inches, Emu(prs.slide_height).inches

    # Drop the team-name oval.
    for shape in list(slide.shapes):
        if shape.name in DROP_SHAPES:
            shape._element.getparent().remove(shape._element)
            print("removed", shape.name)

    # Header: no title of our own. The ODYSSEY logo (Group 8, top-left) names the slide,
    # so the strapline sits between it and the SIH logo and carries the whole message.
    textbox(slide, 3.90, 0.78, 12.70, 1.00, KICKER, 19, True, ACCENT)

    # Flowchart, left.
    picture_fit(slide, FLOWCHART, 0.55, 2.05, 12.30, 7.70)

    # Screenshots, right.
    SS_L, SS_W = 13.20, 6.30
    pic = picture_fit(slide, STRIP, SS_L, 2.05, SS_W, 4.60)
    strip_bottom = Emu(pic.top + pic.height).inches

    present = [stem for stem in CAPTIONS
               if os.path.isfile(os.path.join(SCREENS, stem + ".png"))]
    caption = "   ·   ".join(CAPTIONS[stem] for stem in present)
    textbox(slide, SS_L, strip_bottom + 0.12, SS_W, 0.6, caption, 11.5, False, MUTED,
            align=PP_ALIGN.CENTER)

    # Key numbers, bottom right.
    top = strip_bottom + 0.85
    for value, label in NUMBERS:
        textbox(slide, SS_L, top, 2.05, 0.5, value, 26, True, ACCENT)
        textbox(slide, SS_L + 2.15, top + 0.05, SS_W - 2.15, 0.8, label, 12.5, False, MUTED)
        top += 0.80

    # Punchline, above the template's blue bar (which starts at 10.42in).
    textbox(slide, 0.62, 9.86, 15.9, 0.45, PUNCHLINE, 15, True, ACCENT)

    try:
        prs.save(DECK)
        out = DECK
    except PermissionError:
        out = DECK.replace(".pptx", "_app.pptx")
        prs.save(out)
        print("NOTE: the deck was locked (open in PowerPoint?), wrote a copy instead")

    print("wrote", out)
    print("  slide size  : %.2f x %.2f in" % (W, H))
    print("  screenshots :", ", ".join(present))
    preview(prs, W, H)


def preview(prs, slide_w, slide_h, dpi=90):
    """Flat PNG mock for a layout check - fonts and grouped template art are approximate."""
    import io
    from PIL import Image, ImageDraw, ImageFont

    def font(px, bold=False):
        for name in (("arialbd.ttf",) if bold else ("arial.ttf",)):
            try:
                return ImageFont.truetype(name, px)
            except OSError:
                pass
        return ImageFont.load_default()

    img = Image.new("RGB", (round(slide_w * dpi), round(slide_h * dpi)), "white")
    draw = ImageDraw.Draw(img)
    for sh in prs.slides[0].shapes:
        x, y = Emu(sh.left).inches * dpi, Emu(sh.top).inches * dpi
        w, h = Emu(sh.width).inches * dpi, Emu(sh.height).inches * dpi
        if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
            src = Image.open(io.BytesIO(sh.image.blob)).convert("RGBA")
            src = src.resize((max(1, round(w)), max(1, round(h))), Image.LANCZOS)
            img.paste(src, (round(x), round(y)), src)
            continue
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP or sh.shape_type == MSO_SHAPE_TYPE.FREEFORM:
            draw.rectangle([x, y, x + w, y + h], outline=(200, 200, 200))
            draw.text((x + 4, y + 4), "[template art]", font=font(11), fill=(170, 170, 170))
            continue
        if not sh.has_text_frame:
            continue
        cursor = y
        for p in sh.text_frame.paragraphs:
            for run in p.runs:
                px = round((run.font.size.pt if run.font.size else 14) * dpi / 72)
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
                    draw.text((tx, cursor), ln, font=f, fill=colour)
                    cursor += px * 1.22

    out = os.path.join(SCRIPT_DIR, "odyssey_slide_preview.png")
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
