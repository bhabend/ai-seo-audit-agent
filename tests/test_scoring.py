"""Scoring: the weight table, the page score, the site score."""

import json

from conftest import BASE, crawl_site
from seo_audit.issues import CRAWLER_EFFECT, PAGE_ISSUE_SEVERITY
from seo_audit.scoring import (PERFORMANCE_WEIGHT, ScoreBoard, band_of,
                               check_registry_coverage, load_weights,
                               score_page)


def page(body="word " * 250, links=(), head="", canonical=None):
    anchors = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
    canonical = canonical or BASE + "/"
    return (f"<!doctype html><html lang='en'><head>"
            f"<title>A perfectly reasonable page title</title>"
            f'<meta name="description" content="A description of the page.">'
            f'<meta name="viewport" content="width=device-width">'
            f'<link rel="canonical" href="{canonical}">{head}</head>'
            f"<body><h1>H</h1><p>{body}</p>"
            f'<script type="application/ld+json">'
            f'{{"@type": "WebPage", "name": "n"}}</script>{anchors}</body></html>')


# --- the structural guard ---------------------------------------------------

def test_every_registered_issue_type_has_a_weight():
    """A type that cannot be scored is a build failure, not a silent zero."""
    weights = load_weights()
    problems = check_registry_coverage(weights, PAGE_ISSUE_SEVERITY,
                                       CRAWLER_EFFECT)
    assert problems == [], "\n".join(problems)


def test_the_guard_catches_an_unweighted_type():
    weights = load_weights()
    problems = check_registry_coverage(
        weights, list(PAGE_ISSUE_SEVERITY) + ["brand_new_check"],
        CRAWLER_EFFECT)
    assert any("brand_new_check" in p and "no weight entry" in p
               for p in problems)


def test_the_guard_catches_a_weight_for_a_type_nobody_registered():
    weights = load_weights()
    weights.page_issues["ghost_check"] = {"bucket": "on_page", "points": 5}
    problems = check_registry_coverage(weights, PAGE_ISSUE_SEVERITY,
                                       CRAWLER_EFFECT)
    assert any("ghost_check" in p for p in problems)


def test_the_guard_catches_buckets_that_do_not_sum_to_100():
    weights = load_weights()
    weights.buckets["on_page"] = 40
    problems = check_registry_coverage(weights, PAGE_ISSUE_SEVERITY,
                                       CRAWLER_EFFECT)
    assert any("not 100" in p for p in problems)


def test_crawl_issue_types_carry_no_bucket():
    weights = load_weights()
    assert set(weights.crawl_issues) == set(CRAWLER_EFFECT)
    assert all(e["bucket"] is None for e in weights.crawl_issues.values())


