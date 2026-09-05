"""Command line entry point: python -m seo_audit.cli --domain example.com"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .config import AuditConfig
from .crawl import crawl
from .output import build_summary, write_all


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo-audit",
        description="Crawl a site and write a raw crawl CSV plus a JSON summary.",
    )
    parser.add_argument("--domain", required=True,
                        help="Site to crawl, e.g. example.com or https://example.com")
    parser.add_argument("--sitemap", default=None,
                        help="Sitemap URL. Optional: robots.txt and the usual "
                             "conventional paths are tried when omitted.")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between requests (default 1.0). A larger "
                             "robots.txt Crawl-delay wins.")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--no-robots", action="store_true",
                        help="Do not fetch or obey robots.txt.")
    parser.add_argument("--include-subdomains", action="store_true",
                        help="Treat other subdomains as part of the site.")
    parser.add_argument("--out", default=None,
                        help="Output directory (default output/<host>/<timestamp>/)")
    return parser


def default_out_dir(host: str) -> str:
    return os.path.join("output", host, time.strftime("%Y%m%d-%H%M%S"))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    config = AuditConfig(
        domain=args.domain,
        sitemap_url=args.sitemap,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        crawl_delay=args.delay,
        timeout=args.timeout,
        respect_robots=not args.no_robots,
        include_subdomains=args.include_subdomains,
    )

    out_dir = args.out or default_out_dir(config.host)
    print(f"Crawling {config.domain} (max {config.max_pages} pages, "
          f"depth {config.max_depth})...", file=sys.stderr)

    outcome = crawl(config)
    paths = write_all(outcome, out_dir)
    summary = build_summary(outcome)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nraw crawl : {paths['raw_crawl_csv']}", file=sys.stderr)
    print(f"summary   : {paths['crawl_summary_json']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
