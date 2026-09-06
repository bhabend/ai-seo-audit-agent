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

# One call per narrative section plus the executive summary.
MAX_CALLS = 14
CALL_TIMEOUT = 90
RETRY_AFTER_SECONDS = 5
RETRY_STATUSES = (429, 500, 502, 503, 504)
# Refusals that must stop the run rather than be retried.
BILLING_STATUSES = (401, 402, 403)

MAX_WORDS_PER_SECTION = 320
MAX_WORDS_SUMMARY = 220

STYLE = (
    "You are writing one section of an SEO audit report for the head of "
    "marketing at the audited company. They are clever but not technical.\n"
    "Rules:\n"
    "- Structure: what we found, why it matters, what to do. No headings.\n"
    "- Define any SEO term the first time you use it, in a few plain words.\n"
    "- Never use a dash of any kind. No em dash, no en dash, no hyphen as "
    "punctuation. Use a comma or a full stop.\n"
    "- Never state a number that is not in the JSON you were given.\n"
    "- Do not restate every figure. Tables and charts carry the numbers. "
    "Explain what they mean.\n"
    "- At most five items in any list.\n"
    "- Plain sentences. No marketing language, no exclamation marks.\n"
)


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

    def as_dict(self) -> Dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "max_calls": MAX_CALLS,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "guard_events": self.guard_events,
            "errors": self.errors,
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
                 retry_after: float = RETRY_AFTER_SECONDS):
        self.model = model
        self.use_ai = use_ai
        self.key = key if key is not None else api_key()
        self.session = session
        self.retry_after = retry_after
        self.usage = Usage(model=model if use_ai else "none (--no-ai)")

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

    def _call(self, prompt: str) -> str:
        """One completion, one retry on a rate limit or server error only."""
        if self.usage.calls >= MAX_CALLS:
            raise NarrativeError(
                f"call ceiling of {MAX_CALLS} reached; refusing to spend more")

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": STYLE},
                         {"role": "user", "content": prompt}],
        }

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
                choices = body.get("choices") or [{}]
                return (choices[0].get("message") or {}).get("content", "")

            text = response.text[:400]
            if status in BILLING_STATUSES or "insufficient_quota" in text:
                # Billing or auth: stop, do not retry, do not switch models.
                raise NarrativeError(
                    f"HTTP {status} from the model API, stopping without "
                    f"retry: {text}")
            if status in RETRY_STATUSES and attempt == 0:
                time.sleep(self.retry_after)
                continue
            raise NarrativeError(f"HTTP {status}: {text}")
        raise NarrativeError("exhausted retries")

    # -- one section --------------------------------------------------------

    def write(self, section_name: str, section: Any,
              limit: int = MAX_WORDS_PER_SECTION) -> str:
        """Prose for one section, guarded. Never raises for bad prose."""
        fallback = templated(section_name, section)
        if not self.use_ai:
            return cap_words(strip_dashes(fallback), limit)

        prompt = (
            f"Write the '{section_name.replace('_', ' ')}' section of the "
            f"report from this JSON. Use only what is here.\n\n"
            + json.dumps(section, indent=2, ensure_ascii=False)[:12000])
        text = self._call(prompt)

        text = strip_dashes(text)
        text, dropped = check_numbers(text, section)
        if dropped:
            self.usage.guard_events.append({
                "section": section_name,
                "sentences_dropped": len(dropped),
                "examples": dropped[:2],
                "reason": "number not present in the section's data",
            })
            text = (text + " " + fallback).strip() if text else fallback
            text = strip_dashes(text)
        if not text.strip():
            text = strip_dashes(fallback)
        return cap_words(text, limit)

    def summary(self, headline: Dict, fixes: List[Dict]) -> str:
        """The executive summary, written from the headline and the fixes."""
        section = {"headline": headline, "prioritised_fixes": fixes[:10]}
        return self.write("executive_summary", section,
                          limit=MAX_WORDS_SUMMARY)
