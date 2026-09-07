"""findings.json: the run's own files, read back and turned into findings.

Three rules shape this module, and all three come from earlier sessions
producing numbers that read as more than they were:

  * Every share is an object -- {count, whole, whole_is} -- so nothing can be
    drawn as a proportion without stating what it is a proportion of. "167
    orphans" is not a finding; "167 of 604 crawled pages" is.
  * Faults are grouped by root cause. Forty 404s that are one broken template
    are one fix, not forty rows, and the group says how many link instances
    it accounts for.
  * A fact is stated once. Orphan pages appeared as both `orphan_page` and
    `pages_with_zero_inlinks`; here they appear in one place.

Nothing is recomputed that crawl_summary.json already holds: this reads it.
The patterns used for grouping are derived from each URL (its path template
plus the names of its query parameters), never matched against a site.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from .issues import label_of, plain_finding, unit_of
from .pagespeed import template_of

MAX_EVIDENCE = 5
MAX_FIXES = 15

# How much a finding's severity multiplies its reach when ordering fixes.
# Weight 0 means the type is reported but never offered as a fix. "info" is
# for things the site is doing right, such as a filtered address correctly
# pointing at its clean page.
SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1, "unmeasured": 0,
                   "info": 0}

# A metric has moved enough to be worth naming when it shifts by either.
NOTABLE_RATIO = 0.20
NOTABLE_ABSOLUTE = 50

RUN_FILES = ("raw_crawl.csv", "audit_pages.csv", "page_issues.csv",
             "crawl_issues.csv", "sitemap_sweep.csv", "pagespeed.csv",
             "crawl_summary.json")

_MISSING = "not in previous run"
NOT_COMPARABLE = "not comparable, coverage changed"

# Metrics that describe how much of the site the crawl saw, rather than a
# fault count. A change in these explains changes in everything else.
COVERAGE_METRICS = {"pages_found", "pages_parsed", "sitemap_urls_total"}


# --- reading ----------------------------------------------------------------

def _read_csv(path: str) -> List[Dict[str, str]]:
    """A run file, or an empty list when the stage did not run."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _dig(doc: Dict, *path, default=None):
    """Nested lookup that tolerates a summary written by an older version."""
    node: Any = doc
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


# --- the share object -------------------------------------------------------

def share(count: int, whole: int, whole_is: str) -> Dict[str, Any]:
    """A count that can never be drawn without saying what it is out of."""
    return {"count": int(count), "whole": int(whole), "whole_is": whole_is}


# --- root-cause grouping ----------------------------------------------------

def _params_of(url: str) -> List[str]:
    return sorted({k for k, _v in parse_qsl(urlsplit(url).query,
                                            keep_blank_values=True)})


def pattern_of(url: str) -> str:
    """The shape a URL shares with its siblings: section, depth, parameters.

    `/on-demand/pune/?area=wakad` and `/on-demand/noida/?area=sector-62` are
    one pattern; `/blogs/x` is another.

    Deliberately coarser than pagespeed.template_of, which keeps the second
    path segment literal because a section is the right unit for choosing
    what to measure. For root causes it is the wrong unit: the city in
    `/on-demand/<city>/` is the variable part, and forty 404s that are one
    broken template must collapse to one finding. Only the first segment
    survives; the rest become placeholders, so depth is part of the shape.

    Derived from the URL alone, so it carries no knowledge of any site.
    """
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if segments:
        shape = "/" + segments[0].lower()
        shape += "/{seg}" * (len(segments) - 1)
    else:
        shape = "/"
    params = _params_of(url)
    return f"{shape}?{'&'.join(params)}" if params else shape


def parameter_pattern_of(url: str) -> str:
    """Group by the query parameters alone.

    Some faults travel with a parameter rather than a section: a `?mlg=0`
    variant declares alternates the clean URL does not return, on every
    section of the site at once. Grouping those by path would report the same
    root cause once per section.
    """
    params = _params_of(url)
    return "?" + "&".join(params) if params else "(no query parameters)"


def group_by_pattern(items: Iterable[Tuple[str, Dict]], pattern=None
                     ) -> List[Tuple[str, List[Dict]]]:
    """Group (url, row) pairs by derived pattern, biggest group first."""
    pattern = pattern or pattern_of
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for url, row in items:
        groups[pattern(url)].append(row)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


_REFERRER_COUNT_RE = re.compile(r"linked from (\d+) page")


def _referrer_count(detail: str) -> int:
    match = _REFERRER_COUNT_RE.search(detail or "")
    return int(match.group(1)) if match else 0


# --- sections ---------------------------------------------------------------

def _meta(summary: Dict, run_dir: str) -> Dict:
    parsed = _dig(summary, "pages_parsed", default=0)
    return {
        "domain": _dig(summary, "domain"),
        "host": _dig(summary, "host"),
        "run_id": os.path.basename(os.path.normpath(run_dir)),
        "mode": "baseline",
        "timestamp": _dig(summary, "started_at"),
        "run_duration_seconds": _dig(summary, "run_duration_seconds"),
        "stage_seconds": _dig(summary, "stage_seconds", default={}),
        "caps_applied": {
            "max_pages": _dig(summary, "limits", "max_pages"),
            "max_depth": _dig(summary, "limits", "max_depth"),
            "sweep_limit": _dig(summary, "sitemap_sweep", "sweep_limit"),
            "external_check_limit": _dig(summary, "links",
                                         "external_check_limit"),
            "canonical_check_limit": _dig(summary, "canonical_check_limit"),
            "pagespeed_templates": _dig(summary, "limits",
                                        "pagespeed_templates"),
        },
        "coverage": {
            "pages_found": _dig(summary, "pages_found", default=0),
            "pages_parsed": parsed,
            "redirect_duplicates": _dig(summary, "redirect_duplicates",
                                        default=0),
            "sitemap_urls_total": _dig(summary, "sitemap_sweep",
                                       "sitemap_urls_total", default=0),
            "sitemap_urls_swept": _dig(summary, "sitemap_sweep",
                                       "sitemap_urls_swept", default=0),
            "sweep_capped": _dig(summary, "sitemap_sweep", "sweep_capped",
                                 default=False),
            "sweep_throttled": _dig(summary, "sitemap_sweep",
                                    "sweep_throttled", default=False),
        },
    }


