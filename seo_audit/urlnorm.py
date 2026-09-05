"""URL normalisation and same-site tests.

Lesson from v0: internal/external classification used `domain in href`, which
misfired on subdomains, protocol-relative links and `../` paths. Everything
here goes through urlparse/urljoin instead of substring matching.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# Params that never change the page served, only the analytics attached to it.
TRACKING_PARAMS = {
    "gclid",
    "fbclid",
    "msclkid",
    "dclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "igshid",
}
TRACKING_PREFIXES = ("utm_",)

DEFAULT_PORTS = {"http": "80", "https": "443"}

_WHITESPACE_RE = re.compile(r"\s+")

# Schemes we never follow.
NON_FETCHABLE_SCHEMES = {
    "mailto",
    "tel",
    "javascript",
    "sms",
    "fax",
    "callto",
    "whatsapp",
    "data",
    "file",
    "ftp",
    "skype",
    "viber",
}


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    if lowered in TRACKING_PARAMS:
        return True
    return any(lowered.startswith(p) for p in TRACKING_PREFIXES)


def strip_www(host: str) -> str:
    """`www.example.com` -> `example.com`. Anything else is left alone."""
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _resolve_dots(path: str) -> str:
    """Collapse `.` and `..` segments while preserving a trailing slash."""
    if not path:
        return "/"
    had_trailing_slash = path.endswith("/")
    resolved = posixpath.normpath(path)
    if resolved == ".":
        resolved = "/"
    if not resolved.startswith("/"):
        resolved = "/" + resolved
    if had_trailing_slash and not resolved.endswith("/"):
        resolved += "/"
    return resolved


def normalize(url: str, base: Optional[str] = None) -> Optional[str]:
    """Canonical form of `url`, or None if it is not a fetchable http(s) URL.

    Lowercases scheme and host, drops the fragment, drops default ports,
    removes tracking params, resolves `..` segments. A trailing slash is kept
    exactly as the site wrote it: `/path` and `/path/` stay distinct, because
    on some sites they genuinely are.
    """
    if url is None:
        return None
    url = url.strip()
    if not url:
        return None
    if url.startswith("#"):
        # A fragment-only href goes nowhere; resolving it would yield a
        # self-link back to the page we are already on.
        return None
    if base:
        url = urljoin(base, url)

    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme in NON_FETCHABLE_SCHEMES:
        return None
    if scheme not in ("http", "https"):
        return None
    if not parts.netloc:
        return None

    netloc = parts.netloc.lower()
    # Drop any userinfo and a port that is the scheme default.
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    if ":" in netloc:
        host, _, port = netloc.rpartition(":")
        if port == DEFAULT_PORTS.get(scheme) or port == "":
            netloc = host
        elif not port.isdigit():
            netloc = netloc  # malformed; leave as-is rather than guess

    path = _resolve_dots(parts.path)

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking(k)]
    query = urlencode(kept, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def is_same_site(url: str, host: str, include_subdomains: bool = False) -> bool:
    """True when `url` belongs to the site rooted at `host`.

    `www.example.com` and `example.com` always count as the same site. Other
    subdomains only count when include_subdomains is True.
    """
    if not url:
        return False
    try:
        url_host = urlsplit(url).netloc.lower()
    except ValueError:
        return False
    if not url_host:
        return False
    if "@" in url_host:
        url_host = url_host.rsplit("@", 1)[1]
    url_host = url_host.split(":", 1)[0]

    base = strip_www(host.split(":", 1)[0])
    candidate = strip_www(url_host)

    if candidate == base:
        return True
    if include_subdomains and candidate.endswith("." + base):
        return True
    return False


def slash_variant(url: str) -> Optional[str]:
    """The same URL with its trailing slash toggled, or None if not applicable.

    `/x` <-> `/x/`. The site root has no variant: `/` toggled is not a URL.
    """
    if not url:
        return None
    parts = urlsplit(url)
    path = parts.path
    if not path or path == "/":
        return None
    flipped = path[:-1] if path.endswith("/") else path + "/"
    if not flipped:
        return None
    return urlunsplit((parts.scheme, parts.netloc, flipped, parts.query, ""))


def is_slash_variant(a: str, b: str) -> bool:
    """True when two URLs differ only by a trailing slash."""
    return bool(a and b and slash_variant(a) == b)


def make_soup(html: str):
    """The one BeautifulSoup pass a page gets.

    Link extraction, field parsing and JSON-LD all read this same tree, so a
    page is never parsed twice.
    """
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "lxml")


@dataclass(frozen=True)
class Link:
    """One outgoing link: where it points, what it said, and whether it counts."""

    target: str
    anchor: str = ""
    nofollow: bool = False


# Anchor text long enough to be useful, short enough not to bloat the graph.
MAX_ANCHOR_CHARS = 120


def extract_links(soup, base_url: str) -> List[Link]:
    """Every <a> in `soup` as a Link, in document order.

    Returns anchor text and rel alongside the href so the link graph, the
    nofollow count and the anchor-text checks all come off the one pass the
    page already had. Deduped on (target, anchor): the same page linked twice
    under two different anchors is two facts, not one.
    """
    seen = set()
    out: List[Link] = []
    for anchor in soup.find_all("a", href=True):
        target = normalize(anchor["href"], base=base_url)
        if not target:
            continue
        text = _WHITESPACE_RE.sub(" ", anchor.get_text(" ", strip=True)).strip()
        if len(text) > MAX_ANCHOR_CHARS:
            text = text[:MAX_ANCHOR_CHARS]
        rel = anchor.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        nofollow = any(r.lower() == "nofollow" for r in rel)
        key = (target, text)
        if key in seen:
            continue
        seen.add(key)
        out.append(Link(target=target, anchor=text, nofollow=nofollow))
    return out


def distinct_targets(links: List[Link]) -> List[str]:
    """The unique URLs a page links to, in first-seen order."""
    seen = set()
    out = []
    for link in links:
        if link.target not in seen:
            seen.add(link.target)
            out.append(link.target)
    return out
