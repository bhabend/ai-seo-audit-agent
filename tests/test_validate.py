"""One fixture per check. Everything is keyed on final_url."""

from conftest import BASE, crawl_site, html_page

GOOD_HEAD = """
    <title>A perfectly reasonable page title here</title>
    <meta name="description" content="A description of a sensible length that
    says what the page is for without running past the limit.">
    <meta name="viewport" content="width=device-width">
    <link rel="canonical" href="{canonical}">
"""

BODY_WORDS = " ".join(["content"] * 250)


def page(canonical, head="", body=None, lang=' lang="en"', links=()):
    """A page that passes every per-page check unless told otherwise."""
    anchors = "".join(f'<a href="{h}">l</a>' for h in links)
    return (
        f"<!doctype html><html{lang}><head>"
        + GOOD_HEAD.format(canonical=canonical) + head +
        f"</head><body><h1>Heading</h1><p>{body or BODY_WORDS}</p>"
        f'<script type="application/ld+json">'
        f'{{"@type": "WebPage", "name": "n"}}</script>'
        f"{anchors}</body></html>"
    )


def test_a_clean_page_raises_nothing(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": page(BASE + "/")})
    per_page = {i["issue_type"] for i in run.page_issues
                if i["site_level"] != "True"}
    assert per_page == set()


# --- lesson 6: a redirected page is judged on the URL that answered ---------

def test_redirected_page_whose_canonical_matches_final_url_is_clean(tmp_path):
    """The v0-shaped trap: keying on the requested URL would fire here."""
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", links=["/moved"]),
        BASE + "/moved": {"status_code": 301,
                          "headers": {"Location": BASE + "/moved/"}},
        BASE + "/moved/": page(BASE + "/moved/"),
    })

    rows = run.pages_by_url()
    moved = rows[BASE + "/moved/"]
    # url is what was asked for; final_url is what answered.
    assert moved["url"] == BASE + "/moved"
    assert moved["final_url"] == BASE + "/moved/"
    assert moved["canonical"] == BASE + "/moved/"
    assert moved["canonical_is_self"] == "True"
    assert run.page_issues_of("canonical_off_page") == []


def test_canonical_pointing_elsewhere_is_reported(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", links=["/dup"]),
        BASE + "/dup": page(BASE + "/"),
    })
    off = run.page_issues_of("canonical_off_page")
    assert [i["final_url"] for i in off] == [BASE + "/dup"]
    assert BASE + "/" in off[0]["detail"]


def test_relative_canonical_is_reported_as_not_absolute(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": page("/")})
    assert [i["final_url"] for i in
            run.page_issues_of("canonical_not_absolute")] == [BASE + "/"]


def test_canonical_chain_is_reported(tmp_path):
    # All three must be crawled: a chain is only visible once the middle of
    # it has been seen.
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", links=["/a", "/b", "/c"]),
        BASE + "/a": page(BASE + "/b"),
        BASE + "/b": page(BASE + "/c"),
        BASE + "/c": page(BASE + "/c"),
    })
    chain = run.page_issues_of("canonical_chain")
    assert [i["final_url"] for i in chain] == [BASE + "/a"]
    assert BASE + "/c" in chain[0]["detail"]


def test_canonical_target_noindex_is_reported(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", links=["/a", "/hidden"]),
        BASE + "/a": page(BASE + "/hidden"),
        BASE + "/hidden": page(BASE + "/hidden",
                               head='<meta name="robots" content="noindex">'),
    })
    assert [i["final_url"] for i in
            run.page_issues_of("canonical_target_noindex")] == [BASE + "/a"]


def test_canonical_target_outside_the_crawl_is_head_checked(tmp_path):
    def extra(mock):
        mock.get("https://example.com/away", status_code=404,
                 headers={"Content-Type": "text/html"}, text="no")
        mock.head("https://example.com/away", status_code=404,
                  headers={"Content-Type": "text/html"})

    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/away"),
    }, extra=extra, max_pages=1)

    assert [i["final_url"] for i in
            run.page_issues_of("canonical_target_non_200")] == [BASE + "/"]