def _headline(summary: Dict) -> Dict:
    score = _dig(summary, "score", default={})
    scored = score.get("pages_scored", 0)
    distribution = {
        band: share(count, scored, "pages scored")
        for band, count in (score.get("distribution") or {}).items()
    }
    return {
        "site_score": score.get("site_score"),
        "mean_page_score": score.get("mean_page_score"),
        "performance_component": score.get("performance_component"),
        "performance_included": score.get("performance_included"),
        "performance_coverage": share(
            score.get("performance_measured", 0),
            score.get("performance_sampled", 0) or 0,
            "templates sampled for performance"),
        "site_level_deduction": score.get("site_level_deduction", 0),
        "site_level_types": score.get("site_level_types", []),
        "pages_scored": scored,
        "noindex_excluded": share(score.get("noindex_pages", 0),
                                  _dig(summary, "pages_parsed", default=0),
                                  "pages parsed"),
        "distribution": distribution,
        "lowest_pages": (score.get("lowest_pages") or [])[:10],
        "note": score.get("note"),
    }


def _evidence(rows: List[Dict], *fields: str) -> List[Dict]:
    out = []
    for row in rows[:MAX_EVIDENCE]:
        out.append({f: row.get(f) for f in fields if row.get(f) not in (None, "")})
    return out


def _crawlability(summary: Dict, crawl_issues: List[Dict],
                  raw: List[Dict]) -> Dict:
    counts = _dig(summary, "crawl_issues", default={}) or {}
    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for row in crawl_issues:
        by_type[row["issue_type"]].append(row)

    found = _dig(summary, "pages_found", default=0)
    parsed = _dig(summary, "pages_parsed", default=0)
    sitemap_total = _dig(summary, "sitemap_sweep", "sitemap_urls_total",
                         default=0)
    redirected = _dig(summary, "redirected", default=0)
    slash = counts.get("trailing_slash_redirect", 0)

    in_sitemap_not_crawled = max(
        0, sitemap_total - _dig(summary, "sitemap_sweep", "already_crawled",
                                default=0))
    crawled_not_in_sitemap = counts.get("crawl_only_page", 0)

    return {
        "sitemap": {
            "source": _dig(summary, "sitemap", "source"),
            "sitemaps_used": _dig(summary, "sitemap", "sitemaps_used",
                                  default=[]),
            "urls_total": sitemap_total,
            "urls_swept": share(_dig(summary, "sitemap_sweep",
                                     "sitemap_urls_swept", default=0),
                                sitemap_total, "sitemap URLs"),
            "capped": _dig(summary, "sitemap_sweep", "sweep_capped",
                           default=False),
            "throttled": _dig(summary, "sitemap_sweep", "sweep_throttled",
                              default=False),
            "non_200": counts.get("sitemap_non_200", 0),
            "in_sitemap_not_crawled": share(in_sitemap_not_crawled,
                                            sitemap_total, "sitemap URLs"),
            "crawled_not_in_sitemap": share(crawled_not_in_sitemap, found,
                                            "pages found"),
            "evidence": _evidence(by_type.get("sitemap_non_200", []),
                                  "url", "detail"),
        },
        "redirects": {
            "total": share(redirected, found, "pages found"),
            "trailing_slash": share(slash, redirected or 0,
                                    "redirects"),
            "chains": counts.get("redirect_chain", 0),
            "loops": counts.get("redirect_loop", 0),
            "host_redirect": counts.get("host_redirect", 0),
            "evidence": _evidence(by_type.get("trailing_slash_redirect", []),
                                  "url", "referrer", "detail"),
        },
        "robots": {
            "found": _dig(summary, "robots", "found", default=False),
            "fetched_from_host": _dig(summary, "robots", "fetched_from_host"),
            "disallow_rules": _dig(summary, "robots", "disallow_rules",
                                   default=0),
            "urls_blocked": _dig(summary, "robots", "urls_blocked", default=0),
            "sitemap_directives": _dig(summary, "robots", "sitemap_directives",
                                       default=[]),
            "ai_crawler_rules": len(by_type.get("ai_crawler_rule", [])),
            "ai_crawler_evidence": _evidence(by_type.get("ai_crawler_rule", []),
                                             "url", "detail"),
            "blocked_linked": counts.get("robots_blocked_linked", 0),
            "blocked_in_sitemap": counts.get("robots_blocked_in_sitemap", 0),
        },
        "render_suspects": share(
            _dig(summary, "render_suspects", "count", default=0),
            parsed, "pages parsed"),
        "documents_linked": counts.get("document_linked", 0),
        "non_html_linked": counts.get("non_html_linked", 0),
        "deep_pages": share(counts.get("deep_page", 0), parsed,
                            "pages parsed"),
        "slow_responses": counts.get("slow_response", 0),
        "fetch_errors": _dig(summary, "errors", default=0),
    }


