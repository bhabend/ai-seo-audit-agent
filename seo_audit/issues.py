"""The crawlability log: every obstacle a search engine would meet.

The rule this file exists to enforce: whenever the crawler resolves, folds,
adopts or skips something to keep going, it says so here. Nothing is silently
improved. Each issue type carries one plain sentence about what a search
engine actually does with it, so the log reads without a decoder ring.
"""

from __future__ import annotations

import csv
import os
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Optional

# The fixed set. A type not in here cannot be recorded, so a typo fails loudly
# instead of quietly inventing a category nobody counts.
CRAWLER_EFFECT: Dict[str, str] = {
    "host_redirect":
        "Every crawl of the configured host costs an extra hop before any "
        "page is reached, and split signals between the two hostnames.",
    "trailing_slash_redirect":
        "The crawler spends an extra request for this link and the link "
        "equity passes through a redirect instead of landing directly.",
    "redirect_chain":
        "Chained redirects slow discovery and some crawlers give up before "
        "reaching the destination.",
    "redirect_loop":
        "The URL never resolves, so the page cannot be crawled or indexed.",
    "redirect_in_sitemap":
        "A sitemap should list final URLs; a redirecting entry wastes crawl "
        "budget and sends a contradictory canonical signal.",
    "robots_blocked_linked":
        "The page is linked internally but robots.txt forbids fetching it, "
        "so that link leads nowhere a crawler can follow.",
    "robots_blocked_in_sitemap":
        "The sitemap asks the crawler to index a URL that robots.txt forbids "
        "it to fetch; the two directives contradict each other.",
    "sitemap_non_200":
        "A sitemap entry that does not return 200 wastes crawl budget and "
        "marks the sitemap as stale.",
    "multiple_sitemaps":
        "Several sitemaps exist; any not advertised in robots.txt may never "
        "be discovered by a crawler that was not told where to look.",
    "crawl_only_page":
        "The page is reachable by links but missing from the sitemap, so "
        "discovery depends entirely on internal linking.",
    "deep_page":
        "Pages more than three clicks from the homepage are crawled less "
        "often and tend to rank worse.",
    "slow_response":
        "Slow responses reduce how many pages a crawler fetches per visit.",
    "fetch_error":
        "The crawler gets nothing back, so the page cannot be indexed.",
    "request_refused":
        "The server answered, but refused: nothing behind this address was "
        "read, and nothing about it can be judged from what came back.",
    "non_html_linked":
        "The URL is linked as if it were a page but returns something that is "
        "neither a page nor an indexable document.",
    "document_linked":
        "A PDF or Office file linked as a page. Search engines index these on "
        "their own terms, so it is worth knowing about, not a fault.",
    "render_suspect":
        "Almost no text arrives in the HTML, so a crawler that does not run "
        "JavaScript sees a nearly empty page.",
    "sweep_throttled":
        "The site started refusing the sweep, so the URLs after that point "
        "were never checked and their status is unknown, not broken.",
    "sitemap_sweep_capped":
        "The sitemap is larger than the sweep limit, so the URLs past the cap "
        "were never status-checked and their state is unknown.",
    "ai_crawler_rule":
        "This robots.txt group decides whether AI assistants and their "
        "crawlers may read the site's content.",
}

ISSUE_COLUMNS = ["issue_type", "url", "referrer", "detail", "crawler_effect"]

# A server that refuses hands back a body: an edge network's "Access Denied",
# a login wall, a rate limit notice. It is not the thing that was asked for.
# The test lives here rather than in the crawl, because robots.txt and the
# sitemap are refused by the same servers and must read them the same way.
BLOCKED_STATUSES = (401, 403, 429)


def is_blocked_status(status) -> bool:
    """True when the server refused rather than answered."""
    if status is None:
        return False
    return status in BLOCKED_STATUSES or status >= 500

# Thresholds the crawl layer judges against.
DEEP_PAGE_DEPTH = 3
# A document is a big file over a slow pipe; a page is not. Judging both at
# 3 seconds reported every large PDF as a site defect.
SLOW_RESPONSE_MS = 3000
SLOW_DOCUMENT_MS = 10000
RENDER_SUSPECT_CHARS = 500

