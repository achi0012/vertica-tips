#!/usr/bin/env python3
"""從 _scheduler/*.json 與已發布文章產生 _scheduler/progress.md 進度看板。

用法：
    /usr/bin/python3 tools/build_progress.py            # 產生／更新
    /usr/bin/python3 tools/build_progress.py --check    # 只印摘要，不寫檔

判斷規則與每日排程一致：_posts 與 _en 兩邊都有同 slug 的檔案，才算完成。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHED = ROOT / "_scheduler"
SERIES_FILES = ["vertica-series.json", "vertica-eon-series.json"]
BASE_URL = "https://achi0012.github.io/vertica-tips"


def read_fm_date(p: Path) -> str | None:
    m = re.match(r"(\d{4}-\d{2}-\d{2})-", p.name)
    return m.group(1) if m else None


def site_date(p: Path) -> tuple[str, str]:
    """回傳 (yyyy/mm, 年份) 給網址用。"""
    d = read_fm_date(p)
    if not d:
        return "", ""
    y, m, _ = d.split("-")
    return f"{y}/{m}", y


def find(slug: str, sub: str) -> Path | None:
    hits = sorted((ROOT / sub).glob(f"*-{slug}.md"))
    return hits[0] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now()
    base = now.date() + dt.timedelta(days=1) if now.hour >= 5 else now.date()

    blocks: list[str] = []
    total = done = 0
    pending_slugs: list[tuple[str, int, str, str]] = []   # (series, n, slug, zh_title)
    next_assign: dict[str, str] = {}

    out = [
        "# 文章產線進度",
        "",
        f"> 自動產生，請勿手改。來源：`_scheduler/vertica-series.json`、`_scheduler/vertica-eon-series.json`。",
        f"> 更新時間：{now:%Y-%m-%d %H:%M}（Asia/Taipei）· 產生指令：`/usr/bin/python3 tools/build_progress.py`",
        "",
        "排程：每天 05:00 產下一篇（中英文各一），完成後 commit + push；週日 20:00 另檢查官方公告。",
        "",
    ]

    for fname in SERIES_FILES:
        f = SCHED / fname
        if not f.exists():
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        topics = data.get("topics", [])
        rows = []
        series_done = 0
        for t in topics:
            total += 1
            zh = find(t["slug"], "_posts")
            en = find(t["slug"], "_en")
            if zh and en:
                done += 1
                series_done += 1
                sub, y = site_date(zh)
                url_zh = f"{BASE_URL}/{sub}/{t['slug']}/"
                url_en = f"{BASE_URL}/en/{sub}/{t['slug']}/"
                date = read_fm_date(zh) or ""
                rows.append(
                    f"| {t['n']} | [{t['zh_title']}]({url_zh}) | [EN]({url_en}) | {date} | ✅ |"
                )
            elif zh or en:
                have = "中文" if zh else "英文"
                miss = "英文" if zh else "中文"
                rows.append(f"| {t['n']} | {t['zh_title']} | — | — | 🟨 缺{miss}（有{have}） |")
            else:
                slot = base + dt.timedelta(days=len(pending_slugs))
                next_assign.setdefault(t["slug"], slot.isoformat())
                pending_slugs.append((data["series"], t["n"], t["slug"], t["zh_title"]))
                rows.append(f"| {t['n']} | {t['zh_title']} | — | 預計 {slot:%m-%d} | ⬜ |")

        label = data.get("series", fname)
        extra = ""
        if "eon" in label:
            c = data.get("cluster", {})
            extra = (
                f"\n環境：`{c.get('database','eondb')}` · {c.get('nodes','?')} nodes · "
                f"K-Safe={c.get('ksafe','?')} · v{c.get('version','?').lstrip('v')} · "
                f"**唯讀，不動叢集**\n"
            )
        blocks.append(
            f"## {label}（{series_done}/{len(topics)}）\n{extra}\n"
            "| # | 中文標題 | English | 日期 | 狀態 |\n|---|---|---|---|---|\n"
            + "\n".join(rows) + "\n"
        )

    out.append(f"**總進度：{done}/{total}**，剩 {total-done} 篇。\n")
    if pending_slugs:
        s, n, slug, title = pending_slugs[0]
        out.append(f"下一篇：**{title}**（{slug}，預計 {next_assign[slug]}）\n")
    else:
        out.append("全部完成。\n")
    out.append("")
    out.extend(blocks)

    text = "\n".join(out)

    if args.check:
        print(f"done={done} total={total} next={pending_slugs[0][2] if pending_slugs else '-'}")
        return 0

    dest = SCHED / "progress.md"
    dest.write_text(text, encoding="utf-8")
    print(f"已寫入 {dest} （{done}/{total}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
