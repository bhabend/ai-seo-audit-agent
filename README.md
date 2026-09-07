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
JSON-LD and validation), the link and content layer (internal link graph,
duplicate detection), and performance and scoring (PageSpeed on a template
sample, a weighted page and site score). The report and the Streamlit console
are not built yet. The sections below describe only what runs today.

## Install

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in what you have:

```
OPENAI_API_KEY=
PAGESPEED_API_KEY=
```

No key is needed for the crawl, the page checks, the link graph or scoring.
`PAGESPEED_API_KEY` (a free Google API key) enables the performance sample;
without it that one stage is skipped and the summary says so. Keys are read
from the environment only -- never passed on the command line, never written
to any output file. `.env` is gitignored.

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
  `error`, `redirect_duplicate`. `final_url` is normalised the same way as
  `url`, so the two columns join. `redirect_duplicate` marks a URL that
  redirected to a page another URL had already reached: the redirect is
  still recorded as a finding, but the page behind it is parsed and scored
  exactly once. `audit_pages.csv` therefore has one row per distinct
  `final_url`, and `pages_parsed` counts distinct pages.
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
- **`pagespeed.csv`** (only when the performance sample runs): one row per
  sampled URL per strategy, with the template it stands for, the group size,
  the performance score, the lab metrics and the CrUX field metrics.
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

External targets are checked **8 at a time**: they are other people's servers,
independent of each other and of the audited site, and checking 1,000 of them
one after another cost 19 minutes. Results are collected in submission order,
so the most-linked target is still reported first.

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

## Performance

PageSpeed Insights is run on a **sample of templates, not of pages**. Pages
are grouped by path shape -- `/blog/post-one` and `/blog/post-two` are one
template, `/blog/x` and `/pricing/x` are two -- and the best-linked page in
each group is measured as its representative. That is what lets the audit say
"the product template, 212 pages, scores 34 on mobile" instead of "one page
scored 34".

The cost ceiling is structural and **enforced in code**, not merely intended:
each measurement gets at most 2 attempts, so the ceiling is
`2 x (templates + 1) x 2 strategies` — **64 attempts at the default of 15
templates**, for 32 measurements. The summary reports `pagespeed_attempts`,
`pagespeed_attempts_ceiling` and `pagespeed_calls` (attempts that returned a
usable result) separately, because an attempt that timed out is not a
measurement.

Calls run **4 in flight** with a **150 second** timeout. Both numbers are
answers to a real failure: at 60 seconds and one at a time, 27 of 32 calls
timed out and the stage took most of an hour to return one number. A 429, a
5xx **or a read timeout** is retried once after 10 seconds; a second failure
becomes a `pagespeed_error` row rather than a crash.

The attempt ceiling bounds cost; a **12 minute stage deadline** bounds time.
Once it passes no new call starts, calls already in flight finish normally,
and every URL that was never attempted gets a `pagespeed_error` row reading
`stage deadline` at severity `unmeasured` — never silence, and never a row
that could be mistaken for a healthy page. The summary reports
`pagespeed_deadline_hit` and `pagespeed_stage_seconds`.

`pagespeed.csv` is written **one row per call as it completes**, so a stage
that runs out of time still leaves everything it measured.

Both lab and field data are recorded. Field (CrUX) data is real users and is
preferred; when there is none at URL level the origin is used, and
`field_data_level` says which. Pages with no field data at all raise
`performance_no_field_data`, because a lab number alone is a weaker claim.
The raw API response is discarded; only the extracted metrics are stored.

## Scoring

A page starts at 100 and loses points from six buckets:

| Bucket | Weight |
| --- | --- |
| indexability | 25 |
| technical | 20 |
| on_page | 20 |
| content | 15 |
| schema | 10 |
| links | 10 |

Each issue subtracts its points from its bucket, **each bucket floors at
zero**, and the page score is the sum of the buckets. The floor matters: a
page with a dozen on-page faults should not rank below a page that cannot be
indexed at all.

`site_score` is the mean page score, plus performance when there is enough of
it to mean anything. The 15% performance component applies **only when at
least half the sampled URLs returned a mobile score**; below that the
component is reported but not folded in, and the summary says so with the
ratio. One successful measurement out of sixteen once moved a site score by
eight points, which is noise wearing a number's clothes.

**Pages marked noindex are excluded from the site mean and the distribution.**
They still get their own score, and the summary lists how many there are and
up to twenty of their URLs — but a page the site deliberately kept out of the
index should not drag down the average of the pages it wants indexed.