def test_canonical_check_limit_marks_the_rest_unchecked(tmp_path):
    """Past the limit the tool says 'not checked', it does not guess."""
    pages = {BASE + "/": page(BASE + "/", links=["/a", "/b"])}
    pages[BASE + "/a"] = page(BASE + "/away-a")
    pages[BASE + "/b"] = page(BASE + "/away-b")

    def extra(mock):
        for name in ("away-a", "away-b"):
            mock.head(f"{BASE}/{name}", status_code=200,
                      headers={"Content-Type": "text/html"})
            mock.get(f"{BASE}/{name}", text=page(f"{BASE}/{name}"),
                     headers={"Content-Type": "text/html"})

    run = crawl_site(tmp_path, pages, extra=extra, canonical_check_limit=1)
    unchecked = run.page_issues_of("canonical_target_unchecked")
    assert len(unchecked) == 1
    assert "check limit of 1" in unchecked[0]["detail"]


# --- per-page checks --------------------------------------------------------

def test_title_and_description_length_checks(tmp_path):
    long_title = "x" * 70
    run = crawl_site(tmp_path, {
        BASE + "/": f'<html lang="en"><head><title>{long_title}</title>'
                    f'<meta name="description" content="{"d" * 200}">'
                    f'<meta name="viewport" content="w">'
                    f'<link rel="canonical" href="{BASE}/">'
                    f"</head><body><h1>h</h1><p>{BODY_WORDS}</p></body></html>",
    })
    types = run.page_issue_types()
    assert "title_too_long" in types
    assert "meta_description_too_long" in types


def test_missing_title_description_h1_viewport_lang_and_schema(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": f"<html><head></head><body><p>{BODY_WORDS}</p></body></html>",
    })
    types = run.page_issue_types()
    for expected in ("title_missing", "meta_description_missing", "h1_missing",
                     "viewport_missing", "html_lang_missing",
                     "canonical_missing", "schema_missing"):
        assert expected in types, expected


def test_short_title_and_multiple_h1(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": f'<html lang="en"><head><title>Short</title>'
                    f'<meta name="description" content="d">'
                    f'<meta name="viewport" content="w">'
                    f'<link rel="canonical" href="{BASE}/"></head>'
                    f"<body><h1>a</h1><h1>b</h1><p>{BODY_WORDS}</p></body></html>",
    })
    types = run.page_issue_types()
    assert "title_too_short" in types
    assert "h1_multiple" in types


def test_noindex_and_nofollow_pages(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/",
                         head='<meta name="robots" content="noindex, nofollow">'),
    })
    assert "noindex_page" in run.page_issue_types()
    assert "nofollow_page" in run.page_issue_types()


def test_thin_page_and_missing_alt(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", body="only a handful of words here",
                         head="") .replace(
            "<h1>Heading</h1>", '<h1>Heading</h1><img src="/a.png">'),
    })
    types = run.page_issue_types()
    assert "thin_page" in types
    assert "images_missing_alt" in types
    detail = run.page_issues_of("images_missing_alt")[0]["detail"]
    assert "1 of 1" in detail


def test_mixed_content_is_reported(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/").replace(
            "<h1>Heading</h1>",
            '<h1>Heading</h1><script src="http://cdn.example.com/a.js"></script>'),
    })
    mixed = run.page_issues_of("mixed_content")
    assert [i["final_url"] for i in mixed] == [BASE + "/"]
    assert mixed[0]["severity"] == "high"


def test_invalid_json_ld_is_reported(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/").replace(
            '{"@type": "WebPage", "name": "n"}', '{"@type": "WebPage",'),
    })
    assert "schema_invalid_json" in run.page_issue_types()


def test_schema_missing_property_reaches_the_page_issues(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/").replace(
            '{"@type": "WebPage", "name": "n"}', '{"@type": "Organization"}'),
    })
    details = {i["detail"] for i in
               run.page_issues_of("schema_missing_property")}
    assert "Organization is missing name" in details
    assert "Organization is missing url" in details


