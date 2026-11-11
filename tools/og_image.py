#!/usr/bin/env python3
"""Generate 1200x630 Open Graph cards for Vertica Tips.

Two cards per article: one Chinese, one English. The card is a deterministic
Pillow render, not an AI image. It reads the post front matter, draws the
card, writes the PNG, and adds an `image:` field to the post.

Usage:
  python3 tools/og_image.py --all
  python3 tools/og_image.py --slug vertica-partition-design-pruning
  python3 tools/og_image.py --lang zh --title "..." --subtitle "..." \
      --category PARTITION --kicker "VERTICA 25.4 · 實測" --out /tmp/card.png
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
POSTS = ROOT / "_posts"
EN_POSTS = ROOT / "_en"
OUT_DIR = ROOT / "assets" / "images" / "og"

W, H = 1200, 630
CJK = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG = (10, 10, 10)
TEXT = (245, 245, 245)
MUTED = (154, 154, 154)
BLUE = (0, 112, 243)
BLUE_L = (124, 184, 255)
GREEN = (94, 186, 125)
ORANGE = (245, 166, 35)

# category token -> (pill label, visual label)
CATEGORY_META = {
    "data-load": ("DATA LOAD", "COPY · ROS · RESOURCE POOL"),
    "health-watchdog": ("WATCHDOG", "TRUNCATION · GCLX · MERGEOUT"),
    "iceberg": ("ICEBERG", "CATALOG · PARQUET · S3"),
    "node-recovery": ("RECOVERY", "BUDDY · EPOCH · CATALOG"),
    "rebalancing": ("REBALANCING", "SEGMENT · MOVE · VERIFY"),
    "performance": ("PERFORMANCE", "PLAN · PROFILE · TUNE"),
    "smart-scanning": ("SMART SCAN", "GLOB · PRUNE · READ"),
    "disaster-recovery": ("ASR", "EPOCH · AHM · RECOVERY"),
    "asr": ("ASR", "EPOCH · AHM · RECOVERY"),
    "delete": ("DELETE", "VECTOR · PURGE · AHM"),
    "log-analysis": ("LOG RCA", "QUERY · EVENT · LOG"),
    "machine-learning": ("ML", "TRAIN · PREDICT · TUNE"),
    "oom": ("OOM", "MEMORY · CATALOG · KERNEL"),
    "resource-pool": ("RESOURCE POOL", "QUEUE · MEMORY · PRIORITY"),
    "tuple-mover": ("TUPLE MOVER", "MOVEOUT · MERGEOUT · STRATA"),
    "numa": ("NUMA", "CORE · MEMORY · THROUGHPUT"),
    "storage": ("SPACE AUDIT", "ROS · BYTES · PURGE"),
    "vector": ("VECTOROPS", "EMBED · COSINE · TOP-K"),
    "statistics": ("STATISTICS", "ESTIMATE · ANALYZE · PLAN"),
    "projection": ("PROJECTION", "SEGMENT · SORT · ENCODE"),
    "partition": ("PARTITION", "SCAN 1 / 24 · PRUNE"),
    "release-notes": ("RELEASE", "PATCH · CVE · UPGRADE"),
}


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(CJK, size, index=0)


def mono(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(MONO_B if bold else MONO, size)


def text_w(draw: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(s, font=f)


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: float) -> list[str]:
    """Wrap mixed CJK/Latin text. CJK chars break anywhere; Latin words at spaces."""
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if ch.isspace():
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(" ")
        elif "\u4e00" <= ch <= "\u9fff" or ch in "，。、；：！？（）【】「」":
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
        else:
            buf += ch
    if buf:
        tokens.append(buf)

    lines: list[str] = []
    cur = ""
    for tok in tokens:
        candidate = cur + tok
        if text_w(draw, candidate.rstrip(), f) <= max_w or not cur:
            cur = candidate
        else:
            if tok in "，。、；：！？）】」":
                # Closing punctuation must not start a line; allow a tiny overflow.
                lines.append((cur + tok).rstrip())
                cur = ""
            else:
                lines.append(cur.rstrip())
                cur = "" if tok == " " else tok
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def fit_title(draw: ImageDraw.ImageDraw, text: str, max_w: float) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in (54, 50, 46, 42):
        f = font(size)
        lines = wrap(draw, text, f, max_w)
        if len(lines) <= 2:
            return f, lines
    for size in (40, 38, 36, 34):
        f = font(size)
        lines = wrap(draw, text, f, max_w)
        if len(lines) <= 3:
            return f, lines
    f = font(34)
    return f, wrap(draw, text, f, max_w)[:3]


def rounded(draw: ImageDraw.ImageDraw, box, r, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def penguin(img: Image.Image, x: int, y: int, s: int = 30):
    d = ImageDraw.Draw(img)
    d.ellipse((x, y + 2, x + s, y + s + 4), fill=(17, 17, 17), outline=(60, 60, 60), width=2)
    d.ellipse((x + s * 0.22, y + s * 0.36, x + s * 0.78, y + s * 1.02), fill=(237, 237, 237))
    d.ellipse((x + s * 0.34, y + s * 0.38, x + s * 0.42, y + s * 0.48), fill=(17, 17, 17))
    d.ellipse((x + s * 0.58, y + s * 0.38, x + s * 0.66, y + s * 0.48), fill=(17, 17, 17))
    d.polygon([(x + s * 0.46, y + s * 0.56), (x + s * 0.54, y + s * 0.56), (x + s * 0.50, y + s * 0.66)], fill=ORANGE)


def build_card(title: str, subtitle: str, category: str, kicker: str, visual_label: str, out: Path):
    img = Image.new("RGB", (W, H), BG)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((760, -120, 1420, 420), fill=(0, 112, 243, 58))
    gd.ellipse((-220, 420, 520, 900), fill=(94, 186, 125, 26))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")

    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, W, 48):
        alpha = max(0, int(14 * (1 - x / W)))
        gd.line((x, 0, x, H), fill=(255, 255, 255, alpha), width=1)
    for y in range(0, H, 48):
        alpha = max(0, int(12 * (1 - y / H * 0.5)))
        gd.line((0, y, W, y), fill=(255, 255, 255, alpha), width=1)
    img = Image.alpha_composite(img.convert("RGBA"), grid).convert("RGB")

    penguin(img, 56, 34, 32)
    d = ImageDraw.Draw(img)
    d.text((100, 38), "Vertica Tips", font=font(21), fill=TEXT, stroke_width=1, stroke_fill=TEXT)
    d.text((258, 44), "/", font=font(19), fill=(58, 58, 58))
    d.text((278, 45), "TECHNICAL NOTES", font=mono(13), fill=(138, 138, 138))

    # Uniform badge + category pill (top right)
    cat = category.upper()
    cf = mono(13, bold=True)
    pill_w = int(text_w(d, cat, cf)) + 46
    pill_x = W - 56 - pill_w
    rounded(d, (pill_x, 36, pill_x + pill_w, 70), 17, fill=(10, 38, 78), outline=(0, 112, 243), width=1)
    d.ellipse((pill_x + 14, 49, pill_x + 21, 56), fill=BLUE)
    d.text((pill_x + 30, 46), cat, font=cf, fill=BLUE_L)

    badge = "VERTICA TIPS"
    bf = mono(13)
    bw = text_w(d, badge, bf)
    bx = pill_x - 26 - bw
    d.ellipse((bx - 14, 49, bx - 7, 56), fill=BLUE)
    d.text((bx, 46), badge, font=bf, fill=(138, 138, 138))

    d.text((56, 148), kicker.upper(), font=mono(14, bold=True), fill=GREEN)

    tf, lines = fit_title(d, title, 700)
    y = 168
    for line in lines:
        d.text((56, y), line, font=tf, fill=TEXT, stroke_width=1, stroke_fill=TEXT)
        y += int(tf.size * 1.2)

    sf = font(20)
    all_slines = wrap(d, subtitle, sf, 720)
    slines = all_slines[:2]
    if len(all_slines) > 2 and slines:
        last = slines[1]
        while last and text_w(d, last + "…", sf) > 720:
            last = last[:-1]
        slines[1] = last + "…"
    y = max(y + 10, 312)
    for line in slines:
        d.text((56, y), line, font=sf, fill=MUTED)
        y += 26

    d.rounded_rectangle((56, 396, 120, 399), radius=2, fill=BLUE)

    # Abstract data-block grid
    gx, gy, gw, gh = W - 356, 214, 300, 264
    cols, rows = 6, 4
    gap = 8
    cw = (gw - gap * (cols - 1)) / cols
    ch = (gh - gap * (rows - 1)) / rows
    hot = 14
    hx = gx + (hot % cols) * (cw + gap)
    hy = gy + (hot // cols) * (ch + gap)
    cell_glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cgd = ImageDraw.Draw(cell_glow)
    cgd.rounded_rectangle((hx - 4, hy - 4, hx + cw + 4, hy + ch + 4), radius=14, fill=(0, 112, 243, 90))
    cell_glow = cell_glow.filter(ImageFilter.GaussianBlur(18))
    img = Image.alpha_composite(img.convert("RGBA"), cell_glow).convert("RGB")
    d = ImageDraw.Draw(img)

    for i in range(cols * rows):
        x = gx + (i % cols) * (cw + gap)
        y2 = gy + (i // cols) * (ch + gap)
        if i == hot:
            rounded(d, (x, y2, x + cw, y2 + ch), 10, fill=(0, 52, 112), outline=(0, 112, 243), width=2)
            d.rounded_rectangle((x + 8, y2 + 9, x + cw - 8, y2 + 18), radius=4, fill=(0, 112, 243))
        else:
            fill = (16, 16, 16) if i % 3 == 0 else (20, 20, 20)
            outline = (36, 36, 36) if i % 3 == 0 else (50, 50, 50)
            rounded(d, (x, y2, x + cw, y2 + ch), 10, fill=fill, outline=outline, width=1)

    d.text((gx, gy + gh + 16), "MEASURED", font=mono(11), fill=(91, 91, 91))
    d.text((gx + 74, gy + gh + 15), visual_label, font=mono(11, bold=True), fill=BLUE_L)

    d.text((56, H - 52), "achi0012.github.io/vertica-tips", font=mono(15), fill=(185, 185, 185))
    lang = "中文  ·  English"
    d.text((W - 56 - text_w(d, lang, font(15)), H - 52), lang, font=font(15), fill=(111, 111, 111))

    for x in range(W):
        t = x / W
        if t < 0.55:
            c = tuple(int(BLUE[i] + (GREEN[i] - BLUE[i]) * (t / 0.55)) for i in range(3))
        else:
            c = tuple(int(GREEN[i] + (ORANGE[i] - GREEN[i]) * ((t - 0.55) / 0.45)) for i in range(3))
        d.line((x, H - 4, x, H - 1), fill=c)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


def parse_front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise ValueError(f"no front matter: {path}")
    return yaml.safe_load(m.group(1)) or {}


def set_image_field(path: Path, image_path: str):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"no front matter: {path}")
    end = lines.index("---", 1)
    fm = [l for l in lines[1:end] if not l.startswith("image:")]
    idx = next((i for i, l in enumerate(fm) if l.startswith("description:")), None)
    if idx is None:
        idx = next((i for i, l in enumerate(fm) if l.startswith("tags:")), len(fm) - 1)
    fm.insert(idx + 1, f"image: {image_path}")
    new = "\n".join([lines[0]] + fm + lines[end:]) + "\n"
    path.write_text(new, encoding="utf-8")


def category_token(categories: str) -> str:
    parts = [p.strip() for p in str(categories).split() if p.strip()]
    for p in parts:
        if p.lower() != "vertica":
            return p.lower()
    return "vertica"


def slug_from_path(path: Path) -> str:
    name = path.stem
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", name)


def kicker_for(fm: dict, pill: str, default: str = "VERTICA 25.4 · MEASURED") -> str:
    """卡片左上角的版本標籤：優先取 tags 裡的 "26.3" 這種版本，其次標題裡的版本號。"""
    ver = None
    for t in fm.get("tags") or []:
        m = re.fullmatch(r"v?(\d+\.\d+)", str(t).strip())
        if m:
            ver = m.group(1)
            break
    if not ver:
        m = re.search(r"Vertica\s+(\d+\.\d+)", str(fm.get("title", "")))
        if m:
            ver = m.group(1)
    if not ver:
        return default
    label = "RELEASE NOTES" if pill.upper() == "RELEASE" else "MEASURED"
    return f"VERTICA {ver} · {label}"


def process_pair(zh: Path, en: Path, dry_run: bool = False) -> dict:
    z = parse_front_matter(zh)
    e = parse_front_matter(en)
    slug = slug_from_path(zh)
    token = category_token(z.get("categories", ""))
    pill, visual = CATEGORY_META.get(token, (token.upper(), "VERTICA 25.4 · MEASURED"))

    zh_out = OUT_DIR / f"{slug}-zh.png"
    en_out = OUT_DIR / f"{slug}-en.png"

    if not dry_run:
        build_card(
            title=str(z.get("title", slug)),
            subtitle=str(e.get("title", "")),
            category=pill,
            kicker=kicker_for(z, pill),
            visual_label=visual,
            out=zh_out,
        )
        build_card(
            title=str(e.get("title", slug)),
            subtitle=str(z.get("title", "")),
            category=pill,
            kicker=kicker_for(e, pill),
            visual_label=visual,
            out=en_out,
        )
        set_image_field(zh, f"/assets/images/og/{slug}-zh.png")
        set_image_field(en, f"/assets/images/og/{slug}-en.png")

    return {
        "slug": slug,
        "category": pill,
        "zh": str(zh_out.relative_to(ROOT)),
        "en": str(en_out.relative_to(ROOT)),
        "title_zh": z.get("title"),
        "title_en": e.get("title"),
    }


def all_pairs() -> list[tuple[Path, Path]]:
    zh = {p.stem: p for p in POSTS.glob("*.md")}
    en = {p.stem: p for p in EN_POSTS.glob("*.md")}
    pairs = []
    for name in sorted(set(zh) & set(en)):
        pairs.append((zh[name], en[name]))
    missing_zh = sorted(set(en) - set(zh))
    missing_en = sorted(set(zh) - set(en))
    if missing_zh:
        print(f"WARNING: English posts without Chinese pair: {missing_zh}", file=sys.stderr)
    if missing_en:
        print(f"WARNING: Chinese posts without English pair: {missing_en}", file=sys.stderr)
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="process every zh/en pair")
    ap.add_argument("--slug", help="process one slug")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lang", choices=["zh", "en"], help="single-card mode")
    ap.add_argument("--title")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--category", default="VERTICA")
    ap.add_argument("--kicker", default="VERTICA 25.4 · MEASURED")
    ap.add_argument("--visual-label", default="VERTICA 25.4 · MEASURED")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.lang:
        if not args.title or not args.out:
            ap.error("--lang requires --title and --out")
        out = Path(args.out)
        if not out.is_absolute():
            out = ROOT / out
        build_card(args.title, args.subtitle, args.category, args.kicker, args.visual_label, out)
        print(json.dumps({"out": str(out), "width": W, "height": H}, ensure_ascii=False))
        return

    if args.slug:
        zh = next(POSTS.glob(f"*-{args.slug}.md"), None)
        en = next(EN_POSTS.glob(f"*-{args.slug}.md"), None)
        if not zh or not en:
            ap.error(f"missing pair for slug {args.slug}: zh={zh} en={en}")
        result = process_pair(zh, en, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.all:
        results = [process_pair(zh, en, args.dry_run) for zh, en in all_pairs()]
        print(json.dumps({"count": len(results), "results": results}, ensure_ascii=False, indent=2))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
