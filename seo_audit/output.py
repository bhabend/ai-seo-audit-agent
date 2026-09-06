"""Run artefacts: streaming CSVs plus the summary JSON.

Rows are written as they complete, not gathered and dumped at the end, so
memory stays flat on a 2,000-page crawl and a crash leaves the rows that had
already finished. Nothing but CSV and JSON lands in a run folder unless
--keep-html is asked for.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
from collections import Counter
from typing import Any, Dict, List, Optional

CSV_COLUMNS = [
    "url",
    "final_url",
    "status_code",
    "redirect_hops",
    "response_time_ms",
    "content_type",
    "in_sitemap",
    "in_crawl",
    "depth",
    "discovered_from",
    "text_chars",
    "error",
    "redirect_duplicate",
]

SWEEP_COLUMNS = [
    "url",
    "status_code",
    "final_url",
    "redirect_hops",
    "blocked_by_robots",
    "in_crawl",
    "error",
]

AUDIT_PAGE_COLUMNS = [
    "url",
    "final_url",
    "status_code",
    "title",
    "title_length",
    "meta_description",
    "meta_description_length",
    "meta_robots",
    "x_robots_tag",
    "canonical",
    "canonical_is_self",
    "hreflang_count",
    "hreflang",
    "viewport",
    "html_lang",
    "h1_count",
    "h1",
    "h2_count",
    "h3_count",
    "word_count",
    "image_count",
    "images_missing_alt",
    "images_empty_alt",
    "internal_links",
    "external_links",
    "nofollow_internal_links",
    "mixed_content_count",
    "schema_types",
    "schema_block_count",
    "schema_invalid_count",
    "microdata_types",
    "has_microdata",
    "inlinks",
    "nofollow_inlinks",
    "outlinks_internal",
    "outlinks_external",
    "anchor_texts",
    "content_simhash",
    "content_md5",
    "in_sitemap",
    "depth",
    "score",
    "score_indexability",
    "score_technical",
    "score_on_page",
    "score_content",
    "score_schema",
    "score_links",
    "issue_count",
    "issues",
]

# Below this many visible characters a page is almost certainly a JS shell.
RENDER_SUSPECT_THRESHOLD = 500


class StreamingCsv:
    """A CSV that is open for the length of the run and flushed per row."""

    def __init__(self, path: str, columns: List[str]):
        self.path = path
        self.columns = columns
        self.rows_written = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._handle = open(path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._handle, fieldnames=columns,
                                      extrasaction="ignore")
        self._writer.writeheader()
        self._handle.flush()

    def write(self, row: Dict[str, Any]) -> None:
        self._writer.writerow({c: _cell(row.get(c)) for c in self.columns})
        self.rows_written += 1
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _cell(value: Any) -> Any:
    """Empty string for missing values; everything else as-is."""
    return "" if value is None else value


class CrawlStats:
    """Running counters, so the summary never needs the rows back."""

    def __init__(self) -> None:
        self.pages = 0
        self.pages_parsed = 0
        self.schema_type_counts: Counter = Counter()
        self.status_counts: Counter = Counter()
        self.depth_counts: Counter = Counter()
        self.errors = 0
        self.non_html = 0
        self.redirected = 0
        self.render_suspects = 0
        self.both = 0
        self.sitemap_only = 0
        self.crawl_only = 0
        self.render_examples: List[Dict[str, Any]] = []
        self.max_depth_reached = -1

    def add(self, row: Dict[str, Any]) -> None:
        self.pages += 1
        status = row.get("status_code")
        self.status_counts[str(status) if status is not None else "no_response"] += 1
        depth = row.get("depth")
        self.depth_counts[depth if depth is not None else "sitemap_only"] += 1
        if isinstance(depth, int):
            self.max_depth_reached = max(self.max_depth_reached, depth)
        if row.get("error"):
            self.errors += 1
        elif row.get("text_chars") is None:
            self.non_html += 1
        if row.get("redirect_hops"):
            self.redirected += 1

        text_chars = row.get("text_chars")
        if text_chars is not None and text_chars < RENDER_SUSPECT_THRESHOLD:
            self.render_suspects += 1
            if len(self.render_examples) < 5:
                self.render_examples.append(
                    {"url": row["url"], "text_chars": text_chars})

        in_sitemap, in_crawl = row.get("in_sitemap"), row.get("in_crawl")
        if in_sitemap and in_crawl:
            self.both += 1
        elif in_sitemap:
            self.sitemap_only += 1
        elif in_crawl:
            self.crawl_only += 1


class HtmlStore:
    """Gzipped page HTML, written only when --keep-html is passed."""

    def __init__(self, run_dir: str, enabled: bool):
        self.enabled = enabled
        self.dir = os.path.join(run_dir, "html")
        self.files_written = 0
        if enabled:
            os.makedirs(self.dir, exist_ok=True)

    def save(self, url: str, html: Optional[str]) -> None:
        if not self.enabled or not html:
            return
        name = hashlib.sha1(url.encode("utf-8")).hexdigest() + ".html.gz"
        with gzip.open(os.path.join(self.dir, name), "wt",
                       encoding="utf-8") as handle:
            handle.write(html)
        self.files_written += 1


def build_summary(outcome) -> Dict[str, Any]:
    """The run in numbers: what was found, from where, and what got in the way."""
    stats = outcome.stats
    robots = outcome.robots
    sitemap = outcome.sitemap
    config = outcome.config

    return {
        "domain": config.domain,
        "host": config.host,
        "configured_host": config.configured_host,
        "host_adopted": config.host_was_adopted,
        "started_at": outcome.started_at,
        "run_duration_seconds": outcome.duration_seconds,
        "limits": {
            "max_pages": config.max_pages,
            "max_depth": config.max_depth,
            "workers": config.workers,
            "sitemap_reserve": config.sitemap_reserve,
            "include_subdomains": config.include_subdomains,
            "respect_robots": config.respect_robots,
            "keep_html": config.keep_html,
            "pagespeed_templates": config.pagespeed_templates,
        },
        "pages_found": stats.pages,
        "by_source": {
            "in_sitemap_and_crawl": stats.both,
            "sitemap_only": stats.sitemap_only,
            "crawl_only": stats.crawl_only,
        },
        "status_counts": dict(sorted(stats.status_counts.items())),
        "depth_counts": {str(k): v for k, v in sorted(
            stats.depth_counts.items(), key=lambda kv: str(kv[0]))},
        "max_depth_reached": (stats.max_depth_reached
                              if stats.max_depth_reached >= 0 else None),
        "errors": stats.errors,
        "non_html": stats.non_html,
        "redirected": stats.redirected,
        "render_suspects": {
            "threshold_text_chars": RENDER_SUSPECT_THRESHOLD,
            "count": stats.render_suspects,
            "share_of_pages": round(stats.render_suspects / stats.pages, 3)
            if stats.pages else 0.0,
            "examples": stats.render_examples,
        },
        "pages_parsed": stats.pages_parsed,
        "stage_seconds": outcome.stage_seconds,
        "findings_written": outcome.findings_written,
        "redirect_duplicates": outcome.redirect_duplicates,
        "canonical_targets_unchecked": outcome.canonical_targets_unchecked,
        "canonical_check_limit": config.canonical_check_limit,
        "page_issue_counts": outcome.page_issue_counts,
        "page_issue_severity": outcome.page_issue_severity,
        "page_issues_total": sum(outcome.page_issue_counts.values()),
        "schema_type_counts": dict(sorted(
            stats.schema_type_counts.items(),
            key=lambda kv: (-kv[1], kv[0]))),
        "robots": {
            "found": bool(robots and robots.found),
            "url": robots.url if robots else None,
            # robots.txt is read before the homepage tells us which host the
            # site really serves, so this may be the pre-adoption hostname.
            "fetched_from_host": outcome.config.configured_host,
            "status_code": robots.status_code if robots else None,
            "disallow_rules": len(robots.disallow) if robots else 0,
            "crawl_delay_declared": robots.crawl_delay if robots else None,
            "sitemap_directives": list(robots.sitemaps) if robots else [],
            "urls_blocked": outcome.robots_blocked_count,
        },
        "sitemap": {
            "source": sitemap.source_of_sitemap if sitemap else None,
            "sitemaps_used": list(sitemap.sitemaps_used) if sitemap else [],
            "sitemaps_failed": [
                {"url": u, "reason": why}
                for u, why in (sitemap.sitemaps_failed if sitemap else [])
            ],
            "urls_in_sitemap": len(sitemap.urls) if sitemap else 0,
        },
        "sitemap_sweep": {
            "sitemap_urls_total": outcome.sitemap_urls_total,
            "sitemap_urls_swept": outcome.sweep_checked,
            "sweep_capped": outcome.sweep_capped,
            "sweep_limit": config.sweep_limit,
            "sweep_throttled": outcome.sweep_throttled,
            "sweep_throttled_responses": outcome.sweep_throttled_responses,
            "checked": outcome.sweep_checked,
            "already_crawled": outcome.sweep_already_crawled,
            "blocked_by_robots": outcome.sweep_blocked,
            "status_counts": dict(sorted(outcome.sweep_status_counts.items())),
        },
        "links": outcome.link_stats,
        "content": outcome.content_stats,
        "pagespeed": outcome.pagespeed_stats,
        "score": outcome.score_stats,
        "crawl_issues": outcome.issue_counts,
        "crawl_issues_total": sum(outcome.issue_counts.values()),
        "crawl_delay_applied_seconds": outcome.crawl_delay_applied,
        "html_files_kept": outcome.html_files_kept,
    }


def write_summary_json(outcome, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(build_summary(outcome), handle, indent=2, ensure_ascii=False)
    return path
