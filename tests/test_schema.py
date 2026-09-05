"""JSON-LD: what is declared, and whether the declaration is complete."""

from seo_audit.schema import parse_schema
from seo_audit.urlnorm import make_soup


def schema_for(*blocks):
    body = "".join(
        f'<script type="application/ld+json">{b}</script>' for b in blocks)
    return parse_schema(make_soup(f"<html><head>{body}</head><body>x</body></html>"))


def test_types_are_collected_sorted_and_deduped():
    result = schema_for(
        '{"@type": "Organization", "name": "Acme", "url": "https://acme.com"}',
        '{"@type": "Organization", "name": "Acme", "url": "https://acme.com"}',
        '{"@type": "WebSite", "name": "Acme", "url": "https://acme.com"}',
    )
    assert result.types == ["Organization", "WebSite"]
    assert result.block_count == 3
    assert result.invalid_count == 0
    assert result.missing_properties == []


def test_graph_and_nested_types_are_unwrapped():
    block = """{
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "Organization", "name": "Acme", "url": "https://acme.com"},
        {"@type": "WebPage", "name": "Home",
         "breadcrumb": {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home",
             "item": "https://acme.com/"}]}}
      ]}"""
    result = schema_for(block)
    assert result.types == ["BreadcrumbList", "ListItem", "Organization",
                            "WebPage"]
    assert result.missing_properties == []


def test_type_may_be_a_list_and_arrays_are_walked():
    result = schema_for('[{"@type": ["LocalBusiness", "Organization"],'
                        ' "name": "Acme", "url": "https://acme.com",'
                        ' "address": "1 Road"}]')
    assert result.types == ["LocalBusiness", "Organization"]
    assert result.missing_properties == []


def test_invalid_json_is_counted_not_fatal():
    result = schema_for('{"@type": "Organization", "name": ',
                        '{"@type": "WebSite", "name": "A", "url": "https://a.com"}')
    assert result.block_count == 2
    assert result.invalid_count == 1
    assert result.invalid_details
    assert result.types == ["WebSite"]  # the good block still counted


def test_no_blocks_at_all():
    result = parse_schema(make_soup("<html><body>nothing</body></html>"))
    assert result.block_count == 0
    assert result.types == []
    assert result.has_schema is False


def test_other_script_types_are_not_read_as_json_ld():
    soup = make_soup('<html><head><script type="application/json">{"a":1}'
                     '</script><script>var x = 1;</script></head></html>')
    assert parse_schema(soup).block_count == 0


# --- one case per checked type ---------------------------------------------

def test_organization_requires_name_and_url():
    assert schema_for('{"@type": "Organization"}').missing_properties == [
        ("Organization", "name"), ("Organization", "url")]
    assert schema_for('{"@type": "Organization", "name": "A",'
                      ' "url": "https://a.com"}').missing_properties == []


def test_website_requires_name_and_url():
    assert ("WebSite", "url") in schema_for(
        '{"@type": "WebSite", "name": "A"}').missing_properties


def test_webpage_accepts_either_name_or_headline():
    assert schema_for('{"@type": "WebPage", "headline": "H"}'
                      ).missing_properties == []
    assert schema_for('{"@type": "WebPage", "name": "N"}'
                      ).missing_properties == []
    assert schema_for('{"@type": "WebPage"}').missing_properties == [
        ("WebPage", "name or headline")]


def test_breadcrumb_items_need_position_name_and_item():
    missing = schema_for("""{"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "name": "Home", "item": "https://a.com/"},
        {"@type": "ListItem", "position": 2, "item": "https://a.com/b"}]}"""
                         ).missing_properties
    assert ("BreadcrumbList", "itemListElement.position") in missing
    assert ("BreadcrumbList", "itemListElement.name") in missing


def test_last_breadcrumb_may_omit_item():
    assert schema_for("""{"@type": "BreadcrumbList", "itemListElement": [
        {"position": 1, "name": "Home", "item": "https://a.com/"},
        {"position": 2, "name": "Here"}]}""").missing_properties == []


def test_empty_breadcrumb_reports_the_container():
    assert schema_for('{"@type": "BreadcrumbList"}').missing_properties == [
        ("BreadcrumbList", "itemListElement")]


def test_faq_entries_need_a_question_and_an_answer_with_text():
    missing = schema_for("""{"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": "Why?",
         "acceptedAnswer": {"@type": "Answer", "text": "Because."}},
        {"@type": "Question", "acceptedAnswer": {"@type": "Answer"}}]}"""
                         ).missing_properties
    assert ("FAQPage", "mainEntity.name") in missing
    assert ("FAQPage", "mainEntity.acceptedAnswer.text") in missing


def test_article_and_blogposting_require_headline_date_and_author():
    for type_name in ("Article", "BlogPosting"):
        missing = schema_for(f'{{"@type": "{type_name}", "headline": "H"}}'
                             ).missing_properties
        assert (type_name, "datePublished") in missing
        assert (type_name, "author") in missing


def test_product_needs_name_and_either_offers_or_rating():
    assert schema_for('{"@type": "Product", "name": "P",'
                      ' "offers": {"price": "1"}}').missing_properties == []
    assert schema_for('{"@type": "Product", "name": "P",'
                      ' "aggregateRating": {"ratingValue": "4"}}'
                      ).missing_properties == []
    assert schema_for('{"@type": "Product", "name": "P"}'
                      ).missing_properties == [
        ("Product", "offers or aggregateRating")]


def test_service_requires_name_and_provider():
    assert ("Service", "provider") in schema_for(
        '{"@type": "Service", "name": "S"}').missing_properties


def test_localbusiness_and_place_require_name_and_address():
    for type_name in ("LocalBusiness", "Place"):
        assert (type_name, "address") in schema_for(
            f'{{"@type": "{type_name}", "name": "N"}}').missing_properties


def test_unknown_types_are_listed_but_never_judged():
    result = schema_for('{"@type": "SoftwareApplication"}')
    assert result.types == ["SoftwareApplication"]
    assert result.missing_properties == []


def test_empty_values_do_not_count_as_present():
    assert ("Organization", "name") in schema_for(
        '{"@type": "Organization", "name": "", "url": "https://a.com"}'
    ).missing_properties
