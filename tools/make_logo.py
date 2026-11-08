#!/usr/bin/env python3
"""Generate the square brand logo used by the Organization JSON-LD."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "images" / "logo.png"
CJK = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
BLUE = (0, 112, 243)
GREEN = (94, 186, 125)
ORANGE = (245, 166, 35)
TEXT = (245, 245, 245)


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(CJK, size, index=0)


def penguin(img: Image.Image, x: int, y: int, s: int = 30):
    d = ImageDraw.Draw(img)
    d.ellipse((x, y + 2, x + s, y + s + 4), fill=(17, 17, 17), outline=(60, 60, 60), width=max(2, s // 16))
    d.ellipse((x + s * 0.22, y + s * 0.36, x + s * 0.78, y + s * 1.02), fill=(237, 237, 237))
    d.ellipse((x + s * 0.34, y + s * 0.38, x + s * 0.42, y + s * 0.48), fill=(17, 17, 17))
    d.ellipse((x + s * 0.58, y + s * 0.38, x + s * 0.66, y + s * 0.48), fill=(17, 17, 17))
    d.polygon([(x + s * 0.46, y + s * 0.56), (x + s * 0.54, y + s * 0.56), (x + s * 0.50, y + s * 0.66)], fill=ORANGE)


def main():
    size = 512
    img = Image.new("RGB", (size, size), (10, 10, 10))

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((250, -180, 700, 300), fill=(0, 112, 243, 70))
    gd.ellipse((-200, 300, 320, 760), fill=(94, 186, 125, 30))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")

    grid = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, size, 32):
        gd.line((x, 0, x, size), fill=(255, 255, 255, 10), width=1)
    for y in range(0, size, 32):
        gd.line((0, y, size, y), fill=(255, 255, 255, 10), width=1)
    img = Image.alpha_composite(img.convert("RGBA"), grid).convert("RGB")

    penguin(img, 161, 54, 190)
    d = ImageDraw.Draw(img)
    text = "VERTICA TIPS"
    f = font(42)
    bbox = d.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    d.text(((size - tw) / 2, 360), text, font=f, fill=TEXT, stroke_width=1, stroke_fill=TEXT)

    sub = "25.4 FIELD NOTES"
    sf = font(18)
    sb = d.textbbox((0, 0), sub, font=sf)
    sw = sb[2] - sb[0]
    d.text(((size - sw) / 2, 420), sub, font=sf, fill=(154, 154, 154))

    for x in range(size):
        t = x / size
        if t < 0.55:
            c = tuple(int(BLUE[i] + (GREEN[i] - BLUE[i]) * (t / 0.55)) for i in range(3))
        else:
            c = tuple(int(GREEN[i] + (ORANGE[i] - GREEN[i]) * ((t - 0.55) / 0.45)) for i in range(3))
        d.line((x, size - 8, x, size - 1), fill=c)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(OUT)


if __name__ == "__main__":
    main()