**Site-wide faults deduct from the site score, not from a page.** Missing
HSTS, CSP, `X-Content-Type-Options`, `X-Frame-Options` and a missing
http-to-https redirect are `scope: "site"` in the weight table: together they
cost at most 5 points off `site_score`, recorded as `site_level_deduction`
with the types that fired. Previously they landed on whichever row carried
them, which was the homepage, so a site-wide gap cost one page's score.
`scope: "once"` still means a group finding (duplicate titles, a broken
internal target) that deducts from its representative page as before.

The weights live in **`seo_audit/score_weights.json`**, which is data, not
code: edit the numbers there to retune the audit without touching Python.
Every registered issue type must appear in that file -- a test fails the
build if one is missing or if a weight names a type nobody registered, so an
unscorable issue can never become a silent zero. Crawl-layer types are listed
with `bucket: null`: they describe the crawl, not a page, and never affect a
score.

Performance issue types carry **zero points** in the page score by design.
Only the sampled page of each template gets those rows, so deducting would
punish a page for being the one that was measured while its identical
siblings scored full marks. Performance is scored once instead, as the site
score's 15% component.

## findings.json

The eighth file in a run folder, computed by code from the run's own seven
files after everything else has finished. It is what a report is written
from: sections for `meta`, `headline`, `crawlability`,
`indexability_technical`, `on_page`, `content`, `schema`, `links`,
`performance`, `prioritised_fixes`, `search_performance` and `comparison`.

Three conventions make it safe to draw from:

- **Every share is an object**: `{"count": 167, "whole": 604, "whole_is":
  "pages parsed"}`. Nothing can be turned into a percentage or a pie without
  stating what it is a share of, and no capped number can pass as a total.
- **Faults are grouped by root cause.** Forty 404s under
  `/on-demand/<city>/?area=` are one finding that names how many link
  instances it accounts for, not forty rows. The pattern is derived from each
  URL -- its first path segment, its depth, and the *names* of its query
  parameters -- so it carries no knowledge of any particular site. Faults
  that travel with a parameter rather than a section, such as non-reciprocal
  hreflang on `?mlg=0` variants, are grouped by parameter instead.
- **A fact is stated once.** Orphan pages appear in `links.orphan_pages` with
  their sitemap split, and nowhere else.

`prioritised_fixes` holds up to 15 entries ordered by reach times severity
(high 3, medium 2, low 1; `unmeasured` never becomes a fix). A root-cause
group counts as one fix. Each carries a plain-language title, the pages
affected with their whole, five evidence rows, and a `fix_scope` of
`template`, `config` or `page`.

The file is bounded: five evidence rows per finding, fifteen fixes, no page
text.

### Comparing runs

Every run compares itself against **the newest other complete run for the
same host** and records the result in `comparison`: deltas for pages found
and parsed, sitemap size, site score, mobile performance, orphans, broken
internal targets and broken external links, plus per-issue-type counts of
what is new, resolved and unchanged. Anything that moved by more than 20% or
50 units lands in `notable` — a sitemap that quietly fell from 709 URLs to
106 between two runs is exactly what this is for.

Older run folders were written by earlier versions and are missing keys. A
metric the previous run never recorded is reported as `"not in previous
run"`, never as a change: two versions of the tool are not two states of a
site.

`--compare-to PATH` picks a specific folder; `--no-compare` switches it off.

## The report

```bash
python -m seo_audit.report --run output/example.com/20260101-120000
```

Builds a Word document from a finished run folder and validates it before
exiting. The audit itself is never re run: the report reads `findings.json`
and the other files that are already there.

| Flag | Default | What it does |
| --- | --- | --- |
| `--run PATH` | required | The run folder to report on. |
| `--model NAME` | `gpt-5-mini` | Model for the narrative. |
| `--no-ai` | off | Write the narrative from templates. No API calls, no cost. |

**Cost.** At most **16 calls per report**: one per section, one for the
executive summary, and up to five regenerations of sections the model cut
off. Each call is capped at 2500 completion tokens and asks for low reasoning
effort. About **three to five cents** on gpt-5-mini. Running out of budget
never loses the report: the remaining sections are written from the data. A 429 or a server error is retried once. A 401, 402, 403 or an
`insufficient_quota` body stops the run immediately: it is neither retried
nor quietly downgraded to another model. Every call's tokens are summed into
`report_usage.json` alongside the model name.

