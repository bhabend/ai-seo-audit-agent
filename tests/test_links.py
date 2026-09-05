"""The internal link graph: inlinks, orphans, broken targets, anchors."""

import os

from conftest import BASE, crawl_site, html_page
from seo_audit.links import cap_note, describe_referrers, is_generic_anchor
from seo_audit.urlnorm import Link


def linked(html_links, body="Some ordinary body copy for this test page."):
    """A page whose links carry anchor text."""
    anchors = "".join(f'<a href="{href}">{text}</a>' for href, text in html_links)
    return ("<!doctype html><html lang='en'><head><title>t</title></head>"
            f"<body><p>{body}</p>{anchors}</body></html>")


def test_extract_links_returns_anchor_text_and_rel():
    from seo_audit.urlnorm import extract_links, make_soup
    soup = make_soup('<a href="/a">Real anchor</a>'
                     '<a href="/b" rel="nofollow noopener">Other</a>'
                     '<a href="/a">Click here</a>')
    links = extract_links(soup, BASE + "/")
    assert links == [
        Link(BASE + "/a", "Real anchor", False),
        Link(BASE + "/b", "Other", True),
        # Same target, different anchor: two facts, not one.
        Link(BASE + "/a", "Click here", False),
    ]


def test_inlinks_and_outlinks_are_counted_per_page(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/a", "About us"), ("/b", "Pricing")]),
        BASE + "/a": linked([("/b", "Pricing"), ("https://other.com/", "out")]),
        BASE + "/b": linked([]),
    })

    pages = run.pages_by_url()
    assert pages[BASE + "/b"]["inlinks"] == "2"      # from / and /a
    assert pages[BASE + "/a"]["inlinks"] == "1"
    assert pages[BASE + "/a"]["outlinks_internal"] == "1"
    assert pages[BASE + "/a"]["outlinks_external"] == "1"
    assert "Pricing" in pages[BASE + "/b"]["anchor_texts"]


def test_every_parsed_row_has_the_link_and_content_columns(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/a", "About")]),
        BASE + "/a": linked([]),
    })
    for row in run.pages:
        assert row["inlinks"] != ""
        assert row["outlinks_internal"] != ""
        assert row["content_simhash"] != ""
        assert row["content_md5"] != ""


# --- lesson 6: resolve targets through the redirect map --------------------

def test_a_page_linked_only_by_its_redirecting_form_is_not_an_orphan(tmp_path):
    """The trap: /x is linked, /x/ is the page. Counting raw targets orphans it."""
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/docs", "Documentation")]),
        BASE + "/docs": {"status_code": 301,
                         "headers": {"Location": BASE + "/docs/"}},
        BASE + "/docs/": linked([]),
    })

    pages = run.pages_by_url()
    assert BASE + "/docs/" in pages
    assert pages[BASE + "/docs/"]["inlinks"] == "1"
    assert run.page_issues_of("orphan_page") == []


def test_orphan_page_excludes_the_homepage_and_states_the_cap(tmp_path):
    sitemap = f"""<?xml version="1.0"?><urlset>
      <url><loc>{BASE}/</loc></url>
      <url><loc>{BASE}/lonely</loc></url>
    </urlset>"""
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/a", "About")]),
        BASE + "/a": linked([]),
        BASE + "/lonely": linked([]),
    }, sitemaps={BASE + "/sitemap.xml": sitemap})

    orphans = [i["final_url"] for i in run.page_issues_of("orphan_page")]
    assert orphans == [BASE + "/lonely"]        # the homepage is never an orphan
    assert "within the 20 pages this run crawled" in \
        run.page_issues_of("orphan_page")[0]["detail"]


