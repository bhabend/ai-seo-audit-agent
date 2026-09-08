# SEO Audit Agent

What the tool is and how a run goes from start to finish.

Last updated: 2026-09-08.

---

## 1. What it is

A standalone SEO audit tool. It takes a domain, crawls the site, validates
what it finds, scores every page, and produces a Word report beside the data
files it was built from.

It runs two ways over the same Python package in `seo_audit/`:

- **Command line**, four commands: `python -m seo_audit.cli --domain ...` to
  audit, `python -m seo_audit.report --run FOLDER` to write the report,
  `python -m seo_audit.gsc --run FOLDER --export PATH` to attach Search
  Console data, and `python -m seo_audit.clean` for housekeeping.
- **Streamlit console**: `streamlit run app/streamlit_app.py`, four pages
  over those same commands.

Two modes, and the mode is a property of a run folder rather than of a
command:

- **Baseline**: everything computed from the crawl and a free PageSpeed
  sample. `meta.mode` is `baseline`.
- **Full**: a client's Search Console export attached to a finished run,
  which adds the search performance section and the findings that need both
  sides. `meta.mode` becomes `full`.

**Deliberately excluded**: third-party rankings, keyword gap and backlink
data. They are estimates sold as measurements, and mixing them with counted
facts teaches a client to trust both equally. Search Console, the client's own
data, is the only source of search performance figures.

**Two report formats.**

- **Short**, the default: score, ten priority fixes each with an action, one
  table per section, two charts. Built entirely from `findings.json` by code,
  so it makes **no model calls and costs nothing**.
- **Long**, `--format long`: the same findings with a model-written "why it
  matters" and "what to do" per section. About ten calls, roughly a cent, and
  it needs `OPENAI_API_KEY`.

**Deliverable rules, enforced on the saved file before either command exits
zero**: the `.docx` is reopened and checked for headings in order, no dash
used as punctuation anywhere including table cells, no placeholder, no cut-off
list item, a whole beside every count, British spelling, and template shapes
such as `/blog/{slug}` only in the appendix column meant for developers.

## 2. Pipeline

```
Intake -> Discovery -> Crawl & fetch -> Parse -> Validate -> Links & content
-> Performance -> Score -> Findings -> [Search Console] -> Report
```

| Module | What it produces |
| --- | --- |
| `config.py` | One `AuditConfig`: domain, caps, delays, limits |
| `urlnorm.py` | The normalised address every layer joins on |
| `discovery.py` | Seeds from robots.txt, sitemaps (indexes and gz), homepage |
| `fetch.py` | The HTTP layer: retries, timeouts, encodings, redirects |
| `crawl.py` | The crawl, the sitemap sweep, and `raw_crawl.csv` |
| `issues.py` | The registry: each issue type's label, finding, unit, action |
| `parse.py` | Title, meta, headings, canonical, hreflang, images, links, JSON-LD |
| `schema.py` | Structured data types and their required properties |
| `validate.py` | Per-page and site-level checks over what was parsed |
| `links.py` | Broken and redirected targets, orphans, depth, anchor quality |
| `content.py` | Duplicate and near-duplicate text, thin pages |
| `pagespeed.py` | Core Web Vitals on a template sample, with its deadline |
| `scoring.py` + `score_weights.json` | Page and site scores; weights are data |
| `output.py` | Streaming CSV writers and the run summary |
| `findings.py` | `findings.json`: shares, groups, priority fixes, comparison |
| `gsc.py` | The Search Console export, read and joined to the crawl |
| `narrative.py` | The long report's model calls, behind their guards |
| `report.py` | Both report formats, and validation of the saved file |
| `clean.py` | Lists and prunes run folders, keeping the newest per host |
| `cli.py` | The audit command |
| `app/runner.py` | Launches the commands and reads the files they write |
| `app/streamlit_app.py` | The four-page console |

## 3. Workflow, start to finish

**1. Intake.** A domain, optionally a sitemap URL, page and depth caps,
workers, delay, sweep and external-check limits, `--no-pagespeed`,
`--no-compare` or `--compare-to`; the console's form carries the same fields.
The run folder is `output/<host>/<timestamp>/`, named once the homepage
answers, because the host a site redirects to is the host the run belongs
to.

**2. Discovery.** robots.txt is recorded, including disallow rules, crawl
delay and any AI-crawler policy. Sitemaps come from robots.txt, `--sitemap` or
the conventional paths; indexes are followed and gz files read. Every address
is normalised, so one page never appears twice.

