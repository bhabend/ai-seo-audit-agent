"""The deliverable: a validated Word document built from a finished run.

Numbers come from code, prose comes from the model, and the document is
reopened and checked before this command will exit zero. A file that opens is
not a file that is right, so validation is part of producing it rather than
something a human does afterwards.

Every chart that shows a part of something is a pie whose title says what the
whole is, in words. That is the whole point of the share objects in
findings.json: a percentage with no stated denominator is how a capped sample
gets read as a fact about a site.

    python -m seo_audit.report --run output/example.com/20260101-120000
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .issues import label_of, unit_of
from .narrative import Narrator, strip_dashes

# Section order in the document. The key is the findings section it reads.
SECTIONS: List[Tuple[str, str]] = [
    ("crawlability", "Crawlability and sitemap"),
    ("indexability_technical", "Indexability and technical"),
    ("on_page", "On page"),
    ("content", "Content"),
    ("schema", "Structured data"),
    ("links", "Internal and external links"),
    ("performance", "Performance"),
]

CHART_WIDTH_INCHES = 5.8
MAX_DOC_BYTES = 2 * 1024 * 1024
TOP_FIXES = 10
APPENDIX_ROWS = 20

# Real unfilled placeholders. A bare "{" is not one: template names such as
# "/workspaces/{slug}" are data this product produces on purpose.
PLACEHOLDERS = ("TODO", "lorem ipsum", "{{", "}}", "{0}", "{}")

# Written by code, after the executive summary, so the narrative never has to
# stop and define a word mid sentence.
GLOSSARY = [
    ("Sitemap", "A file listing the pages a site wants search engines to "
     "know about."),
    ("Robots file", "A file at the root of a site telling search engines "
     "which addresses they may fetch."),
    ("Redirect", "An instruction sending a visitor from one address to "
     "another."),
    ("Preferred address", "The address a page names as the one it wants "
     "search engines to list, also called a canonical."),
    ("Hidden from search", "A page carrying an instruction that asks search "
     "engines not to list it, also called noindex."),
    ("Orphan page", "A page no other page on the site links to."),
    ("Internal link", "A link from one page of the site to another."),
    ("Structured data", "Extra machine readable notes in a page describing "
     "what it is about, used for richer search results."),
    ("Schema", "The shared vocabulary structured data is written in."),
    ("Language version", "An alternate page for another language or region, "
     "declared with hreflang."),
    ("Secure connection enforced", "A setting telling browsers to reach the "
     "site only over a secure connection, also called HSTS."),
    ("Core Web Vitals", "Google's three measures of how a page feels to "
     "use: loading, stability and responsiveness."),
    ("Largest contentful paint", "How long the main thing on the page takes "
     "to appear."),
    ("Cumulative layout shift", "How much the page moves about while it "
     "loads."),
    ("Interaction to next paint", "How quickly the page responds when "
     "someone taps or clicks."),
    ("Template", "A page layout shared by many pages, such as every blog "
     "post or every location page."),
]


# --- charts -----------------------------------------------------------------

def _pie(slices: List[Tuple[str, float]], title: str):
    """One pie, drawn to memory. Returns None when there is nothing to draw."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    slices = [(label, float(value)) for label, value in slices
              if value and float(value) > 0]
    if not slices:
        return None

    figure, axes = plt.subplots(figsize=(5.6, 3.6))
    values = [v for _l, v in slices]
    labels = [l for l, _v in slices]
    axes.pie(values, labels=labels, autopct="%1.0f%%", startangle=90,
             counterclock=False,
             textprops={"fontsize": 9})
    axes.axis("equal")
    axes.set_title(title, fontsize=10)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=150, bbox_inches="tight")
    plt.close(figure)
    buffer.seek(0)
    return buffer


