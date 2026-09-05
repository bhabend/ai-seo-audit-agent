"""The CLI wiring: flags reach the config, artefacts land, summary prints."""

import json
import os

import requests_mock

from conftest import BASE, html_page, register_site
from seo_audit.cli import build_parser, main


def test_flags_reach_the_parser():
    args = build_parser().parse_args(["--domain", "example.com"])
    assert args.max_pages == 2000
    assert args.workers == 4
    assert args.sitemap_reserve == 0.25
    assert args.keep_html is False
    assert args.sweep_limit == 10000
    assert args.sweep_delay == 0.25
    assert args.canonical_check_limit == 500
    assert args.external_check_limit == 1000
    assert args.pagespeed_templates == 15
    assert args.no_pagespeed is False
    assert args.write_links is False


def test_cli_writes_every_artefact_and_prints_the_summary(tmp_path, capsys):
    with requests_mock.Mocker() as mock:
        register_site(mock, {
            BASE + "/": html_page(["/about"]),
            BASE + "/about": html_page([]),
        })
        # --no-pagespeed keeps the test off the key and the API entirely.
        code = main(["--domain", "example.com", "--out", str(tmp_path),
                     "--delay", "0", "--workers", "2", "--no-pagespeed"])

    assert code == 0
    assert sorted(os.listdir(str(tmp_path))) == [
        "audit_pages.csv", "crawl_issues.csv", "crawl_summary.json",
        "page_issues.csv", "raw_crawl.csv", "sitemap_sweep.csv"]

    summary = json.loads(capsys.readouterr().out)
    assert summary["pages_found"] == 2
    assert summary["limits"]["workers"] == 2
