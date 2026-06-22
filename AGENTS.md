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

After front matter, include an AI metadata block before `<!--more-->`:

```
<!--
AI Summary: Brief summary of the article content.
AI Keywords: keyword1, keyword2, keyword3
-->
<!--more-->
```

When adding a post to `README.md`, link to it as `/_posts/filename.md`.

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
