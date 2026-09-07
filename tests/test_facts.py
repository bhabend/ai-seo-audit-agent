"""The six wording defects left in the second client report.

The through line: the model no longer says what was found. Code does, from
the findings, so a number can never end up attached to the wrong noun and a
section can never open on something that was not found.
"""

import json

import pytest
import requests
import requests_mock

from seo_audit import narrative as narr
from seo_audit.findings import build_findings
from seo_audit.narrative import (API_URL, Narrator, filter_todo, find_jargon,
                                 is_about_the_audit, section_facts,
                                 summary_facts)
from seo_audit.report import _fixes_table_rows, GLOSSARY, generate
from test_findings import make_run
from test_report import HOST, completion, run_dir  # noqa: F401  (fixture)


def page_rows(issue_type, severity, count, shape="{host}/blogs/post-{i}",
              detail="x"):
    return [{"issue_type": issue_type, "severity": severity,
             "url": shape.format(host=HOST, i=i),
             "final_url": shape.format(host=HOST, i=i),
             "detail": detail, "site_level": "False"} for i in range(count)]


# --- item 1: the findings paragraph is written by code -----------------------

def test_the_found_paragraph_names_every_finding_the_section_has():
    section = {
        "title": {"missing": {"count": 3, "whole": 842,
                              "whole_is": "pages parsed"},
                  "too_long": {"count": 233, "whole": 842,
                               "whole_is": "pages parsed"},
                  "too_short": {"count": 0, "whole": 842,
                                "whole_is": "pages parsed"},
                  "duplicate_groups": 44},
        "meta_description": {"missing": {"count": 9, "whole": 842,
                                         "whole_is": "pages parsed"},
                             "too_long": {"count": 251, "whole": 842,
                                          "whole_is": "pages parsed"},
                             "duplicate_groups": 0},
        "h1": {"missing": {"count": 19, "whole": 842,
                           "whole_is": "pages parsed"},
               "multiple": {"count": 0, "whole": 842,
                            "whole_is": "pages parsed"}},
        "images_missing_alt": {"count": 280, "whole": 842,
                               "whole_is": "pages parsed"},
        "viewport_missing": {"count": 0, "whole": 842,
                             "whole_is": "pages parsed"},
        "html_lang_missing": {"count": 0, "whole": 842,
                              "whole_is": "pages parsed"},
    }
    facts = section_facts("on_page", section, 842)

    # Every finding with a count is stated, with its own count and whole.
    # Six of them are sentences in the paragraph and the rest are rows in
    # the "Also found" table; section_facts is both halves together.
    assert "3 of 842 pages have no page title" in facts
    assert "251 of 842 pages have a search description that will be cut" \
        in facts
    assert "44 groups of pages share one title" in facts
    assert "280 of 842 pages have images with no text description" in facts
    # Nothing that was not found is mentioned at all.
    for absent in ("only a few characters", "more than one main heading",
                   "mobile layout setting", "do not declare their language",
                   "share one search description"):
        assert absent not in facts, absent


def test_the_found_paragraph_puts_the_worst_first_and_the_good_news_last():
    section = {
        "noindex_pages": {"count": 5, "whole": 842,
                          "whole_is": "pages parsed"},
        "canonical": {
            "canonical_off_page": {"count": 124, "whole": 842,
                                   "whole_is": "pages parsed"},
            "canonical_consolidates_variant": {"count": 30, "whole": 842,
                                               "whole_is": "pages parsed"},
        },
        "site_level": {"deduction": 0, "types": []},
    }
    facts = section_facts("indexability_technical", section, 842)

    high = facts.index("tell search engines not to list them")
    medium = facts.index("name a different page as the preferred one")
    good = facts.index("The site also gets some things right")
    assert high < medium < good
    assert facts.rstrip().endswith(".")


