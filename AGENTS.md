# AGENTS.md

## What this repo is

Jekyll static site (GitHub Pages) for Vertica database technical articles. Content is in **Traditional Chinese (zh-TW)**. Repository: `achi0012/vertica-tips`.

## Local development

```bash
gem install bundler jekyll
bundle install
bundle exec jekyll serve --livereload
```

Site serves at `http://localhost:4000/vertica-tips/` (note `baseurl` in `_config.yml`).

## Post conventions

Posts live in `_posts/` with filename pattern `YYYY-MM-DD-slug.md`.

Required front matter fields:

```yaml
---
layout: post
title: "Title here"
date: 2026-06-20 10:00:00 +0800
categories: vertica category-name        # space-separated, NOT a YAML array
tags: [vertica, tag1, tag2]              # YAML array syntax
description: "One-line description for SEO."
---
```

When you materially update an existing post, add `last_modified_at` so search engines and AI answer engines see a fresh `dateModified`:

```yaml
last_modified_at: 2026-06-21 10:00:00 +0800
```

After front matter, include an AI metadata block before `<!--more-->`:

```
<!--
AI Summary: Brief summary of the article content.
AI Keywords: keyword1, keyword2, keyword3
-->
<!--more-->
```

When adding a post to `README.md`, link to it as `/_posts/filename.md`.

Every post needs an `image:` field pointing to its 1200×630 OG card, for example `image: /assets/images/og/<slug>-zh.png`. The scheduled article job generates the Chinese and English cards with `tools/og_image.py`.

## Images

Optional, 1–2 per post, only when a diagram carries a relationship better than prose (lifecycle, architecture, plan shape before/after). Never screenshot SQL, `EXPLAIN` output, or error messages — use code blocks.

- Hand-written SVG in `assets/images/`, kebab-case with a topic prefix (`stats-lifecycle.svg`).
- Canvas width ≤ 820px (post column is 720px); body text ≥ 12px.
- Reference with `relative_url` because `baseurl` is `/vertica-tips`:
  ```markdown
  ![alt text]({{ '/assets/images/name.svg' | relative_url }}){: loading="lazy" }
  ```
- Chinese and English posts share the same file; localize the `alt` text.
- Every number in a diagram must come from a real measurement.
- Verify before committing: SVG parses as XML, no text overflows the canvas or overlaps another label (check `getBBox()` in a browser), and the rendered `<img>` resolves to `/vertica-tips/assets/images/...`.

## Site structure

- `_config.yml` — site config, plugins (jekyll-feed, jekyll-seo-tag, jekyll-sitemap)
- `_layouts/` — custom layouts (default, home, page, post)
- `_includes/` — partials (header, footer, head)
- `assets/images/` — post images
- `about.md` — about page (nav-linked via `header_pages`)
- `index.md` — homepage (uses `layout: home`)

## Gotchas

- No `Gemfile.lock` committed; run `bundle install` first.
- `exclude` list in `_config.yml` omits `Gemfile`, `node_modules`, `vendor` from build.
- Permalink pattern: `/:year/:month/:title/` — not date-based.
- All posts target **Vertica 25.x** specifically; older version references should be noted as historical context.
