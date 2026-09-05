from bs4 import BeautifulSoup
import json


def analyze_schema(html):
    soup = BeautifulSoup(html, "html.parser")

    schema_types = []
    has_schema = False

    scripts = soup.find_all("script", type="application/ld+json")

    for script in scripts:
        try:
            data = json.loads(script.string)

            if isinstance(data, dict):
                schema_type = data.get("@type")
                if schema_type:
                    schema_types.append(schema_type)

            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "@type" in item:
                        schema_types.append(item["@type"])

        except:
            continue

    if schema_types:
        has_schema = True

    # Normalize
    schema_types = list(set(schema_types))

    # Categorize important ones
    has_article_schema = any("Article" in str(t) for t in schema_types)
    has_faq_schema = any("FAQPage" in str(t) for t in schema_types)
    has_medical_schema = any("Medical" in str(t) for t in schema_types)

    return {
        "has_schema": has_schema,
        "schema_types": ", ".join(schema_types) if schema_types else None,
        "has_article_schema": has_article_schema,
        "has_faq_schema": has_faq_schema,
        "has_medical_schema": has_medical_schema
    }