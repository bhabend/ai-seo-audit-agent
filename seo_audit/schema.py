"""JSON-LD structured data: what is declared, and whether it is complete.

JSON-LD only. Microdata and RDFa are out of scope and are not read at all, so
a page marked up entirely in microdata reports as having no schema -- the
README says so rather than the tool pretending otherwise.

The prototype's mistake was treating presence as validity: it recorded that a
page had an Organization block without ever asking whether the block said who
the organization was. Required properties are checked here for the types
below; unknown types are listed, never judged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# type -> the properties Google's rich-result docs treat as required.
# A tuple inside the list means "any one of these will do".
REQUIRED_PROPERTIES: Dict[str, List[Any]] = {
    "Organization": ["name", "url"],
    "WebSite": ["name", "url"],
    "WebPage": [("name", "headline")],
    "BreadcrumbList": ["itemListElement"],
    "FAQPage": ["mainEntity"],
    "Article": ["headline", "datePublished", "author"],
    "BlogPosting": ["headline", "datePublished", "author"],
    "Product": ["name", ("offers", "aggregateRating")],
    "Service": ["name", "provider"],
    "LocalBusiness": ["name", "address"],
    "Place": ["name", "address"],
}

# Nested shapes that need more than a present key.
BREADCRUMB_ITEM_PROPERTIES = ("position", "name", "item")
FAQ_ITEM_PROPERTIES = ("name", "acceptedAnswer")


@dataclass
class SchemaResult:
    """What one page's JSON-LD amounts to."""

    types: List[str] = field(default_factory=list)
    block_count: int = 0
    invalid_count: int = 0
    invalid_details: List[str] = field(default_factory=list)
    # (type, property) pairs that a declared type should have carried.
    missing_properties: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def has_schema(self) -> bool:
        return self.block_count > 0 and self.block_count > self.invalid_count


def _types_of(node: Dict) -> List[str]:
    """@type as a list, since it may be a string or a list of strings."""
    raw = node.get("@type")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def iter_nodes(value: Any) -> Iterable[Dict]:
    """Every dict in a JSON-LD document, unwrapping @graph, arrays and nesting.

    A page's Organization can sit three levels down inside an @graph inside a
    list; a flat scan of top-level blocks would miss it.
    """
    if isinstance(value, list):
        for item in value:
            yield from iter_nodes(item)
        return
    if not isinstance(value, dict):
        return

    graph = value.get("@graph")
    if graph is not None:
        yield from iter_nodes(graph)

    if _types_of(value):
        yield value

    for key, child in value.items():
        if key in ("@graph", "@context", "@type"):
            continue
        if isinstance(child, (dict, list)):
            yield from iter_nodes(child)


def _has(node: Dict, prop: Any) -> bool:
    """Is the property present and not empty? A tuple means any one of them."""
    if isinstance(prop, tuple):
        return any(_has(node, p) for p in prop)
    value = node.get(prop)
    if value is None:
        return False
    if isinstance(value, (str, list, dict)):
        return len(value) > 0
    return True


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _check_breadcrumb(node: Dict) -> List[str]:
    """A BreadcrumbList needs items that actually say where they point."""
    missing = []
    items = _as_list(node.get("itemListElement"))
    if not items:
        return ["itemListElement"]
    for item in items:
        if not isinstance(item, dict):
            missing.append("itemListElement")
            break
        for prop in BREADCRUMB_ITEM_PROPERTIES:
            # The last crumb may legitimately omit `item`: it is the page.
            if prop == "item" and item is items[-1]:
                continue
            if not _has(item, prop):
                missing.append(f"itemListElement.{prop}")
    return missing


def _check_faq(node: Dict) -> List[str]:
    """Every FAQ entry needs a question and an answer with text in it."""
    missing = []
    entries = _as_list(node.get("mainEntity"))
    if not entries:
        return ["mainEntity"]
    for entry in entries:
        if not isinstance(entry, dict):
            missing.append("mainEntity")
            break
        if not _has(entry, "name"):
            missing.append("mainEntity.name")
        answer = entry.get("acceptedAnswer")
        if not isinstance(answer, dict) or not _has(answer, "text"):
            missing.append("mainEntity.acceptedAnswer.text")
    return missing


def check_node(node: Dict) -> List[Tuple[str, str]]:
    """Required properties a node's declared types should have carried."""
    problems: List[Tuple[str, str]] = []
    for type_name in _types_of(node):
        required = REQUIRED_PROPERTIES.get(type_name)
        if required is None:
            continue  # unknown type: listed, not judged
        for prop in required:
            if not _has(node, prop):
                label = " or ".join(prop) if isinstance(prop, tuple) else prop
                problems.append((type_name, label))
        if type_name == "BreadcrumbList":
            problems.extend((type_name, p) for p in _check_breadcrumb(node))
        elif type_name == "FAQPage":
            problems.extend((type_name, p) for p in _check_faq(node))
    # One report per (type, property), however deeply the shape repeats.
    return sorted(set(problems))


def parse_schema(soup) -> SchemaResult:
    """Read every JSON-LD block on the page and check what it declares."""
    result = SchemaResult()
    types: Set[str] = set()
    problems: Set[Tuple[str, str]] = set()

    for block in soup.find_all("script", attrs={"type": True}):
        if block.get("type", "").strip().lower() != "application/ld+json":
            continue
        result.block_count += 1
        raw = block.string or block.get_text() or ""
        try:
            document = json.loads(raw)
        except (ValueError, TypeError) as exc:
            result.invalid_count += 1
            if len(result.invalid_details) < 3:
                result.invalid_details.append(str(exc)[:120])
            continue

        for node in iter_nodes(document):
            for type_name in _types_of(node):
                types.add(type_name)
            problems.update(check_node(node))

    result.types = sorted(types)
    result.missing_properties = sorted(problems)
    return result
