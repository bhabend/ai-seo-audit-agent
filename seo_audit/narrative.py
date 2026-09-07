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
    "- British spelling throughout: optimise, prioritise, organise, "
    "recognise, analyse. Never the American z spelling.\n"
    "- Never use these empty words: leverage, robust, ensure, impactful, "
    "best practices, seamless, holistic, utilise, actionable, streamline. "
    "Say the thing itself instead.\n"
    "- Call the setting that keeps the site on a secure connection 'the "
    "secure connection setting'.\n"
    "- Never use an internal identifier. Words joined by underscores are "
    "never allowed, and neither are page shapes in braces such as "
    "{slug}: name a template in words, for example 'workspaces pages'.\n"
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
#
# Matched as phrases, not as words. The first version of this rule dropped
# any item containing "audit", which took "Audit and reduce third party
# scripts on high traffic templates" out of a client's report: a perfectly
# good instruction, thrown away for using the word as a verb. Only our own
# measuring is dropped now.
_ABOUT_THE_AUDIT = re.compile(
    r"\bunmeasured\b"
    r"|\bmeasure (more|the remaining|the rest|further|additional)\b"
    r"|\bexpand (the )?(measurement|coverage|sampling|crawl)\b"
    r"|\bmeasurement (coverage|to)\b"
    r"|\bsample more\b|\bsampled templates\b|\bwiden the sample\b"
    r"|\bre-?run the audit\b|\brun the audit again\b|\bre-?audit\b"
    r"|\bcrawl the site again\b|\bre-?crawl\b"
    r"|\bthe audit did not\b|\bthis audit\b|\bthe next audit\b"
    r"|\bin a future audit\b|\bfuture audits\b",
    re.I)


def is_about_the_audit(item: str) -> bool:
    return bool(_ABOUT_THE_AUDIT.search(str(item or "")))


def filter_todo(items: Iterable[str]) -> Tuple[List[str], List[str]]:
    """(kept, dropped): actions on the site, and actions on the audit."""
    kept, dropped = [], []
    for item in items:
        (dropped if is_about_the_audit(item) else kept).append(item)
    return kept, dropped


# --- guard: British spelling and empty words --------------------------------

# The report is written for a client in India, in British English. American
# spellings arrive in almost every paragraph, so they are corrected rather
# than argued with: a spelling is not worth a second call.
_IZE_WORDS = ("optimi", "prioriti", "organi", "recogni", "reali", "minimi",
              "maximi", "utili", "summari", "categori", "standardi",
              "personali", "emphasi", "analy")
_IZE = re.compile(r"\b(" + "|".join(_IZE_WORDS) + r")(z)(e|es|ed|ing|ation)\b",
                  re.I)
# Words that fill a sentence without saying anything. Unlike a spelling,
# these mean the sentence has to be written again.
VAGUE_WORDS = ("leverage", "leveraging", "robust", "ensure", "ensuring",
               "impactful", "best practices", "best practice", "seamless",
               "holistic", "synergy", "utilise", "utilize", "actionable",
               "world class", "cutting edge", "streamline", "streamlined")
_VAGUE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(VAGUE_WORDS, key=len,
                                                   reverse=True)) + r")\b",
    re.I)


def to_british(text: str) -> str:
    """American -ize spellings to -ise, keeping the writer's capitals."""
    def swap(match):
        return match.group(1) + ("S" if match.group(2).isupper() else "s") \
            + match.group(3)
    return _IZE.sub(swap, str(text or ""))


def _to_british_section(parsed: Optional[Dict]) -> Optional[Dict]:
    """The model's two parts, respelled, never sent back for a spelling."""
    if not parsed:
        return parsed
    return {**parsed,
            "why": to_british(parsed.get("why", "")),
            "todo": [to_british(t) for t in (parsed.get("todo") or [])]}


def _word_problems(parsed: Optional[Dict]) -> List[str]:
    """Jargon plus empty words: one list, so one regeneration covers both."""
    if not parsed:
        return []
    return (find_jargon(parsed.get("why"), parsed.get("todo"))
            + find_vague(parsed.get("why"), parsed.get("todo")))


