"""Page-level field extraction from an already-built soup.

Every field here belongs to `final_url` -- the URL that actually answered --
not to the URL that was requested. On a redirected page those differ, and a
check keyed on the requested URL would call every redirect a canonical fault.

This module reads the soup the worker already built for link extraction and
JSON-LD. It never fetches, never re-parses, and never holds the HTML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .urlnorm import distinct_targets, is_same_site, normalize

# Extensions that make a link a document rather than a page. Documents are
# indexable in their own right, so they are reported, not condemned.
DOCUMENT_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)

# Elements whose src/href pulls a subresource into the page. An http:// one on
# an https:// page is mixed content.
_SUBRESOURCE_TAGS = {
    "script": "src",
    "img": "src",
    "iframe": "src",
    "audio": "src",
    "video": "src",
    "embed": "src",
    "source": "src",
    "object": "data",
}

_WS_RE = re.compile(r"\s+")


@dataclass
class PageFields:
    """Everything one HTML page says about itself."""

    title: Optional[str] = None
    title_length: int = 0
    meta_description: Optional[str] = None
    meta_description_length: int = 0
    meta_robots: Optional[str] = None
    x_robots_tag: Optional[str] = None
    canonical: Optional[str] = None
    canonical_raw: Optional[str] = None
    hreflang: List[Tuple[str, str]] = field(default_factory=list)
    viewport: bool = False
    html_lang: Optional[str] = None
    h1: List[str] = field(default_factory=list)
    h2_count: int = 0
    h3_count: int = 0
    word_count: int = 0
    image_count: int = 0
    images_missing_alt: int = 0
    images_empty_alt: int = 0
    internal_links: int = 0
    external_links: int = 0
    nofollow_internal_links: int = 0
    mixed_content: List[str] = field(default_factory=list)
    microdata_types: List[str] = field(default_factory=list)
    visible_text: str = ""

    @property
    def has_microdata(self) -> bool:
        return bool(self.microdata_types)

    @property
    def h1_count(self) -> int:
        return len(self.h1)

    @property
    def first_h1(self) -> Optional[str]:
        return self.h1[0] if self.h1 else None

    def directives(self) -> set:
        """Lower-cased robots directives from the meta tag and the header."""
        found = set()
        for source in (self.meta_robots, self.x_robots_tag):
            if not source:
                continue
            for part in source.split(","):
                token = part.strip().lower()
                if token:
                    # "googlebot: noindex" carries the directive after the colon.
                    found.add(token.split(":")[-1].strip())
        return found

    @property
    def is_noindex(self) -> bool:
        return "noindex" in self.directives()

    @property
    def is_nofollow(self) -> bool:
        return "nofollow" in self.directives()


def is_document_url(url: str) -> bool:
    """True for a PDF or Office file, judged by the path, not the query."""
    path = urlsplit(url).path.lower()
    return path.endswith(DOCUMENT_EXTENSIONS)


def _text(node) -> str:
    return _WS_RE.sub(" ", node.get_text(" ", strip=True)).strip()


def _meta_content(soup, name: str) -> Optional[str]:
    tag = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)})
    if tag and tag.get("content") is not None:
        return tag["content"].strip()
    return None


def visible_text(soup) -> str:
    """The text a reader would see, with script and style stripped out.

    Destructive: it removes those elements from the soup, so nothing that
    needs them may run afterwards. The text is returned rather than counted
    because the content fingerprint needs it -- and it is dropped inside the
    worker, never stored.
    """
    for node in soup(["script", "style", "noscript", "template"]):
        node.extract()
    body = soup.body or soup
    return _text(body)


def extract_microdata_types(soup) -> List[str]:
    """Schema.org type names declared as microdata or RDFa.

    Presence only: microdata properties are not validated. Without this a
    page marked up entirely in microdata reports as having no structured
    data at all, which is a claim about our reader, not about the page.
    """
    found = set()
    for tag in soup.find_all(attrs={"itemtype": True}):
        for value in str(tag.get("itemtype", "")).split():
            name = value.rstrip("/").rsplit("/", 1)[-1]
            if name:
                found.add(name)
    for tag in soup.find_all(attrs={"typeof": True}):
        for value in str(tag.get("typeof", "")).split():
            name = value.split(":")[-1].rstrip("/").rsplit("/", 1)[-1]
            if name:
                found.add(name)
    return sorted(found)


def parse_page(soup, final_url: str, links: List, host: str,
               include_subdomains: bool = False,
               response_headers: Optional[Dict[str, str]] = None) -> PageFields:
    """Read one page's SEO fields off the shared soup.

    `links` is the already-extracted, already-normalised link list, so link
    counts cost nothing extra. `final_url` is the base every relative URL is
    resolved against, because that is the page that answered.
    """
    fields = PageFields()

    if soup.title and soup.title.string:
        fields.title = _WS_RE.sub(" ", soup.title.string).strip()
        fields.title_length = len(fields.title)

    fields.meta_description = _meta_content(soup, "description")
    if fields.meta_description:
        fields.meta_description_length = len(fields.meta_description)

    fields.meta_robots = _meta_content(soup, "robots")
    if response_headers:
        for key, value in response_headers.items():
            if key.lower() == "x-robots-tag":
                fields.x_robots_tag = value
                break

    canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in
                              [r.lower() for r in (v if isinstance(v, list) else [v])])
    if canonical_tag and canonical_tag.get("href"):
        fields.canonical_raw = canonical_tag["href"].strip()
        fields.canonical = normalize(fields.canonical_raw, base=final_url)

    for tag in soup.find_all("link", rel=lambda v: v and "alternate" in
                             [r.lower() for r in (v if isinstance(v, list) else [v])]):
        lang = tag.get("hreflang")
        href = tag.get("href")
        if lang and href:
            resolved = normalize(href, base=final_url)
            if resolved:
                fields.hreflang.append((lang.strip(), resolved))

    fields.viewport = _meta_content(soup, "viewport") is not None
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        fields.html_lang = html_tag["lang"].strip()

    fields.h1 = [_text(h) for h in soup.find_all("h1")]
    fields.h2_count = len(soup.find_all("h2"))
    fields.h3_count = len(soup.find_all("h3"))

    images = soup.find_all("img")
    fields.image_count = len(images)
    for img in images:
        alt = img.get("alt")
        if alt is None:
            fields.images_missing_alt += 1
        elif not alt.strip():
            # An empty alt is a deliberate "decorative" marker, not a fault.
            fields.images_empty_alt += 1

    for href in distinct_targets(links):
        if is_same_site(href, host, include_subdomains):
            fields.internal_links += 1
        else:
            fields.external_links += 1

    # rel comes off the same pass now, so there is no second walk of the DOM.
    nofollow_targets = {link.target for link in links if link.nofollow}
    fields.nofollow_internal_links = sum(
        1 for target in nofollow_targets
        if is_same_site(target, host, include_subdomains))

    fields.mixed_content = find_mixed_content(soup, final_url)
    fields.microdata_types = extract_microdata_types(soup)

    # Text last: it strips script and style out of the soup, so nothing that
    # needs those elements may run after it.
    fields.visible_text = visible_text(soup)
    fields.word_count = len(fields.visible_text.split())
    return fields


def find_mixed_content(soup, final_url: str) -> List[str]:
    """Subresources loaded over http:// on an https:// page."""
    if urlsplit(final_url).scheme != "https":
        return []
    found = []
    for tag_name, attr in _SUBRESOURCE_TAGS.items():
        for tag in soup.find_all(tag_name):
            value = tag.get(attr)
            if value and value.strip().lower().startswith("http://"):
                found.append(value.strip())
    for tag in soup.find_all("link", href=True):
        rel = tag.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        if any(r.lower() in ("stylesheet", "preload") for r in rel):
            if tag["href"].strip().lower().startswith("http://"):
                found.append(tag["href"].strip())
    return found
