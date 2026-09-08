"""Search Console mode: reading an export, and joining it to a crawl.

Every fixture here is a file on disk, because that is what a client sends:
a zip of CSVs whose headers depend on their locale and whose numbers depend
on their spreadsheet. The join is the point, so most of these tests are about
an address in the export finding the crawl row it belongs to.
"""

import csv
import io
import json
import os
import zipfile

import pytest

from seo_audit import gsc
from seo_audit.findings import GSC_WHOLES, build_findings
from seo_audit.issues import (PAGE_ISSUE_ACTION, PAGE_ISSUE_META,
                              PAGE_ISSUE_SEVERITY)
from test_findings import make_run

HOST = "https://example.com"
GSC_TYPES = ("gsc_impressions_on_noindex", "gsc_impressions_on_off_canonical",
             "gsc_sitemap_page_no_impressions", "gsc_orphan_page_with_clicks",
             "gsc_query_cannibalised")

PAGE_COLUMNS = ["url", "final_url", "status_code", "in_sitemap", "inlinks",
                "canonical", "canonical_is_self", "word_count",
                "schema_block_count", "has_microdata", "score"]


def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def crawled_run(tmp_path, pages=None, issues=None, name="20260101-000000"):
    """A finished run folder with the page columns the join reads."""
    run = make_run(tmp_path, name)
    pages = pages if pages is not None else [
        {"url": f"{HOST}/a/", "final_url": f"{HOST}/a/", "status_code": "200",
         "in_sitemap": "True", "inlinks": "4", "canonical": f"{HOST}/a/",
         "canonical_is_self": "True", "word_count": "400",
         "schema_block_count": "1", "has_microdata": "False", "score": "90"},
    ]
    write_csv(os.path.join(run, "audit_pages.csv"), PAGE_COLUMNS, pages)
    write_csv(os.path.join(run, "page_issues.csv"),
              ["issue_type", "severity", "url", "final_url", "detail",
               "site_level"], issues or [])
    build_findings(run, compare_enabled=False)
    return run


def page(path, *, sitemap="True", inlinks="4", final=None):
    url = f"{HOST}{path}"
    return {"url": url, "final_url": final or url, "status_code": "200",
            "in_sitemap": sitemap, "inlinks": inlinks, "canonical": url,
            "canonical_is_self": "True", "word_count": "400",
            "schema_block_count": "1", "has_microdata": "False",
            "score": "90"}


def export_zip(tmp_path, tables, name="export.zip"):
    """A zip shaped like the real thing: one CSV per table."""
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w") as archive:
        for filename, rows in tables.items():
            buffer = io.StringIO()
            csv.writer(buffer, lineterminator="\n").writerows(rows)
            archive.writestr(filename, buffer.getvalue())
    return path


PAGES_TABLE = [["Top pages", "Clicks", "Impressions", "CTR", "Position"]]
QUERIES_TABLE = [["Top queries", "Clicks", "Impressions", "CTR", "Position"]]


# --- reading what a client sends --------------------------------------------

def test_a_zip_export_is_read(tmp_path):
    path = export_zip(tmp_path, {
        "Pages.csv": PAGES_TABLE + [[f"{HOST}/a/", 12, 400, "3%", 4.2]],
        "Queries.csv": QUERIES_TABLE + [["office space", 9, 300, "3%", 6.1]],
        "Dates.csv": [["Date", "Clicks", "Impressions"],
                      ["2026-01-01", 5, 50], ["2026-06-30", 7, 90]]})
    export = gsc.read_export(path)

    assert [row["url"] for row in export.pages] == [f"{HOST}/a/"]
    assert export.pages[0]["clicks"] == 12
    assert export.pages[0]["impressions"] == 400
    assert [row["query"] for row in export.queries] == ["office space"]
    assert export.date_range == "2026-01-01 to 2026-06-30"