**3. Crawl and fetch.** Pages are fetched in parallel, each worker waiting its
delay, obeying robots and the caps. A quarter of the page budget is held back
for sitemap URLs no link reached. Sitemap URLs the crawl did not fetch are
status-checked in a sweep that backs off and stops if the site starts
refusing, recording how far it got rather than calling a rate-limited sitemap
broken. **The seen-pages guard**: several addresses can redirect to one page,
so `final_url` is claimed under a lock before parsing. The duplicate keeps its
`raw_crawl.csv` row marked `redirect_duplicate`, so the redirect stays a
finding, but the page behind it is parsed, scored and counted once.

**4. Parse and validate.** Each page is parsed once, then checked: canonical
target, hreflang reciprocity, indexability, mixed content, viewport, language,
headings, titles, descriptions, and structured data against its required
properties. Site-level checks, such as the HTTPS redirect and security
headers, are recorded once for the site.

**5. Links and content.** From the graph: broken and redirected internal
targets grouped by root cause, orphan pages, click depth, vague anchor text,
and external targets checked most-linked first up to the limit. From the text:
duplicate and near-duplicate pages, thin pages, repeated titles and
descriptions. Any count that depends on a cap says so.

**6. Performance.** URL templates are derived, one page of each is measured on
mobile and desktop, and the stage has a **12-minute deadline** and an attempts
ceiling. A template that could not be measured is recorded as `unmeasured`,
never as healthy.

**7. Score.** A page starts at 100 and loses points in six buckets
(indexability, technical, on-page, content, schema, links); each bucket floors
at zero. `site_score` is the mean page score. **Three gates**: the 15%
performance component folds in only when at least half the sampled URLs
returned a mobile score; pages marked noindex are excluded from the mean and
the distribution; site-wide faults deduct from the site score, at most 5
points, not from whichever page happened to carry them.

**8. Findings.** `findings.json` is computed from the run's own files. Every
share states what it is a share of; faults are grouped by root cause, so forty
404s under one URL shape are one finding; up to fifteen priority fixes carry a
title, an action, evidence and a scope of template, config or page by page.
The run compares itself against the newest other complete run for the same
host, recording deltas, what is new, resolved and unchanged, and whether
coverage moved enough to make the counts incomparable.

**9. Search Console, optional.** `python -m seo_audit.gsc --run FOLDER
--export PATH` reads the export (zip, folder or single CSV; tables found by
shape, so other locales and decimal commas read correctly), normalises every
address, including the scheme-less ones a domain property exports, and joins
it to `audit_pages.csv`. It writes `gsc_join.csv`, the `search_performance`
block, and five findings that need both sides: impressions on a page hidden
from search, impressions on a page that names another as preferred, a sitemap
page never shown, a page with no links in that earns clicks, and a search
answered by more than one page. Rates are computed, and search totals are
reported as a floor because Google omits rare searches. A second export
replaces the first rather than doubling it.

**10. Report.** `python -m seo_audit.report --run FOLDER` writes
`SEO_Audit_<host>_<date>.docx` into the run folder and validates it before
exiting. Short by default; `--format long` adds the narrative and writes
`report_usage.json` with the calls and tokens it cost.

**11. The console.** Four pages: **Run audit** (form, then stage, counters and
log tail refreshed every four seconds; one audit at a time, bound to the log
it started); **Results** (score, priority fixes, the same section tables the
report prints, and the export uploader); **Report** (short or long, cost and
mode stated before the click); **Runs** (every folder with its size, deletion
behind a confirmation, and runs that stopped without finishing). Audits are
separate processes, so closing the browser does not stop them.

**12. Housekeeping.** `python -m seo_audit.clean` lists run folders and
deletes them, keeping each host's newest run; `--delete-host` is the one path
that keeps nothing and needs `--yes`.

### What a run folder holds

`raw_crawl.csv`, `sitemap_sweep.csv`, `crawl_issues.csv`, `audit_pages.csv`,
`page_issues.csv`, `pagespeed.csv` (when the sample ran), `crawl_summary.json`
and `findings.json`; then `gsc_join.csv` once an export is attached, the
`.docx` once a report is written, and `report_usage.json` after a long report.
`links.csv` only with `--write-links`.

Console logs live in `output/logs/<timestamp>.log`, carrying the command, the
`RUN_DIR=` line and the `EXIT_CODE=` line saying how the run ended. An uploaded
export is written to a temporary file, used and deleted: **the client's export
is never kept**, only the audit's own join.