def test_orphan_page_states_sitemap_membership_and_replaces_sitemap_only(tmp_path):
    """A1: one finding, not two. The old sitemap_only_page is gone."""
    sitemap = f"""<?xml version="1.0"?><urlset>
      <url><loc>{BASE}/</loc></url>
      <url><loc>{BASE}/linked</loc></url>
      <url><loc>{BASE}/unlinked</loc></url>
      <url><loc>{BASE}/never-crawled</loc></url>
    </urlset>"""
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/linked", "Linked page"), ("/stray", "Stray")]),
        BASE + "/linked": linked([]),
        BASE + "/unlinked": linked([]),
        BASE + "/stray": linked([]),
        BASE + "/never-crawled": linked([]),
    }, sitemaps={BASE + "/sitemap.xml": sitemap}, max_pages=4)

    orphans = {i["final_url"]: i["detail"]
               for i in run.page_issues_of("orphan_page")}
    assert BASE + "/unlinked" in orphans
    assert "also listed in sitemap: yes" in orphans[BASE + "/unlinked"]
    assert "within the 4 pages this run crawled" in orphans[BASE + "/unlinked"]

    # Linked pages and the homepage are never orphans.
    assert BASE + "/linked" not in orphans
    assert BASE + "/" not in orphans

    # The type is retired: no rows, and it is not even registered any more.
    from seo_audit.issues import CRAWLER_EFFECT, PAGE_ISSUE_SEVERITY
    assert "sitemap_only_page" not in CRAWLER_EFFECT
    assert "sitemap_only_page" not in PAGE_ISSUE_SEVERITY
    assert all(i["issue_type"] != "sitemap_only_page" for i in run.issues)
    assert run.summary["links"]["orphan_pages_in_sitemap"] >= 1


def test_orphan_outside_the_sitemap_says_so(tmp_path):
    sitemap = f'<?xml version="1.0"?><urlset><url><loc>{BASE}/</loc></url></urlset>'
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/a", "A")]),
        BASE + "/a": linked([]),
        BASE + "/stray": linked([]),
    }, sitemaps={BASE + "/sitemap.xml": sitemap})
    # /stray is unreachable by link and not in the sitemap, so it is not
    # crawled at all -- nothing to report. The homepage and /a are fine.
    assert run.page_issues_of("orphan_page") == []
    assert run.summary["links"]["orphan_pages_in_sitemap"] == 0


def test_low_inlink_page_is_reported_separately(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/a", "About"), ("/b", "Bee")]),
        BASE + "/a": linked([("/b", "Bee")]),
        BASE + "/b": linked([]),
    })
    low = [i["final_url"] for i in run.page_issues_of("low_inlink_page")]
    assert low == [BASE + "/a"]


# --- broken and redirected internal targets --------------------------------

def test_broken_internal_link_groups_by_target_with_referrers(tmp_path):
    """Fifty links to one dead page are one finding, not fifty."""
    hub = linked([(f"/gone", "Gone"), ("/a", "A"), ("/b", "B")])
    run = crawl_site(tmp_path, {
        BASE + "/": hub,
        BASE + "/a": linked([("/gone", "Gone")]),
        BASE + "/b": linked([("/gone", "Gone")]),
        BASE + "/gone": {"status_code": 404,
                         "headers": {"Content-Type": "text/html"},
                         "text": "<html><body>no</body></html>"},
    })

    broken = run.page_issues_of("broken_internal_link")
    assert len(broken) == 1
    assert broken[0]["url"] == BASE + "/gone"
    assert "HTTP 404" in broken[0]["detail"]
    assert "linked from 3 page(s)" in broken[0]["detail"]
    assert broken[0]["severity"] == "high"


def test_redirected_internal_link_names_the_target_once(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/old", "Old page")]),
        BASE + "/old": {"status_code": 301,
                        "headers": {"Location": BASE + "/new/"}},
        BASE + "/new/": linked([]),
    })
    rows = run.page_issues_of("redirected_internal_link")
    assert [r["url"] for r in rows] == [BASE + "/old"]
    assert BASE + "/new/" in rows[0]["detail"]
    assert "linked from 1 page(s)" in rows[0]["detail"]


# --- anchors and nofollow ---------------------------------------------------

def test_generic_anchor_fires_only_when_every_anchor_is_generic(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("/vague", "Click here"), ("/clear", "Our pricing")]),
        BASE + "/vague": linked([]),
        BASE + "/clear": linked([]),
    })
    generic = [i["final_url"] for i in run.page_issues_of("generic_anchor")]
    assert generic == [BASE + "/vague"]


def test_is_generic_anchor_handles_punctuation_and_case():
    for text in ("Click here", "READ MORE", "  learn more  ", "More »"):
        assert is_generic_anchor(text), text
    assert not is_generic_anchor("Supermicro rackmount servers")


