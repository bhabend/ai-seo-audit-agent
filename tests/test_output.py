"""Output files: columns, encoding, and the render-suspect diagnostic."""

import csv
import json

import pytest
import requests_mock

from conftest import BASE, html_page, make_config, register_site
from seo_audit.config import AuditConfig
from seo_audit.crawl import crawl
from seo_audit.output import CSV_COLUMNS, write_all


def run_crawl(pages, **config_overrides):
    with requests_mock.Mocker() as mock:
        register_site(mock, pages)
        return crawl(make_config(**config_overrides))


def test_csv_and_summary_agree_and_open_as_utf8(tmp_path):
    outcome = run_crawl({
        BASE + "/": html_page(["/naive"]),
        BASE + "/naive": html_page(body="Le café coûte cher — naïve résumé."),
    })
    paths = write_all(outcome, str(tmp_path))

    raw = open(paths["raw_crawl_csv"], "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM, so Excel reads UTF-8

    with open(paths["raw_crawl_csv"], encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    summary = json.load(open(paths["crawl_summary_json"], encoding="utf-8"))

    assert list(rows[0].keys()) == CSV_COLUMNS
    assert len(rows) == summary["pages_found"] == len(outcome.records)
    assert len({r["url"] for r in rows}) == len(rows)


def test_render_suspects_are_counted_with_examples(tmp_path):
    shell = ("<html><body><div id='root'></div>"
             "<script>" + ("x" * 4000) + "</script></body></html>")
    outcome = run_crawl({
        BASE + "/": html_page(["/spa"], body="A" * 800),
        BASE + "/spa": shell,
    })
    summary = json.load(
        open(write_all(outcome, str(tmp_path))["crawl_summary_json"],
             encoding="utf-8"))

    assert summary["render_suspects"]["count"] == 1
    assert summary["render_suspects"]["examples"][0]["url"] == BASE + "/spa"
    assert summary["render_suspects"]["threshold_text_chars"] == 500


def test_render_flag_is_rejected_with_a_clear_message():
    with pytest.raises(NotImplementedError, match="later session"):
        AuditConfig(domain="example.com", render=True)


def test_domain_is_required():
    with pytest.raises(ValueError):
        AuditConfig(domain="")
