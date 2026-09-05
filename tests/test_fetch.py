"""Fetch layer: decoding, redirect history, non-HTML (lessons 1-3)."""

import requests
import requests_mock

from conftest import BASE, make_config
from seo_audit.fetch import Fetcher


def test_latin1_page_with_declared_charset_decodes_cleanly():
    config = make_config()
    body = "<html><body><p>Le café coûte cher</p></body></html>"
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/fr", content=body.encode("latin-1"),
                 headers={"Content-Type": "text/html; charset=iso-8859-1"})
        result = Fetcher(config).fetch(BASE + "/fr")

    assert "café coûte" in result.html
    assert "Ã" not in result.html  # no mojibake


def test_utf8_page_without_declared_charset_decodes_cleanly():
    """The v0 bug: requests guesses ISO-8859-1 and turns U+2019 into a-tilde."""
    config = make_config()
    body = "<html><body><p>Wework’s café</p></body></html>"
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/x", content=body.encode("utf-8"),
                 headers={"Content-Type": "text/html"})
        result = Fetcher(config).fetch(BASE + "/x")

    assert "Wework’s café" in result.html
    assert "â€" not in result.html


def test_meta_charset_in_body_is_used_when_header_is_silent():
    config = make_config()
    body = ('<html><head><meta charset="iso-8859-1"></head>'
            '<body><p>naïve</p></body></html>')
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/m", content=body.encode("latin-1"),
                 headers={"Content-Type": "text/html"})
        result = Fetcher(config).fetch(BASE + "/m")
    assert "naïve" in result.html


def test_redirect_chain_comes_from_the_single_fetch():
    config = make_config()
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/old", status_code=301,
                 headers={"Location": BASE + "/mid"})
        mock.get(BASE + "/mid", status_code=302,
                 headers={"Location": BASE + "/new"})
        mock.get(BASE + "/new", text="<html><body>arrived</body></html>",
                 headers={"Content-Type": "text/html; charset=utf-8"})
        fetcher = Fetcher(config)
        result = fetcher.fetch(BASE + "/old")
        request_count = len(mock.request_history)

    assert result.status_code == 200
    assert result.final_url == BASE + "/new"
    assert result.redirect_chain == [(BASE + "/old", 301), (BASE + "/mid", 302)]
    assert result.redirect_hops == 2
    # Three requests for three hops: no second pass to replay the redirects.
    assert request_count == 3


def test_non_html_is_recorded_but_not_parsed():
    config = make_config()
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/doc.pdf", content=b"%PDF-1.4 binary junk",
                 headers={"Content-Type": "application/pdf"})
        result = Fetcher(config).fetch(BASE + "/doc.pdf")

    assert result.status_code == 200
    assert result.content_type == "application/pdf"
    assert result.html is None
    assert result.text_chars is None
    assert result.is_html is False


def test_text_chars_ignores_script_and_style_blocks():
    config = make_config()
    shell = ("<html><head><style>body{color:red}</style></head><body>"
             "<div id='root'></div>"
             "<script>var big='" + ("x" * 5000) + "';</script>"
             "</body></html>")
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/spa", text=shell,
                 headers={"Content-Type": "text/html; charset=utf-8"})
        result = Fetcher(config).fetch(BASE + "/spa")

    # 5KB of JavaScript must not be mistaken for 5KB of page copy.
    assert result.text_chars < 100


def test_connection_error_is_retried_then_reported():
    config = make_config()
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/down", exc=requests.exceptions.ConnectionError)
        fetcher = Fetcher(config, max_retries=2, backoff=0.0)
        result = fetcher.fetch(BASE + "/down")
        attempts = len(mock.request_history)

    assert result.error is not None
    assert result.status_code is None
    assert attempts == 3  # initial try plus two retries


def test_server_error_is_not_retried():
    config = make_config()
    with requests_mock.Mocker() as mock:
        mock.get(BASE + "/boom", status_code=500,
                 headers={"Content-Type": "text/html"})
        fetcher = Fetcher(config, max_retries=2, backoff=0.0)
        result = fetcher.fetch(BASE + "/boom")
        attempts = len(mock.request_history)

    assert result.status_code == 500
    assert attempts == 1
