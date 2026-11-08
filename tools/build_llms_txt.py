#!/usr/bin/env python3
"""Generate llms.txt from the Chinese and English post front matter."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / "_config.yml").read_text(encoding="utf-8"))
SITE = CONFIG.get("url", "").rstrip("/")
BASE = CONFIG.get("baseurl", "").rstrip("/")
PREFIX = f"{SITE}{BASE}"


def parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    return yaml.safe_load(m.group(1)) or {}


def slug(path: Path) -> str:
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem)


def url(path: Path, lang: str) -> str:
    fm = parse(path)
    date = str(fm.get("date", ""))[:10]
    y, m = date.split("-")[:2] if len(date) >= 7 else ("1970", "01")
    prefix = "/en" if lang == "en" else ""
    return f"{PREFIX}{prefix}/{y}/{m}/{slug(path)}/"


def line(path: Path, lang: str) -> str:
    fm = parse(path)
    title = str(fm.get("title", slug(path))).strip()
    desc = str(fm.get("description", "")).strip()
    suffix = f": {desc}" if desc else ""
    return f"- [{title}]({url(path, lang)}){suffix}"


def main():
    zh = sorted((ROOT / "_posts").glob("*.md"))
    en = sorted((ROOT / "_en").glob("*.md"))
    zh.sort(key=lambda p: str(parse(p).get("date", "")), reverse=True)
    en.sort(key=lambda p: str(parse(p).get("date", "")), reverse=True)

    out = [
        "# Vertica Tips",
        "",
        "> In-depth Vertica 25.x technical articles, verified on Vertica v25.4.0-0. "
        "Bilingual: Traditional Chinese (zh-TW) and English (en).",
        "",
        f"Canonical: {PREFIX}/",
        f"Sitemap: {PREFIX}/sitemap.xml",
        "Author: Achi (https://github.com/achi0012)",
        "Languages: zh-TW, en",
        "License: content is copyright Achi; code samples may be reused with attribution.",
        "",
        "## Chinese (zh-TW)",
        "",
    ]
    out.extend(line(p, "zh") for p in zh)
    out.extend(["", "## English (en)", ""])
    out.extend(line(p, "en") for p in en)
    out.append("")

    (ROOT / "llms.txt").write_text("\n".join(out), encoding="utf-8")
    print(f"llms.txt: {len(zh)} zh + {len(en)} en articles")


if __name__ == "__main__":
    main()
