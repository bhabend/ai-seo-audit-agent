"""The short report: the client deliverable, built by code with no model.

Management's rule is that a client receives something short that does not
read as machine written. So the whole document is tables: a score, ten fixes
each with an action, one table per section, two charts. These tests hold the
shape of it and the promises it makes about counts and wholes.
"""

import json
import os

import pytest
import requests
import requests_mock
from docx import Document

from seo_audit.findings import build_findings
from seo_audit.issues import (PAGE_ISSUE_ACTION, PAGE_ISSUE_SEVERITY,
                              action_of, label_of)
from seo_audit.narrative import API_URL, BANNED_WORDS, find_jargon
from seo_audit.report import (FIXES_COLUMNS, IN_PLACE, SHORT_MAX_WORDS,
                              ValidationError, build_parser, build_report,
                              build_short_charts, build_short_document,
                              expected_headings_for_short, generate_short,
                              main, validate_short)
from test_findings import make_run

HOST = "https://example.com"

# One run with something in every section, so the tables have rows to hold.
ISSUES = (
    ("title_missing", "high", 3),
    ("title_too_long", "low", 20),
    ("meta_description_missing", "medium", 9),
    ("h1_missing", "medium", 6),
    ("images_missing_alt", "low", 28),
    ("noindex_page", "high", 2),
    ("canonical_off_page", "medium", 12),
    ("thin_page", "medium", 3),
    ("duplicate_content", "medium", 2),
    ("schema_missing", "low", 7),
    ("orphan_page", "medium", 5),
    ("broken_internal_link", "high", 4),
    ("external_link_broken", "medium", 6),
    ("hsts_missing", "medium", 1),
)


def issue_rows():
    rows = []
    for issue_type, severity, count in ISSUES:
        for index in range(count):
            rows.append({
                "issue_type": issue_type, "severity": severity,
                "url": f"{HOST}/{issue_type}/p{index}",
                "final_url": f"{HOST}/{issue_type}/p{index}",
                "detail": f"HTTP 404, linked from {10 + index} page(s): "
                          f"{HOST}/a, {HOST}/b",
                "site_level": str(issue_type == "hsts_missing"),
            })
    return rows


def pagespeed_rows():
    rows = []
    for template, score, lcp, cls in (
            ("/workspaces/{slug} (depth 4)", "15", "9000", "0.4"),
            ("/blogs/{slug}", "45", "3000", "0.05"),
            ("/ (homepage)", "80", "1800", "0.02")):
        for strategy in ("mobile", "desktop"):
            rows.append({"url": f"{HOST}{template.split(' ')[0]}",
                         "template": template, "group_size": "12",
                         "strategy": strategy, "performance_score": score,
                         "lab_lcp_ms": lcp, "lab_cls": cls,
                         "field_data_level": "url", "error": ""})
    return rows


def short_run_dir(tmp_path, name="20260101-000000", **extra):
    rows = issue_rows()
    counts = {}
    for row in rows:
        counts[row["issue_type"]] = counts.get(row["issue_type"], 0) + 1
    pages = [{"url": f"{HOST}/p{i}", "final_url": f"{HOST}/p{i}",
              "status_code": "200", "word_count": "300",
              "schema_block_count": "1" if i % 2 else "0",
              "has_microdata": "False", "canonical": f"{HOST}/p{i}",
              "score": "90"} for i in range(60)]
    summary_extra = {
        "page_issue_counts": counts,
        "score": {"site_score": 72, "mean_page_score": 84.0,
                  "performance_component": 40.0, "performance_included": True,
                  "performance_measured": 3, "performance_sampled": 3,
                  "site_level_deduction": 4, "site_level_types":
                      ["hsts_missing"],
                  "pages_scored": 58, "noindex_pages": 2, "noindex_urls": [],
                  "distribution": {"0-39": 1, "40-59": 5, "60-79": 12,
                                   "80-100": 40},
                  "lowest_pages": [{"final_url": f"{HOST}/p1", "score": 41,
                                    "top_issue": "title_missing"}],
                  "note": "x"},
        "pagespeed": {"skipped": False, "templates_sampled": 3,
                      "pages_represented": 36, "mean_mobile_score": 46.7,
                      "mean_desktop_score": 61.0, "errors": 0,
                      "worst_template_mobile": {
                          "template": "/workspaces/{slug} (depth 4)",
                          "url": f"{HOST}/workspaces/a", "group_size": 12,
                          "performance_score": 15}},
        "crawl_issues": {"crawl_only_page": 14, "deep_page": 6,
                         "trailing_slash_redirect": 9, "redirect_chain": 2,
                         "document_linked": 3, "sitemap_non_200": 1},
        "redirected": 15,
    }
    summary_extra.update(extra)
    return make_run(tmp_path, name, page_issues=rows, pages=pages,
                    pagespeed=pagespeed_rows(), summary_extra=summary_extra)