# Page-level checks, each with the severity it is reported at. Separate from
# CRAWLER_EFFECT above: those are obstacles a crawler meets on the way in,
# these are faults in a page that was fetched successfully.
PAGE_ISSUE_SEVERITY: Dict[str, str] = {
    # per page
    "title_missing": "high",
    "title_too_long": "low",
    "title_too_short": "low",
    "meta_description_missing": "medium",
    "meta_description_too_long": "low",
    "h1_missing": "medium",
    "h1_multiple": "low",
    "noindex_page": "high",
    "nofollow_page": "medium",
    "canonical_missing": "medium",
    "canonical_off_page": "medium",
    # A filtered or tracked address pointing back at its clean page is the
    # site doing the right thing. It is reported, never listed as a fix.
    "canonical_consolidates_variant": "info",
    "canonical_not_absolute": "low",
    "viewport_missing": "medium",
    "html_lang_missing": "low",
    "images_missing_alt": "low",
    "thin_page": "medium",
    "mixed_content": "high",
    "schema_missing": "low",
    "schema_invalid_json": "medium",
    "schema_missing_property": "low",
    # cross page
    "canonical_target_not_crawled": "medium",
    "canonical_target_unchecked": "low",
    "canonical_target_non_200": "high",
    "canonical_chain": "medium",
    "canonical_target_noindex": "high",
    "hreflang_not_reciprocal": "medium",
    "hreflang_target_non_200": "medium",
    "duplicate_title": "medium",
    "duplicate_meta_description": "low",
    "sitemap_noindex": "high",
    "sitemap_off_canonical": "medium",
    # links
    "orphan_page": "medium",
    "low_inlink_page": "low",
    "broken_internal_link": "high",
    "redirected_internal_link": "low",
    "nofollow_internal_link": "low",
    "generic_anchor": "low",
    "external_link_broken": "medium",
    # content
    "duplicate_content": "medium",
    "near_duplicate_content": "low",
    "schema_microdata_only": "low",
    # performance, measured on a template sample
    "performance_poor": "medium",
    "lcp_poor": "medium",
    "cls_poor": "medium",
    "inp_poor": "medium",
    # Not "info": these mean the measurement did not happen, which is a
    # different statement from "we looked and there is nothing to report".
    "performance_no_field_data": "unmeasured",
    "pagespeed_error": "unmeasured",
    # search performance, recorded only when a Search Console export has
    # been attached to the run. They describe what search engines did with a
    # page, not what is wrong with the page, so they are never scored.
    "gsc_impressions_on_noindex": "high",
    "gsc_impressions_on_off_canonical": "medium",
    "gsc_sitemap_page_no_impressions": "low",
    "gsc_orphan_page_with_clicks": "medium",
    "gsc_query_cannibalised": "medium",
    # site level, recorded once on the homepage row
    "http_to_https_redirect": "high",
    "hsts_missing": "medium",
    "x_content_type_options_missing": "low",
    "x_frame_options_missing": "low",
    "csp_missing": "low",
}

PAGE_ISSUE_COLUMNS = ["issue_type", "severity", "url", "final_url", "detail",
                      "site_level"]

