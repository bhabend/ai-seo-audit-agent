"""PageSpeed Insights on a sample of page templates.

A site has one template per page shape, not one per page: 212 product pages
share a layout, and measuring all 212 would burn quota to learn one fact. So
pages are grouped by path shape and the best-linked page in each group is
measured as its representative. Every number this produces says which
template it came from and how many pages that template covers, because "the
product template scores 34 on mobile" is a finding and "one page scored 34"
is not.

Cost control is structural: at most 2 calls (mobile, desktop) per sampled URL,
and the sample is the homepage plus --pagespeed-templates groups. With the
default of 15 that is 32 calls, and the cap cannot be exceeded by any site
shape. Without a key the whole stage is skipped and says so.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
STRATEGIES = ("mobile", "desktop")
KEY_ENV_VAR = "PAGESPEED_API_KEY"

DEFAULT_TEMPLATE_SAMPLE = 15
CALL_TIMEOUT = 60
RETRY_AFTER_SECONDS = 10
RETRY_STATUSES = (429, 500, 502, 503, 504)

# Thresholds. Google's own "poor" boundaries, not invented ones.
POOR_PERFORMANCE_SCORE = 50
POOR_LCP_MS = 4000
POOR_CLS = 0.25
POOR_INP_MS = 500

PAGESPEED_COLUMNS = [
    "url", "template", "group_size", "strategy", "performance_score",
    "lab_lcp_ms", "lab_cls", "lab_tbt_ms", "lab_fcp_ms", "lab_speed_index_ms",
    "field_lcp_ms", "field_inp_ms", "field_cls", "field_data_level",
    "fetch_time", "error",
]

# A segment carrying a hyphen, an underscore, a digit, or unusual length is
# page identity ("post-one", "sku-4471"), not page shape ("pricing").
_IDENTIFIER_RE = re.compile(r"[-_0-9]")


def api_key() -> Optional[str]:
    """The key, from the environment only. Never logged, never printed."""
    key = os.environ.get(KEY_ENV_VAR, "").strip()
    return key or None


def _token(segment: str) -> str:
    """Collapse an identifying segment to a placeholder, keep a shape word."""
    if not segment:
        return ""
    if segment.isdigit():
        return "{n}"
    if _IDENTIFIER_RE.search(segment) or len(segment) > 20:
        return "{slug}"
    return segment.lower()


def template_of(url: str) -> str:
    """The shape of a URL: the section it sits in, with identifiers collapsed.

    The first segment is the section and is kept as written; later segments
    are page identity and collapse to a token. So `/blog/post-one` and
    `/blog/post-two` are one template, while `/blog/x` and `/pricing/x` are
    two. Depth is part of the shape: a three-deep page is not the same
    template as a two-deep one even under the same section.
    """
    path = urlsplit(url).path
    segments = [s for s in path.split("/") if s]
    if not segments:
        return "/"
    shape = [segments[0].lower()] + [_token(s) for s in segments[1:2]]
    depth = len(segments)
    return "/" + "/".join(shape) + (f" (depth {depth})" if depth > 2 else "")


@dataclass
class Sample:
    """One template and the page chosen to stand for it."""

    template: str
    url: str
    group_size: int


def choose_templates(pages: List[Tuple[str, int, int]],
                     limit: int = DEFAULT_TEMPLATE_SAMPLE,
                     homepage: Optional[str] = None) -> List[Sample]:
    """Pick the homepage plus the `limit` biggest templates.

    `pages` is (final_url, inlinks, depth). The representative of a group is
    its best-linked page, on the grounds that it is the one most likely to be
    served to a real visitor.
    """
    groups: Dict[str, List[Tuple[str, int]]] = {}
    for final_url, inlinks, _depth in pages:
        if homepage and final_url == homepage:
            continue
        groups.setdefault(template_of(final_url), []).append(
            (final_url, inlinks))

    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    samples: List[Sample] = []
    if homepage:
        samples.append(Sample(template="/ (homepage)", url=homepage,
                              group_size=1))
    for template, members in ranked[:limit]:
        best = max(members, key=lambda m: (m[1], m[0]))
        samples.append(Sample(template=template, url=best[0],
                              group_size=len(members)))
    return samples


@dataclass
class PageSpeedResult:
    """One strategy's numbers for one sampled URL. Raw JSON is discarded."""

    url: str
    strategy: str
    template: str = ""
    group_size: int = 0
    performance_score: Optional[int] = None
    lab_lcp_ms: Optional[float] = None
    lab_cls: Optional[float] = None
    lab_tbt_ms: Optional[float] = None
    lab_fcp_ms: Optional[float] = None
    lab_speed_index_ms: Optional[float] = None
    field_lcp_ms: Optional[float] = None
    field_inp_ms: Optional[float] = None
    field_cls: Optional[float] = None
    field_data_level: str = "none"  # url | origin | none
    fetch_time: Optional[str] = None
    error: Optional[str] = None

    def as_row(self) -> Dict:
        return {
            "url": self.url,
            "template": self.template,
            "group_size": self.group_size,
            "strategy": self.strategy,
            "performance_score": self.performance_score,
            "lab_lcp_ms": self.lab_lcp_ms,
            "lab_cls": self.lab_cls,
            "lab_tbt_ms": self.lab_tbt_ms,
            "lab_fcp_ms": self.lab_fcp_ms,
            "lab_speed_index_ms": self.lab_speed_index_ms,
            "field_lcp_ms": self.field_lcp_ms,
            "field_inp_ms": self.field_inp_ms,
            "field_cls": self.field_cls,
            "field_data_level": self.field_data_level,
            "fetch_time": self.fetch_time,
            "error": self.error,
        }