def test_buckets_sum_to_one_hundred_on_disk():
    with open("seo_audit/score_weights.json", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert sum(doc["buckets"].values()) == 100
    assert set(doc["buckets"]) == {"indexability", "technical", "on_page",
                                   "content", "schema", "links"}


# --- the page score ---------------------------------------------------------

def test_a_page_with_no_issues_scores_one_hundred():
    result = score_page([], load_weights())
    assert result.score == 100
    assert result.top_issue is None


def test_each_bucket_floors_at_zero():
    """Fourteen on-page faults must not drag the other buckets negative."""
    weights = load_weights()
    on_page = ["title_missing", "meta_description_missing", "h1_missing",
               "title_too_long", "images_missing_alt", "h1_multiple",
               "meta_description_too_long", "duplicate_title",
               "duplicate_meta_description"]
    result = score_page(on_page, weights)

    assert result.buckets["on_page"] == 0
    assert result.score == 100 - weights.buckets["on_page"]
    assert all(v >= 0 for v in result.buckets.values())


def test_deductions_land_in_the_right_bucket():
    weights = load_weights()
    result = score_page(["noindex_page"], weights)
    assert result.buckets["indexability"] == 0      # 25 points, 25 bucket
    assert result.buckets["on_page"] == weights.buckets["on_page"]
    assert result.score == 75


def test_the_top_issue_is_the_heaviest_one():
    result = score_page(["h1_multiple", "noindex_page", "title_too_long"],
                        load_weights())
    assert result.top_issue == "noindex_page"


def test_crawl_layer_types_do_not_score_a_page():
    weights = load_weights()
    assert score_page(["deep_page", "slow_response", "document_linked"],
                      weights).score == 100


def test_performance_types_do_not_deduct_from_the_sampled_page():
    """Only sampled pages carry these rows; deducting would punish sampling."""
    weights = load_weights()
    result = score_page(["performance_poor", "lcp_poor", "cls_poor",
                         "inp_poor"], weights)
    assert result.score == 100


def test_band_boundaries():
    assert band_of(0) == "0-39" and band_of(39) == "0-39"
    assert band_of(40) == "40-59" and band_of(59) == "40-59"
    assert band_of(60) == "60-79" and band_of(79) == "60-79"
    assert band_of(80) == "80-100" and band_of(100) == "80-100"


# --- the site score ---------------------------------------------------------

def board_of(*scores):
    weights = load_weights()
    board = ScoreBoard(weights)
    for index, value in enumerate(scores):
        page_score = score_page([], weights, f"{BASE}/p{index}")
        page_score.score = value
        board.add(page_score)
    return board


def test_site_score_without_pagespeed_is_the_mean_page_score():
    result = board_of(80, 60, 40).site_score(None)
    assert result.mean_page_score == 60
    assert result.site_score == 60
    assert result.performance_included is False
    assert "performance was not included" in result.note


def test_site_score_with_pagespeed_mixes_in_the_mobile_mean():
    # Both sampled URLs measured, so coverage is 100% and the gate opens.
    result = board_of(80, 60, 40).site_score([50, 30])
    expected = round(0.85 * 60 + PERFORMANCE_WEIGHT * 40)
    assert result.performance_score == 40
    assert result.site_score == expected
    assert result.performance_included is True
    assert "15% mean mobile performance" in result.note


def test_missing_scores_in_the_sample_are_ignored_not_counted_as_zero():
    result = board_of(80, 80).site_score([60, None, None])
    # The mean is over what was measured, never over zeros...
    assert result.performance_score == 60
    # ...but one of three is under the coverage gate, so it is reported and
    # not folded into the site score.
    assert result.performance_included is False
    assert result.site_score == 80


def test_an_all_error_pagespeed_sample_falls_back_cleanly():
    result = board_of(70, 70).site_score([None, None])
    assert result.performance_included is False
    assert result.site_score == 70


def test_distribution_counts_every_page_once():
    result = board_of(10, 45, 65, 85, 95).site_score(None)
    assert result.distribution == {"0-39": 1, "40-59": 1,
                                   "60-79": 1, "80-100": 2}
    assert sum(result.distribution.values()) == result.pages_scored


def test_lowest_pages_are_ordered_worst_first_and_capped_at_ten():
    board = board_of(*range(100, 80, -1))
    result = board.site_score(None)
    scores = [p["score"] for p in result.lowest_pages]
    assert len(scores) == 10
    assert scores == sorted(scores)
    assert scores[0] == 81


def test_no_pages_scores_nothing_rather_than_crashing():
    result = ScoreBoard(load_weights()).site_score(None)
    assert result.pages_scored == 0
    assert result.site_score == 0
    assert "nothing to score" in result.note


# --- end to end -------------------------------------------------------------

def test_every_parsed_row_gets_a_score_and_its_buckets(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/a", "A page")]),
        BASE + "/a": page(canonical=BASE + "/a"),
    })

    for row in run.pages:
        assert row["score"] != ""
        assert 0 <= int(row["score"]) <= 100
        for bucket in ("indexability", "technical", "on_page", "content",
                       "schema", "links"):
            assert row[f"score_{bucket}"] != ""

    score = run.summary["score"]
    assert score["pages_scored"] == run.summary["pages_parsed"] == len(run.pages)
    assert sum(score["distribution"].values()) == score["pages_scored"]


def test_a_page_issue_found_after_the_crawl_still_scores(tmp_path):
    """Orphans are found post-crawl; the score must include them."""
    run = crawl_site(tmp_path, {
        BASE + "/": page(links=[("/a", "A page")]),
        BASE + "/a": page(canonical=BASE + "/a"),
        BASE + "/orphan": page(canonical=BASE + "/orphan"),
    }, sitemaps={BASE + "/sitemap.xml":
                 f'<?xml version="1.0"?><urlset>'
                 f'<url><loc>{BASE}/orphan</loc></url></urlset>'})

    rows = run.pages_by_url()
    orphan = rows[BASE + "/orphan"]
    assert "orphan_page" in orphan["issues"]
    assert int(orphan["score"]) < 100
    assert int(orphan["score_links"]) < 10


def test_the_issue_list_on_a_row_matches_its_issue_count(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": page()})
    for row in run.pages:
        listed = [i for i in row["issues"].split(";") if i]
        assert len(listed) <= int(row["issue_count"])
