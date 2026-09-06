"""The twelve defects found in the first client report, one fixture each."""

import json
import os
import re

import pytest
import requests
import requests_mock

from seo_audit import narrative as narr
from seo_audit.findings import build_findings, compare
from seo_audit.narrative import API_URL, Narrator, curate, parse_sections
from seo_audit.report import (build_charts, expected_headings_for, generate,
                              _sitemap_title, validate)
from test_findings import make_run
from test_report import HOST, completion, run_dir  # noqa: F401  (fixture)


# --- item 1: human labels ----------------------------------------------------

def test_every_registered_type_has_a_label_and_a_plain_finding():
    """No identifier may reach a client, so every type carries a plain name."""
    from seo_audit.issues import (PAGE_ISSUE_META, PAGE_ISSUE_SEVERITY,
                                  label_of, plain_finding, unit_of)
    for issue_type in PAGE_ISSUE_SEVERITY:
        assert issue_type in PAGE_ISSUE_META, issue_type
        label, template, unit = PAGE_ISSUE_META[issue_type]
        assert label and "_" not in label, issue_type
        skeleton = template.replace("{count}", "").replace("{whole}", "")
        assert skeleton and "_" not in skeleton, issue_type
        assert unit in ("page", "target", "group", "site"), issue_type

    assert label_of("title_missing") == "page title missing"
    assert plain_finding("title_missing", 40, 842) == \
        "40 of 842 pages have no page title"
    assert unit_of("external_link_broken") == "target"


# --- item 2: the canonical split ---------------------------------------------

def test_a_variant_canonical_is_not_reported_as_a_fault():
    """A filtered address pointing at its clean page is correct, not a fix."""
    from seo_audit.validate import _consolidates_variant
    assert _consolidates_variant(f"{HOST}/a/?product=1", f"{HOST}/a/")
    assert _consolidates_variant(f"{HOST}/a/?a=1&b=2", f"{HOST}/a/?a=1")
    assert not _consolidates_variant(f"{HOST}/a/", f"{HOST}/b/")
    assert not _consolidates_variant(f"{HOST}/a/?x=1", "https://other.com/a/")
    assert not _consolidates_variant(f"{HOST}/a/", f"{HOST}/a/?x=1")


def test_the_variant_canonical_costs_a_page_nothing():
    from seo_audit.scoring import load_weights, score_page
    assert score_page(["canonical_consolidates_variant"],
                      load_weights()).score == 100


def test_a_consolidating_canonical_is_never_offered_as_a_fix(tmp_path):
    rows = [{"issue_type": "canonical_consolidates_variant", "severity": "info",
             "url": f"{HOST}/a/?product={i}",
             "final_url": f"{HOST}/a/?product={i}",
             "detail": "correctly points back", "site_level": "False"}
            for i in range(5)]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    types = [f["issue_type"] for f in findings["prioritised_fixes"]]
    assert "canonical_consolidates_variant" not in types


# --- item 3: wholes per unit -------------------------------------------------

def test_a_target_fix_states_targets_and_pages_separately(tmp_path):
    """118 broken external links are not 118 pages."""
    rows = [{"issue_type": "external_link_broken", "severity": "medium",
             "url": f"https://dead{i}.com/",
             "final_url": f"https://dead{i}.com/",
             "detail": f"HTTP 404, linked from 20 page(s): {HOST}/a, {HOST}/b",
             "site_level": "False"} for i in range(3)]
    pages = [{"url": f"{HOST}/p{i}", "final_url": f"{HOST}/p{i}",
              "status_code": "200", "word_count": "300",
              "schema_block_count": "1", "has_microdata": "False",
              "canonical": f"{HOST}/p{i}", "score": "90"} for i in range(30)]
    findings = build_findings(
        make_run(tmp_path, pages=pages, page_issues=rows),
        compare_enabled=False)
    fix = [f for f in findings["prioritised_fixes"]
           if f["issue_type"] == "external_link_broken"][0]

    assert fix["unit"] == "target"
    assert fix["targets"]["count"] == 3
    assert fix["targets"]["whole_is"] == "links to other websites checked"
    assert fix["pages_affected"]["count"] == 20
    assert fix["pages_affected"]["whole_is"] == "pages parsed"


