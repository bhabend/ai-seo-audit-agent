import hashlib


def generate_content_hash(text):
    if not text:
        return None

    # Normalize content
    text = text.lower().strip()

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def detect_duplicates(df):
    """
    Takes full dataframe AFTER parsing
    """

    # Create hash column
    df["content_hash"] = df["clean_text"].apply(generate_content_hash)

    # Find duplicates
    hash_counts = df["content_hash"].value_counts()

    duplicate_hashes = hash_counts[hash_counts > 1].index

    df["is_duplicate"] = df["content_hash"].isin(duplicate_hashes)

    # Group duplicates
    df["duplicate_group"] = df["content_hash"].where(df["is_duplicate"], None)

    return df