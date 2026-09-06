"""findings.json: grouping, share objects, prioritised fixes, comparison."""

import csv
import json
import os

import pytest

from seo_audit.findings import (MAX_EVIDENCE, MAX_FIXES, build_findings,
                                compare, pattern_of, previous_run_dir, share)

HOST = "https://example.com"


# --- a synthetic run folder --------------------------------------------------

def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def make_run(tmp_path, name="20260101-000000", *, page_issues=None,
             pages=None, summary_extra=None, pagespeed=None,
             crawl_issues=None):
    """A complete seven-file run folder, minimal but realistically shaped."""
    run = tmp_path / "example.com" / name
    run.mkdir(parents=True)

    pages = pages if pages is not None else [
        {"url": f"{HOST}/p{i}", "final_url": f"{HOST}/p{i}",
         "status_code": "200", "word_count": str(100 + i * 50),
         "schema_block_count": "1" if i % 2 else "0",
         "has_microdata": "True" if i == 1 else "False",
         "canonical": f"{HOST}/p{i}", "issues": "", "issue_count": "0",
         "score": "90"}
        for i in range(4)
    ]
    page_issues = page_issues if page_issues is not None else []
    crawl_issues = crawl_issues if crawl_issues is not None else []
    pagespeed = pagespeed if pagespeed is not None else []

    write_csv(run / "audit_pages.csv",
              ["url", "final_url", "status_code", "word_count",
               "schema_block_count", "has_microdata", "canonical", "issues",
               "issue_count", "score"], pages)
    write_csv(run / "page_issues.csv",
              ["issue_type", "severity", "url", "final_url", "detail",
               "site_level"], page_issues)
    write_csv(run / "crawl_issues.csv",
              ["issue_type", "url", "referrer", "detail", "crawler_effect"],
              crawl_issues)
    write_csv(run / "raw_crawl.csv",
              ["url", "final_url", "status_code", "redirect_duplicate"],
              [{"url": p["url"], "final_url": p["final_url"],
                "status_code": "200", "redirect_duplicate": "False"}
               for p in pages])
    write_csv(run / "sitemap_sweep.csv",
              ["url", "status_code", "final_url", "redirect_hops",
               "blocked_by_robots", "in_crawl", "error"], [])
    write_csv(run / "pagespeed.csv",
              ["url", "template", "group_size", "strategy",
               "performance_score", "lab_lcp_ms", "lab_cls", "lab_tbt_ms",
               "lab_fcp_ms", "lab_speed_index_ms", "field_lcp_ms",
               "field_inp_ms", "field_cls", "field_data_level", "fetch_time",
               "error"], pagespeed)

    summary = {
        "domain": HOST, "host": "example.com",
        "started_at": "2026-01-01T00:00:00", "run_duration_seconds": 12.0,
        "limits": {"max_pages": 2000, "max_depth": 5, "workers": 4,
                   "pagespeed_templates": 15},
        "pages_found": len(pages), "pages_parsed": len(pages),
        "redirect_duplicates": 0, "canonical_check_limit": 500,
        "canonical_targets_unchecked": 0,
        "errors": 0, "redirected": 0,
        "render_suspects": {"count": 0},
        "page_issue_counts": {}, "page_issue_severity": {},
        "page_issues_total": len(page_issues),
        "schema_type_counts": {"WebPage": 2},
        "robots": {"found": True, "fetched_from_host": "example.com",
                   "disallow_rules": 1, "urls_blocked": 0,
                   "sitemap_directives": []},
        "sitemap": {"source": "robots", "sitemaps_used": [],
                    "urls_in_sitemap": 10},
        "sitemap_sweep": {"sitemap_urls_total": 10, "sitemap_urls_swept": 2,
                          "sweep_capped": False, "sweep_limit": 10000,
                          "sweep_throttled": False, "already_crawled": 8},
        "links": {"pages_with_zero_inlinks": 1, "orphan_pages_in_sitemap": 1,
                  "broken_internal_targets": 0,
                  "redirected_internal_targets": 0,
                  "external_targets_found": 10, "external_checked": 10,
                  "external_broken": 1, "external_unchecked": 0,
                  "external_check_limit": 1000, "edges": 40},
        "content": {"duplicate_groups": 0, "near_duplicate_groups": 0,
                    "pages_compared": 2, "min_words_for_comparison": 200},
        "pagespeed": {"skipped": not pagespeed, "templates_sampled": 1,
                      "pages_represented": 2, "mean_mobile_score": 50.0,
                      "mean_desktop_score": 70.0,
                      "pagespeed_attempts": 2, "pagespeed_attempts_ceiling": 4,
                      "pagespeed_calls": 2, "pagespeed_deadline_hit": False,
                      "pagespeed_stage_seconds": 3.0, "errors": 0,
                      "worst_template_mobile": None},
        "score": {"site_score": 80, "mean_page_score": 85.0,
                  "performance_component": 50.0, "performance_included": True,
                  "performance_measured": 1, "performance_sampled": 1,
                  "site_level_deduction": 0, "site_level_types": [],
                  "pages_scored": len(pages), "noindex_pages": 0,
                  "noindex_urls": [],
                  "distribution": {"0-39": 0, "40-59": 0, "60-79": 0,
                                   "80-100": len(pages)},
                  "lowest_pages": [], "note": "x"},
        "crawl_issues": {}, "crawl_issues_total": len(crawl_issues),
        "stage_seconds": {"crawl": 3.0},
    }
    if summary_extra:
        summary.update(summary_extra)
    (run / "crawl_summary.json").write_text(
        json.dumps(summary), encoding="utf-8")
    return str(run)


