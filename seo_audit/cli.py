"""Command line entry point: python -m seo_audit.cli --domain example.com"""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from .config import AuditConfig
from .crawl import run_crawl
from .output import build_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo-audit",
        description="Crawl a site and write the raw crawl, the sitemap sweep, "
                    "the crawlability log and a JSON summary.",
    )
    parser.add_argument("--domain", required=True,
                        help="Site to crawl, e.g. example.com or https://example.com")
    parser.add_argument("--sitemap", default=None,
                        help="Sitemap URL. Optional: robots.txt and the usual "
                             "conventional paths are tried when omitted.")
    parser.add_argument("--max-pages", type=int, default=2000,
                        help="Pages to fetch and parse (default 2000). Every "
                             "sitemap URL beyond this is still status-checked.")
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel fetchers (default 4). Each one waits "
                             "--delay between its own requests.")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between requests per worker (default "
                             "1.0). A larger robots.txt Crawl-delay wins.")
    parser.add_argument("--sitemap-reserve", type=float, default=0.25,
                        help="Share of the page budget held back for sitemap "
                             "URLs no link reached, 0 to 0.5 (default 0.25).")
    parser.add_argument("--sweep-limit", type=int, default=10000,
                        help="Most sitemap URLs the sweep will status-check "
                             "(default 10000). Beyond this the sweep stops "
                             "and the summary says it was capped.")
    parser.add_argument("--sweep-delay", type=float, default=0.25,
                        help="Per-worker delay for sweep requests only "
                             "(default 0.25). HEAD is cheap; pages are not.")
    parser.add_argument("--canonical-check-limit", type=int, default=500,
                        help="Most canonical targets outside the crawl that "
                             "will be checked with HEAD (default 200). The "
                             "rest are reported as unchecked.")
    parser.add_argument("--external-check-limit", type=int, default=1000,
                        help="Most distinct external link targets to check "
                             "with HEAD (default 1000), most-linked first. "
                             "The rest are counted as unchecked, never "
                             "assumed working.")
    parser.add_argument("--write-links", action="store_true",
                        help="Also write links.csv (source, target, anchor, "
                             "nofollow). Off by default: the edge list is "
                             "far larger than the page list.")
    parser.add_argument("--pagespeed-templates", type=int, default=15,
                        help="Page templates to sample with PageSpeed "
                             "Insights (default 15). The homepage is always "
                             "included, and each sampled URL costs two calls "
                             "(mobile and desktop), so the default is at most "
                             "32 calls. Needs PAGESPEED_API_KEY in .env.")
    parser.add_argument("--no-pagespeed", action="store_true",
                        help="Skip the performance sample entirely.")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--no-robots", action="store_true",
                        help="Do not fetch or obey robots.txt.")
    parser.add_argument("--include-subdomains", action="store_true",
                        help="Treat other subdomains as part of the site.")
    parser.add_argument("--keep-html", action="store_true",
                        help="Also store each page's HTML gzipped under "
                             "html/. Off by default: runs stay small.")
    parser.add_argument("--out", default=None,
                        help="Output directory (default output/<host>/<timestamp>/)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Keys come from the environment only, never from the command line.
    load_dotenv()

    config = AuditConfig(
        domain=args.domain,
        sitemap_url=args.sitemap,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        workers=args.workers,
        crawl_delay=args.delay,
        sitemap_reserve=args.sitemap_reserve,
        sweep_limit=args.sweep_limit,
        sweep_delay=args.sweep_delay,
        canonical_check_limit=args.canonical_check_limit,
        external_check_limit=args.external_check_limit,
        write_links=args.write_links,
        pagespeed_templates=args.pagespeed_templates,
        pagespeed=not args.no_pagespeed,
        timeout=args.timeout,
        respect_robots=not args.no_robots,
        include_subdomains=args.include_subdomains,
        keep_html=args.keep_html,
    )

    print(f"Crawling {config.domain} (max {config.max_pages} pages, "
          f"depth {config.max_depth}, {config.workers} workers)...",
          file=sys.stderr)

    outcome = run_crawl(config, out_dir=args.out)
    print(json.dumps(build_summary(outcome), indent=2, ensure_ascii=False))

    for label, key in (("raw crawl    ", "raw_crawl_csv"),
                       ("audit pages  ", "audit_pages_csv"),
                       ("page issues  ", "page_issues_csv"),
                       ("pagespeed   ", "pagespeed_csv"),
                       ("sitemap sweep", "sitemap_sweep_csv"),
                       ("crawl issues ", "crawl_issues_csv"),
                       ("summary      ", "crawl_summary_json")):
        if key in outcome.paths:
            print(f"{label}: {outcome.paths[key]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
