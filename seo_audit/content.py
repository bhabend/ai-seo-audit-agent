"""Duplicate and near-duplicate detection by fingerprint.

The prototype compared exact hashes, which finds nothing on a real site: two
product pages differing by one model number are not byte-identical, and that
is exactly the pair worth reporting. So each page gets two fingerprints:

  * an md5 of its normalised text, which finds true duplicates;
  * a 64-bit simhash of its word shingles, where near-duplicates land within
    a few bits of each other and can be grouped by Hamming distance.

Both are computed in the worker and are all that survives it. The page text
itself is never stored, and 16 bytes per page is the whole cost.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Shorter than this and the text is not worth comparing: navigation furniture
# repeats across every page and would group the whole site as duplicates.
MIN_WORDS_FOR_COMPARISON = 200

# Two pages within this Hamming distance of each other are near-duplicates.
NEAR_DUPLICATE_DISTANCE = 3

SHINGLE_SIZE = 4
HASH_BITS = 64
_MASK = (1 << HASH_BITS) - 1

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def normalise_text(text: str) -> str:
    """Lower-cased words joined by single spaces: punctuation and layout out."""
    return " ".join(_WORD_RE.findall((text or "").lower()))


def text_md5(text: str) -> str:
    """Exact fingerprint of the normalised text."""
    return hashlib.md5(normalise_text(text).encode("utf-8")).hexdigest()


def _shingles(words: Sequence[str], size: int = SHINGLE_SIZE) -> Iterable[str]:
    if len(words) < size:
        if words:
            yield " ".join(words)
        return
    for index in range(len(words) - size + 1):
        yield " ".join(words[index:index + size])


def simhash(text: str, size: int = SHINGLE_SIZE) -> int:
    """64-bit simhash over overlapping word shingles.

    Overlapping shingles rather than bare words, so that word order matters:
    two pages with the same vocabulary in a different arrangement are not
    reported as the same page.
    """
    words = normalise_text(text).split()
    if not words:
        return 0

    vector = [0] * HASH_BITS
    for shingle in _shingles(words, size):
        digest = hashlib.md5(shingle.encode("utf-8")).digest()[:8]
        value = int.from_bytes(digest, "big")
        for bit in range(HASH_BITS):
            if value >> bit & 1:
                vector[bit] += 1
            else:
                vector[bit] -= 1

    result = 0
    for bit in range(HASH_BITS):
        if vector[bit] > 0:
            result |= 1 << bit
    return result & _MASK


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & _MASK).count("1")


@dataclass
class ContentFingerprint:
    """What one page's text reduces to. Computed in the worker, text dropped."""

    simhash: int = 0
    md5: str = ""
    word_count: int = 0

    @property
    def simhash_hex(self) -> str:
        return format(self.simhash, "016x")


def fingerprint(text: str, word_count: Optional[int] = None) -> ContentFingerprint:
    normalised = normalise_text(text)
    words = normalised.split()
    return ContentFingerprint(
        simhash=simhash(text),
        md5=hashlib.md5(normalised.encode("utf-8")).hexdigest(),
        word_count=word_count if word_count is not None else len(words),
    )


@dataclass
class ContentIndex:
    """Fingerprints for every parsed page, compared once the crawl is done."""

    entries: List[Tuple[str, str, int, int]] = field(default_factory=list)

    def add(self, final_url: str, md5: str, simhash_value: int,
            word_count: int) -> None:
        self.entries.append((final_url, md5, simhash_value, word_count))

    def comparable(self) -> List[Tuple[str, str, int, int]]:
        """Only pages with enough text to be worth comparing."""
        return [e for e in self.entries if e[3] >= MIN_WORDS_FOR_COMPARISON]

    def exact_groups(self) -> List[List[str]]:
        """Pages whose normalised text is byte-identical."""
        buckets: Dict[str, List[str]] = defaultdict(list)
        for final_url, md5, _sim, _words in self.comparable():
            buckets[md5].append(final_url)
        return [sorted(urls) for _md5, urls in sorted(buckets.items())
                if len(urls) > 1]

    def near_groups(self, distance: int = NEAR_DUPLICATE_DISTANCE
                    ) -> List[List[str]]:
        """Groups of pages within `distance` bits, excluding exact duplicates.

        Single-link clustering: a page joins a group if it is close to any
        member. Comparison is quadratic in the number of comparable pages,
        which is fine at the 2,000-page cap and is why the cap exists.
        """
        entries = self.comparable()
        exact = {url for group in self.exact_groups() for url in group}

        parent: Dict[str, str] = {}

        def find(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(a: str, b: str) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        for final_url, _md5, _sim, _words in entries:
            parent.setdefault(final_url, final_url)

        for i in range(len(entries)):
            url_a, md5_a, sim_a, _wa = entries[i]
            for j in range(i + 1, len(entries)):
                url_b, md5_b, sim_b, _wb = entries[j]
                if md5_a == md5_b:
                    continue  # already reported as an exact duplicate
                if hamming(sim_a, sim_b) <= distance:
                    union(url_a, url_b)

        groups: Dict[str, List[str]] = defaultdict(list)
        for final_url in parent:
            groups[find(final_url)].append(final_url)

        out = []
        for members in groups.values():
            if len(members) < 2:
                continue
            if all(m in exact for m in members):
                continue
            out.append(sorted(members))
        return sorted(out)