def test_a_page_fix_has_no_target_number(tmp_path):
    rows = [{"issue_type": "title_missing", "severity": "high",
             "url": f"{HOST}/p{i}", "final_url": f"{HOST}/p{i}",
             "detail": "no title", "site_level": "False"} for i in range(2)]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    fix = [f for f in findings["prioritised_fixes"]
           if f["issue_type"] == "title_missing"][0]
    assert fix["unit"] == "page"
    assert fix["targets"] is None


# --- item 4: sitemap attribution ---------------------------------------------

def sitemap_pair(tmp_path, before, after):
    old = make_run(tmp_path, "20260101-000000", summary_extra={
        "sitemap_sweep": {"sitemap_urls_total": before, "sitemap_urls_swept": 0,
                          "sweep_capped": False, "sweep_limit": 10000,
                          "sweep_throttled": False, "already_crawled": before}})
    new = make_run(tmp_path, "20260102-000000", summary_extra={
        "sitemap_sweep": {"sitemap_urls_total": after, "sitemap_urls_swept": 1,
                          "sweep_capped": False, "sweep_limit": 10000,
                          "sweep_throttled": False,
                          "already_crawled": after - 1}})
    return old, new


def test_a_sitemap_change_is_attributed_to_the_site(tmp_path):
    old, new = sitemap_pair(tmp_path, 106, 709)
    result = compare(new, old)

    sitemap = [n for n in result["notable"]
               if n["metric"] == "sitemap_urls_total"][0]
    assert sitemap["attribution"] == "site"
    assert "the site's sitemap listed 106 URLs last time and 709 now" in \
        sitemap["note"]
    assert result["sitemap_changed"] is True
    assert result["issue_counts_comparable"] is False


def test_count_movements_are_marked_not_comparable(tmp_path):
    old, new = sitemap_pair(tmp_path, 106, 709)
    result = compare(new, old)
    for item in result["notable"]:
        if item["metric"] == "sitemap_urls_total":
            continue
        assert item["note"] == "not comparable, coverage changed"


def test_the_comparison_is_written_by_code_when_coverage_moved(tmp_path):
    from docx import Document
    old, new = sitemap_pair(tmp_path, 106, 709)
    build_findings(new, previous_dir=old)

    with requests_mock.Mocker() as mock:
        mock.post(API_URL, status_code=500, text="must not be called")
        result = generate(new, use_ai=False)
        assert mock.call_count == 0

    text = " ".join(p.text for p in Document(result["docx"]).paragraphs)
    assert "listed 106 addresses last time and 709 this time" in text
    assert "not comparable with each other" in text
    assert "caching or generation problem" in text


def test_the_model_writes_the_comparison_when_coverage_held(tmp_path):
    old, new = sitemap_pair(tmp_path, 700, 709)
    findings = build_findings(new, previous_dir=old)
    assert findings["comparison"]["coverage_changed"] is False


# --- item 5: fix scope -------------------------------------------------------

def test_fix_scope_is_template_when_the_pages_share_one_shape(tmp_path):
    rows = [{"issue_type": "images_missing_alt", "severity": "low",
             "url": f"{HOST}/blogs/post-{i}",
             "final_url": f"{HOST}/blogs/post-{i}",
             "detail": "2 of 5 images have no alt", "site_level": "False"}
            for i in range(10)]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    fix = [f for f in findings["prioritised_fixes"]
           if f["issue_type"] == "images_missing_alt"][0]
    assert fix["fix_scope"] == "template"


