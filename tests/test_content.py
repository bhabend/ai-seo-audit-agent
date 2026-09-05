"""Duplicate and near-duplicate detection by fingerprint."""

from conftest import BASE, crawl_site
from seo_audit.content import (MIN_WORDS_FOR_COMPARISON, ContentIndex,
                               fingerprint, hamming, simhash, text_md5)

FILLER = " ".join([f"sentence number {i} about servers and storage."
                   for i in range(60)])


def page(body, title="A perfectly reasonable page title"):
    return (f"<!doctype html><html lang='en'><head><title>{title}</title>"
            f'<meta name="description" content="A description of the page.">'
            f'<meta name="viewport" content="width=device-width">'
            f'<link rel="canonical" href="{BASE}/"></head>'
            f"<body><h1>Heading</h1><p>{body}</p></body></html>")


def test_normalised_text_ignores_case_and_punctuation():
    assert text_md5("Hello, World!") == text_md5("hello world")
    assert text_md5("Hello world") != text_md5("Goodbye world")


def test_simhash_is_close_for_near_identical_text():
    a = FILLER
    b = FILLER + " One extra closing sentence appears only here."
    assert hamming(simhash(a), simhash(b)) <= 3


def test_simhash_is_far_for_unrelated_text():
    a = " ".join(["servers and storage systems for the data centre"] * 40)
    b = " ".join(["a recipe for lemon cake with butter icing"] * 40)
    assert hamming(simhash(a), simhash(b)) > 3


def test_simhash_notices_word_order():
    """Shingles, not a bag of words: rearrangement is a different page."""
    words = [f"word{i}" for i in range(300)]
    a = " ".join(words)
    b = " ".join(reversed(words))
    assert hamming(simhash(a), simhash(b)) > 3


def test_empty_text_is_stable():
    marks = fingerprint("")
    assert marks.simhash == 0
    assert marks.word_count == 0
    assert len(marks.simhash_hex) == 16


def test_index_groups_exact_duplicates():
    index = ContentIndex()
    marks = fingerprint(FILLER)
    for url in ("https://e.com/a", "https://e.com/b", "https://e.com/c"):
        index.add(url, marks.md5, marks.simhash, 300)
    assert index.exact_groups() == [["https://e.com/a", "https://e.com/b",
                                     "https://e.com/c"]]
    # Exact duplicates are not reported a second time as near-duplicates.
    assert index.near_groups() == []


def test_index_excludes_pages_under_the_word_floor():
    index = ContentIndex()
    marks = fingerprint(FILLER)
    index.add("https://e.com/a", marks.md5, marks.simhash,
              MIN_WORDS_FOR_COMPARISON - 1)
    index.add("https://e.com/b", marks.md5, marks.simhash,
              MIN_WORDS_FOR_COMPARISON - 1)
    assert index.comparable() == []
    assert index.exact_groups() == []
    assert index.near_groups() == []


# --- end to end through a crawl --------------------------------------------

def test_exact_duplicate_pages_are_grouped(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(FILLER + ' <a href="/copy">Copy</a>'),
        BASE + "/copy": page(FILLER),
    })
    # The homepage carries one extra anchor word, so these are near, not exact.
    assert run.summary["content"]["pages_compared"] == 2

    groups = run.page_issues_of("near_duplicate_content")
    assert len(groups) == 1
    assert "2 pages are near-identical" in groups[0]["detail"]


def test_identical_bodies_are_an_exact_group(tmp_path):
    body = FILLER
    run = crawl_site(tmp_path, {
        BASE + "/": page(body + ' <a href="/a">x</a><a href="/b">x</a>'),
        BASE + "/a": page(body),
        BASE + "/b": page(body),
    })
    exact = run.page_issues_of("duplicate_content")
    assert len(exact) == 1
    assert "2 pages have identical text" in exact[0]["detail"]
    assert BASE + "/a" in exact[0]["detail"] and BASE + "/b" in exact[0]["detail"]
    assert run.summary["content"]["duplicate_groups"] == 1


def test_near_duplicates_differing_by_one_sentence(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page(FILLER + ' <a href="/near">n</a>'),
        BASE + "/near": page(FILLER + " One extra closing sentence here only."),
    })
    near = run.page_issues_of("near_duplicate_content")
    assert len(near) == 1
    assert near[0]["severity"] == "low"
    assert run.summary["content"]["near_duplicate_groups"] == 1


def test_short_pages_are_never_compared(tmp_path):
    run = crawl_site(tmp_path, {
        BASE + "/": page("short body" + ' <a href="/a">a</a>'),
        BASE + "/a": page("short body"),
    })
    assert run.summary["content"]["pages_compared"] == 0
    assert run.page_issues_of("duplicate_content") == []
    assert run.page_issues_of("near_duplicate_content") == []
    # They are still reported as thin, which is a different fault.
    assert run.page_issues_of("thin_page")


def test_different_pages_are_not_grouped(tmp_path):
    other = " ".join([f"completely different topic {i} entirely."
                      for i in range(60)])
    run = crawl_site(tmp_path, {
        BASE + "/": page(FILLER + ' <a href="/a">a</a>'),
        BASE + "/a": page(other),
    })
    assert run.page_issues_of("duplicate_content") == []
    assert run.page_issues_of("near_duplicate_content") == []