# --- patterns and grouping ---------------------------------------------------

def test_pattern_merges_siblings_but_keeps_sections_apart():
    a = pattern_of(f"{HOST}/on-demand/pune/?area=wakad")
    b = pattern_of(f"{HOST}/on-demand/noida/?area=sector-62")
    c = pattern_of(f"{HOST}/blogs/some-post")
    assert a == b, "sibling faceted URLs must be one pattern"
    assert a != c
    # The query parameter name is part of the pattern, its value is not.
    assert "area" in a


def test_pattern_separates_different_parameters():
    assert pattern_of(f"{HOST}/a/x?area=1") != pattern_of(f"{HOST}/a/x?mlg=0")
    assert pattern_of(f"{HOST}/a/x?area=1") == pattern_of(f"{HOST}/a/y?area=2")


def test_broken_links_group_by_root_cause(tmp_path):
    rows = [{"issue_type": "broken_internal_link", "severity": "high",
             "url": f"{HOST}/on-demand/{city}/?area=x", "final_url": "",
             "detail": "HTTP 404, linked from 500 page(s): a, b",
             "site_level": "False"}
            for city in ("pune", "noida", "mumbai")]
    rows.append({"issue_type": "broken_internal_link", "severity": "high",
                 "url": f"{HOST}/other/thing", "final_url": "",
                 "detail": "HTTP 404, linked from 2 page(s): a",
                 "site_level": "False"})

    run = make_run(tmp_path, page_issues=rows)
    findings = build_findings(run, compare_enabled=False)
    broken = findings["links"]["broken_internal"]

    assert broken["targets"] == 4
    assert broken["group_count"] == 2, "three sibling 404s are one root cause"
    biggest = broken["groups"][0]
    assert biggest["targets"] == 3
    assert biggest["link_instances"] == 1500
    assert len(biggest["evidence"]) <= MAX_EVIDENCE


def test_hreflang_groups_by_the_parameter_that_causes_it(tmp_path):
    rows = [{"issue_type": "hreflang_not_reciprocal", "severity": "medium",
             "url": f"{HOST}/en/?mlg=0", "final_url": f"{HOST}/en/?mlg=0",
             "detail": "points to x", "site_level": "False"},
            {"issue_type": "hreflang_not_reciprocal", "severity": "medium",
             "url": f"{HOST}/fr/?mlg=0", "final_url": f"{HOST}/fr/?mlg=0",
             "detail": "points to y", "site_level": "False"}]
    run = make_run(tmp_path, page_issues=rows)
    findings = build_findings(run, compare_enabled=False)

    block = findings["indexability_technical"]["hreflang_not_reciprocal"]
    assert block["group_count"] == 1
    assert "mlg" in block["groups"][0]["pattern"]
    assert block["groups"][0]["pages_affected"] == 2


def test_h1_multiple_is_presented_by_template(tmp_path):
    rows = [{"issue_type": "h1_multiple", "severity": "low",
             "url": f"{HOST}/blogs/post-{i}",
             "final_url": f"{HOST}/blogs/post-{i}",
             "detail": "3 h1 elements", "site_level": "False"}
            for i in range(4)]
    run = make_run(tmp_path, page_issues=rows)
    findings = build_findings(run, compare_enabled=False)

    h1 = findings["on_page"]["h1"]
    assert h1["template_count"] == 1
    assert h1["multiple_by_template"][0]["pages_affected"] == 4


# --- share objects and bounds ------------------------------------------------

