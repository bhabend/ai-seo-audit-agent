"""Prose for the report. The model explains; the facts are ours.

The division of labour is structural, not a matter of prompting politely.
Every "what we found" paragraph is written here, by code, from the findings:
each issue type with a count above zero, said through its own plain sentence
with its own count and its own whole. The model is never asked what was
found, so it can never attach a right number to the wrong noun, and a section
can never open on something that was not found.

What the model is asked for is why it matters and what to do. That comes back
through five guards before it reaches the document:

  1. Dashes are removed. The client asked for prose without them, and a model
     reaches for an em dash in almost every paragraph.
  2. Jargon is refused. A word the glossary does not carry sends the section
     back once, and a second offence falls back to the templated wording.
  3. Advice about the audit itself is dropped. "Expand measurement to the 125
     unmeasured templates" is work for us, not for the client.
  4. Every number in the prose must already exist in the section it was
     written from, or in the facts paragraph we handed over. A sentence
     carrying a figure we cannot find is dropped. A fabricated statistic in a
     client deliverable is worse than a dull one.
  5. Length is capped, because a section that runs long buries the finding.

Every drop is recorded in report_usage.json, so a reader can see where the
model was overruled rather than having to trust that it was.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_MODEL = "gpt-5-mini"
API_URL = "https://api.openai.com/v1/chat/completions"
KEY_ENV_VAR = "OPENAI_API_KEY"

# One call per narrative section plus the executive summary, plus up to five
# regenerations of sections that came back cut off.
CALL_TIMEOUT = 90
RETRY_AFTER_SECONDS = 5
RETRY_STATUSES = (429, 500, 502, 503, 504)
# Refusals that must stop the run rather than be retried.
BILLING_STATUSES = (401, 402, 403)

MAX_WORDS_PER_SECTION = 320
MAX_WORDS_SUMMARY = 220
# Room to finish a thought. Session 8 ran with no cap and five sections still
# ended mid sentence, because reasoning tokens ate the budget.
MAX_COMPLETION_TOKENS = 2500
# Ceiling raised for the regeneration a truncated section is allowed.
MAX_CALLS = 16

# Words a marketing lead should not have to look up, and the plain phrasing
# the glossary and the findings already use instead. Checked on the model's
# text only: the facts paragraph is written from plain_finding, which never
# contains any of them.
BANNED_WORDS = (
    "equity", "link equity", "referral equity", "crawl budget", "crawl waste",
    "serp", "serps", "canonicalise", "canonicalize", "canonicalisation",
    "canonicalization", "indexation", "ux", "dom", "hreflang", "hsts",
    "simhash", "lighthouse", "noindex", "nofollow", "canonical", "canonicals",
)

PREFERRED_WORDING = (
    "preferred address (not canonical), hidden from search (not noindex), "
    "secure connection enforced (not HSTS), language version (not hreflang), "
    "page title (not title tag), search description (not meta description), "
    "image description (not alt text), link wording (not anchor text)"
)

STYLE = (
    "You are writing one section of an SEO audit report for the head of "
    "marketing at the audited company. They are clever but not technical.\n"
    "The section's findings paragraph is already written and is given to "
    "you. You are not asked what was found.\n"
    "Return JSON only, with exactly these keys:\n"
    '  {"why": "one paragraph", "todo": ["sentence", "sentence"]}\n'
    "Rules:\n"
    "- why: why these findings matter to this business. Under 110 words.\n"
    "- todo: up to five actions, each a complete sentence ending in a full "
    "stop, each something the client's own team does to the website.\n"
    "- Never repeat the findings paragraph back. It is already in the "
    "document above your text.\n"
    "- Never advise anything about this audit itself. No measuring, "
    "sampling, re-crawling, expanding coverage or auditing further. The "
    "client fixes the site; the audit is our job.\n"
    "- Never define a term. A glossary elsewhere does that.\n"
    "- Never use any of these words: " + ", ".join(BANNED_WORDS) + ".\n"
    "- Use this wording instead: " + PREFERRED_WORDING + ".\n"
    "- Never use an internal identifier. Words joined by underscores are "
    "never allowed. Use the plain labels given to you.\n"
    "- Never use a dash of any kind. No em dash, no en dash, no hyphen as "
    "punctuation. Use a comma or a full stop.\n"
    "- Never state a number that is not in the text you were given.\n"
    "- Do not restate every figure. Tables and charts carry the numbers. "
    "Explain what they mean.\n"
    "- Plain sentences. No marketing language, no exclamation marks.\n"
    "- Finish every sentence. Never stop mid clause.\n"
)

# Keys that describe how the audit works, not what it found. The model never
# sees them: "simhash" and "min_words_for_comparison" are our business.
INTERNAL_KEY_PARTS = (
    "simhash", "md5", "pattern_of", "threshold", "min_words",
    "pages_compared", "check_limit", "attempts", "ceiling", "stage_seconds",
    "coverage_changed", "previous_run_path", "impact", "reach", "weight",
    "issue_type", "run_id", "method", "sweep_limit", "edges",
)
INTERNAL_KEY_SUFFIXES = ("_id", "_md5", "_simhash", "_path")

MAX_TODO_ITEMS = 5


# --- guard 1: dashes --------------------------------------------------------

_EM_EN = re.compile(r"\s*[‐-―−]\s*")
_SPACED_HYPHEN = re.compile(r"(?<=\S) - (?=\S)")
# A bullet marker at the start of a line. Invisible to the rule above, but a
# Word paragraph folds the line break into a space on the way back out, so a
# newline followed by "- item" becomes " - item" and reads as a dash in the
# finished document. Caught live on the first real report.
_LINE_BULLET = re.compile(r"(?m)^[ \t]*[-*•]+[ \t]+")


def strip_dashes(text: str) -> str:
    """Remove dashes used as punctuation, keep hyphens inside words and URLs.

    An em dash joins two clauses, so a comma is the honest replacement. A
    hyphen with spaces around it does the same job and goes the same way.
    `on-demand` and `wework.co.in/a-b` are data and are left alone.

    Bullet markers go first: a model writes lists as a line beginning with a
    hyphen, which the rules below would miss, and Word folds the line break
    into a space so the marker resurfaces as a dash.
    """
    text = _LINE_BULLET.sub("", text)
    text = _EM_EN.sub(", ", text)
    text = _SPACED_HYPHEN.sub(", ", text)
    # A replacement can leave ", ," or a comma before a full stop.
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r",\s*([.;:!?])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# --- guard 2: numbers -------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _numbers_in(value: Any, out: Optional[Set[str]] = None) -> Set[str]:
    """Every number a section legitimately contains, as comparable strings."""
    out = set() if out is None else out
    if isinstance(value, bool):
        return out
    if isinstance(value, int):
        out.add(str(value))
    elif isinstance(value, float):
        out.add(str(round(value, 1)))
        out.add(str(int(value)) if value == int(value) else str(value))
    elif isinstance(value, str):
        for token in _NUMBER.findall(value):
            out.add(token.replace(",", "").rstrip("%"))
    elif isinstance(value, dict):
        # A share object also licenses its percentage.
        if {"count", "whole"} <= set(value) and value.get("whole"):
            pct = value["count"] / value["whole"] * 100
            out.add(str(round(pct)))
            out.add(str(round(pct, 1)))
        for item in value.values():
            _numbers_in(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _numbers_in(item, out)
    return out


def _allowed_numbers(section: Any, also: str = "") -> Set[str]:
    allowed = _numbers_in(section)
    # The facts paragraph is ours and its numbers come from the findings, but
    # a whole taken from the run's coverage is not always inside the section
    # the model was given, so what we said is licensed too.
    allowed.update(_numbers_in(also))
    # Small integers are ordinary English ("three things", "one page").
    allowed.update(str(n) for n in range(0, 11))
    return allowed


def check_numbers(text: str, section: Any,
                  also: str = "") -> Tuple[str, List[str]]:
    """Drop any sentence carrying a number the section does not contain."""
    allowed = _allowed_numbers(section, also)
    kept, dropped = [], []
    for sentence in _SENTENCE.split(text):
        if not sentence.strip():
            continue
        bad = [n for n in _NUMBER.findall(sentence)
               if n.replace(",", "").rstrip("%") not in allowed]
        if bad:
            dropped.append(sentence.strip())
        else:
            kept.append(sentence.strip())
    return " ".join(kept), dropped


# --- guard: jargon ----------------------------------------------------------

_JARGON = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(BANNED_WORDS, key=len,
                                                   reverse=True)) + r")\b",
    re.I)


def find_jargon(*texts: Any) -> List[str]:
    """Banned words in the model's text, in the order they appear."""
    hits: List[str] = []
    for text in texts:
        items = text if isinstance(text, (list, tuple)) else [text]
        for item in items:
            for match in _JARGON.finditer(str(item or "")):
                word = match.group(1).lower()
                if word not in hits:
                    hits.append(word)
    return hits