def test_a_folder_of_csvs_is_read(tmp_path):
    folder = tmp_path / "unzipped"
    folder.mkdir()
    (folder / "Pages.csv").write_text(
        "Top pages,Clicks,Impressions,CTR,Position\n"
        f"{HOST}/a/,3,90,3.33%,7.7\n", encoding="utf-8")
    export = gsc.read_export(str(folder))

    assert len(export.pages) == 1 and export.pages[0]["clicks"] == 3
    assert export.date_range == gsc.NO_RANGE
    # The note now tells the client what to do about it.
    assert any("16 months" in note for note in export.notes)


def test_a_single_csv_is_read(tmp_path):
    path = tmp_path / "Queries.csv"
    path.write_text("Top queries,Clicks,Impressions,CTR,Position\n"
                    "day pass pune,4,120,3.33%,11.2\n", encoding="utf-8")
    export = gsc.read_export(str(path))
    assert export.queries[0]["query"] == "day pass pune"
    assert export.queries[0]["position"] == 11.2


def test_headers_in_another_language_still_find_the_columns(tmp_path):
    """A German export names nothing the way the English one does."""
    path = export_zip(tmp_path, {
        "Seiten.csv": [["Häufigste Seiten", "Klicks", "Impressionen",
                        "Klickrate", "Position"],
                       [f"{HOST}/a/", "12", "400", "3,0 %", "4,2"]],
        "Suchanfragen.csv": [["Häufigste Suchanfragen", "Klicks",
                              "Impressionen", "Klickrate", "Position"],
                             ["büro mieten", "9", "300", "3,0 %", "6,1"]]})
    export = gsc.read_export(path)

    assert export.pages[0]["url"] == f"{HOST}/a/"
    assert export.pages[0]["impressions"] == 400
    assert export.pages[0]["position"] == 4.2
    assert export.queries[0]["query"] == "büro mieten"


def test_numbers_written_the_european_way_are_read_the_same(tmp_path):
    path = tmp_path / "Pages.csv"
    path.write_text("Top pages;Clicks;Impressions;CTR;Position\n"
                    f"{HOST}/a/;1.234;45.678;3,45 %;12,3\n",
                    encoding="utf-8")
    export = gsc.read_export(str(path))

    assert export.pages[0]["clicks"] == 1234
    assert export.pages[0]["impressions"] == 45678
    assert export.pages[0]["position"] == 12.3
    # The rate column said "3,45 %" and was ignored: 1234 of 45678 is 2.7%.
    assert export.pages[0]["ctr_percent"] == 2.7


def test_a_table_nobody_can_read_is_said_out_loud(tmp_path):
    path = tmp_path / "notes.csv"
    path.write_text("Some,Other,Thing\n1,2,3\n", encoding="utf-8")
    export = gsc.read_export(str(path))
    assert export.pages == [] and export.queries == []
    assert any("no pages or searches" in note for note in export.notes)


# --- the join ----------------------------------------------------------------

def test_an_address_matches_however_the_export_wrote_it(tmp_path):
    """Trailing slash, www and capitals are not different pages."""
    run = crawled_run(tmp_path, pages=[page("/a/"), page("/b/"), page("/c/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        [f"{HOST}/a", 1, 100, "1%", 5.0],            # no trailing slash
        ["https://www.example.com/b/", 2, 200, "1%", 5.0],   # www
        ["https://EXAMPLE.COM/c/", 3, 300, "1%", 5.0],       # capitals
    ]})
    joined = gsc.join(run, gsc.read_export(path))

    assert len(joined["by_final_url"]) == 3, joined["unseen"]
    assert set(joined["by_final_url"]) == {f"{HOST}/a/", f"{HOST}/b/",
                                           f"{HOST}/c/"}
    assert joined["unseen"] == []


