"""Field extraction from the shared soup."""

from seo_audit.parse import is_document_url, parse_page
from seo_audit.urlnorm import extract_links, make_soup

BASE = "https://example.com"
PAGE = "https://example.com/page/"

FULL_HTML = """
<!doctype html>
<html lang="en-GB">
  <head>
    <title>  A perfectly reasonable page title  </title>
    <meta name="description" content="What the page is about.">
    <meta name="viewport" content="width=device-width">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="/page/">
    <link rel="alternate" hreflang="fr" href="/fr/page/">
    <link rel="alternate" hreflang="en-GB" href="/page/">
  </head>
  <body>
    <h1>The heading</h1>
    <h2>One</h2><h2>Two</h2>
    <h3>Three</h3>
    <p>Some body copy that a reader would actually see on the page.</p>
    <img src="/a.png" alt="described">
    <img src="/b.png" alt="">
    <img src="/c.png">
    <a href="/internal">in</a>
    <a href="/other" rel="nofollow">nofollow internal</a>
    <a href="https://elsewhere.com/x">out</a>
    <script>var hidden = "this text is not visible";</script>
  </body>
</html>
"""


def fields_for(html, final_url=PAGE, host="example.com"):
    soup = make_soup(html)
    links = extract_links(soup, final_url)
    return parse_page(soup, final_url, links, host)


def test_every_field_comes_off_one_pass():
    f = fields_for(FULL_HTML)

    assert f.title == "A perfectly reasonable page title"
    assert f.title_length == len("A perfectly reasonable page title")
    assert f.meta_description == "What the page is about."
    assert f.meta_description_length == 23
    assert f.meta_robots == "index, follow"
    assert f.viewport is True
    assert f.html_lang == "en-GB"
    assert f.h1 == ["The heading"]
    assert (f.h2_count, f.h3_count) == (2, 1)
    assert f.image_count == 3
    assert f.images_missing_alt == 1   # no alt attribute at all
    assert f.images_empty_alt == 1     # alt="" is a decorative marker
    assert f.internal_links == 2
    assert f.external_links == 1
    assert f.nofollow_internal_links == 1


def test_canonical_and_hreflang_resolve_against_final_url():
    f = fields_for(FULL_HTML)
    assert f.canonical == PAGE
    assert f.canonical_raw == "/page/"
    assert f.hreflang == [("fr", BASE + "/fr/page/"), ("en-GB", PAGE)]


def test_word_count_ignores_script_text():
    f = fields_for(FULL_HTML)
    # "this text is not visible" lives in a <script> and must not count.
    assert 0 < f.word_count < 30


def test_robots_directives_come_from_meta_and_header():
    html = '<html><head><meta name="robots" content="noindex,nofollow">' \
           '</head><body>x</body></html>'
    f = fields_for(html)
    assert f.is_noindex is True
    assert f.is_nofollow is True

    soup = make_soup("<html><body>x</body></html>")
    from seo_audit.parse import parse_page as pp
    f2 = pp(soup, PAGE, [], "example.com",
            response_headers={"X-Robots-Tag": "noindex"})
    assert f2.x_robots_tag == "noindex"
    assert f2.is_noindex is True


def test_x_robots_tag_header_lookup_is_case_insensitive():
    soup = make_soup("<html><body>x</body></html>")
    f = parse_page(soup, PAGE, [], "example.com",
                   response_headers={"x-robots-tag": "googlebot: noindex"})
    assert f.is_noindex is True


def test_mixed_content_is_found_only_on_https_pages():
    html = """<html><body>
      <script src="http://cdn.example.com/a.js"></script>
      <img src="http://img.example.com/b.png">
      <link rel="stylesheet" href="http://css.example.com/c.css">
      <script src="https://secure.example.com/ok.js"></script>
    </body></html>"""
    assert len(fields_for(html).mixed_content) == 3
    # The same page served over http has nothing to mix.
    assert fields_for(html, final_url="http://example.com/p") .mixed_content == []


def test_document_urls_are_recognised_by_path_not_query():
    assert is_document_url("https://x.com/a/report.pdf")
    assert is_document_url("https://x.com/a/sheet.XLSX")
    assert is_document_url("https://x.com/deck.pptx?v=2")
    assert not is_document_url("https://x.com/page?file=report.pdf")
    assert not is_document_url("https://x.com/page/")


def test_missing_fields_are_none_not_crashes():
    f = fields_for("<html><body><p>bare</p></body></html>")
    assert f.title is None
    assert f.meta_description is None
    assert f.canonical is None
    assert f.html_lang is None
    assert f.viewport is False
    assert f.h1 == []
