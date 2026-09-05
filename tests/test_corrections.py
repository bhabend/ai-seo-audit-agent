"""The session-6 corrections, one fixture per item."""

import os

import pytest
import requests_mock

from conftest import BASE, crawl_site, make_config, pagespeed_payload
from seo_audit.fetch import Fetcher
from seo_audit.issues import PAGE_ISSUE_SEVERITY
from seo_audit.pagespeed import (ATTEMPTS_PER_CALL, CALL_TIMEOUT,
                                 MAX_IN_FLIGHT, ENDPOINT, PageSpeedClient,
                                 issues_for, run_pagespeed)
from seo_audit.scoring import (MIN_PERFORMANCE_COVERAGE, SITE_DEDUCTION_CAP,
                               ScoreBoard, load_weights, score_page,
                               site_deduction)

BODY = " ".join(["content"] * 250)


def page(canonical=None, head="", body=None, links=()):
    anchors = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
    canonical = canonical or BASE + "/"
    return (f"<!doctype html><html lang='en'><head>"
            f"<title>A perfectly reasonable page title</title>"
            f'<meta name="description" content="A description of the page.">'
            f'<meta name="viewport" content="width=device-width">'
            f'<link rel="canonical" href="{canonical}">{head}</head>'
            f"<body><h1>H</h1><p>{body or BODY}</p>"
            f'<script type="application/ld+json">'
            f'{{"@type": "WebPage", "name": "n"}}</script>{anchors}</body></html>')


# --- item 1: the seen-pages guard -------------------------------------------

def test_three_urls_resolving_to_one_page_give_one_audit_row(tmp_path):
    """/x, /x/ and /y all answer as /x/: one page, parsed once."""
    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/x", "X"), ("/y", "Y")]),
        BASE + "/x": {"status_code": 301,
                      "headers": {"Location": BASE + "/x/"}},
        BASE + "/y": {"status_code": 301,
                      "headers": {"Location": BASE + "/x/"}},
        BASE + "/x/": page(canonical=BASE + "/x/"),
    })

    rows = [r for r in run.pages if r["final_url"] == BASE + "/x/"]
    assert len(rows) == 1
    assert run.summary["pages_parsed"] == len(run.pages)

    # The redirects are still recorded in the crawl, so they stay findings.
    raw = {r["url"]: r for r in run.rows}
    assert BASE + "/y" in raw
    assert raw[BASE + "/y"]["final_url"] == BASE + "/x/"
    assert raw[BASE + "/y"]["redirect_duplicate"] == "True"
    assert run.summary["redirect_duplicates"] >= 1


def test_audit_pages_final_url_is_unique_on_every_fixture(tmp_path):
    """The standing guard: one row per final page, whatever the redirects."""
    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/a", "A"), ("/a/", "A slash"),
                                ("/b", "B"), ("/dup", "Dup")]),
        BASE + "/a": {"status_code": 301, "headers": {"Location": BASE + "/a/"}},
        BASE + "/a/": page(canonical=BASE + "/a/"),
        BASE + "/b": page(canonical=BASE + "/b"),
        BASE + "/dup": {"status_code": 302,
                        "headers": {"Location": BASE + "/b"}},
    })

    finals = [r["final_url"] for r in run.pages]
    assert len(finals) == len(set(finals))
    assert run.summary["pages_parsed"] == len(set(finals))


def test_a_duplicate_is_not_scored_or_fingerprinted(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/only", "Only"), ("/alias", "Alias")]),
        BASE + "/only": page(canonical=BASE + "/only"),
        BASE + "/alias": {"status_code": 301,
                          "headers": {"Location": BASE + "/only"}},
    })

    # One score, one fingerprint, one content entry for the one real page.
    rows = [r for r in run.pages if r["final_url"] == BASE + "/only"]
    assert len(rows) == 1
    assert run.summary["score"]["pages_scored"] + \
        run.summary["score"]["noindex_pages"] == len(run.pages)
    assert run.page_issues_of("duplicate_content") == []


