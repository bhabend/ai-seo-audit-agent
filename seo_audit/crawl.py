"""Breadth-first crawl over the link graph, plus sitemap seeds.

Ordering policy (deliberate, and worth knowing when reading the output):
the link frontier is drained first so that `depth` means something, but a
slice of the page budget is reserved for sitemap URLs that links never
reached, so sitemap-only coverage gaps still show up in a capped run.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import AuditConfig
from .discovery import (RobotsInfo, SitemapResult, discover_sitemaps,
                        effective_crawl_delay, fetch_robots)
from .fetch import Fetcher
from .urlnorm import extract_links, is_same_site, normalize

# Fraction of max_pages held back for sitemap URLs the link crawl never reached.
SITEMAP_RESERVE_FRACTION = 4


@dataclass
class CrawlRecord:
    """One row of the raw crawl: one URL, one fetch."""

    url: str
    final_url: Optional[str] = None
    status_code: Optional[int] = None
    redirect_hops: int = 0
    response_time_ms: Optional[int] = None
    content_type: Optional[str] = None
    in_sitemap: bool = False
    in_crawl: bool = False
    depth: Optional[int] = None
    discovered_from: Optional[str] = None
    text_chars: Optional[int] = None
    error: Optional[str] = None


@dataclass
class CrawlOutcome:
    """Everything one crawl produced, ready for output.py."""

    config: AuditConfig
    records: List[CrawlRecord] = field(default_factory=list)
    robots: Optional[RobotsInfo] = None
    sitemap: Optional[SitemapResult] = None
    crawl_delay_applied: float = 0.0
    robots_blocked: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    started_at: Optional[str] = None


def crawl(config: AuditConfig, fetcher: Optional[Fetcher] = None) -> CrawlOutcome:
    """Discover and fetch up to `max_pages` URLs for `config.domain`."""
    started = time.perf_counter()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(config)

    outcome = CrawlOutcome(config=config, started_at=started_at)

    robots = (fetch_robots(fetcher, config) if config.respect_robots
              else RobotsInfo(url=config.url_for("/robots.txt"), found=False))
    outcome.robots = robots
    delay = effective_crawl_delay(config, robots)
    outcome.crawl_delay_applied = delay

    sitemap = discover_sitemaps(fetcher, config, robots)
    outcome.sitemap = sitemap

    sitemap_urls = [
        u for u in sitemap.urls
        if is_same_site(u, config.host, config.include_subdomains)
    ]
    sitemap_set = set(sitemap_urls)

    start_url = normalize(config.start_url)
    records: Dict[str, CrawlRecord] = {}
    queued = set()

    frontier = deque()
    if start_url:
        frontier.append((start_url, 0, None))
        queued.add(start_url)

    reserve = min(len(sitemap_set), config.max_pages // SITEMAP_RESERVE_FRACTION)
    link_budget = max(1, config.max_pages - reserve)

    def allowed(url: str) -> bool:
        if not config.respect_robots:
            return True
        if robots.is_allowed(url):
            return True
        outcome.robots_blocked.append(url)
        return False

    def fetch_one(url: str, depth: Optional[int], via_link: bool,
                  discovered_from: Optional[str]):
        """Fetch one URL, record the row, hand back the HTML for link mining."""
        if records:
            time.sleep(delay)
        result = fetcher.fetch(url)
        record = CrawlRecord(
            url=url,
            final_url=result.final_url,
            status_code=result.status_code,
            redirect_hops=result.redirect_hops,
            response_time_ms=result.response_time_ms,
            content_type=result.content_type,
            in_sitemap=url in sitemap_set,
            in_crawl=via_link,
            depth=depth,
            discovered_from=discovered_from,
            text_chars=result.text_chars,
            error=result.error,
        )
        records[url] = record
        return record, result.html

    # Phase 1: follow links from the homepage.
    while frontier and len(records) < link_budget:
        url, depth, discovered_from = frontier.popleft()
        if url in records or not allowed(url):
            continue
        record, html = fetch_one(url, depth, True, discovered_from)
        # Non-HTML responses are recorded with their status but not parsed.
        if not html or depth >= config.max_depth:
            continue
        base = record.final_url or url
        for href in extract_links(html, base):
            if href in queued or href in records:
                continue
            if not is_same_site(href, config.host, config.include_subdomains):
                continue
            queued.add(href)
            frontier.append((href, depth + 1, url))

    # Phase 2: sitemap URLs the link crawl never reached.
    for url in sitemap_urls:
        if len(records) >= config.max_pages:
            break
        if url in records or not allowed(url):
            continue
        fetch_one(url, None, False, None)

    outcome.records = list(records.values())
    outcome.duration_seconds = round(time.perf_counter() - started, 2)
    if own_fetcher:
        fetcher.close()
    return outcome