def test_fix_scope_is_page_when_the_pages_are_scattered(tmp_path):
    rows = [{"issue_type": "images_missing_alt", "severity": "low",
             "url": f"{HOST}/s{i}/p", "final_url": f"{HOST}/s{i}/p",
             "detail": "no alt", "site_level": "False"} for i in range(10)]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    fix = [f for f in findings["prioritised_fixes"]
           if f["issue_type"] == "images_missing_alt"][0]
    assert fix["fix_scope"] == "page"


def test_a_site_wide_fix_is_a_config_change(tmp_path):
    rows = [{"issue_type": "hsts_missing", "severity": "medium",
             "url": f"{HOST}/", "final_url": f"{HOST}/", "detail": "no hsts",
             "site_level": "True"}]
    findings = build_findings(make_run(tmp_path, page_issues=rows),
                              compare_enabled=False)
    fix = [f for f in findings["prioritised_fixes"]
           if f["issue_type"] == "hsts_missing"][0]
    assert fix["fix_scope"] == "config"


# --- item 6: structured narrative and glossary -------------------------------

def test_a_section_comes_back_as_three_parts():
    parsed = parse_sections(
        '{"found": "We found things.", "why": "It matters.", '
        '"todo": ["Do this.", "Then this."]}')
    assert parsed["found"] == "We found things."
    assert parsed["why"] == "It matters."
    assert parsed["todo"] == ["Do this.", "Then this."]


def test_json_wrapped_in_a_code_fence_still_parses():
    parsed = parse_sections(
        'Here you go:\n```json\n{"found": "A.", "why": "B.", '
        '"todo": ["C."]}\n```')
    assert parsed and parsed["found"] == "A."


def test_unparseable_json_is_regenerated_once_then_templated(monkeypatch):
    monkeypatch.setattr(narr.time, "sleep", lambda s: None)
    section = {"orphans": {"count": 1, "whole": 2, "whole_is": "pages"}}
    with requests_mock.Mocker() as mock:
        mock.post(API_URL, json=completion("this is not json at all"))
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("links", section)

    assert narrator.usage.calls == 2, "exactly one regeneration"
    assert parts["found"]
    actions = [e.get("action") for e in narrator.usage.guard_events]
    assert "regenerated once" in actions
    assert "used the templated section" in actions


def test_the_glossary_sits_directly_after_the_executive_summary(run_dir):
    from docx import Document
    result = generate(run_dir, use_ai=False)
    headings = [p.text for p in Document(result["docx"]).paragraphs
                if p.style.name.startswith("Heading")
                or p.style.name == "Title"]
    assert "Terms used in this report" in headings
    assert headings.index("Terms used in this report") == \
        headings.index("Executive summary") + 1


def test_what_to_do_is_a_numbered_list_in_the_saved_file(run_dir):
    from docx import Document
    result = generate(run_dir, use_ai=False)
    styles = [p.style.name for p in Document(result["docx"]).paragraphs]
    assert "List Number" in styles


def test_the_style_forbids_defining_terms():
    assert "Never define a term" in narr.STYLE
    assert "Never describe how anything was measured" in narr.STYLE


# --- item 7: truncation ------------------------------------------------------