def test_a_page_reached_by_one_url_only_is_never_marked_duplicate(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/a", "A")]),
        BASE + "/a": page(canonical=BASE + "/a"),
    })
    assert all(r["redirect_duplicate"] == "False" for r in run.rows)
    assert run.summary["redirect_duplicates"] == 0


# --- item 2: PageSpeed reliability ------------------------------------------

def test_read_timeouts_are_retried_once(monkeypatch):
    import requests
    config = make_config()
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", lambda s: None)

    with requests_mock.Mocker() as mock:
        mock.get(ENDPOINT, [{"exc": requests.exceptions.ReadTimeout},
                            {"status_code": 200, "json": pagespeed_payload()}])
        client = PageSpeedClient(Fetcher(config, delay=0), key="k")
        result = client.measure(BASE + "/", "mobile")

    assert result.error is None
    assert result.performance_score == 92
    assert client.attempts == 2
    assert client.measurements == 1


def test_two_read_timeouts_give_up_and_record_the_reason(monkeypatch):
    import requests
    config = make_config()
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", lambda s: None)

    with requests_mock.Mocker() as mock:
        mock.get(ENDPOINT, exc=requests.exceptions.ReadTimeout)
        client = PageSpeedClient(Fetcher(config, delay=0), key="k")
        result = client.measure(BASE + "/", "mobile")

    assert "ReadTimeout" in result.error
    assert client.attempts == 2
    assert client.measurements == 0


def test_the_attempts_ceiling_is_enforced_not_merely_stated(monkeypatch):
    import requests
    config = make_config()
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", lambda s: None)
    pages = [(BASE + f"/s-{i}/p-{j}", 1, 2) for i in range(20) for j in range(2)]

    with requests_mock.Mocker() as mock:
        # Everything fails, so every call wants its retry.
        mock.get(ENDPOINT, exc=requests.exceptions.ReadTimeout)
        run = run_pagespeed(Fetcher(config, delay=0), pages, BASE + "/",
                            limit=15, key="k")

    ceiling = ATTEMPTS_PER_CALL * len(run.samples) * 2
    assert run.attempts_ceiling == ceiling == 64
    assert run.calls_made <= ceiling
    assert run.measurements == 0
    assert len(run.results) == 32          # still one row per URL per strategy


def test_the_timeout_is_generous_and_calls_run_concurrently():
    assert CALL_TIMEOUT == 150
    assert MAX_IN_FLIGHT == 4


def test_the_summary_separates_attempts_from_measurements(monkeypatch):
    config = make_config()
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", lambda s: None)

    with requests_mock.Mocker() as mock:
        mock.get(ENDPOINT, json=pagespeed_payload())
        run = run_pagespeed(Fetcher(config, delay=0),
                            [(BASE + "/blog/a-post", 1, 2)], BASE + "/",
                            limit=5, key="k")

    summary = run.summary()
    assert summary["pagespeed_attempts"] == 4      # 2 URLs x 2 strategies
    assert summary["pagespeed_calls"] == 4         # all succeeded
    assert summary["pagespeed_attempts_ceiling"] == 8
    assert summary["mobile_measured"] == 2


# --- item 3: the performance gate -------------------------------------------

def board_of(*scores, noindex=()):
    weights = load_weights()
    board = ScoreBoard(weights)
    for index, value in enumerate(scores):
        page_score = score_page([], weights, f"{BASE}/p{index}")
        page_score.score = value
        page_score.noindex = index in noindex
        board.add(page_score)
    return board


def test_performance_is_ignored_when_too_little_of_the_sample_succeeded():
    """One measurement of sixteen moved a site score eight points. No longer."""
    mobile = [36] + [None] * 15
    result = board_of(90, 90, 90).site_score(mobile, sampled_urls=16)

    assert result.performance_included is False
    assert result.site_score == 90
    assert result.performance_coverage == pytest.approx(1 / 16)
    assert "under the 50% needed to count" in result.note
    # The measurement is still reported, just not folded in.
    assert result.performance_score == 36


