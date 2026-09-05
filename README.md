# SEO Audit

URL discovery and fetch layer for a site audit. Give it a domain (and
optionally a sitemap URL); it crawls the site by following links and reading
the sitemap, then writes a raw crawl CSV and a JSON summary.

This is the crawl layer only. Page-level SEO parsing, scoring and the
Streamlit console are not built yet.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python -m seo_audit.cli --domain example.com --max-pages 40
```

Useful flags: `--sitemap URL`, `--max-depth N`, `--delay SECONDS`,
`--include-subdomains`, `--no-robots`, `--out DIR`.

With no `--sitemap`, the tool looks for `Sitemap:` lines in `robots.txt`, then
`/sitemap.xml`, then `/sitemap_index.xml`. Sitemap index files are followed
and gzipped sitemaps are decompressed.

## Output

Written to `output/<host>/<timestamp>/` unless `--out` says otherwise.

- **`raw_crawl.csv`** — one row per URL: `url`, `final_url`, `status_code`,
  `redirect_hops`, `response_time_ms`, `content_type`, `in_sitemap`,
  `in_crawl`, `depth`, `discovered_from`, `text_chars`, `error`.
  UTF-8 with a BOM, so Excel opens it without mangling accents.
- **`crawl_summary.json`** — domain, sitemap used, pages found, counts by
  source and status, robots findings, crawl delay applied, run duration, and
  a `render_suspects` count.

`text_chars` is the visible text length after script/style/noscript are
stripped. Pages under 500 characters are counted as `render_suspects`: they
are usually JavaScript shells whose real content never arrives over plain
HTTP. A high count means the site needs a rendering pass before page-level
parsing is worth anything.

`depth` is empty for URLs that came from the sitemap and were never reached by
a link. The link crawl is drained first; a quarter of the page budget is held
back for those sitemap-only URLs so coverage gaps still show up in a capped
run.

## Tests

```bash
pytest
```

All tests are offline — no network, no API keys.

## Next session

Streamlit console.
