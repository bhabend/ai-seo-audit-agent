"""Turning findings into a page score and a site score.

The weights live in score_weights.json, not here: retuning the audit should
be a data edit, not a code change. This module only applies them.

Two design points worth stating, because both are places a score can lie:

  * Each bucket floors at zero. Without that, a page with fourteen on-page
    faults would drag the whole score negative and rank below a page that is
    merely unindexable, which is the wrong order of seriousness.
  * A type with no entry in the weights file is a build failure, not a silent
    zero. `check_registry_coverage` is run by a test for exactly that reason:
    an unscored issue is an issue nobody sees.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "score_weights.json")

# The share of the site score that performance carries when PageSpeed ran.
PERFORMANCE_WEIGHT = 0.15

SCORE_BANDS = (("0-39", 0, 39), ("40-59", 40, 59),
               ("60-79", 60, 79), ("80-100", 80, 100))


@dataclass
class Weights:
    """The weight table, loaded once per run."""

    buckets: Dict[str, int] = field(default_factory=dict)
    page_issues: Dict[str, Dict] = field(default_factory=dict)
    crawl_issues: Dict[str, Dict] = field(default_factory=dict)

    @property
    def bucket_names(self) -> List[str]:
        return list(self.buckets)

    def bucket_of(self, issue_type: str) -> Optional[str]:
        entry = self.page_issues.get(issue_type)
        return entry.get("bucket") if entry else None

    def points_of(self, issue_type: str) -> int:
        entry = self.page_issues.get(issue_type)
        return int(entry.get("points", 0)) if entry else 0


def load_weights(path: str = WEIGHTS_PATH) -> Weights:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    return Weights(buckets=doc.get("buckets", {}),
                   page_issues=doc.get("page_issues", {}),
                   crawl_issues=doc.get("crawl_issues", {}))


def check_registry_coverage(weights: Weights, page_types: Iterable[str],
                            crawl_types: Iterable[str]) -> List[str]:
    """Problems with the weight table, as plain sentences. Empty means fine."""
    problems: List[str] = []
    page_types, crawl_types = set(page_types), set(crawl_types)

    total = sum(weights.buckets.values())
    if total != 100:
        problems.append(f"buckets sum to {total}, not 100")

    for issue_type in sorted(page_types - set(weights.page_issues)):
        problems.append(
            f"registered page issue {issue_type!r} has no weight entry")
    for issue_type in sorted(set(weights.page_issues) - page_types):
        problems.append(
            f"weight entry {issue_type!r} names no registered page issue")
    for issue_type in sorted(crawl_types - set(weights.crawl_issues)):
        problems.append(
            f"registered crawl issue {issue_type!r} is not listed")
    for issue_type in sorted(set(weights.crawl_issues) - crawl_types):
        problems.append(
            f"crawl weight entry {issue_type!r} names no registered issue")

    for issue_type, entry in sorted(weights.page_issues.items()):
        bucket = entry.get("bucket")
        if bucket not in weights.buckets:
            problems.append(
                f"page issue {issue_type!r} names unknown bucket {bucket!r}")
        if entry.get("points", 0) < 0:
            problems.append(f"page issue {issue_type!r} has negative points")
    for issue_type, entry in sorted(weights.crawl_issues.items()):
        if entry.get("bucket") is not None:
            problems.append(
                f"crawl issue {issue_type!r} must have bucket null: crawl "
                f"issues describe the crawl, not a page")
    return problems


@dataclass
class PageScore:
    """One page's score and the buckets it came from."""

    final_url: str
    score: int = 100
    buckets: Dict[str, int] = field(default_factory=dict)
    top_issue: Optional[str] = None

    def as_columns(self) -> Dict[str, int]:
        row = {"score": self.score}
        for name, value in self.buckets.items():
            row[f"score_{name}"] = value
        return row


def score_page(issue_types: Iterable[str], weights: Weights,
               final_url: str = "") -> PageScore:
    """A page starts at 100 and loses points bucket by bucket, never below 0."""
    remaining = dict(weights.buckets)
    worst: Tuple[int, str] = (0, "")

    for issue_type in issue_types:
        bucket = weights.bucket_of(issue_type)
        if bucket is None or bucket not in remaining:
            continue  # crawl-layer or unknown: does not score the page
        points = weights.points_of(issue_type)
        remaining[bucket] = max(0, remaining[bucket] - points)
        if points > worst[0]:
            worst = (points, issue_type)

    return PageScore(final_url=final_url,
                     score=sum(remaining.values()),
                     buckets=remaining,
                     top_issue=worst[1] or None)


def band_of(score: int) -> str:
    for name, low, high in SCORE_BANDS:
        if low <= score <= high:
            return name
    return SCORE_BANDS[-1][0]


@dataclass
class SiteScore:
    """The headline number, and enough context to know what it means."""

    site_score: int = 0
    mean_page_score: float = 0.0
    performance_score: Optional[float] = None
    performance_included: bool = False
    pages_scored: int = 0
    distribution: Dict[str, int] = field(default_factory=dict)
    lowest_pages: List[Dict] = field(default_factory=list)
    note: str = ""

    def as_summary(self) -> Dict:
        return {
            "site_score": self.site_score,
            "mean_page_score": round(self.mean_page_score, 1),
            "performance_component": self.performance_score,
            "performance_included": self.performance_included,
            "performance_weight": PERFORMANCE_WEIGHT
            if self.performance_included else 0,
            "pages_scored": self.pages_scored,
            "distribution": self.distribution,
            "lowest_pages": self.lowest_pages,
            "note": self.note,
        }


class ScoreBoard:
    """Accumulates page scores and produces the site score."""

    def __init__(self, weights: Weights):
        self.weights = weights
        self.scores: List[PageScore] = []

    def add(self, page: PageScore) -> None:
        self.scores.append(page)

    def site_score(self, mobile_scores: Optional[List[int]] = None
                   ) -> SiteScore:
        """Mean page score, with performance folded in only if it was measured."""
        result = SiteScore(pages_scored=len(self.scores))
        if not self.scores:
            result.note = "no pages were parsed, so there is nothing to score"
            return result

        values = [p.score for p in self.scores]
        result.mean_page_score = sum(values) / len(values)

        usable = [s for s in (mobile_scores or []) if s is not None]
        if usable:
            result.performance_score = round(sum(usable) / len(usable), 1)
            result.performance_included = True
            result.site_score = round(
                (1 - PERFORMANCE_WEIGHT) * result.mean_page_score
                + PERFORMANCE_WEIGHT * result.performance_score)
            result.note = (
                f"{int((1 - PERFORMANCE_WEIGHT) * 100)}% mean page score, "
                f"{int(PERFORMANCE_WEIGHT * 100)}% mean mobile performance "
                f"over {len(usable)} sampled template(s)")
        else:
            result.site_score = round(result.mean_page_score)
            result.note = ("performance was not included: no PageSpeed "
                           "results, so this is the mean page score alone")

        counts = Counter(band_of(v) for v in values)
        result.distribution = {name: counts.get(name, 0)
                               for name, _low, _high in SCORE_BANDS}

        lowest = sorted(self.scores, key=lambda p: (p.score, p.final_url))[:10]
        result.lowest_pages = [
            {"final_url": p.final_url, "score": p.score,
             "top_issue": p.top_issue} for p in lowest]
        return result