# --- guard: advice about the audit rather than the site ---------------------

# An action item is for the client's website. "Expand measurement to the 125
# unmeasured templates" is a note to ourselves that reached a client report.
_ABOUT_THE_AUDIT = re.compile(
    r"\b(measure\w*|sampl\w*|unmeasured|audit\w*|re-?run|rerun|recrawl)\b"
    r"|\bcrawl the\b|\bexpand coverage\b|\bexpand (the )?crawl\b", re.I)


def is_about_the_audit(item: str) -> bool:
    return bool(_ABOUT_THE_AUDIT.search(str(item or "")))


def filter_todo(items: Iterable[str]) -> Tuple[List[str], List[str]]:
    """(kept, dropped): actions on the site, and actions on the audit."""
    kept, dropped = [], []
    for item in items:
        (dropped if is_about_the_audit(item) else kept).append(item)
    return kept, dropped


# --- guard 3: length --------------------------------------------------------

def cap_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return text
    trimmed = " ".join(words[:limit])
    cut = trimmed.rfind(".")
    return trimmed[:cut + 1] if cut > 40 else trimmed + "."


# --- templated fallback -----------------------------------------------------

def _describe_share(name: str, obj: Dict) -> str:
    count, whole, whole_is = obj["count"], obj["whole"], obj["whole_is"]
    label = name.replace("_", " ")
    if not whole:
        return f"{label}: {count}."
    return f"{label}: {count} of {whole} {whole_is}."