**The findings are written by code. The model only explains them.** Every
"What we found" paragraph, the executive summary's included, is built here
from `findings.json`: each issue type with a count above zero, said through
its own plain sentence with its own count and its own whole, worst severity
first, anything the site already does right in one sentence at the end, and
**zero counts left out entirely**. The model is never asked what was found,
so a right number can never end up attached to the wrong noun and no section
can open on something that was not found. The summary's facts are the score,
the pages scored, any site wide deduction, the spread of page scores and the
five biggest fixes by name.

The model is handed that paragraph plus the section's curated data and asked
for two things only: `why` and `todo`. A `found` key in its answer is ignored.
Five guards run on what it does return:

1. **Dashes are removed.** Em dashes, en dashes, hyphens used with spaces and
   bullet markers at the start of a line become commas or full stops. Hyphens
   inside words and URLs are data and stay.
2. **Jargon is refused.** A banned list (equity, crawl budget, crawl waste,
   SERP, indexation, hreflang, HSTS, noindex, canonical, Lighthouse and the
   rest) is checked on `why` and `todo`. A hit sends the section back once;
   a second hit falls back to the templated wording. The style sheet names
   the banned words and the phrasing to use instead: preferred address,
   hidden from search, secure connection enforced, language version, page
   title, search description.
3. **Advice about the audit is dropped.** An action mentioning measuring,
   sampling, unmeasured templates, re running or crawling further is work for
   us, not for the client. If every action goes, the templated action is used,
   so a section is never left without one.
4. **Every number must already be in that section** or in the facts paragraph
   we wrote. Any sentence carrying a figure that is not is dropped. The
   finding itself is never at risk: it is in the paragraph above, written
   from the data.
5. **A cut off answer is regenerated once**, and **length is capped** per
   section, because a section that runs long buries the finding.

Every drop, regeneration and fallback is recorded in `report_usage.json` under
`guard_events`.

**Sections have a shape.** Each renders as three parts: a "What we found"
paragraph written by code, a "Why it matters" paragraph, and "What to do" as
a numbered list. The model is told never to define a term, because a
**glossary written by code** sits right after the executive summary and does
the defining once.

**Identifiers never reach the page.** Every issue type carries a plain label
("page title missing", not `title_missing`) and a one sentence finding. The
data handed to the model is curated first: internal keys such as fingerprints
and thresholds are stripped, and identifiers are swapped for their labels, so
the model cannot repeat one back even if it wanted to.

**A count says what it counts.** A fix affecting broken links reports the
broken things first and the pages linking to them second: "118 of 1000 links
to other websites checked, linked from 834 of 842 pages parsed". The whole for
a broken internal address is the addresses this crawl reached, never the
number of links between them, because "53 of 106768 internal link targets"
divided one kind of thing by another. Fix scope is `one setting`, `one
template` or `page by page`: site wide settings are one setting, and work is
template work when the three commonest URL shapes cover 70% or more of the
affected pages, because 251 missing search descriptions across a blog, a
location and a workspace template are three template edits and not 251 page
edits.

**A canonical that consolidates is not a fault.** A filtered or tracked
address naming its clean page is the site doing the right thing; it is
reported as such and never offered as a fix. Only a canonical pointing at a
genuinely different page is.

**A sitemap that changes size is the site changing.** When the sitemap or the
page count moves more than 20% between audits, the comparison section is
written entirely by code: it attributes the sitemap change to the site, states
plainly that the counts either side are not comparable, and suggests checking
why the sitemap size varies. The model is not asked to narrate a change it
would turn into a story about decline.

**Charts are drawn by code**, in memory, and no image files are left in the
run folder. Every chart that shows a part of a whole is a pie whose title
states that whole in words, for example "Structured data coverage, of 842
pages parsed". The one exception is a bar chart of performance scores by
template, which is not a share of anything.

**The document is validated before the command exits.** It is reopened and
checked for headings in order, the expected number of images, no dash used as
punctuation anywhere including table cells, no empty section, no placeholder
text, every pie title stating its whole, and a file under 2 MB. Any failure
names the failing check and exits non zero.

Output lands next to the run: `SEO_Audit_<host>_<date>.docx` and
`report_usage.json`.

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
    fingerprints the text; `validate.py` turns those facts into findings;
    `pagespeed.py` samples templates for performance; `scoring.py` applies
    `score_weights.json` to produce page and site scores.
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
3. Links and content similarity, PageSpeed sample and scoring (done)
4. Findings JSON, report narrative, docx report, baseline vs full mode
5. Streamlit console with optional Search Console upload

Rendering with Playwright is added when the `render_suspects` count on a real
site says it is needed.
