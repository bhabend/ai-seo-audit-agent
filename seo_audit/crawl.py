"""Breadth-first crawl over the link graph, plus sitemap seeds and a sweep.

Three shapes worth knowing before reading the output:

* BFS is level-synchronous. A whole depth is fetched by the worker pool before
  the next one starts, so `depth` still means what it says once there is more
  than one worker, and a capped run samples the site evenly rather than
  diving down whichever branch happens to answer first.
* Page HTML never leaves the worker that fetched it. Links are extracted and
  the text is measured inside the worker; only a finished row and a list of
  links come back. Memory stays flat whether the crawl is 40 pages or 2,000.
* Every time the crawler works around something -- adopting a redirected
  host, folding a trailing-slash redirect, skipping a blocked URL -- it
  writes a row to the crawlability log instead of quietly moving on.
"""

from __future__ import annotations

import os
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .config import AuditConfig
from .discovery import (RobotsInfo, SitemapResult, discover_sitemaps,
                        effective_crawl_delay, fetch_robots)
from .fetch import Fetcher, is_html_content_type
from .issues import (DEEP_PAGE_DEPTH, RENDER_SUSPECT_CHARS, SLOW_DOCUMENT_MS,
                     SLOW_RESPONSE_MS, IssueLog, PageIssueLog,
                     ai_crawler_groups)
from .output import (AUDIT_PAGE_COLUMNS, CSV_COLUMNS, SWEEP_COLUMNS,
                     CrawlStats, HtmlStore, StreamingCsv, write_summary_json)
from .content import MIN_WORDS_FOR_COMPARISON, ContentIndex, fingerprint
from .links import (LinkGraph, cap_note, describe_referrers,
                    is_generic_anchor)
from .parse import is_document_url, parse_page
from .schema import parse_schema
from .urlnorm import (distinct_targets, extract_links, is_same_site,
                      make_soup, normalize, slash_variant)
from .validate import (CrossPageIndex, PageRecord, check_cross_page,
                       check_page, check_site_level)

# How many URLs are in flight at once, as a multiple of the worker count.
# Caps how much page HTML can exist at any moment.
CHUNK_PER_WORKER = 4

# Sweep throttle guard. 403 and 503 are how a site says "slow down"; treating
# them as sitemap defects told a client two thirds of their sitemap was dead.
THROTTLE_STATUSES = (403, 503)
THROTTLE_CONSECUTIVE = 20
THROTTLE_WINDOW = 100
THROTTLE_RATE = 0.5
MAX_SWEEP_DELAY = 4.0


@dataclass
class CrawlOutcome:
    """Everything one crawl produced. Rows live in the CSV, not in here."""

    config: AuditConfig
    stats: CrawlStats = field(default_factory=CrawlStats)
    robots: Optional[RobotsInfo] = None
    sitemap: Optional[SitemapResult] = None
    crawl_delay_applied: float = 0.0
    robots_blocked_count: int = 0
    sweep_checked: int = 0
    sweep_already_crawled: int = 0
    sweep_blocked: int = 0
    sitemap_urls_total: int = 0
    sweep_capped: bool = False
    sweep_throttled: bool = False
    sweep_throttled_responses: int = 0
    link_stats: Dict[str, int] = field(default_factory=dict)
    content_stats: Dict[str, int] = field(default_factory=dict)
    canonical_targets_unchecked: int = 0
    page_issue_counts: Dict[str, int] = field(default_factory=dict)
    page_issue_severity: Dict[str, int] = field(default_factory=dict)
    sweep_status_counts: Counter = field(default_factory=Counter)
    issue_counts: Dict[str, int] = field(default_factory=dict)
    html_files_kept: int = 0
    duration_seconds: float = 0.0
    started_at: Optional[str] = None
    paths: Dict[str, str] = field(default_factory=dict)


def _host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