def _barh(rows: List[Tuple[str, float]], title: str, xlabel: str):
    """The one non share chart: template scores, which are not parts of a whole."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = [(str(l), float(v)) for l, v in rows if v is not None][:12]
    if not rows:
        return None
    rows.reverse()

    figure, axes = plt.subplots(figsize=(6.0, max(2.4, 0.32 * len(rows) + 1)))
    axes.barh([l for l, _v in rows], [v for _l, v in rows])
    axes.set_xlim(0, 100)
    axes.set_xlabel(xlabel, fontsize=9)
    axes.set_title(title, fontsize=10)
    axes.tick_params(labelsize=8)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=150, bbox_inches="tight")
    plt.close(figure)
    buffer.seek(0)
    return buffer


def pie_title(label: str, whole: int, whole_is: str) -> str:
    """Every pie says its whole, in words, so no share can float free."""
    return f"{label}, of {whole} {whole_is}"


def _sitemap_title(whole: int, crawled: int, not_crawled: int) -> str:
    """Spell the whole out: two different things are being added together."""
    word = "URL" if not_crawled == 1 else "URLs"
    return (f"Sitemap coverage, of {whole} URLs seen ({crawled} crawled plus "
            f"{not_crawled} sitemap {word} not crawled)")


def build_charts(findings: Dict) -> Dict[str, Any]:
    """Every chart the document can carry, keyed by the section it belongs to."""
    charts: Dict[str, Any] = {}

    headline = findings.get("headline") or {}
    distribution = headline.get("distribution") or {}
    scored = headline.get("pages_scored", 0)
    if distribution and scored:
        charts["headline"] = (
            _pie([(band, obj["count"]) for band, obj in distribution.items()],
                 pie_title("Page scores", scored, "pages scored")),
            pie_title("Page scores", scored, "pages scored"))

    schema = (findings.get("schema") or {}).get("coverage") or {}
    if schema:
        whole = schema.get("json_ld", {}).get("whole", 0)
        charts["schema"] = (
            _pie([("JSON-LD", schema.get("json_ld", {}).get("count", 0)),
                  ("Microdata only",
                   schema.get("microdata_only", {}).get("count", 0)),
                  ("No structured data",
                   schema.get("none", {}).get("count", 0))],
                 pie_title("Structured data coverage", whole, "pages parsed")),
            pie_title("Structured data coverage", whole, "pages parsed"))

    links = findings.get("links") or {}
    orphans = links.get("orphan_pages") or {}
    if orphans.get("whole"):
        whole = orphans["whole"]
        charts["links"] = (
            _pie([("Linked from other pages", whole - orphans["count"]),
                  ("No internal links in", orphans["count"])],
                 pie_title("Pages reachable by internal links", whole,
                           "pages parsed")),
            pie_title("Pages reachable by internal links", whole,
                      "pages parsed"))

    crawl = findings.get("crawlability") or {}
    sitemap = crawl.get("sitemap") or {}
    both = sitemap.get("crawled_not_in_sitemap") or {}
    not_crawled = sitemap.get("in_sitemap_not_crawled") or {}
    found = (findings.get("meta") or {}).get("coverage", {}).get("pages_found", 0)
    in_both = max(0, found - both.get("count", 0))
    whole = in_both + both.get("count", 0) + not_crawled.get("count", 0)
    if whole:
        charts["crawlability"] = (
            _pie([("In the sitemap and crawled", in_both),
                  ("Crawled, not in the sitemap", both.get("count", 0)),
                  ("In the sitemap, not crawled",
                   not_crawled.get("count", 0))],
                 _sitemap_title(whole, found, not_crawled.get("count", 0))),
            _sitemap_title(whole, found, not_crawled.get("count", 0)))

    redirects = crawl.get("redirects") or {}
    total = (redirects.get("total") or {}).get("count", 0)
    slash = (redirects.get("trailing_slash") or {}).get("count", 0)
    if total:
        charts["crawlability_redirects"] = (
            _pie([("Missing trailing slash", slash),
                  ("Other redirects", max(0, total - slash))],
                 pie_title("Redirects by kind", total, "redirects")),
            pie_title("Redirects by kind", total, "redirects"))

    performance = findings.get("performance") or {}
    if not performance.get("skipped"):
        represented = performance.get("pages_represented") or {}
        whole = represented.get("whole", 0)
        if whole:
            charts["performance"] = (
                _pie([("Covered by a measured template",
                       represented.get("count", 0)),
                      ("Not measured",
                       max(0, whole - represented.get("count", 0)))],
                     pie_title("Performance coverage", whole,
                               "pages parsed")),
                pie_title("Performance coverage", whole, "pages parsed"))
        measured = performance.get("templates_measured") or []
        charts["performance_bar"] = (
            _barh([(m["template"], m["mobile_score"]) for m in measured
                   if m.get("mobile_score") is not None],
                  "Mobile performance score by template",
                  "Score out of 100"),
            "Mobile performance score by template")
    return {k: v for k, v in charts.items() if v[0] is not None}


# --- document ---------------------------------------------------------------

def _add_table(document, columns: List[str], rows: List[List[str]]):
    table = document.add_table(rows=1, cols=len(columns))
    table.style = "Light Grid Accent 1"
    for index, name in enumerate(columns):
        table.rows[0].cells[index].text = strip_dashes(str(name))
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = strip_dashes(str(value))
    return table


def _add_section_body(document, parts: Dict) -> None:
    """What we found, why it matters, what to do, as three visible parts.

    Session 8 rendered each section as one wall of text and every "what to do"
    ran into the paragraph before it.
    """
    found = (parts.get("found") or "").strip()
    why = (parts.get("why") or "").strip()
    todo = [t for t in (parts.get("todo") or []) if str(t).strip()]

    if found:
        paragraph = document.add_paragraph()
        paragraph.add_run("What we found. ").bold = True
        paragraph.add_run(strip_dashes(found))
    if why:
        paragraph = document.add_paragraph()
        paragraph.add_run("Why it matters. ").bold = True
        paragraph.add_run(strip_dashes(why))
    if todo:
        paragraph = document.add_paragraph()
        paragraph.add_run("What to do.").bold = True
        for item in todo:
            document.add_paragraph(strip_dashes(str(item)),
                                   style="List Number")


def _add_glossary(document) -> None:
    document.add_heading("Terms used in this report", level=1)
    document.add_paragraph(strip_dashes(
        "A few words appear throughout. They are set out here once so the "
        "sections that follow can get on with the findings."))
    for term, meaning in GLOSSARY:
        paragraph = document.add_paragraph()
        paragraph.add_run(f"{term}. ").bold = True
        paragraph.add_run(strip_dashes(meaning))


def _add_chart(document, chart):
    from docx.shared import Inches
    buffer, _title = chart
    document.add_picture(buffer, width=Inches(CHART_WIDTH_INCHES))


def _add_comparison(document, comparison: Dict, narrator: Narrator) -> None:
    """When coverage moved, this section is written by code, not the model.

    A sitemap that went from 106 URLs to 709 is the site changing, not the
    crawler wobbling, and counts either side of that are not comparable. A
    model asked to narrate it will reach for a story about decline, so it is
    not asked.
    """
    sitemap = (comparison.get("deltas") or {}).get("sitemap_urls_total") or {}
    coverage_changed = comparison.get("coverage_changed")

    if coverage_changed:
        if comparison.get("sitemap_changed"):
            document.add_paragraph(strip_dashes(
                f"The site's own sitemap changed between the two audits. It "
                f"listed {sitemap.get('previous')} addresses last time and "
                f"{sitemap.get('now')} this time. That is a change on the "
                f"site, not in how it was audited."))
        document.add_paragraph(strip_dashes(
            "Because the two audits saw different amounts of the site, the "
            "counts below are not comparable with each other. A number that "
            "looks higher or lower mostly reflects how many pages were "
            "reached, so this report draws no conclusion from it."))
        document.add_paragraph(strip_dashes(
            "Worth checking on your side: a sitemap that reports a different "
            "size from one fetch to the next usually points to a caching or "
            "generation problem, and it affects what search engines see too."))
    else:
        _add_section_body(document, narrator.write("comparison", comparison))

    notable = comparison.get("notable") or []
    if notable:
        _add_table(document,
                   ["Measure", "Previous", "Now", "Change", "Note"],
                   [[_metric_label(n["metric"]), n["previous"], n["now"],
                     n["change"], n.get("note", "")]
                    for n in notable[:APPENDIX_ROWS]])


def _metric_label(metric: str) -> str:
    """Plain names for the handful of metrics the comparison tracks."""
    names = {
        "pages_found": "addresses crawled",
        "pages_parsed": "pages read",
        "sitemap_urls_total": "addresses in the sitemap",
        "site_score": "overall score",
        "mean_mobile_score": "average mobile speed score",
        "orphan_pages": "pages with no links in",
        "broken_internal_targets": "broken internal addresses",
        "external_broken": "broken links to other sites",
    }
    return names.get(metric, metric.replace("_", " "))


def _coverage_paragraph(findings: Dict) -> str:
    meta = findings.get("meta") or {}
    coverage = meta.get("coverage") or {}
    caps = meta.get("caps_applied") or {}
    performance = findings.get("performance") or {}
    templates = performance.get("templates_sampled", 0)

    sentence = (
        f"This audit crawled {coverage.get('pages_found', 0)} URLs on "
        f"{meta.get('host', 'the site')} and read "
        f"{coverage.get('pages_parsed', 0)} of them as pages. The sitemap "
        f"listed {coverage.get('sitemap_urls_total', 0)} URLs. "
        f"The crawl stopped at {caps.get('max_pages')} pages and "
        f"{caps.get('max_depth')} clicks from the home page, checked at most "
        f"{caps.get('external_check_limit')} links to other websites, and "
        f"measured speed on {templates} page templates rather than every "
        f"page, because pages built from one template perform alike. "
        f"Every figure in this report says what it is a share of, so a "
        f"sampled number is never read as a total.")
    return strip_dashes(sentence)


SCOPE_WORDS = {"config": "one setting", "template": "one template",
               "page": "page by page"}


def _fixes_table_rows(fixes: List[Dict]) -> List[List[str]]:
    rows = []
    for fix in fixes[:TOP_FIXES]:
        affected = fix.get("pages_affected") or {}
        scale = (f"{affected.get('count', 0)} of {affected.get('whole', 0)} "
                 f"{affected.get('whole_is', 'pages')}")
        targets = fix.get("targets")
        if targets:
            # "118 of 842 pages" was 118 external links. Say which is which.
            scale += (f" ({targets['count']} of {targets['whole']} "
                      f"{targets['whole_is']})")
        rows.append([
            fix.get("title", ""),
            scale,
            fix.get("severity", ""),
            SCOPE_WORDS.get(fix.get("fix_scope", ""), fix.get("fix_scope", "")),
        ])
    return rows


def build_document(findings: Dict, narrator: Narrator, charts: Dict,
                   host: str) -> Any:
    from docx import Document

    document = Document()
    today = date.today().isoformat()

    document.add_heading(f"SEO Audit: {host}", level=0)
    document.add_paragraph(strip_dashes(
        f"Prepared {today}. Mode: "
        f"{(findings.get('meta') or {}).get('mode', 'baseline')}."))
    document.add_paragraph(_coverage_paragraph(findings))

    document.add_heading("Executive summary", level=1)
    _add_section_body(document, narrator.summary(
        findings.get("headline") or {},
        findings.get("prioritised_fixes") or []))
    if "headline" in charts:
        _add_chart(document, charts["headline"])

    _add_glossary(document)

    document.add_heading("Priority fixes", level=1)
    fixes = findings.get("prioritised_fixes") or []
    if fixes:
        _add_table(document, ["Fix", "Pages affected", "Severity", "Scope"],
                   _fixes_table_rows(fixes))
    _add_section_body(document, narrator.write("prioritised_fixes",
                                               fixes[:TOP_FIXES]))

    for key, heading in SECTIONS:
        section = findings.get(key)
        if section is None:
            continue
        document.add_heading(heading, level=1)
        _add_section_body(document, narrator.write(key, section))
        if key in charts:
            _add_chart(document, charts[key])
        if key == "crawlability" and "crawlability_redirects" in charts:
            _add_chart(document, charts["crawlability_redirects"])
        if key == "performance" and "performance_bar" in charts:
            _add_chart(document, charts["performance_bar"])

    comparison = findings.get("comparison") or {}
    if comparison.get("previous_run"):
        document.add_heading("Comparison with the previous audit", level=1)
        _add_comparison(document, comparison, narrator)

    document.add_heading("Search performance", level=1)
    document.add_paragraph(strip_dashes(
        "Search Console data was not provided for this audit, so this report "
        "does not cover what people searched for, how often the site "
        "appeared, or how often it was clicked. Supply a Search Console "
        "export and a later audit can include it."))

    document.add_heading("Appendix", level=1)
    links = findings.get("links") or {}
    broken = (links.get("broken_internal") or {}).get("groups") or []
    if broken:
        document.add_heading("Broken internal link groups", level=2)
        _add_table(document, ["Pattern", "Targets", "Links pointing there"],
                   [[g["pattern"], g["targets"], g["link_instances"]]
                    for g in broken[:APPENDIX_ROWS]])

    lowest = (findings.get("headline") or {}).get("lowest_pages") or []
    if lowest:
        document.add_heading("Lowest scoring pages", level=2)
        _add_table(document, ["Page", "Score", "Main issue"],
                   [[p["final_url"], p["score"],
                     str(p.get("top_issue") or "").replace("_", " ")]
                    for p in lowest[:APPENDIX_ROWS]])

    measured = (findings.get("performance") or {}).get("templates_measured")
    if measured:
        document.add_heading("Templates measured for speed", level=2)
        _add_table(document, ["Template", "Pages", "Mobile", "Desktop"],
                   [[m["template"], m["group_size"], m["mobile_score"],
                     m["desktop_score"]] for m in measured[:APPENDIX_ROWS]])
    return document


# --- validation -------------------------------------------------------------

class ValidationError(RuntimeError):
    """The document was written but is not fit to send."""


_BAD_DASH = re.compile(r"[‐-―−]|(?<=\S) - (?=\S)")
# A finished sentence. An action item that does not match this stopped early.
_ENDS_PROPERLY = re.compile(r"[.!?][\"')\]]?$")


def _sections_missing_parts(document) -> List[str]:
    """Narrative headings whose body lacks found, why or what to do."""
    narrative_headings = {title for _key, title in SECTIONS}
    narrative_headings.add("Executive summary")
    narrative_headings.add("Priority fixes")

    missing: List[str] = []
    current: Optional[str] = None
    seen: set = set()
    for paragraph in document.paragraphs:
        style = paragraph.style.name
        text = paragraph.text.strip()
        if style.startswith("Heading") or style == "Title":
            if current in narrative_headings and len(seen) < 3:
                missing.append(current)
            current = text if text in narrative_headings else None
            seen = set()
            continue
        if current is None:
            continue
        for part in ("What we found.", "Why it matters.", "What to do."):
            if text.startswith(part.rstrip(".")):
                seen.add(part)
    if current in narrative_headings and len(seen) < 3:
        missing.append(current)
    return missing


def validate(path: str, expected_headings: List[str],
             expected_images: int) -> List[str]:
    """Reopen the file and check it. Raises with the failing check named."""
    from docx import Document

    problems: List[str] = []
    document = Document(path)

    headings = [p.text.strip() for p in document.paragraphs
                if p.style.name.startswith("Heading") or p.style.name == "Title"]
    position = 0
    for wanted in expected_headings:
        while position < len(headings) and headings[position] != wanted:
            position += 1
        if position >= len(headings):
            problems.append(f"heading missing or out of order: {wanted!r}")
        position += 1

    images = len(document.inline_shapes)
    if images != expected_images:
        problems.append(f"image count is {images}, expected {expected_images}")

    texts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.append(cell.text)

    for text in texts:
        if _BAD_DASH.search(text):
            problems.append(f"dash used as punctuation: {text[:70]!r}")
            break
    for text in texts:
        lowered = text.lower()
        if any(p.lower() in lowered for p in PLACEHOLDERS):
            problems.append(f"placeholder text present: {text[:70]!r}")
            break

    # Every heading must be followed by something before the next heading.
    body = [(p.text.strip(),
             p.style.name.startswith("Heading") or p.style.name == "Title")
            for p in document.paragraphs]
    for index, (text, is_heading) in enumerate(body):
        if not is_heading:
            continue
        following = body[index + 1:]
        has_body = any(t and not h for t, h in
                       following[:6]) if following else False
        if not has_body and index < len(body) - 1:
            # A heading may be followed by a table or an image instead.
            if not document.tables and not document.inline_shapes:
                problems.append(f"empty section body under {text!r}")

    # Truncation. The real failure mode is a list that stopped mid item, so
    # the check is on the list items themselves rather than on any paragraph
    # ending in a digit: "Trailing slash: 0." is data, not a cut off list.
    for paragraph in document.paragraphs:
        if paragraph.style.name != "List Number":
            continue
        item = paragraph.text.strip()
        if item and not _ENDS_PROPERLY.search(item):
            problems.append(f"action item ends mid sentence: {item[-60:]!r}")
            break

    # "What to do." is a label this code writes; it must be followed by at
    # least one action.
    paragraphs = list(document.paragraphs)
    for index, paragraph in enumerate(paragraphs):
        if paragraph.text.strip() != "What to do.":
            continue
        following = paragraphs[index + 1:index + 2]
        if not following or following[0].style.name != "List Number":
            problems.append("a what to do heading has no actions under it")
            break

    # Every narrative section must carry all three parts.
    missing_parts = _sections_missing_parts(document)
    for heading in missing_parts:
        problems.append(f"section {heading!r} is missing a part")

    if os.path.getsize(path) > MAX_DOC_BYTES:
        problems.append(f"file is {os.path.getsize(path)} bytes, over the "
                        f"{MAX_DOC_BYTES} limit")
    return problems


def expected_headings_for(findings: Dict) -> List[str]:
    headings = ["Executive summary", "Terms used in this report",
                "Priority fixes"]
    headings += [title for key, title in SECTIONS if findings.get(key) is not None]
    if (findings.get("comparison") or {}).get("previous_run"):
        headings.append("Comparison with the previous audit")
    headings += ["Search performance", "Appendix"]
    return headings


# --- the command ------------------------------------------------------------

def generate(run_dir: str, model: str = "gpt-5-mini", use_ai: bool = True,
             session=None) -> Dict:
    """Build, write and validate the report for one finished run folder."""
    findings_path = os.path.join(run_dir, "findings.json")
    if not os.path.exists(findings_path):
        raise FileNotFoundError(
            f"no findings.json in {run_dir}; run the audit first")
    with open(findings_path, encoding="utf-8") as handle:
        findings = json.load(handle)

    host = (findings.get("meta") or {}).get("host") or "site"
    charts = build_charts(findings)
    narrator = Narrator(model=model, use_ai=use_ai, session=session)

    document = build_document(findings, narrator, charts, host)
    name = f"SEO_Audit_{host}_{date.today().isoformat()}.docx"
    path = os.path.join(run_dir, name)
    document.save(path)

    usage_path = os.path.join(run_dir, "report_usage.json")
    with open(usage_path, "w", encoding="utf-8") as handle:
        json.dump(narrator.usage.as_dict(), handle, indent=2,
                  ensure_ascii=False)

    problems = validate(path, expected_headings_for(findings), len(charts))
    if problems:
        raise ValidationError("; ".join(problems))

    return {"docx": path, "usage": usage_path, "charts": len(charts),
            "usage_data": narrator.usage.as_dict()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo-audit-report",
        description="Build a Word report from a finished audit run folder.")
    parser.add_argument("--run", required=True,
                        help="Run folder containing findings.json")
    parser.add_argument("--model", default="gpt-5-mini",
                        help="Model for the narrative (default gpt-5-mini). "
                             "At most 14 calls per report.")
    parser.add_argument("--no-ai", action="store_true",
                        help="Write the narrative from templates instead of "
                             "calling the model. No API calls, no cost.")
    return parser


def main(argv=None) -> int:
    from dotenv import load_dotenv

    args = build_parser().parse_args(argv)
    load_dotenv()

    try:
        result = generate(args.run, model=args.model, use_ai=not args.no_ai)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    usage = result["usage_data"]
    print(f"report   : {result['docx']}", file=sys.stderr)
    print(f"usage    : {result['usage']}", file=sys.stderr)
    print(f"charts   : {result['charts']}", file=sys.stderr)
    print(f"calls    : {usage['calls']} of {usage['max_calls']} "
          f"({usage['model']})", file=sys.stderr)
    print(f"tokens   : {usage['input_tokens']} in, "
          f"{usage['output_tokens']} out", file=sys.stderr)
    print(f"guarded  : {len(usage['guard_events'])} section(s) had a sentence "
          f"dropped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
