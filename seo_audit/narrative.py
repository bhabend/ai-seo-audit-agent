"""Prose for the report. The model writes sentences; the numbers are ours.

The division of labour is strict and structural, not a matter of prompting
politely. The model is handed one section of findings.json and asked to
explain it. Whatever comes back is then put through three guards before it
reaches the document:

  1. Dashes are removed. The client asked for prose without them, and a model
     reaches for an em dash in almost every paragraph.
  2. Every number in the prose must already exist in the section it was
     written from. A sentence carrying a figure we cannot find is dropped and
     replaced with a templated sentence built from the section's own counts.
     A fabricated statistic in a client deliverable is worse than a dull one.
  3. Length is capped, because a section that runs long buries the finding.

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

STYLE = (
    "You are writing one section of an SEO audit report for the head of "
    "marketing at the audited company. They are clever but not technical.\n"
    "Return JSON only, with exactly these keys:\n"
    '  {"found": "one paragraph", "why": "one paragraph", '
    '"todo": ["sentence", "sentence"]}\n'
    "Rules:\n"
    "- found: what the audit found here. Under 120 words.\n"
    "- why: why it matters to this business. Under 120 words.\n"
    "- todo: up to five actions, each a complete sentence ending in a full "
    "stop.\n"
    "- Never define a term. A glossary elsewhere does that. Write as if the "
    "reader knows the word or can look it up.\n"
    "- Never describe how anything was measured, sampled or calculated.\n"
    "- Never use an internal identifier. Words joined by underscores are "
    "never allowed. Use the plain labels given to you.\n"
    "- Never use a dash of any kind. No em dash, no en dash, no hyphen as "
    "punctuation. Use a comma or a full stop.\n"
    "- Never state a number that is not in the JSON you were given.\n"
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


def _allowed_numbers(section: Any) -> Set[str]:
    allowed = _numbers_in(section)
    # Small integers are ordinary English ("three things", "one page").
    allowed.update(str(n) for n in range(0, 11))
    return allowed


def check_numbers(text: str, section: Any) -> Tuple[str, List[str]]:
    """Drop any sentence carrying a number the section does not contain."""
    allowed = _allowed_numbers(section)
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
    """True when the answer stopped in the middle of something."""
    if finish_reason == "length":
        return True
    for key in ("found", "why"):
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


def templated_section(section_name: str, section: Any) -> Dict:
    """The three parts, built from the data, for --no-ai and for fallback."""
    found = templated(section_name, section)
    restated, _types = restate_findings(section)
    if restated:
        found = (found + " " + restated).strip()
    return {
        "found": found,
        "why": ("These are the findings for this part of the audit. The "
                "priority fixes section lists what to do first."),
        "todo": ["Review the figures in this section against the priority "
                 "fixes table."],
    }


def templated(section_name: str, section: Any, limit: int = 5) -> str:
    """Sentences built from the section itself, used with --no-ai and as the
    replacement when a model sentence is dropped."""
    title = section_name.replace("_", " ")
    shares = _walk_shares(section)
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
                 reasoning_effort: Optional[str] = "low"):
        self.model = model
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
              limit: int = MAX_WORDS_PER_SECTION) -> Dict:
        """One section as {found, why, todo}. Never raises for bad prose."""
        fallback = templated_section(section_name, section)
        if not self.use_ai:
            return _clean_section(fallback, limit)

        curated = curate(section)
        prompt = (
            f"Write the '{section_name.replace('_', ' ')}' section of the "
            f"report from this data. Use only what is here.\n\n"
            + json.dumps(curated, indent=2, ensure_ascii=False)[:12000])

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

        return self._guard(section_name, parsed, section, fallback, limit)

    def _attempt(self, prompt: str, section_name: str):
        """One call, parsed into the three parts. None when it will not parse."""
        try:
            text, finish = self._call(prompt)
        except NarrativeError:
            raise
        return parse_sections(text), finish

    def _guard(self, section_name: str, parsed: Dict, section: Any,
               fallback: Dict, limit: int) -> Dict:
        """Dashes out, invented numbers out, findings kept."""
        out = {"found": strip_dashes(parsed.get("found", "")),
               "why": strip_dashes(parsed.get("why", "")),
               "todo": [strip_dashes(t) for t in parsed.get("todo", [])
                        if str(t).strip()][:MAX_TODO_ITEMS]}

        dropped_all: List[str] = []
        for key in ("found", "why"):
            kept, dropped = check_numbers(out[key], section)
            out[key] = kept
            dropped_all.extend(dropped)

        kept_todo = []
        for item in out["todo"]:
            _kept, dropped = check_numbers(item, section)
            if dropped:
                dropped_all.extend(dropped)
            else:
                kept_todo.append(item)
        out["todo"] = kept_todo

        if dropped_all:
            # The replacement states the findings, not just a count: a real
            # finding must never vanish because a sentence was dropped.
            restated, types = restate_findings(section)
            self.usage.guard_events.append({
                "section": section_name,
                "sentences_dropped": len(dropped_all),
                "examples": dropped_all[:2],
                "reason": "number not present in the section's data",
                "action": "restated the findings from the data",
                "types_restated": types,
            })
            if restated:
                out["found"] = (out["found"] + " " + restated).strip()
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
                          limit=MAX_WORDS_SUMMARY)