def find_vague(*texts: Any) -> List[str]:
    """Empty words in the model's text, in the order they appear."""
    hits: List[str] = []
    for text in texts:
        items = text if isinstance(text, (list, tuple)) else [text]
        for item in items:
            for match in _VAGUE.finditer(str(item or "")):
                word = match.group(1).lower()
                if word not in hits:
                    hits.append(word)
    return hits


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


# A URL shape as the sampler writes it: "/workspaces/{slug} (depth 4)".
_SHAPE = re.compile(r"^/\S*(\{[a-z]+\})?\S*(\s*\((depth \d+|homepage)\))?$")


def _is_shape(value: str) -> bool:
    """True for a template shape, false for a URL or ordinary text."""
    text = str(value).strip()
    if not text.startswith("/"):
        return False
    return bool("{" in text or "(depth " in text or "(homepage)" in text
                or _SHAPE.match(text))


def curate(node: Any) -> Any:
    """The section as the model should see it.

    Internal machinery is removed, identifiers are replaced with their plain
    labels and template shapes with their plain words, so the model cannot
    repeat back simhash, title_missing or /workspaces/{slug} even if it
    wanted to.
    """
    from .findings import template_words
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
    if isinstance(node, str) and _is_shape(node):
        return template_words(node)
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
    """Dashes out and length capped, on every part.

    The findings paragraph is never word capped: it is ours, every sentence
    in it is a finding, and a finding may not be lost to a word count. What
    runs long moves into the "Also found" table instead.
    """
    return {
        "found": strip_dashes(section.get("found", "")),
        "why": cap_words(strip_dashes(section.get("why", "")), limit),
        "todo": [strip_dashes(t) for t in section.get("todo", [])
                 if str(t).strip()][:MAX_TODO_ITEMS],
        "also_found": [[strip_dashes(str(cell)) for cell in row]
                       for row in section.get("also_found") or []],
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
                      default_whole: int = 0,
                      also_found: Optional[List[List[str]]] = None) -> Dict:
    """The three parts, built from the data, for --no-ai and for fallback."""
    if facts is None:
        facts, also_found, _everything = section_facts_parts(
            section_name, section, default_whole)
    return {
        "found": facts,
        "also_found": also_found or [],
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
# Six sentences is a paragraph. Ten is a list that has not admitted it yet,
# so the rest of the findings become the "Also found" table.
MAX_FACT_SENTENCES = 6


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


def fact_entries(section_name: str, section: Any, default_whole: int = 0
                 ) -> List[Tuple[str, int, int, str]]:
    """(issue type, count, whole, what the whole is) for what was found.

    Zero counts are left out entirely: a section that opens on what was not
    found buries what was.
    """
    from .issues import PAGE_ISSUE_META, unit_of

    entries: List[Tuple[str, int, int, str]] = []
    seen: Set[str] = set()

    def add(issue_type: str, value: Any):
        count, whole = _count_and_whole(value, default_whole)
        if count <= 0:
            return
        whole_is = (value.get("whole_is") or "pages parsed"
                    if isinstance(value, dict) else "pages parsed")
        if unit_of(issue_type) != "page" and not isinstance(value, dict):
            # A count of addresses or of groups is not a count of pages, so
            # it is never handed the page total as its whole.
            whole, whole_is = 0, ""
        entries.append((issue_type, count, whole, whole_is))
        seen.add(issue_type)

    for issue_type, path in FACT_PATHS.get(section_name, ()):
        add(issue_type, _value_at(section, path))

    # Site level settings are recorded as a list of types, not as counts.
    for issue_type in (_value_at(section, ("site_level", "types")) or []):
        if issue_type in PAGE_ISSUE_META and issue_type not in seen:
            entries.append((issue_type, 1, 0, ""))
            seen.add(issue_type)

    if not entries:
        # Sections this table does not cover, and the shapes the fixes list
        # and the tests use: read whatever issue types are named inside.
        for issue_type, (count, whole, _ev) in _collect_issue_counts(
                section).items():
            if count > 0 and issue_type not in seen:
                page_unit = unit_of(issue_type) == "page"
                entries.append((issue_type, count,
                                whole or (default_whole if page_unit else 0),
                                "pages parsed" if page_unit else ""))
                seen.add(issue_type)
    return entries


def _positive_sentences(section_name: str, section: Any,
                        entries: List[Tuple[str, int, int, str]]) -> List[str]:
    """What the site already does right, said last and said once."""
    from .issues import plain_finding

    out = []
    for issue_type, count, whole, _whole_is in entries:
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


def _count_text(issue_type: str, count: int, whole: int,
                whole_is: str) -> str:
    """What the count counts, for the "Also found" table's second column."""
    from .issues import unit_of

    if whole:
        return f"{count} of {whole} {whole_is}".strip()
    unit = unit_of(issue_type)
    if unit == "site":
        return "site wide"
    if unit == "group":
        return _plural(count, "group") + " of pages"
    if unit == "target":
        return _plural(count, "link" if "external" in issue_type
                       else "address", None if "external" in issue_type
                       else "addresses")
    return _plural(count, "page")


def section_facts(section_name: str, section: Any,
                  default_whole: int = 0) -> str:
    """Everything this section found, said in full. The model sees this."""
    return section_facts_parts(section_name, section, default_whole)[2]


def section_facts_parts(section_name: str, section: Any,
                        default_whole: int = 0
                        ) -> Tuple[str, List[List[str]], str]:
    """(paragraph, "Also found" rows, everything).

    Ten findings in a row stop being a paragraph and become a list wearing a
    paragraph's clothes. The six that matter most are said in sentences and
    the rest go into a small table, where a reader can scan them. Nothing is
    dropped: every finding is in exactly one of the two, and the model is
    handed all of it either way.
    """
    from .issues import PAGE_ISSUE_SEVERITY, label_of, plain_finding

    if _is_fix_list(section):
        # The fixes table sits directly above this paragraph and lists every
        # one of them with its count, so the overflow needs no table of its
        # own. The model is still handed all of them.
        paragraph, everything = _fix_list_facts(section)
        return paragraph, [], everything

    entries = fact_entries(section_name, section, default_whole)
    negatives = [e for e in entries
                 if PAGE_ISSUE_SEVERITY.get(e[0]) != "info"]
    positives = [e for e in entries if PAGE_ISSUE_SEVERITY.get(e[0]) == "info"]
    negatives.sort(key=lambda e: (SEVERITY_ORDER.get(
        PAGE_ISSUE_SEVERITY.get(e[0], "low"), 2), -e[1]))

    # Sentences this section writes for itself, which have no issue type and
    # so are always in the paragraph.
    lead = _performance_facts(section_name, section)
    if section_name == "crawlability":
        lead = lead + _crawlability_facts(section)

    findings = lead + [(plain_finding(t, c, w).capitalize().rstrip(".") + ".",
                        label_of(t), _count_text(t, c, w, whole_is))
                       for t, c, w, whole_is in negatives]

    sentences = [sentence for sentence, _l, _c
                 in findings[:MAX_FACT_SENTENCES]]
    rows = [[label.capitalize(), count] for _s, label, count
            in findings[MAX_FACT_SENTENCES:]]

    good = _positive_sentences(section_name, section, positives)
    if good:
        # Good news is a summary, not a finding, so it closes the paragraph
        # rather than competing for a place in it.
        sentences.append("The site also gets some things right: "
                         + "; ".join(good) + ".")

    if not sentences and not rows:
        blank = templated(section_name, section, skip_zero=True)
        return blank, [], blank

    paragraph = " ".join(sentences)
    everything = " ".join(
        sentences + [s for s, _l, _c in findings[MAX_FACT_SENTENCES:]])
    return paragraph, rows, everything


def _performance_facts(section_name: str,
                       section: Any) -> List[Tuple[str, str, str]]:
    """Speed has no page counts of its own: it is measured per template."""
    from .findings import template_words

    if section_name != "performance" or not isinstance(section, dict):
        return []
    if section.get("skipped"):
        return [("Speed was not measured for this audit.", "Speed",
                 "not measured")]
    represented = section.get("pages_represented") or {}
    measured = section.get("templates_measured") or []
    out: List[Tuple[str, str, str]] = []
    if measured:
        out.append((f"Speed was measured on {len(measured)} page templates, "
                    f"covering {represented.get('count', 0)} of "
                    f"{represented.get('whole', 0)} pages read.",
                    "Templates measured for speed",
                    f"{len(measured)} of "
                    f"{len(measured) + (section.get('templates_unmeasured_count') or 0)}"
                    f" templates"))
    mobile = section.get("mean_mobile_score")
    desktop = section.get("mean_desktop_score")
    if mobile is not None:
        out.append((f"The measured templates average {mobile} out of 100 on "
                    f"a phone and {desktop} out of 100 on a desktop.",
                    "Average speed score",
                    f"{mobile} of 100 on a phone"))
    worst = section.get("worst_template")
    if isinstance(worst, dict):
        # The shape is for the developers in the appendix; a sentence gets
        # the words. "/workspaces/{slug} (depth 4)" is not English.
        worst = worst.get("template_words") or template_words(
            worst.get("template", ""))
    elif isinstance(worst, str):
        worst = template_words(worst)
    if worst:
        out.append((f"The slowest template on a phone is {worst}.",
                    "Slowest template on a phone", str(worst)))
    return out


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    """"1 addresses redirect" is how a report loses a reader's trust."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _crawlability_facts(section: Any) -> List[Tuple[str, str, str]]:
    """Crawlability counts obstacles, not page faults, so it says its own.

    Each fact is a sentence for the paragraph and a name and a count for the
    table, because a section with nine obstacles is a list, not a paragraph.
    """
    if not isinstance(section, dict):
        return []
    sitemap = section.get("sitemap") or {}
    redirects = section.get("redirects") or {}
    robots = section.get("robots") or {}
    out: List[Tuple[str, str, str]] = []

    total = sitemap.get("urls_total") or 0
    if total:
        out.append((f"The sitemap lists {total} addresses.",
                    "Sitemap size",
                    _plural(total, "address", "addresses")))
    count, whole = _count_and_whole(sitemap.get("in_sitemap_not_crawled"), 0)
    if count:
        out.append((f"{count} of {whole} addresses in the sitemap "
                    f"{'was' if count == 1 else 'were'} not reached from the "
                    f"site's own links.",
                    "Sitemap addresses the crawl never reached",
                    f"{count} of {whole} sitemap addresses"))
    count, whole = _count_and_whole(sitemap.get("crawled_not_in_sitemap"), 0)
    if count:
        out.append((f"{count} of {whole} addresses found by following links "
                    f"{'is' if count == 1 else 'are'} missing from the "
                    f"sitemap.",
                    "Addresses missing from the sitemap",
                    f"{count} of {whole} addresses found"))
    if sitemap.get("non_200"):
        many = sitemap["non_200"]
        out.append((f"{_plural(many, 'address', 'addresses')} listed in the "
                    f"sitemap does not load." if many == 1 else
                    f"{many} addresses listed in the sitemap do not load.",
                    "Sitemap entries that do not load",
                    _plural(many, "address", "addresses")))

    count, whole = _count_and_whole(redirects.get("total"), 0)
    slash = _count_and_whole(redirects.get("trailing_slash"), 0)[0]
    if count:
        sentence = (f"{count} of {whole} addresses "
                    f"{'answers' if count == 1 else 'answer'} with a "
                    f"redirect")
        if slash:
            sentence += (f", and {slash} of those redirect only to add a "
                         f"slash at the end")
        out.append((sentence + ".", "Addresses that redirect",
                    f"{count} of {whole} addresses"))
    if redirects.get("chains"):
        counted = _plural(redirects["chains"], "address", "addresses")
        out.append((f"{counted} redirect more than once before arriving.",
                    "Redirects that pass through another redirect", counted))
    if redirects.get("loops"):
        counted = _plural(redirects["loops"], "address", "addresses")
        out.append((f"{counted} redirect in a loop and never arrive.",
                    "Redirect loops", counted))

    for key, sentence, name in (
            ("blocked_linked", "{n} linked from the site cannot be fetched, "
                               "because the robots file forbids it.",
             "Linked addresses the robots file blocks"),
            ("blocked_in_sitemap", "{n} listed in the sitemap cannot be "
                                   "fetched, because the robots file forbids "
                                   "it.",
             "Sitemap addresses the robots file blocks")):
        if robots.get(key):
            counted = _plural(robots[key], "address", "addresses")
            out.append((sentence.format(n=counted), name, counted))
    if not robots.get("found", True):
        out.append(("The site serves no robots file.", "Robots file",
                    "not found"))

    count, whole = _count_and_whole(section.get("deep_pages"), 0)
    if count:
        out.append((f"{count} of {whole} pages sit more than three clicks "
                    f"from the home page.",
                    "Pages more than three clicks from the home page",
                    f"{count} of {whole} pages parsed"))
    count, whole = _count_and_whole(section.get("render_suspects"), 0)
    if count:
        out.append((f"{count} of {whole} pages arrive with almost no text "
                    f"in them until scripts run.",
                    "Pages with almost no text until scripts run",
                    f"{count} of {whole} pages parsed"))
    if section.get("slow_responses"):
        counted = _plural(section["slow_responses"], "address", "addresses")
        out.append((f"{counted} were slow to answer.",
                    "Addresses slow to answer", counted))
    if section.get("fetch_errors"):
        counted = _plural(section["fetch_errors"], "address", "addresses")
        out.append((f"{counted} returned nothing at all.",
                    "Addresses that returned nothing", counted))
    if section.get("documents_linked"):
        many = section["documents_linked"]
        out.append((f"{_plural(many, 'link')} points at a document such as a "
                    f"PDF rather than at a page." if many == 1 else
                    f"{many} links point at documents such as PDFs rather "
                    f"than at pages.",
                    "Links to documents rather than pages",
                    _plural(many, "link")))
    return out


def _is_fix_list(section: Any) -> bool:
    return (isinstance(section, list) and section
            and all(isinstance(f, dict) and f.get("plain_finding")
                    for f in section))


def _fix_list_facts(fixes: List[Dict]) -> Tuple[str, str]:
    """The priority fixes carry their own sentence, already unit correct."""
    sentences = []
    for fix in fixes:
        sentence = str(fix["plain_finding"]).strip().rstrip(".")
        sentences.append(sentence[0].upper() + sentence[1:] + ".")
    return (" ".join(sentences[:MAX_FACT_SENTENCES]), " ".join(sentences))


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
            facts, rows, everything = section_facts_parts(
                section_name, section, self.default_whole)
        else:
            rows, everything = [], facts
        fallback = templated_section(section_name, section, facts,
                                     also_found=rows)
        if not self.use_ai:
            return _clean_section(fallback, limit)

        curated = curate(section)
        prompt = (
            f"Section: {section_name.replace('_', ' ')}.\n\n"
            f"What we found, already written and already in the document:\n"
            f"{everything}\n\n"
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

        # Word guard: jargon the glossary does not carry, and empty words.
        # Spelling is corrected in place first, because no spelling is worth
        # a second call. What is left is worth exactly one.
        parsed = _to_british_section(parsed)
        hits = _word_problems(parsed)
        if hits:
            self.usage.guard_events.append({
                "section": section_name,
                "reason": f"words the report does not use: {', '.join(hits)}",
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
            else:
                retry = _to_british_section(retry)
            still = _word_problems(retry) if retry else hits
            if still:
                self.usage.guard_events.append({
                    "section": section_name,
                    "reason": f"the same words again: {', '.join(still)}",
                    "action": "used the templated why and what to do",
                })
                parsed = {"why": fallback["why"], "todo": fallback["todo"]}
            else:
                parsed = retry

        return self._guard(section_name, parsed, section, fallback, limit,
                           facts, rows, everything)

    def _attempt(self, prompt: str, section_name: str):
        """One call, parsed into the three parts. None when it will not parse."""
        try:
            text, finish = self._call(prompt)
        except NarrativeError:
            raise
        return parse_sections(text), finish

    def _guard(self, section_name: str, parsed: Dict, section: Any,
               fallback: Dict, limit: int, facts: str,
               rows: Optional[List[List[str]]] = None,
               everything: Optional[str] = None) -> Dict:
        """Facts from us, dashes out, invented numbers out, our work out.

        Whatever the model returned under "found" is dropped on the floor:
        the findings paragraph is written by code.
        """
        everything = everything or facts
        out = {"found": facts,
               "also_found": rows or [],
               "why": strip_dashes(parsed.get("why", "")),
               "todo": [strip_dashes(t) for t in parsed.get("todo", [])
                        if str(t).strip()][:MAX_TODO_ITEMS]}

        dropped_all: List[str] = []
        kept, dropped = check_numbers(out["why"], section, everything)
        out["why"] = kept
        dropped_all.extend(dropped)

        kept_todo = []
        for item in out["todo"]:
            _kept, dropped = check_numbers(item, section, everything)
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
