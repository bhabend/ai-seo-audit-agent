"""PageSpeed on a template sample: grouping, budget, retries, skipping."""

import requests_mock

from conftest import BASE, crawl_site, make_config, pagespeed_payload
from seo_audit.fetch import Fetcher
from seo_audit.pagespeed import (ENDPOINT, PageSpeedClient, choose_templates,
                                 issues_for, parse_response, run_pagespeed,
                                 template_of)


def page(body="word " * 250, links=(), title="A perfectly good page title"):
    anchors = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
    return (f"<!doctype html><html lang='en'><head><title>{title}</title>"
            f'<meta name="description" content="A description of the page.">'
            f'<meta name="viewport" content="width=device-width">'
            f'<link rel="canonical" href="{BASE}/"></head>'
            f"<body><h1>H</h1><p>{body}</p>{anchors}</body></html>")


# --- template grouping ------------------------------------------------------

def test_template_collapses_slugs_and_numbers():
    assert template_of(BASE + "/blog/post-one") == \
        template_of(BASE + "/blog/post-two")
    assert template_of(BASE + "/products/12345") == \
        template_of(BASE + "/products/67890")
    # Different sections stay different templates.
    assert template_of(BASE + "/blog/x-y") != template_of(BASE + "/pricing/x-y")
    assert template_of(BASE + "/") == "/"


def test_choose_templates_picks_the_best_linked_page_per_group():
    pages = [
        (BASE + "/blog/post-one", 3, 2),
        (BASE + "/blog/post-two", 11, 2),   # best linked in its group
        (BASE + "/blog/post-three", 7, 2),
        (BASE + "/pricing/plans", 2, 2),
    ]
    samples = choose_templates(pages, limit=5, homepage=BASE + "/")

    assert samples[0].url == BASE + "/"
    assert samples[0].template == "/ (homepage)"
    blog = [s for s in samples if "blog" in s.template][0]
    assert blog.url == BASE + "/blog/post-two"
    assert blog.group_size == 3


def test_choose_templates_takes_the_biggest_groups_first():
    pages = ([(BASE + f"/big/p-{i}", 1, 2) for i in range(10)]
             + [(BASE + "/small/p-a", 1, 2)])
    samples = choose_templates(pages, limit=1, homepage=None)
    assert len(samples) == 1
    assert samples[0].group_size == 10


def test_the_homepage_is_never_counted_inside_another_group():
    pages = [(BASE + "/", 50, 0), (BASE + "/blog/a-post", 1, 2)]
    samples = choose_templates(pages, limit=5, homepage=BASE + "/")
    assert sum(1 for s in samples if s.url == BASE + "/") == 1


# --- response parsing -------------------------------------------------------

def test_parse_response_extracts_lab_and_field_metrics():
    result = parse_response(pagespeed_payload(score=0.42, lcp=5200.0,
                                              cls=0.31, inp=620.0),
                            BASE + "/", "mobile")
    assert result.performance_score == 42
    assert result.lab_lcp_ms == 5200.0
    assert result.lab_cls == 0.31
    assert result.field_data_level == "url"
    assert result.field_inp_ms == 620.0
    # CrUX reports CLS x100; we store it on the real scale.
    assert result.field_cls == 0.31


def test_origin_level_field_data_is_labelled_as_such():
    result = parse_response(pagespeed_payload(field_level="origin"),
                            BASE + "/", "mobile")
    assert result.field_data_level == "origin"


def test_absent_field_data_is_none_not_zero():
    result = parse_response(pagespeed_payload(field_level="none"),
                            BASE + "/", "mobile")
    assert result.field_data_level == "none"
    assert result.field_inp_ms is None


def test_issues_name_the_template_and_its_size():
    result = parse_response(pagespeed_payload(score=0.34, lcp=5200.0,
                                              cls=0.31, inp=620.0),
                            BASE + "/products/x", "mobile")
    result.template, result.group_size = "/products", 212
    found = dict(issues_for(result))

    assert "performance_poor" in found
    assert "/products template (212 page(s)), mobile" in found["performance_poor"]
    assert "34 of 100" in found["performance_poor"]
    assert "lcp_poor" in found and "cls_poor" in found and "inp_poor" in found


def test_a_healthy_page_raises_only_the_field_data_note_when_absent():
    good = parse_response(pagespeed_payload(score=0.95, lcp=1200.0, cls=0.01,
                                            inp=90.0), BASE + "/", "mobile")
    assert issues_for(good) == []

    nofield = parse_response(pagespeed_payload(score=0.95, lcp=1200.0,
                                               cls=0.01, field_level="none"),
                             BASE + "/", "mobile")
    assert [t for t, _d in issues_for(nofield)] == ["performance_no_field_data"]


def test_inp_is_only_judged_when_real_user_data_exists():
    result = parse_response(pagespeed_payload(score=0.9, inp=900.0,
                                              field_level="none"),
                            BASE + "/", "mobile")
    assert "inp_poor" not in dict(issues_for(result))


# --- calls, retries and the budget -----------------------------------------

def test_a_429_is_retried_once_then_succeeds(monkeypatch):
    config = make_config()
    slept = []
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", slept.append)

    with requests_mock.Mocker() as mock:
        mock.get(ENDPOINT, [{"status_code": 429, "json": {}},
                            {"status_code": 200, "json": pagespeed_payload()}])
        client = PageSpeedClient(Fetcher(config, delay=0), key="test-key",
                                 retry_after=10)
        result = client.measure(BASE + "/", "mobile")

    assert result.error is None
    assert result.performance_score == 92
    assert client.calls_made == 2
    assert slept == [10]


