# SEO Audit Agent

What the tool is and how a run goes from start to finish.

---

## 1. What it is

A standalone SEO audit tool. It takes a website domain, crawls the site, extracts and validates SEO signals, scores every page, and produces a Word report plus data files.

It runs two ways, both calling the same Python package in `seo_audit/`:

- Command line: `python -m seo_audit.cli --domain https://www.example.com`
- Streamlit console: `streamlit run app/streamlit_app.py`

It has two audit modes:

- **Baseline**: domain in, everything computed from the crawl and a free PageSpeed sample. For prospective and newly onboarded clients.
- **Full**: same, plus the client's Google Search Console data, which adds a search performance section and joins GSC to the crawl. For clients from the second audit onward.

Rankings, keyword gap and backlink data from third-party tools are never used. Search Console is the only source of search performance figures.

## 2. Pipeline

```
Intake -> Discovery -> Fetch -> Parse -> Validate -> Links & Content -> Performance -> Score -> Findings -> Report
```

| Stage | Module | Output |
|---|---|---|
| Intake | `config.py`, `cli.py` | Run configuration |
| Discovery | `discovery.py`, `urlnorm.py` | Seed URL list from robots.txt, sitemaps and the homepage |
| Fetch | `fetch.py`, `crawl.py` | One row per URL with status, redirects, timing, HTML |
| Parse | `parse.py` | SEO fields per page from a single HTML pass |
| Validate | `technical.py`, `schema.py`, `sitemap_check.py` | Technical, schema and sitemap issues per page |
| Links & Content | `links.py`, `content.py` | Link graph, broken links, orphans, depth, duplicates, thin pages |
| Performance | `pagespeed.py` | Core Web Vitals on a template sample |
| Score | `scoring.py` | Page score and issue list |
| Findings | `findings.py` | `findings.json`, site-level, baseline or full mode |
| Report | `report.py` | `.docx` report with narrative written from `findings.json` |

## 3. Workflow, start to finish

**1. Intake.**
The operator gives a domain. Optional: a sitemap URL, page and depth limits, crawl delay, subdomains on or off, render on or off, and in full mode a Search Console export or API connection. Everything lands in one `AuditConfig` object. A dated output folder is created at `output/<host>/<timestamp>/`.

**2. Discovery.**
The tool fetches `robots.txt`, records disallow rules, crawl delay and any AI-crawler policy, and reads its `Sitemap:` lines. It fetches the sitemap given, or the ones robots.txt names, or `/sitemap.xml` and `/sitemap_index.xml` as fallbacks. Sitemap indexes are followed recursively; gz sitemaps are supported. Every URL is normalised (fragments and tracking parameters removed, `../` resolved, www and non-www treated as one site) so the same page never appears twice.

**3. Crawl and fetch.**
Starting from the homepage and the sitemap URLs, the crawler fetches each page once, records status code, redirect chain, response time, content type and the decoded HTML, then extracts every same-site link on the page and queues it with depth plus one. Robots disallows and the crawl delay are honoured; page and depth caps stop the run. Each row is marked as found in the sitemap, found by crawling, or both, with its depth and first referring page. A visible-text length is recorded per page: if most pages come back near-empty the site depends on JavaScript, and the run is repeated with rendering on.

Output: `raw_crawl.csv` and `crawl_summary.json`.

**4. Parse.**
Each HTML page is parsed once. Title, meta description, headings, canonical, robots meta, hreflang, viewport, images and alt text, internal and external links with anchor text, and JSON-LD blocks are extracted into the page row.

**5. Validate.**
Per page: canonical target exists and self-canonicalises; hreflang pairs are reciprocal; page is indexable; HTTPS with no mixed content; schema types carry their required properties. Site-wide: www and HTTP redirects are consistent; the sitemap contains no non-200, noindex or off-canonical URLs; crawlable pages missing from the sitemap are listed.

**6. Links and content.**
From the link graph: broken internal and external links with the pages that link to them, orphan pages, click depth, anchor-text quality. From the text: near-duplicate pages by similarity, thin pages, duplicate titles and meta descriptions across the site.

**7. Performance.**
Page templates are identified from URL patterns and structure; a sample of each is sent to the PageSpeed Insights API for Core Web Vitals.

**8. Score.**
Every page gets a score from a configurable weight table (indexability, technical, on-page, content, schema, duplication, performance) and a list of the issues that cost it points.

Output: `audit_pages.csv`.

**9. Findings.**
The code aggregates everything into `findings.json`: counts, worst pages, issue lists by category, score distribution, template-level patterns. In full mode it adds the Search Console section and the GSC-to-crawl joins (pages with impressions that are noindex or canonicalised away, sitemap pages with zero impressions, orphan pages that still earn clicks). If a previous run exists for the domain it records what was fixed and what regressed.

**10. Report.**
`findings.json` is sent to OpenAI to write the narrative for each section. The model writes prose only; every number in the report comes from the JSON. `python-docx` assembles the report with fixed sections: executive summary, technical, on-page, content, links, schema, performance, search performance (full mode only, otherwise one line saying it was not provided), prioritised fix list, and appendix tables.

Output: `SEO_Audit_<host>_<date>.docx`, alongside the CSVs and JSON in the run folder.

**11. Console.**
The Streamlit console wraps the same steps: intake form, run with progress, results tables per stage, optional GSC upload, and a Generate Report button that calls step 10.
