"""Search Console mode: a client's export, joined to a finished crawl.

    python -m seo_audit.gsc --run output/example.com/20260101-120000 \\
                            --export ~/Downloads/example-performance.zip

A separate step, deliberately. The crawl says what the site is; the export
says what search engines did with it, and the two are only worth anything
together: a page hidden from search that still collects impressions, a page
in the sitemap nobody has ever been shown, a page with no links in that earns
clicks anyway. None of that is knowable from either side alone.

Nothing here re crawls and nothing here modifies crawl_summary.json. The
export is read, joined to `audit_pages.csv` on the normalised address, and
written back as `gsc_join.csv`, a `search_performance` block in
`findings.json` and a handful of rows appended to `page_issues.csv`. Attach a
second export and the previous answer is replaced rather than added to.

Header names in a real export vary by locale and by which button produced it,
so tables are found by shape rather than by name: a column that looks like an
address, a column that looks like a query, and the click, impression, rate
and position columns beside them.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .urlnorm import normalize, slash_variant, strip_www

# Column names, in the languages a client is likely to send. Matched as
# substrings of the lowercased header, so "Top pages" and "Pages les plus
# populaires" both land on the address column.
PAGE_WORDS = ("page", "url", "address", "adresse", "seite", "página",
              "pagina", "sivu", "halaman")
QUERY_WORDS = ("query", "queries", "search term", "keyword", "requête",
               "requete", "consulta", "suchanfrage", "abfrage", "zoekopdracht",
               "kysely")
CLICK_WORDS = ("click", "clic", "klick", "cliques", "kliks", "napsauket")
IMPRESSION_WORDS = ("impression", "impresion", "impressionen", "impressões",
                    "impressioni", "vertoningen", "näyttökerrat")
CTR_WORDS = ("ctr", "click-through", "clickthrough", "click through",
             "taux de clic", "clickrate", "prozentsatz")
POSITION_WORDS = ("position", "posición", "posicion", "posizione", "posição",
                  "positie", "sijainti", "rank")
DATE_WORDS = ("date", "datum", "fecha", "data", "päivämäärä")

TOP_ROWS = 20
NEAR_MISS_LOW, NEAR_MISS_HIGH = 4.0, 15.0
NO_RANGE = "not stated in export"
GSC_PREFIX = "gsc_"
JOIN_FILE = "gsc_join.csv"
JOIN_COLUMNS = ["final_url", "gsc_url", "clicks", "impressions", "ctr_percent",
                "position", "in_sitemap", "inlinks", "hidden_from_search",
                "preferred_address_elsewhere"]


# --- reading the export -----------------------------------------------------

def _clean_header(cell: str) -> str:
    return str(cell or "").replace("﻿", "").strip().lower()


def _matches(header: str, words: Sequence[str]) -> bool:
    return any(word in header for word in words)


def _find_column(headers: List[str], words: Sequence[str]) -> Optional[int]:
    for index, header in enumerate(headers):
        if _matches(header, words):
            return index
    return None


_NUMBER_CHARS = re.compile(r"[^\d,.\-]")


_GROUPED = re.compile(r"^\d{1,3}([.,]\d{3})+$")


def parse_number(text: Any, whole: bool = False) -> float:
    """A number as a client's spreadsheet wrote it.

    Handles thousands separators, decimal commas and percentage signs. When a
    value carries both separators the last one is the decimal point, which is
    true in every locale that uses both.

    One separator alone is ambiguous: "1.234" is a thousand clicks in Berlin
    and one and a bit in London. `whole` says which column this is. Clicks
    and impressions are counted things, so their separators group thousands;
    a position or a rate is not, so its separator is a decimal point.
    """
    if isinstance(text, (int, float)):
        return float(text)
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    if whole and _GROUPED.match(raw.replace(" ", "")):
        return float(re.sub(r"[.,]", "", raw.replace(" ", "")))
    cleaned = _NUMBER_CHARS.sub("", raw.replace(" ", " ").replace(" ", ""))
    if not cleaned or cleaned in ("-", ".", ","):
        return 0.0

    last_comma, last_dot = cleaned.rfind(","), cleaned.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        decimal_at = max(last_comma, last_dot)
        keep = cleaned[decimal_at]
        drop = "," if keep == "." else "."
        cleaned = cleaned.replace(drop, "").replace(keep, ".")
    elif last_comma >= 0:
        tail = cleaned[last_comma + 1:]
        # "1,234" is a thousand; "3,45" is three and a half.
        cleaned = (cleaned.replace(",", ".") if len(tail) in (1, 2)
                   else cleaned.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_rate(text: Any) -> float:
    """A click through rate as a percentage, however it was written.

    "3.4%" and "3,4 %" are 3.4. A bare 0.034 is also 3.4: no real page has a
    rate of three hundredths of one percent alongside four figure
    impressions, so a value at or under one is read as a fraction.
    """
    raw = str(text or "")
    value = parse_number(raw)
    if "%" in raw:
        return round(value, 2)
    return round(value * 100, 2) if 0 < value <= 1 else round(value, 2)


def _read_csv_bytes(data: bytes) -> List[List[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4000]
    delimiter = ","
    for candidate in (";", "\t", ","):
        if sample.count(candidate) > sample.count(delimiter):
            delimiter = candidate
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)
            if any(cell.strip() for cell in row)]


def _tables(path: str) -> List[Tuple[str, List[List[str]]]]:
    """(name, rows) for every CSV in a zip, a folder or a single file."""
    out: List[Tuple[str, List[List[str]]]] = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith(".csv"):
                with open(os.path.join(path, name), "rb") as handle:
                    out.append((name, _read_csv_bytes(handle.read())))
        return out
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                if name.lower().endswith(".csv"):
                    out.append((os.path.basename(name),
                                _read_csv_bytes(archive.read(name))))
        return out
    with open(path, "rb") as handle:
        return [(os.path.basename(path), _read_csv_bytes(handle.read()))]


@dataclass
class Export:
    """What a Search Console export turned out to contain."""

    pages: List[Dict[str, Any]] = field(default_factory=list)
    queries: List[Dict[str, Any]] = field(default_factory=list)
    pairs: List[Dict[str, Any]] = field(default_factory=list)
    date_range: str = NO_RANGE
    tables_found: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def coverage_note(self) -> str:
        found = ", ".join(self.tables_found) or "nothing readable"
        return (f"Read {found}. Covers {self.date_range}. "
                f"{len(self.pages)} pages and {len(self.queries)} searches.")


def _row_dicts(rows: List[List[str]], columns: Dict[str, Optional[int]]
               ) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if not row:
            continue
        def cell(key):
            index = columns.get(key)
            return row[index] if index is not None and index < len(row) else ""
        entry = {
            "clicks": int(parse_number(cell("clicks"), whole=True)),
            "impressions": int(parse_number(cell("impressions"),
                                            whole=True)),
            "ctr_percent": parse_rate(cell("ctr")),
            "position": round(parse_number(cell("position")), 1),
        }
        if columns.get("page") is not None:
            entry["url"] = str(cell("page")).strip()
        if columns.get("query") is not None:
            entry["query"] = str(cell("query")).strip()
        if entry.get("url") or entry.get("query"):
            out.append(entry)
    return out


def read_export(path: str) -> Export:
    """Read a zip, a folder or a single CSV into pages, queries and dates."""
    export = Export()
    if not path or not os.path.exists(path):
        export.notes.append(f"nothing to read at {path}")
        return export

    dates: List[str] = []
    for name, rows in _tables(path):
        if len(rows) < 2:
            continue
        headers = [_clean_header(cell) for cell in rows[0]]
        columns = {
            "page": _find_column(headers, PAGE_WORDS),
            "query": _find_column(headers, QUERY_WORDS),
            "clicks": _find_column(headers, CLICK_WORDS),
            "impressions": _find_column(headers, IMPRESSION_WORDS),
            "ctr": _find_column(headers, CTR_WORDS),
            "position": _find_column(headers, POSITION_WORDS),
            "date": _find_column(headers, DATE_WORDS),
        }
        measured = columns["clicks"] is not None or \
            columns["impressions"] is not None
        if not measured:
            continue
        body = rows[1:]

        if columns["page"] is not None and columns["query"] is not None:
            export.pairs.extend(_row_dicts(body, columns))
            export.tables_found.append(f"{name} (searches by page)")
        elif columns["page"] is not None:
            export.pages.extend(_row_dicts(body, columns))
            export.tables_found.append(f"{name} (pages)")
        elif columns["query"] is not None:
            export.queries.extend(_row_dicts(body, columns))
            export.tables_found.append(f"{name} (searches)")
        elif columns["date"] is not None:
            index = columns["date"]
            dates = [row[index].strip() for row in body
                     if index < len(row) and row[index].strip()]
            export.tables_found.append(f"{name} (dates)")

    if dates:
        export.date_range = f"{min(dates)} to {max(dates)}"
    else:
        export.notes.append("no date table in the export, so the period the "
                            "numbers cover is not stated")
    if export.pairs and not export.queries:
        # A "searches by page" table also answers every question the plain
        # searches table would have.
        export.queries = _fold_pairs(export.pairs)
    if not export.pages and not export.queries:
        export.notes.append("no pages or searches table could be recognised")
    return export


def _fold_pairs(pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Searches by page, added up per search."""
    folded: Dict[str, Dict[str, Any]] = {}
    for row in pairs:
        query = row.get("query", "")
        entry = folded.setdefault(query, {"query": query, "clicks": 0,
                                          "impressions": 0, "ctr_percent": 0.0,
                                          "position": 0.0, "_weight": 0})
        entry["clicks"] += row["clicks"]
        entry["impressions"] += row["impressions"]
        entry["position"] += row["position"] * max(row["impressions"], 1)
        entry["_weight"] += max(row["impressions"], 1)
    out = []
    for entry in folded.values():
        weight = entry.pop("_weight") or 1
        entry["position"] = round(entry["position"] / weight, 1)
        entry["ctr_percent"] = round(
            entry["clicks"] / entry["impressions"] * 100, 2
        ) if entry["impressions"] else 0.0
        out.append(entry)
    return out


