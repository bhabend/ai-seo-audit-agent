from collections import defaultdict
from urllib.parse import urlparse
from bs4 import BeautifulSoup


# -------------------------
# EXISTING (IMPROVED)
# -------------------------
def build_link_graph(data_rows):
    in_links = defaultdict(int)
    out_links = {}

    all_urls = set(row["url"] for row in data_rows)

    for row in data_rows:
        url = row["url"]
        links = row.get("internal_links_list", [])

        valid_links = []

        for link in links:
            # Exact match instead of partial match (fix)
            if link in all_urls:
                valid_links.append(link)
                in_links[link] += 1

        out_links[url] = len(valid_links)

    return in_links, out_links


def detect_orphan_pages(data_rows, in_links):
    orphan_pages = []

    for row in data_rows:
        url = row["url"]
        if in_links.get(url, 0) == 0:
            orphan_pages.append(url)

    return orphan_pages


def classify_page_type(url):
    if url.endswith(".com.au/"):
        return "homepage"
    elif "/blog/" in url:
        return "blog"
    elif "/treatments/" in url:
        return "treatment"
    elif any(x in url for x in ["privacy", "terms", "contact"]):
        return "legal"
    else:
        return "other"


# -------------------------
# NEW (ADVANCED LINK LAYER 🔥)
# -------------------------

def extract_internal_links_with_anchors(row, domain):
    html = row.get("html")
    source_url = row.get("url")

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        anchor = a_tag.get_text(strip=True).lower()

        # Normalize relative URLs
        if href.startswith("/"):
            href = domain + href

        # Remove fragments
        href = href.split("#")[0]

        # Keep only internal links
        if domain in href:
            links.append({
                "source": source_url,
                "target": href,
                "anchor": anchor
            })

    return links


def analyze_internal_links(df, domain):
    """
    Full link intelligence layer
    """

    all_links = []

    # -------------------------
    # Build link graph
    # -------------------------
    for _, row in df.iterrows():
        links = extract_internal_links_with_anchors(row, domain)
        all_links.extend(links)

    # -------------------------
    # Maps
    # -------------------------
    inlink_map = defaultdict(list)
    outlink_map = defaultdict(list)
    anchor_map = defaultdict(list)

    for link in all_links:
        src = link["source"]
        tgt = link["target"]
        anchor = link["anchor"]

        outlink_map[src].append(tgt)
        inlink_map[tgt].append(src)
        anchor_map[tgt].append(anchor)

    # -------------------------
    # Populate dataframe
    # -------------------------
    df["internal_inlinks"] = 0
    df["internal_outlinks"] = 0
    df["anchor_texts"] = None

    for idx, row in df.iterrows():
        url = row["url"]

        inlinks = inlink_map.get(url, [])
        outlinks = outlink_map.get(url, [])
        anchors = anchor_map.get(url, [])

        df.at[idx, "internal_inlinks"] = len(inlinks)
        df.at[idx, "internal_outlinks"] = len(outlinks)
        df.at[idx, "anchor_texts"] = ", ".join(set(anchors)) if anchors else None

    # -------------------------
    # Link Score (importance)
    # -------------------------
    max_inlinks = df["internal_inlinks"].max()

    if max_inlinks > 0:
        df["link_score"] = (df["internal_inlinks"] / max_inlinks) * 100
    else:
        df["link_score"] = 0

    # -------------------------
    # Flags
    # -------------------------
    df["is_weak_page"] = df["internal_inlinks"] < 2
    df["is_overlinked"] = df["internal_outlinks"] > 100

    return df