def test_performance_counts_once_half_the_sample_succeeded():
    mobile = [40] * 8 + [None] * 8
    result = board_of(90, 90).site_score(mobile, sampled_urls=16)

    assert result.performance_included is True
    assert result.performance_coverage == pytest.approx(0.5)
    assert result.site_score == round(0.85 * 90 + 0.15 * 40)
    assert "8 of 16 sampled template(s)" in result.note


def test_the_coverage_threshold_is_half():
    assert MIN_PERFORMANCE_COVERAGE == 0.5


# --- item 4: weights and noindex --------------------------------------------

def test_the_two_retuned_weights():
    weights = load_weights()
    assert weights.points_of("orphan_page") == 5
    assert weights.points_of("thin_page") == 6


def test_noindex_pages_are_excluded_from_the_mean_and_distribution():
    result = board_of(100, 100, 20, noindex=(2,)).site_score(None)

    assert result.noindex_pages == 1
    assert result.pages_scored == 2
    assert result.mean_page_score == 100
    assert sum(result.distribution.values()) == 2
    assert result.noindex_urls == [f"{BASE}/p2"]
    assert "1 noindex page(s) excluded" in result.note


def test_noindex_urls_are_capped_at_twenty():
    result = board_of(*([50] * 25), noindex=tuple(range(25))).site_score(None)
    assert result.noindex_pages == 25
    assert len(result.noindex_urls) == 20
    assert result.pages_scored == 0


def test_a_noindex_page_still_gets_its_own_score(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/hidden", "Hidden")]),
        BASE + "/hidden": page(canonical=BASE + "/hidden",
                               head='<meta name="robots" content="noindex">'),
    })
    row = run.pages_by_url()[BASE + "/hidden"]
    assert int(row["score"]) < 100
    assert run.summary["score"]["noindex_pages"] == 1
    assert BASE + "/hidden" not in [p["final_url"]
                                    for p in run.summary["score"]["lowest_pages"]]


# --- item 5: site-level deduction -------------------------------------------

def test_site_wide_faults_do_not_deduct_from_the_page_that_carried_them():
    weights = load_weights()
    result = score_page(["hsts_missing", "csp_missing",
                         "x_frame_options_missing"], weights)
    assert result.score == 100


def test_site_wide_faults_deduct_from_the_site_score_and_are_capped():
    weights = load_weights()
    points, fired = site_deduction(
        ["hsts_missing", "csp_missing", "x_content_type_options_missing",
         "x_frame_options_missing", "http_to_https_redirect"], weights)
    assert points == SITE_DEDUCTION_CAP
    assert "hsts_missing" in fired and "duplicate_title" not in fired

    board = board_of(90, 90)
    board.note_site_issues(["hsts_missing"])
    result = board.site_score(None)
    assert result.site_level_deduction == 4
    assert result.site_level_types == ["hsts_missing"]
    assert result.site_score == 86
    assert "4 point(s) deducted for site-wide faults" in result.note


def test_group_findings_keep_deducting_from_their_page():
    """scope 'once' is not scope 'site': duplicate_title must still bite."""
    weights = load_weights()
    assert weights.scope_of("duplicate_title") == "once"
    assert score_page(["duplicate_title"], weights).score < 100

    points, fired = site_deduction(["duplicate_title", "broken_internal_link"],
                                   weights)
    assert (points, fired) == (0, [])


def test_a_clean_site_loses_nothing_at_site_level():
    result = board_of(90, 90).site_score(None)
    assert result.site_level_deduction == 0
    assert result.site_level_types == []


# --- item 6: the unmeasured severity ----------------------------------------

def test_the_two_pagespeed_types_are_unmeasured_not_info():
    assert PAGE_ISSUE_SEVERITY["pagespeed_error"] == "unmeasured"
    assert PAGE_ISSUE_SEVERITY["performance_no_field_data"] == "unmeasured"


