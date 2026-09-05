def calculate_seo_score(row):

    score = 0

    # -------------------------
    # 1. INDEXABILITY (25)
    # -------------------------
    if row.get("is_indexable"):
        score += 25
    else:
        score += 0

    # -------------------------
    # 2. TECHNICAL SEO (20)
    # -------------------------
    tech_score = 20

    if not row.get("has_canonical"):
        tech_score -= 8

    if not row.get("canonical_match"):
        tech_score -= 4

    if row.get("redirect_chain", 0) > 1:
        tech_score -= 4

    if row.get("meta_robots") and "noindex" in row.get("meta_robots"):
        tech_score -= 4

    tech_score = max(tech_score, 0)
    score += tech_score

    # -------------------------
    # 3. ON-PAGE SEO (20)
    # -------------------------
    onpage_score = 20

    if not row.get("title"):
        onpage_score -= 6

    if not row.get("meta_description"):
        onpage_score -= 4

    if row.get("h1_count", 0) == 0:
        onpage_score -= 6

    if row.get("h1_count", 0) > 1:
        onpage_score -= 2

    if row.get("image_missing_alt_count", 0) > 0:
        onpage_score -= 2

    onpage_score = max(onpage_score, 0)
    score += onpage_score

    # -------------------------
    # 4. CONTENT QUALITY (15)
    # -------------------------
    content_score = 15

    word_count = row.get("word_count", 0)

    if word_count < 300:
        content_score -= 8
    elif word_count < 600:
        content_score -= 4

    content_score = max(content_score, 0)
    score += content_score

    # -------------------------
    # 5. SCHEMA (10)
    # -------------------------
    schema_score = 10

    if not row.get("has_schema"):
        schema_score -= 6

    if not row.get("has_article_schema") and not row.get("has_medical_schema"):
        schema_score -= 2

    schema_score = max(schema_score, 0)
    score += schema_score

    # -------------------------
    # 6. DUPLICATE CONTENT (10)
    # -------------------------
    duplicate_score = 10

    if row.get("is_duplicate"):
        duplicate_score = 0

    score += duplicate_score

    # -------------------------
    # FINAL SCORE
    # -------------------------
    return round(score, 2)