def test_an_address_that_redirected_matches_the_page_it_landed_on(tmp_path):
    """The export knows the address people asked for, not where it ended."""
    landed = {"url": f"{HOST}/old", "final_url": f"{HOST}/new/",
              "status_code": "200", "in_sitemap": "True", "inlinks": "3",
              "canonical": f"{HOST}/new/", "canonical_is_self": "True",
              "word_count": "400", "schema_block_count": "1",
              "has_microdata": "False", "score": "90"}
    run = crawled_run(tmp_path, pages=[landed])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        [f"{HOST}/old", 5, 500, "1%", 3.0]]})

    joined = gsc.join(run, gsc.read_export(path))
    assert list(joined["by_final_url"]) == [f"{HOST}/new/"]


def test_pages_the_crawl_never_saw_are_counted_not_dropped(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        [f"{HOST}/a/", 1, 100, "1%", 5.0],
        [f"{HOST}/ghost/", 2, 200, "1%", 5.0]]})

    block, _rows, _matched = gsc.search_performance(run, gsc.read_export(path))
    assert block["join"]["matched"] == {"count": 1, "whole": 2,
                                        "whole_is": "pages in the export"}
    assert block["join"]["not_crawled"]["count"] == 1
    assert block["join"]["crawled_not_in_export"]["whole"] == 1


def test_every_share_in_the_block_states_its_whole(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        [f"{HOST}/a/", 1, 100, "1%", 5.0]]})
    block, _rows, _matched = gsc.search_performance(run, gsc.read_export(path))

    for name, share in block["join"].items():
        assert share["whole"], f"{name} has no whole"
        assert share["whole_is"], f"{name} does not say what its whole is"
    assert block["date_range"], "the period the numbers cover is not stated"


# --- what the two sides together find ---------------------------------------

def joined_run(tmp_path, name="20260101-000000"):
    """One page of each kind the join has something to say about."""
    pages = [
        page("/hidden/"),                       # noindex, gets impressions
        page("/elsewhere/"),                    # canonical points away
        page("/quiet/", sitemap="True"),        # in the sitemap, never shown
        page("/orphan/", sitemap="False", inlinks="0"),   # no links, clicks
        page("/normal/"),
    ]
    issues = [
        {"issue_type": "noindex_page", "severity": "high",
         "url": f"{HOST}/hidden/", "final_url": f"{HOST}/hidden/",
         "detail": "noindex", "site_level": "False"},
        {"issue_type": "canonical_off_page", "severity": "medium",
         "url": f"{HOST}/elsewhere/", "final_url": f"{HOST}/elsewhere/",
         "detail": "points at /normal/", "site_level": "False"},
        {"issue_type": "orphan_page", "severity": "medium",
         "url": f"{HOST}/orphan/", "final_url": f"{HOST}/orphan/",
         "detail": "no inlinks", "site_level": "False"},
    ]
    run = crawled_run(tmp_path, pages=pages, issues=issues, name=name)
    path = export_zip(tmp_path, {
        "Pages.csv": PAGES_TABLE + [
            [f"{HOST}/hidden/", 3, 900, "0.3%", 8.0],
            [f"{HOST}/elsewhere/", 4, 800, "0.5%", 9.0],
            [f"{HOST}/orphan/", 11, 700, "1.5%", 6.0],
            [f"{HOST}/normal/", 20, 600, "3.3%", 4.5]],
        "Queries by page.csv": [
            ["Top queries", "Top pages", "Clicks", "Impressions", "CTR",
             "Position"],
            ["office space", f"{HOST}/normal/", 10, 300, "3%", 4.0],
            ["office space", f"{HOST}/elsewhere/", 2, 100, "2%", 9.0],
            ["day pass", f"{HOST}/normal/", 5, 200, "2.5%", 12.0]]})
    return run, path


