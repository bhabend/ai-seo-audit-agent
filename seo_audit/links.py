"""The internal link graph, built after the crawl from edges the workers emit.

Two things make this honest rather than merely countable:

  * Every target is resolved through the crawl's url -> final_url map before
    it is counted. A page linked only as `/x` when the site serves `/x/` has
    one inlink, not zero, and is not an orphan.
  * Every count that depends on the page cap says so in its own detail. A
    page with no inlinks inside a 2,000-page sample is not the same claim as
    a page with no inlinks on the site, and the row says which one it is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .urlnorm import is_same_site

# Anchors that tell a search engine nothing about the destination.
GENERIC_ANCHORS = {
    "click here", "read more", "learn more", "here", "more",
    "more info", "more information", "find out more", "see more",
    "details", "link", "this", "this page", "continue", "continue reading",
}

MAX_REFERRERS_LISTED = 5
MAX_ANCHORS_LISTED = 5


@dataclass(frozen=True)
class Edge:
    """One internal link, before target resolution."""

    source: str
    target: str
    anchor: str = ""
    nofollow: bool = False


@dataclass
class PageLinks:
    """What the graph knows about one crawled page."""

    inlinks: int = 0
    nofollow_inlinks: int = 0
    outlinks_internal: int = 0
    outlinks_external: int = 0
    nofollow_outlinks: int = 0
    anchor_texts: List[str] = field(default_factory=list)


class LinkGraph:
    """Edges in, aggregates out.

    Edges are held in memory for the length of the run and written only as
    per-page aggregates, unless --write-links asks for the raw list.
    """

    def __init__(self, host: str, include_subdomains: bool = False):
        self.host = host
        self.include_subdomains = include_subdomains
        self.edges: List[Edge] = []
        self.external_targets: Dict[str, Set[str]] = defaultdict(set)
        self._outlinks_external: Dict[str, int] = defaultdict(int)

    def add_page(self, source: str, links) -> None:
        """Record one page's outgoing links. `links` are urlnorm.Link objects."""
        seen_internal: Set[Tuple[str, str]] = set()
        for link in links:
            if is_same_site(link.target, self.host, self.include_subdomains):
                key = (link.target, link.anchor)
                if key in seen_internal:
                    continue
                seen_internal.add(key)
                self.edges.append(Edge(source=source, target=link.target,
                                       anchor=link.anchor,
                                       nofollow=link.nofollow))
            else:
                if link.target not in self.external_targets:
                    self.external_targets[link.target] = set()
                self.external_targets[link.target].add(source)
                self._outlinks_external[source] += 1

    def resolve(self, url_to_final: Dict[str, str]) -> None:
        """Point every edge at the URL that actually answered.

        Without this a link to `/x` that 301s to `/x/` credits nothing, and
        the page it points at looks like an orphan.
        """
        resolved = []
        for edge in self.edges:
            final = url_to_final.get(edge.target, edge.target)
            resolved.append(Edge(source=edge.source, target=final,
                                 anchor=edge.anchor, nofollow=edge.nofollow))
        self.edges = resolved

    # ---- aggregates ------------------------------------------------------

    def per_page(self, crawled: Iterable[str]) -> Dict[str, PageLinks]:
        """Inlink and outlink counts for every crawled page."""
        pages = {url: PageLinks() for url in crawled}

        inbound_sources: Dict[str, Set[str]] = defaultdict(set)
        inbound_nofollow: Dict[str, Set[str]] = defaultdict(set)
        anchors: Dict[str, List[str]] = defaultdict(list)
        outbound_targets: Dict[str, Set[str]] = defaultdict(set)
        nofollow_out: Dict[str, int] = defaultdict(int)

        for edge in self.edges:
            outbound_targets[edge.source].add(edge.target)
            if edge.nofollow:
                nofollow_out[edge.source] += 1
            if edge.source == edge.target:
                continue  # a page linking to itself is not an inlink
            inbound_sources[edge.target].add(edge.source)
            if edge.nofollow:
                inbound_nofollow[edge.target].add(edge.source)
            if edge.anchor and edge.anchor not in anchors[edge.target]:
                anchors[edge.target].append(edge.anchor)

        for url, page in pages.items():
            page.inlinks = len(inbound_sources.get(url, ()))
            page.nofollow_inlinks = len(inbound_nofollow.get(url, ()))
            page.outlinks_internal = len(outbound_targets.get(url, ()))
            page.outlinks_external = self._outlinks_external.get(url, 0)
            page.nofollow_outlinks = nofollow_out.get(url, 0)
            page.anchor_texts = anchors.get(url, [])[:MAX_ANCHORS_LISTED]
        return pages

    def referrers_of(self, target: str) -> List[str]:
        return sorted({e.source for e in self.edges if e.target == target})

    def targets_not_crawled(self, crawled: Set[str]) -> Dict[str, Set[str]]:
        """Internal targets that link out to something the crawl never fetched."""
        out: Dict[str, Set[str]] = defaultdict(set)
        for edge in self.edges:
            if edge.target not in crawled:
                out[edge.target].add(edge.source)
        return out

    def inbound_anchors(self, target: str) -> List[str]:
        return [e.anchor for e in self.edges
                if e.target == target and e.source != target and e.anchor]


def describe_referrers(sources: Iterable[str]) -> str:
    """"linked from N pages: a, b, c" -- the count first, examples after."""
    listed = sorted(sources)
    head = ", ".join(listed[:MAX_REFERRERS_LISTED])
    if len(listed) > MAX_REFERRERS_LISTED:
        head += f", and {len(listed) - MAX_REFERRERS_LISTED} more"
    return f"linked from {len(listed)} page(s): {head}"


def is_generic_anchor(text: str) -> bool:
    return text.strip().lower().strip(" .:>»-") in GENERIC_ANCHORS


def cap_note(cap: int, what: str = "pages") -> str:
    """The sentence that keeps a capped count from reading as a site-wide fact."""
    return f"within the {cap} {what} this run crawled"