def test_a_section_with_no_page_faults_still_states_its_findings():
    """Crawlability counts obstacles, not page faults, and still speaks."""
    section = {
        "sitemap": {"urls_total": 709, "non_200": 0,
                    "in_sitemap_not_crawled": {"count": 1, "whole": 709,
                                               "whole_is": "sitemap URLs"},
                    "crawled_not_in_sitemap": {"count": 267, "whole": 948,
                                               "whole_is": "pages found"}},
        "redirects": {"total": {"count": 58, "whole": 948,
                                "whole_is": "pages found"},
                      "trailing_slash": {"count": 38, "whole": 58,
                                         "whole_is": "redirects"},
                      "chains": 0, "loops": 0},
        "robots": {"found": True, "blocked_linked": 0,
                   "blocked_in_sitemap": 0},
        "deep_pages": {"count": 17, "whole": 842, "whole_is": "pages parsed"},
        "render_suspects": {"count": 0, "whole": 842,
                            "whole_is": "pages parsed"},
        "slow_responses": 0, "fetch_errors": 0, "documents_linked": 0,
    }
    facts = section_facts("crawlability", section)

    assert "The sitemap lists 709 addresses." in facts
    assert "38 of those redirect only to add a slash at the end" in facts
    assert "17 of 842 pages sit more than three clicks" in facts
    assert "chains" not in facts and "loop" not in facts


def test_the_summary_states_the_score_the_spread_and_the_top_five():
    headline = {"site_score": 80, "pages_scored": 837,
                "site_level_deduction": 4,
                "distribution": {"0-39": {"count": 0, "whole": 837,
                                          "whole_is": "pages scored"},
                                 "60-79": {"count": 47, "whole": 837,
                                           "whole_is": "pages scored"},
                                 "80-100": {"count": 789, "whole": 837,
                                            "whole_is": "pages scored"}}}
    fixes = [{"label": f"fix {n}", "title": f"Do thing {n}"}
             for n in range(1, 8)]
    facts = summary_facts(headline, fixes)

    assert "The site scores 80 out of 100, across 837 pages scored." in facts
    assert "deduction of 4 points" in facts
    assert "789 score 80 to 100" in facts and "47 score 60 to 79" in facts
    assert "0 score" not in facts, "an empty score band is not a finding"
    for n in range(1, 6):
        assert f"fix {n}" in facts
    assert "fix 6" not in facts
    assert "-" not in facts, "a score band must not reach the page as a dash"


def test_a_found_key_from_the_model_changes_nothing():
    section = {"title": {"missing": {"count": 3, "whole": 842,
                                     "whole_is": "pages parsed"}}}
    body = ('{"found": "The model would like to say something else.", '
            '"why": "It matters here.", "todo": ["Add the missing titles."]}')
    with requests_mock.Mocker() as mock:
        mock.post(API_URL, json=completion(body))
        narrator = Narrator(key="k", session=requests.Session(),
                            default_whole=842)
        parts = narrator.write("on_page", section)

    assert parts["found"] == section_facts("on_page", section, 842)
    assert "would like to say" not in parts["found"]
    assert parts["why"] == "It matters here."


def test_the_priority_fixes_paragraph_uses_each_fix_own_sentence(tmp_path):
    rows = page_rows("external_link_broken", "medium", 3,
                     shape="https://dead-{i}.com/",
                     detail="HTTP 404, linked from 20 page(s): "
                            f"{HOST}/a, {HOST}/b")
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    facts = section_facts("prioritised_fixes",
                          findings["prioritised_fixes"], 4)

    assert "3 links to other websites no longer load" in facts
    assert "3 of 4 pages" not in facts, "targets are not pages"


# --- item 2: a whole that means something ------------------------------------