# What each issue is called in front of a client, what its count counts, and
# one sentence stating the finding. Identifiers like "title_missing" are for
# the code; none of them should reach a document.
#   label         short plain name, no underscores
#   plain_finding one sentence taking {count} and {whole}
#   unit          what the count counts: page, target, group or site
PAGE_ISSUE_META: Dict[str, tuple] = {
    "title_missing": ("page title missing",
                      "{count} of {whole} pages have no page title", "page"),
    "title_too_long": ("page title too long",
                       "{count} of {whole} pages have a title that will be "
                       "cut short in search results", "page"),
    "title_too_short": ("page title very short",
                        "{count} of {whole} pages have a title of only a few "
                        "characters", "page"),
    "meta_description_missing": (
        "search description missing",
        "{count} of {whole} pages have no search description", "page"),
    "meta_description_too_long": (
        "search description too long",
        "{count} of {whole} pages have a search description that will be cut "
        "short", "page"),
    "h1_missing": ("main heading missing",
                   "{count} of {whole} pages have no main heading", "page"),
    "h1_multiple": ("more than one main heading",
                    "{count} of {whole} pages carry more than one main "
                    "heading", "page"),
    "noindex_page": ("page hidden from search",
                     "{count} of {whole} pages tell search engines not to "
                     "list them", "page"),
    "nofollow_page": ("links on the page not followed",
                      "{count} of {whole} pages tell search engines not to "
                      "follow their links", "page"),
    "canonical_missing": ("preferred address missing",
                          "{count} of {whole} pages do not name a preferred "
                          "address", "page"),
    "canonical_off_page": ("preferred address points elsewhere",
                           "{count} of {whole} pages name a different page as "
                           "the preferred one", "page"),
    "canonical_consolidates_variant": (
        "preferred address points a variant to its clean page",
        "{count} of {whole} pages correctly point a filtered or tracked "
        "address back to their clean address", "page"),
    "canonical_not_absolute": ("preferred address written as a partial link",
                               "{count} of {whole} pages write the preferred "
                               "address as a partial link", "page"),
    "viewport_missing": ("no mobile layout setting",
                         "{count} of {whole} pages have no mobile layout "
                         "setting", "page"),
    "html_lang_missing": ("page language not declared",
                          "{count} of {whole} pages do not declare their "
                          "language", "page"),
    "images_missing_alt": ("images without a description",
                           "{count} of {whole} pages have images with no text "
                           "description", "page"),
    "thin_page": ("very little text",
                  "{count} of {whole} pages carry very little text", "page"),
    "mixed_content": ("insecure files on a secure page",
                      "{count} of {whole} pages load files over an insecure "
                      "connection", "page"),
    "schema_missing": ("no structured data",
                       "{count} of {whole} pages carry no structured data",
                       "page"),
    "schema_invalid_json": ("structured data will not parse",
                            "{count} of {whole} pages carry structured data "
                            "that cannot be read", "page"),
    "schema_missing_property": ("structured data incomplete",
                                "{count} of {whole} pages carry structured "
                                "data missing a required field", "page"),
    "schema_microdata_only": ("structured data in an older format",
                              "{count} of {whole} pages carry structured data "
                              "only in an older format", "page"),
    "canonical_target_not_crawled": (
        "preferred address never reached",
        "{count} of {whole} pages point to a preferred address the crawl "
        "never reached", "page"),
    "canonical_target_unchecked": (
        "preferred address not checked",
        "{count} of {whole} pages point to a preferred address that was not "
        "checked", "page"),
    "canonical_target_non_200": (
        "preferred address is broken",
        "{count} of {whole} pages point to a preferred address that does not "
        "load", "page"),
    "canonical_chain": ("preferred address points on again",
                        "{count} of {whole} pages point to a page that itself "
                        "points somewhere else", "page"),
    "canonical_target_noindex": (
        "preferred address is hidden from search",
        "{count} of {whole} pages point to a page that is hidden from search",
        "page"),
    "hreflang_not_reciprocal": ("language versions do not agree",
                                "{count} of {whole} pages name a language "
                                "version that does not name them back",
                                "page"),
    "hreflang_target_non_200": ("language version is broken",
                                "{count} of {whole} pages name a language "
                                "version that does not load", "page"),
    "duplicate_title": ("same title on several pages",
                        "{count} groups of pages share one title", "group"),
    "duplicate_meta_description": (
        "same search description on several pages",
        "{count} groups of pages share one search description", "group"),
    "sitemap_noindex": ("hidden page listed in the sitemap",
                        "{count} of {whole} pages are listed in the sitemap "
                        "but hidden from search", "page"),
    "sitemap_off_canonical": (
        "sitemap lists a page that points elsewhere",
        "{count} of {whole} pages are listed in the sitemap but name another "
        "page as preferred", "page"),
    "orphan_page": ("no links to the page",
                    "{count} of {whole} pages have no links from any other "
                    "page on the site", "page"),
    "low_inlink_page": ("only one link to the page",
                        "{count} of {whole} pages have just one link from "
                        "elsewhere on the site", "page"),
    "broken_internal_link": ("links to pages that are gone",
                             "{count} addresses linked from the site no "
                             "longer load", "target"),
    "redirected_internal_link": ("links that bounce through a redirect",
                                 "{count} addresses linked from the site send "
                                 "visitors on to another address", "target"),
    "nofollow_internal_link": ("internal links marked not to follow",
                               "{count} of {whole} pages carry internal links "
                               "marked not to follow", "page"),
    "generic_anchor": ("vague link wording",
                       "{count} of {whole} pages are linked only by vague "
                       "wording such as click here", "page"),
    "external_link_broken": ("links to other websites that are gone",
                             "{count} links to other websites no longer load",
                             "target"),
    "duplicate_content": ("pages with the same text",
                          "{count} groups of pages carry the same text",
                          "group"),
    "near_duplicate_content": ("pages with almost the same text",
                               "{count} groups of pages carry almost the same "
                               "text", "group"),
    "gsc_impressions_on_noindex": (
        "page hidden from search still appears in search",
        "{count} of {whole} pages that appeared in search are hidden from "
        "search", "page"),
    "gsc_impressions_on_off_canonical": (
        "page that appears in search names another page as preferred",
        "{count} of {whole} pages that appeared in search name a different "
        "page as the preferred one", "page"),
    "gsc_sitemap_page_no_impressions": (
        "page in the sitemap never appeared in search",
        "{count} of {whole} pages listed in the sitemap were never shown in "
        "search results", "page"),
    "gsc_orphan_page_with_clicks": (
        "page with no links to it is earning clicks",
        "{count} of {whole} pages with no links from the site still bring "
        "visitors in from search", "page"),
    "gsc_query_cannibalised": (
        "one search answered by several pages",
        "{count} searches are answered by more than one page of the site",
        "group"),
    "http_to_https_redirect": ("insecure address does not redirect",
                               "the insecure address of the site does not send "
                               "visitors to the secure one", "site"),
    "hsts_missing": ("secure connection not enforced",
                     "the site does not tell browsers to always use a secure "
                     "connection", "site"),
    "csp_missing": ("no content security policy",
                    "the site does not send a content security policy",
                    "site"),
    "x_content_type_options_missing": (
        "file type protection header missing",
        "the site does not send the header that stops browsers guessing file "
        "types", "site"),
    "x_frame_options_missing": ("framing protection header missing",
                                "the site does not send the header that stops "
                                "other sites framing its pages", "site"),
    "performance_poor": ("slow page template",
                         "{count} measured page templates score poorly for "
                         "speed", "group"),
    "lcp_poor": ("main content loads slowly",
                 "{count} measured page templates take a long time to show "
                 "their main content", "group"),
    "cls_poor": ("layout moves while loading",
                 "{count} measured page templates move about while they load",
                 "group"),
    "inp_poor": ("slow to respond to taps",
                 "{count} measured page templates are slow to respond when "
                 "tapped", "group"),
    "performance_no_field_data": ("no real visitor speed data",
                                  "{count} measured page templates have no "
                                  "speed data from real visitors", "group"),
    "pagespeed_error": ("speed could not be measured",
                        "{count} measured page templates could not be "
                        "measured for speed", "group"),
}


