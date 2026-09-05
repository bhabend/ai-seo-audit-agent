"""The checks. Facts become findings here.

Everything is keyed on `final_url`, the URL that actually answered. Keying on
the requested URL would report a canonical fault on every redirected page --
58 of them on the WeWork run alone -- because the page correctly points at
where it ended up, not at where the link pointed.

Three kinds of check live here:
  * per page, run in the worker while the soup is still alive;
  * cross page, run after the crawl over the compact index the crawl kept,
    never re-fetching anything already crawled or swept;
  * site level, run once against the homepage response.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from .parse import PageFields
from .schema import SchemaResult

Finding = Tuple[str, str]  # (issue_type, detail)

TITLE_MAX = 60
TITLE_MIN = 20
META_DESCRIPTION_MAX = 160
THIN_PAGE_WORDS = 200


def check_page(fields: PageFields, schema: SchemaResult,
               final_url: str) -> List[Finding]:
    """Everything that can be judged from one page on its own."""
    found: List[Finding] = []

    if not fields.title:
        found.append(("title_missing", "no <title> or it is empty"))
    else:
        if fields.title_length > TITLE_MAX:
            found.append(("title_too_long",
                          f"{fields.title_length} characters (over {TITLE_MAX})"))
        elif fields.title_length < TITLE_MIN:
            found.append(("title_too_short",
                          f"{fields.title_length} characters (under {TITLE_MIN})"))

    if not fields.meta_description:
        found.append(("meta_description_missing", "no meta description"))
    elif fields.meta_description_length > META_DESCRIPTION_MAX:
        found.append(("meta_description_too_long",
                      f"{fields.meta_description_length} characters "
                      f"(over {META_DESCRIPTION_MAX})"))

    if fields.h1_count == 0:
        found.append(("h1_missing", "no h1 on the page"))
    elif fields.h1_count > 1:
        found.append(("h1_multiple", f"{fields.h1_count} h1 elements"))

    if fields.is_noindex:
        source = "meta robots" if fields.meta_robots else "X-Robots-Tag header"
        found.append(("noindex_page", f"noindex via {source}"))
    if fields.is_nofollow:
        found.append(("nofollow_page", "nofollow directive on the page"))

    if not fields.canonical:
        found.append(("canonical_missing", "no rel=canonical"))
    else:
        if fields.canonical_raw and not urlsplit(fields.canonical_raw).netloc:
            found.append(("canonical_not_absolute",
                          f"relative canonical {fields.canonical_raw!r}"))
        if fields.canonical != final_url:
            found.append(("canonical_off_page",
                          f"canonical points to {fields.canonical}"))

    if not fields.viewport:
        found.append(("viewport_missing", "no viewport meta tag"))
    if not fields.html_lang:
        found.append(("html_lang_missing", "no lang attribute on <html>"))

    if fields.images_missing_alt:
        found.append(("images_missing_alt",
                      f"{fields.images_missing_alt} of {fields.image_count} "
                      f"images have no alt attribute"))

    if fields.word_count < THIN_PAGE_WORDS:
        found.append(("thin_page",
                      f"{fields.word_count} words (under {THIN_PAGE_WORDS})"))

    if fields.mixed_content:
        sample = ", ".join(fields.mixed_content[:3])
        found.append(("mixed_content",
                      f"{len(fields.mixed_content)} http:// subresource(s): "
                      f"{sample}"))

    if schema.invalid_count:
        detail = "; ".join(schema.invalid_details) or "unparseable JSON-LD"
        found.append(("schema_invalid_json",
                      f"{schema.invalid_count} block(s) failed to parse: {detail}"))
    if schema.block_count == 0:
        if fields.has_microdata:
            # Structured data is there, just not in the format rich results
            # prefer. Reporting "no schema" here would be our limitation
            # stated as the page's fault.
            found.append((
                "schema_microdata_only",
                "structured data is microdata/RDFa only ("
                + ", ".join(fields.microdata_types[:5]) + "), no JSON-LD"))
        else:
            found.append(("schema_missing",
                          "no JSON-LD and no microdata on the page"))
    for type_name, prop in schema.missing_properties:
        found.append(("schema_missing_property",
                      f"{type_name} is missing {prop}"))

    return found


@dataclass
class PageRecord:
    """The little a cross-page check needs, kept for every parsed page.

    Deliberately not the whole row and never the HTML: a few hundred bytes a
    page, so a 2,000-page crawl costs about a megabyte.
    """

    url: str
    final_url: str
    title: Optional[str] = None
    meta_description: Optional[str] = None
    canonical: Optional[str] = None
    noindex: bool = False
    hreflang: List[Tuple[str, str]] = field(default_factory=list)
    in_sitemap: bool = False


class CrossPageIndex:
    """Accumulates the per-page facts the after-the-crawl checks need."""

    def __init__(self) -> None:
        self.pages: Dict[str, PageRecord] = {}
        # Everything the crawl already knows the status of, so a cross-page
        # check never re-fetches a URL the crawl or the sweep already saw.
        self.status_by_url: Dict[str, Optional[int]] = {}

    def add_page(self, record: PageRecord) -> None:
        self.pages[record.final_url] = record

    def note_status(self, url: str, status: Optional[int]) -> None:
        if url:
            self.status_by_url[url] = status

    def known(self, url: str) -> bool:
        return url in self.status_by_url or url in self.pages


def check_cross_page(index: CrossPageIndex, head_check=None,
                     canonical_check_limit: int = 200
                     ) -> List[Tuple[str, str, str, str]]:
    """Checks that need more than one page.

    Returns (issue_type, url, final_url, detail) tuples. `head_check` is a
    callable taking a URL and returning a status code, used only for canonical
    targets the crawl never saw, and only up to `canonical_check_limit`.
    """
    out: List[Tuple[str, str, str, str]] = []
    pages = index.pages
    checked_externally = 0

    for final_url, page in pages.items():
        # --- canonical targets -------------------------------------------
        target = page.canonical
        if target and target != final_url:
            if target in pages:
                other = pages[target]
                if other.canonical and other.canonical != target:
                    out.append((
                        "canonical_chain", page.url, final_url,
                        f"canonical {target} itself canonicalises to "
                        f"{other.canonical}"))
                if other.noindex:
                    out.append(("canonical_target_noindex", page.url, final_url,
                                f"canonical target {target} is noindex"))
            elif target in index.status_by_url:
                status = index.status_by_url[target]
                if status is not None and status != 200:
                    out.append(("canonical_target_non_200", page.url, final_url,
                                f"canonical target {target} returned {status}"))
            elif head_check is not None and checked_externally < canonical_check_limit:
                checked_externally += 1
                status = head_check(target)
                index.note_status(target, status)
                if status is None:
                    out.append(("canonical_target_not_crawled", page.url,
                                final_url,
                                f"canonical target {target} did not respond"))
                elif status != 200:
                    out.append(("canonical_target_non_200", page.url, final_url,
                                f"canonical target {target} returned {status}"))
            else:
                out.append((
                    "canonical_target_unchecked", page.url, final_url,
                    f"canonical target {target} was not crawled and the "
                    f"check limit of {canonical_check_limit} was reached"))

        # --- hreflang reciprocity ----------------------------------------
        for lang, href in page.hreflang:
            if href == final_url:
                continue  # self-reference is expected
            other = pages.get(href)
            if other is None:
                status = index.status_by_url.get(href)
                if status is not None and status != 200:
                    out.append(("hreflang_target_non_200", page.url, final_url,
                                f"hreflang {lang} target {href} returned {status}"))
                continue
            back = {h for _lang, h in other.hreflang}
            if final_url not in back:
                out.append(("hreflang_not_reciprocal", page.url, final_url,
                            f"points to {href} as {lang}, which does not "
                            f"point back"))

        # --- sitemap agreement -------------------------------------------
        if page.in_sitemap:
            if page.noindex:
                out.append(("sitemap_noindex", page.url, final_url,
                            "listed in the sitemap but marked noindex"))
            if page.canonical and page.canonical != final_url:
                out.append(("sitemap_off_canonical", page.url, final_url,
                            f"listed in the sitemap but canonicalises to "
                            f"{page.canonical}"))

    out.extend(_duplicate_groups(pages))
    return out


def _duplicate_groups(pages: Dict[str, PageRecord]
                      ) -> List[Tuple[str, str, str, str]]:
    """One row per group of pages sharing a title or a meta description."""
    out: List[Tuple[str, str, str, str]] = []
    for attribute, issue_type in (("title", "duplicate_title"),
                                  ("meta_description",
                                   "duplicate_meta_description")):
        groups: Dict[str, List[PageRecord]] = defaultdict(list)
        for page in pages.values():
            value = getattr(page, attribute)
            if value:
                groups[value].append(page)
        for value, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            urls = sorted(p.final_url for p in members)
            listed = ", ".join(urls[:5])
            if len(urls) > 5:
                listed += f", and {len(urls) - 5} more"
            out.append((issue_type, urls[0], urls[0],
                        f"{len(urls)} pages share {attribute} "
                        f"{value[:80]!r}: {listed}"))
    return out


# --- site level -------------------------------------------------------------

SECURITY_HEADERS = (
    ("strict-transport-security", "hsts_missing"),
    ("x-content-type-options", "x_content_type_options_missing"),
    ("x-frame-options", "x_frame_options_missing"),
    ("content-security-policy", "csp_missing"),
)


def check_site_level(homepage_headers: Dict[str, str],
                     http_redirects_to_https: Optional[bool]) -> List[Finding]:
    """Headers and the plain-http redirect, judged once for the whole site."""
    found: List[Finding] = []

    if http_redirects_to_https is False:
        found.append(("http_to_https_redirect",
                      "http:// does not redirect to https://"))

    present = {key.lower() for key in homepage_headers}
    for header, issue_type in SECURITY_HEADERS:
        if header not in present:
            found.append((issue_type, f"no {header} header on the homepage"))

    # X-Frame-Options is satisfied by a CSP frame-ancestors directive.
    csp = next((v for k, v in homepage_headers.items()
                if k.lower() == "content-security-policy"), "")
    if "frame-ancestors" in csp.lower():
        found = [f for f in found if f[0] != "x_frame_options_missing"]
    return found
