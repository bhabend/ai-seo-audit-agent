"""Single source of truth for audit run settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

DEFAULT_USER_AGENT = (
    "seo-audit-bot/0.1 (+https://github.com/seo-audit; python-requests)"
)


@dataclass
class AuditConfig:
    """Everything the crawl layer needs, resolved in one place.

    `domain` is required and may be given as a bare host ("example.com") or a
    full URL ("https://example.com/path"); it is normalised to a scheme + host.
    """

    domain: str
    sitemap_url: Optional[str] = None
    max_pages: int = 2000
    max_depth: int = 5
    crawl_delay: float = 1.0
    timeout: int = 20
    workers: int = 4
    sitemap_reserve: float = 0.25
    keep_html: bool = False
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots: bool = True
    include_subdomains: bool = False
    render: bool = False

    def __post_init__(self) -> None:
        if not self.domain or not self.domain.strip():
            raise ValueError("domain is required")
        if self.render:
            raise NotImplementedError(
                "render=True is not supported yet: JavaScript rendering "
                "(Playwright) arrives in a later session. Run with render=False "
                "and check the text_chars column to see which pages need it."
            )

        if not 0.0 <= self.sitemap_reserve <= 0.5:
            raise ValueError(
                "sitemap_reserve must be between 0.0 and 0.5, got "
                f"{self.sitemap_reserve}")
        if self.workers < 1:
            raise ValueError(f"workers must be at least 1, got {self.workers}")

        raw = self.domain.strip()
        if "://" not in raw:
            raw = "https://" + raw
        parsed = urlparse(raw)
        if not parsed.netloc:
            raise ValueError(f"could not parse a host out of domain={self.domain!r}")

        self.scheme = parsed.scheme.lower() or "https"
        self.host = parsed.netloc.lower()
        # Kept for the record: adopt_host() may replace `host` once the
        # homepage tells us which hostname the site actually serves.
        self.configured_host = self.host
        self.domain = f"{self.scheme}://{self.host}"

    def adopt_host(self, host: str) -> None:
        """Switch the canonical host after a same-site homepage redirect."""
        self.host = host.lower()
        self.domain = f"{self.scheme}://{self.host}"

    @property
    def host_was_adopted(self) -> bool:
        return self.host != self.configured_host

    @property
    def start_url(self) -> str:
        """Homepage the crawl seeds from."""
        return self.domain + "/"

    def url_for(self, path: str) -> str:
        """Absolute URL for a site-root-relative path such as /robots.txt."""
        return self.domain + path