# What the client's team does about each type, in one imperative sentence a
# marketing head can hand to a developer or an editor without translating it.
# Kept beside PAGE_ISSUE_META rather than inside it so the tuple keeps its
# shape; a registry test fails the build if a type is missing an action, so
# the two cannot drift apart. Group and target types are phrased at template
# level, because that is where the work happens.
PAGE_ISSUE_ACTION: Dict[str, str] = {
    "title_missing": "Write a page title for each of these pages.",
    "title_too_long":
        "Shorten these page titles so the important words come first.",
    "title_too_short": "Give these pages a fuller page title.",
    "meta_description_missing":
        "Write a search description for each of these pages.",
    "meta_description_too_long":
        "Shorten these search descriptions so they are not cut short.",
    "h1_missing": "Add a main heading to each of these pages.",
    "h1_multiple": "Leave one main heading per page in this template.",
    "noindex_page":
        "Confirm these pages are meant to be hidden from search, and remove "
        "the instruction from any that are not.",
    "nofollow_page":
        "Confirm these pages are meant to stop search engines following "
        "their links, and remove the instruction from any that are not.",
    "canonical_missing": "Name a preferred address on each of these pages.",
    "canonical_off_page":
        "Point the preferred address at the page it sits on.",
    "canonical_consolidates_variant":
        "No action needed, this is the site working as it should.",
    "canonical_not_absolute":
        "Write the preferred address as a full web address.",
    "viewport_missing": "Add the mobile layout setting to this template.",
    "html_lang_missing": "Declare the page language in this template.",
    "images_missing_alt":
        "Add a short description to every image in this template.",
    "thin_page":
        "Add useful text to these pages, or fold them into a stronger page.",
    "mixed_content":
        "Load every file on these pages over a secure connection.",
    "schema_missing": "Add structured data to this template.",
    "schema_invalid_json":
        "Repair the structured data in this template so it can be read.",
    "schema_missing_property":
        "Fill in the required fields missing from this template's structured "
        "data.",
    "schema_microdata_only":
        "Move the structured data in this template into the current format.",
    "canonical_target_not_crawled":
        "Check where these preferred addresses point, and correct any that "
        "are wrong.",
    "canonical_target_unchecked":
        "Check by hand that these preferred addresses load.",
    "canonical_target_non_200":
        "Point these preferred addresses at a page that loads.",
    "canonical_chain":
        "Point these preferred addresses straight at the final page.",
    "canonical_target_noindex":
        "Point these preferred addresses at a page search engines may list.",
    "hreflang_not_reciprocal":
        "Make each language version name the others back.",
    "hreflang_target_non_200":
        "Point these language versions at addresses that load.",
    "duplicate_title": "Give each of these pages its own page title.",
    "duplicate_meta_description":
        "Give each of these pages its own search description.",
    "sitemap_noindex":
        "Take the pages hidden from search out of the sitemap.",
    "sitemap_off_canonical":
        "List the preferred page in the sitemap instead of these addresses.",
    "orphan_page":
        "Link to these pages from the relevant city or category pages.",
    "low_inlink_page":
        "Add a second route into these pages from related pages.",
    "broken_internal_link":
        "Correct or remove the links in the template that point at these "
        "addresses.",
    "redirected_internal_link":
        "Point these internal links straight at the address they end up on.",
    "nofollow_internal_link":
        "Let search engines follow these internal links.",
    "generic_anchor":
        "Replace wording such as click here with words describing the page.",
    "external_link_broken":
        "Repair or remove these links to other websites.",
    "duplicate_content":
        "Keep one page for this text and point the others at it.",
    "near_duplicate_content":
        "Give these pages their own text, local detail and offers.",
    "performance_poor":
        "Speed up this template, starting with images and third party "
        "scripts.",
    "lcp_poor": "Make the main content of this template appear sooner.",
    "cls_poor":
        "Reserve space for images and adverts so this template stops moving "
        "as it loads.",
    "inp_poor":
        "Reduce the scripts that run when someone taps on this template.",
    "performance_no_field_data":
        "No action needed, this template has too few visitors for speed data "
        "from real people.",
    "pagespeed_error":
        "No action needed, speed could not be measured for this template.",
    "gsc_impressions_on_noindex":
        "Decide whether these pages should be found: remove the hidden from "
        "search instruction, or accept that the impressions will stop.",
    "gsc_impressions_on_off_canonical":
        "Point the preferred address at the page people are actually "
        "landing on, or move the content to the preferred page.",
    "gsc_sitemap_page_no_impressions":
        "Give these pages a reason to be found, or take them out of the "
        "sitemap.",
    "gsc_orphan_page_with_clicks":
        "Link to these pages from the relevant section: they earn visitors "
        "with no help from the site at all.",
    "gsc_query_cannibalised":
        "Choose one page for each of these searches and point the others at "
        "it, so the two stop competing.",
    "http_to_https_redirect":
        "Ask the developers to send the insecure address on to the secure "
        "one.",
    "hsts_missing":
        "Ask the developers to switch on the secure connection setting.",
    "x_content_type_options_missing":
        "Ask the developers to send the header that stops browsers guessing "
        "file types.",
    "x_frame_options_missing":
        "Ask the developers to send the header that stops other sites "
        "framing your pages.",
    "csp_missing":
        "Ask the developers to send a content security policy.",
}