def test_nofollow_internal_link_counts_per_source_page(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": ("<html lang='en'><head><title>t</title></head><body>"
                     "<p>copy</p>"
                     '<a href="/a" rel="nofollow">A</a>'
                     '<a href="/b" rel="nofollow">B</a></body></html>'),
        BASE + "/a": linked([]),
        BASE + "/b": linked([]),
    })
    rows = run.page_issues_of("nofollow_internal_link")
    assert [r["final_url"] for r in rows] == [BASE + "/"]
    assert "2 internal link(s)" in rows[0]["detail"]


# --- external links ---------------------------------------------------------

def test_external_links_are_checked_and_broken_ones_reported(tmp_path):
    def extra(mock):
        mock.head("https://dead.com/gone", status_code=404,
                  headers={"Content-Type": "text/html"})

    run = crawl_site(tmp_path, {
        BASE + "/": linked([("https://dead.com/gone", "Dead"),
                            ("https://other.com/", "Alive")]),
    }, extra=extra)

    broken = run.page_issues_of("external_link_broken")
    assert [b["url"] for b in broken] == ["https://dead.com/gone"]
    assert "HTTP 404" in broken[0]["detail"]
    assert run.summary["links"]["external_checked"] == 2
    assert run.summary["links"]["external_broken"] == 1
    assert run.summary["links"]["external_unchecked"] == 0


def test_external_check_limit_leaves_the_rest_unchecked_not_assumed(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": linked([("https://dead.com/gone", "One"),
                            ("https://other.com/", "Two"),
                            ("https://partner.com/", "Three")]),
    }, external_check_limit=1)

    stats = run.summary["links"]
    assert stats["external_targets_found"] == 3
    assert stats["external_checked"] == 1
    assert stats["external_unchecked"] == 2
    assert stats["external_check_limit"] == 1


def test_external_check_limit_default_is_one_thousand():
    from seo_audit.config import AuditConfig
    assert AuditConfig(domain="example.com").external_check_limit == 1000


def test_the_most_linked_external_targets_are_checked_first(tmp_path):
    """A2: under a cap, a link used everywhere must not lose to alphabet."""
    def extra(mock):
        # The busiest target is dead; the rarely used one is alphabetically
        # first, so alphabetical order would check it and miss the real fault.
        mock.head("https://dead.com/gone", status_code=404,
                  headers={"Content-Type": "text/html"})

    pages = {BASE + "/": linked([("https://dead.com/gone", "Everywhere"),
                                 ("https://aaa-rare.com/", "Once"),
                                 ("/a", "A"), ("/b", "B")])}
    for name in ("a", "b"):
        pages[f"{BASE}/{name}"] = linked([("https://dead.com/gone", "Everywhere")])

    run = crawl_site(tmp_path, pages, extra=extra, external_check_limit=1)

    # One check, spent on the target three pages link to.
    assert run.summary["links"]["external_checked"] == 1
    broken = run.page_issues_of("external_link_broken")
    assert [b["url"] for b in broken] == ["https://dead.com/gone"]
    assert "linked from 3 page(s)" in broken[0]["detail"]


# --- --write-links ----------------------------------------------------------

def test_write_links_is_off_by_default(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": linked([("/a", "A")]),
                                BASE + "/a": linked([])})
    assert "links.csv" not in os.listdir(run.out_dir)
    assert len(os.listdir(run.out_dir)) == 6


def test_write_links_writes_the_edge_list(tmp_path):
    run = crawl_site(tmp_path, {BASE + "/": linked([("/a", "About us")]),
                                BASE + "/a": linked([])},
                     write_links=True)
    assert "links.csv" in os.listdir(run.out_dir)
    edge = [e for e in run.links if e["target"] == BASE + "/a"][0]
    assert edge["source"] == BASE + "/"
    assert edge["anchor"] == "About us"
    assert edge["nofollow"] == "False"


def test_helpers_read_as_plain_english():
    assert describe_referrers(["b", "a"]) == "linked from 2 page(s): a, b"
    assert cap_note(2000) == "within the 2000 pages this run crawled"