class _Crawler:
    """One run. Holds the bookkeeping the workers must not touch."""

    def __init__(self, config: AuditConfig, fetcher: Fetcher,
                 issues: IssueLog, rows: StreamingCsv, html_store: HtmlStore,
                 outcome: CrawlOutcome, pages: Optional[StreamingCsv] = None,
                 page_issues: Optional[PageIssueLog] = None):
        self.config = config
        self.fetcher = fetcher
        self.issues = issues
        self.rows = rows
        self.pages = pages
        self.page_issues = page_issues
        self.index = CrossPageIndex()
        self.graph = LinkGraph(config.host, config.include_subdomains)
        self.content = ContentIndex()
        # url -> final_url for every fetched URL, so a link to the redirecting
        # form of a page still credits the page that answered (lesson 6).
        self.url_to_final: Dict[str, str] = {}
        self.status_by_final: Dict[str, Optional[int]] = {}
        self.sitemap_crawled: set = set()
        self.html_store = html_store
        self.outcome = outcome
        self.robots: Optional[RobotsInfo] = None
        self.sitemap_set: set = set()
        # URLs that must never be fetched again: already recorded, or reached
        # as the destination of a redirect we already followed.
        self.handled: set = set()
        # URLs an internal link pointed at, whether or not we got to fetch them.
        self.link_discovered: set = set()
        # Blocked URLs already reported, so one forbidden path linked from
        # thirty pages produces one issue row, not thirty.
        self.blocked_seen: set = set()
        self.pages_done = 0

    # ---- bookkeeping -----------------------------------------------------

    def allowed(self, url: str, referrer: Optional[str] = None,
                from_link: bool = False) -> bool:
        if not self.config.respect_robots:
            return True
        if self.robots is None or self.robots.is_allowed(url):
            return True
        if url not in self.blocked_seen:
            self.blocked_seen.add(url)
            self.outcome.robots_blocked_count += 1
            if from_link:
                self.issues.add("robots_blocked_linked", url, referrer,
                                "robots.txt disallows this path")
        return False

    def record(self, row: Dict, links: List[str],
               page: Optional[Dict] = None,
               findings: Optional[List] = None) -> None:
        """Write one finished row and log what a search engine would hit."""
        url = row["url"]
        self.handled.add(url)
        self.pages_done += 1
        row["in_sitemap"] = url in self.sitemap_set
        self.rows.write(row)
        self.outcome.stats.add(row)
        final = row.get("final_url") or url
        self.index.note_status(url, row.get("status_code"))
        self.index.note_status(final, row.get("status_code"))
        # The map lesson 6 needs: a link to `/x` must credit `/x/`.
        self.url_to_final[url] = final
        self.status_by_final[final] = row.get("status_code")
        if row.get("in_sitemap"):
            self.sitemap_crawled.add(final)
        self._log_row_issues(row)
        if page is not None:
            self._record_page(row, page, findings or [])

        # A redirect we already followed: never spend a second fetch on the
        # destination, and say so when it is only a trailing slash apart.
        final_url = row.get("final_url")
        if final_url and final_url != url:
            if is_same_site(final_url, self.config.host,
                            self.config.include_subdomains):
                self.handled.add(final_url)
            if slash_variant(url) == final_url:
                self.issues.add(
                    "trailing_slash_redirect", url, row.get("discovered_from"),
                    f"redirects to {final_url}")

    def _record_page(self, row: Dict, page: Dict, findings: List) -> None:
        """Write the parsed fields and this page's own findings.

        Everything here is keyed on final_url: the page that answered is the
        page being judged.
        """
        final_url = row.get("final_url") or row["url"]
        page["url"] = row["url"]
        page["final_url"] = final_url
        page["status_code"] = row.get("status_code")
        page["in_sitemap"] = row.get("in_sitemap")
        page["depth"] = row.get("depth")
        page["issue_count"] = len(findings)
        page["issues"] = ";".join(sorted({f[0] for f in findings}))

        if self.pages is not None:
            self.pages.write(page)
        self.outcome.stats.pages_parsed += 1
        for type_name in page.get("_schema_types", ()):  # counted, not written
            self.outcome.stats.schema_type_counts[type_name] += 1

        if self.page_issues is not None:
            for issue_type, detail in findings:
                self.page_issues.add(issue_type, row["url"], final_url, detail)

        self.graph.add_page(final_url, page.pop("_links", []) or [])
        simhash_value = page.get("_simhash", 0)
        self.content.add(final_url, page.get("content_md5", ""),
                         simhash_value, page.get("word_count", 0) or 0)

        self.index.add_page(PageRecord(
            url=row["url"],
            final_url=final_url,
            title=page.get("title") or None,
            meta_description=page.get("meta_description") or None,
            canonical=page.get("canonical") or None,
            noindex=bool(page.get("_noindex")),
            hreflang=list(page.get("_hreflang") or []),
            in_sitemap=bool(row.get("in_sitemap")),
        ))

    def _log_row_issues(self, row: Dict) -> None:
        url = row["url"]
        referrer = row.get("discovered_from")
        status = row.get("status_code")
        depth = row.get("depth")

        if row.get("error"):
            self.issues.add("fetch_error", url, referrer, str(row["error"]))
        if row.get("redirect_loop"):
            self.issues.add("redirect_loop", url, referrer,
                            "redirect chain never resolved")
        if status is not None and status >= 500:
            self.issues.add("fetch_error", url, referrer, f"HTTP {status}")
        if (row.get("redirect_hops") or 0) >= 2:
            self.issues.add("redirect_chain", url, referrer,
                            f"{row['redirect_hops']} hops to {row.get('final_url')}")
        if isinstance(depth, int) and depth > DEEP_PAGE_DEPTH:
            self.issues.add("deep_page", url, referrer,
                            f"{depth} clicks from the homepage")
        rt = row.get("response_time_ms")
        is_document = is_document_url(url)
        slow_limit = SLOW_DOCUMENT_MS if is_document else SLOW_RESPONSE_MS
        if rt is not None and rt > slow_limit:
            kind = "document" if is_document else "page"
            self.issues.add("slow_response", url, referrer,
                            f"{rt} ms for a {kind} (limit {slow_limit} ms)")
        text_chars = row.get("text_chars")
        if text_chars is not None and text_chars < RENDER_SUSPECT_CHARS:
            self.issues.add("render_suspect", url, referrer,
                            f"{text_chars} visible characters")
        if (row.get("in_crawl") and text_chars is None and not row.get("error")
                and not is_html_content_type(row.get("content_type"))):
            # A PDF or Office file is indexable in its own right; anything
            # else linked as a page is not.
            kind = "document_linked" if is_document else "non_html_linked"
            self.issues.add(kind, url, referrer,
                            f"content-type {row.get('content_type')}")
        if row.get("in_crawl") and not row.get("in_sitemap"):
            self.issues.add("crawl_only_page", url, referrer,
                            "reachable by links, absent from the sitemap")
        # sitemap_only_page is NOT raised here: it needs the link graph, so
        # it moved to the post-crawl pass where inlinks are actually known.

    # ---- fetching --------------------------------------------------------

    def _work(self, task: Tuple[str, Optional[int], bool, Optional[str]]):
        """Runs on a worker thread. HTML is used and dropped in here.

        One soup per page, shared by link extraction, field parsing and
        JSON-LD. Nothing HTML-shaped survives this function.
        """
        url, depth, via_link, discovered_from = task
        result = self.fetcher.fetch(url)
        links: List[str] = []
        page = None
        findings: List = []
        if result.html:
            self.html_store.save(url, result.html)
            base = result.final_url or url
            soup = make_soup(result.html)
            all_links = extract_links(soup, base)
            links = [
                target for target in distinct_targets(all_links)
                if is_same_site(target, self.config.host,
                                self.config.include_subdomains)
            ]
            # Only a page that actually answered 200 is worth judging: a 404
            # body is a real HTML page, but it is not a page of the site.
            if result.status_code == 200:
                page, findings = self._parse_and_check(result, base, soup,
                                                       all_links)
        row = {
            "url": url,
            "final_url": result.final_url,
            "status_code": result.status_code,
            "redirect_hops": result.redirect_hops,
            "response_time_ms": result.response_time_ms,
            "content_type": result.content_type,
            "in_crawl": via_link,
            "depth": depth,
            "discovered_from": discovered_from,
            "text_chars": result.text_chars,
            "error": result.error,
            "redirect_loop": result.redirect_loop,
        }
        return row, links, page, findings

    def _parse_and_check(self, result, base: str, soup, all_links: List[str]):
        """Extract this page's fields, read its JSON-LD, and judge both."""
        schema = parse_schema(soup)
        fields = parse_page(soup, base, all_links, self.config.host,
                            self.config.include_subdomains, result.headers)
        findings = check_page(fields, schema, base)
        # The text is fingerprinted here and dropped here: 16 bytes leave the
        # worker, never the page text.
        marks = fingerprint(fields.visible_text, fields.word_count)
        fields.visible_text = ""
        page = {
            "title": fields.title,
            "title_length": fields.title_length,
            "meta_description": fields.meta_description,
            "meta_description_length": fields.meta_description_length,
            "meta_robots": fields.meta_robots,
            "x_robots_tag": fields.x_robots_tag,
            "canonical": fields.canonical,
            "canonical_is_self": (fields.canonical == base
                                  if fields.canonical else None),
            "hreflang_count": len(fields.hreflang),
            "hreflang": ";".join(f"{lang}={href}"
                                 for lang, href in fields.hreflang),
            "viewport": fields.viewport,
            "html_lang": fields.html_lang,
            "h1_count": fields.h1_count,
            "h1": " | ".join(fields.h1[:3]),
            "h2_count": fields.h2_count,
            "h3_count": fields.h3_count,
            "word_count": fields.word_count,
            "image_count": fields.image_count,
            "images_missing_alt": fields.images_missing_alt,
            "images_empty_alt": fields.images_empty_alt,
            "internal_links": fields.internal_links,
            "external_links": fields.external_links,
            "nofollow_internal_links": fields.nofollow_internal_links,
            "mixed_content_count": len(fields.mixed_content),
            "schema_types": ";".join(schema.types),
            "schema_block_count": schema.block_count,
            "schema_invalid_count": schema.invalid_count,
            "microdata_types": ";".join(fields.microdata_types),
            "has_microdata": fields.has_microdata,
            "content_simhash": marks.simhash_hex,
            "content_md5": marks.md5,
            # Underscored keys never reach the CSV; the writer drops them.
            "_schema_types": schema.types,
            "_noindex": fields.is_noindex,
            "_hreflang": fields.hreflang,
            "_links": all_links,
            "_simhash": marks.simhash,
        }
        return page, findings

    def run_batch(self, tasks: List[Tuple], executor: ThreadPoolExecutor):
        """Fetch a chunk in parallel, then fold the results in order."""
        out = []
        for row, links, page, findings in executor.map(self._work, tasks):
            self.record(row, links, page, findings)
            out.append((row, links))
        return out


