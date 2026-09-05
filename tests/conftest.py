"""Shared fixtures. Every test here is offline: nothing leaves the process."""

import csv
import json
import os

import pytest
import requests_mock

from seo_audit.config import AuditConfig
from seo_audit.crawl import run_crawl

BASE = "https://example.com"

# External hosts the fixtures link to. Registered before any test runs.
EXTERNAL_HOSTS = (
    "https://other.com/",
    "https://other.com/outside",
    "https://elsewhere.com/",
    "https://elsewhere.com/x",
    "https://partner.com/",
    "https://dead.com/gone",
    "https://blog.example.com/post",
    "https://aaa-rare.com/",
)

# A minimal but realistically shaped PageSpeed response.
def pagespeed_payload(score=0.92, lcp=2200.0, cls=0.04, tbt=120.0,
                      field_level="url", inp=180.0):
    payload = {
        "lighthouseResult": {
            "fetchTime": "2026-09-05T12:00:00.000Z",
            "categories": {"performance": {"score": score}},
            "audits": {
                "largest-contentful-paint": {"numericValue": lcp},
                "cumulative-layout-shift": {"numericValue": cls},
                "total-blocking-time": {"numericValue": tbt},
                "first-contentful-paint": {"numericValue": 900.0},
                "speed-index": {"numericValue": 1800.0},
            },
        }
    }
    metrics = {
        "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": lcp},
        "INTERACTION_TO_NEXT_PAINT": {"percentile": inp},
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": cls * 100},
    }
    if field_level == "url":
        payload["loadingExperience"] = {"metrics": metrics}
    elif field_level == "origin":
        payload["originLoadingExperience"] = {"metrics": metrics}
    return payload


def make_config(**overrides) -> AuditConfig:
    """An AuditConfig with test-friendly defaults (no delay, small caps)."""
    kwargs = dict(
        domain=BASE,
        crawl_delay=0.0,
        max_pages=20,
        max_depth=5,
        workers=2,
        timeout=5,
        pagespeed=False,
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
    # Lesson 4/7: every request path the new code can take is registered up
    # front, so a missing mock never masquerades as a code failure. That now
    # includes the PageSpeed endpoint, which the scoring stage calls.
    from seo_audit.pagespeed import ENDPOINT
    mock.get(ENDPOINT, json=pagespeed_payload())

    # The external-link check HEADs whatever hosts a fixture links to.
    for host in EXTERNAL_HOSTS:
        mock.head(host, status_code=200,
                  headers={"Content-Type": "text/html"})
        mock.get(host, status_code=200,
                 headers={"Content-Type": "text/html"}, text="external")

    # The site-level check asks whether plain http redirects to https.
    # Registered first so a test's own `extra` can override it.
    mock.head("http://example.com/", status_code=301,
              headers={"Location": BASE + "/"})
    mock.get("http://example.com/", status_code=301,
             headers={"Location": BASE + "/"})

    mock.get(f"{BASE}/robots.txt",
             text=robots if robots is not None else "",
             status_code=200 if robots is not None else 404)

    sitemaps = sitemaps or {}
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        url = BASE + path
        if url not in sitemaps:
            mock.get(url, status_code=404)
    for url, xml in sitemaps.items():
        mock.get(url, text=xml, headers={"Content-Type": "application/xml"})

    for url, page in pages.items():
        if isinstance(page, dict):
            mock.get(url, **page)
            # The sitemap sweep asks with HEAD: same status, same headers.
            head_kwargs = {k: v for k, v in page.items()
                           if k in ("status_code", "headers")}
            mock.head(url, **head_kwargs)
        else:
            html_headers = {"Content-Type": "text/html; charset=utf-8"}
            mock.get(url, text=page, headers=html_headers)
            mock.head(url, headers=html_headers)


def read_rows(path):
    """Read a run CSV back the way the deliverable is actually consumed."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class Run:
    """A finished crawl plus its files, read back from disk."""

    def __init__(self, outcome, out_dir):
        self.outcome = outcome
        self.out_dir = str(out_dir)
        self.rows = read_rows(outcome.paths["raw_crawl_csv"])
        self.issues = read_rows(outcome.paths["crawl_issues_csv"])
        self.sweep = read_rows(outcome.paths["sitemap_sweep_csv"])
        self.pages = read_rows(outcome.paths["audit_pages_csv"])
        self.links = (read_rows(outcome.paths["links_csv"])
                      if "links_csv" in outcome.paths else [])
        self.pagespeed = (read_rows(outcome.paths["pagespeed_csv"])
                          if "pagespeed_csv" in outcome.paths else [])
        self.page_issues = read_rows(outcome.paths["page_issues_csv"])
        with open(outcome.paths["crawl_summary_json"], encoding="utf-8") as fh:
            self.summary = json.load(fh)

    def by_url(self):
        return {r["url"]: r for r in self.rows}

    def issues_of(self, issue_type):
        return [i for i in self.issues if i["issue_type"] == issue_type]

    def issue_types(self):
        return {i["issue_type"] for i in self.issues}

    def pages_by_url(self):
        """Audit rows keyed on final_url: the page that answered."""
        return {p["final_url"]: p for p in self.pages}

    def page_issues_of(self, issue_type):
        return [i for i in self.page_issues if i["issue_type"] == issue_type]

    def page_issue_types(self):
        return {i["issue_type"] for i in self.page_issues}

    def has_dir(self, name):
        return os.path.isdir(os.path.join(self.out_dir, name))


def crawl_site(tmp_path, pages, robots=None, sitemaps=None, extra=None,
               **config_overrides) -> Run:
    """Register a fake site, crawl it into tmp_path, read the files back."""
    config = make_config(**config_overrides)
    with requests_mock.Mocker() as mock:
        register_site(mock, pages, robots=robots, sitemaps=sitemaps)
        if extra is not None:
            extra(mock)
        outcome = run_crawl(config, out_dir=str(tmp_path))
    return Run(outcome, tmp_path)


@pytest.fixture
def base():
    return BASE
