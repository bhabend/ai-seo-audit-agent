from bs4 import BeautifulSoup


def parse_html(url, html):
    soup = BeautifulSoup(html, "html.parser")

    # -------------------------
    # Title
    # -------------------------
    title = soup.title.string.strip() if soup.title and soup.title.string else None

    # -------------------------
    # Meta Description
    # -------------------------
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_desc_tag["content"].strip() if meta_desc_tag and meta_desc_tag.get("content") else None

    # -------------------------
    # Headings
    # -------------------------
    h1_tags = [h.get_text(strip=True) for h in soup.find_all("h1")]
    h2_tags = [h.get_text(strip=True) for h in soup.find_all("h2")]

    # -------------------------
    # Images
    # -------------------------
    images = soup.find_all("img")
    image_count = len(images)
    missing_alt = len([img for img in images if not img.get("alt")])

    # -------------------------
    # Clean Text (IMPORTANT 🚨)
    # -------------------------
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()

    clean_text = soup.get_text(separator=" ")
    clean_text = " ".join(clean_text.split())

    word_count = len(clean_text.split())

    return {
        "url": url,
        "title": title,
        "meta_description": meta_description,
        "h1_count": len(h1_tags),
        "h2_count": len(h2_tags),
        "image_count": image_count,
        "image_missing_alt_count": missing_alt,
        "word_count": word_count,
        "clean_text": clean_text
    }