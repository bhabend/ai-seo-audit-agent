"""robots.txt and sitemap discovery.

Lessons from v0 designed out here: sitemap handling was flat-only, so sitemap
index files and the Sitemap: directives in robots.txt were invisible. Both are
first-class now, and gzipped sitemaps are decompressed.
"""

from __future__ import annotations

import gzip
import io
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .urlnorm import normalize

MAX_SITEMAP_DEPTH = 3
MAX_SITEMAPS = 50

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_SITEMAP_BLOCK_RE = re.compile(r"<sitemap\b.*?</sitemap>", re.I | re.S)
_SITEMAPINDEX_RE = re.compile(r"<sitemapindex\b", re.I)


@dataclass
class RobotsInfo:
    """What robots.txt told us, or the fact that it told us nothing."""

    url: str
    found: bool = False
    status_code: Optional[int] = None
    disallow: List[str] = field(default_factory=list)
    allow: List[str] = field(default_factory=list)
    crawl_delay: Optional[float] = None
    sitemaps: List[str] = field(default_factory=list)
    error: Optional[str] = None
    raw_text: str = ""

    def is_allowed(self, url: str) -> bool:
        """Longest matching rule wins; Allow beats Disallow at equal length."""
        if not self.found:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = path + "?" + parts.query

        best_len = -1
        best_allowed = True
        for pattern in self.disallow:
            if _path_matches(pattern, path) and len(pattern) > best_len:
                best_len, best_allowed = len(pattern), False
        for pattern in self.allow:
            if _path_matches(pattern, path) and len(pattern) >= best_len:
                best_len, best_allowed = len(pattern), True
        return best_allowed


def _path_matches(pattern: str, path: str) -> bool:
    """robots.txt path matching, including the * and $ extensions."""
    if not pattern:
        return False
    regex = "".join(
        ".*" if ch == "*" else ("$" if ch == "$" else re.escape(ch))
        for ch in pattern
    )
    return re.match(regex, path) is not None


def _ua_matches(token: str, user_agent: str) -> bool:
    token = token.strip().lower()
    if token == "*":
        return True
    return token in user_agent.lower()


def parse_robots(text: str, user_agent: str, robots_url: str = "") -> RobotsInfo:
    """Parse robots.txt, keeping only the rule group that applies to us.

    Sitemap directives are global and are collected from the whole file.
    """
    info = RobotsInfo(url=robots_url, found=True)
    groups: List[Tuple[List[str], List[Tuple[str, str]], Optional[float]]] = []
    current_agents: List[str] = []
    current_rules: List[Tuple[str, str]] = []
    current_delay: Optional[float] = None
    last_was_agent = False

    def flush() -> None:
        nonlocal current_agents, current_rules, current_delay
        if current_agents:
            groups.append((current_agents, current_rules, current_delay))
        current_agents, current_rules, current_delay = [], [], None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            if not last_was_agent:
                flush()
            current_agents.append(value)
            last_was_agent = True
            continue
        last_was_agent = False

        if field_name in ("disallow", "allow"):
            current_rules.append((field_name, value))
        elif field_name == "crawl-delay":
            try:
                current_delay = float(value)
            except ValueError:
                pass
        elif field_name == "sitemap":
            if value:
                info.sitemaps.append(value)
    flush()

    # Prefer a group that names our UA; fall back to the wildcard group.
    specific = [
        g for g in groups
        if any(_ua_matches(a, user_agent) and a.strip() != "*" for a in g[0])
    ]
    wildcard = [g for g in groups if any(a.strip() == "*" for a in g[0])]
    chosen = specific or wildcard

    for _agents, rules, delay in chosen:
        for kind, value in rules:
            if kind == "disallow" and value:
                info.disallow.append(value)
            elif kind == "allow" and value:
                info.allow.append(value)
        if delay is not None:
            info.crawl_delay = (
                delay if info.crawl_delay is None
                else max(info.crawl_delay, delay)
            )
    return info


def fetch_robots(fetcher, config) -> RobotsInfo:
    """Fetch and parse robots.txt. A missing file is not an error."""
    robots_url = config.url_for("/robots.txt")
    status, body, _content_type, error = fetcher.fetch_bytes(robots_url)
    if error is not None:
        return RobotsInfo(url=robots_url, found=False, error=error)
    if status != 200 or not body:
        return RobotsInfo(url=robots_url, found=False, status_code=status)
    text = body.decode("utf-8", errors="replace")
    info = parse_robots(text, config.user_agent, robots_url)
    info.status_code = status
    info.raw_text = text
    return info


