from bs4 import BeautifulSoup
import requests


def analyze_technical(url, html, status_code):
    soup = BeautifulSoup(html, "html.parser")

    # -------------------------
    # Canonical Tag
    # -------------------------
    canonical_tag = soup.find("link", rel="canonical")
    canonical_url = canonical_tag["href"].strip() if canonical_tag and canonical_tag.get("href") else None

    has_canonical = canonical_url is not None
    canonical_match = (canonical_url == url) if canonical_url else False

    # -------------------------
    # Meta Robots
    # -------------------------
    meta_robots_tag = soup.find("meta", attrs={"name": "robots"})
    meta_robots = meta_robots_tag["content"].lower() if meta_robots_tag and meta_robots_tag.get("content") else "index, follow"

    is_indexable = "noindex" not in meta_robots and status_code == 200

    # -------------------------
    # Hreflang
    # -------------------------
    hreflang_tags = soup.find_all("link", rel="alternate")
    hreflang_count = len([tag for tag in hreflang_tags if tag.get("hreflang")])

    # -------------------------
    # Redirect Chain (basic)
    # -------------------------
    redirect_chain = 0
    try:
        response = requests.get(url, allow_redirects=True, timeout=5)
        redirect_chain = len(response.history)
    except:
        redirect_chain = -1

    # -------------------------
    # Technical Issues Summary
    # -------------------------
    issues = []

    if not has_canonical:
        issues.append("Missing canonical")

    if has_canonical and not canonical_match:
        issues.append("Canonical mismatch")

    if not is_indexable:
        issues.append("Noindex or non-200")

    if redirect_chain > 1:
        issues.append("Redirect chain")

    technical_issues = ", ".join(issues) if issues else "None"

    return {
        "has_canonical": has_canonical,
        "canonical_url": canonical_url,
        "canonical_match": canonical_match,
        "meta_robots": meta_robots,
        "is_indexable": is_indexable,
        "hreflang_count": hreflang_count,
        "redirect_chain": redirect_chain,
        "technical_issues": technical_issues
    }