"""The crawlability log: every obstacle a search engine would meet.

The rule this file exists to enforce: whenever the crawler resolves, folds,
adopts or skips something to keep going, it says so here. Nothing is silently
improved. Each issue type carries one plain sentence about what a search
engine actually does with it, so the log reads without a decoder ring.
"""

from __future__ import annotations

import csv
import os
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Optional

# The fixed set. A type not in here cannot be recorded, so a typo fails loudly
# instead of quietly inventing a category nobody counts.
CRAWLER_EFFECT: Dict[str, str] = {
    "host_redirect":
        "Every crawl of the configured host costs an extra hop before any "
        "page is reached, and split signals between the two hostnames.",
    "trailing_slash_redirect":
        "The crawler spends an extra request for this link and the link "
        "equity passes through a redirect instead of landing directly.",
    "redirect_chain":
        "Chained redirects slow discovery and some crawlers give up before "
        "reaching the destination.",
    "redirect_loop":
        "The URL never resolves, so the page cannot be crawled or indexed.",
    "redirect_in_sitemap":
        "A sitemap should list final URLs; a redirecting entry wastes crawl "
        "budget and sends a contradictory canonical signal.",
    "robots_blocked_linked":
        "The page is linked internally but robots.txt forbids fetching it, "
        "so that link leads nowhere a crawler can follow.",
    "robots_blocked_in_sitemap":
        "The sitemap asks the crawler to index a URL that robots.txt forbids "
        "it to fetch; the two directives contradict each other.",
    "sitemap_non_200":
        "A sitemap entry that does not return 200 wastes crawl budget and "
        "marks the sitemap as stale.",
    "multiple_sitemaps":
        "Several sitemaps exist; any not advertised in robots.txt may never "
        "be discovered by a crawler that was not told where to look.",
    "crawl_only_page":
        "The page is reachable by links but missing from the sitemap, so "
        "discovery depends entirely on internal linking.",
    "sitemap_only_page":
        "No internal link points at this page, so it receives no link equity "
        "and is discovered only because the sitemap lists it.",
    "deep_page":
        "Pages more than three clicks from the homepage are crawled less "
        "often and tend to rank worse.",
    "slow_response":
        "Slow responses reduce how many pages a crawler fetches per visit.",
    "fetch_error":
        "The crawler gets nothing back, so the page cannot be indexed.",
    "non_html_linked":
        "The URL is linked as if it were a page but returns something that is "
        "neither a page nor an indexable document.",
    "document_linked":
        "A PDF or Office file linked as a page. Search engines index these on "
        "their own terms, so it is worth knowing about, not a fault.",
    "render_suspect":
        "Almost no text arrives in the HTML, so a crawler that does not run "
        "JavaScript sees a nearly empty page.",
    "sitemap_sweep_capped":
        "The sitemap is larger than the sweep limit, so the URLs past the cap "
        "were never status-checked and their state is unknown.",
    "ai_crawler_rule":
        "This robots.txt group decides whether AI assistants and their "
        "crawlers may read the site's content.",
}

ISSUE_COLUMNS = ["issue_type", "url", "referrer", "detail", "crawler_effect"]

# Thresholds the crawl layer judges against.
DEEP_PAGE_DEPTH = 3
# A document is a big file over a slow pipe; a page is not. Judging both at
# 3 seconds reported every large PDF as a site defect.
SLOW_RESPONSE_MS = 3000
SLOW_DOCUMENT_MS = 10000
RENDER_SUSPECT_CHARS = 500

# Page-level checks, each with the severity it is reported at. Separate from
# CRAWLER_EFFECT above: those are obstacles a crawler meets on the way in,
# these are faults in a page that was fetched successfully.
PAGE_ISSUE_SEVERITY: Dict[str, str] = {
    # per page
    "title_missing": "high",
    "title_too_long": "low",
    "title_too_short": "low",
    "meta_description_missing": "medium",
    "meta_description_too_long": "low",
    "h1_missing": "medium",
    "h1_multiple": "low",
    "noindex_page": "high",
    "nofollow_page": "medium",
    "canonical_missing": "medium",
    "canonical_off_page": "medium",
    "canonical_not_absolute": "low",
    "viewport_missing": "medium",
    "html_lang_missing": "low",
    "images_missing_alt": "low",
    "thin_page": "medium",
    "mixed_content": "high",
    "schema_missing": "low",
    "schema_invalid_json": "medium",
    "schema_missing_property": "low",
    # cross page
    "canonical_target_not_crawled": "medium",
    "canonical_target_unchecked": "low",
    "canonical_target_non_200": "high",
    "canonical_chain": "medium",
    "canonical_target_noindex": "high",
    "hreflang_not_reciprocal": "medium",
    "hreflang_target_non_200": "medium",
    "duplicate_title": "medium",
    "duplicate_meta_description": "low",
    "sitemap_noindex": "high",
    "sitemap_off_canonical": "medium",
    # site level, recorded once on the homepage row
    "http_to_https_redirect": "high",
    "hsts_missing": "medium",
    "x_content_type_options_missing": "low",
    "x_frame_options_missing": "low",
    "csp_missing": "low",
}

