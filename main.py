import pandas as pd
from urllib.parse import urlparse

from crawler import crawl_site
from parser import parse_html
from enrichment import enrich_row
from duplicate import detect_duplicates
from sitewide import check_sitewide
from scoring import calculate_seo_score
from link_analyzer import analyze_internal_links  # ✅ NEW


def run_audit(start_url):

    # -------------------------
    # Crawl
    # -------------------------
    crawl_data = crawl_site(start_url)

    rows = []

    for item in crawl_data:
        url = item.get("url")
        html = item.get("html")
        status_code = item.get("status_code")

        if not html:
            continue

        # -------------------------
        # Parse
        # -------------------------
        parsed = parse_html(url, html)

        parsed["html"] = html
        parsed["status_code"] = status_code

        rows.append(parsed)

    df = pd.DataFrame(rows)

    # -------------------------
    # Enrichment (Technical + Schema)
    # -------------------------
    df = df.apply(enrich_row, axis=1)

    # -------------------------
    # Duplicate Detection
    # -------------------------
    df = detect_duplicates(df)

    # -------------------------
    # Internal Link Analysis (NEW 🔥)
    # -------------------------
    parsed_domain = urlparse(start_url)
    domain = f"{parsed_domain.scheme}://{parsed_domain.netloc}"

    df = analyze_internal_links(df, domain)

    # -------------------------
    # Sitewide Signals
    # -------------------------
    sitewide_data = check_sitewide(start_url)

    for key, value in sitewide_data.items():
        df[key] = value

    # -------------------------
    # SEO Scoring
    # -------------------------
    df["seo_score"] = df.apply(calculate_seo_score, axis=1)

    # -------------------------
    # Cleanup
    # -------------------------
    df = df.drop(columns=["html"], errors="ignore")

    return df


if __name__ == "__main__":
    print("[START] Running automated SEO audit pipeline...")

    url = "https://www.directmeds.com.au/"
    df = run_audit(url)

    df.to_csv("output/audit_report_v3.csv", index=False)

    print("[DONE] Audit completed. Output saved to output/audit_report_v3.csv")
