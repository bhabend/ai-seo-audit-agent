"""Crawl behaviour: caps, robots, source flags, host adoption, slash folding."""

from conftest import BASE, crawl_site, html_page

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc></url>
  <url><loc>https://example.com/orphan</loc></url>
</urlset>"""


def test_crawl_follows_links_and_records_depth_and_referrer(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/about", "/pricing"]),
        BASE + "/about": html_page(["/team"]),
        BASE + "/pricing": html_page([]),
        BASE + "/team": html_page([]),
    })

    found = run.by_url()
    assert set(found) == {BASE + "/", BASE + "/about", BASE + "/pricing",
                          BASE + "/team"}
    assert found[BASE + "/"]["depth"] == "0"
    assert found[BASE + "/about"]["depth"] == "1"
    assert found[BASE + "/team"]["depth"] == "2"
    assert found[BASE + "/team"]["discovered_from"] == BASE + "/about"
    assert all(r["in_crawl"] == "True" for r in run.rows)


def test_external_and_subdomain_links_are_not_crawled(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page([
            "/inside",
            "https://other.com/outside",
            "https://blog.example.com/post",
            "https://www.example.com/inside-www",
        ]),
        BASE + "/inside": html_page([]),
        "https://www.example.com/inside-www": html_page([]),
    })

    urls = set(run.by_url())
    assert BASE + "/inside" in urls
    # www of our own host counts as us; other hosts do not.
    assert "https://www.example.com/inside-www" in urls
    assert "https://other.com/outside" not in urls
    assert "https://blog.example.com/post" not in urls


def test_max_pages_cap_is_respected(tmp_path):
    pages = {BASE + "/": html_page([f"/p{i}" for i in range(10)])}
    for i in range(10):
        pages[BASE + f"/p{i}"] = html_page([])
    run = crawl_site(tmp_path, pages, max_pages=3)

    assert len(run.rows) == 3
    assert run.summary["pages_found"] == 3


def test_max_pages_default_is_two_thousand():
    from seo_audit.config import AuditConfig
    assert AuditConfig(domain="example.com").max_pages == 2000


def test_max_depth_cap_stops_link_following(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/a"]),
        BASE + "/a": html_page(["/b"]),
        BASE + "/b": html_page(["/c"]),
        BASE + "/c": html_page([]),
    }, max_depth=1)

    assert set(run.by_url()) == {BASE + "/", BASE + "/a"}
    assert run.summary["max_depth_reached"] == 1


def test_deep_pages_beyond_three_clicks_are_reported(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/a"]),
        BASE + "/a": html_page(["/b"]),
        BASE + "/b": html_page(["/c"]),
        BASE + "/c": html_page(["/d"]),
        BASE + "/d": html_page([]),
    }, max_depth=5)

    deep = run.issues_of("deep_page")
    assert [i["url"] for i in deep] == [BASE + "/d"]
    assert run.summary["max_depth_reached"] == 4


def test_robots_disallow_is_respected_and_can_be_switched_off(tmp_path):
    robots_txt = "User-agent: *\nDisallow: /private/\n"
    pages = {
        BASE + "/": html_page(["/private/secret", "/public"]),
        BASE + "/private/secret": html_page([]),
        BASE + "/public": html_page([]),
    }
    run = crawl_site(tmp_path, pages, robots=robots_txt)
    urls = set(run.by_url())
    assert BASE + "/public" in urls
    assert BASE + "/private/secret" not in urls
    blocked = run.issues_of("robots_blocked_linked")
    assert [i["url"] for i in blocked] == [BASE + "/private/secret"]
    assert blocked[0]["referrer"] == BASE + "/"

    run = crawl_site(tmp_path / "off", pages, robots=robots_txt,
                     respect_robots=False)
    assert BASE + "/private/secret" in set(run.by_url())


def test_in_sitemap_and_in_crawl_flags_cover_all_three_cases(tmp_path):
    run = crawl_site(
        tmp_path,
        {
            BASE + "/": html_page(["/linked"]),
            BASE + "/linked": html_page([]),
            BASE + "/orphan": html_page([]),
        },
        sitemaps={BASE + "/sitemap.xml": SITEMAP_XML},
    )

    found = run.by_url()
    both = found[BASE + "/"]
    crawl_only = found[BASE + "/linked"]
    sitemap_only = found[BASE + "/orphan"]

    assert (both["in_sitemap"], both["in_crawl"]) == ("True", "True")
    assert (crawl_only["in_sitemap"], crawl_only["in_crawl"]) == ("False", "True")
    assert (sitemap_only["in_sitemap"], sitemap_only["in_crawl"]) == ("True", "False")
    # Never reached by a link, so it has no depth and no referrer.
    assert sitemap_only["depth"] == ""
    assert sitemap_only["discovered_from"] == ""
    assert crawl_only["depth"] == "1"

    assert run.summary["by_source"] == {
        "in_sitemap_and_crawl": 1, "sitemap_only": 1, "crawl_only": 1
    }
    assert run.summary["sitemap"]["source"] == "guess"
    assert [i["url"] for i in run.issues_of("crawl_only_page")] == [BASE + "/linked"]
    assert [i["url"] for i in run.issues_of("sitemap_only_page")] == [BASE + "/orphan"]


def test_non_html_urls_are_recorded_but_not_parsed_for_links(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/brochure.pdf", "/logo.png"]),
        BASE + "/brochure.pdf": {
            "content": b"%PDF-1.4 <a href='/hidden'>",
            "headers": {"Content-Type": "application/pdf"},
        },
        BASE + "/logo.png": {
            "content": b"\x89PNG\r\n",
            "headers": {"Content-Type": "image/png"},
        },
        BASE + "/hidden": html_page([]),
    })

    found = run.by_url()
    assert found[BASE + "/brochure.pdf"]["status_code"] == "200"
    assert found[BASE + "/brochure.pdf"]["text_chars"] == ""
    # The href inside the PDF bytes must not become a crawl target.
    assert BASE + "/hidden" not in found
    assert {i["url"] for i in run.issues_of("non_html_linked")} == {
        BASE + "/brochure.pdf", BASE + "/logo.png"}


def test_duplicate_urls_collapse_to_one_row(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page([
            "/about", "/about#team", "/about?utm_source=nav",
            "https://EXAMPLE.com/about",
        ]),
        BASE + "/about": html_page([]),
    })

    urls = [r["url"] for r in run.rows]
    assert len(urls) == len(set(urls))
    assert sorted(urls) == [BASE + "/", BASE + "/about"]


def test_error_pages_are_recorded_with_their_status(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/gone", "/broken"]),
        BASE + "/gone": {"status_code": 404,
                         "headers": {"Content-Type": "text/html"},
                         "text": "not found"},
        BASE + "/broken": {"status_code": 500,
                           "headers": {"Content-Type": "text/html"},
                           "text": "boom"},
    })

    assert run.summary["status_counts"] == {"200": 1, "404": 1, "500": 1}
    assert run.summary["pages_found"] == 3
    # A 5xx is a page the crawler could not read, so it is a fetch error.
    assert [i["url"] for i in run.issues_of("fetch_error")] == [BASE + "/broken"]


def test_slow_pages_are_reported(tmp_path, monkeypatch):
    import seo_audit.fetch as fetch_module

    import itertools
    ticks = itertools.count(0.0, 5.0)
    monkeypatch.setattr(fetch_module.time, "perf_counter", lambda: next(ticks))
    run = crawl_site(tmp_path, {BASE + "/": html_page([])})

    assert [i["url"] for i in run.issues_of("slow_response")] == [BASE + "/"]
    assert "5000 ms" in run.issues_of("slow_response")[0]["detail"]


# --- decision A2: adopt the host the homepage actually redirects to ---------

def test_www_homepage_redirect_adopts_the_non_www_host(tmp_path):
    """The site says it lives at example.com; the run follows it there."""
    def extra(mock):
        mock.get("https://www.example.com/", status_code=301,
                 headers={"Location": BASE + "/"})
        mock.get("https://www.example.com/robots.txt", status_code=404)
        mock.get("https://www.example.com/sitemap.xml", status_code=404)
        mock.get("https://www.example.com/sitemap_index.xml", status_code=404)

    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/about"]),
        BASE + "/about": html_page([]),
    }, extra=extra, domain="https://www.example.com")

    assert run.summary["configured_host"] == "www.example.com"
    assert run.summary["host"] == "example.com"
    assert run.summary["host_adopted"] is True
    adopted = run.issues_of("host_redirect")
    assert len(adopted) == 1
    assert "example.com" in adopted[0]["detail"]
    # The seed row keeps the URL we asked for; everything found after the
    # redirect is on the adopted host.
    seed = run.by_url()["https://www.example.com/"]
    assert seed["final_url"] == BASE + "/"
    assert [r["url"] for r in run.rows if r["url"] != seed["url"]] ==         [BASE + "/about"]


def test_host_is_not_adopted_when_the_redirect_leaves_the_site(tmp_path):
    def extra(mock):
        mock.get("https://other.com/", text=html_page([]),
                 headers={"Content-Type": "text/html"})

    run = crawl_site(tmp_path, {
        BASE + "/": {"status_code": 301,
                     "headers": {"Location": "https://other.com/"}},
    }, extra=extra)

    assert run.summary["host"] == "example.com"
    assert run.summary["host_adopted"] is False
    assert run.issues_of("host_redirect") == []


# --- decision A3: fold trailing-slash redirects, and say so -----------------

def test_trailing_slash_redirect_is_folded_and_reported(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/workspaces"]),
        BASE + "/workspaces": {"status_code": 301,
                               "headers": {"Location": BASE + "/workspaces/"}},
        BASE + "/workspaces/": html_page([]),
    })

    found = run.by_url()
    # One row, one fetch: the slashed target is never fetched separately.
    assert BASE + "/workspaces" in found
    assert BASE + "/workspaces/" not in found
    assert found[BASE + "/workspaces"]["final_url"] == BASE + "/workspaces/"

    folded = run.issues_of("trailing_slash_redirect")
    assert len(folded) == 1
    assert folded[0]["url"] == BASE + "/workspaces"
    assert folded[0]["referrer"] == BASE + "/"
    assert "crawler spends an extra request" in folded[0]["crawler_effect"]


def test_slash_variants_stay_distinct_when_neither_redirects(tmp_path):
    """normalize() must not merge /x and /x/ on its own; only a 301 folds them."""
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/docs", "/docs/"]),
        BASE + "/docs": html_page([]),
        BASE + "/docs/": html_page([]),
    })

    urls = set(run.by_url())
    assert BASE + "/docs" in urls and BASE + "/docs/" in urls
    assert run.issues_of("trailing_slash_redirect") == []


def test_redirect_chain_of_two_or_more_hops_is_reported(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": html_page(["/a"]),
        BASE + "/a": {"status_code": 301, "headers": {"Location": BASE + "/b"}},
        BASE + "/b": {"status_code": 301, "headers": {"Location": BASE + "/c"}},
        BASE + "/c": html_page([]),
    })

    chained = run.issues_of("redirect_chain")
    assert [i["url"] for i in chained] == [BASE + "/a"]
    assert "2 hops" in chained[0]["detail"]