def _grouped_findings(rows: List[Dict], key: str = "final_url",
                      pattern=pattern_of) -> List[Dict]:
    """Root-cause groups for a list of page-issue rows."""
    groups = group_by_pattern([(r.get(key) or r.get("url"), r) for r in rows],
                              pattern=pattern)
    out = []
    for pattern, members in groups:
        out.append({
            "pattern": pattern,
            "pages_affected": len(members),
            "evidence": _evidence(members, "final_url", "url", "detail"),
        })
    return out


def _indexability_technical(summary: Dict, issues_by_type: Dict[str, List[Dict]],
                            parsed: int) -> Dict:
    counts = _dig(summary, "page_issue_counts", default={}) or {}
    canonical_types = [t for t in counts if t.startswith("canonical")]

    # Grouped by parameter: the fault travels with `?mlg=0` and friends,
    # not with any one section of the site.
    hreflang_groups = _grouped_findings(
        issues_by_type.get("hreflang_not_reciprocal", []),
        pattern=parameter_pattern_of)

    return {
        "noindex_pages": share(_dig(summary, "score", "noindex_pages",
                                    default=0), parsed, "pages parsed"),
        "noindex_urls": (_dig(summary, "score", "noindex_urls", default=[])
                         or [])[:MAX_EVIDENCE],
        "canonical": {
            t: share(counts.get(t, 0), parsed, "pages parsed")
            for t in sorted(canonical_types)
        },
        "canonical_targets_unchecked": share(
            _dig(summary, "canonical_targets_unchecked", default=0),
            _dig(summary, "canonical_check_limit", default=0) or 0,
            "canonical check limit"),
        "hreflang_not_reciprocal": {
            "pages_affected": share(
                counts.get("hreflang_not_reciprocal", 0), parsed,
                "pages parsed"),
            "groups": hreflang_groups[:MAX_EVIDENCE],
            "group_count": len(hreflang_groups),
        },
        "hreflang_target_non_200": counts.get("hreflang_target_non_200", 0),
        "mixed_content": share(counts.get("mixed_content", 0), parsed,
                               "pages parsed"),
        "site_level": {
            "deduction": _dig(summary, "score", "site_level_deduction",
                              default=0),
            "types": _dig(summary, "score", "site_level_types", default=[]),
        },
    }


def _on_page(summary: Dict, issues_by_type: Dict[str, List[Dict]],
             pages: List[Dict], parsed: int) -> Dict:
    counts = _dig(summary, "page_issue_counts", default={}) or {}

    # h1_multiple by template: one template repeating a layout fault is one
    # fix, not four hundred.
    h1_rows = issues_by_type.get("h1_multiple", [])
    by_template: Dict[str, int] = Counter(
        template_of(r.get("final_url") or r.get("url")) for r in h1_rows)
    h1_groups = [{"template": t, "pages_affected": c}
                 for t, c in by_template.most_common(MAX_EVIDENCE)]

    def s(name: str) -> Dict:
        return share(counts.get(name, 0), parsed, "pages parsed")

    return {
        "title": {
            "missing": s("title_missing"),
            "too_long": s("title_too_long"),
            "too_short": s("title_too_short"),
            "duplicate_groups": counts.get("duplicate_title", 0),
        },
        "meta_description": {
            "missing": s("meta_description_missing"),
            "too_long": s("meta_description_too_long"),
            "duplicate_groups": counts.get("duplicate_meta_description", 0),
        },
        "h1": {
            "missing": s("h1_missing"),
            "multiple": s("h1_multiple"),
            "multiple_by_template": h1_groups,
            "template_count": len(by_template),
        },
        "images_missing_alt": s("images_missing_alt"),
        "viewport_missing": s("viewport_missing"),
        "html_lang_missing": s("html_lang_missing"),
    }


def _canonical_handled(detail: str, canonical_by_url: Dict[str, str]) -> bool:
    """True when every page in a duplicate group canonicalises to one member.

    Such a group is the site already handling its own duplication: worth
    reporting, not worth fixing.
    """
    members = re.findall(r"https?://\S+?(?=[,\s]|$)", detail or "")
    members = [m.rstrip(".,") for m in members]
    if len(members) < 2:
        return False
    targets = {canonical_by_url.get(m) for m in members}
    targets.discard(None)
    return len(targets) == 1 and next(iter(targets)) in members


def _content(summary: Dict, issues_by_type: Dict[str, List[Dict]],
             pages: List[Dict], parsed: int) -> Dict:
    counts = _dig(summary, "page_issue_counts", default={}) or {}
    canonical_by_url = {p["final_url"]: p.get("canonical") for p in pages}

    def split(name: str) -> Dict:
        rows = issues_by_type.get(name, [])
        handled = [r for r in rows
                   if _canonical_handled(r.get("detail", ""), canonical_by_url)]
        unhandled = [r for r in rows if r not in handled]
        return {
            "groups": len(rows),
            "canonical_handled": len(handled),
            "needs_attention": len(unhandled),
            "evidence": _evidence(unhandled or rows, "final_url", "detail"),
        }

    words = sorted(_int(p.get("word_count")) for p in pages)
    quartiles = {}
    if words:
        def at(fraction: float) -> int:
            return words[min(len(words) - 1, int(len(words) * fraction))]
        quartiles = {"min": words[0], "q1": at(0.25), "median": at(0.5),
                     "q3": at(0.75), "max": words[-1]}

    return {
        "thin_pages": share(counts.get("thin_page", 0), parsed,
                            "pages parsed"),
        "duplicate_content": split("duplicate_content"),
        "near_duplicate_content": split("near_duplicate_content"),
        "pages_compared": share(_dig(summary, "content", "pages_compared",
                                     default=0), parsed, "pages parsed"),
        "min_words_for_comparison": _dig(summary, "content",
                                         "min_words_for_comparison"),
        "word_count_quartiles": quartiles,
    }


