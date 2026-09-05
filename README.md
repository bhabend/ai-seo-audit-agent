# SEO Audit

A standalone SEO audit tool. Give it a domain (and optionally a sitemap URL);
it crawls the site, extracts and validates SEO signals, scores every page and
produces a Word report plus data files. Search performance comes from Google
Search Console only when a client provides it; third-party ranking or backlink
data is never used.

How the full pipeline works, stage by stage, is in `SEO_AUDIT_AGENT.md`.
Read that first.

## What is built so far

The crawl layer (URL discovery and fetch), the page layer (field extraction,
JSON-LD and validation), and the link and content layer (internal link graph,
duplicate detection). Scoring, the report and the Streamlit console are not
built yet. The sections below describe only what runs today.

## Install

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` when the report stage arrives; no key is needed
for the crawl.

## Run

```bash
python -m seo_audit.cli --domain example.com
```

| Flag | Default | What it does |
| --- | --- | --- |
| `--sitemap URL` | discovered | Skip discovery and use this sitemap. |
| `--max-pages N` | 2000 | Pages fetched **and parsed**. Sitemap URLs beyond this are still status-checked by the sweep. |
| `--max-depth N` | 5 | How many clicks from the homepage to follow. |
| `--workers N` | 4 | Parallel fetchers. Each waits `--delay` between its own requests. |
| `--delay SECONDS` | 1.0 | Politeness delay per worker. A larger robots.txt `Crawl-delay` wins. |
| `--sitemap-reserve F` | 0.25 | Share of the page budget held back for sitemap URLs no link reached (0 to 0.5). |
| `--sweep-limit N` | 10000 | Most sitemap URLs the sweep will status-check. Past this it stops and the summary says it was capped. |
| `--sweep-delay SECONDS` | 0.25 | Per-worker delay for sweep requests only. HEAD is cheap; page fetches are not. |
| `--canonical-check-limit N` | 200 | Most canonical targets outside the crawl to check with HEAD. The rest are reported as unchecked, never guessed. |
| `--include-subdomains` | off | Treat other subdomains as part of the site. |
| `--no-robots` | off | Do not fetch or obey robots.txt. |
| `--keep-html` | off | Also store each page's HTML, gzipped. |
| `--out DIR` | `output/<host>/<timestamp>/` | Where the run folder goes. |

With no `--sitemap`, the tool looks for `Sitemap:` lines in `robots.txt`, then
`/sitemap.xml`, then `/sitemap_index.xml`. Sitemap index files are followed
and gzipped sitemaps are decompressed.

If the homepage redirects to a different hostname on the same site (`www` to
non-`www` or the reverse), that hostname is adopted for the rest of the run,
including the output folder name, and the swap is recorded as a
`host_redirect` issue.

## Crawled versus swept

Two different jobs, and the difference matters when reading the output:

- **Crawled** pages are fetched in full, up to `--max-pages`. They get a row
  in `raw_crawl.csv` with response time, content type and text length.
- **Swept** URLs are every sitemap URL the crawl did not reach. They get a
  `HEAD` request (falling back to `GET` if the server refuses), so a sitemap
  far larger than the page cap is still checked in full rather than sampled.
  No HTML is fetched or stored for them.

So on a site with 20,000 sitemap URLs and the default cap, 2,000 pages are
parsed and all 20,000 are status-checked.

## Output

Written to `output/<host>/<timestamp>/` unless `--out` says otherwise. Every
run gets its own dated folder so runs of the same domain can be compared
later. All four files are UTF-8 with a BOM, so Excel opens them cleanly.

- **`raw_crawl.csv`**: one row per fetched URL: `url`, `final_url`,
  `status_code`, `redirect_hops`, `response_time_ms`, `content_type`,
  `in_sitemap`, `in_crawl`, `depth`, `discovered_from`, `text_chars`,
  `error`. `final_url` is normalised the same way as `url`, so the two
  columns join.
- **`sitemap_sweep.csv`**: one row per sitemap URL the crawl did not fetch:
  `url`, `status_code`, `final_url`, `redirect_hops`, `blocked_by_robots`,
  `in_crawl`, `error`.
- **`crawl_issues.csv`**: the crawlability log. One row per obstacle, with
  `issue_type`, `url`, `referrer`, `detail` and `crawler_effect` -- one plain
  sentence saying what a search engine does with it. Whenever the crawler
  works around something (adopting a redirected host, folding a
  trailing-slash redirect, skipping a blocked URL) it writes a row here
  instead of quietly moving on.
- **`audit_pages.csv`**: one row per parsed page. Title, meta description,
  robots directives, canonical, hreflang, headings, word count, image and
  link counts, JSON-LD types, and this page's own issue list.
- **`page_issues.csv`**: one row per finding, with `issue_type`, `severity`
  (high, medium, low), `url`, `final_url`, `detail` and `site_level`.
- **`links.csv`** (only with `--write-links`): the raw edge list. Off by
  default because the edge list is an order of magnitude larger than the page
  list and the per-page aggregates answer most questions; turn it on when you
  want to trace individual links.
- **`crawl_summary.json`**: the run in numbers, including per-type issue
  counts, page issue counts by type and severity, schema type counts, link
  and content totals, the sweep totals and the depth distribution.

`text_chars` is the visible text length after script/style/noscript are
stripped. Pages under 500 characters are counted as `render_suspects`: they
are usually JavaScript shells whose real content never arrives over plain
HTTP. A high count means the site needs a rendering pass before page-level
parsing is worth anything.

`depth` is empty for URLs that came from the sitemap and were never reached by
a link. The link crawl is drained first; a slice of the page budget
(`--sitemap-reserve`, 25% by default) is held back for those sitemap-only
URLs, so coverage gaps still show up in a capped run.

## Honest aggregates

Any count that depends on a cap says so, in the finding itself and in the
summary. An orphan page is reported as having no inbound links **"within the
2000 pages this run crawled"**, not as having none on the site, because the
tool cannot know the second thing. The same rule covers the sweep cap, the
canonical check limit and the external check limit: each reports what it
checked and what it did not, and nothing is assumed working because it was
never looked at.

Two counts had to be corrected under this rule after earlier runs:

- **`sitemap_non_200` no longer counts 403 or 503 at all.** Those are how a
  site says "slow down". When the sweep starts seeing them it doubles its
  delay up to 4 seconds, and if 20 arrive in a row (or more than half of the
  last hundred) it stops and writes one `sweep_throttled` row saying how far
  it got. A throttled sweep is an unknown, not a dead sitemap: a previous run
  reported two thirds of a sitemap as broken when the site was simply rate
  limiting.
- **`sitemap_only_page` now means something.** It is raised only for a URL
  that is in the sitemap, **was crawled**, and has no inbound link from any
  other crawled page. URLs the sweep merely status-checked never raise it,
  because nothing was parsed for them and their inbound links are unknown.

## The link graph

Built after the crawl from edges the workers emit, one per `<a>` with its
anchor text and `rel`. Every target is resolved through the crawl's
url to final_url map first: a page linked only as `/x` when the site serves
`/x/` has one inbound link, not zero, and is not an orphan.

From it: `inlinks`, `nofollow_inlinks`, `outlinks_internal`,
`outlinks_external` and up to five distinct inbound `anchor_texts` per page,
plus `orphan_page`, `low_inlink_page`, `broken_internal_link` (one row per
broken target with its referrer count, so fifty links to one dead page are
one finding), `redirected_internal_link`, `nofollow_internal_link`,
`generic_anchor` and `external_link_broken`.

## Duplicate content

Each page gets two fingerprints, both computed in the worker and both 16
bytes: an md5 of its normalised text, and a 64-bit simhash over overlapping
word shingles. Exact duplicates group by md5; near-duplicates group by simhash
Hamming distance of 3 or less. Pages under 200 words are never compared, since
navigation furniture repeats across a site and would group everything. The
page text itself is never stored.

## What gets parsed, and what a check is keyed on

**Only pages the crawl fetched and that answered 200 are parsed.** A 404 body
is real HTML, but it is not a page of the site, so it is recorded in
`raw_crawl.csv` and never parsed. Sitemap URLs that only the sweep reached are
status-checked, not parsed, so a check that needs page content -- including
`sitemap_noindex` -- can only see the sitemap URLs the crawl actually fetched.

**Every page-level field and check belongs to `final_url`**, the URL that
answered, not to the URL that was requested. On a redirected page those
differ, and a canonical that correctly points at where the page ended up would
otherwise be reported as a fault on every redirect. `audit_pages.csv` carries
both columns so the join is always available.

**Structured data is validated as JSON-LD only.** Microdata and RDFa are
*detected* -- their type names appear in `microdata_types` and `has_microdata`
-- but their properties are not checked. A page whose only structured data is
microdata is reported as `schema_microdata_only`, not as `schema_missing`;
`schema_missing` now means no JSON-LD **and** no microdata. Presence is not
treated as validity: for the JSON-LD types the tool knows (Organization,
WebSite, WebPage, BreadcrumbList, FAQPage, Article, BlogPosting, Product,
Service, LocalBusiness, Place) the required properties are checked and
reported as `schema_missing_property`. Types it does not know are listed and
never judged.

**PDFs and Office files are documents, not defects.** A linked `.pdf`, `.doc`,
`.xlsx` or `.pptx` is recorded as `document_linked` because search engines
index those on their own terms; `non_html_linked` is kept for everything else
linked as if it were a page. Documents also get a slower response threshold
(10s) than pages (3s), since a large PDF is not a site fault.

## Keeping HTML

`--keep-html` is **off by default**. A run folder holds CSVs and JSON only, so
runs stay small enough to keep many of them side by side. Turn it on and each
page's HTML is written to `html/<sha1 of the URL>.html.gz`, which lets a later
analyser re-run over a past crawl without re-fetching the site. Expect the
folder to grow by roughly the size of the site.

## Housekeeping

```bash
python -m seo_audit.clean
```

Lists every run folder by host, with size and date. `--delete HOST` removes
all but the newest run for that host, `--delete-all HOST` every run for it,
and `--older-than DAYS` prunes across hosts by age. The newest run for a host
is never deleted, whatever is asked, so there is always a baseline to compare
against. Add `--yes` to skip the confirmation prompt.

## Tests

```bash
pytest
```

All tests are offline: no network, no API keys.

## Layout

- `seo_audit/`: the package. All audit logic lives here.
  - `crawl.py` fetches; `parse.py` reads a page's fields; `schema.py` reads
    its JSON-LD; `links.py` builds the internal link graph; `content.py`
    fingerprints the text; `validate.py` turns those facts into findings.
  - A page gets exactly one BeautifulSoup pass, shared by link extraction,
    field parsing and JSON-LD, and the HTML is released inside the worker
    before the next page starts.
- `app/streamlit_app.py`: placeholder for the console; a thin layer over the
  package, never audit logic.
- `tests/`: offline tests on fixture HTML and XML.
- `archive/v0/`: the original prototype, kept for reference, not edited.

## Build order

1. Crawl layer (done)
2. Parse, technical validation, schema validation, sitemap hygiene (done)
3. Links and content similarity (done); PageSpeed sample and scoring
4. Findings JSON, report narrative, docx report, baseline vs full mode
5. Streamlit console with optional Search Console upload

Rendering with Playwright is added when the `render_suspects` count on a real
site says it is needed.