def _maybe_gunzip(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as fh:
                return fh.read()
        except OSError:
            return body
    return body


def parse_sitemap(xml_text: str, base_url: str) -> Tuple[List[str], List[str]]:
    """Split a sitemap document into (page urls, child sitemap urls)."""
    child_urls: List[str] = []
    index_blocks = _SITEMAP_BLOCK_RE.findall(xml_text)
    is_index = bool(index_blocks) or bool(_SITEMAPINDEX_RE.search(xml_text))

    consumed = xml_text
    for block in index_blocks:
        for loc in _LOC_RE.findall(block):
            child_urls.append(urljoin(base_url, loc.strip()))
        consumed = consumed.replace(block, " ")

    page_urls: List[str] = []
    for loc in _LOC_RE.findall(consumed):
        page_urls.append(urljoin(base_url, loc.strip()))

    if is_index and not child_urls:
        # A sitemapindex without <sitemap> wrappers: every loc is a child.
        child_urls, page_urls = page_urls, []
    return page_urls, child_urls


@dataclass
class SitemapResult:
    """Outcome of sitemap discovery for one run."""

    urls: List[str] = field(default_factory=list)
    sitemaps_used: List[str] = field(default_factory=list)
    sitemaps_failed: List[Tuple[str, str]] = field(default_factory=list)
    source_of_sitemap: Optional[str] = None  # config | robots | guess
    # Every sitemap URL seen, however we heard about it:
    # (url, how_we_found_it, whether it yielded anything).
    candidates: List[Tuple[str, str, bool]] = field(default_factory=list)


def collect_sitemap_urls(fetcher, start_urls: List[str],
                         max_depth: int = MAX_SITEMAP_DEPTH,
                         source: str = "guess") -> SitemapResult:
    """Walk sitemaps breadth-first, recursing into sitemap index files."""
    result = SitemapResult()
    seen_sitemaps = set()
    seen_urls = set()
    queue: List[Tuple[str, int]] = [(u, 0) for u in start_urls]
    found_via = {u: source for u in start_urls}

    while queue and len(seen_sitemaps) < MAX_SITEMAPS:
        sitemap_url, depth = queue.pop(0)
        if sitemap_url in seen_sitemaps or depth > max_depth:
            continue
        seen_sitemaps.add(sitemap_url)

        origin = found_via.get(sitemap_url, "sitemap_index")
        status, body, _ctype, error = fetcher.fetch_bytes(sitemap_url)
        if error is not None or status != 200 or not body:
            result.sitemaps_failed.append(
                (sitemap_url, error or "HTTP " + str(status)))
            if origin != "guess":
                # A conventional path we merely tried is not evidence of a
                # sitemap; one robots.txt advertises and fails to serve is.
                result.candidates.append((sitemap_url, origin, False))
            continue

        xml_text = _maybe_gunzip(body).decode("utf-8", errors="replace")
        pages, children = parse_sitemap(xml_text, sitemap_url)
        used = bool(pages or children)
        result.candidates.append((sitemap_url, origin, used))
        if used:
            result.sitemaps_used.append(sitemap_url)
        for page in pages:
            normalised = normalize(page)
            if normalised and normalised not in seen_urls:
                seen_urls.add(normalised)
                result.urls.append(normalised)
        for child in children:
            if child not in seen_sitemaps:
                found_via.setdefault(child, "sitemap_index")
                queue.append((child, depth + 1))
    return result


def discover_sitemaps(fetcher, config, robots: RobotsInfo) -> SitemapResult:
    """Find the site's sitemap: configured URL, then robots, then conventions."""
    if config.sitemap_url:
        result = collect_sitemap_urls(fetcher, [config.sitemap_url],
                                      source="config")
        result.source_of_sitemap = "config" if result.sitemaps_used else None
        return result

    if robots.sitemaps:
        result = collect_sitemap_urls(fetcher, list(robots.sitemaps),
                                      source="robots")
        if result.sitemaps_used:
            result.source_of_sitemap = "robots"
            return result

    guesses = [config.url_for("/sitemap.xml"),
               config.url_for("/sitemap_index.xml")]
    result = collect_sitemap_urls(fetcher, guesses, source="guess")
    result.source_of_sitemap = "guess" if result.sitemaps_used else None
    return result


def effective_crawl_delay(config, robots: RobotsInfo) -> float:
    """A robots Crawl-delay overrides the configured delay only when larger."""
    if robots.crawl_delay is not None and robots.crawl_delay > config.crawl_delay:
        return float(robots.crawl_delay)
    return float(config.crawl_delay)