# --- cross-page checks ------------------------------------------------------

def test_duplicate_titles_produce_one_row_naming_the_group(tmp_path):
    same = "<title>Exactly the same title on three</title>"
    body = f"<body><h1>h</h1><p>{BODY_WORDS}</p></body>"
    pages = {BASE + "/": f'<html lang="en"><head>{same}'
                         f'<meta name="description" content="d">'
                         f'<meta name="viewport" content="w">'
                         f'<link rel="canonical" href="{BASE}/"></head>'
                         f'<body><h1>h</h1><p>{BODY_WORDS}</p>'
                         f'<a href="/a">a</a><a href="/b">b</a></body></html>'}
    for name in ("a", "b"):
        pages[f"{BASE}/{name}"] = (
            f'<html lang="en"><head>{same}'
            f'<meta name="description" content="d">'
            f'<meta name="viewport" content="w">'
            f'<link rel="canonical" href="{BASE}/{name}"></head>{body}</html>')

    run = crawl_site(tmp_path, pages)
    dupes = run.page_issues_of("duplicate_title")
    assert len(dupes) == 1
    assert "3 pages share title" in dupes[0]["detail"]
    for name in ("/", "/a", "/b"):
        assert BASE + name in dupes[0]["detail"]

    # The same three share a description, so that fires once too.
    assert len(run.page_issues_of("duplicate_meta_description")) == 1


def test_non_reciprocal_hreflang_is_reported(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", links=["/fr/"],
                         head=f'<link rel="alternate" hreflang="fr" '
                              f'href="{BASE}/fr/">'),
        BASE + "/fr/": page(BASE + "/fr/"),  # points back at nobody
    })
    missing = run.page_issues_of("hreflang_not_reciprocal")
    assert [i["final_url"] for i in missing] == [BASE + "/"]
    assert BASE + "/fr/" in missing[0]["detail"]


def test_reciprocal_hreflang_is_silent(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", links=["/fr/"],
                         head=f'<link rel="alternate" hreflang="fr" '
                              f'href="{BASE}/fr/">'),
        BASE + "/fr/": page(BASE + "/fr/",
                            head=f'<link rel="alternate" hreflang="en" '
                                 f'href="{BASE}/">'),
    })
    assert run.page_issues_of("hreflang_not_reciprocal") == []


def test_sitemap_noindex_and_off_canonical(tmp_path):
    sitemap = f"""<?xml version="1.0"?><urlset>
      <url><loc>{BASE}/</loc></url>
      <url><loc>{BASE}/hidden</loc></url>
      <url><loc>{BASE}/elsewhere</loc></url>
    </urlset>"""
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", links=["/hidden", "/elsewhere"]),
        BASE + "/hidden": page(BASE + "/hidden",
                               head='<meta name="robots" content="noindex">'),
        BASE + "/elsewhere": page(BASE + "/"),
    }, sitemaps={BASE + "/sitemap.xml": sitemap})

    assert [i["final_url"] for i in
            run.page_issues_of("sitemap_noindex")] == [BASE + "/hidden"]
    assert [i["final_url"] for i in
            run.page_issues_of("sitemap_off_canonical")] == [BASE + "/elsewhere"]


# --- what does not get parsed ----------------------------------------------

def test_non_200_html_is_never_parsed(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/", links=["/gone"]),
        BASE + "/gone": {"status_code": 404,
                         "headers": {"Content-Type": "text/html"},
                         "text": "<html><head></head><body>not found</body></html>"},
    })

    assert BASE + "/gone" in {r["url"] for r in run.rows}
    assert BASE + "/gone" not in run.pages_by_url()
    assert run.summary["pages_parsed"] == 1
    # The 404 body has no title, but that is not reported as a page fault.
    assert run.page_issues_of("title_missing") == []


# --- site level -------------------------------------------------------------

