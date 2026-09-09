"""A response the server refused is not a page.

The case this file exists for: an audit of a site behind an edge network that
answers every request with HTTP 403 and a short "Access Denied" body. The run
recorded that body as the homepage, drew a render_suspect from its length,
ran the site level header checks against the error response's headers, and
presented a score of zero as though something had been measured.

Nothing here is site specific. The fixture body is the shape of the refusal,
not the shape of one company's edge network.
"""

import csv
import json
import os

import pytest

from conftest import BASE, crawl_site, html_page
from seo_audit.crawl import is_blocked_status
from seo_audit.findings import build_findings
from seo_audit.report import (build_short_charts, expected_headings_for_short,
                              generate_short, validate_short)

# The body the real site returned, 372 bytes of it, reference line included.
ACCESS_DENIED = (
    "<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>\n"
    "<H1>Access Denied</H1>\n \nYou don't have permission to access "
    "&#34;http&#58;&#47;&#47;example&#46;com&#47;&#34; on this server.<P>\n"
    "Reference&#32;&#35;18&#46;b20e0317&#46;1788946408&#46;2ff95345\n"
    "<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;"
    "b20e0317&#46;1788946408&#46;2ff95345</P>\n</BODY>\n</HTML>\n")

REFUSAL = {"status_code": 403, "text": ACCESS_DENIED,
           "headers": {"Content-Type": "text/html"}}


def refused_site(tmp_path, **kwargs):
    """A site where every address, homepage included, is refused."""
    def extra(mock):
        mock.get(BASE + "/", **REFUSAL)
        mock.head(BASE + "/", **REFUSAL)

    return crawl_site(tmp_path, {BASE + "/": REFUSAL}, extra=extra, **kwargs)


# --- what counts as a refusal ------------------------------------------------

def test_the_statuses_a_server_refuses_with():
    for status in (401, 403, 429, 500, 503):
        assert is_blocked_status(status), status
    for status in (200, 301, 404, 410):
        assert not is_blocked_status(status), status
    assert not is_blocked_status(None), "no status is a transport failure"


# --- nothing is taken from a refused body ------------------------------------

def test_a_refused_response_is_never_parsed_or_scored(tmp_path):
    run = refused_site(tmp_path)

    assert run.summary["status_counts"] == {"403": 1}
    assert run.summary["pages_parsed"] == 0
    assert run.pages == [], "an error page was recorded as an audited page"
    assert run.page_issues == [], run.page_issues


def test_no_finding_is_drawn_from_the_error_page(tmp_path):
    """render_suspect said the site had almost no text. It said it of this."""
    run = refused_site(tmp_path)
    types = run.issue_types()

    assert "render_suspect" not in types, run.issues
    assert "crawl_only_page" not in types
    assert "deep_page" not in types
    assert "non_html_linked" not in types


def test_the_refusal_itself_is_recorded_with_its_status(tmp_path):
    """A refusal has its own type: "nothing answered" is a different fact."""
    run = refused_site(tmp_path)
    refusals = run.issues_of("request_refused")

    assert run.issues_of("fetch_error") == []

    assert [row["url"] for row in refusals] == [BASE + "/"]
    assert "403" in refusals[0]["detail"]
    assert "refused" in refusals[0]["detail"]
    assert "refused the request" in refusals[0]["crawler_effect"] \
        or "refused" in refusals[0]["crawler_effect"]


def test_the_run_counts_what_was_refused(tmp_path):
    run = refused_site(tmp_path)
    blocked = run.summary["blocked"]

    assert blocked["responses"] == 1
    assert blocked["statuses"] == {"403": 1}


def test_the_site_level_checks_do_not_read_an_error_page(tmp_path):
    """Four missing headers, every one of them the edge network's, not the
    client's. A refused homepage is not evidence about the site."""
    run = refused_site(tmp_path)
    site_level = [row for row in run.page_issues
                  if row["site_level"] == "True"]

    assert site_level == [], site_level


# --- a run that read nothing has no score ------------------------------------

def test_findings_say_the_site_could_not_be_read(tmp_path):
    run = refused_site(tmp_path)
    findings = build_findings(run.out_dir, compare_enabled=False)
    headline = findings["headline"]

    assert headline["readable"] is False
    assert headline["site_score"] is None, "a score was reported anyway"
    assert headline["mean_page_score"] is None
    assert headline["blocked_responses"] == 1
    assert "403" in headline["unreadable_note"]
    assert "nothing to score" in headline["unreadable_note"]


def test_the_crawlability_section_counts_the_refusals(tmp_path):
    run = refused_site(tmp_path)
    findings = build_findings(run.out_dir, compare_enabled=False)
    blocked = findings["crawlability"]["blocked"]

    assert blocked["count"] == 1
    assert blocked["whole"] >= 1
    assert blocked["whole_is"] == "addresses requested"
    assert blocked["statuses"] == {"403": 1}


def test_the_report_leads_with_the_refusal_and_shows_no_score(tmp_path):
    from docx import Document

    run = refused_site(tmp_path)
    findings = build_findings(run.out_dir, compare_enabled=False)
    result = generate_short(run.out_dir)
    document = Document(result["docx"])

    headings = [p.text.strip() for p in document.paragraphs
                if p.style.name.startswith("Heading")
                or p.style.name == "Title"]
    assert "The site could not be read" in headings, headings
    assert "Score" not in headings, "a score section survived"

    texts = " ".join(p.text for p in document.paragraphs)
    assert "403" in texts
    assert "nothing to score" in texts
    assert "out of 100" not in texts, "a score was printed anyway"

    # And the document still validates as a deliverable.
    assert validate_short(result["docx"],
                          expected_headings_for_short(findings),
                          len(build_short_charts(findings))) == []


def test_the_refusal_reaches_the_crawlability_table(tmp_path):
    from seo_audit import report

    run = refused_site(tmp_path)
    findings = build_findings(run.out_dir, compare_enabled=False)
    coverage = (findings.get("meta") or {}).get("coverage") or {}
    rows = report.short_section_rows("crawlability",
                                     findings["crawlability"], coverage)

    refused = [row for row in rows
               if row[0] == "Addresses the server refused"]
    assert refused, rows
    assert " of " in refused[0][1], refused
    assert refused[0][2] == "high"
    assert "403" in refused[0][3]


# --- a site that answers is untouched by any of this -------------------------

def test_a_site_that_answers_is_scored_exactly_as_before(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": html_page(["/a"]),
                                BASE + "/a": html_page()})
    findings = build_findings(run.out_dir, compare_enabled=False)

    assert run.summary["pages_parsed"] == 2
    assert run.summary["blocked"]["responses"] == 0
    assert findings["headline"]["readable"] is True
    assert isinstance(findings["headline"]["site_score"], (int, float))
    assert findings["crawlability"]["blocked"]["count"] == 0


def test_one_refused_page_among_many_leaves_the_score_alone(tmp_path):
    """Partial blocking states its count and changes nothing else."""
    def extra(mock):
        mock.get(BASE + "/locked", **REFUSAL)
        mock.head(BASE + "/locked", **REFUSAL)

    run = crawl_site(tmp_path, {BASE + "/": html_page(["/a", "/locked"]),
                                BASE + "/a": html_page()}, extra=extra)
    findings = build_findings(run.out_dir, compare_enabled=False)
    headline = findings["headline"]

    assert run.summary["pages_parsed"] == 2, "the refusal was parsed"
    assert run.summary["blocked"]["responses"] == 1
    assert headline["readable"] is True
    assert headline["site_score"] is not None
    assert findings["crawlability"]["blocked"]["count"] == 1