@pytest.fixture
def short_run(tmp_path):
    run = short_run_dir(tmp_path)
    build_findings(run, compare_enabled=False)
    return run


@pytest.fixture
def short_doc(short_run):
    result = generate_short(short_run)
    return Document(result["docx"]), result


def tables_of(document, header):
    return [t for t in document.tables
            if [c.text.strip() for c in t.rows[0].cells] == header]


def section_tables(document):
    """Every Finding table, keyed by the heading it sits under."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    out, current = {}, None
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            if paragraph.style.name.startswith("Heading"):
                current = paragraph.text.strip()
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            if [c.text.strip() for c in table.rows[0].cells][0] == "Finding":
                out[current] = table
    return out


# --- the registry has an action for every type ------------------------------

def test_every_registered_type_carries_a_plain_action():
    """No blank action can ship: the build fails first."""
    for issue_type in PAGE_ISSUE_SEVERITY:
        action = PAGE_ISSUE_ACTION.get(issue_type)
        assert action, f"{issue_type} has no action"
        assert "_" not in action, issue_type
        assert action.endswith("."), issue_type
        assert action[0].isupper(), issue_type
        assert find_jargon(action) == [], (issue_type, action)
    assert not set(PAGE_ISSUE_ACTION) - set(PAGE_ISSUE_SEVERITY), \
        "an action exists for a type nobody records"
    assert action_of("title_missing").startswith("Write a page title")
    assert action_of("hsts_missing").startswith("Ask the developers")


def test_an_action_never_says_what_the_glossary_would_have_to_explain():
    for word in ("canonical", "noindex", "hreflang", "hsts"):
        assert word in [w.lower() for w in BANNED_WORDS]
        for action in PAGE_ISSUE_ACTION.values():
            assert word not in action.lower(), action


# --- short is what the command builds ---------------------------------------

def test_short_is_the_default_format():
    args = build_parser().parse_args(["--run", "somewhere"])
    assert args.fmt == "short"


def test_the_default_report_calls_no_model(short_run):
    with requests_mock.Mocker() as mock:
        mock.post(API_URL, status_code=500, text="must not be called")
        result = build_report(short_run)
        assert mock.call_count == 0

    assert result["format"] == "short"
    assert result["usage_data"]["calls"] == 0
    assert result["usage"] is None
    assert os.path.exists(result["docx"])


def test_the_command_writes_a_short_report_with_no_key(short_run,
                                                       monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with requests_mock.Mocker() as mock:
        mock.post(API_URL, status_code=500, text="must not be called")
        assert main(["--run", short_run]) == 0
        assert mock.call_count == 0


def test_format_long_reaches_the_narrated_renderer(short_run):
    """A routing test only: the long format has its own tests."""
    result = build_report(short_run, fmt="long", use_ai=False)
    assert result["format"] == "long"
    document = Document(result["docx"])
    assert any(p.text.startswith("What we found.")
               for p in document.paragraphs)


# --- what the document contains ---------------------------------------------

def test_the_headings_are_the_short_ones_in_order(short_doc):
    document, _result = short_doc
    headings = [p.text.strip() for p in document.paragraphs
                if p.style.name.startswith("Heading")
                or p.style.name == "Title"]
    with open(os.path.join(os.path.dirname(_result["docx"]),
                           "findings.json"), encoding="utf-8") as handle:
        expected = expected_headings_for_short(json.load(handle))

    position = 0
    for wanted in expected:
        assert wanted in headings[position:], wanted
        position = headings.index(wanted, position) + 1
    # The long format's furniture is gone.
    assert "Terms used in this report" not in headings
    assert "Executive summary" not in headings


def test_there_are_exactly_two_charts(short_doc):
    document, result = short_doc
    assert result["charts"] == 2
    assert len(document.inline_shapes) == 2


def test_no_prose_beyond_a_lead_sentence(short_doc):
    document, _result = short_doc
    words = sum(len(p.text.split()) for p in document.paragraphs)
    assert words <= SHORT_MAX_WORDS
    assert not any(p.text.startswith("What we found.")
                   for p in document.paragraphs)
    assert not any(p.style.name == "List Number" for p in document.paragraphs)


def test_every_fix_row_carries_an_action(short_doc):
    document, _result = short_doc
    table = tables_of(document, FIXES_COLUMNS)[0]

    assert len(table.rows) > 1
    for index, row in enumerate(table.rows[1:], start=1):
        cells = [c.text.strip() for c in row.cells]
        assert cells[0] == str(index), "the rank column counts up"
        assert cells[1], "a fix with no name"
        assert " of " in cells[2], cells[2]
        assert cells[3] in ("high", "medium", "low"), cells[3]
        assert cells[5].endswith("."), cells[5]


def test_every_section_table_lists_its_findings_once(short_doc):
    document, result = short_doc
    with open(os.path.join(os.path.dirname(result["docx"]),
                           "findings.json"), encoding="utf-8") as handle:
        findings = json.load(handle)

    tables = section_tables(document)
    assert set(tables) >= {"On page", "Content", "Internal and external links",
                           "Indexability and technical", "Performance",
                           "Crawlability and sitemap", "Structured data"}

    on_page = [[c.text.strip() for c in row.cells]
               for row in tables["On page"].rows[1:]]
    findings_named = [row[0] for row in on_page]
    assert len(findings_named) == len(set(findings_named)), "a finding twice"

    for issue_type in ("title_missing", "meta_description_missing",
                       "h1_missing", "images_missing_alt"):
        label = label_of(issue_type).capitalize()
        assert label in findings_named, label
    # Nothing that was not found is listed as a finding.
    for absent in ("Page language not declared", "No mobile layout setting"):
        assert absent not in findings_named
    assert findings_named[-1] == IN_PLACE
    assert on_page[-1][3], "the In place row says what is already right"


def test_every_count_states_its_whole(short_doc):
    document, _result = short_doc
    for heading, table in section_tables(document).items():
        for row in table.rows[1:]:
            finding, count = (row.cells[0].text.strip(),
                              row.cells[1].text.strip())
            if finding == IN_PLACE:
                continue
            assert count, f"{heading}: {finding} has no count"
            assert " of " in count or count == "the whole site", \
                f"{heading}: {finding} = {count!r}"


def test_a_site_wide_setting_states_the_whole_site(short_doc):
    document, _result = short_doc
    table = section_tables(document)["Indexability and technical"]
    rows = {row.cells[0].text.strip(): row.cells[1].text.strip()
            for row in table.rows[1:]}
    assert rows["Secure connection not enforced"] == "the whole site"


def test_the_performance_table_counts_templates(short_doc):
    document, _result = short_doc
    table = section_tables(document)["Performance"]
    rows = {row.cells[0].text.strip(): row.cells[1].text.strip()
            for row in table.rows[1:]}

    assert rows["Templates that score poorly on a phone"] == \
        "2 of 3 measured templates"
    assert IN_PLACE in rows


def test_the_saved_file_carries_no_shape_outside_the_developer_column(
        short_doc):
    document, _result = short_doc
    for table in document.tables:
        header = [c.text.strip() for c in table.rows[0].cells]
        shape_column = (1 if header[:1] == ["Template"]
                        and header[1].startswith("Shape") else None)
        for index, row in enumerate(table.rows):
            for column, cell in enumerate(row.cells):
                if shape_column is not None and index and \
                        column == shape_column:
                    continue
                assert "{" not in cell.text, cell.text[:60]
                assert "(depth" not in cell.text, cell.text[:60]
    for paragraph in document.paragraphs:
        assert "{" not in paragraph.text and "(depth" not in paragraph.text
        assert "—" not in paragraph.text and " - " not in paragraph.text


def test_the_developer_column_keeps_the_shape(short_doc):
    document, _result = short_doc
    tables = [t for t in document.tables
              if [c.text.strip() for c in t.rows[0].cells][:1] == ["Template"]]
    assert tables, "no templates table"
    shapes = [row.cells[1].text for row in tables[0].rows[1:]]
    assert any("{slug}" in shape for shape in shapes)


# --- the comparison block ----------------------------------------------------

def test_no_comparison_block_without_a_previous_run(short_doc):
    document, _result = short_doc
    headings = [p.text.strip() for p in document.paragraphs
                if p.style.name.startswith("Heading")]
    assert "Comparison with the previous audit" not in headings


def test_a_previous_run_brings_the_comparison_block(tmp_path):
    short_run_dir(tmp_path, "20260101-000000", links={
        "pages_with_zero_inlinks": 2, "orphan_pages_in_sitemap": 1,
        "broken_internal_targets": 1, "redirected_internal_targets": 0,
        "external_targets_found": 10, "external_checked": 10,
        "external_broken": 1, "external_unchecked": 0,
        "external_check_limit": 1000, "edges": 40})
    new = short_run_dir(tmp_path, "20260102-000000", links={
        "pages_with_zero_inlinks": 5, "orphan_pages_in_sitemap": 3,
        "broken_internal_targets": 4, "redirected_internal_targets": 0,
        "external_targets_found": 10, "external_checked": 10,
        "external_broken": 6, "external_unchecked": 0,
        "external_check_limit": 1000, "edges": 40})
    findings = build_findings(new)
    assert findings["comparison"]["previous_run"]

    result = generate_short(new)
    document = Document(result["docx"])
    headings = [p.text.strip() for p in document.paragraphs
                if p.style.name.startswith("Heading")]
    assert "Comparison with the previous audit" in headings
    assert tables_of(document,
                     ["Measure", "Previous", "Now", "Change", "Note"])


# --- validation --------------------------------------------------------------

def test_a_valid_short_report_reports_no_problems(short_run):
    result = generate_short(short_run)
    with open(os.path.join(short_run, "findings.json"),
              encoding="utf-8") as handle:
        findings = json.load(handle)
    assert validate_short(result["docx"],
                          expected_headings_for_short(findings)) == []


def test_prose_creeping_back_fails_validation(short_run, tmp_path):
    result = generate_short(short_run)
    document = Document(result["docx"])
    for paragraph in document.paragraphs:
        if paragraph.text.startswith("The site scores"):
            paragraph.text = ("The site scores well. It could score better. "
                              "There is a lot to say about that. And more.")
            break
    path = str(tmp_path / "wordy.docx")
    document.save(path)

    with open(os.path.join(short_run, "findings.json"),
              encoding="utf-8") as handle:
        findings = json.load(handle)
    problems = validate_short(path, expected_headings_for_short(findings))
    assert any("runs to prose" in p for p in problems)


def test_the_word_ceiling_is_enforced(short_run, tmp_path):
    result = generate_short(short_run)
    document = Document(result["docx"])
    document.add_paragraph(" ".join(["word"] * (SHORT_MAX_WORDS + 10)))
    path = str(tmp_path / "long.docx")
    document.save(path)

    with open(os.path.join(short_run, "findings.json"),
              encoding="utf-8") as handle:
        findings = json.load(handle)
    problems = validate_short(path, expected_headings_for_short(findings))
    assert any("over the" in p and "limit" in p for p in problems)


def test_a_count_without_a_whole_fails_validation(short_run, tmp_path):
    result = generate_short(short_run)
    document = Document(result["docx"])
    table = section_tables(document)["On page"]
    table.rows[1].cells[1].text = "3 pages"
    path = str(tmp_path / "nowhole.docx")
    document.save(path)

    with open(os.path.join(short_run, "findings.json"),
              encoding="utf-8") as handle:
        findings = json.load(handle)
    problems = validate_short(path, expected_headings_for_short(findings))
    assert any("states no whole" in p for p in problems)


def test_a_fix_without_an_action_fails_validation(short_run, tmp_path):
    result = generate_short(short_run)
    document = Document(result["docx"])
    table = tables_of(document, FIXES_COLUMNS)[0]
    table.rows[1].cells[5].text = ""
    path = str(tmp_path / "noaction.docx")
    document.save(path)

    with open(os.path.join(short_run, "findings.json"),
              encoding="utf-8") as handle:
        findings = json.load(handle)
    problems = validate_short(path, expected_headings_for_short(findings))
    assert any("no action" in p for p in problems)


def test_generate_short_raises_when_validation_fails(short_run, monkeypatch):
    monkeypatch.setattr("seo_audit.report.validate_short",
                        lambda *a, **k: ["something is wrong"])
    with pytest.raises(ValidationError, match="something is wrong"):
        generate_short(short_run)


def test_a_run_folder_without_findings_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="findings.json"):
        generate_short(str(tmp_path))


# --- what the client read caught --------------------------------------------

def test_the_lead_sentence_names_the_most_serious_not_the_largest(short_doc):
    """It said "the largest" and named the first row, which is the worst.

    On page that read "the largest is page title missing, 3 of 842" while 280
    pages had images with no description. A claim a reader can check has to
    be true.
    """
    document, _result = short_doc
    tables = section_tables(document)
    leads = [p.text.strip() for p in document.paragraphs
             if p.text.strip().endswith(".")
             and "findings here" in p.text]

    assert leads, "no section lead sentences"
    for lead in leads:
        assert "largest" not in lead, lead
        assert "the most serious first" in lead, lead

    on_page = tables["On page"]
    first_finding = on_page.rows[1].cells[0].text.strip()
    first_count = on_page.rows[1].cells[1].text.strip()
    lead = [text for text in leads if first_finding.lower() in text.lower()]
    assert lead, f"no lead names {first_finding!r}"
    assert first_count in lead[0]
    # And the row it names really is the most serious in that table.
    severities = [row.cells[2].text.strip() for row in on_page.rows[1:]
                  if row.cells[0].text.strip() != IN_PLACE]
    assert severities[0] == "high"
