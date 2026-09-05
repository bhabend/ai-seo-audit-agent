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
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from .urlnorm import normalize

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
    redirect_loop: bool = False
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def redirect_hops(self) -> int:
        return len(self.redirect_chain)

    def header(self, name: str) -> Optional[str]:
        """Case-insensitive response header lookup (HTTP headers are not case
        sensitive, and servers disagree about how to spell X-Robots-Tag)."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None

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
    """HTTP access for the whole run, one Session per worker thread.

    A requests.Session is not safe to drive from several threads at once, so
    each thread gets its own and the crawl delay is enforced per thread: every
    worker waits `crawl_delay` between its own requests, which is what a
    politeness delay is supposed to mean once there is more than one worker.
    """

    REQUEST_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en",
    }

    def __init__(self, config, session: Optional[requests.Session] = None,
                 max_retries: int = 2, backoff: float = 1.0,
                 delay: Optional[float] = None):
        self.config = config
        self.max_retries = max_retries
        self.backoff = backoff
        self.delay = config.crawl_delay if delay is None else delay
        self._shared_session = session  # injected by tests; shared on purpose
        self._local = threading.local()
        self._sessions: List[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(self.REQUEST_HEADERS)
        session.headers["User-Agent"] = self.config.user_agent
        with self._sessions_lock:
            self._sessions.append(session)
        return session

    @property
    def session(self) -> requests.Session:
        """This thread's Session, created on first use."""
        if self._shared_session is not None:
            return self._shared_session
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._new_session()
            self._local.session = session
        return session

    def set_delay(self, delay: float) -> None:
        """Used once robots.txt has been read and may have raised the delay."""
        self.delay = delay

    def _wait_turn(self) -> None:
        """Hold this worker back until its own delay has elapsed."""
        if self.delay <= 0:
            return
        last = getattr(self._local, "last_request", None)
        if last is not None:
            remaining = self.delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._local.last_request = time.monotonic()

    def _request(self, url: str, method: str = "GET"):
        """One request with backoff on transport errors only, never on 4xx/5xx."""
        last_error = None
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            try:
                return self.session.request(
                    method,
                    url,
                    timeout=self.config.timeout,
                    allow_redirects=True,
                ), None
            except requests.exceptions.TooManyRedirects as exc:
                return None, f"TooManyRedirects: {exc}"
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
            result.redirect_loop = bool(error and "TooManyRedirects" in error)
            return result

        result.status_code = response.status_code
        # Normalised so it joins against the `url` column: no :443, no
        # fragment, no tracking params. The raw hops are kept below.
        result.final_url = normalize(response.url) or response.url
        result.redirect_chain = [(h.url, h.status_code) for h in response.history]
        result.content_type = response.headers.get("Content-Type")
        result.headers = dict(response.headers)

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

    def head(self, url: str) -> FetchResult:
        """Status-only check for the sitemap sweep. No body, no HTML, no store.

        Falls back to GET when the server rejects HEAD (405/501) or answers
        without saying what it served.
        """
        result = FetchResult(url=url)
        started = time.perf_counter()
        response, error = self._request(url, method="HEAD")

        needs_get = (
            response is None
            or response.status_code in (405, 501)
            or not response.headers.get("Content-Type")
        )
        if needs_get and error is None:
            response, error = self._request(url, method="GET")

        result.response_time_ms = int((time.perf_counter() - started) * 1000)
        if response is None:
            result.error = error or "unknown request failure"
            result.redirect_loop = bool(error and "TooManyRedirects" in error)
            return result

        result.status_code = response.status_code
        result.final_url = normalize(response.url) or response.url
        result.redirect_chain = [(h.url, h.status_code) for h in response.history]
        result.content_type = response.headers.get("Content-Type")
        result.headers = dict(response.headers)
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
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()
