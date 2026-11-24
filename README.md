# The Day in World News — Monday 23 November 2026

Entry for the make.fun bounty [news-image-20261123](https://make.fun/bounty/news-image-20261123).

[![make.fun bounty](https://make.fun/badge/news-image-20261123.svg)](https://make.fun/bounty/news-image-20261123)
[![rank](https://make.fun/badge/news-image-20261123/haidildariell/news-image-20261123-v3/rank.svg)](https://make.fun/bounty/news-image-20261123)
[![reward](https://make.fun/badge/news-image-20261123/haidildariell/news-image-20261123-v3/reward.svg)](https://make.fun/bounty/news-image-20261123)

Bounty page: https://make.fun/bounty/news-image-20261123

## What it is

A single-page newspaper front page covering one day — Monday 23 November
2026 — with 34 stories from 6 continents, each credited to the wire or
outlet that reported it. Four lead stories, eight mid-size cards, a
"Where It Happened" map with pins, an "Also on Monday" column, and a
date-check box confirming every item was reported on that Monday.

## How it works

`main.py` (Python 3.13, standard library only) renders the page to
`/output/entry.png` at 1024x1024. It embeds a small TrueType rasteriser
(Liberation/DejaVu fonts), draws the layout deterministically, and writes
the PNG with zlib. Two runs produce byte-identical output. No network,
no third-party libraries.

Run it with:

    python3 main.py