def test_two_server_errors_give_a_pagespeed_error_row(monkeypatch):
    config = make_config()
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", lambda s: None)

    with requests_mock.Mocker() as mock:
        mock.get(ENDPOINT, [{"status_code": 503, "json": {}},
                            {"status_code": 503, "json": {}}])
        client = PageSpeedClient(Fetcher(config, delay=0), key="test-key")
        result = client.measure(BASE + "/", "mobile")

    assert result.error == "HTTP 503"
    assert client.calls_made == 2
    assert [t for t, _d in issues_for(result)] == ["pagespeed_error"]


def test_the_call_budget_is_two_per_sampled_url(monkeypatch):
    """The cost ceiling: 2 x (templates + homepage), whatever the site shape."""
    config = make_config()
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", lambda s: None)
    pages = [(BASE + f"/section-{i}/page-{j}", 1, 2)
             for i in range(30) for j in range(5)]

    with requests_mock.Mocker() as mock:
        mock.get(ENDPOINT, json=pagespeed_payload())
        run = run_pagespeed(Fetcher(config, delay=0), pages, BASE + "/",
                            limit=15, key="test-key")

    assert len(run.samples) == 16          # 15 templates plus the homepage
    # calls_made counts attempts. All 32 succeed here so there are no
    # retries; the enforced ceiling is twice this (see test_corrections).
    assert run.calls_made == 32
    assert run.attempts_ceiling == 64
    assert run.measurements == 32
    assert len(run.results) == 32
    # 30 sections exist but only 15 are sampled, so the run represents
    # 15 x 5 pages plus the homepage -- a subset, and the summary says so.
    assert run.pages_represented == 76


def test_the_key_never_appears_in_any_recorded_value(monkeypatch):
    config = make_config()
    monkeypatch.setattr("seo_audit.pagespeed.time.sleep", lambda s: None)

    with requests_mock.Mocker() as mock:
        mock.get(ENDPOINT, json=pagespeed_payload())
        run = run_pagespeed(Fetcher(config, delay=0), [(BASE + "/a", 1, 1)],
                            BASE + "/", limit=2, key="super-secret-key")

    blob = repr([r.as_row() for r in run.results]) + repr(run.summary())
    assert "super-secret-key" not in blob


# --- skipping ---------------------------------------------------------------

def test_a_missing_key_skips_the_stage_and_says_why(monkeypatch):
    monkeypatch.delenv("PAGESPEED_API_KEY", raising=False)
    run = run_pagespeed(None, [(BASE + "/a", 1, 1)], BASE + "/")

    assert run.skipped is True
    assert "PAGESPEED_API_KEY" in run.skip_reason
    assert run.calls_made == 0
    assert run.results == []


def test_an_empty_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("PAGESPEED_API_KEY", "   ")
    run = run_pagespeed(None, [(BASE + "/a", 1, 1)], BASE + "/")
    assert run.skipped is True


def test_no_pagespeed_flag_skips_without_looking_for_a_key(monkeypatch):
    monkeypatch.setenv("PAGESPEED_API_KEY", "a-real-key")
    run = run_pagespeed(None, [(BASE + "/a", 1, 1)], BASE + "/", enabled=False)
    assert run.skipped is True
    assert "--no-pagespeed" in run.skip_reason


# --- end to end through a crawl --------------------------------------------

def test_pagespeed_csv_is_the_seventh_file_with_two_rows_per_url(tmp_path,
                                                                 monkeypatch):
    import os
    # The key comes from the environment, exactly as it does in a real run.
    # Nothing here reads .env: load_dotenv is only called by the CLI.
    monkeypatch.setenv("PAGESPEED_API_KEY", "test-key")
    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/blog/post-one", "One"),
                                ("/blog/post-two", "Two")]),
        BASE + "/blog/post-one": page(),
        BASE + "/blog/post-two": page(),
    }, pagespeed=True, pagespeed_templates=5)

    files = sorted(os.listdir(run.out_dir))
    assert "pagespeed.csv" in files
    assert len(files) == 7

    sampled = {r["url"] for r in run.pagespeed}
    assert len(run.pagespeed) == 2 * len(sampled)
    assert {r["strategy"] for r in run.pagespeed} == {"mobile", "desktop"}

    ps = run.summary["pagespeed"]
    assert ps["skipped"] is False
    assert ps["pagespeed_attempts"] == len(run.pagespeed)
    assert ps["pagespeed_calls"] == len(run.pagespeed)
    assert ps["pagespeed_attempts_ceiling"] == 2 * len(run.pagespeed)
    assert ps["mean_mobile_score"] == 92


def test_a_skipped_stage_leaves_six_files_and_an_explained_summary(tmp_path):
    import os
    run = crawl_site(tmp_path, {BASE + "/": page()}, pagespeed=False)

    assert len(os.listdir(run.out_dir)) == 6
    assert "pagespeed.csv" not in os.listdir(run.out_dir)
    ps = run.summary["pagespeed"]
    assert ps["skipped"] is True
    assert ps["calls_made"] == 0
    assert run.summary["score"]["performance_included"] is False
