"""The full sitemap sweep: every sitemap URL checked, none of them parsed."""

from conftest import BASE, crawl_site, html_page

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc></url>
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
  <url><loc>https://example.com/gone</loc></url>
  <url><loc>https://example.com/checkout/</loc></url>
</urlset>"""

ROBOTS = "User-agent: *\nDisallow: /checkout/\n"


def sweep_site(tmp_path, **overrides):
    return crawl_site(
        tmp_path,
        {
            BASE + "/": html_page([]),
            BASE + "/a": html_page([]),
            BASE + "/b": html_page([]),
            BASE + "/gone": {"status_code": 404,
                             "headers": {"Content-Type": "text/html"},
                             "text": "no"},
            BASE + "/checkout/": html_page([]),
        },
        robots=ROBOTS,
        sitemaps={BASE + "/sitemap.xml": SITEMAP_XML},
        **overrides,
    )


def test_sweep_covers_sitemap_urls_the_page_crawl_could_not_reach(tmp_path):
    # A budget of 2 leaves most of the sitemap for the sweep.
    run = sweep_site(tmp_path, max_pages=2)

    crawled = {r["url"] for r in run.rows}
    swept = {r["url"] for r in run.sweep}
    sitemap_urls = {BASE + "/", BASE + "/a", BASE + "/b", BASE + "/gone",
                    BASE + "/checkout/"}

    # Every sitemap URL is accounted for exactly once: crawled or swept.
    assert crawled | swept >= sitemap_urls
    assert not (crawled & swept)
    assert len(run.sweep) == run.summary["sitemap_sweep"]["checked"]
    assert (run.summary["sitemap_sweep"]["checked"]
            + run.summary["sitemap_sweep"]["already_crawled"]) == len(sitemap_urls)


def test_blocked_sitemap_url_is_swept_but_never_fetched(tmp_path):
    run = sweep_site(tmp_path, max_pages=2)

    checkout = [r for r in run.sweep if r["url"] == BASE + "/checkout/"]
    assert len(checkout) == 1
    assert checkout[0]["blocked_by_robots"] == "True"
    # Blocked means not requested at all, so there is no status to report.
    assert checkout[0]["status_code"] == ""
    assert BASE + "/checkout/" not in {r["url"] for r in run.rows}

    blocked = run.issues_of("robots_blocked_in_sitemap")
    assert [i["url"] for i in blocked] == [BASE + "/checkout/"]


def test_sweep_reports_non_200_sitemap_entries(tmp_path):
    run = sweep_site(tmp_path, max_pages=2)
    gone = [r for r in run.sweep if r["url"] == BASE + "/gone"]
    assert gone and gone[0]["status_code"] == "404"
    assert [i["url"] for i in run.issues_of("sitemap_non_200")] == [BASE + "/gone"]


def test_sweep_reports_redirecting_sitemap_entries(tmp_path):
    run = crawl_site(
        tmp_path,
        {BASE + "/": html_page([]),
         BASE + "/moved": {"status_code": 301,
                           "headers": {"Location": BASE + "/final"}},
         BASE + "/final": html_page([])},
        sitemaps={BASE + "/sitemap.xml": """<?xml version="1.0"?>
<urlset><url><loc>https://example.com/moved</loc></url></urlset>"""},
        max_pages=1,
    )

    moved = [r for r in run.sweep if r["url"] == BASE + "/moved"]
    assert moved and moved[0]["redirect_hops"] == "1"
    assert moved[0]["final_url"] == BASE + "/final"
    assert [i["url"] for i in run.issues_of("redirect_in_sitemap")] == [BASE + "/moved"]


def test_sweep_uses_head_and_falls_back_to_get(tmp_path):
    """HEAD first; a server that refuses it still gets checked."""
    calls = []

    def extra(mock):
        def record(request, context):
            calls.append((request.method, request.path))
            if request.method == "HEAD":
                context.status_code = 405
                return ""
            context.status_code = 200
            context.headers["Content-Type"] = "text/html"
            return "ok"

        mock.register_uri("HEAD", BASE + "/stubborn", text=record)
        mock.register_uri("GET", BASE + "/stubborn", text=record)

    run = crawl_site(
        tmp_path,
        {BASE + "/": html_page([])},
        sitemaps={BASE + "/sitemap.xml": """<?xml version="1.0"?>
<urlset><url><loc>https://example.com/stubborn</loc></url></urlset>"""},
        extra=extra,
        max_pages=1,
    )

    methods = [m for m, path in calls if path == "/stubborn"]
    assert methods == ["HEAD", "GET"]
    row = [r for r in run.sweep if r["url"] == BASE + "/stubborn"][0]
    assert row["status_code"] == "200"


def test_sweep_never_stores_html(tmp_path):
    run = sweep_site(tmp_path, max_pages=2)
    assert not run.has_dir("html")
    # The sweep CSV has no body or text column at all.
    assert set(run.sweep[0].keys()) == {
        "url", "status_code", "final_url", "redirect_hops",
        "blocked_by_robots", "in_crawl", "error"}
