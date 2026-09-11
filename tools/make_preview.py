#!/usr/bin/env python3
"""Bake routemap/preview.png, the Open Graph card Discord and friends show when the site is
linked.

The card has to be a flat image baked ahead of time. Link unfurlers fetch the HTML with a plain
HTTP client and read the <meta> tags; none of them run JavaScript, so there is no Leaflet map
for them to screenshot. Whatever this script writes is the only thing a reader sees before they
click.

Usage:
    python3 tools/make_preview.py worlds/-1636594104014467454
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Open Graph's recommended card is 1200x630 (1.91:1). Discord, Slack and Twitter all render a
# large card at this ratio and letterbox or centre-crop anything else, so matching it exactly is
# what keeps the map from being sliced.
CARD_W, CARD_H = 1200, 630

# Panel colours lifted from routemap/style.css so the card looks like the app it opens.
BG = (20, 23, 28)
FG = (223, 228, 236)
DIM = (139, 148, 163)
ACCENT = (127, 178, 255)

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONT_DIR / name
    if path.exists():
        return ImageFont.truetype(str(path), size)
    # Pillow's built-in bitmap font is fixed at ~11px and ignores `size`. The card is still
    # legible, just ugly; a missing font should not fail the build.
    print(f"warning: {path} not found, falling back to the bitmap font", file=sys.stderr)
    return ImageFont.load_default()


def cover_crop(img: Image.Image, span: int | None) -> Image.Image:
    """Centre-crop `img` to the card aspect and scale to the card size.

    `span` is the width in blocks (= pixels, the renders are 1 px per block) to show. None means
    the full width of the render. A smaller span zooms in: it trades map extent for detail that
    survives the downscale.
    """
    w, h = img.size
    want_w = min(w, span or w)
    want_h = round(want_w * CARD_H / CARD_W)
    if want_h > h:  # too short to fill the card at that width; re-derive from the height
        want_h = h
        want_w = round(want_h * CARD_W / CARD_H)
    left = (w - want_w) // 2
    top = (h - want_h) // 2
    crop = img.crop((left, top, left + want_w, top + want_h))
    # LANCZOS over BOX: the block renders are high-frequency (1 px per block), and box-averaging
    # a 3x downscale turns coastlines into mud.
    return crop.resize((CARD_W, CARD_H), Image.LANCZOS)


def caption(card: Image.Image, title: str, subtitle: str) -> None:
    """Draw the title block over a bottom-up gradient scrim.

    The scrim is not decoration. The text sits over whatever terrain happens to be at the bottom
    of the crop -- snow, desert, a village roof -- and light-on-light is unreadable on the one
    surface where it matters most.
    """
    scrim_h = 240
    scrim = Image.new("L", (1, scrim_h))
    for y in range(scrim_h):
        # Ramp to 94% rather than fully opaque: the map should still read through the darkest
        # part. The exponent stays near-linear on purpose -- a steeper curve keeps the scrim
        # nearly clear until it is level with the text, which is exactly where it is needed.
        scrim.putpixel((0, y), int(240 * (y / (scrim_h - 1)) ** 1.15))
    scrim = scrim.resize((CARD_W, scrim_h))
    card.paste(Image.new("RGB", (CARD_W, scrim_h), BG), (0, CARD_H - scrim_h), scrim)

    d = ImageDraw.Draw(card)
    d.text((48, CARD_H - 118), title, font=_font("DejaVuSans-Bold.ttf", 46), fill=FG)
    d.text((48, CARD_H - 58), subtitle, font=_font("DejaVuSans.ttf", 24), fill=DIM)
    # A thin accent rule anchors the text block to the left edge so it does not float.
    d.rectangle((48, CARD_H - 140, 48 + 76, CARD_H - 136), fill=ACCENT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", type=Path, help="worlds/<seed> directory to render the card from")
    ap.add_argument("-o", "--out", type=Path, default=Path("routemap/preview.jpg"))
    ap.add_argument("--dim", default="0", help="dimension to show (default: 0, the Overworld)")
    ap.add_argument("--base", default="blocks", help="base layer key (default: blocks)")
    ap.add_argument("--span", type=int, default=3200,
                    help="width in blocks to show; smaller zooms in (default: 3200)")
    args = ap.parse_args()

    meta = json.loads((args.bundle / "meta.json").read_text())
    dim = meta["dims"][args.dim]
    base = next((b for b in dim["bases"] if b["key"] == args.base), dim["bases"][0])
    src = args.bundle / f"dim{args.dim}" / base["file"]

    img = Image.open(src)
    if img.size[0] < 100:
        # An LFS pointer decodes as nothing; a truncated render decodes as a sliver. Either way
        # the card would ship broken, and a broken card is worse than no card.
        raise SystemExit(f"{src} is {img.size}, which is too small to be a real render")
    # Flatten onto the app background first: the renders have transparent margins outside the
    # probed area, and PNG alpha over an unknown chat background is a coin flip.
    card = Image.new("RGB", img.size, BG)
    card.paste(img.convert("RGBA"), (0, 0), img.convert("RGBA"))

    card = cover_crop(card, args.span)
    caption(card, "GTNH Route Map",
            f"seed {meta['seed']}  ·  {meta['pack']}  ·  ore veins, loot, POIs")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.suffix.lower() in (".jpg", ".jpeg"):
        # JPEG, not PNG, and the reason is size: the hillshade gives every block its own shade,
        # so the card holds ~200k distinct colours and PNG-compresses to 1.5 MB. Quantising to a
        # 256-colour palette halves that but drops the accent rule to grey, because median-cut
        # spends its palette on terrain and ignores a 76 px detail. JPEG keeps the colour and
        # lands around 250 KB. Ringing is invisible at this scale.
        card.save(args.out, quality=88, optimize=True, progressive=True)
    else:
        card.save(args.out, optimize=True)
    kb = args.out.stat().st_size / 1024
    print(f"wrote {args.out} ({CARD_W}x{CARD_H}, {kb:.0f} KB) from {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
