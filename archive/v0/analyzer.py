from openai import OpenAI
from config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)

def analyze_page(data):
    if not OPENAI_API_KEY:
        return "No API key provided"

    prompt = f"""
    Audit this page for SEO:

    URL: {data['url']}
    Title: {data['title']}
    Meta: {data['meta_description']}
    H1: {data['h1_text']}
    H2: {data['h2_text']}
    Word Count: {data['word_count']}

    Give:
    - Key issues
    - Missing topics
    - Improvements
    """

    try:
        res = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        return res.choices[0].message.content

    except Exception as e:
        return f"AI analysis failed: {str(e)}"