# --- matching an address to a crawled page ----------------------------------

def _keys(url: str) -> List[str]:
    """Every form of one address that should still find its page.

    A client's export writes the address the way Search Console holds it,
    which may differ from the crawl by a trailing slash, by www, or by case
    in the host. None of those are different pages.
    """
    normalised = normalize(url)
    if not normalised:
        return []
    forms = {normalised}
    variant = slash_variant(normalised)
    if variant:
        forms.add(variant)
    for form in list(forms):
        scheme, _, rest = form.partition("://")
        host, slash, tail = rest.partition("/")
        bare = strip_www(host)
        if bare != host:
            forms.add(f"{scheme}://{bare}{slash}{tail}")
    return sorted(forms)


def _page_index(pages: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """Every address form of every crawled page, pointing at its row."""
    index: Dict[str, Dict[str, str]] = {}
    for page in pages:
        for column in ("final_url", "url"):
            for key in _keys(page.get(column, "")):
                index.setdefault(key, page)
    return index


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _share(count: int, whole: int, whole_is: str) -> Dict[str, Any]:
    return {"count": int(count), "whole": int(whole), "whole_is": whole_is}


# --- the join ---------------------------------------------------------------

def _read_csv_file(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def join(run_dir: str, export: Export) -> Dict[str, Any]:
    """Everything the export and the crawl say together. Writes nothing."""
    pages = _read_csv_file(os.path.join(run_dir, "audit_pages.csv"))
    issues = _read_csv_file(os.path.join(run_dir, "page_issues.csv"))
    index = _page_index(pages)

    noindex = {row["final_url"] for row in issues
               if row["issue_type"] == "noindex_page"}
    off_canonical = {row["final_url"] for row in issues
                     if row["issue_type"] == "canonical_off_page"}

    matched: List[Dict[str, Any]] = []
    unseen: List[Dict[str, Any]] = []
    seen_final: Dict[str, Dict[str, Any]] = {}
    for row in export.pages:
        page = None
        for key in _keys(row.get("url", "")):
            page = index.get(key)
            if page:
                break
        if not page:
            unseen.append(row)
            continue
        final_url = page.get("final_url", "")
        entry = {
            "final_url": final_url,
            "gsc_url": row.get("url", ""),
            "clicks": row["clicks"],
            "impressions": row["impressions"],
            "ctr_percent": row["ctr_percent"],
            "position": row["position"],
            "in_sitemap": _flag(page.get("in_sitemap")),
            "inlinks": int(parse_number(page.get("inlinks"))),
            "hidden_from_search": final_url in noindex,
            "preferred_address_elsewhere": final_url in off_canonical,
        }
        matched.append(entry)
        # The same page can arrive twice, as two address forms.
        if final_url in seen_final:
            previous = seen_final[final_url]
            previous["clicks"] += entry["clicks"]
            previous["impressions"] += entry["impressions"]
        else:
            seen_final[final_url] = entry

    crawled_absent = [page for page in pages
                      if page.get("final_url") not in seen_final]
    sitemap_pages = [page for page in pages if _flag(page.get("in_sitemap"))]
    with_impressions = [row for row in seen_final.values()
                        if row["impressions"] > 0]

    return {
        "matched": matched,
        "by_final_url": seen_final,
        "unseen": unseen,
        "crawled_absent": crawled_absent,
        "sitemap_pages": sitemap_pages,
        "with_impressions": with_impressions,
        "crawled_pages": pages,
    }


def _issue_rows(joined: Dict[str, Any], export: Export
                ) -> Tuple[List[Dict[str, str]], Dict[str, int],
                           List[Dict[str, Any]]]:
    """The findings only the two sides together can produce."""
    rows: List[Dict[str, str]] = []
    seen_final = joined["by_final_url"]
    impressions_whole = len(joined["with_impressions"])

    def add(issue_type: str, url: str, detail: str):
        from .issues import PAGE_ISSUE_SEVERITY
        rows.append({"issue_type": issue_type,
                     "severity": PAGE_ISSUE_SEVERITY[issue_type],
                     "url": url, "final_url": url, "detail": detail,
                     "site_level": "False"})

    for entry in sorted(joined["with_impressions"],
                        key=lambda e: -e["impressions"]):
        if entry["hidden_from_search"]:
            add("gsc_impressions_on_noindex", entry["final_url"],
                f"{entry['impressions']} impressions and "
                f"{entry['clicks']} clicks on a page hidden from search")
        if entry["preferred_address_elsewhere"]:
            add("gsc_impressions_on_off_canonical", entry["final_url"],
                f"{entry['impressions']} impressions on a page that names "
                f"another page as preferred")
        if entry["clicks"] > 0 and entry["inlinks"] == 0:
            add("gsc_orphan_page_with_clicks", entry["final_url"],
                f"{entry['clicks']} clicks with no links to the page from "
                f"anywhere on the site")

    for page in joined["sitemap_pages"]:
        final_url = page.get("final_url", "")
        entry = seen_final.get(final_url)
        if entry is None or entry["impressions"] == 0:
            add("gsc_sitemap_page_no_impressions", final_url,
                "listed in the sitemap and never shown in search results")

    cannibalised = _cannibalised(export)
    for group in cannibalised:
        add("gsc_query_cannibalised", group["pages"][0],
            f"{group['query']}: {len(group['pages'])} pages answer this "
            f"search, {group['impressions']} impressions")

    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["issue_type"]] = counts.get(row["issue_type"], 0) + 1
    return rows, counts, cannibalised


def _cannibalised(export: Export) -> List[Dict[str, Any]]:
    """Searches answered by more than one page, when the export can say.

    A plain export has one table of pages and one of searches, with nothing
    joining them. Only an export that pairs the two can answer this, so when
    there is no such table the question is left unanswered rather than
    guessed at.
    """
    if not export.pairs:
        return []
    by_query: Dict[str, List[Dict[str, Any]]] = {}
    for row in export.pairs:
        by_query.setdefault(row.get("query", ""), []).append(row)

    groups = []
    for query, rows in by_query.items():
        urls = sorted({row.get("url", "") for row in rows if row.get("url")})
        if len(urls) < 2:
            continue
        groups.append({
            "query": query,
            "pages": urls,
            "impressions": sum(row["impressions"] for row in rows),
            "clicks": sum(row["clicks"] for row in rows),
        })
    return sorted(groups, key=lambda g: -g["impressions"])


def _top(rows: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda row: -row.get("impressions", 0))[:TOP_ROWS]


def search_performance(run_dir: str, export: Export) -> Tuple[
        Dict[str, Any], List[Dict[str, str]], List[Dict[str, Any]]]:
    """The block that goes into findings.json, and the rows for the issues."""
    joined = join(run_dir, export)
    rows, counts, cannibalised = _issue_rows(joined, export)

    gsc_pages = len(export.pages)
    crawled = len(joined["crawled_pages"])
    near_miss = [row for row in export.queries
                 if NEAR_MISS_LOW <= row.get("position", 0) <= NEAR_MISS_HIGH]

    block = {
        "provided": True,
        "date_range": export.date_range,
        "coverage_note": export.coverage_note,
        "notes": export.notes,
        "totals": {
            "impressions": sum(row["impressions"] for row in export.pages),
            "clicks": sum(row["clicks"] for row in export.pages),
            "pages_in_export": gsc_pages,
            "queries_in_export": len(export.queries),
        },
        "join": {
            "matched": _share(len(joined["by_final_url"]), gsc_pages,
                              "pages in the export"),
            "not_crawled": _share(len(joined["unseen"]), gsc_pages,
                                  "pages in the export"),
            "crawled_not_in_export": _share(len(joined["crawled_absent"]),
                                            crawled, "pages crawled"),
        },
        "top_pages": [
            {k: row[k] for k in ("final_url", "clicks", "impressions",
                                 "ctr_percent", "position")}
            for row in _top(joined["by_final_url"].values(), "impressions")],
        "top_queries": _top(export.queries, "impressions"),
        "near_miss_queries": sorted(near_miss,
                                    key=lambda row: -row["impressions"])[
                                        :TOP_ROWS],
        "cannibalised_queries": cannibalised[:TOP_ROWS],
        "cannibalisation_available": bool(export.pairs),
        "issue_counts": counts,
        "wholes": {
            "pages_with_impressions": len(joined["with_impressions"]),
            "sitemap_pages": len(joined["sitemap_pages"]),
            "queries": len(export.queries),
        },
    }
    return block, rows, joined["matched"]


# --- writing it back --------------------------------------------------------

def _write_join_csv(run_dir: str, matched: List[Dict[str, Any]]) -> str:
    path = os.path.join(run_dir, JOIN_FILE)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=JOIN_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in matched:
            writer.writerow(row)
    return path


def _replace_issue_rows(run_dir: str, rows: List[Dict[str, str]]) -> int:
    """Drop any rows a previous attach wrote, then append this attach's."""
    path = os.path.join(run_dir, "page_issues.csv")
    existing = _read_csv_file(path)
    columns = list(existing[0].keys()) if existing else [
        "issue_type", "severity", "url", "final_url", "detail", "site_level"]
    kept = [row for row in existing
            if not str(row.get("issue_type", "")).startswith(GSC_PREFIX)]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns,
                                extrasaction="ignore")
        writer.writeheader()
        for row in kept + rows:
            writer.writerow(row)
    return len(rows)