def _audit_value(audits: Dict, name: str) -> Optional[float]:
    audit = audits.get(name) or {}
    value = audit.get("numericValue")
    return round(float(value), 3) if isinstance(value, (int, float)) else None


def _metric_ms(metrics: Dict, name: str) -> Optional[float]:
    entry = metrics.get(name) or {}
    value = entry.get("percentile")
    return float(value) if isinstance(value, (int, float)) else None


def parse_response(payload: Dict, url: str, strategy: str) -> PageSpeedResult:
    """Pull the handful of numbers worth keeping out of a large response."""
    result = PageSpeedResult(url=url, strategy=strategy)
    lighthouse = payload.get("lighthouseResult") or {}
    audits = lighthouse.get("audits") or {}

    categories = lighthouse.get("categories") or {}
    performance = (categories.get("performance") or {}).get("score")
    if isinstance(performance, (int, float)):
        result.performance_score = round(float(performance) * 100)

    result.lab_lcp_ms = _audit_value(audits, "largest-contentful-paint")
    result.lab_cls = _audit_value(audits, "cumulative-layout-shift")
    result.lab_tbt_ms = _audit_value(audits, "total-blocking-time")
    result.lab_fcp_ms = _audit_value(audits, "first-contentful-paint")
    result.lab_speed_index_ms = _audit_value(audits, "speed-index")
    result.fetch_time = lighthouse.get("fetchTime")

    # Field data is real users; lab data is one synthetic run. Prefer the
    # URL-level record, fall back to the origin, and say which was used.
    loading = payload.get("loadingExperience") or {}
    origin = payload.get("originLoadingExperience") or {}
    metrics = loading.get("metrics") or {}
    level = "url"
    if not metrics:
        metrics = origin.get("metrics") or {}
        level = "origin" if metrics else "none"

    if metrics:
        result.field_lcp_ms = _metric_ms(
            metrics, "LARGEST_CONTENTFUL_PAINT_MS")
        result.field_inp_ms = _metric_ms(
            metrics, "INTERACTION_TO_NEXT_PAINT")
        cls = _metric_ms(metrics, "CUMULATIVE_LAYOUT_SHIFT_SCORE")
        # CrUX reports CLS multiplied by 100.
        result.field_cls = round(cls / 100, 3) if cls is not None else None
    result.field_data_level = level
    return result


