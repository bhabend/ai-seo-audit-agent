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
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .config import AuditConfig
from .discovery import (RobotsInfo, SitemapResult, discover_sitemaps,
                        effective_crawl_delay, fetch_robots)
from .fetch import Fetcher, is_html_content_type
from .issues import (DEEP_PAGE_DEPTH, RENDER_SUSPECT_CHARS, SLOW_RESPONSE_MS,
                     IssueLog, ai_crawler_groups)
from .output import (CSV_COLUMNS, SWEEP_COLUMNS, CrawlStats, HtmlStore,
                     StreamingCsv, write_summary_json)
from .urlnorm import extract_links, is_same_site, normalize, slash_variant

# How many URLs are in flight at once, as a multiple of the worker count.
# Caps how much page HTML can exist at any moment.
CHUNK_PER_WORKER = 4


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
                 outcome: CrawlOutcome):
        self.config = config
        self.fetcher = fetcher
        self.issues = issues
        self.rows = rows
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

    def record(self, row: Dict, links: List[str]) -> None:
        """Write one finished row and log what a search engine would hit."""
        url = row["url"]
        self.handled.add(url)
        self.pages_done += 1
        row["in_sitemap"] = url in self.sitemap_set
        self.rows.write(row)
        self.outcome.stats.add(row)
        self._log_row_issues(row)

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
        if rt is not None and rt > SLOW_RESPONSE_MS:
            self.issues.add("slow_response", url, referrer, f"{rt} ms")
        text_chars = row.get("text_chars")
        if text_chars is not None and text_chars < RENDER_SUSPECT_CHARS:
            self.issues.add("render_suspect", url, referrer,
                            f"{text_chars} visible characters")
        if (row.get("in_crawl") and text_chars is None and not row.get("error")
                and not is_html_content_type(row.get("content_type"))):
            self.issues.add("non_html_linked", url, referrer,
                            f"content-type {row.get('content_type')}")
        if row.get("in_crawl") and not row.get("in_sitemap"):
            self.issues.add("crawl_only_page", url, referrer,
                            "reachable by links, absent from the sitemap")
        if (row.get("in_sitemap") and not row.get("in_crawl")
                and url not in self.link_discovered):
            self.issues.add("sitemap_only_page", url, None,
                            "no internal link points here")

    # ---- fetching --------------------------------------------------------

    def _work(self, task: Tuple[str, Optional[int], bool, Optional[str]]):
        """Runs on a worker thread. HTML is used and dropped in here."""
        url, depth, via_link, discovered_from = task
        result = self.fetcher.fetch(url)
        links: List[str] = []
        if result.html:
            self.html_store.save(url, result.html)
            base = result.final_url or url
            links = [
                href for href in extract_links(result.html, base)
                if is_same_site(href, self.config.host,
                                self.config.include_subdomains)
            ]
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
        return row, links

    def run_batch(self, tasks: List[Tuple], executor: ThreadPoolExecutor):
        """Fetch a chunk in parallel, then fold the results in order."""
        out = []
        for row, links in executor.map(self._work, tasks):
            self.record(row, links)
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
        if home.html:
            base = home.final_url or start_url
            home_links = [
                href for href in extract_links(home.html, base)
                if is_same_site(href, config.host, config.include_subdomains)
            ]
            home_html = home.html
        del home  # release the response before the crawl proper

    # --- the run folder can only be named now that the host is settled ---
    out_dir = out_dir or default_out_dir(config.host)
    paths = {
        "raw_crawl_csv": f"{out_dir}/raw_crawl.csv",
        "crawl_issues_csv": f"{out_dir}/crawl_issues.csv",
        "sitemap_sweep_csv": f"{out_dir}/sitemap_sweep.csv",
        "crawl_summary_json": f"{out_dir}/crawl_summary.json",
    }
    outcome.paths = paths

    issues = IssueLog(paths["crawl_issues_csv"])
    rows = StreamingCsv(paths["raw_crawl_csv"], CSV_COLUMNS)
    sweep_csv = StreamingCsv(paths["sitemap_sweep_csv"], SWEEP_COLUMNS)
    html_store = HtmlStore(out_dir, config.keep_html)
    if home_row is not None:
        # The homepage was fetched before this folder had a name; store it now.
        html_store.save(home_row["url"], home_html)
        home_html = None

    try:
        for issue in pending_issues:
            issues.add(*issue)

        crawler = _Crawler(config, fetcher, issues, rows, html_store, outcome)
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
                crawler.record(home_row, home_links)
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

        outcome.html_files_kept = html_store.files_written
        outcome.issue_counts = issues.counts()
        outcome.duration_seconds = round(time.perf_counter() - started, 2)
    finally:
        rows.close()
        sweep_csv.close()
        issues.close()
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
    todo = []
    for url in sitemap_urls:
        if url in crawler.handled:
            outcome.sweep_already_crawled += 1
            continue
        todo.append(url)

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

    for chunk in _chunks(todo, chunk_size):
        for row in executor.map(check, chunk):
            url = row["url"]
            row["in_crawl"] = url in crawler.link_discovered
            sweep_csv.write(row)
            outcome.sweep_checked += 1

            if row["blocked_by_robots"]:
                outcome.sweep_blocked += 1
                outcome.sweep_status_counts["blocked_by_robots"] += 1
                crawler.issues.add(
                    "robots_blocked_in_sitemap", url, None,
                    "listed in the sitemap but disallowed by robots.txt")
            else:
                status = row["status_code"]
                outcome.sweep_status_counts[
                    str(status) if status is not None else "no_response"] += 1
                if row.get("error") or status is None:
                    crawler.issues.add("fetch_error", url, None,
                                       str(row.get("error")))
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
            if not row["in_crawl"]:
                crawler.issues.add("sitemap_only_page", url, None,
                                   "no internal link points here")


def default_out_dir(host: str) -> str:
    """output/<adopted host>/<timestamp>/ -- one folder per run."""
    return os.path.join("output", host, time.strftime("%Y%m%d-%H%M%S"))
