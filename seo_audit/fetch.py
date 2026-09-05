"""HTTP layer: one session, one request per URL, careful decoding.

Lessons from v0 designed out here:
  * every URL was fetched twice (once for content, once to replay redirects) --
    the redirect chain now comes off the single response's history;
  * `res.text` produced mojibake because requests falls back to ISO-8859-1 for
    text/* with no declared charset -- we decode from bytes ourselves;
  * JS-rendered pages looked like real pages -- `text_chars` makes the
    emptiness visible in the output instead of hiding it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

_CHARSET_HEADER_RE = re.compile(r"charset=([\w\-]+)", re.I)
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w\-]+)""", re.I)
_SCRIPTISH_RE = re.compile(
    rb"<(script|style|noscript|template)\b.*?</\1>", re.I | re.S
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class FetchResult:
    """Everything one HTTP request tells us, and nothing that needs a second."""

    url: str
    final_url: Optional[str] = None
    status_code: Optional[int] = None
    redirect_chain: List[Tuple[str, int]] = field(default_factory=list)
    response_time_ms: Optional[int] = None
    content_type: Optional[str] = None
    html: Optional[str] = None
    text_chars: Optional[int] = None
    error: Optional[str] = None
    content: Optional[bytes] = None

    @property
    def redirect_hops(self) -> int:
        return len(self.redirect_chain)

    @property
    def is_html(self) -> bool:
        return self.html is not None


def declared_charset(content_type: Optional[str], body: Optional[bytes]) -> Optional[str]:
    """Charset the response declares, header first then a <meta> in the body."""
    if content_type:
        match = _CHARSET_HEADER_RE.search(content_type)
        if match:
            return match.group(1).strip().lower()
    if body:
        match = _META_CHARSET_RE.search(body[:4096])
        if match:
            try:
                return match.group(1).decode("ascii").strip().lower()
            except UnicodeDecodeError:
                return None
    return None


def decode_body(response: requests.Response) -> str:
    """Decode response bytes using the declared charset, then a sniffed one.

    Never touches `response.text`, which guesses ISO-8859-1 and mangles UTF-8.
    """
    body = response.content
    candidates: List[str] = []

    declared = declared_charset(response.headers.get("Content-Type"), body)
    if declared:
        candidates.append(declared)
    try:
        apparent = response.apparent_encoding
    except Exception:  # charset detector failure is not fatal
        apparent = None
    if apparent:
        candidates.append(apparent.lower())
    candidates.append("utf-8")

    for encoding in candidates:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def visible_text_chars(body: bytes, decoded: str) -> int:
    """Length of visible text once script/style/noscript blocks are removed.

    A JS-rendered shell scores in the low hundreds here; a real page does not.
    """
    stripped_bytes = _SCRIPTISH_RE.sub(b" ", body)
    if len(stripped_bytes) != len(body):
        # Re-decode the stripped bytes with whatever worked for the full body.
        try:
            text = stripped_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = stripped_bytes.decode("utf-8", errors="replace")
    else:
        text = decoded
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return len(text)


def is_html_content_type(content_type: Optional[str]) -> bool:
    if not content_type:
        return False
    base = content_type.split(";", 1)[0].strip().lower()
    return base in HTML_CONTENT_TYPES


class Fetcher:
    """Wraps a single requests.Session for the whole run."""

    def __init__(self, config, session: Optional[requests.Session] = None,
                 max_retries: int = 2, backoff: float = 1.0):
        self.config = config
        self.max_retries = max_retries
        self.backoff = backoff
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en",
        })

    def _request(self, url: str, stream: bool = False):
        """One GET with backoff on transport errors only, never on 4xx/5xx."""
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.session.get(
                    url,
                    timeout=self.config.timeout,
                    allow_redirects=True,
                ), None
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    time.sleep(self.backoff * (2 ** attempt))
            except requests.exceptions.RequestException as exc:
                return None, f"{type(exc).__name__}: {exc}"
        return None, last_error

    def fetch(self, url: str) -> FetchResult:
        """Fetch one URL. HTML is decoded; anything else is recorded, not parsed."""
        result = FetchResult(url=url)
        started = time.perf_counter()
        response, error = self._request(url)
        result.response_time_ms = int((time.perf_counter() - started) * 1000)

        if response is None:
            result.error = error or "unknown request failure"
            return result

        result.status_code = response.status_code
        result.final_url = response.url
        result.redirect_chain = [(h.url, h.status_code) for h in response.history]
        result.content_type = response.headers.get("Content-Type")

        if not is_html_content_type(result.content_type):
            # Non-HTML (pdf, image, xml): status recorded, body not parsed.
            return result

        try:
            body = response.content
            result.content = body
            result.html = decode_body(response)
            result.text_chars = visible_text_chars(body, result.html)
        except Exception as exc:  # decoding should never kill the crawl
            result.error = f"decode failed: {type(exc).__name__}: {exc}"
        return result

    def fetch_bytes(self, url: str) -> Tuple[Optional[int], Optional[bytes],
                                             Optional[str], Optional[str]]:
        """Raw fetch for robots.txt and sitemaps.

        Returns (status_code, body, content_type, error).
        """
        response, error = self._request(url)
        if response is None:
            return None, None, None, error or "unknown request failure"
        return (response.status_code, response.content,
                response.headers.get("Content-Type"), None)

    def close(self) -> None:
        self.session.close()