def test_unmeasured_rows_reach_the_summary_severity_counts(tmp_path,
                                                           monkeypatch):
    monkeypatch.setenv("PAGESPEED_API_KEY", "test-key")

    def extra(mock):
        mock.get(ENDPOINT, json=pagespeed_payload(field_level="none"))

    run = crawl_site(tmp_path, {BASE + "/": page()}, extra=extra,
                     pagespeed=True, pagespeed_templates=2)

    severities = {i["issue_type"]: i["severity"] for i in run.page_issues}
    assert severities.get("performance_no_field_data") == "unmeasured"
    assert run.summary["page_issue_severity"].get("unmeasured", 0) > 0
    assert "info" not in run.summary["page_issue_severity"]


def test_unmeasured_rows_still_score_zero():
    weights = load_weights()
    assert score_page(["pagespeed_error",
                       "performance_no_field_data"], weights).score == 100


# --- item 7: the external check timeout -------------------------------------

def test_external_checks_use_a_short_timeout_and_no_retry(tmp_path):
    """The timeout lives in fetch.Fetcher._request; head() now takes it."""
    seen = {}
    from seo_audit.fetch import Fetcher as RealFetcher
    original = RealFetcher.head

    def spy(self, url, timeout=None, max_retries=None):
        if "other.com" in url or "dead.com" in url:
            seen[url] = (timeout, max_retries)
        return original(self, url, timeout=timeout, max_retries=max_retries)

    RealFetcher.head = spy
    try:
        crawl_site(tmp_path, {
            BASE + "/": page(links=[("https://other.com/", "Out")]),
        })
    finally:
        RealFetcher.head = original

    assert seen
    assert all(t == 5 and r == 0 for t, r in seen.values())


def test_the_sweep_keeps_the_crawl_timeout(tmp_path):
    """Only the external check is impatient; the sitemap sweep is not."""
    seen = []
    from seo_audit.fetch import Fetcher as RealFetcher
    original = RealFetcher.head

    def spy(self, url, timeout=None, max_retries=None):
        if url.startswith(BASE):
            seen.append((timeout, max_retries))
        return original(self, url, timeout=timeout, max_retries=max_retries)

    RealFetcher.head = spy
    try:
        crawl_site(tmp_path, {BASE + "/": page(), BASE + "/swept": page()},
                   sitemaps={BASE + "/sitemap.xml":
                             f'<?xml version="1.0"?><urlset>'
                             f'<url><loc>{BASE}/swept</loc></url></urlset>'},
                   max_pages=1)
    finally:
        RealFetcher.head = original

    assert seen
    assert all(t is None and r is None for t, r in seen)


# --- session 7 item 2: external checks run wide ------------------------------

def test_external_checks_run_in_parallel_but_report_in_priority_order(tmp_path):
    """Eight at a time, yet the most-linked target is still reported first."""
    import threading as _t
    seen_concurrent = []
    live = {"n": 0}
    lock = _t.Lock()

    from seo_audit.fetch import Fetcher as RealFetcher
    original = RealFetcher.head

    def spy(self, url, timeout=None, max_retries=None):
        if "example.com" not in url:
            with lock:
                live["n"] += 1
                seen_concurrent.append(live["n"])
            try:
                return original(self, url, timeout=timeout,
                                max_retries=max_retries)
            finally:
                with lock:
                    live["n"] -= 1
        return original(self, url, timeout=timeout, max_retries=max_retries)

    # dead.com is linked from three pages, the others from one each.
    pages = {BASE + "/": page(links=[("https://dead.com/gone", "Everywhere"),
                                     ("https://other.com/", "Once"),
                                     ("https://partner.com/", "Once"),
                                     ("/a", "A"), ("/b", "B")])}
    for name in ("a", "b"):
        pages[f"{BASE}/{name}"] = page(canonical=f"{BASE}/{name}",
                                       links=[("https://dead.com/gone", "Ev")])

    def extra(mock):
        mock.head("https://dead.com/gone", status_code=404,
                  headers={"Content-Type": "text/html"})

    RealFetcher.head = spy
    try:
        run = crawl_site(tmp_path, pages, extra=extra)
    finally:
        RealFetcher.head = original

    assert max(seen_concurrent) > 1, "external checks ran one at a time"
    # Priority preserved: the 3-referrer target is the reported breakage.
    broken = run.page_issues_of("external_link_broken")
    assert [b["url"] for b in broken] == ["https://dead.com/gone"]
    assert "linked from 3 page(s)" in broken[0]["detail"]