def action_of(issue_type: str) -> str:
    """The one thing to do about this type, in plain words."""
    return PAGE_ISSUE_ACTION.get(issue_type, "")


def label_of(issue_type: str) -> str:
    """The plain name for a client. Never the identifier."""
    entry = PAGE_ISSUE_META.get(issue_type)
    return entry[0] if entry else issue_type.replace("_", " ")


def plain_finding(issue_type: str, count: int, whole: int = 0) -> str:
    """One sentence stating the finding, with the numbers filled in."""
    entry = PAGE_ISSUE_META.get(issue_type)
    template = entry[1] if entry else "{count} of {whole} pages are affected"
    try:
        return template.format(count=count, whole=whole)
    except (KeyError, IndexError, ValueError):
        return f"{count} of {whole} pages are affected"


def unit_of(issue_type: str) -> str:
    """What the count counts: page, target, group or site."""
    entry = PAGE_ISSUE_META.get(issue_type)
    return entry[2] if entry else "page"

# Robots user-agent tokens worth reporting when a site names them.
AI_CRAWLER_TOKENS = (
    "gptbot", "chatgpt-user", "oai-searchbot", "claudebot", "anthropic-ai",
    "claude-web", "perplexitybot", "google-extended", "ccbot", "bytespider",
    "applebot-extended", "meta-externalagent", "amazonbot", "youbot",
    "diffbot", "cohere-ai", "imagesiftbot", "timpibot",
)


