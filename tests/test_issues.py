"""The crawlability log: one fixture per issue type, and the AI-crawler scan."""

import pytest
import requests

from conftest import BASE, crawl_site, html_page
from seo_audit.issues import (CRAWLER_EFFECT, IssueLog, ai_crawler_groups)

ROBOTS_WITH_AI = """
User-agent: *
Disallow: /checkout/

User-agent: GPTBot
Disallow: /

User-agent: ClaudeBot
Disallow: /private/
Crawl-delay: 5

User-agent: Googlebot
Disallow:
"""


def test_unknown_issue_type_is_rejected(tmp_path):
    log = IssueLog(str(tmp_path / "issues.csv"))
    with pytest.raises(KeyError):
        log.add("not_a_real_type", BASE + "/")
    log.close()


def test_every_issue_type_has_a_plain_english_effect():
    assert CRAWLER_EFFECT
    for issue_type, effect in CRAWLER_EFFECT.items():
        assert effect.endswith("."), issue_type
        assert len(effect.split()) >= 6, issue_type


def test_ai_crawler_groups_are_picked_out_of_robots():
    groups = dict(ai_crawler_groups(ROBOTS_WITH_AI))
    assert set(groups) == {"GPTBot", "ClaudeBot"}
    assert groups["GPTBot"] == ["Disallow: /"]
    assert "Crawl-delay: 5" in groups["ClaudeBot"]
    # Googlebot is a search crawler, not an AI one: not reported here.
    assert "Googlebot" not in groups


def test_ai_crawler_rules_reach_the_crawl_log(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": html_page([])},
                     robots=ROBOTS_WITH_AI)
    rules = run.issues_of("ai_crawler_rule")
    assert {r["detail"].split(":")[0] for r in rules} == {"GPTBot", "ClaudeBot"}
    assert all("AI assistants" in r["crawler_effect"] for r in rules)


def test_multiple_sitemaps_are_all_reported(tmp_path):
    index = """<?xml version="1.0"?>
    <sitemapindex>
      <sitemap><loc>https://example.com/sm-pages.xml</loc></sitemap>
      <sitemap><loc>https://example.com/sm-empty.xml</loc></sitemap>
    </sitemapindex>"""
    pages_xml = """<?xml version="1.0"?>
    <urlset><url><loc>https://example.com/a</loc></url></urlset>"""

    run = crawl_site(
        tmp_path,
        {BASE + "/": html_page([]), BASE + "/a": html_page([])},
        robots="User-agent: *\nSitemap: https://example.com/sm-index.xml\n",
        sitemaps={BASE + "/sm-index.xml": index,
                  BASE + "/sm-pages.xml": pages_xml,
                  BASE + "/sm-empty.xml": "<?xml version='1.0'?><urlset></urlset>"},
    )

    reported = {i["url"]: i["detail"] for i in run.issues_of("multiple_sitemaps")}
    assert set(reported) == {BASE + "/sm-index.xml", BASE + "/sm-pages.xml",
                             BASE + "/sm-empty.xml"}
    assert "found via robots" in reported[BASE + "/sm-index.xml"]
    assert "found via sitemap_index" in reported[BASE + "/sm-pages.xml"]
    assert "not used" in reported[BASE + "/sm-empty.xml"]


def test_a_single_sitemap_is_not_reported_as_multiple(tmp_path):
    run = crawl_site(
        tmp_path, {BASE + "/": html_page([])},
        sitemaps={BASE + "/sitemap.xml":
                  """<?xml version="1.0"?><urlset>
                     <url><loc>https://example.com/</loc></url></urlset>"""},
    )
    assert run.issues_of("multiple_sitemaps") == []


def test_fetch_error_after_retries_is_reported(tmp_path):
    def extra(mock):
        mock.get(BASE + "/down", exc=requests.exceptions.ConnectionError)

    run = crawl_site(tmp_path, {BASE + "/": html_page(["/down"])},
                     extra=extra)

    errors = run.issues_of("fetch_error")
    assert [i["url"] for i in errors] == [BASE + "/down"]
    assert run.summary["errors"] == 1


def test_redirect_loop_is_reported(tmp_path):
    def extra(mock):
        mock.get(BASE + "/loop", status_code=302,
                 headers={"Location": BASE + "/loop2"})
        mock.get(BASE + "/loop2", status_code=302,
                 headers={"Location": BASE + "/loop"})

    run = crawl_site(tmp_path, {BASE + "/": html_page(["/loop"])},
                     extra=extra)

    assert [i["url"] for i in run.issues_of("redirect_loop")] == [BASE + "/loop"]


def test_issue_counts_survive_parallel_workers(tmp_path):
    """Four workers writing the log at once still produce one row per event."""
    pages = {BASE + "/": html_page([f"/p{i}" for i in range(12)])}
    for i in range(12):
        pages[BASE + f"/p{i}"] = html_page([])

    run = crawl_site(tmp_path, pages, workers=4)

    assert len(run.rows) == 13
    assert len(run.issues_of("crawl_only_page")) == 13
    from collections import Counter
    assert dict(Counter(i["issue_type"] for i in run.issues)) == \
        run.summary["crawl_issues"]
