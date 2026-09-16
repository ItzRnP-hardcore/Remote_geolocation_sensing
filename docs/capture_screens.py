"""Capture IMU Logger UI screenshots off the phone and compose them into a slide strip.

Usage
-----
    python docs/capture_screens.py            # guided capture, then compose
    python docs/capture_screens.py --compose  # re-compose from what is already on disk
    python docs/capture_screens.py --list     # show the shot list and exit

Plug the phone in with USB debugging on. The script tells you what to put on screen,
you press Enter, it pulls the frame. Files land in docs/screens/.

Nothing here touches the app or the device beyond `screencap`, which is read-only.
"""

import argparse
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(SCRIPT_DIR)
SCREENS = os.path.join(SCRIPT_DIR, "screens")

# The shot list, in the order the story is told on the slide.
# (filename stem, what to have on screen, why it is on the slide)
SHOTS = [
    ("01_offline_map",
     "App open, NOT recording, panel collapsed, zoomed OUT to the whole downloaded region.",
     "proves the map renders with no network, and shows what one region covers"),
    ("02_panel",
     "Zoomed IN to street level, panel EXPANDED: metrics, drift, 3-colour legend, buttons.",
     "the whole interface in one frame - no drive needed"),
    ("03_settings",
     "Settings sheet open, scrolled to the zone list: Eastern installed, others Download.",
     "shows maps are downloaded per region, in-app"),
    ("04_recording",
     "Outdoors. Recording, panel expanded, GNSS chip green, blue GPS track on the map.",
     "live capture: sample counts, satellites, fix age"),
    ("05_freerun",
     "Driving. Free-run ON (panel button red). Blue and dashed-orange tracks visibly APART, "
     "drift figure showing.",
     "the money shot: the cost of losing GPS, measured"),
]


def find_adb():
    """adb from PATH, else from the SDK path in local.properties."""
    from shutil import which
    found = which("adb")
    if found:
        return found
    props = os.path.join(REPO, "local.properties")
    if os.path.isfile(props):
        for line in open(props, encoding="utf-8"):
            if line.startswith("sdk.dir="):
                sdk = line.split("=", 1)[1].strip().replace("\\:", ":").replace("\\\\", "/")
                for name in ("adb.exe", "adb"):
                    cand = os.path.join(sdk, "platform-tools", name)
                    if os.path.isfile(cand):
                        return cand
    return None


def devices(adb):
    out = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
    return [ln.split("\t")[0] for ln in out.splitlines()[1:]
            if ln.strip() and ln.strip().endswith("device")]


def capture(adb, path):
    """`adb exec-out screencap -p` — binary PNG straight to stdout, no temp file on the phone."""
    res = subprocess.run([adb, "exec-out", "screencap", "-p"], capture_output=True)
    if res.returncode != 0 or not res.stdout.startswith(b"\x89PNG"):
        err = (res.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"screencap failed: {err or 'no PNG returned'}")
    with open(path, "wb") as f:
        f.write(res.stdout)
    return len(res.stdout)


def do_capture():
    adb = find_adb()
    if not adb:
        sys.exit("adb not found. Put platform-tools on PATH, or fix sdk.dir in local.properties.")
    devs = devices(adb)
    if not devs:
        sys.exit("No device. Plug the phone in, enable USB debugging, and accept the RSA prompt.\n"
                 f"Check with:  {adb} devices")
    if len(devs) > 1:
        print(f"note: {len(devs)} devices attached, using {devs[0]}")

    os.makedirs(SCREENS, exist_ok=True)
    print(f"\nCapturing to {SCREENS}")
    print("Press Enter to grab, 's' then Enter to skip a shot, 'q' to stop.\n")

    for stem, setup, why in SHOTS:
        print(f"-- {stem}")
        print(f"   set up: {setup}")
        print(f"   why:    {why}")
        answer = input("   ready? [Enter / s / q] ").strip().lower()
        if answer == "q":
            break
        if answer == "s":
            print("   skipped\n")
            continue
        path = os.path.join(SCREENS, stem + ".png")
        try:
            n = capture(adb, path)
        except RuntimeError as e:
            print(f"   FAILED: {e}\n")
            continue
        print(f"   saved {os.path.basename(path)}  ({n // 1024} KB)\n")


# ─────────────────────────────── composition ───────────────────────────────

def compose(height=1400, gap=48, radius=36, border=3):
    """Lay the captured shots side by side at a common height, rounded and outlined.

    Writes docs/screens/strip.png with a transparent background, so it drops onto any
    slide colour without a white rectangle around it.
    """
    from PIL import Image, ImageDraw

    files = [os.path.join(SCREENS, stem + ".png") for stem, _, _ in SHOTS]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        sys.exit(f"No screenshots in {SCREENS}. Run without --compose first.")

    tiles = []
    for path in files:
        img = Image.open(path).convert("RGBA")
        w = max(1, round(img.width * height / img.height))
        img = img.resize((w, height), Image.LANCZOS)

        # Rounded corners via an alpha mask, then a matching outline on top.
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, height - 1], radius, fill=255)
        img.putalpha(mask)
        ImageDraw.Draw(img).rounded_rectangle(
            [border // 2, border // 2, w - 1 - border // 2, height - 1 - border // 2],
            radius, outline=(90, 96, 104, 255), width=border,
        )
        tiles.append(img)

    total_w = sum(t.width for t in tiles) + gap * (len(tiles) - 1)
    strip = Image.new("RGBA", (total_w, height), (0, 0, 0, 0))
    x = 0
    for t in tiles:
        strip.alpha_composite(t, (x, 0))
        x += t.width + gap

    out = os.path.join(SCREENS, "strip.png")
    strip.save(out)
    print(f"wrote {out}  ({strip.width}x{strip.height}, {len(tiles)} shots)")
    print("Shots used:", ", ".join(os.path.basename(f) for f in files))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose", action="store_true", help="skip capture, just rebuild strip.png")
    ap.add_argument("--list", action="store_true", help="print the shot list and exit")
    ap.add_argument("--height", type=int, default=1400, help="tile height in px (default 1400)")
    args = ap.parse_args()

    if args.list:
        for stem, setup, why in SHOTS:
            print(f"{stem}\n    {setup}\n    -> {why}\n")
        sys.exit(0)

    if not args.compose:
        do_capture()
    compose(height=args.height)
