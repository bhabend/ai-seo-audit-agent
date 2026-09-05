"""Crawl behaviour: caps, robots, source flags, non-HTML."""

import requests_mock

from conftest import BASE, html_page, make_config, register_site
from seo_audit.crawl import crawl
from seo_audit.output import build_summary

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc></url>
  <url><loc>https://example.com/orphan</loc></url>
</urlset>"""


def records_by_url(outcome):
    return {r.url: r for r in outcome.records}


def test_crawl_follows_links_and_records_depth_and_referrer():
    config = make_config()
    with requests_mock.Mocker() as mock:
        register_site(mock, {
            BASE + "/": html_page(["/about", "/pricing"]),
            BASE + "/about": html_page(["/team"]),
            BASE + "/pricing": html_page([]),
            BASE + "/team": html_page([]),
        })
        outcome = crawl(config)

    found = records_by_url(outcome)
    assert set(found) == {BASE + "/", BASE + "/about", BASE + "/pricing",
                          BASE + "/team"}
    assert found[BASE + "/"].depth == 0
    assert found[BASE + "/about"].depth == 1
    assert found[BASE + "/team"].depth == 2
    assert found[BASE + "/team"].discovered_from == BASE + "/about"
    assert all(r.in_crawl for r in outcome.records)


def test_external_and_subdomain_links_are_not_crawled():
    config = make_config()
    with requests_mock.Mocker() as mock:
        register_site(mock, {
            BASE + "/": html_page([
                "/inside",
                "https://other.com/outside",
                "https://blog.example.com/post",
                "https://www.example.com/inside-www",
            ]),
            BASE + "/inside": html_page([]),
            BASE + "/inside-www": html_page([]),
            "https://www.example.com/inside-www": html_page([]),
        })
        outcome = crawl(config)

    urls = set(records_by_url(outcome))
    assert BASE + "/inside" in urls
    # www of our own host counts as us; other hosts do not.
    assert "https://www.example.com/inside-www" in urls
    assert "https://other.com/outside" not in urls
    assert "https://blog.example.com/post" not in urls


def test_max_pages_cap_is_respected():
    config = make_config(max_pages=3)
    pages = {BASE + "/": html_page([f"/p{i}" for i in range(10)])}
    for i in range(10):
        pages[BASE + f"/p{i}"] = html_page([])
    with requests_mock.Mocker() as mock:
        register_site(mock, pages)
        outcome = crawl(config)

    assert len(outcome.records) == 3


def test_max_depth_cap_stops_link_following():
    config = make_config(max_depth=1)
    with requests_mock.Mocker() as mock:
        register_site(mock, {
            BASE + "/": html_page(["/a"]),
            BASE + "/a": html_page(["/b"]),
            BASE + "/b": html_page(["/c"]),
            BASE + "/c": html_page([]),
        })
        outcome = crawl(config)

    urls = set(records_by_url(outcome))
    assert urls == {BASE + "/", BASE + "/a"}


def test_robots_disallow_is_respected_and_can_be_switched_off():
    robots_txt = "User-agent: *\nDisallow: /private/\n"
    pages = {
        BASE + "/": html_page(["/private/secret", "/public"]),
        BASE + "/private/secret": html_page([]),
        BASE + "/public": html_page([]),
    }
    with requests_mock.Mocker() as mock:
        register_site(mock, pages, robots=robots_txt)
        outcome = crawl(make_config())
    urls = set(records_by_url(outcome))
    assert BASE + "/public" in urls
    assert BASE + "/private/secret" not in urls
    assert outcome.robots_blocked == [BASE + "/private/secret"]

    with requests_mock.Mocker() as mock:
        register_site(mock, pages, robots=robots_txt)
        outcome = crawl(make_config(respect_robots=False))
    assert BASE + "/private/secret" in set(records_by_url(outcome))


def test_in_sitemap_and_in_crawl_flags_cover_all_three_cases():
    config = make_config()
    with requests_mock.Mocker() as mock:
        register_site(
            mock,
            {
                BASE + "/": html_page(["/linked"]),
                BASE + "/linked": html_page([]),
                BASE + "/orphan": html_page([]),
            },
            sitemaps={BASE + "/sitemap.xml": SITEMAP_XML},
        )
        outcome = crawl(config)

    found = records_by_url(outcome)
    both = found[BASE + "/"]
    crawl_only = found[BASE + "/linked"]
    sitemap_only = found[BASE + "/orphan"]

    assert (both.in_sitemap, both.in_crawl) == (True, True)
    assert (crawl_only.in_sitemap, crawl_only.in_crawl) == (False, True)
    assert (sitemap_only.in_sitemap, sitemap_only.in_crawl) == (True, False)
    # Never reached by a link, so it has no depth and no referrer.
    assert sitemap_only.depth is None
    assert sitemap_only.discovered_from is None
    assert crawl_only.depth == 1

    summary = build_summary(outcome)
    assert summary["by_source"] == {
        "in_sitemap_and_crawl": 1, "sitemap_only": 1, "crawl_only": 1
    }
    assert summary["sitemap"]["source"] == "guess"


def test_non_html_urls_are_recorded_but_not_parsed_for_links():
    config = make_config()
    with requests_mock.Mocker() as mock:
        register_site(mock, {
            BASE + "/": html_page(["/brochure.pdf", "/logo.png"]),
            BASE + "/brochure.pdf": {
                "content": b"%PDF-1.4 <a href='/hidden'>",
                "headers": {"Content-Type": "application/pdf"},
            },
            BASE + "/logo.png": {
                "content": b"\x89PNG\r\n",
                "headers": {"Content-Type": "image/png"},
            },
            BASE + "/hidden": html_page([]),
        })
        outcome = crawl(config)

    found = records_by_url(outcome)
    assert found[BASE + "/brochure.pdf"].status_code == 200
    assert found[BASE + "/brochure.pdf"].text_chars is None
    # The href inside the PDF bytes must not become a crawl target.
    assert BASE + "/hidden" not in found


def test_duplicate_urls_collapse_to_one_row():
    config = make_config()
    with requests_mock.Mocker() as mock:
        register_site(mock, {
            BASE + "/": html_page([
                "/about", "/about#team", "/about?utm_source=nav",
                "https://EXAMPLE.com/about",
            ]),
            BASE + "/about": html_page([]),
        })
        outcome = crawl(config)

    urls = [r.url for r in outcome.records]
    assert len(urls) == len(set(urls))
    assert sorted(urls) == [BASE + "/", BASE + "/about"]


def test_error_pages_are_recorded_with_their_status():
    config = make_config()
    with requests_mock.Mocker() as mock:
        register_site(mock, {
            BASE + "/": html_page(["/gone", "/broken"]),
            BASE + "/gone": {"status_code": 404,
                             "headers": {"Content-Type": "text/html"},
                             "text": "not found"},
            BASE + "/broken": {"status_code": 500,
                               "headers": {"Content-Type": "text/html"},
                               "text": "boom"},
        })
        outcome = crawl(config)

    summary = build_summary(outcome)
    assert summary["status_counts"] == {"200": 1, "404": 1, "500": 1}
    assert summary["pages_found"] == 3