# robots.txt directives as they are conventionally written.
_DIRECTIVE_CASING = {
    "disallow": "Disallow",
    "allow": "Allow",
    "crawl-delay": "Crawl-delay",
}


@dataclass
class CrawlIssue:
    issue_type: str
    url: str
    referrer: Optional[str] = None
    detail: str = ""

    @property
    def crawler_effect(self) -> str:
        return CRAWLER_EFFECT[self.issue_type]


class IssueLog:
    """Streams issue rows to CSV and keeps only the counts in memory.

    Safe to call from crawl workers: every write takes the lock.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._counts: Counter = Counter()
        self._lock = threading.Lock()
        self._handle = None
        self._writer = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._handle = open(path, "w", newline="", encoding="utf-8-sig")
            self._writer = csv.DictWriter(self._handle, fieldnames=ISSUE_COLUMNS)
            self._writer.writeheader()
            self._handle.flush()

    def add(self, issue_type: str, url: str, referrer: Optional[str] = None,
            detail: str = "") -> None:
        if issue_type not in CRAWLER_EFFECT:
            raise KeyError(f"unknown issue type {issue_type!r}")
        with self._lock:
            self._counts[issue_type] += 1
            if self._writer is not None:
                self._writer.writerow({
                    "issue_type": issue_type,
                    "url": url,
                    "referrer": referrer or "",
                    "detail": detail,
                    "crawler_effect": CRAWLER_EFFECT[issue_type],
                })
                self._handle.flush()

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
                self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def ai_crawler_groups(robots_text: str):
    """Robots groups naming an AI crawler, as (user_agent, [rule lines]).

    Read straight from the raw file rather than the parsed rules, because the
    interesting part is what the site says to crawlers that are not us.
    """
    groups = []
    current_agents = []
    current_rules = []
    last_was_agent = False

    def flush():
        nonlocal current_agents, current_rules
        for agent in current_agents:
            if any(token in agent.lower() for token in AI_CRAWLER_TOKENS):
                groups.append((agent, list(current_rules)))
        current_agents, current_rules = [], []

    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name == "user-agent":
            if not last_was_agent:
                flush()
            current_agents.append(value)
            last_was_agent = True
            continue
        last_was_agent = False
        if name in _DIRECTIVE_CASING:
            current_rules.append(f"{_DIRECTIVE_CASING[name]}: {value}")
    flush()
    return groups


class PageIssueLog:
    """Streams page-level findings to CSV, counting by type and by severity.

    Same contract as IssueLog: unknown types raise rather than inventing a
    category, and every write takes the lock because crawl workers call it.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._counts: Counter = Counter()
        self._severity_counts: Counter = Counter()
        self._lock = threading.Lock()
        self._handle = None
        self._writer = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._handle = open(path, "w", newline="", encoding="utf-8-sig")
            self._writer = csv.DictWriter(self._handle,
                                          fieldnames=PAGE_ISSUE_COLUMNS)
            self._writer.writeheader()
            self._handle.flush()

    def add(self, issue_type: str, url: str, final_url: Optional[str] = None,
            detail: str = "", site_level: bool = False) -> None:
        severity = PAGE_ISSUE_SEVERITY.get(issue_type)
        if severity is None:
            raise KeyError(f"unknown page issue type {issue_type!r}")
        with self._lock:
            self._counts[issue_type] += 1
            self._severity_counts[severity] += 1
            if self._writer is not None:
                self._writer.writerow({
                    "issue_type": issue_type,
                    "severity": severity,
                    "url": url,
                    "final_url": final_url or "",
                    "detail": detail,
                    "site_level": site_level,
                })
                # Flushed per row so a crash keeps everything already found.
                self._handle.flush()

    def add_many(self, findings, url: str, final_url: Optional[str] = None,
                 site_level: bool = False) -> int:
        """Record a list of (issue_type, detail) pairs for one page."""
        for issue_type, detail in findings:
            self.add(issue_type, url, final_url, detail, site_level)
        return len(findings)

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))

    def severity_counts(self) -> Dict[str, int]:
        with self._lock:
            return {level: self._severity_counts[level]
                    for level in ("high", "medium", "low", "info",
                                  "unmeasured")
                    if self._severity_counts[level]}

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
                self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
