# SEO Audit

A standalone SEO audit tool. Give it a domain (and optionally a sitemap URL);
it crawls the site, extracts and validates SEO signals, scores every page and
produces a Word report plus data files. Search performance comes from Google
Search Console only when a client provides it; third-party ranking or backlink
data is never used.

How the full pipeline works, stage by stage, is in `SEO_AUDIT_AGENT.md`.
Read that first.

## What is built so far

The crawl layer: URL discovery and fetch. Page-level parsing, validation,
links, scoring, the report and the Streamlit console are not built yet. The
sections below describe only what runs today.

## Install

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` when the report stage arrives; no key is needed
for the crawl.

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

Written to `output/<host>/<timestamp>/` unless `--out` says otherwise. Every
run gets its own dated folder so runs of the same domain can be compared later.

- **`raw_crawl.csv`**: one row per URL: `url`, `final_url`, `status_code`,
  `redirect_hops`, `response_time_ms`, `content_type`, `in_sitemap`,
  `in_crawl`, `depth`, `discovered_from`, `text_chars`, `error`.
  UTF-8 with a BOM, so Excel opens it without mangling accents.
- **`crawl_summary.json`**: domain, sitemap used, pages found, counts by
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

All tests are offline: no network, no API keys.

## Layout

- `seo_audit/`: the package. All audit logic lives here.
- `app/streamlit_app.py`: placeholder for the console; a thin layer over the
  package, never audit logic.
- `tests/`: offline tests on fixture HTML and XML.
- `archive/v0/`: the original prototype, kept for reference, not edited.

## Build order

1. Crawl layer (done)
2. Parse, technical validation, schema validation, sitemap hygiene
3. Links, content similarity, PageSpeed sample, scoring
4. Findings JSON, report narrative, docx report, baseline vs full mode
5. Streamlit console with optional Search Console upload

Rendering with Playwright is added when the `render_suspects` count on a real
site says it is needed.
