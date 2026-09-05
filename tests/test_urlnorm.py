"""URL normalisation and same-site tests (lesson 4 from the v0 review)."""

from seo_audit.urlnorm import (extract_links, is_same_site, make_soup,
                               normalize)


def test_fragment_is_stripped():
    assert normalize("https://example.com/a#section") == "https://example.com/a"


def test_tracking_params_are_stripped_but_real_ones_kept():
    url = "https://example.com/a?utm_source=x&utm_medium=y&gclid=z&page=2&fbclid=q"
    assert normalize(url) == "https://example.com/a?page=2"


def test_dot_dot_segments_are_resolved():
    assert normalize("../c", base="https://example.com/a/b/") == \
        "https://example.com/a/c"
    assert normalize("https://example.com/a/b/../../d") == "https://example.com/d"


def test_scheme_and_host_are_lowercased_and_default_port_dropped():
    assert normalize("HTTPS://Example.COM:443/Path") == "https://example.com/Path"
    assert normalize("http://example.com:80/") == "http://example.com/"
    # A non-default port is meaningful and is kept.
    assert normalize("https://example.com:8443/x") == "https://example.com:8443/x"


def test_trailing_slash_is_preserved_as_written():
    assert normalize("https://example.com/a") == "https://example.com/a"
    assert normalize("https://example.com/a/") == "https://example.com/a/"
    assert normalize("https://example.com") == "https://example.com/"


def test_non_fetchable_schemes_return_none():
    for url in ("mailto:hi@example.com", "tel:+123", "javascript:void(0)",
                "#anchor", ""):
        assert normalize(url) is None


def test_www_and_non_www_are_the_same_site():
    assert is_same_site("https://www.example.com/a", "example.com")
    assert is_same_site("https://example.com/a", "www.example.com")


def test_other_subdomains_excluded_by_default_included_on_request():
    url = "https://blog.example.com/post"
    assert not is_same_site(url, "www.example.com")
    assert is_same_site(url, "www.example.com", include_subdomains=True)


def test_lookalike_domain_is_not_the_same_site():
    # The v0 `domain in href` substring test wrongly accepted both of these.
    assert not is_same_site("https://notexample.com/a", "example.com")
    assert not is_same_site("https://evil.com/?ref=example.com", "example.com")


def test_protocol_relative_href_takes_the_base_scheme():
    assert normalize("//example.com/a", base="https://example.com/") == \
        "https://example.com/a"
    assert normalize("//other.com/a", base="https://example.com/") == \
        "https://other.com/a"


def test_extract_links_handles_relative_absolute_and_skips_non_http():
    html = """
      <a href="/about">a</a>
      <a href="contact/">b</a>
      <a href="https://example.com/pricing">c</a>
      <a href="//example.com/proto">d</a>
      <a href="mailto:hi@example.com">e</a>
      <a href="tel:+441234">f</a>
      <a href="javascript:void(0)">g</a>
      <a href="#top">h</a>
      <a href="/about#faq">i</a>
    """
    # extract_links now takes the shared soup, not raw HTML.
    links = list(extract_links(make_soup(html), "https://example.com/docs/"))
    assert links == [
        "https://example.com/about",
        "https://example.com/docs/contact/",
        "https://example.com/pricing",
        "https://example.com/proto",
    ]