def test_each_kind_of_search_finding_is_recorded(tmp_path):
    run, path = joined_run(tmp_path)
    block, rows, _matched = gsc.search_performance(run,
                                                   gsc.read_export(path))
    counts = block["issue_counts"]

    assert counts["gsc_impressions_on_noindex"] == 1
    assert counts["gsc_impressions_on_off_canonical"] == 1
    assert counts["gsc_orphan_page_with_clicks"] == 1
    # /quiet/ is in the sitemap and absent from the export.
    assert counts["gsc_sitemap_page_no_impressions"] == 1
    assert counts["gsc_query_cannibalised"] == 1
    assert len(rows) == sum(counts.values())
    assert all(row["severity"] for row in rows)


def test_the_near_miss_band_is_position_four_to_fifteen(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {
        "Pages.csv": PAGES_TABLE + [[f"{HOST}/a/", 1, 10, "1%", 2.0]],
        "Queries.csv": QUERIES_TABLE + [
            ["winning", 50, 500, "10%", 1.4],
            ["nearly", 5, 900, "0.5%", 6.8],
            ["also nearly", 1, 400, "0.2%", 14.9],
            ["nowhere", 0, 80, "0%", 44.0]]})

    block, _rows, _m = gsc.search_performance(run, gsc.read_export(path))
    assert [row["query"] for row in block["near_miss_queries"]] == [
        "nearly", "also nearly"]


def test_cannibalisation_is_left_unanswered_when_the_export_cannot_say(
        tmp_path):
    """A plain export pairs no search with a page, and says so."""
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {
        "Pages.csv": PAGES_TABLE + [[f"{HOST}/a/", 1, 10, "1%", 2.0]],
        "Queries.csv": QUERIES_TABLE + [["office", 1, 10, "1%", 2.0]]})

    block, rows, _m = gsc.search_performance(run, gsc.read_export(path))
    assert block["cannibalisation_available"] is False
    assert block["cannibalised_queries"] == []
    assert not [row for row in rows
                if row["issue_type"] == "gsc_query_cannibalised"]


# --- writing it back ---------------------------------------------------------

def test_attaching_writes_the_join_the_block_and_the_rows(tmp_path):
    run, path = joined_run(tmp_path)
    before = os.path.getmtime(os.path.join(run, "crawl_summary.json"))
    result = gsc.attach(run, path)

    join_rows = list(csv.DictReader(
        open(os.path.join(run, gsc.JOIN_FILE), encoding="utf-8-sig")))
    assert len(join_rows) == result["matched"]
    assert set(join_rows[0]) >= {"final_url", "clicks", "impressions"}

    findings = json.load(open(os.path.join(run, "findings.json"),
                              encoding="utf-8"))
    assert findings["search_performance"]["provided"] is True
    assert findings["meta"]["mode"] == "full"
    assert os.path.getmtime(os.path.join(run, "crawl_summary.json")) == before


def test_attaching_twice_replaces_rather_than_repeats(tmp_path):
    run, path = joined_run(tmp_path)
    gsc.attach(run, path)
    first = json.load(open(os.path.join(run, "findings.json"),
                           encoding="utf-8"))["search_performance"]
    gsc.attach(run, path)

    issues = list(csv.DictReader(
        open(os.path.join(run, "page_issues.csv"), encoding="utf-8-sig")))
    gsc_rows = [row for row in issues
                if row["issue_type"].startswith("gsc_")]
    assert len(gsc_rows) == sum(first["issue_counts"].values())

    findings = json.load(open(os.path.join(run, "findings.json"),
                              encoding="utf-8"))
    assert isinstance(findings["search_performance"], dict)
    assert findings["search_performance"]["issue_counts"] == \
        first["issue_counts"]


def test_the_command_runs_end_to_end(tmp_path, capsys):
    run, path = joined_run(tmp_path)
    assert gsc.main(["--run", run, "--export", path]) == 0
    assert "matched" in capsys.readouterr().err


def test_the_command_refuses_a_folder_that_is_not_a_run(tmp_path, capsys):
    assert gsc.main(["--run", str(tmp_path), "--export", str(tmp_path)]) == 1
    assert "audit_pages.csv" in capsys.readouterr().err