def _walk_shares(node: Any, prefix: str = "",
                 out: Optional[List[Tuple[str, Dict]]] = None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if {"count", "whole", "whole_is"} <= set(node):
            out.append((prefix, node))
        for key, value in node.items():
            _walk_shares(value, key, out)
    elif isinstance(node, list):
        for item in node[:5]:
            _walk_shares(item, prefix, out)
    return out


def _is_internal_key(key: str) -> bool:
    lowered = str(key).lower()
    if any(lowered.endswith(suffix) for suffix in INTERNAL_KEY_SUFFIXES):
        return True
    return any(part in lowered for part in INTERNAL_KEY_PARTS)


def curate(node: Any) -> Any:
    """The section as the model should see it.

    Internal machinery is removed and identifiers are replaced with their
    plain labels, so the model cannot repeat back a word like simhash or
    title_missing even if it wanted to.
    """
    from .issues import PAGE_ISSUE_META, label_of

    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if _is_internal_key(key):
                continue
            clean_key = str(key).replace("_", " ")
            if isinstance(value, str) and value in PAGE_ISSUE_META:
                out[clean_key] = label_of(value)
            else:
                out[clean_key] = curate(value)
        return out
    if isinstance(node, list):
        return [curate(item) for item in node]
    if isinstance(node, str) and node in PAGE_ISSUE_META:
        return label_of(node)
    return node


def parse_sections(text: str) -> Optional[Dict]:
    """The model's JSON, or None when it will not parse.

    Models fence JSON in code blocks and add a line of preamble; both are
    stripped before giving up.
    """
    if not text:
        return None
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    todo = parsed.get("todo") or []
    if isinstance(todo, str):
        todo = [todo]
    return {"found": str(parsed.get("found") or "").strip(),
            "why": str(parsed.get("why") or "").strip(),
            "todo": [str(t).strip() for t in todo if str(t).strip()]}


_ENDS_PROPERLY = re.compile(r"[.!?][\"')\]]?$")


def _is_truncated(parsed: Dict, finish_reason: str = "stop") -> bool:
    """True when the answer stopped in the middle of something.

    Only the parts the model is asked for are checked. "found" is written by
    code now, so an answer without one is complete, not cut off.
    """
    if finish_reason == "length":
        return True
    for key in ("why",):
        value = (parsed.get(key) or "").strip()
        if not value or not _ENDS_PROPERLY.search(value):
            return True
    todo = parsed.get("todo") or []
    if todo and not _ENDS_PROPERLY.search(str(todo[-1]).strip()):
        return True
    return False


def _clean_section(section: Dict, limit: int) -> Dict:
    """Dashes out and length capped, on every part."""
    return {
        "found": cap_words(strip_dashes(section.get("found", "")), limit),
        "why": cap_words(strip_dashes(section.get("why", "")), limit),
        "todo": [strip_dashes(t) for t in section.get("todo", [])
                 if str(t).strip()][:MAX_TODO_ITEMS],
    }


def restate_findings(section: Any) -> Tuple[str, List[str]]:
    """Say the findings plainly, from the data, naming each one.

    Used when the number guard drops a sentence. A count alone is not a
    finding: five blog posts with a broken preferred address must survive the
    guard that removed the sentence describing them.
    """
    from .issues import PAGE_ISSUE_META, label_of, plain_finding

    counts = _collect_issue_counts(section)
    if not counts:
        return "", []

    sentences, named = [], []
    for issue_type, (count, whole, evidence) in sorted(
            counts.items(), key=lambda kv: -kv[1][0])[:MAX_TODO_ITEMS]:
        if count <= 0:
            continue
        sentence = plain_finding(issue_type, count, whole).capitalize()
        if evidence:
            sentence += ". For example " + ", ".join(evidence[:3])
        sentences.append(sentence.rstrip(".") + ".")
        named.append(label_of(issue_type))
    return " ".join(sentences), named


def _collect_issue_counts(node: Any, out: Optional[Dict] = None) -> Dict:
    """Every issue type named in a section, with its count and examples."""
    from .issues import PAGE_ISSUE_META

    out = {} if out is None else out
    if isinstance(node, dict):
        # A section names its issues two ways: as a field on a fix row, and
        # as a key mapping to a share object, which is how the indexability
        # section carries "canonical_target_non_200: 5 of 842". Missing the
        # second shape is how five real findings vanished from a report.
        for key, value in node.items():
            if key not in PAGE_ISSUE_META:
                continue
            if isinstance(value, dict) and "count" in value:
                count, whole = value.get("count", 0), value.get("whole", 0)
            elif isinstance(value, int) and not isinstance(value, bool):
                count, whole = value, 0
            else:
                continue
            previous = out.get(key, (0, 0, []))
            out[key] = (max(previous[0], int(count or 0)),
                        whole or previous[1], previous[2])

        issue_type = node.get("issue_type") or node.get("type")
        if issue_type in PAGE_ISSUE_META:
            affected = node.get("pages_affected") or {}
            count = (affected.get("count") if isinstance(affected, dict)
                     else None)
            if count is None:
                count = node.get("count") or node.get("pages_affected") or 0
            whole = (affected.get("whole") if isinstance(affected, dict)
                     else 0) or 0
            evidence = []
            for item in (node.get("evidence") or [])[:3]:
                if isinstance(item, dict):
                    url = item.get("final_url") or item.get("url")
                    if url:
                        evidence.append(url)
            previous = out.get(issue_type, (0, 0, []))
            out[issue_type] = (max(previous[0], int(count or 0)),
                               whole or previous[1],
                               evidence or previous[2])
        for value in node.values():
            _collect_issue_counts(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_issue_counts(item, out)
    return out


def templated_section(section_name: str, section: Any,
                      facts: Optional[str] = None,
                      default_whole: int = 0) -> Dict:
    """The three parts, built from the data, for --no-ai and for fallback."""
    if facts is None:
        facts = section_facts(section_name, section, default_whole)
    return {
        "found": facts,
        "why": ("These are the findings for this part of the audit. The "
                "priority fixes section lists what to do first."),
        "todo": ["Work through the fixes listed for this section in the "
                 "priority fixes table, starting with the highest severity."],
    }


def templated(section_name: str, section: Any, limit: int = 5,
              skip_zero: bool = False) -> str:
    """Sentences built from the section itself, used with --no-ai and as the
    replacement when a model sentence is dropped."""
    title = section_name.replace("_", " ")
    shares = _walk_shares(section)
    if skip_zero:
        shares = [(name, obj) for name, obj in shares if obj.get("count")]
    if shares:
        body = " ".join(_describe_share(n, o).capitalize()
                        for n, o in shares[:limit])
        return f"This section covers {title}. {body}"
    if isinstance(section, dict):
        simple = [f"{k.replace('_', ' ')}: {v}" for k, v in section.items()
                  if isinstance(v, (int, float, str)) and not isinstance(v, bool)]
        if simple:
            return (f"This section covers {title}. "
                    + ". ".join(s.capitalize() for s in simple[:limit]) + ".")
    return f"This section covers {title}."


# --- the facts, written by code ---------------------------------------------

# Where each section keeps the count for an issue type. Written out rather
# than guessed, because the sections nest their counts under plain names
# ("title" then "missing") and a walker that matched on shape alone would
# have to know which share meant which finding anyway.
FACT_PATHS: Dict[str, Tuple[Tuple[str, Tuple[str, ...]], ...]] = {
    "indexability_technical": (
        ("noindex_page", ("noindex_pages",)),
        ("canonical_missing", ("canonical", "canonical_missing")),
        ("canonical_off_page", ("canonical", "canonical_off_page")),
        ("canonical_not_absolute", ("canonical", "canonical_not_absolute")),
        ("canonical_chain", ("canonical", "canonical_chain")),
        ("canonical_target_non_200", ("canonical", "canonical_target_non_200")),
        ("canonical_target_noindex", ("canonical", "canonical_target_noindex")),
        ("canonical_target_not_crawled",
         ("canonical", "canonical_target_not_crawled")),
        ("canonical_target_unchecked",
         ("canonical", "canonical_target_unchecked")),
        ("canonical_consolidates_variant",
         ("canonical", "canonical_consolidates_variant")),
        ("hreflang_not_reciprocal", ("hreflang_not_reciprocal",
                                     "pages_affected")),
        ("hreflang_target_non_200", ("hreflang_target_non_200",)),
        ("mixed_content", ("mixed_content",)),
    ),
    "on_page": (
        ("title_missing", ("title", "missing")),
        ("title_too_long", ("title", "too_long")),
        ("title_too_short", ("title", "too_short")),
        ("duplicate_title", ("title", "duplicate_groups")),
        ("meta_description_missing", ("meta_description", "missing")),
        ("meta_description_too_long", ("meta_description", "too_long")),
        ("duplicate_meta_description",
         ("meta_description", "duplicate_groups")),
        ("h1_missing", ("h1", "missing")),
        ("h1_multiple", ("h1", "multiple")),
        ("images_missing_alt", ("images_missing_alt",)),
        ("viewport_missing", ("viewport_missing",)),
        ("html_lang_missing", ("html_lang_missing",)),
    ),
    "content": (
        ("thin_page", ("thin_pages",)),
        ("duplicate_content", ("duplicate_content", "needs_attention")),
        ("near_duplicate_content",
         ("near_duplicate_content", "needs_attention")),
    ),
    "schema": (
        ("schema_missing", ("coverage", "none")),
        ("schema_microdata_only", ("coverage", "microdata_only")),
        ("schema_invalid_json", ("invalid_json",)),
        ("schema_missing_property", ("missing_property_pages",)),
    ),
    "links": (
        ("orphan_page", ("orphan_pages",)),
        ("low_inlink_page", ("low_inlink_pages",)),
        ("broken_internal_link", ("broken_internal", "targets")),
        ("redirected_internal_link", ("redirected_internal", "targets")),
        ("external_link_broken", ("external", "broken")),
        ("generic_anchor", ("generic_anchor",)),
        ("nofollow_internal_link", ("nofollow_internal",)),
    ),
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "unmeasured": 3, "info": 4}
MAX_FACT_SENTENCES = 10


def _value_at(node: Any, path: Tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _count_and_whole(value: Any, default_whole: int) -> Tuple[int, int]:
    """A count and what it is out of, from a share, an int or a list."""
    if isinstance(value, dict) and "count" in value:
        return (int(value.get("count") or 0),
                int(value.get("whole") or 0) or default_whole)
    if isinstance(value, bool) or value is None:
        return 0, default_whole
    if isinstance(value, (int, float)):
        return int(value), default_whole
    if isinstance(value, list):
        return len(value), default_whole
    return 0, default_whole


def fact_entries(section_name: str, section: Any,
                 default_whole: int = 0) -> List[Tuple[str, int, int]]:
    """(issue type, count, whole) for everything this section actually found.

    Zero counts are left out entirely: a section that opens on what was not
    found buries what was.
    """
    entries: List[Tuple[str, int, int]] = []
    seen: Set[str] = set()
    for issue_type, path in FACT_PATHS.get(section_name, ()):
        count, whole = _count_and_whole(_value_at(section, path),
                                        default_whole)
        if count > 0:
            entries.append((issue_type, count, whole))
            seen.add(issue_type)

    from .issues import PAGE_ISSUE_META

    # Site level settings are recorded as a list of types, not as counts.
    for issue_type in (_value_at(section, ("site_level", "types")) or []):
        if issue_type in PAGE_ISSUE_META and issue_type not in seen:
            entries.append((issue_type, 1, 0))
            seen.add(issue_type)

    if not entries:
        # Sections this table does not cover, and the shapes the fixes list
        # and the tests use: read whatever issue types are named inside.
        for issue_type, (count, whole, _ev) in _collect_issue_counts(
                section).items():
            if count > 0 and issue_type not in seen:
                entries.append((issue_type, count, whole or default_whole))
                seen.add(issue_type)
    return entries


def _positive_sentences(section_name: str, section: Any,
                        entries: List[Tuple[str, int, int]]) -> List[str]:
    """What the site already does right, said last and said once."""
    from .issues import plain_finding

    out = []
    for issue_type, count, whole in entries:
        out.append(plain_finding(issue_type, count, whole))
    if section_name == "content":
        handled = 0
        for key in ("duplicate_content", "near_duplicate_content"):
            handled += _count_and_whole(
                _value_at(section, (key, "canonical_handled")), 0)[0]
        if handled:
            out.append(f"{handled} groups of pages with the same text already "
                       f"name one of themselves as the preferred address")
    if section_name == "indexability_technical":
        types = _value_at(section, ("site_level", "types"))
        if isinstance(types, list) and not types:
            out.append("the site wide settings this report checks, including "
                       "a secure connection and the security headers, need "
                       "no work")
    return out


def section_facts(section_name: str, section: Any,
                  default_whole: int = 0) -> str:
    """The "what we found" paragraph, built from the findings themselves."""
    from .issues import PAGE_ISSUE_SEVERITY, plain_finding

    if _is_fix_list(section):
        return _fix_list_facts(section)

    entries = fact_entries(section_name, section, default_whole)
    negatives = [e for e in entries
                 if PAGE_ISSUE_SEVERITY.get(e[0]) != "info"]
    positives = [e for e in entries if PAGE_ISSUE_SEVERITY.get(e[0]) == "info"]
    negatives.sort(key=lambda e: (SEVERITY_ORDER.get(
        PAGE_ISSUE_SEVERITY.get(e[0], "low"), 2), -e[1]))

    sentences = [plain_finding(t, c, w).capitalize().rstrip(".") + "."
                 for t, c, w in negatives[:MAX_FACT_SENTENCES]]
    if section_name == "crawlability":
        sentences = _crawlability_facts(section) + sentences
    sentences = _performance_facts(section_name, section) + sentences

    good = _positive_sentences(section_name, section, positives)
    if good:
        sentences.append("The site also gets some things right: "
                         + "; ".join(good) + ".")

    if not sentences:
        return templated(section_name, section, skip_zero=True)
    return " ".join(sentences)


def _performance_facts(section_name: str, section: Any) -> List[str]:
    """Speed has no page counts of its own: it is measured per template."""
    if section_name != "performance" or not isinstance(section, dict):
        return []
    if section.get("skipped"):
        return ["Speed was not measured for this audit."]
    represented = section.get("pages_represented") or {}
    measured = section.get("templates_measured") or []
    out = []
    if measured:
        out.append(f"Speed was measured on {len(measured)} page templates, "
                   f"covering {represented.get('count', 0)} of "
                   f"{represented.get('whole', 0)} pages read.")
    mobile = section.get("mean_mobile_score")
    desktop = section.get("mean_desktop_score")
    if mobile is not None:
        out.append(f"The measured templates average {mobile} out of 100 on a "
                   f"phone and {desktop} out of 100 on a desktop.")
    worst = section.get("worst_template")
    if isinstance(worst, dict):
        worst = worst.get("template")
    if worst:
        out.append(f"The slowest template on a phone is {worst}.")
    return out


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    """"1 addresses redirect" is how a report loses a reader's trust."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _crawlability_facts(section: Any) -> List[str]:
    """Crawlability counts obstacles, not page faults, so it says its own."""
    if not isinstance(section, dict):
        return []
    sitemap = section.get("sitemap") or {}
    redirects = section.get("redirects") or {}
    robots = section.get("robots") or {}
    out: List[str] = []

    total = sitemap.get("urls_total") or 0
    if total:
        out.append(f"The sitemap lists {total} addresses.")
    count, whole = _count_and_whole(sitemap.get("in_sitemap_not_crawled"), 0)
    if count:
        out.append(f"{count} of {whole} addresses in the sitemap "
                   f"{'was' if count == 1 else 'were'} not reached from the "
                   f"site's own links.")
    count, whole = _count_and_whole(sitemap.get("crawled_not_in_sitemap"), 0)
    if count:
        out.append(f"{count} of {whole} addresses found by following links "
                   f"{'is' if count == 1 else 'are'} missing from the "
                   f"sitemap.")
    if sitemap.get("non_200"):
        out.append(f"{_plural(sitemap['non_200'], 'address', 'addresses')} "
                   f"listed in the sitemap does not load."
                   if sitemap["non_200"] == 1 else
                   f"{sitemap['non_200']} addresses listed in the sitemap do "
                   f"not load.")

    count, whole = _count_and_whole(redirects.get("total"), 0)
    slash = _count_and_whole(redirects.get("trailing_slash"), 0)[0]
    if count:
        sentence = (f"{count} of {whole} addresses "
                    f"{'answers' if count == 1 else 'answer'} with a "
                    f"redirect")
        if slash:
            sentence += (f", and {slash} of those redirect only to add a "
                         f"slash at the end")
        out.append(sentence + ".")
    if redirects.get("chains"):
        out.append(f"{_plural(redirects['chains'], 'address', 'addresses')} "
                   f"redirect more than once before arriving.")
    if redirects.get("loops"):
        out.append(f"{_plural(redirects['loops'], 'address', 'addresses')} "
                   f"redirect in a loop and never arrive.")

    for key, sentence in (
            ("blocked_linked", "{n} linked from the site cannot be fetched, "
                               "because the robots file forbids it."),
            ("blocked_in_sitemap", "{n} listed in the sitemap cannot be "
                                   "fetched, because the robots file forbids "
                                   "it.")):
        if robots.get(key):
            out.append(sentence.format(
                n=_plural(robots[key], "address", "addresses")))
    if not robots.get("found", True):
        out.append("The site serves no robots file.")

    count, whole = _count_and_whole(section.get("deep_pages"), 0)
    if count:
        out.append(f"{count} of {whole} pages sit more than three clicks from "
                   f"the home page.")
    count, whole = _count_and_whole(section.get("render_suspects"), 0)
    if count:
        out.append(f"{count} of {whole} pages arrive with almost no text in "
                   f"them until scripts run.")
    if section.get("slow_responses"):
        out.append(f"{_plural(section['slow_responses'], 'address', 'addresses')} "
                   f"were slow to answer.")
    if section.get("fetch_errors"):
        out.append(f"{_plural(section['fetch_errors'], 'address', 'addresses')} "
                   f"returned nothing at all.")
    if section.get("documents_linked"):
        out.append(f"{_plural(section['documents_linked'], 'link')} points "
                   f"at a document such as a PDF rather than at a page."
                   if section["documents_linked"] == 1 else
                   f"{section['documents_linked']} links point at documents "
                   f"such as PDFs rather than at pages.")
    return out


def _is_fix_list(section: Any) -> bool:
    return (isinstance(section, list) and section
            and all(isinstance(f, dict) and f.get("plain_finding")
                    for f in section))


def _fix_list_facts(fixes: List[Dict]) -> str:
    """The priority fixes carry their own sentence, already unit correct."""
    sentences = []
    for fix in fixes[:MAX_FACT_SENTENCES]:
        sentence = str(fix["plain_finding"]).strip().rstrip(".")
        sentences.append(sentence[0].upper() + sentence[1:] + ".")
    return " ".join(sentences)


def _band(name: str) -> str:
    """"60-79" is a score range, and the report carries no dashes."""
    return str(name).replace("-", " to ")


def summary_facts(headline: Dict, fixes: List[Dict]) -> str:
    """The executive summary's findings: the score, the spread, the top five."""
    headline = headline or {}
    fixes = fixes or []
    scored = headline.get("pages_scored", 0)
    sentences = []

    score = headline.get("site_score")
    if score is not None:
        sentences.append(f"The site scores {score} out of 100, across "
                         f"{scored} pages scored.")
    deduction = headline.get("site_level_deduction") or 0
    if deduction:
        sentences.append(f"That includes a deduction of {deduction} points "
                         f"for settings that apply to the whole site.")

    bands = [(band, obj.get("count", 0))
             for band, obj in (headline.get("distribution") or {}).items()
             if obj.get("count", 0) > 0]
    if bands:
        bands.sort(key=lambda b: -b[1])
        spread = ", ".join(
            f"{count} {'scores' if count == 1 else 'score'} {_band(band)}"
            for band, count in bands)
        sentences.append(f"Of those pages, {spread}.")

    labels = [f.get("label") or f.get("title") for f in fixes[:5]]
    labels = [str(l) for l in labels if l]
    if labels:
        joined = (", ".join(labels[:-1]) + " and " + labels[-1]
                  if len(labels) > 1 else labels[0])
        sentences.append(f"The five biggest fixes, in order, are {joined}.")
    return " ".join(sentences)


# --- the client -------------------------------------------------------------

@dataclass
class Usage:
    """What the run cost and where the model was overruled."""

    model: str = DEFAULT_MODEL
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    guard_events: List[Dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    reasoning_effort_accepted: bool = True

    def as_dict(self) -> Dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "max_calls": MAX_CALLS,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "guard_events": self.guard_events,
            "errors": self.errors,
            "regenerations": sum(1 for e in self.guard_events
                                 if e.get("action") == "regenerated once"),
            "reasoning_effort_accepted": self.reasoning_effort_accepted,
        }


class NarrativeError(RuntimeError):
    """A refusal that must stop the run rather than be retried."""


def api_key() -> Optional[str]:
    key = os.environ.get(KEY_ENV_VAR, "").strip()
    return key or None


class Narrator:
    """Writes each section, or falls back to templates when asked to."""

    def __init__(self, model: str = DEFAULT_MODEL, use_ai: bool = True,
                 key: Optional[str] = None, session=None,
                 retry_after: float = RETRY_AFTER_SECONDS,
                 reasoning_effort: Optional[str] = "low",
                 default_whole: int = 0):
        self.model = model
        # Pages read in this run: the whole for a count whose own section
        # does not carry one.
        self.default_whole = default_whole
        self.reasoning_effort = reasoning_effort
        self.reasoning_effort_accepted = bool(reasoning_effort)
        self.use_ai = use_ai
        self.key = key if key is not None else api_key()
        self.session = session
        self.retry_after = retry_after
        self.usage = Usage(model=model if use_ai else "none (--no-ai)")
        self.usage.reasoning_effort_accepted = bool(reasoning_effort)

    # -- the one request ----------------------------------------------------

    def _post(self, payload: Dict):
        import requests

        session = self.session
        if session is None:
            session = requests.Session()
        headers = {"Authorization": f"Bearer {self.key}",
                   "Content-Type": "application/json"}
        try:
            response = session.post(API_URL, headers=headers, json=payload,
                                    timeout=CALL_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            return None, f"{type(exc).__name__}: {exc}"
        return response, None

    def _call(self, prompt: str) -> Tuple[str, str]:
        """One completion, one retry on a rate limit or server error only.

        Returns (text, finish_reason) so the caller can tell a finished
        answer from one the model ran out of room for.
        """
        if self.usage.calls >= MAX_CALLS:
            raise NarrativeError(
                f"call ceiling of {MAX_CALLS} reached; refusing to spend more")

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": STYLE},
                         {"role": "user", "content": prompt}],
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

        for attempt in (0, 1):
            self.usage.calls += 1
            response, error = self._post(payload)
            if error is not None:
                if attempt == 0:
                    time.sleep(self.retry_after)
                    continue
                raise NarrativeError(error)

            status = response.status_code
            if status == 200:
                body = response.json()
                usage = body.get("usage") or {}
                self.usage.input_tokens += usage.get("prompt_tokens", 0)
                self.usage.output_tokens += usage.get("completion_tokens", 0)
                choice = (body.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content", "")
                return content, choice.get("finish_reason") or "stop"

            text = response.text[:400]
            if status in BILLING_STATUSES or "insufficient_quota" in text:
                # Billing or auth: stop, do not retry, do not switch models.
                raise NarrativeError(
                    f"HTTP {status} from the model API, stopping without "
                    f"retry: {text}")
            if status == 400 and self.reasoning_effort:
                # The model does not take reasoning_effort. Drop it once and
                # say so, rather than failing the whole report over a knob.
                self.reasoning_effort_accepted = False
                self.usage.reasoning_effort_accepted = False
                self.reasoning_effort = None
                payload.pop("reasoning_effort", None)
                continue
            if status in RETRY_STATUSES and attempt == 0:
                time.sleep(self.retry_after)
                continue
            raise NarrativeError(f"HTTP {status}: {text}")
        raise NarrativeError("exhausted retries")

    # -- one section --------------------------------------------------------

    def write(self, section_name: str, section: Any,
              limit: int = MAX_WORDS_PER_SECTION,
              facts: Optional[str] = None) -> Dict:
        """One section as {found, why, todo}. Never raises for bad prose.

        "found" is written here from the findings, whatever the model says.
        """
        if facts is None:
            facts = section_facts(section_name, section, self.default_whole)
        fallback = templated_section(section_name, section, facts)
        if not self.use_ai:
            return _clean_section(fallback, limit)

        curated = curate(section)
        prompt = (
            f"Section: {section_name.replace('_', ' ')}.\n\n"
            f"What we found, already written and already in the document:\n"
            f"{facts}\n\n"
            f"The data behind it, for context only:\n"
            + json.dumps(curated, indent=2, ensure_ascii=False)[:12000]
            + "\n\nReturn only why it matters and what to do.")

        try:
            parsed, finish = self._attempt(prompt, section_name)
        except NarrativeError as exc:
            if "ceiling" not in str(exc):
                raise  # billing, auth or a real API failure: stop the run
            # Out of budget is not a reason to lose the report. The rest of
            # the sections are written from the data instead.
            self.usage.guard_events.append({
                "section": section_name,
                "reason": "call ceiling reached",
                "action": "used the templated section",
            })
            return _clean_section(fallback, limit)

        # Truncation guard: a cut off clause is worse than a dull one, so one
        # regeneration is worth the call.
        if parsed is None or _is_truncated(parsed, finish):
            self.usage.guard_events.append({
                "section": section_name,
                "reason": ("model returned unparseable JSON" if parsed is None
                           else "model answer was cut off"),
                "action": "regenerated once",
            })
            try:
                parsed, finish = self._attempt(prompt, section_name)
            except NarrativeError as exc:
                if "ceiling" not in str(exc):
                    raise
                return _clean_section(fallback, limit)
            if parsed is None or _is_truncated(parsed, finish):
                self.usage.guard_events.append({
                    "section": section_name,
                    "reason": "still incomplete after one regeneration",
                    "action": "used the templated section",
                })
                return _clean_section(fallback, limit)

        # Jargon guard: a word the glossary does not carry is worth one more
        # call, and no more than one.
        hits = find_jargon(parsed.get("why"), parsed.get("todo"))
        if hits:
            self.usage.guard_events.append({
                "section": section_name,
                "reason": f"jargon in the model's text: {', '.join(hits)}",
                "action": "regenerated once",
            })
            try:
                retry, finish = self._attempt(prompt, section_name)
            except NarrativeError as exc:
                if "ceiling" not in str(exc):
                    raise
                retry = None
            if retry is None or _is_truncated(retry, finish):
                retry = None
            still = find_jargon(retry.get("why"),
                               retry.get("todo")) if retry else hits
            if still:
                self.usage.guard_events.append({
                    "section": section_name,
                    "reason": f"jargon again: {', '.join(still)}",
                    "action": "used the templated why and what to do",
                })
                parsed = {"why": fallback["why"], "todo": fallback["todo"]}
            else:
                parsed = retry

        return self._guard(section_name, parsed, section, fallback, limit,
                           facts)

    def _attempt(self, prompt: str, section_name: str):
        """One call, parsed into the three parts. None when it will not parse."""
        try:
            text, finish = self._call(prompt)
        except NarrativeError:
            raise
        return parse_sections(text), finish

    def _guard(self, section_name: str, parsed: Dict, section: Any,
               fallback: Dict, limit: int, facts: str) -> Dict:
        """Facts from us, dashes out, invented numbers out, our work out.

        Whatever the model returned under "found" is dropped on the floor:
        the findings paragraph is written by code.
        """
        out = {"found": facts,
               "why": strip_dashes(parsed.get("why", "")),
               "todo": [strip_dashes(t) for t in parsed.get("todo", [])
                        if str(t).strip()][:MAX_TODO_ITEMS]}

        dropped_all: List[str] = []
        kept, dropped = check_numbers(out["why"], section, facts)
        out["why"] = kept
        dropped_all.extend(dropped)

        kept_todo = []
        for item in out["todo"]:
            _kept, dropped = check_numbers(item, section, facts)
            if dropped:
                dropped_all.extend(dropped)
            else:
                kept_todo.append(item)
        out["todo"] = kept_todo

        if dropped_all:
            # The finding itself is safe either way: it is in the paragraph
            # above, written from the data.
            self.usage.guard_events.append({
                "section": section_name,
                "sentences_dropped": len(dropped_all),
                "examples": dropped_all[:2],
                "reason": "number not present in the section's data",
                "action": "dropped the sentence, the findings paragraph "
                          "already states the figures",
            })

        # Advice about the audit is not advice for the client.
        out["todo"], about_us = filter_todo(out["todo"])
        if about_us:
            self.usage.guard_events.append({
                "section": section_name,
                "items_dropped": len(about_us),
                "examples": about_us[:2],
                "reason": "action was about the audit, not about the site",
                "action": ("used the templated action" if not out["todo"]
                           else "dropped the action"),
            })

        for key in ("found", "why"):
            if not out[key].strip():
                out[key] = fallback[key]
        if not out["todo"]:
            out["todo"] = fallback["todo"]
        return _clean_section(out, limit)

    def summary(self, headline: Dict, fixes: List[Dict]) -> Dict:
        """The executive summary, from the headline and the top five fixes."""
        section = {
            "headline": headline,
            "top_fixes": [
                {"issue": f.get("label") or f.get("title"),
                 "pages_affected": f.get("pages_affected"),
                 "severity": f.get("severity"),
                 "where_to_fix": f.get("fix_scope")}
                for f in (fixes or [])[:5]
            ],
        }
        return self.write("executive_summary", section,
                          limit=MAX_WORDS_SUMMARY,
                          facts=summary_facts(headline, fixes))
