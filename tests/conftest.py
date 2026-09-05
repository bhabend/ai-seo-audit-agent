"""Shared fixtures. Every test here is offline: nothing leaves the process."""

import pytest

from seo_audit.config import AuditConfig

BASE = "https://example.com"


def make_config(**overrides) -> AuditConfig:
    """An AuditConfig with test-friendly defaults (no delay, small caps)."""
    kwargs = dict(
        domain=BASE,
        crawl_delay=0.0,
        max_pages=20,
        max_depth=5,
        timeout=5,
    )
    kwargs.update(overrides)
    return AuditConfig(**kwargs)


def html_page(links=(), body="Some ordinary body copy for this test page."):
    """Minimal HTML with the given hrefs."""
    anchors = "".join(f'<a href="{href}">link</a>' for href in links)
    return (
        "<!doctype html><html><head><title>t</title></head>"
        f"<body><p>{body}</p>{anchors}</body></html>"
    )


def register_site(mock, pages, robots=None, sitemaps=None):
    """Wire a fake site into requests_mock.

    `pages` maps absolute URL -> html string (or a dict of kwargs for the mock).
    robots.txt and the conventional sitemap paths 404 unless supplied.
    """
    mock.get(f"{BASE}/robots.txt",
             text=robots if robots is not None else "",
             status_code=200 if robots is not None else 404)

    sitemaps = sitemaps or {}
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        url = BASE + path
        if url not in sitemaps:
            mock.get(url, status_code=404)
    for url, xml in sitemaps.items():
        mock.get(url, text=xml,
                 headers={"Content-Type": "application/xml"})

    for url, page in pages.items():
        if isinstance(page, dict):
            mock.get(url, **page)
        else:
            mock.get(url, text=page,
                     headers={"Content-Type": "text/html; charset=utf-8"})


@pytest.fixture
def base():
    return BASE