# --- the rest of the audit notices -------------------------------------------

def test_search_findings_reach_the_priority_fixes_with_their_wholes(tmp_path):
    run, path = joined_run(tmp_path)
    gsc.attach(run, path)
    findings = json.load(open(os.path.join(run, "findings.json"),
                              encoding="utf-8"))

    fixes = {fix["issue_type"]: fix for fix in findings["prioritised_fixes"]}
    assert "gsc_impressions_on_noindex" in fixes
    share = fixes["gsc_impressions_on_noindex"]["pages_affected"]
    assert share["whole_is"] == "pages that appeared in search"
    assert share["count"] <= share["whole"]
    assert fixes["gsc_impressions_on_noindex"]["label"]


def test_no_search_findings_without_an_export(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/")])
    findings = json.load(open(os.path.join(run, "findings.json"),
                              encoding="utf-8"))

    assert findings["search_performance"] == {"provided": False}
    assert findings["meta"]["mode"] != "full"
    assert not [fix for fix in findings["prioritised_fixes"]
                if fix["issue_type"].startswith("gsc_")]


def test_the_comparison_says_when_the_previous_run_had_no_export(tmp_path):
    old = crawled_run(tmp_path, pages=[page("/a/")], name="20260101-000000")
    new_run, path = joined_run(tmp_path, name="20260102-000000")

    gsc.attach(new_run, path)
    findings = json.load(open(os.path.join(new_run, "findings.json"),
                              encoding="utf-8"))
    search = (findings["comparison"] or {}).get("search_performance") or {}

    assert search.get("available") is False
    assert search.get("note") == "not in previous run"
    assert old


# --- the registry ------------------------------------------------------------

def test_every_search_type_is_registered_with_every_field():
    for issue_type in GSC_TYPES:
        assert issue_type in PAGE_ISSUE_SEVERITY, issue_type
        label, finding, unit = PAGE_ISSUE_META[issue_type]
        assert label and "_" not in label
        assert "_" not in finding.replace("{count}", "").replace("{whole}", "")
        assert unit in ("page", "group")
        assert PAGE_ISSUE_ACTION[issue_type].endswith(".")
        assert issue_type in GSC_WHOLES


def test_a_search_finding_never_moves_a_page_score():
    """These describe what search did, not what is wrong with the page."""
    from seo_audit.scoring import load_weights, score_page

    weights = load_weights()
    for issue_type in GSC_TYPES:
        assert weights.page_issues[issue_type]["points"] == 0, issue_type
    assert score_page(list(GSC_TYPES), weights).score == 100


# --- session 13: what a real export from either property type does ----------

def test_a_domain_property_export_has_no_scheme_and_still_matches(tmp_path):
    """Search Console holds a domain property as example.com/page."""
    run = crawled_run(tmp_path, pages=[page("/a/"), page("/b/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        ["example.com/a/", 5, 200, "2.5%", 4.0],
        ["www.example.com/b", 3, 150, "2%", 6.0]]})

    joined = gsc.join(run, gsc.read_export(path))
    assert set(joined["by_final_url"]) == {f"{HOST}/a/", f"{HOST}/b/"}
    assert joined["unseen"] == []
    assert joined["scheme_less"] == 2


def test_the_count_of_scheme_less_addresses_is_recorded(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/"), page("/b/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        ["example.com/a/", 5, 200, "2.5%", 4.0],
        [f"{HOST}/b/", 3, 150, "2%", 6.0]]})

    block, _rows, _m = gsc.search_performance(run, gsc.read_export(path))
    share = block["property"]["addresses_without_a_scheme"]
    assert share == {"count": 1, "whole": 2,
                     "whole_is": "pages in the export"}
    assert block["property"]["mismatched"] is False