def framed(content, finish="stop"):
    return {"choices": [{"message": {"content": content},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_a_length_finish_reason_triggers_exactly_one_regeneration(monkeypatch):
    monkeypatch.setattr(narr.time, "sleep", lambda s: None)
    cut = framed('{"found": "A truncated", "why": "B.", "todo": ["C."]}',
                 "length")
    good = framed('{"found": "A.", "why": "B.", "todo": ["C."]}')

    with requests_mock.Mocker() as mock:
        mock.post(API_URL, [{"json": cut}, {"json": good}])
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("links", {"a": 1})

    assert narrator.usage.calls == 2
    assert parts["found"] == "A."
    assert narrator.usage.as_dict()["regenerations"] == 1


def test_a_sentence_that_never_ends_counts_as_truncated(monkeypatch):
    monkeypatch.setattr(narr.time, "sleep", lambda s: None)
    cut = framed('{"found": "We looked at the", "why": "B.", "todo": ["C."]}')
    good = framed('{"found": "A.", "why": "B.", "todo": ["C."]}')

    with requests_mock.Mocker() as mock:
        mock.post(API_URL, [{"json": cut}, {"json": good}])
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("links", {"a": 1})

    assert parts["found"] == "A."
    assert narrator.usage.calls == 2


def test_an_action_item_that_stops_mid_sentence_fails_validation(run_dir,
                                                                 tmp_path):
    """A cut off list item is the real truncation, not any sentence ending
    in a digit: "Trailing slash: 0." is data and must pass."""
    from docx import Document
    result = generate(run_dir, use_ai=False)
    document = Document(result["docx"])
    for paragraph in document.paragraphs:
        if paragraph.style.name == "List Number":
            paragraph.text = "Fix the titles and then the"
            break
    path = str(tmp_path / "cut.docx")
    document.save(path)

    with open(os.path.join(run_dir, "findings.json"), encoding="utf-8") as fh:
        findings = json.load(fh)
    problems = validate(path, expected_headings_for(findings),
                        len(build_charts(findings)))
    assert any("mid sentence" in p for p in problems)


def test_a_data_sentence_ending_in_a_number_is_not_truncation(run_dir):
    from docx import Document
    result = generate(run_dir, use_ai=False)
    document = Document(result["docx"])
    text = " ".join(p.text for p in document.paragraphs)
    assert "0." in text or "1." in text  # the templated prose does this
    with open(os.path.join(run_dir, "findings.json"), encoding="utf-8") as fh:
        findings = json.load(fh)
    assert validate(result["docx"], expected_headings_for(findings),
                    len(build_charts(findings))) == []


def test_a_section_missing_a_part_fails_validation(run_dir, tmp_path):
    from docx import Document
    result = generate(run_dir, use_ai=False)
    document = Document(result["docx"])
    for paragraph in document.paragraphs:
        if paragraph.text.startswith("Why it matters."):
            paragraph.text = ""
            break
    path = str(tmp_path / "missing-part.docx")
    document.save(path)

    with open(os.path.join(run_dir, "findings.json"), encoding="utf-8") as fh:
        findings = json.load(fh)
    problems = validate(path, expected_headings_for(findings),
                        len(build_charts(findings)))
    assert any("missing a part" in p for p in problems)


# --- item 8: a dropped sentence must not lose the finding --------------------

def test_a_dropped_sentence_is_replaced_by_the_finding_itself(monkeypatch):
    """The five blog posts with a broken preferred address must survive."""
    monkeypatch.setattr(narr.time, "sleep", lambda s: None)
    section = {"canonical": {"issue_type": "canonical_target_non_200",
                             "pages_affected": {"count": 5, "whole": 842,
                                                "whole_is": "pages parsed"},
                             "evidence": [{"final_url": f"{HOST}/blogs/one"},
                                          {"final_url": f"{HOST}/blogs/two"}]}}
    body = ('{"found": "Exactly 987654 pages are broken.", '
            '"why": "It matters.", "todo": ["Fix it."]}')

    with requests_mock.Mocker() as mock:
        mock.post(API_URL, json=completion(body))
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("indexability_technical", section)

    assert "987654" not in parts["found"]
    assert "preferred address that does not load" in parts["found"]
    assert "5 of 842" in parts["found"]
    assert f"{HOST}/blogs/one" in parts["found"]
    event = [e for e in narrator.usage.guard_events
             if e.get("types_restated")][0]
    assert "preferred address is broken" in event["types_restated"]


# --- item 9: curation and rounding -------------------------------------------

def test_internal_keys_never_reach_the_model():
    curated = curate({
        "content_simhash": "abc", "content_md5": "def",
        "pages_compared": 10, "min_words_for_comparison": 200,
        "external_check_limit": 1000, "run_id": "x", "some_id": "y",
        "orphans": {"count": 1, "whole": 2, "whole_is": "pages parsed"},
    })
    blob = json.dumps(curated)
    for banned in ("simhash", "md5", "pattern_of", "pages_compared",
                   "min_words", "run_id", "some_id"):
        assert banned not in blob, banned
    assert curated["orphans"]["count"] == 1


def test_identifiers_are_swapped_for_labels_before_the_model_sees_them():
    curated = curate({"top_issue": "title_missing",
                      "types": ["hsts_missing", "orphan_page"]})
    assert curated["top issue"] == "page title missing"
    assert "secure connection not enforced" in curated["types"]


def test_performance_milliseconds_are_whole_numbers(tmp_path):
    ps = [{"url": f"{HOST}/p0", "template": "/p0", "group_size": "2",
           "strategy": s, "performance_score": "40",
           "lab_lcp_ms": "11551.021", "lab_cls": "0.4712",
           "field_inp_ms": "331.4", "field_data_level": "url", "error": ""}
          for s in ("mobile", "desktop")]
    findings = build_findings(make_run(tmp_path, pagespeed=ps),
                              compare_enabled=False)
    measured = findings["performance"]["templates_measured"][0]

    assert measured["lab_lcp_ms"] == 11551
    assert measured["lab_cls"] == 0.47
    # Real visitor responsiveness is stated once, not per template.
    assert "field_inp_ms" not in measured
    assert findings["performance"]["field_inp"]["worst_ms"] == 331


def test_the_sitemap_pie_spells_out_what_it_adds_together():
    assert _sitemap_title(949, 948, 1) == (
        "Sitemap coverage, of 949 URLs seen "
        "(948 crawled plus 1 sitemap URL not crawled)")


# --- item 10: no identifiers anywhere in the document ------------------------

def test_no_underscore_identifier_appears_outside_a_url(run_dir):
    from docx import Document
    result = generate(run_dir, use_ai=False)
    document = Document(result["docx"])

    texts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            texts.extend(cell.text for cell in row.cells)

    identifier = re.compile(r"\b[a-z]+_[a-z_]+\b")
    for text in texts:
        without_urls = re.sub(r"https?://\S+", "", text)
        found = identifier.findall(without_urls)
        assert not found, f"identifier {found} in {text[:80]!r}"


# --- item 12: cleanup --------------------------------------------------------

def test_no_commit_message_file_sits_in_the_repo():
    assert not os.path.exists("commitmsg.txt")


def test_findings_named_as_keys_are_restated_too():
    """The real section shape: an issue type as a key mapping to a share.

    The indexability section carries "canonical_target_non_200" this way, and
    missing it is how five real findings vanished from the first report.
    """
    from seo_audit.narrative import restate_findings
    section = {
        "canonical": {
            "canonical_target_non_200": {"count": 5, "whole": 842,
                                         "whole_is": "pages parsed"},
            "canonical_missing": {"count": 5, "whole": 842,
                                  "whole_is": "pages parsed"},
        },
    }
    text, types = restate_findings(section)

    assert "preferred address is broken" in types
    assert "5 of 842 pages point to a preferred address that does not load" \
        in text


def test_a_dropped_sentence_restates_key_shaped_findings(monkeypatch):
    monkeypatch.setattr(narr.time, "sleep", lambda s: None)
    section = {"canonical": {"canonical_target_non_200": {
        "count": 5, "whole": 842, "whole_is": "pages parsed"}}}
    body = ('{"found": "Exactly 987654 pages are affected.", '
            '"why": "It matters.", "todo": ["Fix it."]}')

    with requests_mock.Mocker() as mock:
        mock.post(API_URL, json=completion(body))
        narrator = Narrator(key="k", session=requests.Session())
        parts = narrator.write("indexability_technical", section)

    assert "987654" not in parts["found"]
    assert "5 of 842 pages point to a preferred address" in parts["found"]
    event = [e for e in narrator.usage.guard_events
             if e.get("types_restated")][0]
    assert event["types_restated"] == ["preferred address is broken"]