def attach(run_dir: str, export_path: str) -> Dict[str, Any]:
    """Read an export, join it to this run, and write the answer back."""
    from .findings import build_findings

    if not os.path.exists(os.path.join(run_dir, "audit_pages.csv")):
        raise FileNotFoundError(
            f"no audit_pages.csv in {run_dir}; this is not a finished run")

    export = read_export(export_path)
    block, rows, matched = search_performance(run_dir, export)

    join_path = _write_join_csv(run_dir, matched)
    _replace_issue_rows(run_dir, rows)

    # The block goes in first so that rebuilding the findings can see it:
    # the fixes need its wholes and the comparison needs its totals.
    findings_path = os.path.join(run_dir, "findings.json")
    findings = {}
    if os.path.exists(findings_path):
        with open(findings_path, encoding="utf-8") as handle:
            findings = json.load(handle)
    findings["search_performance"] = block
    with open(findings_path, "w", encoding="utf-8") as handle:
        json.dump(findings, handle, indent=2, ensure_ascii=False)

    findings = build_findings(run_dir)
    return {"run_dir": run_dir, "join_csv": join_path,
            "issue_rows": len(rows), "matched": len(matched),
            "search_performance": findings.get("search_performance", block),
            "export": export}


# --- the command ------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo-audit-gsc",
        description="Join a Search Console export to a finished audit run.")
    parser.add_argument("--run", required=True,
                        help="Run folder holding audit_pages.csv")
    parser.add_argument("--export", required=True,
                        help="The export: a zip, a folder of CSVs, or one CSV")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = attach(args.run, args.export)
    except Exception as exc:  # the console shows this, so it must be plain
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    block = result["search_performance"]
    export = result["export"]
    print(f"run      : {result['run_dir']}", file=sys.stderr)
    print(f"export   : {export.coverage_note}", file=sys.stderr)
    print(f"matched  : {block['join']['matched']['count']} of "
          f"{block['join']['matched']['whole']} pages in the export",
          file=sys.stderr)
    print(f"join file: {result['join_csv']}", file=sys.stderr)
    print(f"findings : {block['totals']['impressions']} impressions, "
          f"{block['totals']['clicks']} clicks over {block['date_range']}",
          file=sys.stderr)
    for issue_type, count in sorted(block["issue_counts"].items()):
        print(f"  {issue_type}: {count}", file=sys.stderr)
    for note in export.notes:
        print(f"note     : {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