def test_a_broken_internal_target_is_out_of_addresses_crawled(tmp_path):
    """"53 of 106768 internal link targets" divided addresses by links."""
    rows = page_rows("broken_internal_link", "high", 4,
                     shape="{host}/gone/{i}",
                     detail=f"HTTP 404, linked from 30 page(s): {HOST}/a")
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    fix = [f for f in findings["prioritised_fixes"]
           if f["issue_type"] == "broken_internal_link"][0]

    assert fix["targets"] == {"count": 4, "whole": 4,
                              "whole_is": "internal addresses crawled"}
    assert fix["plain_finding"] == \
        "4 addresses linked from the site no longer load"


def test_the_fixes_table_says_targets_first_and_pages_second():
    rows = _fixes_table_rows([{
        "title": "Repair or remove links to pages that have gone",
        "pages_affected": {"count": 834, "whole": 842,
                           "whole_is": "pages parsed"},
        "targets": {"count": 118, "whole": 1000,
                    "whole_is": "links to other websites checked"},
        "severity": "medium", "fix_scope": "page"}])

    assert rows[0][1] == ("118 of 1000 links to other websites checked, "
                          "linked from 834 of 842 pages parsed")


# --- item 3: template work is not page by page -------------------------------

def test_three_templates_covering_most_pages_is_one_template_fix(tmp_path):
    """40, 20 and 15 percent over three shapes is template work, not 250 edits."""
    rows = (page_rows("meta_description_missing", "medium", 40,
                      shape="{host}/blogs/post-{i}")
            + page_rows("meta_description_missing", "medium", 20,
                        shape="{host}/locations/{i}/office")
            + page_rows("meta_description_missing", "medium", 15,
                        shape="{host}/workspaces/{i}/plan")
            + page_rows("meta_description_missing", "medium", 25,
                        shape="{host}/loose-{i}"))
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    fix = [f for f in findings["prioritised_fixes"]
           if f["issue_type"] == "meta_description_missing"][0]
    assert fix["fix_scope"] == "template"


def test_pages_spread_over_many_shapes_is_still_page_by_page(tmp_path):
    rows = page_rows("meta_description_missing", "medium", 20,
                     shape="{host}/s{i}/page")
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    fix = [f for f in findings["prioritised_fixes"]
           if f["issue_type"] == "meta_description_missing"][0]
    assert fix["fix_scope"] == "page"


# --- item 4: jargon ----------------------------------------------------------

def test_the_banned_words_are_the_ones_the_glossary_does_not_carry():
    for word in ("equity", "crawl waste", "HSTS", "hreflang", "noindex",
                 "canonical", "SERP", "indexation", "simhash", "Lighthouse"):
        assert find_jargon(word) == [word.lower()], word
    assert find_jargon("Reduce image sizes on the location template") == []
    # The style sheet tells the model both halves: what not to say and what
    # to say instead.
    for word in ("crawl waste", "hsts", "hreflang"):
        assert word in narr.STYLE.lower()
    assert "preferred address" in narr.STYLE


def test_jargon_sends_the_section_back_exactly_once(monkeypatch):
    monkeypatch.setattr(narr.time, "sleep", lambda s: None)
    bad = completion('{"why": "This creates crawl waste.", '
                     '"todo": ["Repair the broken links."]}')
    good = completion('{"why": "Visitors reach a dead end.", '
                      '"todo": ["Repair the broken links."]}')
    with requests_mock.Mocker() as mock:
        mock.post(API_URL, [{"json": bad}, {"json": good}])
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("links", {"a": 1})

    assert narrator.usage.calls == 2
    assert parts["why"] == "Visitors reach a dead end."
    event = [e for e in narrator.usage.guard_events
             if "words the report does not use" in e["reason"]]
    assert event[0]["action"] == "regenerated once"
    assert "crawl waste" in event[0]["reason"]