def _chunks(items: List, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def run_crawl(config: AuditConfig, out_dir: Optional[str] = None,
              fetcher: Optional[Fetcher] = None) -> CrawlOutcome:
    """Crawl `config.domain` and write every artefact into `out_dir`."""
    started = time.perf_counter()
    outcome = CrawlOutcome(config=config,
                           started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(config)

    # --- preflight: robots, then the homepage, which may rename the host ---
    robots = (fetch_robots(fetcher, config) if config.respect_robots
              else RobotsInfo(url=config.url_for("/robots.txt"), found=False))
    outcome.robots = robots
    delay = effective_crawl_delay(config, robots)
    fetcher.set_delay(delay)
    outcome.crawl_delay_applied = delay

    pending_issues: List[Tuple] = []
    for agent, rules in ai_crawler_groups(robots.raw_text):
        pending_issues.append(("ai_crawler_rule", robots.url, None,
                               f"{agent}: {'; '.join(rules) or 'no rules'}"))

    start_url = normalize(config.start_url)
    home_row = None
    home_html: Optional[str] = None
    home_links: List[str] = []
    home_page = None
    home_findings: List = []
    home_headers: Dict[str, str] = {}
    if start_url and (not config.respect_robots or robots.is_allowed(start_url)):
        home = fetcher.fetch(start_url)
        home_final = home.final_url
        if home_final and _host_of(home_final) != config.host and is_same_site(
                home_final, config.host, include_subdomains=True):
            adopted = _host_of(home_final)
            pending_issues.append((
                "host_redirect", start_url, None,
                f"{config.host} redirects to {adopted}; "
                f"{adopted} adopted as the canonical host"))
            config.adopt_host(adopted)
        home_row = {
            "url": start_url,
            "final_url": home.final_url,
            "status_code": home.status_code,
            "redirect_hops": home.redirect_hops,
            "response_time_ms": home.response_time_ms,
            "content_type": home.content_type,
            "in_crawl": True,
            "depth": 0,
            "discovered_from": None,
            "text_chars": home.text_chars,
            "error": home.error,
            "redirect_loop": home.redirect_loop,
        }
        home_headers = dict(home.headers)
        if home.html:
            base = home.final_url or start_url
            soup = make_soup(home.html)
            all_links = extract_links(soup, base)
            home_links = [
                target for target in distinct_targets(all_links)
                if is_same_site(target, config.host, config.include_subdomains)
            ]
            if home.status_code == 200:
                home_page, home_findings = _parse_page_fields(
                    home, base, soup, all_links, config)
            home_html = home.html
            del soup  # the homepage tree does not outlive the preflight
        del home  # release the response before the crawl proper

    # --- the run folder can only be named now that the host is settled ---
    out_dir = out_dir or default_out_dir(config.host)
    paths = {
        "raw_crawl_csv": f"{out_dir}/raw_crawl.csv",
        "audit_pages_csv": f"{out_dir}/audit_pages.csv",
        "page_issues_csv": f"{out_dir}/page_issues.csv",
        "crawl_issues_csv": f"{out_dir}/crawl_issues.csv",
        "sitemap_sweep_csv": f"{out_dir}/sitemap_sweep.csv",
        "crawl_summary_json": f"{out_dir}/crawl_summary.json",
    }
    outcome.paths = paths

    issues = IssueLog(paths["crawl_issues_csv"])
    page_issues = PageIssueLog(paths["page_issues_csv"])
    rows = StreamingCsv(paths["raw_crawl_csv"], CSV_COLUMNS)
    pages_csv = StreamingCsv(paths["audit_pages_csv"], AUDIT_PAGE_COLUMNS)
    sweep_csv = StreamingCsv(paths["sitemap_sweep_csv"], SWEEP_COLUMNS)
    html_store = HtmlStore(out_dir, config.keep_html)
    if home_row is not None:
        # The homepage was fetched before this folder had a name; store it now.
        html_store.save(home_row["url"], home_html)
        home_html = None

    try:
        for issue in pending_issues:
            issues.add(*issue)

        crawler = _Crawler(config, fetcher, issues, rows, html_store, outcome,
                           pages=pages_csv, page_issues=page_issues)
        crawler.robots = robots

        # --- sitemaps, now resolved against the adopted host ---
        sitemap = discover_sitemaps(fetcher, config, robots)
        outcome.sitemap = sitemap
        sitemap_urls = [
            u for u in sitemap.urls
            if is_same_site(u, config.host, config.include_subdomains)
        ]
        crawler.sitemap_set = set(sitemap_urls)

        if len(sitemap.candidates) > 1:
            for url, origin, used in sitemap.candidates:
                issues.add("multiple_sitemaps", url, None,
                           f"found via {origin}; "
                           f"{'used' if used else 'not used'}")

        reserve = min(len(crawler.sitemap_set),
                      int(config.max_pages * config.sitemap_reserve))
        link_budget = max(1, config.max_pages - reserve)

        chunk_size = max(1, config.workers * CHUNK_PER_WORKER)
        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            # Phase 1: the link graph, one depth at a time.
            level: List[Tuple] = []
            if home_row is not None:
                crawler.record(home_row, home_links, home_page, home_findings)
                for href in home_links:
                    crawler.link_discovered.add(href)
                    if href not in crawler.handled:
                        level.append((href, 1, True, home_row["url"]))
            level = _dedupe_tasks(level)

            depth = 1
            while level and depth <= config.max_depth:
                if crawler.pages_done >= link_budget:
                    break
                next_level: List[Tuple] = []
                for chunk in _chunks(level, chunk_size):
                    room = link_budget - crawler.pages_done
                    if room <= 0:
                        break
                    chunk = [t for t in chunk if t[0] not in crawler.handled]
                    chunk = [t for t in chunk
                             if crawler.allowed(t[0], t[3], from_link=True)]
                    chunk = chunk[:room]
                    if not chunk:
                        continue
                    for row, links in crawler.run_batch(chunk, executor):
                        if depth >= config.max_depth:
                            continue
                        for href in links:
                            crawler.link_discovered.add(href)
                            if href not in crawler.handled:
                                next_level.append(
                                    (href, depth + 1, True, row["url"]))
                level = _dedupe_tasks(next_level)
                depth += 1

            # Phase 2: sitemap URLs the link crawl never reached.
            seeds = [(u, None, False, None) for u in sitemap_urls
                     if u not in crawler.handled]
            for chunk in _chunks(seeds, chunk_size):
                room = config.max_pages - crawler.pages_done
                if room <= 0:
                    break
                chunk = [t for t in chunk if t[0] not in crawler.handled]
                chunk = [t for t in chunk if crawler.allowed(t[0])]
                chunk = chunk[:room]
                if chunk:
                    crawler.run_batch(chunk, executor)

            # Phase 3: sweep every sitemap URL the crawl did not fetch.
            _sweep(crawler, sitemap_urls, sweep_csv, executor, chunk_size)

        # --- after the crawl: checks that need more than one page ---
        _run_link_and_content_checks(crawler, page_issues, pages_csv, out_dir)
        _run_cross_page_checks(crawler, page_issues)
        _run_site_level_checks(crawler, page_issues, home_row, home_headers)

        outcome.html_files_kept = html_store.files_written
        outcome.issue_counts = issues.counts()
        outcome.page_issue_counts = page_issues.counts()
        outcome.page_issue_severity = page_issues.severity_counts()
        outcome.duration_seconds = round(time.perf_counter() - started, 2)
    finally:
        rows.close()
        pages_csv.close()
        sweep_csv.close()
        issues.close()
        page_issues.close()
        if own_fetcher:
            fetcher.close()

    write_summary_json(outcome, paths["crawl_summary_json"])
    return outcome


def _dedupe_tasks(tasks: List[Tuple]) -> List[Tuple]:
    """First referrer wins, and BFS order within the level is preserved."""
    seen = set()
    out = []
    for task in tasks:
        if task[0] in seen:
            continue
        seen.add(task[0])
        out.append(task)
    return out


def _sweep(crawler: _Crawler, sitemap_urls: List[str], sweep_csv: StreamingCsv,
           executor: ThreadPoolExecutor, chunk_size: int) -> None:
    """Status-check every sitemap URL the page crawl did not already fetch.

    HEAD only, with a GET fallback, so a sitemap larger than the page cap is
    still audited in full rather than sampled. No HTML is fetched or stored.
    """
    outcome = crawler.outcome
    outcome.sitemap_urls_total = len(sitemap_urls)
    todo = []
    for url in sitemap_urls:
        if url in crawler.handled:
            outcome.sweep_already_crawled += 1
            continue
        todo.append(url)

    limit = crawler.config.sweep_limit
    if len(todo) > limit:
        # A 40,000-URL sitemap would otherwise run for hours. Say so rather
        # than quietly checking a prefix.
        outcome.sweep_capped = True
        crawler.issues.add(
            "sitemap_sweep_capped", crawler.config.domain, None,
            f"{len(todo)} sitemap URLs to sweep, capped at {limit}")
        todo = todo[:limit]

    # HEAD is cheap next to a page fetch, so the sweep gets its own delay.
    page_delay = crawler.fetcher.delay
    crawler.fetcher.set_delay(crawler.config.sweep_delay)

    def check(url: str) -> Dict:
        blocked = (crawler.config.respect_robots and crawler.robots is not None
                   and not crawler.robots.is_allowed(url))
        if blocked:
            # A crawler that obeys robots.txt never makes this request, so
            # neither do we: the row records why, not a status it never saw.
            return {"url": url, "blocked_by_robots": True, "status_code": None,
                    "final_url": None, "redirect_hops": 0, "error": None}
        result = crawler.fetcher.head(url)
        return {
            "url": url,
            "blocked_by_robots": False,
            "status_code": result.status_code,
            "final_url": result.final_url,
            "redirect_hops": result.redirect_hops,
            "error": result.error,
            "redirect_loop": result.redirect_loop,
        }

    # Throttle guard. A site that starts refusing the sweep is not a site
    # whose sitemap is broken, and the two must never be recorded as one.
    consecutive = 0
    recent: deque = deque(maxlen=THROTTLE_WINDOW)
    current_delay = crawler.config.sweep_delay
    stopped_at = None

    for chunk in _chunks(todo, chunk_size):
        if stopped_at is not None:
            break
        for row in executor.map(check, chunk):
            url = row["url"]
            row["in_crawl"] = url in crawler.link_discovered
            sweep_csv.write(row)
            outcome.sweep_checked += 1

            status = row["status_code"]
            throttling = status in THROTTLE_STATUSES
            if not row["blocked_by_robots"]:
                recent.append(1 if throttling else 0)
                consecutive = consecutive + 1 if throttling else 0
                if throttling:
                    outcome.sweep_throttled_responses += 1

            if row["blocked_by_robots"]:
                outcome.sweep_blocked += 1
                outcome.sweep_status_counts["blocked_by_robots"] += 1
                crawler.issues.add(
                    "robots_blocked_in_sitemap", url, None,
                    "listed in the sitemap but disallowed by robots.txt")
            else:
                outcome.sweep_status_counts[
                    str(status) if status is not None else "no_response"] += 1
                if row.get("error") or status is None:
                    crawler.issues.add("fetch_error", url, None,
                                       str(row.get("error")))
                elif throttling or outcome.sweep_throttled:
                    # 403 and 503 are how a site says "slow down". They are
                    # never counted as a dead sitemap entry, and once we know
                    # we are being throttled nothing after that is trusted.
                    pass
                elif status != 200:
                    crawler.issues.add("sitemap_non_200", url, None,
                                       f"HTTP {status}")
                if row["redirect_hops"]:
                    crawler.issues.add(
                        "redirect_in_sitemap", url, None,
                        f"{row['redirect_hops']} hop(s) to {row['final_url']}")
                if row.get("redirect_loop"):
                    crawler.issues.add("redirect_loop", url, None,
                                       "redirect chain never resolved")

            if throttling and current_delay < MAX_SWEEP_DELAY:
                current_delay = min(current_delay * 2, MAX_SWEEP_DELAY)
                crawler.fetcher.set_delay(current_delay)

            rate = sum(recent) / len(recent) if len(recent) == THROTTLE_WINDOW else 0
            if consecutive >= THROTTLE_CONSECUTIVE or rate > THROTTLE_RATE:
                outcome.sweep_throttled = True
                stopped_at = url
                break

    if stopped_at is not None:
        crawler.issues.add(
            "sweep_throttled", crawler.config.domain, None,
            f"stopped after {outcome.sweep_checked} of {len(todo)} sitemap "
            f"URLs: {outcome.sweep_throttled_responses} responses were 403 or "
            f"503, last at {stopped_at}. Those are rate limiting, not broken "
            f"sitemap entries, and are not counted as such.")
    crawler.fetcher.set_delay(page_delay)


def default_out_dir(host: str) -> str:
    """output/<adopted host>/<timestamp>/ -- one folder per run."""
    return os.path.join("output", host, time.strftime("%Y%m%d-%H%M%S"))


def _parse_page_fields(result, base: str, soup, all_links: List[str],
                       config: AuditConfig):
    """Parse and check one page outside a _Crawler, for the preflight homepage.

    The homepage is fetched before the run folder has a name, so it cannot go
    through the worker path; it must not go unparsed either.
    """
    schema = parse_schema(soup)
    fields = parse_page(soup, base, all_links, config.host,
                        config.include_subdomains, result.headers)
    findings = check_page(fields, schema, base)
    marks = fingerprint(fields.visible_text, fields.word_count)
    fields.visible_text = ""
    page = {
        "title": fields.title,
        "title_length": fields.title_length,
        "meta_description": fields.meta_description,
        "meta_description_length": fields.meta_description_length,
        "meta_robots": fields.meta_robots,
        "x_robots_tag": fields.x_robots_tag,
        "canonical": fields.canonical,
        "canonical_is_self": (fields.canonical == base
                              if fields.canonical else None),
        "hreflang_count": len(fields.hreflang),
        "hreflang": ";".join(f"{lang}={href}" for lang, href in fields.hreflang),
        "viewport": fields.viewport,
        "html_lang": fields.html_lang,
        "h1_count": fields.h1_count,
        "h1": " | ".join(fields.h1[:3]),
        "h2_count": fields.h2_count,
        "h3_count": fields.h3_count,
        "word_count": fields.word_count,
        "image_count": fields.image_count,
        "images_missing_alt": fields.images_missing_alt,
        "images_empty_alt": fields.images_empty_alt,
        "internal_links": fields.internal_links,
        "external_links": fields.external_links,
        "nofollow_internal_links": fields.nofollow_internal_links,
        "mixed_content_count": len(fields.mixed_content),
        "schema_types": ";".join(schema.types),
        "schema_block_count": schema.block_count,
        "schema_invalid_count": schema.invalid_count,
        "microdata_types": ";".join(fields.microdata_types),
        "has_microdata": fields.has_microdata,
        "content_simhash": marks.simhash_hex,
        "content_md5": marks.md5,
        "_schema_types": schema.types,
        "_noindex": fields.is_noindex,
        "_hreflang": fields.hreflang,
        "_links": all_links,
        "_simhash": marks.simhash,
    }
    return page, findings


def _run_cross_page_checks(crawler: _Crawler,
                           page_issues: PageIssueLog) -> None:
    """Checks over the pages already crawled. Never re-fetches a known URL."""
    checked = {"count": 0}

    def head_check(url: str) -> Optional[int]:
        """Only ever called for a canonical target the crawl never saw."""
        checked["count"] += 1
        result = crawler.fetcher.head(url)
        return result.status_code

    unchecked = 0
    for issue_type, url, final_url, detail in check_cross_page(
            crawler.index, head_check=head_check,
            canonical_check_limit=crawler.config.canonical_check_limit):
        if issue_type == "canonical_target_unchecked":
            unchecked += 1
        page_issues.add(issue_type, url, final_url, detail)
    crawler.outcome.canonical_targets_unchecked = unchecked


def _run_site_level_checks(crawler: _Crawler, page_issues: PageIssueLog,
                           home_row: Optional[Dict],
                           home_headers: Dict[str, str]) -> None:
    """Transport and header checks, recorded once against the homepage row."""
    if home_row is None:
        return
    http_ok: Optional[bool] = None
    http_url = "http://" + crawler.config.host + "/"
    result = crawler.fetcher.head(http_url)
    if result.status_code is not None:
        http_ok = bool(result.final_url
                       and result.final_url.startswith("https://"))

    findings = check_site_level(home_headers, http_ok)
    final_url = home_row.get("final_url") or home_row["url"]
    for issue_type, detail in findings:
        page_issues.add(issue_type, home_row["url"], final_url, detail,
                        site_level=True)


def _run_link_and_content_checks(crawler: _Crawler, page_issues: PageIssueLog,
                                 pages_csv: StreamingCsv,
                                 out_dir: str) -> None:
    """Build the link graph, compare fingerprints, and write both verdicts.

    Runs after the crawl because a link graph is only meaningful once there is
    something to point at, and because a page's inlink count is not knowable
    while pages are still arriving.
    """
    config = crawler.config
    outcome = crawler.outcome
    cap = config.max_pages
    within = cap_note(cap)

    # Lesson 6: a link to the redirecting form of a page credits the page.
    crawler.graph.resolve(crawler.url_to_final)

    parsed = set(crawler.index.pages)
    per_page = crawler.graph.per_page(parsed)
    home = normalize(config.start_url)
    home_final = crawler.url_to_final.get(home, home)

    zero_inlinks = 0
    for final_url, record in crawler.index.pages.items():
        stats = per_page.get(final_url)
        if stats is None:
            continue
        if stats.inlinks == 0:
            zero_inlinks += 1
            if final_url != home_final:
                page_issues.add(
                    "orphan_page", record.url, final_url,
                    f"no crawled page links here, {within}")
        elif stats.inlinks == 1:
            page_issues.add("low_inlink_page", record.url, final_url,
                            f"exactly 1 inbound link {within}")

        if stats.nofollow_outlinks:
            page_issues.add(
                "nofollow_internal_link", record.url, final_url,
                f"{stats.nofollow_outlinks} internal link(s) on this page "
                f"carry rel=nofollow")

        anchors = crawler.graph.inbound_anchors(final_url)
        if anchors and all(is_generic_anchor(a) for a in anchors):
            page_issues.add(
                "generic_anchor", record.url, final_url,
                f"every inbound anchor is generic: "
                f"{', '.join(sorted(set(anchors))[:5])}")

        # A2: only a crawled sitemap page with no inbound link is orphaned by
        # the sitemap's own account. Swept-only URLs never qualify.
        if (record.in_sitemap and stats.inlinks == 0
                and final_url != home_final):
            crawler.issues.add(
                "sitemap_only_page", final_url, None,
                f"listed in the sitemap, crawled, and no crawled page links "
                f"here ({within})")

    # --- targets the crawl saw as broken or redirecting ---
    broken = 0
    redirected = 0
    by_target: Dict[str, set] = {}
    for edge in crawler.graph.edges:
        by_target.setdefault(edge.target, set()).add(edge.source)

    for target, sources in sorted(by_target.items()):
        status = crawler.status_by_final.get(target)
        if status is None:
            status = crawler.index.status_by_url.get(target)
        if status is None:
            continue  # never fetched: not a claim we can make
        if status >= 400:
            broken += 1
            page_issues.add("broken_internal_link", target, target,
                            f"HTTP {status}, {describe_referrers(sources)}")

    for url, final in crawler.url_to_final.items():
        if url == final:
            continue
        sources = by_target.get(final, set()) | by_target.get(url, set())
        if not sources:
            continue
        redirected += 1
        page_issues.add("redirected_internal_link", url, final,
                        f"links point at {url}, which redirects to {final}; "
                        f"{describe_referrers(sources)}")

    # --- external links, capped ---
    external = sorted(crawler.graph.external_targets)
    limit = config.external_check_limit
    checked = external[:limit]
    unchecked = len(external) - len(checked)
    external_broken = 0
    for target in checked:
        result = crawler.fetcher.head(target)
        status = result.status_code
        if status is None or status >= 400:
            external_broken += 1
            page_issues.add(
                "external_link_broken", target, target,
                f"{'no response' if status is None else 'HTTP ' + str(status)}, "
                f"{describe_referrers(crawler.graph.external_targets[target])}")

    outcome.link_stats = {
        "pages_with_zero_inlinks": zero_inlinks,
        "broken_internal_targets": broken,
        "redirected_internal_targets": redirected,
        "external_targets_found": len(external),
        "external_checked": len(checked),
        "external_broken": external_broken,
        "external_unchecked": unchecked,
        "external_check_limit": limit,
        "edges": len(crawler.graph.edges),
    }

    # --- duplicate and near-duplicate content ---
    exact = crawler.content.exact_groups()
    near = crawler.content.near_groups()
    for group in exact:
        page_issues.add("duplicate_content", group[0], group[0],
                        f"{len(group)} pages have identical text: "
                        f"{', '.join(group[:5])}"
                        + (f", and {len(group) - 5} more" if len(group) > 5 else ""))
    for group in near:
        page_issues.add("near_duplicate_content", group[0], group[0],
                        f"{len(group)} pages are near-identical "
                        f"(simhash distance <= 3): {', '.join(group[:5])}"
                        + (f", and {len(group) - 5} more" if len(group) > 5 else ""))
    outcome.content_stats = {
        "duplicate_groups": len(exact),
        "near_duplicate_groups": len(near),
        "pages_compared": len(crawler.content.comparable()),
        "min_words_for_comparison": MIN_WORDS_FOR_COMPARISON,
    }

    # --- the per-page link columns, merged into audit_pages.csv ---
    # The rows streamed during the crawl, before any of this was knowable.
    # One row-by-row rewrite fills the link columns in without ever holding
    # the page list in memory.
    pages_csv.close()
    _merge_link_columns(pages_csv.path, per_page)

    if config.write_links:
        links_csv = StreamingCsv(os.path.join(out_dir, "links.csv"),
                                 ["source", "target", "anchor", "nofollow"])
        try:
            for edge in crawler.graph.edges:
                links_csv.write({"source": edge.source, "target": edge.target,
                                 "anchor": edge.anchor,
                                 "nofollow": edge.nofollow})
        finally:
            links_csv.close()
        crawler.outcome.paths["links_csv"] = os.path.join(out_dir, "links.csv")


def _merge_link_columns(path: str, per_page: Dict[str, object]) -> None:
    """Fill in the link columns on an already-streamed audit_pages.csv.

    Reads and writes one row at a time, so a 2,000-page file costs one row of
    memory, not two thousand.
    """
    import csv as _csv
    import tempfile

    temp_path = path + ".merging"
    with open(path, encoding="utf-8-sig", newline="") as source,             open(temp_path, "w", encoding="utf-8-sig", newline="") as target:
        reader = _csv.DictReader(source)
        writer = _csv.DictWriter(target, fieldnames=AUDIT_PAGE_COLUMNS,
                                 extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            stats = per_page.get(row.get("final_url"))
            if stats is not None:
                row["inlinks"] = stats.inlinks
                row["nofollow_inlinks"] = stats.nofollow_inlinks
                row["outlinks_internal"] = stats.outlinks_internal
                row["outlinks_external"] = stats.outlinks_external
                row["anchor_texts"] = ";".join(stats.anchor_texts)
            writer.writerow(row)
    os.replace(temp_path, path)