def _schema(summary: Dict, issues_by_type: Dict[str, List[Dict]],
            pages: List[Dict], parsed: int) -> Dict:
    with_jsonld = sum(1 for p in pages if _int(p.get("schema_block_count")) > 0)
    microdata_only = sum(1 for p in pages
                         if _int(p.get("schema_block_count")) == 0
                         and str(p.get("has_microdata")).lower() == "true")
    none_at_all = parsed - with_jsonld - microdata_only

    missing_props = Counter(r.get("detail", "")
                            for r in issues_by_type.get(
                                "schema_missing_property", []))
    return {
        "coverage": {
            "json_ld": share(with_jsonld, parsed, "pages parsed"),
            "microdata_only": share(microdata_only, parsed, "pages parsed"),
            "none": share(max(0, none_at_all), parsed, "pages parsed"),
        },
        "type_counts": _dig(summary, "schema_type_counts", default={}),
        "invalid_json": len(issues_by_type.get("schema_invalid_json", [])),
        "missing_property_pages": len(
            issues_by_type.get("schema_missing_property", [])),
        "missing_properties": [
            {"detail": detail, "pages_affected": count}
            for detail, count in missing_props.most_common(MAX_EVIDENCE)
        ],
        "missing_property_kinds": len(missing_props),
    }


def _links(summary: Dict, issues_by_type: Dict[str, List[Dict]],
           parsed: int) -> Dict:
    links = _dig(summary, "links", default={}) or {}

    def link_groups(name: str) -> Dict:
        rows = issues_by_type.get(name, [])
        groups = group_by_pattern([(r.get("url"), r) for r in rows])
        described = []
        for pattern, members in groups[:MAX_EVIDENCE]:
            instances = sum(_referrer_count(m.get("detail", "")) for m in members)
            described.append({
                "pattern": pattern,
                "targets": len(members),
                "link_instances": instances,
                "evidence": [m.get("url") for m in members[:MAX_EVIDENCE]],
            })
        return {
            "targets": len(rows),
            "group_count": len(groups),
            "groups": described,
            "link_instances": sum(_referrer_count(r.get("detail", ""))
                                  for r in rows),
        }

    return {
        # Said once: orphan_page and pages_with_zero_inlinks were the same
        # fact under two names.
        "orphan_pages": {
            **share(links.get("pages_with_zero_inlinks", 0), parsed,
                    "pages parsed"),
            "in_sitemap": links.get("orphan_pages_in_sitemap", 0),
        },
        "low_inlink_pages": share(
            len(issues_by_type.get("low_inlink_page", [])), parsed,
            "pages parsed"),
        "broken_internal": link_groups("broken_internal_link"),
        "redirected_internal": link_groups("redirected_internal_link"),
        "external": {
            "found": links.get("external_targets_found", 0),
            "checked": share(links.get("external_checked", 0),
                             links.get("external_targets_found", 0),
                             "external targets found"),
            "broken": share(links.get("external_broken", 0),
                            links.get("external_checked", 0),
                            "external targets checked"),
            "unchecked": links.get("external_unchecked", 0),
            "check_limit": links.get("external_check_limit"),
            "evidence": _evidence(
                issues_by_type.get("external_link_broken", []),
                "url", "detail"),
        },
        "generic_anchor": share(len(issues_by_type.get("generic_anchor", [])),
                                parsed, "pages parsed"),
        "nofollow_internal": len(issues_by_type.get("nofollow_internal_link",
                                                    [])),
        "edges": links.get("edges", 0),
    }


def _round_ms(value: Any) -> Optional[int]:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _round_to(value: Any, places: int) -> Optional[float]:
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


