import requests


def check_sitewide(base_url):
    base_url = base_url.rstrip("/")

    robots_url = f"{base_url}/robots.txt"
    sitemap_url = f"{base_url}/sitemap.xml"

    robots_txt_found = False
    sitemap_found = False
    sitemap_url_count = 0

    # -------------------------
    # Robots.txt
    # -------------------------
    try:
        r = requests.get(robots_url, timeout=5)
        if r.status_code == 200:
            robots_txt_found = True
    except:
        pass

    # -------------------------
    # Sitemap.xml
    # -------------------------
    try:
        s = requests.get(sitemap_url, timeout=5)
        if s.status_code == 200:
            sitemap_found = True

            # basic URL count
            sitemap_url_count = s.text.count("<loc>")
    except:
        pass

    return {
        "robots_txt_found": robots_txt_found,
        "sitemap_found": sitemap_found,
        "sitemap_url_count": sitemap_url_count
    }