def test_jargon_twice_falls_back_to_the_templated_wording(monkeypatch):
    monkeypatch.setattr(narr.time, "sleep", lambda s: None)
    bad = completion('{"why": "The canonical is wrong.", '
                     '"todo": ["Fix the noindex pages."]}')
    with requests_mock.Mocker() as mock:
        mock.post(API_URL, json=bad)
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("links", {"a": 1})

    assert narrator.usage.calls == 2, "one regeneration, then stop paying"
    assert find_jargon(parts["why"], parts["todo"]) == []
    assert "priority fixes" in parts["why"]
    assert [e for e in narrator.usage.guard_events
            if e["action"] == "used the templated why and what to do"]


def test_the_glossary_carries_the_two_terms_the_report_now_uses():
    terms = [term for term, _meaning in GLOSSARY]
    assert "Page title" in terms
    assert "Search description" in terms


# --- item 5 and 6: advice about the audit ------------------------------------

def test_advice_about_the_audit_is_not_advice_for_the_client():
    """Matched as phrases about our measuring, not as words.

    The first version of this rule matched the bare word "audit" and threw
    away "Audit and reduce third party scripts", which was real advice.
    """
    assert is_about_the_audit(
        "Expand measurement to the 125 unmeasured templates.")
    assert is_about_the_audit("Re-run the audit after the fixes land.")
    assert is_about_the_audit("Crawl the site again next time.")
    assert not is_about_the_audit(
        "Reduce image sizes on the location template.")
    assert not is_about_the_audit(
        "Audit and reduce third party scripts on high traffic templates.")

    kept, dropped = filter_todo([
        "Expand measurement to the unmeasured templates.",
        "Reduce image sizes on the location template."])
    assert kept == ["Reduce image sizes on the location template."]
    assert len(dropped) == 1


def test_a_sampling_action_is_dropped_and_logged():
    body = ('{"why": "Speed decides whether people stay.", '
            '"todo": ["Expand measurement to the unmeasured templates.", '
            '"Reduce image sizes on the location template."]}')
    with requests_mock.Mocker() as mock:
        mock.post(API_URL, json=completion(body))
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("performance", {"a": 1})

    assert parts["todo"] == ["Reduce image sizes on the location template."]
    event = [e for e in narrator.usage.guard_events
             if e.get("items_dropped")][0]
    assert event["action"] == "dropped the action"


def test_a_to_do_list_that_empties_falls_back_to_the_template():
    """A section can never be left without something to do."""
    body = ('{"why": "Speed decides whether people stay.", '
            '"todo": ["Measure the remaining templates.", '
            '"Re-run the audit next quarter."]}')
    with requests_mock.Mocker() as mock:
        mock.post(API_URL, json=completion(body))
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("performance", {"a": 1})

    assert parts["todo"], "a section always has an action"
    assert not any(is_about_the_audit(t) for t in parts["todo"])
    assert [e for e in narrator.usage.guard_events
            if e.get("action") == "used the templated action"]


# --- the whole document ------------------------------------------------------

def test_the_saved_report_carries_no_jargon_outside_the_glossary(run_dir):
    import re
    from docx import Document

    result = generate(run_dir, use_ai=False)
    document = Document(result["docx"])
    banned = re.compile(
        r"\b(equity|crawl waste|crawl budget|serps?|indexation|hreflang|hsts"
        r"|simhash|lighthouse|noindex|canonicals?)\b", re.I)

    in_glossary = False
    for paragraph in document.paragraphs:
        if paragraph.style.name.startswith("Heading"):
            in_glossary = paragraph.text.strip() == "Terms used in this report"
        if in_glossary:
            continue
        assert not banned.search(paragraph.text), paragraph.text[:80]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                assert not banned.search(cell.text), cell.text[:80]


def test_every_section_of_the_saved_report_says_what_was_found(run_dir):
    from docx import Document

    result = generate(run_dir, use_ai=False)
    document = Document(result["docx"])
    found = [p.text for p in document.paragraphs
             if p.text.startswith("What we found.")]

    assert found, "no findings paragraphs at all"
    for text in found:
        body = text[len("What we found."):].strip()
        assert body, "an empty findings paragraph"
        assert not body.lower().startswith("no "), body[:60]
