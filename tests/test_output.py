"""Run artefacts: streaming, encoding, keep-html, and the config guards."""

import csv
import gzip
import hashlib
import os
import threading

import pytest
import requests_mock

from conftest import BASE, crawl_site, html_page, make_config, register_site
from seo_audit.config import AuditConfig
from seo_audit.crawl import run_crawl
from seo_audit.output import CSV_COLUMNS, StreamingCsv


def test_csv_and_summary_agree_and_open_as_utf8(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/naive"]),
        BASE + "/naive": html_page(body="Le café coûte cher — naïve résumé."),
    })

    raw = open(run.outcome.paths["raw_crawl_csv"], "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM, so Excel reads UTF-8

    assert list(run.rows[0].keys()) == CSV_COLUMNS
    assert len(run.rows) == run.summary["pages_found"]
    assert len({r["url"] for r in run.rows}) == len(run.rows)


def test_render_suspects_are_counted_with_examples(tmp_path):
    shell = ("<html><body><div id='root'></div>"
             "<script>" + ("x" * 4000) + "</script></body></html>")
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/spa"], body="A" * 800),
        BASE + "/spa": shell,
    })

    assert run.summary["render_suspects"]["count"] == 1
    assert run.summary["render_suspects"]["examples"][0]["url"] == BASE + "/spa"
    assert [i["url"] for i in run.issues_of("render_suspect")] == [BASE + "/spa"]


def test_rows_are_written_while_the_crawl_is_still_running(tmp_path):
    """Streaming, not a dump at the end: the file grows as pages complete."""
    seen_during_run = []

    def watcher(request, context):
        path = os.path.join(str(tmp_path), "raw_crawl.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig", newline="") as fh:
                seen_during_run.append(len(list(csv.DictReader(fh))))
        context.headers["Content-Type"] = "text/html"
        return html_page([])

    def extra(mock):
        for i in range(4):
            mock.get(BASE + f"/p{i}", text=watcher)

    crawl_site(tmp_path,
               {BASE + "/": html_page([f"/p{i}" for i in range(4)])},
               extra=extra, workers=1)

    # By the time the last page was fetched, earlier rows were already on disk.
    assert max(seen_during_run) >= 3


def test_summary_counts_match_the_issue_rows(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/a", "/b"]),
        BASE + "/a": html_page([]),
        BASE + "/b": html_page([]),
    })

    from collections import Counter
    on_disk = Counter(i["issue_type"] for i in run.issues)
    assert dict(on_disk) == run.summary["crawl_issues"]
    assert sum(on_disk.values()) == run.summary["crawl_issues_total"]


def test_every_issue_row_carries_a_plain_english_effect(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/a"]),
        BASE + "/a": html_page([]),
    })
    assert run.issues
    assert all(i["crawler_effect"].strip() for i in run.issues)


# --- decision D: --keep-html ------------------------------------------------

def test_keep_html_is_off_by_default_and_writes_no_html_dir(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": html_page([])})
    assert not run.has_dir("html")
    assert run.summary["html_files_kept"] == 0
    assert sorted(os.listdir(run.out_dir)) == [
        "audit_pages.csv", "crawl_issues.csv", "crawl_summary.json",
        "findings.json", "page_issues.csv", "raw_crawl.csv",
        "sitemap_sweep.csv"]


def test_keep_html_on_writes_one_gzip_per_page(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/a"]),
        BASE + "/a": html_page([], body="the second page"),
    }, keep_html=True)

    assert run.has_dir("html")
    files = os.listdir(os.path.join(run.out_dir, "html"))
    assert len(files) == 2
    assert run.summary["html_files_kept"] == 2

    name = hashlib.sha1((BASE + "/a").encode("utf-8")).hexdigest() + ".html.gz"
    assert name in files
    with gzip.open(os.path.join(run.out_dir, "html", name), "rt",
                   encoding="utf-8") as fh:
        assert "the second page" in fh.read()


# --- config guards ----------------------------------------------------------

def test_render_flag_is_rejected_with_a_clear_message():
    with pytest.raises(NotImplementedError, match="later session"):
        AuditConfig(domain="example.com", render=True)


def test_domain_is_required():
    with pytest.raises(ValueError):
        AuditConfig(domain="")


def test_sitemap_reserve_is_bounded():
    assert AuditConfig(domain="example.com").sitemap_reserve == 0.25
    assert AuditConfig(domain="example.com", sitemap_reserve=0.0).sitemap_reserve == 0.0
    assert AuditConfig(domain="example.com", sitemap_reserve=0.5).sitemap_reserve == 0.5
    for bad in (-0.1, 0.51, 1.0):
        with pytest.raises(ValueError, match="sitemap_reserve"):
            AuditConfig(domain="example.com", sitemap_reserve=bad)


def test_workers_must_be_at_least_one():
    with pytest.raises(ValueError, match="workers"):
        AuditConfig(domain="example.com", workers=0)


def test_sitemap_reserve_holds_budget_back_for_unlinked_sitemap_urls(tmp_path):
    """With reserve 0 the link crawl takes everything; with 0.5 it does not."""
    sitemap = ("""<?xml version="1.0"?><urlset>"""
               + "".join(f"<url><loc>{BASE}/s{i}</loc></url>" for i in range(4))
               + "</urlset>")
    pages = {BASE + "/": html_page([f"/p{i}" for i in range(6)])}
    for i in range(6):
        pages[BASE + f"/p{i}"] = html_page([])
    for i in range(4):
        pages[BASE + f"/s{i}"] = html_page([])

    greedy = crawl_site(tmp_path / "greedy", pages,
                        sitemaps={BASE + "/sitemap.xml": sitemap},
                        max_pages=4, sitemap_reserve=0.0)
    assert greedy.summary["by_source"]["sitemap_only"] == 0

    reserved = crawl_site(tmp_path / "reserved", pages,
                          sitemaps={BASE + "/sitemap.xml": sitemap},
                          max_pages=4, sitemap_reserve=0.5)
    assert reserved.summary["by_source"]["sitemap_only"] == 2


def test_streaming_csv_ignores_columns_it_was_not_given(tmp_path):
    path = str(tmp_path / "x.csv")
    with StreamingCsv(path, ["a", "b"]) as out:
        out.write({"a": 1, "b": None, "c": "dropped"})
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows == [{"a": "1", "b": ""}]
