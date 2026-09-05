"""robots.txt and sitemap discovery (lesson 5 from the v0 review)."""

import gzip

import requests_mock

from conftest import BASE, make_config
from seo_audit.discovery import (discover_sitemaps, effective_crawl_delay,
                                 fetch_robots, parse_robots, parse_sitemap)
from seo_audit.fetch import Fetcher

INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-posts.xml.gz</loc></sitemap>
</sitemapindex>"""

PAGES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b?utm_source=news</loc></url>
</urlset>"""

POSTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/post-1</loc></url>
</urlset>"""


def test_parse_sitemap_separates_index_children_from_pages():
    pages, children = parse_sitemap(INDEX_XML, BASE + "/sitemap_index.xml")
    assert pages == []
    assert children == ["https://example.com/sitemap-pages.xml",
                        "https://example.com/sitemap-posts.xml.gz"]

    pages, children = parse_sitemap(PAGES_XML, BASE + "/sitemap-pages.xml")
    assert children == []
    assert pages == ["https://example.com/a",
                     "https://example.com/b?utm_source=news"]


def test_sitemap_index_recursion_including_gzip():
    config = make_config()
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/robots.txt", status_code=404)
        mock.get(BASE + "/sitemap.xml", status_code=404)
        mock.get(BASE + "/sitemap_index.xml", text=INDEX_XML)
        mock.get(BASE + "/sitemap-pages.xml", text=PAGES_XML)
        mock.get(BASE + "/sitemap-posts.xml.gz",
                 content=gzip.compress(POSTS_XML.encode("utf-8")))

        fetcher = Fetcher(config)
        robots = fetch_robots(fetcher, config)
        result = discover_sitemaps(fetcher, config, robots)

    assert result.source_of_sitemap == "guess"
    # Nested sitemaps were followed and the gzipped one was decompressed.
    assert result.urls == ["https://example.com/a",
                           "https://example.com/b",  # utm stripped by normalize
                           "https://example.com/post-1"]
    assert BASE + "/sitemap-posts.xml.gz" in result.sitemaps_used


def test_robots_sitemap_directive_is_found_and_used():
    config = make_config()
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /private/\n"
        "Sitemap: https://example.com/custom-sitemap.xml\n"
    )
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/robots.txt", text=robots_txt)
        mock.get(BASE + "/custom-sitemap.xml", text=PAGES_XML)

        fetcher = Fetcher(config)
        robots = fetch_robots(fetcher, config)
        result = discover_sitemaps(fetcher, config, robots)

    assert robots.sitemaps == ["https://example.com/custom-sitemap.xml"]
    assert result.source_of_sitemap == "robots"
    assert result.sitemaps_used == ["https://example.com/custom-sitemap.xml"]
    assert "https://example.com/a" in result.urls


def test_configured_sitemap_wins_over_robots_and_conventions():
    config = make_config(sitemap_url=BASE + "/given.xml")
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/robots.txt", status_code=404)
        mock.get(BASE + "/given.xml", text=POSTS_XML)

        fetcher = Fetcher(config)
        robots = fetch_robots(fetcher, config)
        result = discover_sitemaps(fetcher, config, robots)

    assert result.source_of_sitemap == "config"
    assert result.urls == ["https://example.com/post-1"]


def test_missing_robots_is_not_an_error_and_allows_everything():
    config = make_config()
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/robots.txt", status_code=404)
        robots = fetch_robots(Fetcher(config), config)
    assert robots.found is False
    assert robots.error is None
    assert robots.is_allowed(BASE + "/anything")


def test_robots_rules_pick_our_group_and_honour_allow_overrides():
    robots_txt = (
        "User-agent: BadBot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: *\n"
        "Disallow: /private/\n"
        "Allow: /private/public-page\n"
        "Disallow: /*.pdf$\n"
        "Crawl-delay: 3\n"
    )
    info = parse_robots(robots_txt, "seo-audit-bot/0.1")
    assert info.crawl_delay == 3.0
    assert not info.is_allowed(BASE + "/private/secret")
    assert info.is_allowed(BASE + "/private/public-page")
    assert not info.is_allowed(BASE + "/files/report.pdf")
    assert info.is_allowed(BASE + "/files/report.pdf.html")
    assert info.is_allowed(BASE + "/about")


def test_named_group_beats_wildcard_group():
    robots_txt = (
        "User-agent: *\n"
        "Disallow: /\n"
        "\n"
        "User-agent: seo-audit-bot\n"
        "Disallow: /admin/\n"
    )
    info = parse_robots(robots_txt, "seo-audit-bot/0.1 (+https://example.org)")
    assert info.is_allowed(BASE + "/about")
    assert not info.is_allowed(BASE + "/admin/x")


def test_crawl_delay_only_overrides_config_when_larger():
    config = make_config(crawl_delay=2.0)
    slow = parse_robots("User-agent: *\nCrawl-delay: 5\n", config.user_agent)
    fast = parse_robots("User-agent: *\nCrawl-delay: 0.5\n", config.user_agent)
    assert effective_crawl_delay(config, slow) == 5.0
    assert effective_crawl_delay(config, fast) == 2.0
