import json
import os
import re
import requests


def fallback_metadata(caption: str):
    base = (caption or "Wait for the ending").strip()
    base = re.sub(r"\s+", " ", base)[:70]
    title = base if base else "Wait for the ending 😂"
    return {
        "title": title,
        "description": f"{title}\n\nFollow for more daily reaction videos.",
        "hashtags": ["#funny", "#reaction", "#reels", "#viral", "#comedy"],
    }


def generate_metadata(caption: str):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return fallback_metadata(caption)

    model = os.getenv("OPENAI_MODEL", "gpt-5")
    prompt = f"""Create Facebook Reel metadata for a reaction video.
Source context: {caption or 'funny short-form clip'}
Return ONLY JSON with keys title, description, hashtags.
Rules: catchy but not misleading; title under 80 characters; description 1-2 short sentences; 4-6 relevant hashtags; do not claim ownership of the source clip."""
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "input": prompt, "store": False},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    text_parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text_parts.append(content.get("text", ""))
    text = "\n".join(text_parts).strip()
    try:
        return json.loads(text)
    except Exception:
        return fallback_metadata(caption)
