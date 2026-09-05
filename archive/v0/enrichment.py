from technical import analyze_technical
from schema import analyze_schema


def enrich_row(row):
    url = row.get("url")
    html = row.get("html")
    status_code = row.get("status_code")

    if not html:
        return row

    # -------------------------
    # Technical SEO
    # -------------------------
    tech_data = analyze_technical(url, html, status_code)

    # -------------------------
    # Schema
    # -------------------------
    schema_data = analyze_schema(html)

    # Merge everything
    row.update(tech_data)
    row.update(schema_data)

    return row