def test_the_external_pool_is_eight_wide():
    from seo_audit.crawl import EXTERNAL_CHECK_WORKERS
    assert EXTERNAL_CHECK_WORKERS == 8


# --- session 7 item 3: PageSpeed stage deadline and streaming ----------------

def test_no_new_call_starts_after_the_stage_deadline(monkeypatch):
    """Two URLs measured, the rest recorded as unattempted, not as healthy."""
    import requests
    config = make_config()
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", lambda s: None)
    clock = [0.0]

    def fake_monotonic():
        # Each reading moves the clock on five minutes, so the 12-minute
        # deadline arrives after the first couple of calls and the rest are
        # recorded as never attempted.
        clock[0] += 300
        return clock[0]

    monkeypatch.setattr("seo_audit.pagespeed.time.monotonic", fake_monotonic)

    pages = [(BASE + f"/s-{i}/p", 1, 2) for i in range(3)]
    with requests_mock.Mocker() as mock:
        mock.get(ENDPOINT, json=pagespeed_payload())
        run = run_pagespeed(Fetcher(config, delay=0), pages, BASE + "/",
                            limit=3, key="k", deadline_seconds=12 * 60)

    assert run.deadline_hit is True
    assert run.stage_seconds > 0
    unattempted = [r for r in run.results if r.error == "stage deadline"]
    assert unattempted, "nothing was recorded as cut off by the deadline"
    # Every sampled URL still has a row for both strategies.
    assert len(run.results) == 2 * len(run.samples)
    assert run.measurements < len(run.results)


def test_a_deadline_row_is_unmeasured_not_a_healthy_page():
    from seo_audit.pagespeed import DEADLINE_REASON, PageSpeedResult
    result = PageSpeedResult(url=BASE + "/", strategy="mobile",
                             error=DEADLINE_REASON)
    result.template, result.group_size = "/x", 5
    found = dict(issues_for(result))
    assert "pagespeed_error" in found
    assert DEADLINE_REASON in found["pagespeed_error"]
    assert PAGE_ISSUE_SEVERITY["pagespeed_error"] == "unmeasured"


def test_pagespeed_rows_are_on_disk_before_the_stage_ends(tmp_path,
                                                          monkeypatch):
    """Lesson 5: a stopped stage must leave its data."""
    monkeypatch.setenv("PAGESPEED_API_KEY", "test-key")
    seen_during = []
    out = tmp_path / "pagespeed.csv"

    def watcher(request, context):
        if out.exists():
            with open(out, encoding="utf-8-sig", newline="") as fh:
                import csv as _csv
                seen_during.append(len(list(_csv.DictReader(fh))))
        return pagespeed_payload()

    def extra(mock):
        mock.get(ENDPOINT, json=watcher)

    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/blog/one", "One"), ("/blog/two", "Two")]),
        BASE + "/blog/one": page(canonical=BASE + "/blog/one"),
        BASE + "/blog/two": page(canonical=BASE + "/blog/two"),
    }, extra=extra, pagespeed=True, pagespeed_templates=3)

    assert len(run.pagespeed) == 2 * run.summary["pagespeed"]["templates_sampled"]
    # Rows were already on disk while later calls were still being made.
    assert max(seen_during) >= 1
    assert run.summary["pagespeed"]["pagespeed_deadline_hit"] is False
    assert run.summary["pagespeed"]["pagespeed_stage_seconds"] >= 0


def test_the_stage_deadline_is_twelve_minutes():
    from seo_audit.pagespeed import STAGE_DEADLINE_SECONDS
    assert STAGE_DEADLINE_SECONDS == 12 * 60