def _performance(summary: Dict, pagespeed: List[Dict],
                 pages: List[Dict], parsed: int) -> Dict:
    block = _dig(summary, "pagespeed", default={}) or {}
    if block.get("skipped"):
        return {"skipped": True, "skip_reason": block.get("skip_reason")}

    by_url: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for row in pagespeed:
        by_url[row["url"]][row["strategy"]] = row

    measured = []
    sampled_templates = set()
    for url, strategies in by_url.items():
        mobile = strategies.get("mobile", {})
        desktop = strategies.get("desktop", {})
        sampled_templates.add(mobile.get("template") or desktop.get("template"))
        measured.append({
            "template": mobile.get("template") or desktop.get("template"),
            "url": url,
            "group_size": _int(mobile.get("group_size")),
            "mobile_score": _int(mobile.get("performance_score"), None)
            if mobile.get("performance_score") else None,
            "desktop_score": _int(desktop.get("performance_score"), None)
            if desktop.get("performance_score") else None,
            # Milliseconds to whole numbers and layout shift to two places:
            # "2026.005 milliseconds" is not a sentence anyone should read.
            "lab_lcp_ms": _round_ms(mobile.get("lab_lcp_ms")),
            "lab_cls": _round_to(mobile.get("lab_cls"), 2),
            "field_data_level": mobile.get("field_data_level"),
            "error": mobile.get("error") or desktop.get("error") or None,
        })
    measured.sort(key=lambda m: (m["mobile_score"] is None,
                                 m["mobile_score"] if m["mobile_score"]
                                 is not None else 0))

    # Which templates never got measured, and how many pages they cover.
    all_templates = Counter(template_of(p["final_url"]) for p in pages)
    unmeasured = [{"template": t, "pages": c}
                  for t, c in all_templates.most_common()
                  if t not in sampled_templates]

    inp_values = [_round_ms(r.get("field_inp_ms")) for r in pagespeed
                  if r.get("strategy") == "mobile" and r.get("field_inp_ms")]
    inp_values = [v for v in inp_values if v is not None]
    origin_level = any(r.get("field_data_level") == "origin"
                       for r in pagespeed)

    return {
        "skipped": False,
        # Real visitor responsiveness, stated once for the report rather than
        # repeated on every template row.
        "field_inp": {
            "measured_templates": len(inp_values),
            "worst_ms": max(inp_values) if inp_values else None,
            "median_ms": sorted(inp_values)[len(inp_values) // 2]
            if inp_values else None,
            "origin_level": origin_level,
        },
        "templates_measured": measured,
        "templates_sampled": block.get("templates_sampled", 0),
        "pages_represented": share(block.get("pages_represented", 0), parsed,
                                   "pages parsed"),
        "templates_unmeasured": unmeasured[:10],
        "templates_unmeasured_count": len(unmeasured),
        "pages_unmeasured": share(
            sum(u["pages"] for u in unmeasured), parsed, "pages parsed"),
        "mean_mobile_score": block.get("mean_mobile_score"),
        "mean_desktop_score": block.get("mean_desktop_score"),
        "worst_template": block.get("worst_template_mobile"),
        "attempts": block.get("pagespeed_attempts"),
        "attempts_ceiling": block.get("pagespeed_attempts_ceiling"),
        "measurements": block.get("pagespeed_calls"),
        "deadline_hit": block.get("pagespeed_deadline_hit"),
        "stage_seconds": block.get("pagespeed_stage_seconds"),
        "errors": block.get("errors", 0),
    }


# --- prioritised fixes ------------------------------------------------------

FIX_TITLES = {
    "broken_internal_link": "Fix internal links pointing at pages that no longer exist",
    "redirected_internal_link": "Point internal links straight at the page they end up on",
    "orphan_page": "Add internal links to pages nothing links to",
    "low_inlink_page": "Give pages with a single internal link more routes in",
    "hreflang_not_reciprocal": "Make language versions point back at each other",
    "meta_description_missing": "Write a search description for pages without one",
    "meta_description_too_long": "Shorten search descriptions that get cut off",
    "title_missing": "Add page titles where there are none",
    "title_too_long": "Shorten page titles that get cut off in results",
    "title_too_short": "Give short page titles more to say",
    "h1_missing": "Add a main heading to pages without one",
    "h1_multiple": "Leave one main heading per page",
    "images_missing_alt": "Describe the images that have no description",
    "thin_page": "Add substance to pages with very little text",
    "duplicate_content": "Resolve pages whose text is identical",
    "near_duplicate_content": "Differentiate pages that read almost the same",
    "duplicate_title": "Give pages that share a title their own",
    "duplicate_meta_description": "Give pages that share a search description their own",
    "schema_missing": "Add structured data to pages that have none",
    "schema_invalid_json": "Repair structured data that fails to parse",
    "schema_missing_property": "Complete structured data that is missing required fields",
    "schema_microdata_only": "Move structured data into the current format",
    "canonical_missing": "Name a preferred address on pages without one",
    "canonical_off_page": "Point each preferred address at the page it is on",
    "canonical_not_absolute": "Write preferred addresses as full addresses",
    "canonical_chain": "Point preferred addresses straight at the final page",
    "canonical_target_non_200": "Repoint preferred addresses that lead to missing pages",
    "canonical_target_noindex": "Repoint preferred addresses that lead to hidden pages",
    "canonical_target_not_crawled": "Check preferred addresses that lead to pages this audit did not reach",
    "canonical_target_unchecked": "Check the preferred addresses left over",
    "noindex_page": "Confirm the pages hidden from search are meant to be",
    "nofollow_page": "Confirm the pages that block link following are meant to",
    "viewport_missing": "Add the mobile layout setting so pages work on phones",
    "html_lang_missing": "Declare the page language",
    "mixed_content": "Load every file over a secure connection",
    "external_link_broken": "Repair or remove links to pages that have gone",
    "generic_anchor": "Replace vague link text with words describing the page",
    "nofollow_internal_link": "Let search engines follow internal links",
    "sitemap_noindex": "Take hidden pages out of the sitemap",
    "sitemap_off_canonical": "List preferred pages in the sitemap",
    "hsts_missing": "Tell browsers to always use a secure connection",
    "csp_missing": "Send a content security policy",
    "x_content_type_options_missing": "Send the header that stops browsers guessing file types",
    "x_frame_options_missing": "Send the header that stops other sites framing your pages",
    "http_to_https_redirect": "Send the insecure address on to the secure one",
    "performance_poor": "Speed up the slowest page templates",
    "lcp_poor": "Make the largest element on the page load sooner",
    "cls_poor": "Stop the layout shifting as the page loads",
    "inp_poor": "Make the page respond to taps sooner",
}

FIX_CATEGORY = {
    "indexability": {"noindex_page", "nofollow_page", "canonical_missing",
                     "canonical_off_page", "canonical_not_absolute",
                     "canonical_chain", "canonical_target_non_200",
                     "canonical_target_noindex", "canonical_target_not_crawled",
                     "canonical_target_unchecked", "sitemap_noindex",
                     "sitemap_off_canonical", "http_to_https_redirect"},
    "technical": {"mixed_content", "viewport_missing", "html_lang_missing",
                  "hreflang_not_reciprocal", "hreflang_target_non_200",
                  "hsts_missing", "csp_missing",
                  "x_content_type_options_missing",
                  "x_frame_options_missing", "performance_poor", "lcp_poor",
                  "cls_poor", "inp_poor"},
    "on_page": {"title_missing", "title_too_long", "title_too_short",
                "meta_description_missing", "meta_description_too_long",
                "h1_missing", "h1_multiple", "images_missing_alt",
                "duplicate_title", "duplicate_meta_description"},
    "content": {"thin_page", "duplicate_content", "near_duplicate_content"},
    "schema": {"schema_missing", "schema_invalid_json",
               "schema_missing_property", "schema_microdata_only"},
    "links": {"orphan_page", "low_inlink_page", "broken_internal_link",
              "redirected_internal_link", "nofollow_internal_link",
              "generic_anchor", "external_link_broken"},
}

SITE_LEVEL_TYPES = {"hsts_missing", "csp_missing",
                    "x_content_type_options_missing",
                    "x_frame_options_missing", "http_to_https_redirect"}

# Types whose findings are grouped by root cause rather than counted per page.
GROUPED_TYPES = {"broken_internal_link", "redirected_internal_link",
                 "hreflang_not_reciprocal", "h1_multiple"}


def _category_of(issue_type: str) -> str:
    for name, members in FIX_CATEGORY.items():
        if issue_type in members:
            return name
    return "other"


def _title_for(issue_type: str) -> str:
    """Plain words, and never a dash: these are read aloud to clients."""
    title = FIX_TITLES.get(issue_type)
    if not title:
        title = "Review " + issue_type.replace("_", " ")
    return title.replace("-", " ").replace("  ", " ")


TEMPLATE_CONCENTRATION = 0.7
TEMPLATE_PATTERNS = 3


def _fix_scope(issue_type: str, rows: List[Dict]) -> str:
    """Where the work happens: one setting, one template, or page by page.

    A fault on 280 pages that all share one URL shape is one template edit,
    not 280 page edits. Counting only the largest shape was still too strict:
    251 missing search descriptions spread over a blog, a location and a
    workspace template are three template edits, and the report called them
    251 page edits. So the top three shapes are counted together.
    """
    if unit_of(issue_type) == "site" or issue_type in SITE_LEVEL_TYPES:
        return "config"
    if not rows:
        return "page"
    patterns = Counter(pattern_of(r.get("final_url") or r.get("url") or "")
                       for r in rows)
    top = sum(c for _p, c in patterns.most_common(TEMPLATE_PATTERNS))
    if len(rows) > 1 and top / len(rows) >= TEMPLATE_CONCENTRATION:
        return "template"
    return "page"


def _affected_share(candidate: Dict, pages_affected: int, parsed: int,
                    totals: Dict[str, int]) -> Dict:
    """How many pages a fix touches. For target types that is the pages that
    link to the broken thing, not the number of broken things."""
    if unit_of(candidate["issue_type"]) == "target":
        # A detail line names at most five referrers, so counting named ones
        # undercounts. The largest single target's referrer count is a lower
        # bound on distinct referring pages and never overstates.
        named = set()
        for row in candidate["rows"]:
            named.update(_referrers_named(row.get("detail", "")))
        biggest = max((_referrer_count(r.get("detail", ""))
                       for r in candidate["rows"]), default=0)
        count = max(len(named), biggest)
        return share(min(count, parsed) or min(pages_affected, parsed),
                     parsed, "pages parsed")
    return share(pages_affected, parsed, "pages parsed")


def _targets_share(candidate: Dict, totals: Dict[str, int]) -> Optional[Dict]:
    """For a target type, the second number: broken things out of things
    checked. "118 of 842 pages" was 118 external links, not 118 pages."""
    issue_type = candidate["issue_type"]
    if unit_of(issue_type) != "target":
        return None
    count = len(candidate["rows"])
    if issue_type == "external_link_broken":
        return share(count, totals.get("external_checked", count) or count,
                     "links to other websites checked")
    # The whole is the addresses on this site the crawl actually reached, not
    # the number of links between them: "53 of 106768 internal link targets"
    # divided a count of addresses by a count of link instances and meant
    # nothing to anybody.
    return share(count, totals.get("internal_targets", count) or count,
                 "internal addresses crawled")


_REFERRER_LIST_RE = re.compile(r"linked from \d+ page\(s\): (.+)$")


def _referrers_named(detail: str) -> List[str]:
    """The referring pages a link finding names, as far as it lists them."""
    match = _REFERRER_LIST_RE.search(detail or "")
    if not match:
        return []
    listed = match.group(1)
    listed = re.sub(r",\s*and \d+ more.*$", "", listed)
    return [u.strip() for u in listed.split(",") if u.strip().startswith("http")]


def _prioritised_fixes(page_issues: List[Dict],
                       severities: Dict[str, str],
                       parsed: int, totals: Optional[Dict[str, int]] = None
                       ) -> List[Dict]:
    """Up to 15 fixes, ordered by reach times severity. A group is one fix."""
    totals = totals or {}
    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for row in page_issues:
        by_type[row["issue_type"]].append(row)

    candidates = []
    for issue_type, rows in by_type.items():
        severity = severities.get(issue_type) or rows[0].get("severity", "low")
        weight = SEVERITY_WEIGHT.get(severity, 1)
        if weight == 0:
            continue  # unmeasured is not a fix

        if issue_type in GROUPED_TYPES:
            grouper = (parameter_pattern_of
                       if issue_type == "hreflang_not_reciprocal"
                       else pattern_of)
            groups = group_by_pattern(
                [(r.get("final_url") or r.get("url"), r) for r in rows],
                pattern=grouper)
            for pattern, members in groups:
                reach = sum(_referrer_count(m.get("detail", ""))
                            for m in members) or len(members)
                candidates.append({
                    "issue_type": issue_type, "pattern": pattern,
                    "rows": members, "reach": reach,
                    "severity": severity, "weight": weight,
                    "patterns": 1,
                })
        else:
            patterns = len({pattern_of(r.get("final_url") or r.get("url"))
                            for r in rows})
            candidates.append({
                "issue_type": issue_type, "pattern": None,
                "rows": rows, "reach": len(rows),
                "severity": severity, "weight": weight,
                "patterns": patterns,
            })

    # A3: impact is how many pages a fix touches, weighted by severity.
    # Multiplying by link instances let one low-severity redirect pattern with
    # 9,564 inbound links outrank every real defect on the site.
    for c in candidates:
        c["impact"] = len(c["rows"]) * c["weight"]
    candidates.sort(key=lambda c: (-c["impact"], -c["reach"],
                                   c["issue_type"], c["pattern"] or ""))

    # A2: one entry per issue type in the list. Sibling patterns of the same
    # type are merged under the biggest, with their patterns named in the
    # evidence, so a single fault does not fill four of the top five slots.
    merged: List[Dict] = []
    seen_types: Dict[str, Dict] = {}
    for c in candidates:
        first = seen_types.get(c["issue_type"])
        if first is None:
            c["sibling_patterns"] = []
            seen_types[c["issue_type"]] = c
            merged.append(c)
            continue
        if c["pattern"]:
            first["sibling_patterns"].append({
                "pattern": c["pattern"],
                "pages_affected": len(c["rows"]),
                "reach": c["reach"],
            })
        first["rows"] = first["rows"] + c["rows"]
        first["reach"] += c["reach"]
        first["impact"] = len(first["rows"]) * first["weight"]
    candidates = sorted(merged, key=lambda c: (-c["impact"], -c["reach"],
                                               c["issue_type"]))

    fixes = []
    for index, c in enumerate(candidates[:MAX_FIXES], start=1):
        pages_affected = len(c["rows"])
        fixes.append({
            "id": f"fix-{index:02d}",
            "title": _title_for(c["issue_type"]),
            "issue_type": c["issue_type"],
            "category": _category_of(c["issue_type"]),
            "severity": c["severity"],
            "pattern": c["pattern"],
            "label": label_of(c["issue_type"]),
            "unit": unit_of(c["issue_type"]),
            # The count is targets for a target type and pages otherwise.
            # Passing reach here said "21372 addresses no longer load" when
            # 53 addresses were broken and 21372 was the number of links
            # pointing at them.
            "plain_finding": plain_finding(c["issue_type"], pages_affected,
                                           parsed),
            "pages_affected": _affected_share(c, pages_affected, parsed,
                                              totals),
            "targets": _targets_share(c, totals),
            "reach": c["reach"],
            "impact": c["impact"],
            "sibling_patterns": c.get("sibling_patterns", [])[:MAX_EVIDENCE],
            "pattern_count": 1 + len(c.get("sibling_patterns", [])),
            "fix_scope": _fix_scope(c["issue_type"], c["rows"]),
            "evidence": _evidence(c["rows"], "final_url", "url", "detail"),
        })
    return fixes


# --- comparison -------------------------------------------------------------

def _complete_runs(host_dir: str) -> List[str]:
    if not os.path.isdir(host_dir):
        return []
    out = []
    for name in sorted(os.listdir(host_dir)):
        path = os.path.join(host_dir, name)
        if os.path.isdir(path) and os.path.exists(
                os.path.join(path, "crawl_summary.json")):
            out.append(path)
    return out


def previous_run_dir(run_dir: str) -> Optional[str]:
    """The newest other complete run for the same host, or None."""
    run_dir = os.path.normpath(run_dir)
    host_dir = os.path.dirname(run_dir)
    others = [p for p in _complete_runs(host_dir)
              if os.path.normpath(p) != run_dir]
    return others[-1] if others else None


def _metric_values(summary: Dict, issues: List[Dict]) -> Dict[str, Any]:
    """The handful of numbers worth comparing between two runs."""
    return {
        "pages_found": _dig(summary, "pages_found"),
        "pages_parsed": _dig(summary, "pages_parsed"),
        "sitemap_urls_total": _dig(summary, "sitemap_sweep",
                                   "sitemap_urls_total"),
        "site_score": _dig(summary, "score", "site_score"),
        "mean_mobile_score": _dig(summary, "pagespeed", "mean_mobile_score"),
        "orphan_pages": _dig(summary, "links", "pages_with_zero_inlinks"),
        "broken_internal_targets": _dig(summary, "links",
                                        "broken_internal_targets"),
        "external_broken": _dig(summary, "links", "external_broken"),
    }


def compare(run_dir: str, previous_dir: Optional[str] = None) -> Dict:
    """Compare a run against a previous one, tolerating a drifted summary.

    A metric the older run never recorded is reported as absent, never as a
    change: two versions of the tool are not two states of the site.
    """
    if previous_dir is None:
        previous_dir = previous_run_dir(run_dir)
    if not previous_dir or not os.path.exists(
            os.path.join(previous_dir, "crawl_summary.json")):
        return {"previous_run": None}

    now_summary = _read_json(os.path.join(run_dir, "crawl_summary.json"))
    old_summary = _read_json(os.path.join(previous_dir, "crawl_summary.json"))
    now_issues = _read_csv(os.path.join(run_dir, "page_issues.csv"))
    old_issues = _read_csv(os.path.join(previous_dir, "page_issues.csv"))

    now_metrics = _metric_values(now_summary, now_issues)
    old_metrics = _metric_values(old_summary, old_issues)

    deltas: Dict[str, Any] = {}
    notable: List[Dict] = []
    for name, current in now_metrics.items():
        before = old_metrics.get(name)
        if before is None:
            deltas[name] = {"now": current, "previous": _MISSING,
                            "change": _MISSING}
            continue
        if current is None:
            deltas[name] = {"now": None, "previous": before, "change": None}
            continue
        # A1: one decimal. A raw float subtraction printed
        # "-1.2000000000000028" into a client-facing document.
        change = round(current - before, 1)
        deltas[name] = {"now": current, "previous": before, "change": change}
        moved_enough = (abs(change) >= NOTABLE_ABSOLUTE
                        or (before and abs(change) / abs(before) >= NOTABLE_RATIO))
        if moved_enough and change:
            notable.append({
                "metric": name, "previous": before, "now": current,
                "change": change,
                "direction": "up" if change > 0 else "down",
            })

    def keyed(rows: List[Dict]) -> set:
        return {(r.get("issue_type"), r.get("final_url") or r.get("url"))
                for r in rows}

    now_keys, old_keys = keyed(now_issues), keyed(old_issues)
    new_only, resolved = now_keys - old_keys, old_keys - now_keys
    by_type: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"new": 0, "resolved": 0, "unchanged": 0})
    for issue_type, _url in new_only:
        by_type[issue_type]["new"] += 1
    for issue_type, _url in resolved:
        by_type[issue_type]["resolved"] += 1
    for issue_type, _url in now_keys & old_keys:
        by_type[issue_type]["unchanged"] += 1

    # A4: when the crawl saw a different amount of the site, count movements
    # are not the site changing. Orphans "rose" 56 to 169 between two runs
    # only because the sitemap recovered from 106 URLs to 709.
    coverage_changed = False
    for metric in ("sitemap_urls_total", "pages_found"):
        entry = deltas.get(metric) or {}
        before, change = entry.get("previous"), entry.get("change")
        if isinstance(before, (int, float)) and isinstance(change, (int, float)):
            if before and abs(change) / abs(before) > NOTABLE_RATIO:
                coverage_changed = True

    sitemap_delta = deltas.get("sitemap_urls_total") or {}
    sitemap_changed = (isinstance(sitemap_delta.get("change"), (int, float))
                       and sitemap_delta.get("change"))

    for item in notable:
        if item["metric"] == "sitemap_urls_total":
            # The site's own sitemap, not our crawl. Saying otherwise blamed
            # the crawler for the client's instability.
            item["attribution"] = "site"
            item["note"] = (
                f"the site's sitemap listed {item['previous']} URLs last time "
                f"and {item['now']} now")
        elif coverage_changed:
            item["attribution"] = "coverage"
            item["note"] = NOT_COMPARABLE

    if coverage_changed:
        # New and resolved counts are a function of how much was crawled, so
        # they say nothing while coverage is moving.
        notable = [n for n in notable
                   if n["metric"] == "sitemap_urls_total"
                   or n["metric"] not in COVERAGE_METRICS]

    return {
        "previous_run": os.path.basename(os.path.normpath(previous_dir)),
        "previous_run_path": previous_dir,
        "coverage_changed": coverage_changed,
        "sitemap_changed": bool(sitemap_changed),
        "issue_counts_comparable": not coverage_changed,
        "deltas": deltas,
        "notable": sorted(notable, key=lambda n: -abs(n["change"])),
        "issue_counts": dict(sorted(by_type.items())),
        "issues_new": len(new_only),
        "issues_resolved": len(resolved),
        "issues_unchanged": len(now_keys & old_keys),
    }


