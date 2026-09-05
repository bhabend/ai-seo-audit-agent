"""Write the two deliverables: raw_crawl.csv and crawl_summary.json."""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from typing import Any, Dict

from .crawl import CrawlOutcome

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
]

# Below this many visible characters a page is almost certainly a JS shell.
RENDER_SUSPECT_THRESHOLD = 500


def write_raw_crawl_csv(outcome: CrawlOutcome, path: str) -> str:
    """One row per URL. utf-8-sig so Excel opens it without mangling accents."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for record in outcome.records:
            writer.writerow({
                "url": record.url,
                "final_url": record.final_url or "",
                "status_code": record.status_code
                if record.status_code is not None else "",
                "redirect_hops": record.redirect_hops,
                "response_time_ms": record.response_time_ms
                if record.response_time_ms is not None else "",
                "content_type": record.content_type or "",
                "in_sitemap": record.in_sitemap,
                "in_crawl": record.in_crawl,
                "depth": record.depth if record.depth is not None else "",
                "discovered_from": record.discovered_from or "",
                "text_chars": record.text_chars
                if record.text_chars is not None else "",
                "error": record.error or "",
            })
    return path


def build_summary(outcome: CrawlOutcome) -> Dict[str, Any]:
    """The run in numbers: what was found, from where, and what looks wrong."""
    records = outcome.records
    robots = outcome.robots
    sitemap = outcome.sitemap

    status_counts = Counter(
        str(r.status_code) if r.status_code is not None else "no_response"
        for r in records
    )
    render_suspects = [
        r for r in records
        if r.text_chars is not None and r.text_chars < RENDER_SUSPECT_THRESHOLD
    ]
    both = sum(1 for r in records if r.in_sitemap and r.in_crawl)
    sitemap_only = sum(1 for r in records if r.in_sitemap and not r.in_crawl)
    crawl_only = sum(1 for r in records if r.in_crawl and not r.in_sitemap)

    return {
        "domain": outcome.config.domain,
        "host": outcome.config.host,
        "started_at": outcome.started_at,
        "run_duration_seconds": outcome.duration_seconds,
        "limits": {
            "max_pages": outcome.config.max_pages,
            "max_depth": outcome.config.max_depth,
            "include_subdomains": outcome.config.include_subdomains,
            "respect_robots": outcome.config.respect_robots,
        },
        "pages_found": len(records),
        "by_source": {
            "in_sitemap_and_crawl": both,
            "sitemap_only": sitemap_only,
            "crawl_only": crawl_only,
        },
        "status_counts": dict(sorted(status_counts.items())),
        "errors": sum(1 for r in records if r.error),
        "non_html": sum(1 for r in records if r.text_chars is None and not r.error),
        "redirected": sum(1 for r in records if r.redirect_hops > 0),
        "render_suspects": {
            "threshold_text_chars": RENDER_SUSPECT_THRESHOLD,
            "count": len(render_suspects),
            "share_of_pages": round(len(render_suspects) / len(records), 3)
            if records else 0.0,
            "examples": [
                {"url": r.url, "text_chars": r.text_chars}
                for r in render_suspects[:5]
            ],
        },
        "robots": {
            "found": bool(robots and robots.found),
            "url": robots.url if robots else None,
            "status_code": robots.status_code if robots else None,
            "disallow_rules": len(robots.disallow) if robots else 0,
            "crawl_delay_declared": robots.crawl_delay if robots else None,
            "sitemap_directives": list(robots.sitemaps) if robots else [],
            "urls_blocked": len(outcome.robots_blocked),
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
        "crawl_delay_applied_seconds": outcome.crawl_delay_applied,
    }


def write_summary_json(outcome: CrawlOutcome, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(build_summary(outcome), handle, indent=2, ensure_ascii=False)
    return path


def write_all(outcome: CrawlOutcome, out_dir: str) -> Dict[str, str]:
    """Write both artefacts into `out_dir` and return their paths."""
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "raw_crawl.csv")
    json_path = os.path.join(out_dir, "crawl_summary.json")
    write_raw_crawl_csv(outcome, csv_path)
    write_summary_json(outcome, json_path)
    return {"raw_crawl_csv": csv_path, "crawl_summary_json": json_path}