def test_a_site_that_answers_on_http_is_still_matched(tmp_path):
    """The export does not say which scheme the site uses; the crawl does."""
    plain = {"url": "http://example.com/a/", "final_url": "http://example.com/a/",
             "status_code": "200", "in_sitemap": "True", "inlinks": "2",
             "canonical": "http://example.com/a/", "canonical_is_self": "True",
             "word_count": "400", "schema_block_count": "1",
             "has_microdata": "False", "score": "90"}
    run = crawled_run(tmp_path, pages=[plain])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        ["example.com/a/", 1, 20, "5%", 3.0]]})

    joined = gsc.join(run, gsc.read_export(path))
    assert list(joined["by_final_url"]) == ["http://example.com/a/"]


def test_an_export_for_another_property_says_so(tmp_path):
    """Nothing matching is not the same as nobody searching."""
    run = crawled_run(tmp_path, pages=[page("/a/"), page("/b/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        ["https://other-client.com/a/", 5, 200, "2%", 4.0],
        ["https://other-client.com/b/", 3, 150, "2%", 6.0]]})

    block, _rows, _m = gsc.search_performance(run, gsc.read_export(path))
    property_note = block["property"]

    assert property_note["mismatched"] is True
    assert property_note["export_host"] == "other-client.com"
    assert property_note["crawl_host"] == "example.com"
    assert property_note["note"] == ("the export covers other-client.com "
                                     "while the audit crawled example.com, "
                                     "so almost none of it can be matched")
    assert property_note["note"] in block["coverage_note"].lower()


def test_a_matching_export_carries_no_mismatch_note(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        [f"{HOST}/a/", 5, 200, "2%", 4.0]]})

    block, _rows, _m = gsc.search_performance(run, gsc.read_export(path))
    assert block["property"]["mismatched"] is False
    assert block["property"]["note"] == ""


def test_the_rate_is_computed_from_clicks_and_impressions(tmp_path):
    """Whatever the export's own rate column says, it is not read."""
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        [f"{HOST}/a/", 5, 200, "90%", 4.0]],
        "Queries.csv": QUERIES_TABLE + [["office space", 1, 8, "0.9", 5.0]]})
    export = gsc.read_export(path)

    assert export.pages[0]["ctr_percent"] == 2.5
    assert export.queries[0]["ctr_percent"] == 12.5
    assert gsc.rate(0, 0) == 0.0
    assert not hasattr(gsc, "parse_rate")

    block, _rows, _m = gsc.search_performance(run, export)
    assert block["ctr_source"] == "computed"


def test_the_search_totals_are_marked_as_a_floor(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        [f"{HOST}/a/", 5, 200, "2%", 4.0]],
        "Queries.csv": QUERIES_TABLE + [["office space", 5, 200, "2%", 4.0]]})

    block, _rows, _m = gsc.search_performance(run, gsc.read_export(path))
    assert "floor" in block["floor_note"]
    assert block["wholes_are"]["queries"] == "searches Google reported"


def test_an_export_without_dates_asks_for_one_with_them(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {"Pages.csv": PAGES_TABLE + [
        [f"{HOST}/a/", 5, 200, "2%", 4.0]]})

    export = gsc.read_export(path)
    assert export.has_period is False
    assert "16 months" in export.coverage_note
    block, _rows, _m = gsc.search_performance(run, export)
    assert block["has_period"] is False
    assert "16 months" in block["coverage_note"]


def test_an_export_with_dates_states_its_period(tmp_path):
    run = crawled_run(tmp_path, pages=[page("/a/")])
    path = export_zip(tmp_path, {
        "Pages.csv": PAGES_TABLE + [[f"{HOST}/a/", 5, 200, "2%", 4.0]],
        "Dates.csv": [["Date", "Clicks", "Impressions"],
                      ["2025-05-01", 1, 10], ["2026-08-31", 2, 20]]})

    block, _rows, _m = gsc.search_performance(run, gsc.read_export(path))
    assert block["has_period"] is True
    assert block["date_range"] == "2025-05-01 to 2026-08-31"
    assert "16 months" not in block["coverage_note"]