PAGE_ISSUE_COLUMNS = ["issue_type", "severity", "url", "final_url", "detail",
                      "site_level"]

# Robots user-agent tokens worth reporting when a site names them.
AI_CRAWLER_TOKENS = (
    "gptbot", "chatgpt-user", "oai-searchbot", "claudebot", "anthropic-ai",
    "claude-web", "perplexitybot", "google-extended", "ccbot", "bytespider",
    "applebot-extended", "meta-externalagent", "amazonbot", "youbot",
    "diffbot", "cohere-ai", "imagesiftbot", "timpibot",
)


# robots.txt directives as they are conventionally written.
_DIRECTIVE_CASING = {
    "disallow": "Disallow",
    "allow": "Allow",
    "crawl-delay": "Crawl-delay",
}


@dataclass
class CrawlIssue:
    issue_type: str
    url: str
    referrer: Optional[str] = None
    detail: str = ""

    @property
    def crawler_effect(self) -> str:
        return CRAWLER_EFFECT[self.issue_type]


class IssueLog:
    """Streams issue rows to CSV and keeps only the counts in memory.

    Safe to call from crawl workers: every write takes the lock.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._counts: Counter = Counter()
        self._lock = threading.Lock()
        self._handle = None
        self._writer = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._handle = open(path, "w", newline="", encoding="utf-8-sig")
            self._writer = csv.DictWriter(self._handle, fieldnames=ISSUE_COLUMNS)
            self._writer.writeheader()
            self._handle.flush()

    def add(self, issue_type: str, url: str, referrer: Optional[str] = None,
            detail: str = "") -> None:
        if issue_type not in CRAWLER_EFFECT:
            raise KeyError(f"unknown issue type {issue_type!r}")
        with self._lock:
            self._counts[issue_type] += 1
            if self._writer is not None:
                self._writer.writerow({
                    "issue_type": issue_type,
                    "url": url,
                    "referrer": referrer or "",
                    "detail": detail,
                    "crawler_effect": CRAWLER_EFFECT[issue_type],
                })
                self._handle.flush()

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
                self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def ai_crawler_groups(robots_text: str):
    """Robots groups naming an AI crawler, as (user_agent, [rule lines]).

    Read straight from the raw file rather than the parsed rules, because the
    interesting part is what the site says to crawlers that are not us.
    """
    groups = []
    current_agents = []
    current_rules = []
    last_was_agent = False

    def flush():
        nonlocal current_agents, current_rules
        for agent in current_agents:
            if any(token in agent.lower() for token in AI_CRAWLER_TOKENS):
                groups.append((agent, list(current_rules)))
        current_agents, current_rules = [], []

    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name == "user-agent":
            if not last_was_agent:
                flush()
            current_agents.append(value)
            last_was_agent = True
            continue
        last_was_agent = False
        if name in _DIRECTIVE_CASING:
            current_rules.append(f"{_DIRECTIVE_CASING[name]}: {value}")
    flush()
    return groups


class PageIssueLog:
    """Streams page-level findings to CSV, counting by type and by severity.

    Same contract as IssueLog: unknown types raise rather than inventing a
    category, and every write takes the lock because crawl workers call it.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._counts: Counter = Counter()
        self._severity_counts: Counter = Counter()
        self._lock = threading.Lock()
        self._handle = None
        self._writer = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._handle = open(path, "w", newline="", encoding="utf-8-sig")
            self._writer = csv.DictWriter(self._handle,
                                          fieldnames=PAGE_ISSUE_COLUMNS)
            self._writer.writeheader()
            self._handle.flush()

    def add(self, issue_type: str, url: str, final_url: Optional[str] = None,
            detail: str = "", site_level: bool = False) -> None:
        severity = PAGE_ISSUE_SEVERITY.get(issue_type)
        if severity is None:
            raise KeyError(f"unknown page issue type {issue_type!r}")
        with self._lock:
            self._counts[issue_type] += 1
            self._severity_counts[severity] += 1
            if self._writer is not None:
                self._writer.writerow({
                    "issue_type": issue_type,
                    "severity": severity,
                    "url": url,
                    "final_url": final_url or "",
                    "detail": detail,
                    "site_level": site_level,
                })
                # Flushed per row so a crash keeps everything already found.
                self._handle.flush()

    def add_many(self, findings, url: str, final_url: Optional[str] = None,
                 site_level: bool = False) -> int:
        """Record a list of (issue_type, detail) pairs for one page."""
        for issue_type, detail in findings:
            self.add(issue_type, url, final_url, detail, site_level)
        return len(findings)

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))

    def severity_counts(self) -> Dict[str, int]:
        with self._lock:
            return {level: self._severity_counts[level]
                    for level in ("high", "medium", "low")
                    if self._severity_counts[level]}

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
                self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