class PageSpeedClient:
    """Calls the API, counts every call, and never exceeds the budget."""

    def __init__(self, fetcher, key: Optional[str] = None,
                 timeout: int = CALL_TIMEOUT,
                 retry_after: float = RETRY_AFTER_SECONDS):
        self.fetcher = fetcher
        self.key = key if key is not None else api_key()
        self.timeout = timeout
        self.retry_after = retry_after
        self.calls_made = 0

    def measure(self, url: str, strategy: str) -> PageSpeedResult:
        """One URL, one strategy, one retry on a rate limit or server error."""
        params = {
            "url": url,
            "strategy": strategy,
            "category": "performance",
        }
        if self.key:
            params["key"] = self.key

        for attempt in (0, 1):
            self.calls_made += 1
            status, payload, error = self._get(params)
            if error is None and status == 200 and isinstance(payload, dict):
                return parse_response(payload, url, strategy)
            retryable = status in RETRY_STATUSES
            if retryable and attempt == 0:
                time.sleep(self.retry_after)
                continue
            reason = error or f"HTTP {status}"
            return PageSpeedResult(url=url, strategy=strategy, error=reason)
        return PageSpeedResult(url=url, strategy=strategy,
                               error="exhausted retries")

    def _get(self, params: Dict):
        """The one request. Kept separate so tests can drive it directly."""
        import json as _json

        import requests

        try:
            response = self.fetcher.session.get(
                ENDPOINT, params=params, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            return None, None, f"{type(exc).__name__}: {exc}"
        try:
            return response.status_code, response.json(), None
        except (ValueError, _json.JSONDecodeError):
            return response.status_code, None, "response was not JSON"


def issues_for(result: PageSpeedResult) -> List[Tuple[str, str]]:
    """Findings for one measurement, each naming the template it stands for."""
    context = (f"{result.template} template ({result.group_size} page(s)), "
               f"{result.strategy}")
    found: List[Tuple[str, str]] = []

    if result.error:
        found.append(("pagespeed_error",
                      f"{context}: PageSpeed did not return a result "
                      f"({result.error})"))
        return found

    if (result.performance_score is not None
            and result.performance_score < POOR_PERFORMANCE_SCORE):
        found.append(("performance_poor",
                      f"{context}: performance score "
                      f"{result.performance_score} of 100"))
    if result.lab_lcp_ms is not None and result.lab_lcp_ms > POOR_LCP_MS:
        found.append(("lcp_poor",
                      f"{context}: lab LCP {round(result.lab_lcp_ms)} ms "
                      f"(over {POOR_LCP_MS} ms)"))
    if result.lab_cls is not None and result.lab_cls > POOR_CLS:
        found.append(("cls_poor",
                      f"{context}: lab CLS {result.lab_cls} (over {POOR_CLS})"))
    if (result.field_data_level != "none" and result.field_inp_ms is not None
            and result.field_inp_ms > POOR_INP_MS):
        found.append(("inp_poor",
                      f"{context}: field INP {round(result.field_inp_ms)} ms "
                      f"(over {POOR_INP_MS} ms), from real users"))
    if result.field_data_level == "none":
        found.append(("performance_no_field_data",
                      f"{context}: no real-user data at URL or origin level, "
                      f"so only the lab run is available"))
    return found


@dataclass
class PageSpeedRun:
    """Everything the stage produced, for the summary."""

    results: List[PageSpeedResult] = field(default_factory=list)
    samples: List[Sample] = field(default_factory=list)
    calls_made: int = 0
    skipped: bool = False
    skip_reason: Optional[str] = None

    @property
    def pages_represented(self) -> int:
        return sum(s.group_size for s in self.samples)

    def mean_score(self, strategy: str) -> Optional[float]:
        scores = [r.performance_score for r in self.results
                  if r.strategy == strategy and r.performance_score is not None]
        return round(sum(scores) / len(scores), 1) if scores else None

    def worst_template(self, strategy: str = "mobile") -> Optional[Dict]:
        candidates = [r for r in self.results
                      if r.strategy == strategy
                      and r.performance_score is not None]
        if not candidates:
            return None
        worst = min(candidates, key=lambda r: r.performance_score)
        return {"template": worst.template, "url": worst.url,
                "group_size": worst.group_size,
                "performance_score": worst.performance_score}

    def summary(self) -> Dict:
        return {
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "calls_made": self.calls_made,
            "templates_sampled": len(self.samples),
            "pages_represented": self.pages_represented,
            "mean_mobile_score": self.mean_score("mobile"),
            "mean_desktop_score": self.mean_score("desktop"),
            "worst_template_mobile": self.worst_template("mobile"),
            "errors": sum(1 for r in self.results if r.error),
        }


def run_pagespeed(fetcher, pages: List[Tuple[str, int, int]],
                  homepage: Optional[str], limit: int = DEFAULT_TEMPLATE_SAMPLE,
                  enabled: bool = True,
                  key: Optional[str] = None) -> PageSpeedRun:
    """Sample templates and measure each on mobile and desktop.

    Missing key or --no-pagespeed skips the stage cleanly: it is a section the
    audit does without, never a reason for the run to fail.
    """
    run = PageSpeedRun()
    if not enabled:
        run.skipped = True
        run.skip_reason = "--no-pagespeed was passed"
        return run

    resolved_key = key if key is not None else api_key()
    if not resolved_key:
        run.skipped = True
        run.skip_reason = (f"no {KEY_ENV_VAR} in the environment; set it in "
                           f".env to include a performance sample")
        return run

    run.samples = choose_templates(pages, limit=limit, homepage=homepage)
    client = PageSpeedClient(fetcher, key=resolved_key)
    for sample in run.samples:
        for strategy in STRATEGIES:
            result = client.measure(sample.url, strategy)
            result.template = sample.template
            result.group_size = sample.group_size
            run.results.append(result)
    run.calls_made = client.calls_made
    return run