def iter_shares(node, path=""):
    if isinstance(node, dict):
        if {"count", "whole", "whole_is"} <= set(node):
            yield path, node
        for key, value in node.items():
            yield from iter_shares(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_shares(value, f"{path}[{index}]")


def test_every_share_states_its_whole_and_never_exceeds_it(tmp_path):
    rows = [{"issue_type": "title_missing", "severity": "high",
             "url": f"{HOST}/p{i}", "final_url": f"{HOST}/p{i}",
             "detail": "no title", "site_level": "False"} for i in range(3)]
    run = make_run(tmp_path, page_issues=rows)
    findings = build_findings(run, compare_enabled=False)

    found = list(iter_shares(findings))
    assert found, "no share objects at all"
    for path, obj in found:
        assert obj["whole_is"], f"{path} has no whole_is"
        assert obj["count"] <= obj["whole"], (
            f"{path}: {obj['count']} of {obj['whole']}")


def test_every_section_is_present(tmp_path):
    findings = build_findings(make_run(tmp_path), compare_enabled=False)
    for section in ("meta", "headline", "crawlability",
                    "indexability_technical", "on_page", "content", "schema",
                    "links", "performance", "prioritised_fixes",
                    "search_performance", "comparison"):
        assert section in findings, section
    assert findings["search_performance"] == {"provided": False}
    assert findings["meta"]["mode"] == "baseline"


def test_evidence_lists_are_capped(tmp_path):
    rows = [{"issue_type": "title_missing", "severity": "high",
             "url": f"{HOST}/x{i}", "final_url": f"{HOST}/x{i}",
             "detail": "no title", "site_level": "False"} for i in range(30)]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    for _path, fix in [("", f) for f in findings["prioritised_fixes"]]:
        assert len(fix["evidence"]) <= MAX_EVIDENCE


# --- orphans said once -------------------------------------------------------

def test_orphans_are_stated_once_with_their_sitemap_split(tmp_path):
    findings = build_findings(make_run(tmp_path), compare_enabled=False)
    orphans = findings["links"]["orphan_pages"]
    assert orphans["count"] == 1
    assert orphans["whole_is"] == "pages parsed"
    assert orphans["in_sitemap"] == 1

    # The old second name for the same fact is gone from the whole document.
    assert "pages_with_zero_inlinks" not in json.dumps(findings)


# --- content ------------------------------------------------------------------

def test_a_duplicate_group_handled_by_canonical_is_informational(tmp_path):
    pages = [
        {"url": f"{HOST}/a", "final_url": f"{HOST}/a", "status_code": "200",
         "word_count": "500", "schema_block_count": "1",
         "has_microdata": "False", "canonical": f"{HOST}/a", "score": "90"},
        {"url": f"{HOST}/b", "final_url": f"{HOST}/b", "status_code": "200",
         "word_count": "500", "schema_block_count": "1",
         "has_microdata": "False", "canonical": f"{HOST}/a", "score": "90"},
    ]
    handled = {"issue_type": "duplicate_content", "severity": "medium",
               "url": f"{HOST}/a", "final_url": f"{HOST}/a",
               "detail": f"2 pages have identical text: {HOST}/a, {HOST}/b",
               "site_level": "False"}
    run = make_run(tmp_path, pages=pages, page_issues=[handled])
    findings = build_findings(run, compare_enabled=False)

    dup = findings["content"]["duplicate_content"]
    assert dup["groups"] == 1
    assert dup["canonical_handled"] == 1
    assert dup["needs_attention"] == 0


def test_a_duplicate_group_not_handled_by_canonical_needs_attention(tmp_path):
    pages = [
        {"url": f"{HOST}/a", "final_url": f"{HOST}/a", "status_code": "200",
         "word_count": "500", "schema_block_count": "1",
         "has_microdata": "False", "canonical": f"{HOST}/a", "score": "90"},
        {"url": f"{HOST}/b", "final_url": f"{HOST}/b", "status_code": "200",
         "word_count": "500", "schema_block_count": "1",
         "has_microdata": "False", "canonical": f"{HOST}/b", "score": "90"},
    ]
    row = {"issue_type": "duplicate_content", "severity": "medium",
           "url": f"{HOST}/a", "final_url": f"{HOST}/a",
           "detail": f"2 pages have identical text: {HOST}/a, {HOST}/b",
           "site_level": "False"}
    findings = build_findings(make_run(tmp_path, pages=pages,
                                       page_issues=[row]),
                              compare_enabled=False)
    dup = findings["content"]["duplicate_content"]
    assert dup["canonical_handled"] == 0
    assert dup["needs_attention"] == 1


def test_word_count_quartiles_are_reported(tmp_path):
    findings = build_findings(make_run(tmp_path), compare_enabled=False)
    q = findings["content"]["word_count_quartiles"]
    assert q["min"] <= q["median"] <= q["max"]


# --- performance --------------------------------------------------------------

def test_unmeasured_templates_are_named_with_their_page_counts(tmp_path):
    pages = ([{"url": f"{HOST}/blogs/post-{i}",
               "final_url": f"{HOST}/blogs/post-{i}", "status_code": "200",
               "word_count": "300", "schema_block_count": "1",
               "has_microdata": "False",
               "canonical": f"{HOST}/blogs/post-{i}", "score": "90"}
              for i in range(3)]
             + [{"url": f"{HOST}/shop/item-{i}",
                 "final_url": f"{HOST}/shop/item-{i}", "status_code": "200",
                 "word_count": "300", "schema_block_count": "1",
                 "has_microdata": "False",
                 "canonical": f"{HOST}/shop/item-{i}", "score": "90"}
                for i in range(2)])
    ps = [{"url": f"{HOST}/blogs/post-0", "template": "/blogs/{slug}",
           "group_size": "3", "strategy": s, "performance_score": "40",
           "lab_lcp_ms": "5000", "lab_cls": "0.3", "field_data_level": "url",
           "error": ""} for s in ("mobile", "desktop")]

    findings = build_findings(make_run(tmp_path, pages=pages, pagespeed=ps),
                              compare_enabled=False)
    perf = findings["performance"]

    assert perf["skipped"] is False
    assert [m["template"] for m in perf["templates_measured"]] == ["/blogs/{slug}"]
    unmeasured = {u["template"]: u["pages"] for u in perf["templates_unmeasured"]}
    assert unmeasured == {"/shop/{slug}": 2}
    assert perf["pages_unmeasured"]["count"] == 2
    assert perf["pages_unmeasured"]["whole"] == 5


def test_a_skipped_pagespeed_stage_says_so(tmp_path):
    findings = build_findings(make_run(tmp_path), compare_enabled=False)
    assert findings["performance"]["skipped"] is True


# --- prioritised fixes --------------------------------------------------------

def test_fixes_are_ordered_by_impact_and_capped(tmp_path):
    rows = []
    for i in range(20):
        rows.append({"issue_type": "title_too_long", "severity": "low",
                     "url": f"{HOST}/a{i}", "final_url": f"{HOST}/a{i}",
                     "detail": "long", "site_level": "False"})
    for i in range(5):
        rows.append({"issue_type": "noindex_page", "severity": "high",
                     "url": f"{HOST}/n{i}", "final_url": f"{HOST}/n{i}",
                     "detail": "noindex", "site_level": "False"})

    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    fixes = findings["prioritised_fixes"]

    assert 0 < len(fixes) <= MAX_FIXES
    impacts = [f["impact"] for f in fixes]
    assert impacts == sorted(impacts, reverse=True)
    # 20 low (weight 1) beats 5 high (weight 3).
    assert fixes[0]["issue_type"] == "title_too_long"


def test_no_fix_title_contains_a_dash(tmp_path):
    rows = [{"issue_type": t, "severity": "medium", "url": f"{HOST}/p0",
             "final_url": f"{HOST}/p0", "detail": "d", "site_level": "False"}
            for t in ("canonical_off_page", "orphan_page", "thin_page",
                      "mixed_content", "schema_missing")]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    for fix in findings["prioritised_fixes"]:
        assert "-" not in fix["title"], fix["title"]
        assert fix["title"][0].isupper()


def test_fix_scope_distinguishes_template_config_and_page(tmp_path):
    rows = [
        # one pattern, many pages -> template
        *[{"issue_type": "h1_multiple", "severity": "low",
           "url": f"{HOST}/blogs/p-{i}", "final_url": f"{HOST}/blogs/p-{i}",
           "detail": "d", "site_level": "False"} for i in range(4)],
        # site wide -> config
        {"issue_type": "hsts_missing", "severity": "medium",
         "url": f"{HOST}/", "final_url": f"{HOST}/", "detail": "d",
         "site_level": "True"},
    ]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    scopes = {f["issue_type"]: f["fix_scope"] for f in
              findings["prioritised_fixes"]}
    assert scopes["h1_multiple"] == "template"
    assert scopes["hsts_missing"] == "config"


def test_unmeasured_severity_never_becomes_a_fix(tmp_path):
    rows = [{"issue_type": "pagespeed_error", "severity": "unmeasured",
             "url": f"{HOST}/p0", "final_url": f"{HOST}/p0",
             "detail": "stage deadline", "site_level": "False"}]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    assert findings["prioritised_fixes"] == []


# --- comparison ---------------------------------------------------------------

def test_no_previous_run_is_stated_not_guessed(tmp_path):
    run = make_run(tmp_path)
    findings = build_findings(run)
    assert findings["comparison"] == {"previous_run": None}


def test_the_default_previous_run_is_the_newest_other_complete_one(tmp_path):
    old = make_run(tmp_path, "20260101-000000")
    new = make_run(tmp_path, "20260102-000000")
    assert previous_run_dir(new) == old
    assert previous_run_dir(old) == new or previous_run_dir(old) is not None


def test_a_big_sitemap_change_lands_in_notable(tmp_path):
    """The 709 to 106 case that passed silently between two real runs."""
    old = make_run(tmp_path, "20260101-000000", summary_extra={
        "sitemap_sweep": {"sitemap_urls_total": 709, "sitemap_urls_swept": 1,
                          "sweep_capped": False, "sweep_limit": 10000,
                          "sweep_throttled": False, "already_crawled": 708}})
    new = make_run(tmp_path, "20260102-000000", summary_extra={
        "sitemap_sweep": {"sitemap_urls_total": 106, "sitemap_urls_swept": 0,
                          "sweep_capped": False, "sweep_limit": 10000,
                          "sweep_throttled": False, "already_crawled": 106}})

    result = compare(new, old)
    assert result["previous_run"] == "20260101-000000"
    assert result["deltas"]["sitemap_urls_total"]["change"] == -603
    metrics = {n["metric"] for n in result["notable"]}
    assert "sitemap_urls_total" in metrics
    top = result["notable"][0]
    assert top["metric"] == "sitemap_urls_total"
    assert top["direction"] == "down"


def test_a_drifted_previous_run_never_crashes(tmp_path):
    """An older folder written by an earlier version is missing keys."""
    old = make_run(tmp_path, "20260101-000000")
    # Strip the score block the way the session-4 folder lacks it.
    path = os.path.join(old, "crawl_summary.json")
    doc = json.loads(open(path, encoding="utf-8").read())
    del doc["score"]
    del doc["pagespeed"]
    open(path, "w", encoding="utf-8").write(json.dumps(doc))

    new = make_run(tmp_path, "20260102-000000")
    result = compare(new, old)

    assert result["previous_run"] == "20260101-000000"
    assert result["deltas"]["site_score"]["previous"] == "not in previous run"
    assert result["deltas"]["site_score"]["change"] == "not in previous run"
    assert result["deltas"]["mean_mobile_score"]["previous"] == \
        "not in previous run"
    # Metrics that do exist still compare normally.
    assert result["deltas"]["pages_found"]["change"] == 0


def test_issue_counts_split_into_new_resolved_and_unchanged(tmp_path):
    old_rows = [
        {"issue_type": "title_missing", "severity": "high",
         "url": f"{HOST}/a", "final_url": f"{HOST}/a", "detail": "d",
         "site_level": "False"},
        {"issue_type": "thin_page", "severity": "medium",
         "url": f"{HOST}/b", "final_url": f"{HOST}/b", "detail": "d",
         "site_level": "False"},
    ]
    new_rows = [
        {"issue_type": "title_missing", "severity": "high",
         "url": f"{HOST}/a", "final_url": f"{HOST}/a", "detail": "d",
         "site_level": "False"},
        {"issue_type": "mixed_content", "severity": "high",
         "url": f"{HOST}/c", "final_url": f"{HOST}/c", "detail": "d",
         "site_level": "False"},
    ]
    old = make_run(tmp_path, "20260101-000000", page_issues=old_rows)
    new = make_run(tmp_path, "20260102-000000", page_issues=new_rows)

    result = compare(new, old)
    assert result["issue_counts"]["title_missing"]["unchanged"] == 1
    assert result["issue_counts"]["thin_page"]["resolved"] == 1
    assert result["issue_counts"]["mixed_content"]["new"] == 1
    assert (result["issues_new"], result["issues_resolved"],
            result["issues_unchanged"]) == (1, 1, 1)


def test_comparison_can_be_switched_off(tmp_path):
    make_run(tmp_path, "20260101-000000")
    new = make_run(tmp_path, "20260102-000000")
    findings = build_findings(new, compare_enabled=False)
    assert findings["comparison"] == {"previous_run": None}


def test_findings_json_is_written_and_bounded(tmp_path):
    run = make_run(tmp_path)
    build_findings(run, compare_enabled=False)
    path = os.path.join(run, "findings.json")
    assert os.path.exists(path)
    assert os.path.getsize(path) < 200 * 1024
    json.loads(open(path, encoding="utf-8").read())