def test_site_level_header_checks_land_on_the_homepage(tmp_path):
    def extra(mock):
        mock.head("http://example.com/", status_code=200,
                  headers={"Content-Type": "text/html"})
        mock.get("http://example.com/", status_code=200,
                 headers={"Content-Type": "text/html"}, text="x")

    run = crawl_site(tmp_path, {BASE + "/": page(BASE + "/")}, extra=extra)
    site = [i for i in run.page_issues if i["site_level"] == "True"]
    types = {i["issue_type"] for i in site}

    assert types == {"http_to_https_redirect", "hsts_missing",
                     "x_content_type_options_missing",
                     "x_frame_options_missing", "csp_missing"}
    assert all(i["final_url"] == BASE + "/" for i in site)


def test_present_security_headers_are_not_reported(tmp_path):
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Strict-Transport-Security": "max-age=63072000",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "frame-ancestors 'self'",
    }

    def extra(mock):
        mock.head("http://example.com/", status_code=301,
                  headers={"Location": BASE + "/"})

    run = crawl_site(tmp_path, {
        BASE + "/": {"text": page(BASE + "/"), "headers": headers},
    }, extra=extra)

    types = {i["issue_type"] for i in run.page_issues
             if i["site_level"] == "True"}
    # frame-ancestors in the CSP satisfies X-Frame-Options.
    assert types == set()


# --- A4: microdata counts as structured data --------------------------------

MICRODATA_BODY = ('<div itemscope itemtype="https://schema.org/Product">'
                  '<span itemprop="name">A product</span></div>')


def no_jsonld(body="", head=""):
    """A page with no JSON-LD block at all."""
    return (f"<!doctype html><html lang='en'><head>"
            + GOOD_HEAD.format(canonical=BASE + "/") + head +
            f"</head><body><h1>Heading</h1><p>{BODY_WORDS}</p>{body}</body></html>")


def test_microdata_only_page_is_not_reported_as_missing_schema(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": no_jsonld(MICRODATA_BODY)})

    assert run.page_issues_of("schema_missing") == []
    only = run.page_issues_of("schema_microdata_only")
    assert [i["final_url"] for i in only] == [BASE + "/"]
    assert only[0]["severity"] == "low"
    assert "Product" in only[0]["detail"]

    row = run.pages_by_url()[BASE + "/"]
    assert row["has_microdata"] == "True"
    assert row["microdata_types"] == "Product"


def test_rdfa_typeof_is_detected_too(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": no_jsonld('<div typeof="foaf:Person"></div>')})
    assert run.pages_by_url()[BASE + "/"]["microdata_types"] == "Person"
    assert run.page_issues_of("schema_missing") == []


def test_no_structured_data_at_all_is_still_schema_missing(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": no_jsonld()})
    missing = run.page_issues_of("schema_missing")
    assert [i["final_url"] for i in missing] == [BASE + "/"]
    assert "no JSON-LD and no microdata" in missing[0]["detail"]
    assert run.page_issues_of("schema_microdata_only") == []
    assert run.pages_by_url()[BASE + "/"]["has_microdata"] == "False"


def test_h1_multiple_is_low_severity(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(BASE + "/").replace(
            "<h1>Heading</h1>", "<h1>One</h1><h1>Two</h1>"),
    })
    rows = run.page_issues_of("h1_multiple")
    assert rows and rows[0]["severity"] == "low"


def test_canonical_check_limit_default_is_five_hundred():
    from seo_audit.config import AuditConfig
    assert AuditConfig(domain="example.com").canonical_check_limit == 500


def test_summary_states_how_many_canonical_targets_went_unchecked(tmp_path):
    pages = {BASE + "/": page(BASE + "/", links=["/a"])}
    pages[BASE + "/a"] = page(BASE + "/away-a")

    def extra(mock):
        mock.head(f"{BASE}/away-a", status_code=200,
                  headers={"Content-Type": "text/html"})

    run = crawl_site(tmp_path, pages, extra=extra, canonical_check_limit=0)
    assert run.summary["canonical_targets_unchecked"] == 1
    assert run.summary["canonical_check_limit"] == 0