# --- the whole thing --------------------------------------------------------

def build_findings(run_dir: str, previous_dir: Optional[str] = None,
                   compare_enabled: bool = True,
                   write: bool = True) -> Dict:
    """Read a run's own files and produce findings.json."""
    from .issues import PAGE_ISSUE_SEVERITY

    summary = _read_json(os.path.join(run_dir, "crawl_summary.json"))
    pages = _read_csv(os.path.join(run_dir, "audit_pages.csv"))
    page_issues = _read_csv(os.path.join(run_dir, "page_issues.csv"))
    crawl_issues = _read_csv(os.path.join(run_dir, "crawl_issues.csv"))
    raw = _read_csv(os.path.join(run_dir, "raw_crawl.csv"))
    pagespeed = _read_csv(os.path.join(run_dir, "pagespeed.csv"))

    parsed = _dig(summary, "pages_parsed", default=len(pages)) or len(pages)

    issues_by_type: Dict[str, List[Dict]] = defaultdict(list)
    for row in page_issues:
        issues_by_type[row["issue_type"]].append(row)

    findings = {
        "meta": _meta(summary, run_dir),
        "headline": _headline(summary),
        "crawlability": _crawlability(summary, crawl_issues, raw),
        "indexability_technical": _indexability_technical(
            summary, issues_by_type, parsed),
        "on_page": _on_page(summary, issues_by_type, pages, parsed),
        "content": _content(summary, issues_by_type, pages, parsed),
        "schema": _schema(summary, issues_by_type, pages, parsed),
        "links": _links(summary, issues_by_type, parsed),
        "performance": _performance(summary, pagespeed, pages, parsed),
        "prioritised_fixes": _prioritised_fixes(
            page_issues, PAGE_ISSUE_SEVERITY, parsed,
            totals={
                "external_checked": _dig(summary, "links", "external_checked",
                                         default=0),
                # Addresses crawled, not link edges: see _targets_share.
                "internal_targets": _dig(summary, "pages_found", default=0),
            }),
        "search_performance": {"provided": False},
        "comparison": (compare(run_dir, previous_dir) if compare_enabled
                       else {"previous_run": None}),
    }

    if write:
        path = os.path.join(run_dir, "findings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(findings, handle, indent=2, ensure_ascii=False)
    return findings
