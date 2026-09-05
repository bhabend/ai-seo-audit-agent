import requests
from bs4 import BeautifulSoup
import time
from config import CRAWL_DELAY

# Mimic a real browser to avoid blocking
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def get_urls_from_sitemap(sitemap_url):
    try:
        res = requests.get(sitemap_url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(res.text, "xml")

        urls = [loc.text for loc in soup.find_all("loc")]

        print(f"✅ Found {len(urls)} URLs in sitemap")
        return urls

    except Exception as e:
        print(f"❌ Failed to fetch sitemap: {e}")
        return []


def fetch_page(url, retries=3):
    for attempt in range(retries):
        try:
            start = time.time()

            res = requests.get(
                url,
                headers=HEADERS,
                timeout=20
            )

            load_time = int((time.time() - start) * 1000)

            # Small delay to avoid getting blocked
            time.sleep(CRAWL_DELAY)

            return {
                "url": url,
                "final_url": res.url,
                "status_code": res.status_code,
                "response_time_ms": load_time,
                "html": res.text
            }

        except Exception as e:
            print(f"⚠️ Retry {attempt + 1}/{retries} failed for {url}")
            print(f"   Error: {e}")

            # Wait before retry
            time.sleep(2)

    print(f"❌ Failed after {retries} attempts: {url}")
    return None


# -------------------------
# NEW: MAIN CRAWLER FUNCTION 🔥
# -------------------------
def crawl_site(start_url):
    """
    Main crawler:
    1. Fetch sitemap
    2. Crawl all URLs
    """

    # Try default sitemap location
    sitemap_url = start_url.rstrip("/") + "/sitemap.xml"

    urls = get_urls_from_sitemap(sitemap_url)

    if not urls:
        print("❌ No URLs found. Exiting crawl.")
        return []

    crawl_results = []

    print(f"🚀 Starting crawl for {len(urls)} pages...\n")

    for i, url in enumerate(urls, start=1):
        print(f"[{i}/{len(urls)}] Crawling: {url}")

        page_data = fetch_page(url)

        if page_data:
            crawl_results.append(page_data)

    print("\n✅ Crawl completed!")

    return crawl_results