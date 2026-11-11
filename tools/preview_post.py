#!/usr/bin/env python3
"""把 _posts 的 markdown 文章轉成單檔 HTML 預覽（不依賴 Jekyll／ruby）。

用途：本機沒有 ruby／bundler，無法 `jekyll build`，但發文前總要看到實際排版。

用法：
    python3 tools/preview_post.py _posts/2026-11-13-vertica-26-3-0-2-hotfix.md
    python3 tools/preview_post.py <post.md> --out /tmp/preview.html

支援的 markdown 子集（本專案文章實際用到的）：
    標題 ## / ###、段落、表格、**粗體**、`行內程式碼`、[連結](url)、> 引用、--- 分隔線
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

import yaml


def parse(path: Path) -> tuple[dict, str]:
    t = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", t, re.S)
    if not m:
        sys.exit(f"沒有 front-matter：{path}")
    return yaml.safe_load(m.group(1)) or {}, t[m.end():]


def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    return s


def md_to_html(body: str) -> str:
    # 去掉 HTML 註解（AI Summary）與 more 標記
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)

    lines = body.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()

        if not s:
            i += 1
            continue

        # 表格
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            header = [c.strip() for c in s.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = ["<table><thead><tr>"]
            t += [f"<th>{inline(c)}</th>" for c in header]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            lvl = len(m.group(1)) or 2
            lvl = min(max(lvl, 2), 4)
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue

        if re.match(r"^---+$", s):
            out.append("<hr>")
            i += 1
            continue

        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(inline(b) for b in buf if b) + "</blockquote>")
            continue

        if re.match(r"^[-*]\s+", s):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(inline(re.sub(r"^[-*]\s+", "", lines[i].strip())))
                i += 1
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            continue

        # 段落：吃到空行
        buf = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^(#{1,4}\s|\||>|---+$|[-*]\s)", lines[i].strip()
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(buf)) + "</p>")

    return "\n".join(out)


TEMPLATE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:0; color:var(--foreground,#1a1a1a);
         font-family:var(--font, -apple-system, "Noto Sans TC", system-ui, sans-serif);
         font-size:16px; line-height:1.75; }}
  .wrap {{ max-width:820px; padding:4px 2px 24px; }}
  .kicker {{ font-size:12px; letter-spacing:.12em; text-transform:uppercase;
             color:var(--accent,#0070f3); font-weight:700; margin-bottom:10px; }}
  h1 {{ font-size:30px; line-height:1.3; margin:0 0 12px; letter-spacing:-.02em; }}
  .meta {{ font-size:13px; color:var(--muted-foreground,#5f6368); margin-bottom:16px; }}
  .tags {{ display:flex; flex-wrap:wrap; gap:6px; margin:14px 0 20px; }}
  .tag {{ font-size:12px; padding:3px 9px; border-radius:999px;
          border:1px solid var(--border,#d8dade); color:var(--muted-foreground,#5f6368); }}
  .desc {{ border-left:3px solid var(--accent,#0070f3); padding:10px 14px; margin:0 0 26px;
           background:var(--card,#f6f7f9); border-radius:0 6px 6px 0;
           color:var(--muted-foreground,#44474d); font-size:14.5px; }}
  h2 {{ font-size:21px; margin:34px 0 14px; padding-bottom:8px;
        border-bottom:1px solid var(--border,#e4e6ea); }}
  h3 {{ font-size:17px; margin:24px 0 10px; }}
  p {{ margin:0 0 14px; }}
  code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.88em;
          background:var(--card,#f1f2f4); border:1px solid var(--border,#e4e6ea);
          padding:1px 5px; border-radius:4px; }}
  pre {{ background:var(--card,#f6f7f9); border:1px solid var(--border,#e4e6ea);
         border-radius:8px; padding:12px 14px; overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; margin:16px 0 22px; font-size:13.5px; }}
  th, td {{ border:1px solid var(--border,#e4e6ea); padding:8px 10px; text-align:left;
            vertical-align:top; }}
  th {{ background:var(--card,#f1f2f4); color:var(--foreground,#1a1a1a);
        font-weight:600; white-space:nowrap; }}
  tbody tr:nth-child(even) {{ background:color-mix(in srgb, var(--card,#f6f7f9) 55%, transparent); }}
  td:first-child {{ white-space:nowrap; font-family:ui-monospace,Menlo,monospace; font-size:12.5px; }}
  blockquote {{ margin:16px 0; padding:10px 14px; border-left:3px solid var(--border,#d0d3d8);
                color:var(--muted-foreground,#5f6368); font-size:14px; }}
  ul {{ margin:0 0 14px; padding-left:22px; }}
  li {{ margin:5px 0; }}
  hr {{ border:0; border-top:1px solid var(--border,#e4e6ea); margin:28px 0; }}
  a {{ color:var(--accent,#0070f3); }}
  details {{ border:1px solid var(--border,#e4e6ea); border-radius:8px; padding:10px 14px;
             margin:8px 0; background:var(--card,#f6f7f9); }}
  summary {{ cursor:pointer; font-weight:600; font-size:14.5px; }}
  details p {{ margin:10px 0 0; color:var(--muted-foreground,#44474d); font-size:14px; }}
  .foot {{ margin-top:34px; padding-top:16px; border-top:1px solid var(--border,#e4e6ea);
           font-size:12.5px; color:var(--muted-foreground,#6b7076); }}
  img.og {{ width:100%; border-radius:10px; border:1px solid var(--border,#e4e6ea); margin:0 0 24px; }}
</style></head><body><div class="wrap">
<div class="kicker">{kicker}</div>
<h1>{title}</h1>
<div class="meta">{meta}</div>
<div class="tags">{tags}</div>
<div class="desc">{desc}</div>
{og}
{faq}
{body}
<div class="foot">預覽模式：單檔 HTML，由 tools/preview_post.py 產生，非 Jekyll 建置結果。</div>
</div></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("post")
    ap.add_argument("--out", help="輸出檔案（預設 docs 旁的 <slug>-preview.html）")
    ap.add_argument("--no-image", action="store_true", help="不顯示 OG 圖")
    args = ap.parse_args()

    p = Path(args.post).resolve()
    fm, body = parse(p)
    root = p.parents[1]
    out = Path(args.out) if args.out else p.with_name(p.stem + "-preview.html")

    tags = "".join(f'<span class="tag">{html.escape(str(t))}</span>' for t in (fm.get("tags") or []))
    faq = ""
    if fm.get("faq"):
        items = "".join(
            f"<details><summary>{inline(str(q.get('q', '')))}</summary>"
            f"<p>{inline(str(q.get('a', '')))}</p></details>"
            for q in fm["faq"]
        )
        faq = f"<h2>常見問題</h2>{items}"

    og = ""
    if fm.get("image") and not args.no_image:
        img = root / str(fm["image"]).lstrip("/")
        if img.exists():
            import base64
            b64 = base64.b64encode(img.read_bytes()).decode()
            og = f'<img class="og" src="data:image/png;base64,{b64}" alt="OG">'

    doc = TEMPLATE.format(
        title=html.escape(str(fm.get("title", ""))),
        kicker=html.escape(" ".join(fm.get("categories", "").split()) if isinstance(fm.get("categories"), str)
                           else " ".join(fm.get("categories") or [])),
        meta=html.escape(str(fm.get("date", ""))),
        tags=tags,
        desc=inline(str(fm.get("description", ""))),
        faq=faq,
        og=og,
        body=md_to_html(body),
    )
    out.write_text(doc, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
